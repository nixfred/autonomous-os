"""GPIO button handler with device-declared wiring."""

import logging
import threading
import time

from hal.board.gpio_button import ButtonConfig
from hal.drivers.button_actions import (
    HoldLEDFeedback,
    DOUBLE_CLICK_WINDOW,
    button_hold_tier,
    button_hold_release_action,
    announce_listening_cue,
    single_click_action,
    triple_click_action,
)

from hal.drivers.device_tap_actions import physical_short_tap

logger = logging.getLogger(__name__)

# Quiet time after the last edge before the line level is read back. Contact bounce on
# the Intern 2 button settles within ~5 ms; a dropped edge is corrected this long after
# the line goes quiet.
SETTLE_S = 0.05

class GPIOButtonHandler:
    def __init__(self, config: ButtonConfig, *, name="primary",
                 behavior="standard", hold_s=5.0, factory_reset=True):
        if behavior not in ("standard", "factory_reset"):
            raise ValueError(f"Unknown GPIO button behavior: {behavior}")
        self._behavior = behavior
        self._hold_s = hold_s
        self._factory_reset = factory_reset
        self._source = ("GPIO button" if name == "primary" and behavior == "standard" else
                        f"GPIO button {name} (gpiochip{config.chip}/line{config.line})")
        self._stopped = False
        self._action_lock = threading.RLock()
        self._lgpio = None
        self._handle = None
        self._callback = None
        self._click_count = 0
        self._click_timer = None
        self._press_start = 0
        # Track whether we've seen the press edge so a stray release edge
        # (debounce-dropped press) doesn't fire stale held-duration actions.
        self._pressed = False
        # Hold-duration LED watcher. Each press creates a new threading.Event
        # (per-watcher stop) so the previous watcher exits cleanly without
        # racing the new one. None when no hold is active.
        self._hold_watcher_stop = None
        self._hold_lock = threading.Lock()
        self._hold_led = HoldLEDFeedback()
        self._chip = config.chip
        self._pin = config.line
        self._debounce_ns = config.debounce_ns
        # One debounce clock for both directions: edges inside the window after an
        # accepted transition are contact bounce. Separate press/release clocks let a
        # release bounce end a tap and a later press bounce start a phantom hold.
        self._last_transition_tick = None
        self._last_edge_tick = None
        self._last_edge_at = 0.0
        # Serializes state changes from the lgpio callback, the settle timer and the
        # hold watcher. Order: _state_lock before _hold_lock.
        self._state_lock = threading.RLock()
        self._settle_timer = None

    def _hold_watcher(self, stop_event):
        """Report hold tiers; shared feedback owns all LED animation.

        Also confirms the hold against the line itself: once the line has been quiet for
        SETTLE_S and reads released, the hold ends there, so a lost release edge can never
        grow into a long hold."""
        last_stage = 0
        while not stop_event.is_set():
            if self._read_level() == 1 and time.monotonic() - self._last_edge_at >= SETTLE_S:
                self._reconcile()
                return
            with self._hold_lock:
                if stop_event.is_set() or self._hold_watcher_stop is not stop_event:
                    return
                held = time.monotonic() - self._press_start
                stage = button_hold_tier(held, behavior=self._behavior, hold_s=self._hold_s,
                                         factory_reset=self._factory_reset)
                if stage != last_stage:
                    self._hold_led.set_tier(stage)
                    last_stage = stage
            if stop_event.wait(timeout=0.1):
                return

    def _run_hold_action(self, held):
        with self._action_lock:
            if self._stopped:
                return
            logger.info("%s released hold %.3fs -- %s", self._source, held, self._behavior)
            button_hold_release_action(
                held, self._hold_led, behavior=self._behavior,
                hold_s=self._hold_s, source=self._source,
                factory_reset=self._factory_reset,
            )

    def _run_single_click(self):
        with self._action_lock:
            if not self._stopped:
                from hal import config
                if getattr(config, "VOICE_INPUT_MODE", "automatic") == "tap_to_talk":
                    physical_short_tap(source=self._source, announce=False)
                else:
                    single_click_action(source=self._source, announce=False)

    @staticmethod
    def _device_tap_mode():
        from hal import config
        import hal.app_state as state
        return (getattr(config, "VOICE_INPUT_MODE", "automatic") == "tap_to_talk"
                and bool(state.voice_service and state.voice_service.device_input.enabled))

    def start(self):
        import lgpio

        self._lgpio = lgpio
        try:
            self._handle = lgpio.gpiochip_open(self._chip)
            lgpio.gpio_claim_alert(
                self._handle, self._pin, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP
            )
            self._callback = lgpio.callback(
                self._handle, self._pin, lgpio.BOTH_EDGES, self._on_edge
            )
        except Exception:
            self.stop()
            raise
        logger.info(
            "%s ready on gpiochip%d line %d (manual debounce %d ms, behavior=%s, hold=%.1fs)",
            self._source, self._chip, self._pin, self._debounce_ns // 1_000_000,
            self._behavior, self._hold_s,
        )

    def stop(self):
        """Cancel pending work; actions already executing cannot be recalled."""
        self._stopped = True
        with self._state_lock:
            if self._settle_timer is not None:
                self._settle_timer.cancel()
                self._settle_timer = None
        with self._hold_lock:
            self._pressed = False
            if self._hold_watcher_stop is not None:
                self._hold_watcher_stop.set()
                self._hold_watcher_stop = None
            self._hold_led.stop()
            if self._click_timer is not None:
                self._click_timer.cancel()
                self._click_timer = None
            self._click_count = 0
        if self._callback is not None:
            try:
                self._callback.cancel()
            except Exception:
                logger.warning("%s callback cleanup failed", self._source, exc_info=True)
            finally:
                self._callback = None
        if self._handle is not None:
            try:
                self._lgpio.gpiochip_close(self._handle)
            except Exception:
                logger.warning("%s chip cleanup failed", self._source, exc_info=True)
            finally:
                self._handle = None

    def _read_level(self):
        """Current line level (0 pressed, 1 released), or None when it cannot be read."""
        if self._handle is None or self._lgpio is None:
            return None
        try:
            level = self._lgpio.gpio_read(self._handle, self._pin)
        except Exception:
            return None
        return level if level in (0, 1) else None

    def _arm_settle_check(self):
        """(Re)start the read-back that runs SETTLE_S after the latest edge."""
        if self._settle_timer is not None:
            self._settle_timer.cancel()
        timer = threading.Timer(SETTLE_S, self._reconcile)
        timer.daemon = True
        self._settle_timer = timer
        timer.start()

    def _reconcile(self):
        """Apply the settled line level when the edges left the state wrong.

        The transition is dated at the latest edge, when the line actually moved."""
        level = self._read_level()
        if level is None or self._stopped:
            return
        with self._state_lock:
            if self._stopped or (level == 0) == self._pressed:
                return
            logger.info("%s line settled %s with its edge debounce-dropped -- applying",
                        self._source, "pressed" if level == 0 else "released")
            self._last_transition_tick = self._last_edge_tick
            self._transition(level, self._last_edge_at)

    def _on_edge(self, chip, gpio, level, tick):
        if self._stopped or level not in (0, 1):
            return
        now = time.monotonic()
        with self._state_lock:
            self._last_edge_tick = tick
            self._last_edge_at = now
            # Whatever this edge does, read the line back once it goes quiet.
            self._arm_settle_check()
            if (self._last_transition_tick is not None
                    and tick - self._last_transition_tick < self._debounce_ns):
                return
            if level == 1 and not self._pressed:
                # Stale release edge (matching press was debounce-dropped).
                # _press_start may be from minutes ago — refusing to act is
                # safer than firing a destructive action against stale state.
                logger.warning("GPIO button release without matching press -- ignoring")
                return
            if level == 0 and self._pressed:
                return
            self._last_transition_tick = tick
            self._transition(level, now)

    def _transition(self, level, at):
        """Press or release at monotonic time `at`; caller holds _state_lock."""
        if level == 0:
            # Button pressed (falling edge). LED feedback runs in a watcher thread.
            with self._hold_lock:
                if self._stopped or self._pressed:
                    return
                self._press_start = at
                self._pressed = True
                if self._hold_watcher_stop is not None:
                    self._hold_watcher_stop.set()
                self._hold_led.release()
                new_stop = threading.Event()
                self._hold_watcher_stop = new_stop
            threading.Thread(
                target=self._hold_watcher,
                args=(new_stop,),
                daemon=True,
                name="gpio-button-hold-led",
            ).start()
            return

        if not self._pressed:
            return
        self._pressed = False
        # Cancel blinking without blocking the GPIO callback on RGB I/O.
        with self._hold_lock:
            if self._hold_watcher_stop is not None:
                self._hold_watcher_stop.set()
                self._hold_watcher_stop = None
            self._hold_led.release()

        held = max(0.0, at - self._press_start)
        if button_hold_tier(held, behavior=self._behavior, hold_s=self._hold_s,
                            factory_reset=self._factory_reset):
            self._click_count = 0
            if self._click_timer:
                self._click_timer.cancel()
                self._click_timer = None

            # Edge handling only supplies the released-duration signal. It may wait for
            # a cue or release servos, so keep the GPIO callback short and never block
            # subsequent hardware edges.
            threading.Thread(
                target=self._run_hold_action,
                args=(held,),
                daemon=True,
                name="gpio-button-hold-action",
            ).start()
            return

        if self._behavior == "factory_reset":
            logger.info("%s released hold %.3fs -- ignored (requires %.1fs)",
                        self._source, held, self._hold_s)
            return

        if self._device_tap_mode():
            # Every completed short tap is independent, even inside the normal
            # multi-click window. Holds and dedicated reset buttons stay above.
            self._click_count = 0
            if self._click_timer:
                self._click_timer.cancel()
                self._click_timer = None
            threading.Thread(
                target=self._run_single_click, daemon=True,
                name="gpio-button-device-tap",
            ).start()
            return

        self._click_count += 1
        if self._click_count == 1:
            # Off-thread: stop_tts/audio_stop/unmute do blocking I/O; the
            # lgpio callback must return promptly (same reasoning as the
            # long-press branches above).
            threading.Thread(
                target=self._run_single_click,
                daemon=True,
                name="gpio-button-single-click",
            ).start()
        if self._click_timer:
            self._click_timer.cancel()
        self._click_timer = threading.Timer(
            DOUBLE_CLICK_WINDOW, self._on_click_timeout
        )
        self._click_timer.daemon = True
        self._click_timer.start()

    def _on_click_timeout(self):
        with self._action_lock:
            if self._stopped or self._behavior != "standard":
                return
            self._resolve_clicks()

    def _resolve_clicks(self):
        count = self._click_count
        self._click_count = 0
        if self._device_tap_mode():
            return
        if count == 3:
            triple_click_action(source=self._source)
            return
        if count != 1:
            # Double or 4+ clicks: never trigger destructive actions.
            logger.info("GPIO button %d clicks -- ignored (only 1=stop, 3=reboot)", count)
        announce_listening_cue(source=self._source)
