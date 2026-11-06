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
 *  - Broker is expected to bind loopback only (127.0.0.1 and ::1); the panel connects to ws://localhost.
 *  - Pairing (lib/pairing.js): the panel proves the pairing token and runs no request, and sends nothing but the
 *    hello and its proof, until the broker has proved the token back; a program that cannot is dropped (fail closed).
 *  - No exception may reach UXP uncaught (see "error guard").
 *
 * Modules: this file is the only one that requires local files. lib/spec.js is generated data;
 * every other lib/*.js is a factory `module.exports = function (deps) { ... }` wired up here.
 * API usage patterns verified against AdobeDocs/uxp-premiere-pro-samples and live on Premiere 27.1.
 */

const ppro = require("premierepro");
const uxp = require("uxp");
const spec = require("./lib/spec.js");
const build = require("./lib/build.js");  // { dev, packaged? }: a release build registers no devOnly action

// The global object (globalThis; window on a host without it): test hooks and the host-table override live here.
const GLOBAL = typeof globalThis !== "undefined" ? globalThis : (typeof window !== "undefined" ? window : {});

const VERSION = spec.version;
const PROTOCOL = spec.protocol;
const MODULES = require.bundle === true ? "bundle" : "commonjs";
const BROKER_URL = "ws://localhost:8017";  // a host name: the manifest's network domains take no IP and no port
const PLUGIN_ID = "org.pproagentbridge.panel";  // manifest.json "id"; the hello names it, the broker checks it
const RECONNECT_MS = 3000;
const REFUSED_RECONNECT_MAX_MS = 30000;  // retry delay ceiling while the broker refuses this panel
const WATCHDOG_GRACE_S = 10;   // a handler this far past its timeout counts as stuck
const CODE_HASH_WAIT_MS = 1500;

let ws = null;
let reconnectTimer = null;
let refusals = 0;               // refusals in a row: each doubles the retry delay
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
  refused: null,        // {reason, closeCode, since, kind?} while the broker refuses this panel (another one is
                        // live), or (kind "pairing") the program on the port did not prove the pairing token
  tokenPath: null,      // where the pairing token was read last (the log says so when it changes)
  unloading: false,
  broker: null,         // the broker's version, from its "registered" notice
  update: null,         // the broker's update check (lib/support.js cleanUpdate), from "registered" / "update"
};
const recentErrors = [];  // the last failed requests and panel errors, for the problem report (lib/support.js)

/* ---------- tiny UI helpers ---------- */

const brand = document.getElementById("brand");
const hint = document.getElementById("hint");
const guide = document.getElementById("guide");
const promptEl = document.getElementById("prompt");
let statusCls = "";
let bridgeFolder = null;  // the folder of start_bridge.bat (lib/launcher.js): the prompt tells the AI helper where it is
let helperSeen = false;   // an AI helper sent a command (other than ping): the first-run steps are done

/* The panel's own words follow Premiere's UI language: Chinese for a zh_* Premiere, English otherwise. Stuck, denied
   and refused texts stay English (rare, and what the docs quote). */
const LANG = /^zh/i.test(String((uxp.host && uxp.host.uiLocale) || "")) ? "zh" : "en";
const UI = {
  en: {
    connected: "Connected to broker",
    connecting: (url) => `Connecting to ${url} ...`,
    pairing: "Pairing with the broker ...",
    disconnected: "Disconnected — broker not running?",
    start: "Start bridge", starting: "Starting ...", reconnect: "Reconnect",
    ready: "Ready. To start editing:",
    seen: "Connected. Commands from your AI helper show up below.",
    looking: "Looking for the bridge on this computer ...",
    notRunning: "The bridge is not running. Click Start bridge, or double-click start_bridge.bat in the bridge " +
      "folder, and keep its window open while you edit.",
    step1: "1. Open Claude Code or Codex in a folder for this job, for example your video project's folder.",
    note1: "That folder becomes the helper's working folder: it saves transcripts, exported frames and edit plans " +
      "there. The bridge itself can stay where it is installed.",
    step2: "2. Copy this message (click the box, then Ctrl+C), paste it into the chat box of Claude Code or Codex " +
      "and press Enter:",
    note2: "Why: it tells your helper where this bridge is installed on your computer (found automatically), to read " +
      "the bridge's instructions, and to check that it can see Premiere. After that, ask for edits in your own " +
      "words, as you would ask an editor.",
    prompt: (where) => `Help me edit in Premiere Pro with the Premiere Pro Agent Bridge ${where}. First read SKILL.md ` +
      "in that folder. Then run `python -m ppro_bridge ping` there to check that the bridge is connected, and tell " +
      "me what is on my active sequence. If something is not set up yet, tell me in plain words what to do.",
    at: (folder) => `installed at ${folder}`,
    somewhere: "installed on this computer (the folder with start_bridge.bat)",
    updateAvailable: (v) => `${v} available`, view: "View", skip: "Skip this version",
    report: "Copy a problem report", issues: "GitHub Issues", close: "Close",
    reportNote: "This is the whole report: versions, Premiere's version, capabilities, the connection and recent " +
      "error codes. No project names, paths or token. Click the box, press Ctrl+C, and paste it into a GitHub issue.",
    openReason: "Agent Bridge opens a GitHub page in your browser.",
  },
  zh: {
    connected: "已连接",
    connecting: (url) => `正在连接 ${url} ……`,
    pairing: "正在与桥配对……",
    disconnected: "未连接：桥没有在运行？",
    start: "启动桥", starting: "正在启动……", reconnect: "重新连接",
    ready: "准备好了。开始剪辑只要两步：",
    seen: "已连接。AI 助手发来的命令会显示在下面。",
    looking: "正在这台电脑上找桥……",
    notRunning: "桥没有在运行。点「启动桥」，或者双击桥文件夹里的 start_bridge.bat；剪辑期间别关它的窗口。",
    step1: "1. 打开 Claude Code 或 Codex，选一个放这次工作的文件夹，比如这个视频项目的文件夹。",
    note1: "这个文件夹就是助手的工作文件夹：转录、导出的画面和剪辑计划都会存在这里。桥本身装在哪里不用动。",
    step2: "2. 复制下面这段话（点一下框，再按 Ctrl+C），粘贴到 Claude Code 或 Codex 的对话框里，按回车发送：",
    note2: "为什么要发这段话：它告诉助手桥装在你电脑的哪里（面板自动找到的），让它先读桥的说明书，再确认能连上 " +
      "Premiere。之后用你自己的话提剪辑要求就行，就像跟剪辑师说话一样。",
    prompt: (where) => `请通过${where}的 Premiere Pro Agent Bridge 帮我在 Premiere Pro 里剪辑。先读这个文件夹里的 ` +
      "SKILL.md，然后在这个文件夹里运行 `python -m ppro_bridge ping`，确认桥已经连上，再告诉我当前活动序列里有什么。" +
      "如果哪里还没装好，请用大白话告诉我该怎么做。",
    at: (folder) => `安装在 ${folder} `,
    somewhere: "这台电脑上（有 start_bridge.bat 的那个文件夹里）",
    updateAvailable: (v) => `有新版本 ${v}`, view: "查看", skip: "跳过此版本",
    report: "复制问题报告", issues: "GitHub Issues", close: "关闭",
    reportNote: "这就是报告的全部内容：版本、Premiere 版本、功能、连接状态和最近的错误代码。没有项目名、路径和令牌。" +
      "点一下框里，再按 Ctrl+C，贴到 GitHub Issues 里。",
    openReason: "Agent Bridge 将在浏览器里打开一个 GitHub 页面。",
  },
}[LANG];

/* The next step for someone who has just opened the panel. Stuck, denied, refused and unloaded panels say theirs in
   the status line. */
function hintText(cls) {
  if (state.stuck || state.denied || state.refused || state.unloading) return "";
  if (cls === "connected") return helperSeen ? UI.seen : UI.ready;
  if (cls === "connecting") return UI.looking;
  return UI.notRunning;
}

/* The first message for the AI helper: where this bridge is installed (wherever that is), so the helper can be
   started in any folder and do the rest itself. */
function promptText() {
  return UI.prompt(bridgeFolder ? UI.at(bridgeFolder) : UI.somewhere);
}

/* The hint line, and the two first-run steps (open Claude Code or Codex, paste the prompt) while connected and no
   AI helper has sent a command yet. */
function refreshHint() {
  hint.textContent = hintText(statusCls);
  guide.style.display = statusCls === "connected" && !helperSeen && hint.textContent ? "block" : "none";
  const text = promptText();
  if (promptEl.value !== text) promptEl.value = text;
}
for (const id of ["step1", "note1", "step2", "note2", "reconnect"]) {
  document.getElementById(id === "reconnect" ? "reconnectBtn" : id).textContent = UI[id];
}
const dot = document.getElementById("dot");
const statusText = document.getElementById("statusText");
const logEl = document.getElementById("log");

function setStatus(cls, text) {
  dot.className = cls; // "", "connecting", "connected"
  brand.className = cls; // the header icon is full colour only while connected
  statusCls = cls;
  refreshHint();
  statusText.textContent = text;
  refreshStartButton(cls);
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

/* ---------- error guard ---------- */

/* An exception that reaches UXP uncaught can break Premiere's scripting thread (Kaltura's panel crashed Premiere on
 * the next ClipSelectionChanged). Socket handlers, timers and buttons run through safely(); the global listeners
 * log whatever still escapes. Logged, never rethrown. Measured on UXP 9.4 (Premiere 27.1, 2 Nov 2026): an ErrorEvent
 * dispatched on globalThis (=== window) reaches the "error" listener, but an unhandled rejection fires no
 * "unhandledrejection" event, so every promise needs its own .catch (safely() adds one). */
function report(label, e) {
  try {
    log(`${label}: ${e && e.message ? e.message : String(e)}`, "err");
    support.noteError(recentErrors, { label });  // the label only, never e
  } catch (_) { /* the log itself failed: nothing left to tell */ }
}

/* fn wrapped so that neither a throw nor a rejected promise escapes; both are logged under `label`. */
function safely(label, fn) {
  return function guarded(...args) {
    try {
      const out = fn.apply(this, args);
      return out && typeof out.then === "function" ? out.then(undefined, (e) => report(`${label} failed`, e)) : out;
    } catch (e) {
      report(`${label} failed`, e);
      return undefined;
    }
  };
}

if (typeof GLOBAL.addEventListener === "function") {
  GLOBAL.addEventListener("error", (evt) => {
    try {
      report("Uncaught error", evt && (evt.error || evt.message));
      if (evt && typeof evt.preventDefault === "function") evt.preventDefault();
    } catch (_) { /* never rethrow */ }
  });
  GLOBAL.addEventListener("unhandledrejection", (evt) => {
    try {
      report("Unhandled rejection", evt && evt.reason);
      if (evt && typeof evt.preventDefault === "function") evt.preventDefault();
    } catch (_) { /* never rethrow */ }
  });
}

/* ---------- modules ---------- */

const core = require("./lib/core.js")({ ppro, spec, log });
const timeline = require("./lib/timeline.js")({ ppro, core });
const projectLib = require("./lib/project.js")({ ppro, core });
const effects = require("./lib/effects.js")({ ppro, core });
const exportJobs = require("./lib/exportjobs.js")({ ppro, core, log });
const capabilities = require("./lib/capabilities.js")({
  ppro, uxp, core, timeline, log, hostTable: GLOBAL.__agentBridgeHostTable,
});
const support = require("./lib/support.js")({ uxp, actions: Object.keys(spec.actions) });  // update notice, report
/* UXP's own modules for the pairing token (os.homedir, fs.readFile); null where a host (or the test harness) lacks
   one: lib/pairing.js then falls back to localFileSystem. */
function hostModule(name) {
  try { return require(name); } catch (e) { return null; }
}
const pairing = require("./lib/pairing.js")({
  uxp, core, os: hostModule("os"), fs: hostModule("fs"), crypto: GLOBAL.crypto,
});
const handlerDeps = { ppro, core, timeline, projectLib, effects, exportJobs, capabilities, state, log };
const handlerGroups = [
  require("./lib/handlers_read.js")(handlerDeps),
  require("./lib/handlers_edit.js")(handlerDeps),
  require("./lib/handlers_items.js")(handlerDeps),
  require("./lib/handlers_effects.js")(handlerDeps),
  require("./lib/handlers_removals.js")(handlerDeps),
  require("./lib/handlers_transcript.js")(handlerDeps),
  require("./lib/handlers_markers.js")(handlerDeps),
  require("./lib/handlers_export.js")(handlerDeps),
  require("./lib/handlers_ops.js")(handlerDeps),  // apply_ops / query: the whitelisted generic layer
  require("./lib/probe.js")({ ppro, uxp, core, timeline, hostInfo: capabilities.hostInfo }),
];
// Tests that change Premiere exist only in dev builds; a release keeps the read-only diagnostics (probe_api).
if (build.dev === true) handlerGroups.push(require("./lib/smoke.js")(handlerDeps));

state.denied = capabilities.denied;

const hasOwn = core.hasOwn;
const stuckText = () => `Stuck on "${state.stuck.action}" — save your project, then Reload this panel in UXP Developer Tool`;
const deniedText = () => `Ping-only: this Premiere build is denied (${state.denied.reason})`;

/* Actions this panel answers, in spec order. */
const dispatchable = () => Object.keys(spec.actions).filter((n) => hasOwn(handlers, n));

/* Liveness check + version/protocol handshake, capabilities, features (panel behaviours a client may
 * branch on), stuck/denied state.
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
    features: core.FEATURES.slice(),
    loadedAt: state.loadedAt,
    build: { dev: build.dev === true, packaged: build.packaged === true },
  };
  if (caps.probedAt) data.capabilitiesProbedAt = caps.probedAt;
  const hash = await capabilities.codeHashWithin(params.probe ? CODE_HASH_WAIT_MS : 0);
  if (hash) data.codeHash = hash;
  return data;
}

/* The whitelist: handler modules merged in spec order; a mismatch with spec.actions (without the devOnly ones
 * in a release build) is logged. */
const handlers = {};
(function buildHandlers() {
  const merged = Object.assign({ ping }, ...handlerGroups);
  const names = Object.keys(spec.actions).filter((n) => build.dev === true || !spec.actions[n].devOnly);
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
 * -> capability -> run under the watchdog (a write pinned to a timeline fingerprint checks it first,
 * once, before its handler) -> outcome rule on error.
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
  if (aspec && aspec.devOnly && build.dev !== true) {
    return fail("UNKNOWN_ACTION", `"${action}" is a test action that only dev builds register; this panel is a ` +
      "release build (load the panel from a dev checkout to run it).", "not_committed");
  }
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
  const run = Promise.resolve()
    .then(() => core.checkFingerprintGuard(ctx, aspec.kind, params, timeline.fingerprint))
    .then(() => handlers[action](params, ctx));
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
  if (msg.action !== "ping" && !helperSeen) { helperSeen = true; refreshHint(); }
  let reply;
  try {
    reply = await dispatch(msg);
  } catch (e) {
    const aspec = hasOwn(spec.actions, msg.action) ? spec.actions[msg.action] : null;
    reply = { id: msg.id, ok: false, error: core.errorMessage(e), code: "INTERNAL",
              outcome: core.outcomeFor(null, state.last, aspec ? aspec.kind : "read") };
  }

  log(reply.ok ? `-> ${msg.action} ok` : `-> ${msg.action} FAILED: ${reply.error}`, reply.ok ? "ok" : "err");
  if (!reply.ok) support.noteError(recentErrors, { action: msg.action, code: reply.code, outcome: reply.outcome });
  if (sock === undefined || sock === null) return reply;
  // Answer on the socket the request came in on; a reconnected socket belongs to newer requests.
  if (sock.readyState === WebSocket.OPEN) {
    try {
      sock.send(JSON.stringify(reply));
    } catch (e) {
      // A spec action name only: the label reaches the problem report, and a request may name any text (QA-3).
      report(`Reply to ${Object.prototype.hasOwnProperty.call(spec.actions, msg.action) ? msg.action : "a request"} not sent`, e);
    }
  } else {
    log(`Reply to ${msg.action} dropped: its connection closed`, "err");
  }
  return reply;
}

/* ---------- connection ---------- */

function connectedText() {
  if (state.stuck) return stuckText();
  if (state.denied) return deniedText();
  return UI.connected;
}

const retryDelay = () => (refusals ? Math.min(RECONNECT_MS * 2 ** refusals, REFUSED_RECONNECT_MAX_MS) : RECONNECT_MS);

function disconnectedText() {
  if (state.stuck) return stuckText();
  if (state.denied) return deniedText();
  const retry = `retrying in ${Math.round(retryDelay() / 1000)} s`;
  if (state.refused && state.refused.kind === "pairing") return `Not paired: ${state.refused.reason} — ${retry}`;
  if (state.refused) return `Refused: ${state.refused.reason} — ${retry}`;
  return UI.disconnected;
}

/* The broker turned this panel away (close 4409: another live panel holds the bridge; 4403: not our plugin id; 4401:
   the pairing proof did not match its token), or (kind "pairing") pairing failed on this side. */
function noteRefusal(reason, closeCode, kind) {
  if (state.refused) return;
  state.refused = { reason: reason || "refused by the broker", closeCode: closeCode || null, since: Date.now() };
  if (kind) state.refused.kind = kind;
  if (kind === "pairing") {
    support.noteError(recentErrors, { label: "not paired with the broker" });
    log(`Not paired: ${state.refused.reason}.`, "err");
    return;
  }
  support.noteError(recentErrors, { label: `refused by the broker (${closeCode || "?"})` });
  log(`The broker refused this panel: ${state.refused.reason}.` + (closeCode === 4409
    ? " Another Premiere window, or a second copy of this panel, holds the bridge: this one retries and takes over"
      + " once that one closes (Reconnect retries now)." : ""), "err");
}

let pairedSock = null;  // the socket whose broker proved the pairing token: the only one whose requests run

/* Pairing failed on `sock` (no token here, or the program on the port did not prove it knows the token): drop the
   socket without running anything it sent, say why in the status line, back off and try again. */
function failPairing(sock, reason) {
  if (ws !== sock) return;
  ws = null;  // its close event is no longer this panel's business
  if (pairedSock === sock) pairedSock = null;
  refusals += 1;
  if (state.refused && state.refused.kind !== "pairing") state.refused = null;  // say this reason, not an older one
  noteRefusal(reason, null, "pairing");
  try { sock.close(); } catch (e) { /* already closed */ }
  setStatus("", disconnectedText());
  scheduleReconnect();
}

const unproven = (what) => `the program on ${BROKER_URL} ${what}; nothing it sent was run`;

/* Registered after all: the broker proved the pairing token in its "registered" notice. */
function accepted() {
  if (state.refused) log("The broker accepted this panel.", "ok");
  state.refused = null;
  refusals = 0;
  setStatus(state.stuck || state.denied ? "" : "connected", connectedText());
}

/* A broker notice, {"event": "registered"|"refused"|"update", ...} (brokers 0.6+; "update" 0.7+), or null for a
   request (id + action). */
function brokerNotice(raw) {
  if (typeof raw !== "string" || raw.length > 4096 || raw.indexOf('"event"') < 0) return null;
  try {
    const msg = JSON.parse(raw);
    return msg && typeof msg === "object" && typeof msg.event === "string" && !msg.id ? msg : null;
  } catch (e) {
    return null;
  }
}

function connect() {
  if (state.unloading) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  if (!state.refused) setStatus("connecting", UI.connecting(BROKER_URL));

  let sock;
  try {
    sock = new WebSocket(BROKER_URL);
  } catch (e) {
    // UXP throws "Permission denied to the url" when manifest.json's network domains do not cover the URL
    setStatus("", `Cannot open ${BROKER_URL} — check manifest network permission (ws://localhost/)`);
    log(String(e), "err");
    scheduleReconnect();
    return;
  }
  ws = sock;
  let refusedHere = false;  // this socket got a "refused" notice (the close code may not reach us)

  let pair = null;  // {token, nonce, brokerNonce} of this socket's handshake (lib/pairing.js)

  sock.onopen = safely("Connect", async () => {
    if (!state.refused) {
      setStatus(state.stuck || state.denied ? "" : "connecting", state.stuck || state.denied ? connectedText() : UI.pairing);
    }
    log("Connected. Pairing with the broker.", "ok");
    const found = await pairing.readToken();
    if (ws !== sock || sock.readyState !== WebSocket.OPEN) return;
    if (!found.token) {
      failPairing(sock, found.error);
      return;
    }
    if (found.path !== state.tokenPath) {
      state.tokenPath = found.path;
      log(`Pairing token: ${found.path} (${found.via}).`);
    }
    pair = { token: found.token, nonce: pairing.newNonce(), brokerNonce: null };
    sock.send(JSON.stringify({ role: "panel", plugin: PLUGIN_ID, version: VERSION, auth: pairing.SCHEME, nonce: pair.nonce }));
  });

  sock.onmessage = safely("Message", (evt) => {
    const notice = brokerNotice(evt.data);
    if (notice) {
      if (notice.event === "refused") {
        if (!refusedHere) refusals += 1;
        refusedHere = true;
        noteRefusal(notice.error, notice.closeCode);
        setStatus("", disconnectedText());
      } else if (notice.event === "challenge" && ws === sock) {
        if (!pair || pair.brokerNonce || notice.auth !== pairing.SCHEME || !pairing.validNonce(notice.nonce)) {
          failPairing(sock, unproven("sent a pairing challenge out of turn or malformed"));
          return;
        }
        pair.brokerNonce = notice.nonce;
        sock.send(JSON.stringify({ proof: pairing.proof(pair.token, "client", pair.nonce, pair.brokerNonce) }));
      } else if (notice.event === "registered" && ws === sock) {
        if (!pair || !pair.brokerNonce
            || !pairing.proofMatches(notice.proof, pair.token, "broker", pair.nonce, pair.brokerNonce)) {
          failPairing(sock, unproven(typeof notice.proof === "string" ? "sent a pairing proof that does not match "
            + "the token file" : "did not prove the pairing token (a broker older than 0.7, or another program)"));
          return;
        }
        pair.token = null;  // not needed again on this socket
        pairedSock = sock;
        state.broker = support.isVersion(notice.broker) ? notice.broker : null;
        setUpdate(notice.update);
        accepted();
      } else if (notice.event === "update" && ws === sock && pairedSock === sock) {
        setUpdate(notice.update);
        if (typeof notice.error === "string") log(`Skip this version: the broker refused (${notice.error}).`, "err");
      }
      return;
    }
    if (pairedSock !== sock) {  // a request before (or without) the broker's proof: never run (fail closed)
      failPairing(sock, unproven("sent a request before proving the pairing token"));
      return;
    }
    queue = queue.then(() => handleMessage(evt.data, sock)).catch((e) => report("Queue error", e));
  });

  sock.onclose = safely("Disconnect", (evt) => {
    if (ws !== sock) return;  // replaced by a newer connection
    const code = evt ? evt.code : undefined;
    if (refusedHere || code === 4409 || code === 4403 || code === 4401) {
      if (!refusedHere) refusals += 1;
      noteRefusal(evt && evt.reason, code);
    } else {
      // not a refusal: the broker went away (or replaced this socket, 4410), so the next try starts afresh
      if (code === 4410) log(`Disconnected: ${(evt && evt.reason) || "replaced by a newer panel"}`, "err");
      state.refused = null;
      refusals = 0;
    }
    setStatus("", disconnectedText());
    scheduleReconnect();
  });

  sock.onerror = () => {
    /* onclose follows; nothing extra to do */
  };
}

function scheduleReconnect() {
  if (reconnectTimer || state.unloading) return;
  reconnectTimer = setTimeout(safely("Reconnect", () => {
    reconnectTimer = null;
    connect();
  }), retryDelay());
}

/* Panel reload / unload: stop reconnecting, clear timers, close the socket. */
function teardown(evt) {
  if (state.unloading) return;
  state.unloading = true;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  for (const t of watchdogTimers) clearTimeout(t);
  watchdogTimers.clear();
  try { exportJobs.dispose(); } catch (e) { /* noop */ }  // encoder listeners off before the context goes
  const sock = ws;
  ws = null;
  if (sock) {
    sock.onopen = null;
    sock.onmessage = null;
    sock.onclose = null;
    sock.onerror = null;
    try { sock.close(); } catch (e) { /* noop */ }
  }
  // seen only if the host fired the event but kept this page: Reconnect brings the bridge back
  try { setStatus("", `Unloaded (${(evt && evt.type) || "teardown"}) — Reconnect connects again`); } catch (e) { /* gone */ }
}

if (typeof GLOBAL.addEventListener === "function") {
  GLOBAL.addEventListener("beforeunload", safely("Unload", teardown));
  GLOBAL.addEventListener("unload", safely("Unload", teardown));
}

document.getElementById("reconnectBtn").addEventListener("click", safely("Reconnect", () => {
  if (state.unloading) {  // a click proves the page is alive: the host fired unload but kept the panel
    state.unloading = false;
    log("Reconnect after an unload event: this panel is still open, connecting again.", "ok");
  }
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  refusals = 0;  // a refused panel retries now, and backs off afresh
  const old = ws;
  ws = null;
  if (old) { try { old.close(); } catch (e) { /* noop */ } }
  connect();
}));

/* ---------- starting the broker (the Start bridge button) ---------- */

const START_POLL_MS = 1000;    // after a launch, try to connect this often ...
const START_WAIT_MS = 20000;   // ... for this long, then back to RECONNECT_MS
const launcher = require("./lib/launcher.js")({ uxp, core, log });
launcher.resolve().then((r) => {
  if (r && r.path) { bridgeFolder = launcher.parentDir(r.path); refreshHint(); }
}, () => { /* the prompt names the folder in words instead */ });
promptEl.addEventListener("focus", safely("Select the prompt", () => { if (promptEl.select) promptEl.select(); }));

/* Shown while the panel has no broker: hidden while the socket is open, the broker refuses this panel
 * (state.refused) or the panel was unloaded (it does not connect then; Reconnect first), and kept as it is while a
 * connection attempt runs (no flash on load). setStatus calls it, so it may run before this section: it looks the
 * button up and keeps its own state on `state`. */
function refreshStartButton(cls) {
  const btn = document.getElementById("startBtn");
  if (!btn || !btn.style) return;
  const open = !!ws && ws.readyState === WebSocket.OPEN;
  if (state.launching && (open || state.refused)) {  // calls this again, not launching
    if (open) stopStartPoll("Start bridge: connected to the broker.", "ok");
    else stopStartPoll("Start bridge: a broker answers but refuses this panel (see the status line).", "err");
    return;
  }
  if (open || state.refused || state.unloading) btn.style.display = "none";
  else if (cls !== "connecting") btn.style.display = "inline-block";
  btn.disabled = !!state.launching;
  btn.style.opacity = state.launching ? "0.5" : "1"; // a div: no :disabled style (startBridge ignores clicks meanwhile)
  btn.textContent = state.launching ? UI.starting : UI.start;
}

function stopStartPoll(text, cls) {
  if (state.launching && state.launching.timer) clearInterval(state.launching.timer);
  state.launching = null;
  if (text) log(text, cls);
  refreshStartButton();
}

/* The click: open the launcher (lib/launcher.js), then connect every START_POLL_MS until the broker answers or
 * START_WAIT_MS pass; a refusal ends that (the connection's own backoff takes over). Never runs on its own, never
 * throws, one launch at a time, and none while a broker answers. */
async function startBridge() {
  if (state.unloading || state.launching || state.refused || (ws && ws.readyState === WebSocket.OPEN)) return null;
  state.launching = { since: Date.now(), timer: null };
  refreshStartButton();
  let result;
  try {
    result = await launcher.launch();
  } catch (e) {
    result = { ok: false, message: `Start bridge failed: ${core.errorMessage(e)}` };
    log(result.message, "err");
  }
  if (!state.launching) return result;  // a broker answered (or refused) while UXP asked: nothing to wait for
  if (!result.ok || state.unloading) {
    stopStartPoll();
    return result;
  }
  const until = Date.now() + START_WAIT_MS;
  state.launching.timer = setInterval(safely("Start bridge poll", () => {
    if (state.unloading) stopStartPoll();
    else if ((ws && ws.readyState === WebSocket.OPEN) || state.refused) refreshStartButton();  // stops the poll
    else if (Date.now() >= until) {
      stopStartPoll(`Start bridge: no broker after ${START_WAIT_MS / 1000} s. Look at the bridge window it opened `
        + "(port in use? Python or websockets missing?); the panel keeps retrying.", "err");
    } else connect();
  }), START_POLL_MS);
  return result;
}

document.getElementById("startBtn").addEventListener("click", safely("Start bridge", () => startBridge()));
GLOBAL.__agentBridgeLauncher = { launcher, startBridge };

/* ---------- update notice, GitHub pages, problem report (lib/support.js) ---------- */

const updateEl = document.getElementById("update");
const reportBox = document.getElementById("reportBox");
const reportText = document.getElementById("reportText");
const reportStatus = document.getElementById("reportStatus");
for (const [id, key] of [["updateView", "view"], ["updateSkip", "skip"], ["reportBtn", "report"],
  ["issuesBtn", "issues"], ["reportClose", "close"], ["reportNote", "reportNote"]]) {
  document.getElementById(id).textContent = UI[key];
}

/* The quiet line under the header: "0.7.1 available · View · Skip this version" while the broker knows a newer
   release that was not skipped (support.notice); hidden otherwise. */
function refreshUpdate() {
  const n = support.notice(state.update);
  updateEl.style.display = n.show ? "block" : "none";
  document.getElementById("updateText").textContent = n.show ? UI.updateAvailable(n.version) : "";
  document.getElementById("updateViewGroup").style.display = n.view ? "inline" : "none";
}

function setUpdate(u) {
  state.update = support.cleanUpdate(u);
  refreshUpdate();
}

/* Open a page of this repository in the browser (support.openUrl refuses every other URL). */
async function openPage(url) {
  log(`Opening ${url} ...`, "cmd");
  const error = await support.openUrl(url, UI.openReason);
  if (error) log(`Could not open ${url}: ${error}`, "err");
  return error;
}

document.getElementById("updateView").addEventListener("click", safely("View the release", () => {
  const u = support.cleanUpdate(state.update);
  return u && u.url ? openPage(u.url) : null;
}));

document.getElementById("updateSkip").addEventListener("click", safely("Skip this version", () => {
  const msg = support.skipMessage(state.update);
  if (!msg || !ws || ws.readyState !== WebSocket.OPEN || pairedSock !== ws) return;
  ws.send(JSON.stringify(msg));
  log(`Skip this version: ${msg.version} is not shown again (a newer release is).`, "ok");
  state.update = Object.assign({}, state.update, { skipped: true });  // the broker's "update" notice confirms it
  refreshUpdate();
}));

document.getElementById("issuesBtn").addEventListener("click", safely("Open GitHub Issues",
  () => openPage(support.ISSUES_URL)));

/* What the panel knows, as the problem report's whitelist takes it (support.buildReport). */
function reportInfo() {
  const open = !!ws && ws.readyState === WebSocket.OPEN && pairedSock === ws;
  return {
    version: VERSION, protocol: PROTOCOL, modules: MODULES,
    build: { dev: build.dev === true, packaged: build.packaged === true },
    broker: state.broker, hostInfo: capabilities.hostInfo(), capabilities: capabilities.snapshot().flags,
    connection: open && !state.refused ? "connected" : (statusCls === "connecting" ? "connecting" : "disconnected"),
    refused: state.refused ? { closeCode: state.refused.closeCode } : null,
    stuck: state.stuck ? { action: state.stuck.action, seconds: (Date.now() - state.stuck.since) / 1000 } : null,
    denied: state.denied ? { reason: state.denied.reason } : false,
    update: state.update, errors: recentErrors.slice(), created: new Date().toISOString(),
  };
}

/* "Copy a problem report": the whole text in a read-only box; a click into it selects it all (its focus handler) and
   Ctrl+C copies it, as in the first-run box. No Copy button: the panel has no clipboard permission (UXP offers only
   read or read-and-write; 27.1 refuses the call from a manifest below version 5), and a selection made by script from a
   button does not take in UXP's text box (live, 6 Nov 2026: Ctrl+C then copied nothing). */
document.getElementById("reportBtn").addEventListener("click", safely("Problem report", () => {
  reportText.value = support.buildReport(reportInfo());
  reportStatus.textContent = "";
  reportBox.style.display = "block";
  if (typeof reportBox.scrollIntoView === "function") reportBox.scrollIntoView();
}));

function selectReport() {
  if (typeof reportText.focus === "function") reportText.focus();
  if (typeof reportText.select === "function") reportText.select();
}
reportText.addEventListener("focus", safely("Select the report", selectReport));


document.getElementById("reportClose").addEventListener("click", safely("Close the report", () => {
  reportBox.style.display = "none";
}));

/* Test hook (the node harness loads this script in a vm context; `module` is undefined here). */
GLOBAL.__agentBridge = {
  handlers, handleMessage, state, spec, build, teardown, safely,
  lib: { core, timeline, project: projectLib, effects, capabilities, support, pairing },
};

log(`Agent Bridge ${VERSION} ready: ${dispatchable().length} commands ` +
    `(protocol ${PROTOCOL}, ${MODULES}, ${build.dev === true ? "dev" : "release"} build).`);
if (state.denied) {
  log(deniedText(), "err");
  setStatus("", deniedText());
}
capabilities.startCodeHash();
connect();
