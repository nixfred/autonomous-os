"""GPIO gesture and lifecycle tests without hardware or destructive actions."""
from unittest import mock

import pytest

from hal.board.gpio_button import ButtonConfig
from hal.drivers import button_actions, gpio_button


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **unused):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        if self.target.__name__ != "_hold_watcher":
            self.target(*self.args, **self.kwargs)


@pytest.fixture
def actions():
    with (
        mock.patch.object(gpio_button, "HoldLEDFeedback") as led,
        mock.patch.object(gpio_button.threading, "Thread", ImmediateThread),
        mock.patch.object(gpio_button.threading, "Timer", FakeTimer),
        mock.patch.object(button_actions, "factory_reset_action") as reset,
        mock.patch.object(button_actions, "hold_release_action") as normal,
        mock.patch.object(gpio_button, "single_click_action") as single,
        mock.patch.object(gpio_button, "triple_click_action") as triple,
        mock.patch.object(gpio_button, "announce_listening_cue") as cue,
    ):
        yield led, reset, normal, single, triple, cue


def handler(**kwargs):
    return gpio_button.GPIOButtonHandler(ButtonConfig(chip=0, line=99, debounce_ns=0),
                                         name="reset", behavior="factory_reset", **kwargs)


def edge(button, level, seconds):
    with mock.patch.object(gpio_button.time, "monotonic", return_value=seconds):
        button._on_edge(0, 99, level, int((seconds + 1) * 1e9))


@pytest.mark.parametrize("duration,expected", [(0.1, 0), (4.999, 0), (5.0, 1), (20.0, 1)])
def test_reset_release_boundary(actions, duration, expected):
    led, reset, normal, single, triple, cue = actions
    button = handler()
    edge(button, 0, 0)
    reset.assert_not_called()
    edge(button, 1, duration)
    assert reset.call_count == expected
    if expected:
        reset.assert_called_once_with("GPIO button reset (gpiochip0/line99)")
        led.return_value.commit_tier.assert_called_once_with(3)
        led.return_value.commit.assert_not_called()
    edge(button, 1, duration + 1)
    assert reset.call_count == expected
    for action in (normal, single, triple, cue):
        action.assert_not_called()


def test_reset_ignores_triple_tap_and_watchdog(actions):
    button = handler()
    for start in (1, 2, 3):
        edge(button, 0, start)
        edge(button, 2, start + 0.01)
        assert button._pressed
        edge(button, 1, start + 0.1)
    button._on_click_timeout()
    for action in actions[1:]:
        action.assert_not_called()
    assert button._click_timer is None


def test_reset_hold_feedback_at_threshold_without_action(actions):
    button = handler()
    edge(button, 0, 0)
    stop = button._hold_watcher_stop
    with mock.patch.object(gpio_button.time, "monotonic", return_value=5):
        with mock.patch.object(stop, "wait", return_value=True):
            button._hold_watcher(stop)
    actions[0].return_value.set_tier.assert_called_once_with(3)
    actions[1].assert_not_called()


def test_independent_buttons_use_independent_release_duration(actions):
    first, second = handler(), handler(hold_s=8)
    edge(first, 0, 0)
    edge(second, 0, 4)
    edge(first, 1, 5)
    edge(second, 1, 9)
    assert actions[1].call_count == 1


def test_stop_cancels_all_resources_and_pending_actions(actions):
    button = handler()
    edge(button, 0, 0)
    watcher = button._hold_watcher_stop
    callback, timer, lgpio = mock.Mock(), mock.Mock(), mock.Mock()
    button._callback, button._click_timer = callback, timer
    button._handle, button._lgpio = 12, lgpio
    button.stop()
    button.stop()
    assert watcher.is_set()
    callback.cancel.assert_called_once_with()
    timer.cancel.assert_called_once_with()
    lgpio.gpiochip_close.assert_called_once_with(12)
    edge(button, 1, 10)
    button._run_hold_action(10)
    button._run_single_click()
    button._on_click_timeout()
    for action in actions[1:]:
        action.assert_not_called()


def test_start_failure_closes_open_chip(actions):
    lgpio = mock.Mock()
    lgpio.gpiochip_open.return_value = 7
    lgpio.gpio_claim_alert.side_effect = RuntimeError("line busy")
    button = handler()
    with mock.patch.dict("sys.modules", lgpio=lgpio):
        with pytest.raises(RuntimeError, match="line busy"):
            button.start()
    lgpio.gpiochip_close.assert_called_once_with(7)
    assert button._stopped


def test_standard_policy_preserves_existing_hold_action(actions):
    button = gpio_button.GPIOButtonHandler(ButtonConfig(chip=0, line=100, debounce_ns=0))
    edge(button, 0, 0)
    edge(button, 1, 5)
    actions[0].return_value.commit.assert_called_once_with(5, factory_reset=True)
    actions[2].assert_called_once_with(5, source="GPIO button", factory_reset=True)
    actions[1].assert_not_called()


def test_cancelled_led_commit_does_not_reset(actions):
    actions[0].return_value.commit_tier.return_value = False
    button = handler()
    edge(button, 0, 0)
    edge(button, 1, 5)
    actions[1].assert_not_called()


def test_cleanup_errors_do_not_prevent_remaining_cleanup(actions):
    button = handler()
    button._callback = mock.Mock()
    button._callback.cancel.side_effect = RuntimeError("callback failure")
    button._lgpio = mock.Mock()
    button._lgpio.gpiochip_close.side_effect = RuntimeError("close failure")
    button._handle = 7
    button.stop()
    button._lgpio.gpiochip_close.assert_called_once_with(7)
    assert button._callback is None and button._handle is None


class FakeTimer:
    """threading.Timer stand-in: the test decides when the settle read-back runs."""
    pending = []

    def __init__(self, interval, function):
        self.function, self.cancelled = function, False

    def start(self):
        FakeTimer.pending.append(self)

    def cancel(self):
        self.cancelled = True


class Line:
    """The button line as lgpio reports it: 1 released (pull-up), 0 pressed."""
    def __init__(self):
        self.level = 1

    def gpio_read(self, handle, pin):
        return self.level


def wired(behavior="factory_reset", debounce_ms=200, **kwargs):
    name = "reset" if behavior == "factory_reset" else "primary"
    button = gpio_button.GPIOButtonHandler(
        ButtonConfig(chip=1, line=9, debounce_ns=debounce_ms * 1_000_000),
        name=name, behavior=behavior, **kwargs)
    line = Line()
    button._handle, button._lgpio = 3, line
    return button, line


def bounce(button, line, level, seconds):
    """One edge at `seconds`; the line is left at `level`."""
    line.level = level
    edge(button, level, seconds)


def settle(seconds):
    """Run the latest pending read-back, as SETTLE_S after the last edge would."""
    live = [t for t in FakeTimer.pending if not t.cancelled]
    FakeTimer.pending.clear()
    if live:
        with mock.patch.object(gpio_button.time, "monotonic", return_value=seconds):
            live[-1].function()


@pytest.fixture
def timers():
    FakeTimer.pending = []
    yield


def test_bounced_tap_does_not_leave_a_phantom_hold(actions, timers):
    # A ~250 ms tap whose press and release both bounce. Separate press/release clocks
    # accepted the last press bounce and dropped the final release, so the next tap
    # 20 s later closed a 20 s "hold" and factory-reset the device.
    led, reset, normal, single, triple, cue = actions
    button, line = wired(behavior="standard", factory_reset=True)
    bounce(button, line, 0, 0.000)   # press
    bounce(button, line, 1, 0.002)   # press bounce
    bounce(button, line, 0, 0.004)
    bounce(button, line, 1, 0.250)   # real release
    bounce(button, line, 0, 0.254)   # release bounce
    bounce(button, line, 1, 0.255)
    settle(0.305)
    assert not button._pressed
    bounce(button, line, 0, 20.80)   # the next ordinary tap
    bounce(button, line, 1, 20.95)
    settle(21.0)
    reset.assert_not_called()
    normal.assert_not_called()
    assert not button._pressed


def test_tap_shorter_than_debounce_is_released_by_the_read_back(actions, timers):
    button, line = wired(behavior="standard")
    bounce(button, line, 0, 0.0)
    bounce(button, line, 1, 0.1)     # inside the 200 ms window: dropped
    assert button._pressed
    settle(0.15)
    assert not button._pressed
    actions[3].assert_called_once_with(source="GPIO button", announce=False)


def test_read_back_applies_a_dropped_press(actions, timers):
    button, line = wired(behavior="standard")
    bounce(button, line, 0, 0.0)
    bounce(button, line, 1, 0.3)     # accepted release
    bounce(button, line, 0, 0.35)    # real press inside the window: dropped
    settle(0.4)
    assert button._pressed and button._press_start == 0.35


def test_hold_watcher_ends_a_hold_whose_release_edge_was_lost(actions, timers):
    led, reset = actions[0], actions[1]
    button, line = wired()
    bounce(button, line, 0, 0.0)
    line.level = 1                   # released, but no edge ever arrives
    stop = button._hold_watcher_stop
    with mock.patch.object(gpio_button.time, "monotonic", return_value=30.0):
        button._hold_watcher(stop)
    assert not button._pressed and stop.is_set()
    reset.assert_not_called()


def test_real_long_hold_still_factory_resets(actions, timers):
    led, reset = actions[0], actions[1]
    button, line = wired(hold_s=10)
    bounce(button, line, 0, 0.0)
    bounce(button, line, 1, 0.003)   # press bounce
    bounce(button, line, 0, 0.005)
    settle(0.055)
    stop = button._hold_watcher_stop
    with mock.patch.object(gpio_button.time, "monotonic", return_value=5.0):
        with mock.patch.object(stop, "wait", return_value=True):
            button._hold_watcher(stop)
    assert button._pressed           # line still reads pressed: the hold continues
    bounce(button, line, 1, 12.0)
    reset.assert_called_once()
