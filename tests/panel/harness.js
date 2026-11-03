#!/usr/bin/env node
"use strict";
/*
 * Node harness for the Agent Bridge panel (node built-ins only).
 *
 *   node tests/panel/harness.js <command> [--bundle PATH]      request JSON on stdin
 *
 * Loads plugin/main.js (or a bundle from scripts/build_panel.py) in a vm context with a UXP-like
 * `require`: "premierepro" / "uxp" -> tests/panel/mocks.js, "./lib/<name>.js" -> compiled in the same
 * context. Prints exactly one JSON object; exit 0 when the harness ran (even if the panel replied
 * ok:false), exit 2 with {"ok": false, "error": ...} on a harness failure.
 *
 * Commands
 *   describe      stdin {dev?, buildJs?}
 *                 -> {ok, version, protocol, modules, build, specMismatch, handlers: [names in order],
 *                     specActions: [names]}
 *   call          stdin {action, params?, guard?, timeoutSeconds?, scenario?, hostVersion?, buildNumber?,
 *                        hostTable?, stuck?, mock?, dev?, buildJs?}
 *                 -> {ok, reply: <panel reply envelope>, transactions: [labels], sideEffects: n}
 *   batch         stdin {steps: [{action, params?, guard?, timeoutSeconds?, waitMs?}], scenario?, ...as call}
 *                 (waitMs: real milliseconds to let pass before that step, for timers such as a mock render)
 *                 -> {ok, replies: [...], transactions: [labels], sideEffects: [n per step]}
 *   capabilities  stdin {scenario?, hostVersion?, buildNumber?, hostTable?}   (runs ping {probe: true})
 *                 -> {ok, capabilities, denied, host, hostInfo}
 *   launcher      stdin {broker?, clicks?: n, waitMs?, refused?, unloading?, pluginFolder?, mock?}: Start bridge -
 *                 settles the first connection attempt, clicks n times (awaiting each), waits waitMs
 *                 -> {ok, before: button, after: button, results: [launch results], openPath: [{path,
 *                     developerText, result}], log: [{text, cls}]}; button = {display, disabled, text}
 *                 broker (any command): the sockets main.js opens settle by themselves - "down" every attempt
 *                 fails, "up" every attempt opens, "after_launch" opens once shell.openPath resolved to "",
 *                 "refusing" every attempt is closed with 4409 (a live panel holds the broker);
 *                 absent: they stay CONNECTING (default). pluginFolder (any command): the folder uxp
 *                 getPluginFolder() returns instead of plugin/ (e.g. one with a launcher.json).
 *   timebase      stdin {vectors: [set_params vectors from actions.json]}
 *                 -> {ok, results: [{mediaSeconds} | {code}]}
 *   connection    stdin {steps: [...], ...as call}: drives the socket main.js opened at load (a stub) and the page.
 *                 Steps: {open: true} | {message: string or object} | {close: {code, reason} or null} |
 *                 {throwingMessage: true} (an event whose data getter throws) | {click: element id} |
 *                 {fire: "error"|"unhandledrejection"|"beforeunload"|..., message?} (global listeners) | {safely: "sync"|"async"} (a throwing callback
 *                 wrapped by main.js's safely) | {runTimer: ms} (runs the newest timer of that delay now) | {wait: ms}
 *                 -> {ok, sockets: [{url, readyState, sent: [JSON-parsed]}], status, dot, log: [lines],
 *                     delays: [setTimeout ms during the steps], refused, prevented: n, escaped: [what a step threw]}
 * `call` / `batch` add "violations" (mock lock-scope breaches) only when there were any.
 * The panel loads as a dev build whatever plugin/lib/build.js says (so a release export's own tests check the same
 * panel), as a release build with `dev: false`: the one dev flag in build.js (or in a bundle) is rewritten in memory
 * the way scripts/build_ccx.py packages it, and loading fails unless it took. `buildJs` is the whole text of a
 * lib/build.js to load instead (a .ccx package's, say); then nothing is rewritten.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { createMocks, TPS } = require("./mocks.js");

const REPO = path.resolve(__dirname, "..", "..");
const DEFAULT_MAIN = path.join(REPO, "plugin", "main.js");
const LIB_REQUIRE = /^\.\/lib\/[A-Za-z0-9_]+\.js$/;
const DEV_FLAG = /\bdev\s*:\s*(true|false)\b/g;  // the build flag, as the .ccx build finds it

/* The source with its one dev flag set to `dev`; anything but exactly one flag is a harness failure. */
function withDevFlag(source, dev, file) {
  const n = (source.match(DEV_FLAG) || []).length;
  if (n !== 1) throw new Error(`${file}: expected exactly one dev flag, found ${n}`);
  return source.replace(DEV_FLAG, `dev: ${dev === true}`);
}

function parseArgs(argv) {
  const out = { command: null, bundle: null };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--bundle") {
      out.bundle = argv[i + 1];
      i += 1;
      if (!out.bundle) throw new Error("--bundle needs a path");
    } else if (!out.command) {
      out.command = argv[i];
    } else {
      throw new Error(`unexpected argument ${argv[i]}`);
    }
  }
  if (!out.command) throw new Error("usage: harness.js <describe|call|batch|capabilities|timebase|connection|launcher> [--bundle PATH]");
  return out;
}

function readStdin() {
  return new Promise((resolve) => {
    if (process.stdin.isTTY) { resolve(""); return; }
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { data += chunk; });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", () => resolve(data));
  });
}

function element() {
  return {
    className: "", textContent: "", childNodes: [], scrollTop: 0, scrollHeight: 0, listeners: {},
    get firstChild() { return this.childNodes[0] || null; },
    appendChild(c) { this.childNodes.push(c); return c; },
    removeChild(c) { const i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); return c; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  };
}

function makeDocument() {
  const byId = {};
  return {
    getElementById(id) { if (!byId[id]) byId[id] = element(); return byId[id]; },
    createElement() { return element(); },
  };
}

class StubWebSocket {
  constructor(url) {
    this.url = url; this.readyState = StubWebSocket.CONNECTING; this.sent = [];
    StubWebSocket.instances.push(this);
  }
  send(data) { this.sent.push(data); }
  close() { this.readyState = StubWebSocket.CLOSED; }
}
StubWebSocket.CONNECTING = 0;
StubWebSocket.OPEN = 1;
StubWebSocket.CLOSING = 2;
StubWebSocket.CLOSED = 3;
StubWebSocket.instances = [];  // every socket the panel opened, oldest first

const stderrConsole = {
  log: (...a) => process.stderr.write(a.join(" ") + "\n"),
  info: (...a) => process.stderr.write(a.join(" ") + "\n"),
  warn: (...a) => process.stderr.write(a.join(" ") + "\n"),
  error: (...a) => process.stderr.write(a.join(" ") + "\n"),
  debug: () => {},
};

/* Sockets that settle on their own (the "broker" request option): open or fail one tick after construction. */
function brokerWebSocket(mode, mocks) {
  if (["down", "up", "after_launch", "refusing"].indexOf(mode) < 0) throw new Error(`unknown broker mode "${mode}"`);
  const up = () => mode === "up" || (mode === "after_launch" && mocks.record.openPath.some((c) => c.result === ""));
  return class BrokerWebSocket extends StubWebSocket {
    constructor(url) {
      super(url);
      setTimeout(() => {
        if (this.readyState !== StubWebSocket.CONNECTING) return;
        if (up()) {
          this.readyState = StubWebSocket.OPEN;
          if (this.onopen) this.onopen({});
        } else {
          this.readyState = StubWebSocket.CLOSED;
          if (this.onerror) this.onerror({});
          if (this.onclose) {
            this.onclose(mode === "refusing" ? { code: 4409, reason: "A live Agent Bridge panel is already connected" }
              : { code: 1006, reason: "" });
          }
        }
      }, 0);
    }
  };
}

function loadPanel(req, mainPath) {
  const pluginDir = path.dirname(mainPath);
  const dev = typeof req.buildJs === "string" ? undefined : req.dev !== false;  // the flag to load with
  const mocks = createMocks({
    scenario: req.scenario, hostVersion: req.hostVersion, buildNumber: req.buildNumber, pluginDir, mock: req.mock,
    pluginFolder: req.pluginFolder,
  });
  const listeners = {};  // global event listeners (main.js registers them on its GLOBAL)
  const delays = [];     // the ms of every setTimeout the panel made
  const timers = [];     // {ms, fn, handle} of each, for the connection command's runTimer step
  const sandbox = {
    console: stderrConsole, clearTimeout, setInterval, clearInterval,
    setTimeout: (fn, ms, ...rest) => {
      const handle = setTimeout(fn, ms, ...rest);
      delays.push(ms);
      timers.push({ ms, fn: () => fn(...rest), handle });
      return handle;
    },
    document: makeDocument(), WebSocket: StubWebSocket,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener() {},
  };
  if (req.hostTable !== undefined) sandbox.__agentBridgeHostTable = req.hostTable;
  if (req.broker !== undefined) sandbox.WebSocket = brokerWebSocket(req.broker, mocks);
  sandbox.document.getElementById("startBtn").style = {};  // main.js shows / hides it through style.display
  sandbox.document.getElementById("guide").style = {};     // ... and the first-run steps
  const context = vm.createContext(sandbox);
  const modules = {};
  const libRequire = (name) => { throw new Error(`lib modules must not require ("${name}"); use deps`); };
  sandbox.require = function require(name) {
    if (name === "premierepro") return mocks.ppro;
    if (name === "uxp") return mocks.uxp;
    if (LIB_REQUIRE.test(name)) {
      if (!modules[name]) {
        const file = path.join(pluginDir, name.slice(2));
        let source = fs.readFileSync(file, "utf8");
        if (name === "./lib/build.js" && typeof req.buildJs === "string") source = req.buildJs;
        else if (name === "./lib/build.js" && dev !== undefined) source = withDevFlag(source, dev, file);
        const fn = vm.runInContext(`(function (module, exports, require) {${source}\n})`, context, { filename: file });
        const module = { exports: {} };
        modules[name] = module;
        fn.call(module.exports, module, module.exports, libRequire);
      }
      return modules[name].exports;
    }
    throw new Error(`harness: cannot require "${name}"`);
  };
  let main = fs.readFileSync(mainPath, "utf8");
  if (dev !== undefined && main.match(DEV_FLAG)) main = withDevFlag(main, dev, mainPath);  // a bundle
  vm.runInContext(main, context, { filename: mainPath });
  const bridge = sandbox.__agentBridge;
  if (!bridge || typeof bridge.handleMessage !== "function") throw new Error("main.js did not set globalThis.__agentBridge");
  if (dev !== undefined && (!bridge.build || bridge.build.dev !== dev)) {
    throw new Error(`the panel did not load as a ${dev ? "dev" : "release"} build`);
  }
  const page = { listeners, delays, timers, document: sandbox.document, sockets: StubWebSocket.instances };
  return { bridge, mocks, page };
}

const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const tick = () => new Promise((resolve) => setImmediate(resolve));

/* One `connection` step against the newest socket and the page (see the header). */
async function connectionStep(bridge, page, step, counts) {
  const sock = page.sockets[page.sockets.length - 1];
  if (step.open) {
    sock.readyState = StubWebSocket.OPEN;
    sock.onopen({});
  } else if (step.fire) {
    const error = new Error(step.message || "boom");
    const evt = { type: step.fire, preventDefault() { counts.prevented += 1; } };
    Object.assign(evt, step.fire === "error" ? { error, message: error.message } : { reason: error });
    for (const fn of page.listeners[step.fire] || []) fn(evt);
  } else if (has(step, "message")) {
    sock.onmessage({ data: typeof step.message === "string" ? step.message : JSON.stringify(step.message) });
  } else if (step.throwingMessage) {
    sock.onmessage({ get data() { throw new Error("unreadable message"); } });
  } else if (has(step, "close")) {
    sock.readyState = StubWebSocket.CLOSED;
    sock.onclose(step.close === null ? undefined : step.close);
  } else if (step.click) {
    for (const fn of page.document.getElementById(step.click).listeners.click || []) fn({});
  } else if (step.safely) {
    const boom = new Error(`${step.safely} boom`);
    const fn = step.safely === "async" ? async () => { throw boom; } : () => { throw boom; };
    await bridge.safely("Test callback", fn)();
  } else if (step.runTimer) {
    const i = page.timers.map((t) => t.ms).lastIndexOf(step.runTimer);
    if (i < 0) throw new Error(`no ${step.runTimer} ms timer`);
    const [timer] = page.timers.splice(i, 1);
    clearTimeout(timer.handle);
    timer.fn();
  } else if (has(step, "wait")) {
    await new Promise((resolve) => setTimeout(resolve, step.wait));
  } else {
    throw new Error(`unknown connection step ${JSON.stringify(step)}`);
  }
}

function applyStuck(bridge, req) {
  if (!req.stuck) return;
  bridge.state.stuck = (req.stuck && typeof req.stuck === "object")
    ? req.stuck : { action: "insert_clip", id: "harness-stuck", since: Date.now() };
}

let counter = 0;
async function callOnce(bridge, step) {
  if (!step || typeof step.action !== "string") throw new Error("a call needs an action");
  if (step.waitMs > 0) await new Promise((resolve) => setTimeout(resolve, step.waitMs));
  counter += 1;
  const msg = { id: `harness-${counter}`, action: step.action };
  if (Object.prototype.hasOwnProperty.call(step, "params")) msg.params = step.params;
  if (step.guard !== undefined) msg.guard = step.guard;
  if (step.timeoutSeconds !== undefined) msg.timeoutSeconds = step.timeoutSeconds;
  const reply = await bridge.handleMessage(JSON.stringify(msg), null);
  if (!reply) throw new Error("the panel dropped the request");
  return JSON.parse(JSON.stringify(reply));
}

const sideEffectsOf = (bridge) => (bridge.state.last ? bridge.state.last.sideEffects : 0);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* The Start bridge button (command "launcher"). */
async function launcherRun(bridge, mocks, page, req) {
  const btn = page.document.getElementById("startBtn");
  const button = () => ({ display: btn.style.display === undefined ? null : btn.style.display, disabled: !!btn.disabled,
    text: btn.textContent });
  if (req.refused) bridge.state.refused = { reason: "harness", closeCode: 4409, since: Date.now() };
  if (req.unloading) bridge.state.unloading = true;
  await sleep(20);  // the first connection attempt settles
  const before = button();
  const results = [];
  for (let i = 0; i < (req.clicks || 0); i++) {
    for (const fn of btn.listeners.click || []) results.push(await fn({ type: "click", target: btn }));
  }
  if (req.waitMs) await sleep(req.waitMs);
  return {
    ok: true, before, after: button(), results: JSON.parse(JSON.stringify(results)),
    openPath: mocks.record.openPath.slice(),
    log: page.document.getElementById("log").childNodes.map((n) => ({ text: n.textContent, cls: n.className })),
  };
}

function withViolations(out, mocks) {
  if (mocks.record.violations.length) out.violations = mocks.record.violations.slice();
  return out;
}

async function run(args, req) {
  const mainPath = args.bundle ? path.resolve(args.bundle) : DEFAULT_MAIN;
  if (args.command === "timebase") {
    const { bridge } = loadPanel(req, mainPath);
    const effects = bridge.lib.effects;
    const results = (req.vectors || []).map((v) => {
      try {
        const r = effects.convertKeyframeTime({
          timeBase: v.timeBase, t: v.t, start: v.clip.start, inPoint: v.clip.inPoint, outPoint: v.clip.outPoint,
          speed: v.clip.speed, frameTicks: Math.round(v.frameSeconds * TPS),
        });
        return { mediaSeconds: r.mediaSeconds };
      } catch (e) {
        if (e && typeof e.code === "string") return { code: e.code };
        throw e;
      }
    });
    return { ok: true, results };
  }
  const loaded = loadPanel(req, mainPath);
  const { bridge, mocks } = loaded;
  switch (args.command) {
    case "describe":
      return {
        ok: true, version: bridge.spec.version, protocol: bridge.spec.protocol, modules: bridge.state.modules,
        build: { dev: bridge.build.dev === true, packaged: bridge.build.packaged === true },  // as ping reports it
        specMismatch: bridge.state.specMismatch,
        handlers: Object.keys(bridge.handlers), specActions: Object.keys(bridge.spec.actions),
      };
    case "call": {
      applyStuck(bridge, req);
      const reply = await callOnce(bridge, req);
      return withViolations({ ok: true, reply, transactions: mocks.record.transactions.slice(), sideEffects: sideEffectsOf(bridge) }, mocks);
    }
    case "batch": {
      applyStuck(bridge, req);
      const replies = [];
      const sideEffects = [];
      for (const step of req.steps || []) {
        replies.push(await callOnce(bridge, step));
        sideEffects.push(sideEffectsOf(bridge));
      }
      return withViolations({ ok: true, replies, transactions: mocks.record.transactions.slice(), sideEffects }, mocks);
    }
    case "connection": {
      const page = loaded.page;
      const from = page.delays.length;
      const counts = { prevented: 0 };
      const escaped = [];
      for (const step of req.steps || []) {
        try {
          await connectionStep(bridge, page, step, counts);
        } catch (e) {
          escaped.push(String(e && e.message ? e.message : e));
        }
        for (let i = 0; i < 3; i += 1) await tick();
      }
      const parse = (x) => { try { return JSON.parse(x); } catch (e) { return x; } };
      const text = (id) => page.document.getElementById(id);
      return withViolations({
        ok: true, escaped, prevented: counts.prevented, refused: bridge.state.refused,
        sockets: page.sockets.map((s) => ({ url: s.url, readyState: s.readyState, sent: s.sent.map(parse) })),
        status: text("statusText").textContent, dot: text("dot").className, brand: text("brand").className, hint: text("hint").textContent, guide: text("guide").style.display, prompt: text("prompt").value,
        log: text("log").childNodes.map((n) => n.textContent), delays: page.delays.slice(from),
      }, mocks);
    }
    case "capabilities": {
      const reply = await callOnce(bridge, { action: "ping", params: { probe: true } });
      if (!reply.ok) throw new Error(`ping failed: ${reply.error}`);
      const d = reply.data;
      return withViolations({ ok: true, capabilities: d.capabilities, denied: d.denied, host: d.host, hostInfo: d.hostInfo }, mocks);
    }
    case "launcher":
      return withViolations(await launcherRun(bridge, mocks, loaded.page, req), mocks);
    default:
      throw new Error(`unknown command "${args.command}" (describe, call, batch, capabilities, timebase, connection, launcher)`);
  }
}

function emit(obj, code) {
  process.stdout.write(JSON.stringify(obj) + "\n", () => process.exit(code));
}

async function main() {
  try {
    const args = parseArgs(process.argv.slice(2));
    const raw = await readStdin();
    const req = raw.trim() ? JSON.parse(raw) : {};
    if (!req || typeof req !== "object" || Array.isArray(req)) throw new Error("stdin must be a JSON object");
    emit(await run(args, req), 0);
  } catch (e) {
    emit({ ok: false, error: String(e && e.stack ? e.stack : e) }, 2);
  }
}

if (require.main === module) main();
else module.exports = { loadPanel, callOnce, run, DEFAULT_MAIN };
