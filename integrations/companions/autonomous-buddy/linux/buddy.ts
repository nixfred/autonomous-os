#!/usr/bin/env bun
// Autonomous Buddy for Linux (Omarchy / Hyprland / Wayland).
// Speaks the Buddy v1 protocol (docs/autonomous-buddy.md): pair once with a 6-digit code, then
// keep a WebSocket to the device and execute its JSON commands on this desktop.
//
//   buddy.ts pair <device-host[:port]> <code>   exchange the code for a token (stored 0600)
//   buddy.ts run [--dry-run]                    connect and serve commands (backoff reconnect)
//   buddy.ts exec <action> '<params-json>'      run one action locally (testing)
//
// Executors (Hyprland Lua dispatch on Omarchy 4, wtype, grim, xdg-open, notify-send):
//   ping, open_app, close_app, open_url, type_text, key_combo, notification,
//   screenshot, desktop_info, focus_window
// --dry-run logs every command and executes nothing: use it before trusting a new device.
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync, chmodSync } from "node:fs";
import { hostname, release } from "node:os";

const CONF_DIR = `${process.env.HOME}/.config/autonomous-buddy`;
const TOKEN_FILE = `${CONF_DIR}/pairing.json`;
const DRY = process.argv.includes("--dry-run");
const log = (...a: unknown[]) => console.log(new Date().toISOString(), ...a);

type Cmd = { id: string; action: string; params?: Record<string, any>; timeout_ms?: number };
type Result = { id: string; ok: boolean; result?: unknown; error?: string | null; duration_ms: number };

function sh(argv: string[], input?: string): { ok: boolean; out: string; err: string } {
  const p = Bun.spawnSync(argv, { stdin: input !== undefined ? new TextEncoder().encode(input) : "ignore" });
  return { ok: p.exitCode === 0, out: new TextDecoder().decode(p.stdout), err: new TextDecoder().decode(p.stderr) };
}
const hypr = (lua: string) => sh(["hyprctl", "dispatch", lua]);
const clients = (): any[] => { try { return JSON.parse(sh(["hyprctl", "clients", "-j"]).out); } catch { return []; } };
const luaStr = (s: string) => JSON.stringify(s); // JSON string literal is a valid Lua string literal for our inputs

// .desktop lookup: "Google Chrome" -> google-chrome.desktop, by Name= or file id
function desktopEntry(app: string): string | null {
  const want = app.toLowerCase().trim();
  const dirs = [`${process.env.HOME}/.local/share/applications`, "/usr/share/applications"];
  for (const d of dirs) {
    let files: string[] = [];
    try { files = readdirSync(d).filter((f) => f.endsWith(".desktop")); } catch { continue; }
    for (const f of files) {
      const id = f.replace(/\.desktop$/, "");
      if (id.toLowerCase() === want || id.toLowerCase() === want.replace(/\s+/g, "-")) return id;
      try {
        const name = readFileSync(`${d}/${f}`, "utf8").match(/^Name=(.+)$/m)?.[1]?.trim().toLowerCase();
        if (name === want) return id;
      } catch {}
    }
  }
  return null;
}

function findWindow(q: string): any | null {
  const w = q.toLowerCase();
  return clients().find((c) => String(c.class).toLowerCase() === w) ||
    clients().find((c) => String(c.class).toLowerCase().includes(w) || String(c.title).toLowerCase().includes(w)) || null;
}

// Keystrokes land in whatever has focus, so an input action may name its target window:
// it is focused first, and the action refuses if that window does not exist.
function focusTarget(p: Record<string, any>) {
  if (!p.app) return null;
  const w = findWindow(String(p.app));
  if (!w) throw new Error(`target window "${p.app}" not found; refusing to type blind`);
  hypr(`hl.dsp.focus({window=${luaStr(`address:${w.address}`)}})`);
  Bun.sleepSync(120);
  return w.class;
}

const MOD: Record<string, string> = { cmd: "super", command: "super", super: "super", win: "super", ctrl: "ctrl", control: "ctrl", alt: "alt", option: "alt", shift: "shift" };

const ACTIONS: Record<string, (p: Record<string, any>) => unknown> = {
  ping: () => ({ pong: true, host: hostname(), os: `Linux ${release()}`, desktop: "Omarchy/Hyprland" }),

  open_app: (p) => {
    const app = String(p.app || "").trim();
    if (!app) throw new Error("app required");
    const id = desktopEntry(app);
    if (id) { hypr(`hl.dsp.exec_cmd(${luaStr(`gtk-launch ${id}`)})`); return { launched: id }; }
    const bin = app.toLowerCase().replace(/\s+/g, "-");
    if (!sh(["which", bin]).ok) throw new Error(`no application "${app}"`);
    hypr(`hl.dsp.exec_cmd(${luaStr(bin)})`);
    return { launched: bin };
  },

  close_app: (p) => {
    const w = findWindow(String(p.app || ""));
    if (!w) throw new Error(`no window for "${p.app}"`);
    hypr(`hl.dsp.window.close({window=${luaStr(`address:${w.address}`)}})`);
    return { closed: w.class, title: w.title };
  },

  focus_window: (p) => {
    const w = findWindow(String(p.app || p.title || ""));
    if (!w) throw new Error(`no window for "${p.app || p.title}"`);
    hypr(`hl.dsp.focus({window=${luaStr(`address:${w.address}`)}})`);
    return { focused: w.class, title: w.title, workspace: w.workspace?.name };
  },

  open_url: (p) => {
    const url = String(p.url || "");
    if (!/^https?:\/\//i.test(url)) throw new Error("http(s) url required");
    const r = sh(["xdg-open", url]);
    if (!r.ok) throw new Error(r.err.trim() || "xdg-open failed");
    return { opened: url };
  },

  type_text: (p) => {
    const target = focusTarget(p);
    const text = String(p.text ?? "");
    const r = sh(["wtype", "-d", String(Math.max(0, Math.min(200, Number(p.delay_ms ?? 8)))), "-"], text);
    if (!r.ok) throw new Error(r.err.trim() || "wtype failed");
    return { typed: text.length, target };
  },

  key_combo: (p) => {
    const target = focusTarget(p);
    const keys: string[] = Array.isArray(p.keys) ? p.keys.map(String) : [];
    if (!keys.length) throw new Error("keys required");
    const mods = keys.slice(0, -1).map((k) => MOD[k.toLowerCase()]);
    if (mods.some((m) => !m)) throw new Error(`unknown modifier in ${keys.join("+")}`);
    const key = keys[keys.length - 1];
    const argv = ["wtype"];
    for (const m of mods) argv.push("-M", m);
    argv.push("-k", key.length === 1 ? key : key.charAt(0).toUpperCase() + key.slice(1));
    for (const m of [...mods].reverse()) argv.push("-m", m);
    const r = sh(argv);
    if (!r.ok) throw new Error(r.err.trim() || "wtype failed");
    return { pressed: keys.join("+"), target };
  },

  notification: (p) => {
    sh(["notify-send", "-a", "Autonomous", String(p.title || "Autonomous"), String(p.body || "")]);
    return { shown: true };
  },

  screenshot: (p) => {
    const w = p.app ? findWindow(String(p.app)) : null;
    const argv = ["grim", "-t", "png", "-s", String(Math.max(0.1, Math.min(1, Number(p.scale ?? 0.5))))];
    if (w) argv.push("-g", `${w.at[0]},${w.at[1]} ${w.size[0]}x${w.size[1]}`);
    argv.push("-");
    const r = Bun.spawnSync(argv);
    if (r.exitCode !== 0) throw new Error("grim failed");
    return { format: "png", base64: Buffer.from(r.stdout).toString("base64"), window: w?.title ?? null };
  },

  desktop_info: () => {
    const j = (a: string) => { try { return JSON.parse(sh(["hyprctl", a, "-j"]).out); } catch { return null; } };
    let theme = "";
    try { theme = readFileSync(`${process.env.HOME}/.local/state/omarchy/current/theme.name`, "utf8").trim(); } catch {}
    return {
      desktop: "Omarchy/Hyprland", theme,
      workspace: j("activeworkspace")?.name,
      active: (({ class: c, title }: any) => ({ class: c, title }))(j("activewindow") || {}),
      windows: clients().map((c) => ({ class: c.class, title: c.title, workspace: c.workspace?.name })),
    };
  },
};

async function execute(cmd: Cmd): Promise<Result> {
  const t0 = Date.now();
  const fn = ACTIONS[cmd.action];
  if (!fn) return { id: cmd.id, ok: false, error: `unsupported action "${cmd.action}"`, duration_ms: 0 };
  if (DRY && cmd.action !== "ping" && cmd.action !== "desktop_info") {
    log("DRY-RUN", cmd.action, JSON.stringify(cmd.params || {}).slice(0, 200));
    return { id: cmd.id, ok: true, result: { dry_run: true }, duration_ms: Date.now() - t0 };
  }
  try {
    const result = await fn(cmd.params || {});
    log("ok", cmd.action);
    return { id: cmd.id, ok: true, result, error: null, duration_ms: Date.now() - t0 };
  } catch (e: any) {
    log("fail", cmd.action, e.message);
    return { id: cmd.id, ok: false, error: e.message, duration_ms: Date.now() - t0 };
  }
}

function loadPairing(): { device: string; token: string; buddy_id?: string } {
  if (!existsSync(TOKEN_FILE)) throw new Error("not paired: buddy.ts pair <device-host> <code>");
  return JSON.parse(readFileSync(TOKEN_FILE, "utf8"));
}

async function pair(device: string, code: string) {
  const base = device.startsWith("http") ? device : `http://${device}`;
  const fp = new Bun.CryptoHasher("sha256").update(`${hostname()}:${process.env.HOME}`).digest("hex").slice(0, 32);
  const r = await fetch(`${base}/api/buddy/pair/confirm`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ code, name: `${hostname()} (Omarchy)`, fingerprint: fp, os_version: `Linux ${release()}` }),
  });
  const j: any = await r.json().catch(() => ({}));
  if (!r.ok || !j.token) throw new Error(`pairing failed: ${r.status} ${j.error || ""}`);
  mkdirSync(CONF_DIR, { recursive: true, mode: 0o700 });
  writeFileSync(TOKEN_FILE, JSON.stringify({ device: base, token: j.token, buddy_id: j.buddy_id }, null, 2));
  chmodSync(TOKEN_FILE, 0o600);
  log(`paired with ${base} as ${j.buddy_id}`);
}

async function run() {
  const p = loadPairing();
  const url = p.device.replace(/^http/, "ws") + "/api/buddy/ws";
  let backoff = 1000;
  for (;;) {
    await new Promise<void>((resolve) => {
      const ws = new WebSocket(url, { headers: { Authorization: `Bearer ${p.token}` } } as any);
      let alive: any;
      ws.onopen = () => { backoff = 1000; log(`connected ${url}${DRY ? " (dry-run)" : ""}`); alive = setInterval(() => { try { ws.send(JSON.stringify({ type: "ping" })); } catch {} }, 15000); };
      ws.onmessage = async (ev) => {
        let cmd: Cmd;
        try { cmd = JSON.parse(String(ev.data)); } catch { return; }
        if (!cmd.id || !cmd.action) return;
        ws.send(JSON.stringify(await execute(cmd)));
      };
      ws.onclose = (ev) => {
        clearInterval(alive);
        if (ev.code === 1008 || ev.code === 4401) { log("rejected by device (revoked?); re-pair"); process.exit(2); }
        log(`disconnected (${ev.code}); retry in ${backoff / 1000}s`); resolve();
      };
      ws.onerror = () => {};
    });
    await Bun.sleep(backoff);
    backoff = Math.min(backoff * 2, 30000);
  }
}

const [cmd, a1, a2] = process.argv.slice(2).filter((x) => x !== "--dry-run");
if (cmd === "pair" && a1 && a2) await pair(a1, a2);
else if (cmd === "run") await run();
else if (cmd === "exec" && a1) console.log(JSON.stringify(await execute({ id: "local", action: a1, params: a2 ? JSON.parse(a2) : {} })));
else { console.error("usage: buddy.ts pair <host> <code> | run [--dry-run] | exec <action> '<json>'"); process.exit(2); }
