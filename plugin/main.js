/*
 * Agent Bridge — UXP panel for Premiere Pro (entry script; index.html loads it with <script src="main.js">)
 *
 * Role: dumb executor. Connects OUT to a local broker (UXP plugins cannot
 * listen on sockets), receives whitelisted JSON commands, runs them against
 * the Premiere UXP DOM, returns JSON results.
 *
 * Security posture:
 *  - Command whitelist only (the actions in lib/spec.js). There is deliberately NO eval / runScript action.
 *  - Params are checked against the spec before any handler runs; unknown params are refused.
 *  - Broker is expected to bind 127.0.0.1 only.
 *
 * Modules: this file is the only one that requires local files. lib/spec.js is generated data;
 * every other lib/*.js is a factory `module.exports = function (deps) { ... }` wired up here.
 * API usage patterns verified against AdobeDocs/uxp-premiere-pro-samples and live on Premiere 27.1.
 */

const ppro = require("premierepro");
const uxp = require("uxp");
const spec = require("./lib/spec.js");

// The global object (globalThis; window on a host without it): test hooks and the host-table override live here.
const GLOBAL = typeof globalThis !== "undefined" ? globalThis : (typeof window !== "undefined" ? window : {});

const VERSION = spec.version;
const PROTOCOL = spec.protocol;
const MODULES = require.bundle === true ? "bundle" : "commonjs";
const BROKER_URL = "ws://127.0.0.1:8017";
const RECONNECT_MS = 3000;
const WATCHDOG_GRACE_S = 10;   // a handler this far past its timeout counts as stuck
const CODE_HASH_WAIT_MS = 1500;

let ws = null;
let reconnectTimer = null;
let queue = Promise.resolve();  // requests run one at a time, in arrival order
const watchdogTimers = new Set();

const state = {
  version: VERSION,
  protocol: PROTOCOL,
  modules: MODULES,
  loadedAt: Date.now(),
  stuck: null,          // {action, id, since} once a handler outlived the watchdog
  denied: false,        // {match, builds?, reason} when this host build is on the denylist
  last: null,           // context of the last request: {action, id, sideEffects}
  specMismatch: null,   // {missing, extra} when handlers and spec.actions disagree
  unloading: false,
};

/* ---------- tiny UI helpers ---------- */

const dot = document.getElementById("dot");
const statusText = document.getElementById("statusText");
const logEl = document.getElementById("log");

function setStatus(cls, text) {
  dot.className = cls; // "", "connecting", "connected"
  statusText.textContent = text;
}

function log(msg, cls) {
  const line = document.createElement("div");
  if (cls) line.className = cls;
  const t = new Date().toTimeString().slice(0, 8);
  line.textContent = `[${t}] ${msg}`;
  logEl.appendChild(line);
  while (logEl.childNodes.length > 300) logEl.removeChild(logEl.firstChild);
  logEl.scrollTop = logEl.scrollHeight;
}

/* ---------- modules ---------- */

const core = require("./lib/core.js")({ ppro, spec, log });
const timeline = require("./lib/timeline.js")({ ppro, core });
const projectLib = require("./lib/project.js")({ ppro, core });
const effects = require("./lib/effects.js")({ ppro, core });
const capabilities = require("./lib/capabilities.js")({
  ppro, uxp, core, timeline, log, hostTable: GLOBAL.__agentBridgeHostTable,
});
const handlerDeps = { ppro, core, timeline, projectLib, effects, capabilities, state, log };
const handlerGroups = [
  require("./lib/handlers_read.js")(handlerDeps),
  require("./lib/handlers_edit.js")(handlerDeps),
  require("./lib/handlers_effects.js")(handlerDeps),
  require("./lib/handlers_transcript.js")(handlerDeps),
  require("./lib/probe.js")({ ppro, uxp, core, timeline, hostInfo: capabilities.hostInfo }),
  require("./lib/smoke.js")(handlerDeps),
];

state.denied = capabilities.denied;

const hasOwn = core.hasOwn;
const stuckText = () => `Stuck on "${state.stuck.action}" — save your project, then Reload this panel in UXP Developer Tool`;
const deniedText = () => `Ping-only: this Premiere build is denied (${state.denied.reason})`;

/* Actions this panel answers, in spec order. */
const dispatchable = () => Object.keys(spec.actions).filter((n) => hasOwn(handlers, n));

/* Liveness check + version/protocol handshake, capabilities, stuck/denied state.
 * A plain ping makes no Premiere calls (as in 0.3.3): every CLI command pings first, and a ping must
 * answer even while Premiere is busy. Instance capabilities come from the last `ping {probe: true}`. */
async function ping(params) {
  if (params.probe) await capabilities.ensureProbe({ force: true, stuck: !!state.stuck, denied: !!state.denied });
  const caps = capabilities.snapshot();
  const data = {
    pong: true,
    host: capabilities.hostString(),
    hostInfo: capabilities.hostInfo(),
    plugin: `ppro-agent-bridge ${VERSION}`,
    version: VERSION,
    protocol: PROTOCOL,
    modules: MODULES,
    actions: dispatchable(),
    stuck: state.stuck ? Object.assign({ seconds: Math.round((Date.now() - state.stuck.since) / 1000) }, state.stuck) : false,
    denied: state.denied || false,
    capabilities: caps.flags,
    loadedAt: state.loadedAt,
  };
  if (caps.probedAt) data.capabilitiesProbedAt = caps.probedAt;
  const hash = await capabilities.codeHashWithin(params.probe ? CODE_HASH_WAIT_MS : 0);
  if (hash) data.codeHash = hash;
  return data;
}

/* The whitelist: handler modules merged in spec order; a mismatch with spec.actions is logged. */
const handlers = {};
(function buildHandlers() {
  const merged = Object.assign({ ping }, ...handlerGroups);
  const names = Object.keys(spec.actions);
  for (const n of names) if (hasOwn(merged, n)) handlers[n] = merged[n];
  const missing = names.filter((n) => !hasOwn(merged, n));
  const extra = Object.keys(merged).filter((n) => !hasOwn(spec.actions, n));
  for (const n of extra) handlers[n] = merged[n]; // listed, never dispatched: not in the spec
  if (missing.length || extra.length) {
    state.specMismatch = { missing, extra };
    log(`Handlers and spec disagree. Missing: ${missing.join(", ") || "-"}; not in spec: ${extra.join(", ") || "-"}`, "err");
  }
})();

/* ---------- dispatch ---------- */

const WATCHDOG = Symbol("watchdog");

/*
 * One request -> one reply envelope. Order: unknown action -> stuck -> denied -> params (spec)
 * -> capability -> run under the watchdog -> outcome rule on error.
 */
async function dispatch(msg) {
  const action = msg.action;
  const reply = { id: msg.id };
  const fail = (code, error, outcome, data) => {
    Object.assign(reply, { ok: false, error, code, outcome });
    if (data !== undefined) reply.data = data;
    return reply;
  };
  state.last = { action, id: msg.id, sideEffects: 0 };

  const aspec = typeof action === "string" && hasOwn(spec.actions, action) ? spec.actions[action] : null;
  if (!aspec || !hasOwn(handlers, action)) {
    return fail("UNKNOWN_ACTION", `Unknown action "${action}". Whitelist: ${dispatchable().join(", ")}`, "not_committed");
  }
  if (state.stuck && !aspec.allowedWhenStuck) {
    // A handler never finished, so Premiere's state is unknown: refuse everything but ping.
    return fail("PANEL_STUCK", stuckText(), "not_committed");
  }
  if (state.denied && !aspec.allowedWhenDenied) {
    return fail("HOST_DENIED", `This Premiere build is denied (${state.denied.reason}); the panel answers ping only.`, "not_committed");
  }
  const params = msg.params === undefined || msg.params === null ? {} : msg.params;
  const problems = core.checkParams(action, params);
  if (action !== "ping") for (const p of core.checkGuardShape(msg.guard)) problems.push(p);
  if (problems.length) {
    return fail("BAD_PARAMS", `Bad params for ${action}: ${problems.join("; ")}`, "not_committed", { problems });
  }
  if (aspec.capability && capabilities.flag(aspec.capability) === false) {
    return fail("UNSUPPORTED", `${action} needs the "${aspec.capability}" capability, which this Premiere build lacks.`, "not_committed");
  }

  const ctx = {
    action,
    id: msg.id,
    guard: action === "ping" ? null : (msg.guard || null),
    sideEffects: 0,
    sideEffect() { this.sideEffects += 1; },
  };
  state.last = ctx;
  const timeoutS = Number(msg.timeoutSeconds) > 0 ? Number(msg.timeoutSeconds)
    : (aspec.timeoutSeconds || spec.defaultTimeoutSeconds);
  const limit = timeoutS + WATCHDOG_GRACE_S;
  let timer;
  const watchdog = new Promise((resolve) => {
    timer = setTimeout(() => resolve(WATCHDOG), limit * 1000);
    watchdogTimers.add(timer);
  });
  const run = Promise.resolve().then(() => handlers[action](params, ctx));
  run.catch(() => { /* observed by the race; this only silences a late rejection */ });
  try {
    const result = await Promise.race([run, watchdog]);
    if (result === WATCHDOG) {
      state.stuck = { action, id: msg.id, since: Date.now() };
      setStatus("", stuckText());
      run.then(() => log(`${action} finished late; the panel stays stuck until Reload`, "err"),
               () => log(`${action} failed late; the panel stays stuck until Reload`, "err"));
      return fail("PANEL_STUCK",
        `${action} did not finish within ${limit}s; it may still change the project. ` + stuckText(),
        aspec.kind === "read" ? "not_committed" : "unknown");
    }
    reply.ok = true;
    reply.data = result;
    return reply;
  } catch (e) {
    return fail(core.codeOf(e), core.errorMessage(e), core.outcomeFor(e, ctx, aspec.kind),
      e && e.data !== undefined ? e.data : undefined);
  } finally {
    clearTimeout(timer);
    watchdogTimers.delete(timer);
  }
}

/* Run one request. Called through `queue`, so handlers never overlap: Premiere
 * crashes when lockedAccess chains from several requests interleave.
 * Returns the reply (null for a dropped message); answers on `sock` when given. */
async function handleMessage(raw, sock) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch (e) {
    log(`Bad JSON from broker: ${raw}`, "err");
    return null;
  }
  if (!msg || typeof msg !== "object" || !msg.id || !msg.action) return null;

  log(`<- ${msg.action}`, "cmd");
  let reply;
  try {
    reply = await dispatch(msg);
  } catch (e) {
    const aspec = hasOwn(spec.actions, msg.action) ? spec.actions[msg.action] : null;
    reply = { id: msg.id, ok: false, error: core.errorMessage(e), code: "INTERNAL",
              outcome: core.outcomeFor(null, state.last, aspec ? aspec.kind : "read") };
  }

  log(reply.ok ? `-> ${msg.action} ok` : `-> ${msg.action} FAILED: ${reply.error}`, reply.ok ? "ok" : "err");
  if (sock === undefined || sock === null) return reply;
  // Answer on the socket the request came in on; a reconnected socket belongs to newer requests.
  if (sock.readyState === WebSocket.OPEN) {
    sock.send(JSON.stringify(reply));
  } else {
    log(`Reply to ${msg.action} dropped: its connection closed`, "err");
  }
  return reply;
}

/* ---------- connection ---------- */

function connectedText() {
  if (state.stuck) return stuckText();
  if (state.denied) return deniedText();
  return "Connected to broker";
}

function connect() {
  if (state.unloading) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  setStatus("connecting", `Connecting to ${BROKER_URL} ...`);

  let sock;
  try {
    sock = new WebSocket(BROKER_URL);
  } catch (e) {
    setStatus("", "WebSocket unavailable — check manifest network permission");
    log(String(e), "err");
    scheduleReconnect();
    return;
  }
  ws = sock;

  sock.onopen = () => {
    setStatus(state.stuck || state.denied ? "" : "connected", connectedText());
    log("Connected. Registering as panel.", "ok");
    sock.send(JSON.stringify({ role: "panel" }));
  };

  sock.onmessage = (evt) => {
    queue = queue.then(() => handleMessage(evt.data, sock)).catch((e) => log(`Queue error: ${e}`, "err"));
  };

  sock.onclose = () => {
    if (ws !== sock) return;  // replaced by a newer connection
    setStatus("", state.stuck ? stuckText() : (state.denied ? deniedText() : "Disconnected — broker not running?"));
    scheduleReconnect();
  };

  sock.onerror = () => {
    /* onclose follows; nothing extra to do */
  };
}

function scheduleReconnect() {
  if (reconnectTimer || state.unloading) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_MS);
}

/* Panel reload / unload: stop reconnecting, clear timers, close the socket. */
function teardown() {
  if (state.unloading) return;
  state.unloading = true;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  for (const t of watchdogTimers) clearTimeout(t);
  watchdogTimers.clear();
  const sock = ws;
  ws = null;
  if (sock) {
    sock.onopen = null;
    sock.onmessage = null;
    sock.onclose = null;
    sock.onerror = null;
    try { sock.close(); } catch (e) { /* noop */ }
  }
}

if (typeof GLOBAL.addEventListener === "function") {
  GLOBAL.addEventListener("beforeunload", teardown);
  GLOBAL.addEventListener("unload", teardown);
}

document.getElementById("reconnectBtn").addEventListener("click", () => {
  if (state.unloading) return;
  if (ws) { try { ws.close(); } catch (e) { /* noop */ } }
  ws = null;
  connect();
});

/* Test hook (the node harness loads this script in a vm context; `module` is undefined here). */
GLOBAL.__agentBridge = {
  handlers, handleMessage, state, spec, teardown,
  lib: { core, timeline, project: projectLib, effects, capabilities },
};

log(`Agent Bridge ${VERSION} loaded (protocol ${PROTOCOL}, ${MODULES}). Whitelist: ` + dispatchable().join(", "));
if (state.denied) {
  log(deniedText(), "err");
  setStatus("", deniedText());
}
capabilities.startCodeHash();
connect();
