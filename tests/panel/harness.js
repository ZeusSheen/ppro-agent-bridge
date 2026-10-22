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
 *   describe      -> {ok, version, protocol, modules, handlers: [names in order], specActions: [names]}
 *   call          stdin {action, params?, guard?, timeoutSeconds?, scenario?, hostVersion?, buildNumber?,
 *                        hostTable?, stuck?, mock?}
 *                 -> {ok, reply: <panel reply envelope>, transactions: [labels], sideEffects: n}
 *   batch         stdin {steps: [{action, params?, guard?, timeoutSeconds?}], scenario?, ...as call}
 *                 -> {ok, replies: [...], transactions: [labels], sideEffects: [n per step]}
 *   capabilities  stdin {scenario?, hostVersion?, buildNumber?, hostTable?}   (runs ping {probe: true})
 *                 -> {ok, capabilities, denied, host, hostInfo}
 *   timebase      stdin {vectors: [set_params vectors from actions.json]}
 *                 -> {ok, results: [{mediaSeconds} | {code}]}
 * `call` / `batch` add "violations" (mock lock-scope breaches) only when there were any.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { createMocks, TPS } = require("./mocks.js");

const REPO = path.resolve(__dirname, "..", "..");
const DEFAULT_MAIN = path.join(REPO, "plugin", "main.js");
const LIB_REQUIRE = /^\.\/lib\/[A-Za-z0-9_]+\.js$/;

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
  if (!out.command) throw new Error("usage: harness.js <describe|call|batch|capabilities|timebase> [--bundle PATH]");
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
    className: "", textContent: "", childNodes: [], scrollTop: 0, scrollHeight: 0,
    get firstChild() { return this.childNodes[0] || null; },
    appendChild(c) { this.childNodes.push(c); return c; },
    removeChild(c) { const i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); return c; },
    addEventListener() {},
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
  constructor(url) { this.url = url; this.readyState = StubWebSocket.CONNECTING; this.sent = []; }
  send(data) { this.sent.push(data); }
  close() { this.readyState = StubWebSocket.CLOSED; }
}
StubWebSocket.CONNECTING = 0;
StubWebSocket.OPEN = 1;
StubWebSocket.CLOSING = 2;
StubWebSocket.CLOSED = 3;

const stderrConsole = {
  log: (...a) => process.stderr.write(a.join(" ") + "\n"),
  info: (...a) => process.stderr.write(a.join(" ") + "\n"),
  warn: (...a) => process.stderr.write(a.join(" ") + "\n"),
  error: (...a) => process.stderr.write(a.join(" ") + "\n"),
  debug: () => {},
};

function loadPanel(req, mainPath) {
  const pluginDir = path.dirname(mainPath);
  const mocks = createMocks({
    scenario: req.scenario, hostVersion: req.hostVersion, buildNumber: req.buildNumber, pluginDir, mock: req.mock,
  });
  const sandbox = {
    console: stderrConsole, setTimeout, clearTimeout, setInterval, clearInterval,
    document: makeDocument(), WebSocket: StubWebSocket,
    addEventListener() {}, removeEventListener() {},
  };
  if (req.hostTable !== undefined) sandbox.__agentBridgeHostTable = req.hostTable;
  const context = vm.createContext(sandbox);
  const modules = {};
  const libRequire = (name) => { throw new Error(`lib modules must not require ("${name}"); use deps`); };
  sandbox.require = function require(name) {
    if (name === "premierepro") return mocks.ppro;
    if (name === "uxp") return mocks.uxp;
    if (LIB_REQUIRE.test(name)) {
      if (!modules[name]) {
        const file = path.join(pluginDir, name.slice(2));
        const source = fs.readFileSync(file, "utf8");
        const fn = vm.runInContext(`(function (module, exports, require) {${source}\n})`, context, { filename: file });
        const module = { exports: {} };
        modules[name] = module;
        fn.call(module.exports, module, module.exports, libRequire);
      }
      return modules[name].exports;
    }
    throw new Error(`harness: cannot require "${name}"`);
  };
  vm.runInContext(fs.readFileSync(mainPath, "utf8"), context, { filename: mainPath });
  const bridge = sandbox.__agentBridge;
  if (!bridge || typeof bridge.handleMessage !== "function") throw new Error("main.js did not set globalThis.__agentBridge");
  return { bridge, mocks };
}

function applyStuck(bridge, req) {
  if (!req.stuck) return;
  bridge.state.stuck = (req.stuck && typeof req.stuck === "object")
    ? req.stuck : { action: "insert_clip", id: "harness-stuck", since: Date.now() };
}

let counter = 0;
async function callOnce(bridge, step) {
  if (!step || typeof step.action !== "string") throw new Error("a call needs an action");
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
  const { bridge, mocks } = loadPanel(req, mainPath);
  switch (args.command) {
    case "describe":
      return {
        ok: true, version: bridge.spec.version, protocol: bridge.spec.protocol, modules: bridge.state.modules,
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
    case "capabilities": {
      const reply = await callOnce(bridge, { action: "ping", params: { probe: true } });
      if (!reply.ok) throw new Error(`ping failed: ${reply.error}`);
      const d = reply.data;
      return withViolations({ ok: true, capabilities: d.capabilities, denied: d.denied, host: d.host, hostInfo: d.hostInfo }, mocks);
    }
    default:
      throw new Error(`unknown command "${args.command}" (describe, call, batch, capabilities, timebase)`);
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
