/*
 * core: shared plumbing for the Agent Bridge panel.
 *  - BridgeError (every error carries a code from ppro_bridge/actions.json) and the outcome rule
 *  - the spec param check, a mirror of ppro_bridge/spec.py (same rules, same problem texts)
 *  - request guards, checked on the very project / sequence object a handler then uses, and the
 *    fingerprint guard (the timeline must be the one the request was planned on; sha256Hex / fixed6)
 *  - runTransaction: build every action inside lockedAccess first, then execute them in one transaction
 *  - small time helpers
 * Factory module: module.exports = function (deps) { ...; return api; }   deps: { ppro, spec, log? }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const spec = deps.spec;

  const TICKS_PER_SECOND = 254016000000;
  const EPS = 1e-3; // seconds; tolerance for timeline comparisons
  // Premiere floors source in/out points to whole frames, and a frame boundary that went
  // through float seconds (1.001 -> 1.000999...) lands a tick short and loses the frame.
  // One microsecond (1/16000 of a frame) keeps boundaries on their frame; measured on 27.1.
  // SOURCE points only: sequence edges are kept as given (see seqTime).
  const TIME_NUDGE_S = 1e-6;

  const OUTCOMES = ["not_committed", "committed", "partial", "unknown"];
  // Codes the panel may send (a subset of actions.json "errors"; tests/test_panel.py checks it).
  const ERROR_CODES = [
    "UNKNOWN_ACTION", "BAD_PARAMS", "PANEL_STUCK", "HOST_DENIED", "INTERNAL", "PROJECT_MISMATCH",
    "SEQUENCE_MISMATCH", "NO_PROJECT", "NO_SEQUENCE", "NOT_FOUND", "AMBIGUOUS", "UNSUPPORTED", "KEYFRAMED",
    "KEYFRAME_OUT_OF_RANGE", "NO_TRANSCRIPT", "TRANSCRIPT_EXISTS", "TRANSCRIPT_NOT_IMPORTED",
    "TRANSCRIBE_RUNNING", "TRANSCRIBE_FAILED", "LANGUAGE_UNAVAILABLE", "EXPORT_FAILED", "IMPORT_FAILED",
    "TRANSACTION_FAILED", "PRECONDITION_FAILED", "IN_USE", "VERIFY_FAILED", "EXPORT_RUNNING",
  ];
  // Guard keys that pin the project / sequence (every action but ping and export_status checks them), plus
  // `fingerprint`: the timeline's items.fingerprint, checked on writes that are not dry runs (checkFingerprintGuard).
  const PIN_KEYS = ["project", "projectPrefix", "projectGuid", "sequence", "sequenceGuid"];
  const GUARD_KEYS = PIN_KEYS.concat(["fingerprint"]);
  const FINGERPRINT_RE = /^[0-9a-f]{16}$/;
  // Behaviours a client may branch on, sent in the ping reply (actions.json "features" documents each).
  const FEATURES = ["fingerprintGuard", "shiftItemsPrecise", "exportJobs"];
  // Actions one transaction may hold: a runaway guard, not a working limit. The largest real transaction so far
  // is shift_items moving 41 clips (a 4-minute job); a long timeline (4 tracks x 100 clips) or set_params with a
  // keyframe per word of a 3-minute speech (~450) stays well below it. A build loop gone wrong makes thousands,
  // and Premiere would run them all inside lockedAccess, where the watchdog cannot stop it.
  const MAX_TRANSACTION_ACTIONS = 1000;

  /* An error the CLI can branch on: `code` (and an explicit `outcome` / `data`) travel in the reply. */
  class BridgeError extends Error {
    constructor(code, message, opts) {
      super(message);
      this.code = code;
      if (opts && typeof opts.outcome === "string") this.outcome = opts.outcome;
      if (opts && opts.data !== undefined) this.data = opts.data;
    }
  }

  const hasOwn = (o, k) => o !== null && o !== undefined && Object.prototype.hasOwnProperty.call(o, k);
  const isPlainObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const errorMessage = (e) => String(e && e.message ? e.message : e);

  function isFn(obj, name) {
    try { return obj !== null && obj !== undefined && typeof obj[name] === "function"; } catch (e) { return false; }
  }

  function codeOf(e) {
    return e && typeof e.code === "string" && ERROR_CODES.indexOf(e.code) >= 0 ? e.code : "INTERNAL";
  }

  /* Same code, outcome and data; the message gets a "[step] " style prefix. */
  function prefixError(e, prefix) {
    const opts = {};
    if (e && typeof e.outcome === "string") opts.outcome = e.outcome;
    if (e && e.data !== undefined) opts.data = e.data;
    return new BridgeError(codeOf(e), prefix + errorMessage(e), opts);
  }

  /* The outcome rule: an explicit outcome wins; otherwise not_committed while this request
   * committed nothing, else unknown. Read actions never report unknown. */
  function outcomeFor(e, ctx, kind) {
    let outcome = e && typeof e.outcome === "string" && OUTCOMES.indexOf(e.outcome) >= 0 ? e.outcome : null;
    if (!outcome) outcome = ctx && ctx.sideEffects > 0 ? "unknown" : "not_committed";
    if (kind === "read" && outcome === "unknown") outcome = "not_committed";
    return outcome;
  }

  /* ---------- time helpers ---------- */

  const secs = (t) => (t && typeof t.seconds === "number" ? t.seconds : null);

  /* A source in/out point, or a point to read a frame at, that may sit exactly on a frame boundary
   * (see TIME_NUDGE_S). Never a sequence edge: use seqTime. */
  const atTime = (s) => ppro.TickTime.createWithSeconds(s + TIME_NUDGE_S);

  /* A TickTime for integer ticks (createWithTicks where the build has it). */
  function tickTimeFromTicks(ticks) {
    if (isFn(ppro.TickTime, "createWithTicks")) {
      try { return ppro.TickTime.createWithTicks(String(ticks)); } catch (e) { /* fall through */ }
    }
    return ppro.TickTime.createWithSeconds(ticks / TICKS_PER_SECOND);
  }

  /* Ticks of a TickTime read from Premiere (its exact `ticks` string where present). */
  function ticksOf(t) {
    if (!t) return null;
    const n = typeof t.ticks === "string" && t.ticks !== "" ? Number(t.ticks) : NaN;
    if (Number.isFinite(n)) return Math.round(n);
    return typeof t.seconds === "number" ? Math.round(t.seconds * TICKS_PER_SECOND) : null;
  }

  /* Ticks per frame of a sequence (getTimebase() is a string, "4237833600" = 59.94 fps). Sequence edits
   * need it to land on whole frames, so a build that does not report it is refused before any change. */
  async function frameTicksOf(sequence) {
    const n = Number(await tryGet(() => sequence.getTimebase()));
    if (!Number.isFinite(n) || n <= 0) {
      throw new BridgeError("UNSUPPORTED", "The sequence timebase is unreadable on this build; sequence edits need it to land on whole frames.");
    }
    return Math.round(n);
  }

  /* Seconds -> whole frames of the sequence, as exact ticks (nearest frame). Premiere keeps sequence
   * edges as given, unsnapped (measured on 27.1 b17): an end a few microseconds past a frame boundary
   * shows the clip for that whole next frame, a start past one loses the clip's first frame. Used for
   * every sequence position, end, move / shift delta and clone offset; integers stay exact in a
   * double up to 2^53 ticks (9.8 hours). */
  const seqTicks = (s, frameTicks) => Math.round((s * TICKS_PER_SECOND) / frameTicks) * frameTicks;
  const seqTime = (s, frameTicks) => tickTimeFromTicks(seqTicks(s, frameTicks));

  async function tryGet(fn, fallback = null) {
    try { const v = await fn(); return v === undefined ? fallback : v; } catch (e) { return fallback; }
  }

  function tryGetSync(fn, fallback = null) {
    try { const v = fn(); return v === undefined ? fallback : v; } catch (e) { return fallback; }
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /* ---------- spec param check (mirror of ppro_bridge/spec.py) ---------- */

  function pyTypeName(v) {
    if (v === null || v === undefined) return "NoneType";
    if (typeof v === "boolean") return "bool";
    if (typeof v === "string") return "str";
    if (typeof v === "number") return Number.isInteger(v) ? "int" : "float";
    if (Array.isArray(v)) return "list";
    if (typeof v === "object") return "dict";
    return typeof v;
  }

  function typeOk(kind, value) {
    if (kind === "any") return true;
    if (kind === "string") return typeof value === "string";
    if (kind === "boolean") return typeof value === "boolean";
    if (kind === "number" || kind === "integer") {
      if (typeof value !== "number" || !Number.isFinite(value)) return false;
      return kind === "number" || Number.isInteger(value);
    }
    if (kind === "array") return Array.isArray(value);
    if (kind === "object") return isPlainObject(value);
    throw new Error(`spec: unknown type '${kind}'`);
  }

  const own = (obj, name) => (hasOwn(obj, name) ? obj[name] : undefined);
  const absent = (v) => v === undefined || v === null;

  /* ---------- paths Premiere may open (mirror of ppro_bridge/paths.py: the same rules and texts) ----------
   * A network (UNC) path would make Windows offer the user's credentials to that host; a device path holds no media;
   * a data stream writes what no folder listing shows. Refused in the spec check of every `localPath` param, before
   * any handler runs, so a request that did not come through the CLI's checks cannot hand Premiere one either.
   * / and \ count the same; \\?\<drive>:\ is a local long path (Premiere reports project paths that way). */
  const DRIVE = /^[A-Za-z]:/;
  const DRIVE_ROOT = /^[A-Za-z]:(\\|$)/;

  /* "UNC", "device" or null (local). */
  function remoteKind(text) {
    const s = String(text).trim().replace(/\//g, "\\");
    if (s.startsWith("\\\\?\\")) {
      const rest = s.slice(4);
      if (DRIVE_ROOT.test(rest)) return null;
      return rest.slice(0, 4).toUpperCase() === "UNC\\" ? "UNC" : "device";
    }
    if (s.startsWith("\\\\.\\")) return s.slice(4, 8).toUpperCase() === "UNC\\" ? "UNC" : "device";
    if (s.startsWith("\\??\\") || s === "\\\\." || s === "\\\\?") return "device";
    if (s.startsWith("\\\\")) return "UNC";
    return null;
  }

  function hasStream(text) {
    let s = String(text).trim().replace(/\//g, "\\");
    if (s.startsWith("\\\\?\\") && DRIVE_ROOT.test(s.slice(4))) s = s.slice(4);
    const rest = DRIVE.test(s) ? s.slice(2) : s;
    return rest.indexOf(":") >= 0;
  }

  /* Why Premiere must not be handed this path, or null. */
  function pathProblem(text) {
    const kind = remoteKind(text);
    if (kind === "UNC") {
      return "a network (UNC) path: Premiere would connect to that host with your Windows credentials. Copy the " +
        "file to a local disk, or map a drive letter to the share";
    }
    if (kind === "device") return "a Windows device path (\\\\.\\, \\??\\ or \\\\?\\ without a drive): no media lives there";
    if (hasStream(String(text).trim())) {
      return "an NTFS data stream or a URL (a colon after the drive): only plain local paths are opened";
    }
    return null;
  }

  /* Why this is not a plain file name (no folder, drive or stream), or null: export_frame's name. */
  function nameProblem(text) {
    const s = String(text).trim();
    const bad = !s || s.indexOf("/") >= 0 || s.indexOf("\\") >= 0 || s === "." || s === ".." || s.indexOf(":") >= 0;
    return bad ? "must be a plain file name (no folder, drive or stream)" : null;
  }

  function checkValue(schema, value, path) {
    const t = hasOwn(schema, "type") ? schema.type : "any";
    if (!typeOk(t, value)) return [`${path}: expected ${t}, got ${pyTypeName(value)}`];
    const problems = [];
    if (hasOwn(schema, "enum") && !schema.enum.some((e) => e === value)) {
      problems.push(`${path}: must be one of ${schema.enum.map(String).join(", ")}`);
    }
    if (t === "string") {
      const why = (schema.localPath === true ? pathProblem(value) : null) ||
        (schema.fileName === true ? nameProblem(value) : null);
      if (why) problems.push(`${path}: ${why}`);
    }
    if (t === "number" || t === "integer") {
      if (hasOwn(schema, "minimum") && value < schema.minimum) problems.push(`${path}: must be >= ${schema.minimum}`);
      if (hasOwn(schema, "maximum") && value > schema.maximum) problems.push(`${path}: must be <= ${schema.maximum}`);
    }
    if (t === "array") {
      if (hasOwn(schema, "minItems") && value.length < schema.minItems) {
        problems.push(`${path}: needs at least ${schema.minItems} item(s)`);
      }
      if (hasOwn(schema, "items")) {
        for (let i = 0; i < value.length; i++) {
          for (const p of checkValue(schema.items, value[i], `${path}[${i}]`)) problems.push(p);
        }
      }
    }
    if (t === "object" && hasOwn(schema, "properties")) {
      for (const p of checkObject(schema.properties, value, path, !!schema.additionalProperties, [])) problems.push(p);
    }
    return problems;
  }

  function checkObject(properties, obj, path, additional, requireOneOf) {
    const problems = [];
    if (!additional) {
      for (const k of Object.keys(obj)) if (!hasOwn(properties, k)) problems.push(`${path}.${k}: unknown param`);
    }
    for (const name of Object.keys(properties)) {
      const schema = properties[name];
      const value = own(obj, name);
      if (absent(value)) {
        if (schema.required) problems.push(`${path}.${name}: required`);
        continue;
      }
      for (const p of checkValue(schema, value, `${path}.${name}`)) problems.push(p);
      const partner = schema.pairedWith;
      if (partner && absent(own(obj, partner))) problems.push(`${path}.${name}: needs ${partner} too`);
    }
    for (const group of requireOneOf || []) {
      const present = group.some((n) => {
        const v = own(obj, n);
        return !(absent(v) || v === "" || (Array.isArray(v) && v.length === 0));
      });
      if (!present) problems.push(`${path}: needs one of ${group.join(", ")}`);
    }
    return problems;
  }

  /* Problems with a request's params for one action; an unknown action is one problem. */
  function checkParams(actionName, params) {
    const a = hasOwn(spec.actions, actionName) ? spec.actions[actionName] : null;
    if (!a) return [`unknown action '${actionName}'`];
    if (absent(params)) params = {};
    if (!isPlainObject(params)) return ["params: expected object"];
    return checkObject(a.params, params, "params", false, a.requireOneOf || []);
  }

  /* Required strings that must not be empty either (the spec only types them; 0.3.3 refused ""). */
  function requireText(params, names) {
    const empty = names.filter((n) => params[n] === "");
    if (empty.length) {
      throw new BridgeError("BAD_PARAMS", empty.map((n) => `params.${n}: must not be empty`).join("; "));
    }
  }

  /* The guard travels next to params: an object with GUARD_KEYS, string values (null = absent). */
  function checkGuardShape(guard) {
    if (absent(guard)) return [];
    if (!isPlainObject(guard)) return [`guard: expected object, got ${pyTypeName(guard)}`];
    const problems = [];
    for (const k of Object.keys(guard)) {
      if (GUARD_KEYS.indexOf(k) < 0) problems.push(`guard.${k}: unknown key`);
      else if (!absent(guard[k]) && typeof guard[k] !== "string") {
        problems.push(`guard.${k}: expected string, got ${pyTypeName(guard[k])}`);
      } else if (guard[k] === "") {
        problems.push(`guard.${k}: must not be empty (an empty prefix would match every project)`);
      } else if (k === "fingerprint" && !absent(guard[k]) && !FINGERPRINT_RE.test(guard[k])) {
        problems.push("guard.fingerprint: expected 16 lowercase hex digits (get_sequence's fingerprint)");
      }
    }
    return problems;
  }

  /* ---------- guards: project / sequence ---------- */

  /* The guard when it pins a project or sequence (a fingerprint alone pins neither: reads ignore it). */
  function guardOf(ctx) {
    const g = ctx && ctx.guard;
    if (!isPlainObject(g)) return null;
    for (const k of PIN_KEYS) if (!absent(g[k])) return g;
    return null;
  }

  const hasGuard = (ctx) => guardOf(ctx) !== null;

  function readProp(obj, name) {
    try { return obj[name]; } catch (e) { return undefined; }
  }

  /* guid of a Project / Sequence as a string, or null where the build has none or it does not
   * stringify to an id (a guid pin must never match "[object Object]" on every project). */
  function readGuid(obj) {
    const v = readProp(obj, "guid");
    if (v === undefined || v === null) return null;
    let s;
    try { s = String(v); } catch (e) { return null; }
    return s && s.indexOf("[object") !== 0 ? s : null;
  }

  function checkProjectGuard(project, ctx) {
    const g = guardOf(ctx);
    if (!g) return;
    const name = String(readProp(project, "name"));
    if (!absent(g.project) && name !== g.project) {
      throw new BridgeError("PROJECT_MISMATCH", `Active project is "${name}"; the request is pinned to project "${g.project}".`);
    }
    if (!absent(g.projectPrefix) && name.indexOf(g.projectPrefix) !== 0) {
      throw new BridgeError("PROJECT_MISMATCH", `Active project "${name}" does not start with "${g.projectPrefix}".`);
    }
    if (!absent(g.projectGuid)) {
      const guid = readGuid(project);
      if (guid === null || guid !== g.projectGuid) {
        throw new BridgeError("PROJECT_MISMATCH", `Active project "${name}" has guid ${guid}; the request is pinned to ${g.projectGuid}.`);
      }
    }
  }

  function guardNamesSequence(ctx) {
    const g = guardOf(ctx);
    return !!g && (!absent(g.sequence) || !absent(g.sequenceGuid));
  }

  function checkSequenceGuard(sequence, ctx) {
    const g = guardOf(ctx);
    if (!g || !guardNamesSequence(ctx)) return;
    if (!sequence) {
      throw new BridgeError("SEQUENCE_MISMATCH", "The project has no active sequence; the request is pinned to one.");
    }
    const name = String(readProp(sequence, "name"));
    if (!absent(g.sequence) && name !== g.sequence) {
      throw new BridgeError("SEQUENCE_MISMATCH", `Active sequence is "${name}"; the request is pinned to sequence "${g.sequence}".`);
    }
    if (!absent(g.sequenceGuid)) {
      const guid = readGuid(sequence);
      if (guid === null || guid !== g.sequenceGuid) {
        throw new BridgeError("SEQUENCE_MISMATCH", `Active sequence "${name}" has guid ${guid}; the request is pinned to ${g.sequenceGuid}.`);
      }
    }
  }

  async function activeProject() {
    const project = await ppro.Project.getActiveProject();
    if (!project) throw new BridgeError("NO_PROJECT", "No active project open in Premiere.");
    return project;
  }

  /* The active project, checked against the request's guard (sequence keys too, when present). */
  async function requireProject(ctx) {
    const project = await activeProject();
    checkProjectGuard(project, ctx);
    if (guardNamesSequence(ctx)) checkSequenceGuard(await tryGet(() => project.getActiveSequence()), ctx);
    return project;
  }

  /* The active project and sequence, both checked against the request's guard. */
  async function requireSequence(ctx) {
    const project = await activeProject();
    checkProjectGuard(project, ctx);
    const sequence = await project.getActiveSequence();
    if (!sequence) throw new BridgeError("NO_SEQUENCE", "Project has no active sequence.");
    checkSequenceGuard(sequence, ctx);
    return { project, sequence };
  }

  /* For handlers that need no project: still refuse when a guard is given and does not match. */
  async function checkGuard(ctx) {
    if (hasGuard(ctx)) await requireProject(ctx);
  }

  /* ---------- guards: timeline fingerprint ---------- */

  /*
   * guard.fingerprint (ppro_bridge/items.py fingerprint, the value get_sequence reports): a write that is not a dry
   * run runs only while the active sequence still holds the timeline the request was planned on; otherwise
   * PRECONDITION_FAILED before anything changes. Reads and dry runs ignore it, like the CLI's own check. main.js
   * calls this once per request, before the handler, so the request's own writes never trip it.
   * fingerprintOf(sequence) is timeline.fingerprint (core cannot depend on timeline).
   */
  async function checkFingerprintGuard(ctx, kind, params, fingerprintOf) {
    const g = ctx && isPlainObject(ctx.guard) ? ctx.guard : null;
    const want = g && !absent(g.fingerprint) ? g.fingerprint : null;
    if (!want || kind !== "write" || (params && params.dryRun === true)) return;
    const project = await requireProject(ctx);
    let sequence;
    try {
      sequence = await project.getActiveSequence();
    } catch (e) {
      throw new BridgeError("INTERNAL", `[read] the active sequence could not be read, so the timeline fingerprint ` +
        `cannot be checked; nothing was changed: ${errorMessage(e)}`);
    }
    if (!sequence) {
      throw new BridgeError("PRECONDITION_FAILED", `The project has no active sequence; the request is pinned to ` +
        `timeline fingerprint ${want}. Nothing was changed.`, { data: { expected: want, actual: null } });
    }
    const actual = await fingerprintOf(sequence);
    if (actual !== want) {
      throw new BridgeError("PRECONDITION_FAILED", `The timeline changed since its fingerprint was read (${want}, ` +
        `now ${actual}): someone edited it, or another sequence is active. Nothing was changed; read it again ` +
        "(get_sequence) and re-plan.", { data: { expected: want, actual } });
    }
  }

  /* A number as Python's f"{v:.6f}" prints it (items.fingerprint), with "-0.000000" as "0.000000" on both sides.
   * toFixed rounds an exact tie away from zero where Python rounds it to even; at 6 decimals a double is an exact
   * tie only when 128 * |v| is an odd integer (sample 375 of 48 kHz audio is one), so that case is rounded here.
   * Times and speeds stay far below 1e21, where toFixed would switch to exponent notation. */
  function fixed6(v) {
    const a = Math.abs(v);
    const t = a * 128;  // exact: a power-of-two scale
    let s;
    if (Number.isInteger(t) && t % 2 === 1 && t < 2 ** 40) {
      const lo = Math.floor(a * 1e6);  // a * 1e6 = t * 7812.5 exactly
      const n = lo % 2 === 0 ? lo : lo + 1;
      const frac = n % 1e6;
      s = `${(n - frac) / 1e6}.${String(frac).padStart(6, "0")}`;
    } else {
      s = a.toFixed(6);
    }
    return v < 0 && s !== "0.000000" ? "-" + s : s;
  }

  /* ---------- SHA-256 (the fingerprint; UXP offers no crypto.subtle to count on) ---------- */

  const SHA256_K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];

  /* UTF-8 bytes of a string, like Python's str.encode("utf-8") (a lone surrogate becomes U+FFFD). */
  function utf8Bytes(s) {
    const out = [];
    for (let i = 0; i < s.length; i++) {
      let c = s.charCodeAt(i);
      if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length && s.charCodeAt(i + 1) >= 0xdc00 && s.charCodeAt(i + 1) <= 0xdfff) {
        c = 0x10000 + ((c - 0xd800) << 10) + (s.charCodeAt(i + 1) - 0xdc00);
        i += 1;
      } else if (c >= 0xd800 && c <= 0xdfff) {
        c = 0xfffd;
      }
      if (c < 0x80) out.push(c);
      else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
      else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
      else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
    }
    return out;
  }

  /* SHA-256 of a string's UTF-8 bytes, as lowercase hex (hashlib.sha256(s.encode("utf-8")).hexdigest()). */
  function sha256Hex(text) {
    const bytes = utf8Bytes(String(text));
    const bits = bytes.length * 8;
    bytes.push(0x80);
    while (bytes.length % 64 !== 56) bytes.push(0);
    const hi = Math.floor(bits / 0x100000000);
    const lo = bits >>> 0;
    bytes.push(hi >>> 24, (hi >>> 16) & 255, (hi >>> 8) & 255, hi & 255, lo >>> 24, (lo >>> 16) & 255, (lo >>> 8) & 255, lo & 255);
    const h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    const w = new Array(64);
    const rotr = (x, n) => (x >>> n) | (x << (32 - n));
    for (let off = 0; off < bytes.length; off += 64) {
      for (let i = 0; i < 16; i++) {
        const j = off + i * 4;
        w[i] = (bytes[j] << 24) | (bytes[j + 1] << 16) | (bytes[j + 2] << 8) | bytes[j + 3];
      }
      for (let i = 16; i < 64; i++) {
        const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }
      let [a, b, c, d, e, f, g, k] = h;
      for (let i = 0; i < 64; i++) {
        const t1 = (k + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + SHA256_K[i] + w[i]) | 0;
        const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        k = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      [a, b, c, d, e, f, g, k].forEach((x, i) => { h[i] = (h[i] + x) | 0; });
    }
    return h.map((x) => (x >>> 0).toString(16).padStart(8, "0")).join("");
  }

  /* ---------- transactions ---------- */

  /*
   * lockedAccess + executeTransaction. buildActions() returns an Action or an array of Actions. It
   * runs inside the transaction callback (where 0.3.3 created its actions — the pattern verified on
   * 27.1 and used by Adobe's samples), and every action is built before the first addAction, so a
   * throw while building adds nothing. More than MAX_TRANSACTION_ACTIONS actions are refused the same
   * way (BAD_PARAMS, nothing added). Throws TRANSACTION_FAILED when Premiere reports failure.
   */
  function runTransaction(project, label, buildActions, ctx) {
    if (!ctx || typeof ctx.sideEffect !== "function") {
      throw new BridgeError("INTERNAL", `runTransaction("${label}") needs the request context.`);
    }
    let ok = true;
    let buildError = null;
    project.lockedAccess(() => {
      ok = project.executeTransaction((compoundAction) => {
        let actions;
        try {
          const built = buildActions();
          actions = Array.isArray(built) ? built : [built];
          for (let i = 0; i < actions.length; i++) {
            if (!actions[i]) throw new BridgeError("INTERNAL", `Transaction "${label}": action ${i + 1} could not be created.`);
          }
          if (actions.length > MAX_TRANSACTION_ACTIONS) {
            throw new BridgeError("BAD_PARAMS", `Transaction "${label}" would hold ${actions.length} actions; the panel ` +
              `runs at most ${MAX_TRANSACTION_ACTIONS} in one (a runaway guard). Nothing was changed: split the edit ` +
              "into smaller requests.", { data: { actions: actions.length, max: MAX_TRANSACTION_ACTIONS } });
          }
        } catch (e) {
          buildError = e;  // nothing was added; rethrown below even if Premiere swallows it
          throw e;
        }
        ctx.sideEffect();
        for (const a of actions) compoundAction.addAction(a);
      }, label);
    });
    if (buildError) throw buildError;
    if (!ok) throw new BridgeError("TRANSACTION_FAILED", `Transaction "${label}" returned false.`);
    return true;
  }

  /* ---------- transcripts ---------- */

  /* Words in an Adobe transcript document: tokens with type "word" or no type. */
  function countWords(doc) {
    if (!doc || !Array.isArray(doc.segments)) return 0;
    let n = 0;
    for (const seg of doc.segments) {
      if (!seg || !Array.isArray(seg.words)) continue;
      for (const w of seg.words) if (w && (absent(w.type) || w.type === "word")) n++;
    }
    return n;
  }

  return {
    TICKS_PER_SECOND, EPS, TIME_NUDGE_S, OUTCOMES, ERROR_CODES, GUARD_KEYS, FEATURES, MAX_TRANSACTION_ACTIONS,
    BridgeError, hasOwn, isPlainObject, isFn, errorMessage, codeOf, prefixError, outcomeFor,
    secs, atTime, tickTimeFromTicks, ticksOf, frameTicksOf, seqTicks, seqTime, tryGet, tryGetSync, sleep,
    checkParams, checkValue, checkObject, checkGuardShape, pyTypeName, requireText, pathProblem, nameProblem,
    hasGuard, readGuid, requireProject, requireSequence, checkGuard, checkFingerprintGuard, fixed6, sha256Hex,
    runTransaction, countWords,
  };
};
