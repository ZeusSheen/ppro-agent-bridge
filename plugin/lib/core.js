/*
 * core: shared plumbing for the Agent Bridge panel.
 *  - BridgeError (every error carries a code from ppro_bridge/actions.json) and the outcome rule
 *  - the spec param check, a mirror of ppro_bridge/spec.py (same rules, same problem texts)
 *  - request guards, checked on the very project / sequence object a handler then uses
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
    "TRANSACTION_FAILED", "PRECONDITION_FAILED",
  ];
  const GUARD_KEYS = ["project", "projectPrefix", "projectGuid", "sequence", "sequenceGuid"];

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

  function checkValue(schema, value, path) {
    const t = hasOwn(schema, "type") ? schema.type : "any";
    if (!typeOk(t, value)) return [`${path}: expected ${t}, got ${pyTypeName(value)}`];
    const problems = [];
    if (hasOwn(schema, "enum") && !schema.enum.some((e) => e === value)) {
      problems.push(`${path}: must be one of ${schema.enum.map(String).join(", ")}`);
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
      }
    }
    return problems;
  }

  /* ---------- guards: project / sequence ---------- */

  function guardOf(ctx) {
    const g = ctx && ctx.guard;
    if (!isPlainObject(g)) return null;
    for (const k of GUARD_KEYS) if (!absent(g[k])) return g;
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

  /* ---------- transactions ---------- */

  /*
   * lockedAccess + executeTransaction. buildActions() returns an Action or an array of Actions. It
   * runs inside the transaction callback (where 0.3.3 created its actions — the pattern verified on
   * 27.1 and used by Adobe's samples), and every action is built before the first addAction, so a
   * throw while building adds nothing. Throws TRANSACTION_FAILED when Premiere reports failure.
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
    TICKS_PER_SECOND, EPS, TIME_NUDGE_S, OUTCOMES, ERROR_CODES, GUARD_KEYS,
    BridgeError, hasOwn, isPlainObject, isFn, errorMessage, codeOf, prefixError, outcomeFor,
    secs, atTime, tickTimeFromTicks, ticksOf, frameTicksOf, seqTicks, seqTime, tryGet, tryGetSync, sleep,
    checkParams, checkValue, checkObject, checkGuardShape, pyTypeName, requireText,
    hasGuard, readGuid, requireProject, requireSequence, checkGuard,
    runTransaction, countWords,
  };
};
