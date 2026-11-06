/*
 * exportjobs: renders started by export_sequence {wait: false}, one at a time (lost on panel reload).
 * EncoderManager#exportSequence(..., IMMEDIATELY, ...) returns a promise that settled when the render was done on
 * 27.1 (a 253 s sequence: 79 s). The encoder's render events (progress / complete / error / cancel / queue) and the
 * project's OperationCompleteEvent ExportMediaComplete may or may not fire, and the output file proves nothing while
 * it renders (Kaltura's panel: IMMEDIATELY creates it early and fills it later; 27.1's H.264 writes temp files beside
 * it and the .mp4 at the end: ops.export_video watches both). A job settles from that promise
 * only (true: done; false or a throw: failed, or cancelled when a cancel event or ExportMediaComplete CANCELLED came
 * meanwhile; an error event's message joins the error). The events are not tied to this render (a frame export or the
 * user's own export fires them too), so they never end a job: they set its progress and record what fired: counts per
 * event type and per listener route, the first event's keys and primitive values, the last ending event (endEvent),
 * so a live render shows what this build sends. Live on 27.1 (2 Nov): none fired on any of the four routes during
 * three renders and a frame export, with all 12 listeners added without error; the promise settled every job.
 * Listeners go on through EventManager.addEventListener(target, ...) and the target's own addEventListener, each
 * counted on its own route; they come off LINGER_MS after the job ended and its promise settled (late events still
 * count), or at once on dispose (panel unload). Nothing here may throw into Premiere: every listener body, promise
 * callback, timer and dispose catches everything itself and only logs (an exception reaching Premiere's event
 * dispatch can corrupt its scripting thread and crash it on the next event).
 * export_aaf jobs (kind "aaf") share the table and the one-at-a-time rule: ProjectConverter.exportAAF's promise
 * settles them the same way, with no listeners (no encoder render); while one runs past its hintAfterMs, its view
 * carries its hint (27.1 b21: a modal "AAF export log" holds exportAAF until someone clicks OK).
 * Factory module.   deps: { ppro, core, log? }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const tryGetSync = core.tryGetSync;

  /* Log a caught exception from a callback; never throws itself. */
  function swallowed(where, err) {
    try {
      if (typeof deps.log === "function") deps.log(`export job ${where}: ${core.errorMessage(err)}`, "err");
    } catch (e) { /* noop */ }
  }

  const LINGER_MS = 3000;  // listeners stay this long after a job ended, to count late events
  const KEEP_JOBS = 10;    // finished jobs kept for export_status
  const MAX_KEYS = 40;     // keys recorded of an event object
  // [type, EncoderManager static getter, Constants.EncoderEvent key, the value both have on 27.1]
  const ENCODER_EVENTS = [
    ["progress", "EVENT_RENDER_PROGRESS", "RENDER_PROGRESS", "RenderProgressEvent"],
    ["complete", "EVENT_RENDER_COMPLETE", "RENDER_COMPLETE", "RenderCompleteEvent"],
    ["error", "EVENT_RENDER_ERROR", "RENDER_ERROR", "RenderErrorEvent"],
    ["cancel", "EVENT_RENDER_CANCEL", "RENDER_CANCEL", "RenderCancelEvent"],
    ["queue", "EVENT_RENDER_QUEUE", "RENDER_QUEUE", "RenderQueueEvent"],
  ];
  const PROGRESS_KEYS = ["progress", "percentComplete", "percentage", "percent", "value", "amount"];
  const MESSAGE_KEYS = ["message", "error", "errorMessage", "reason", "description"];

  const list = [];
  let counter = 0;

  /* An event name: the static getter, else the Constants entry, else the 27.1 value. */
  function nameOf(owner, getter, constants, key, fallback) {
    const a = tryGetSync(() => owner[getter]);
    if (typeof a === "string" && a) return a;
    const b = tryGetSync(() => constants[key]);
    return typeof b === "string" && b ? b : fallback;
  }

  /* OperationCompleteState values (27.1: SUCCESS 1, CANCELLED 2, FAILED 3). */
  function operationStates() {
    const c = tryGetSync(() => ppro.Constants.OperationCompleteState) || {};
    const e = tryGetSync(() => ppro.OperationCompleteEvent) || {};
    const pick = (key, getter, fallback) => {
      for (const v of [tryGetSync(() => c[key]), tryGetSync(() => e[getter])]) if (typeof v === "number") return v;
      return fallback;
    };
    return {
      success: pick("SUCCESS", "OPERATION_STATE_SUCCESS", 1),
      cancelled: pick("CANCELLED", "OPERATION_STATE_CANCELLED", 2),
      failed: pick("FAILED", "OPERATION_STATE_FAILED", 3),
    };
  }

  /* Own and inherited property names of an event (native events keep theirs as prototype getters), short of the
   * chain's root (Object.prototype of whichever realm made the object). */
  function keysOf(e) {
    if (e === null || e === undefined) return [];
    if (typeof e !== "object" && typeof e !== "function") return [`<${typeof e}>`];
    const keys = [];
    const add = (k) => { if (k !== "constructor" && keys.indexOf(k) < 0 && keys.length < MAX_KEYS) keys.push(k); };
    let o = e;
    for (let depth = 0; o && tryGetSync(() => Object.getPrototypeOf(o)) !== null && depth < 4; depth++) {
      for (const k of tryGetSync(() => Object.getOwnPropertyNames(o), [])) add(k);
      o = tryGetSync(() => Object.getPrototypeOf(o));
    }
    try { for (const k in e) add(k); } catch (err) { /* a host object may refuse enumeration */ }
    return keys;
  }

  /* The primitive values among `keys` (strings cut at 200 characters). */
  function sampleOf(e, keys) {
    if (e === null || typeof e !== "object") return { value: e === undefined ? null : e };
    const out = {};
    for (const k of keys) {
      const v = tryGetSync(() => e[k]);
      if (typeof v === "number" || typeof v === "boolean" || v === null) out[k] = v;
      else if (typeof v === "string") out[k] = v.length > 200 ? v.slice(0, 200) : v;
    }
    return out;
  }

  /* The progress an event carries as {raw, value 0-1}, or null. 0-100 values are scaled. */
  function progressOf(e) {
    const vals = typeof e === "number" || typeof e === "string" ? [e] : PROGRESS_KEYS.map((k) => tryGetSync(() => e[k]));
    for (const v of vals) {
      const n = typeof v === "string" && v.trim() !== "" ? Number(v) : v;
      if (typeof n !== "number" || !Number.isFinite(n)) continue;
      const value = n > 1 && n <= 100 ? n / 100 : Math.max(0, Math.min(1, n));
      return { raw: n, value: Math.round(value * 10000) / 10000 };
    }
    return null;
  }

  function messageOf(e) {
    if (typeof e === "string") return e;
    for (const k of MESSAGE_KEYS) {
      const v = tryGetSync(() => e[k]);
      if (typeof v === "string" && v) return v;
    }
    return null;
  }

  function settle(job, state, error) {
    if (job.state !== "running") return;
    job.state = state;
    job.settledBy = "promise";
    job.error = error || null;
    job.finishedAt = Date.now();
  }

  function onEvent(job, route, type, e) {
    job.routes[route] = (job.routes[route] || 0) + 1;
    job.events[type] = (job.events[type] || 0) + 1;
    if (!job.eventKeys[type]) {
      job.eventKeys[type] = keysOf(e);
      job.eventSample[type] = sampleOf(e, job.eventKeys[type]);
    }
    job.lastEventAt = Date.now();
    if (type === "progress") {
      const p = progressOf(e);
      if (p) { job.progress = p.value; job.progressRaw = p.raw; }
      return;
    }
    if (type === "queue") return;
    // complete / error / cancel / exportMediaComplete: remembered for the promise's verdict, never an end by itself
    const end = { type, at: Date.now(), message: messageOf(e) };
    if (type === "exportMediaComplete") end.state = tryGetSync(() => e.state);
    job.endEvent = end;
  }

  /* One listener on one route; registration failures are recorded, never thrown. */
  function addListener(job, route, target, name, type) {
    const fn = (e) => {
      try { onEvent(job, route, type, e); } catch (err) { swallowed(`${route} ${name} listener`, err); }
    };
    try {
      if (route.indexOf("EventManager") === 0) ppro.EventManager.addEventListener(target, name, fn);
      else target.addEventListener(name, fn);
      job._listeners.push({ route, target, name, fn });
      job.listeners.push(`${route} ${name}`);
    } catch (err) {
      job.listenerErrors.push(`${route} ${name}: ${core.errorMessage(err)}`);
    }
  }

  function removeListeners(job) {
    for (const l of job._listeners.splice(0)) {
      try {
        if (l.route.indexOf("EventManager") === 0) ppro.EventManager.removeEventListener(l.target, l.name, l.fn);
        else l.target.removeEventListener(l.name, l.fn);
      } catch (err) { swallowed(`removing ${l.route} ${l.name}`, err); }
    }
    if (job._timer) { clearTimeout(job._timer); job._timer = null; }
  }

  /* Listeners come off LINGER_MS after the job ended and its promise settled. */
  function finishLater(job) {
    if (job.state === "running" || job._pending || job._timer || !job._listeners.length) return;
    job._timer = setTimeout(() => {
      try { job._timer = null; removeListeners(job); } catch (err) { swallowed("listener removal", err); }
    }, LINGER_MS);
  }

  /* exportSequence's result settles the job; a cancel / error event seen meanwhile says why it failed (a promise
   * callback, so it never throws). */
  function promiseSettled(job, ok, value) {
    try {
      job._pending = false;
      job.returned = !ok ? "threw" : typeof value === "boolean" ? value : tryGetSync(() => String(value));
      if (ok && value) {
        settle(job, "done");
      } else {
        const call = job.call || "exportSequence";
        const why = ok ? `${call} returned ${job.returned}${job.preset ? ` (preset ${job.preset})` : ""}`
          : `${call} failed (${core.errorMessage(value)}${job.preset ? `; preset ${job.preset}` : ""})`;
        const end = job.endEvent;
        const cancelled = !!end && (end.type === "cancel" ||
          (end.type === "exportMediaComplete" && end.state === operationStates().cancelled));
        if (cancelled) settle(job, "cancelled", `The render was cancelled in Premiere (${why}).`);
        else settle(job, "failed", end && end.message ? `${why}; Premiere: ${end.message}` : why);
      }
      finishLater(job);
    } catch (err) {
      swallowed("promise callback", err);
    }
    return job.state;
  }

  return {
    /* A new running job (the oldest finished ones beyond KEEP_JOBS are dropped). */
    create(fields) {
      counter += 1;
      const job = Object.assign({
        jobId: `export-${Date.now().toString(36)}-${counter}`, state: "running", progress: null, progressRaw: null,
        startedAt: Date.now(), finishedAt: null, lastEventAt: null, endEvent: null, error: null, settledBy: null,
        returned: null, events: {}, eventKeys: {}, eventSample: {}, routes: {}, listeners: [], listenerErrors: [],
        _listeners: [], _pending: true, _timer: null,
      }, fields);
      list.push(job);
      for (let i = 0; list.length > KEEP_JOBS && i < list.length; ) {
        if (list[i].state !== "running" && !list[i]._pending) list.splice(i, 1); else i++;
      }
      return job;
    },

    /* Listen for the encoder's render events and the project's ExportMediaComplete, on both routes. */
    listen(job, encoder, project) {
      const em = core.isFn(ppro.EventManager, "addEventListener");
      const constants = tryGetSync(() => ppro.Constants.EncoderEvent) || {};
      for (const [type, getter, key, fallback] of ENCODER_EVENTS) {
        const name = nameOf(ppro.EncoderManager, getter, constants, key, fallback);
        if (em) addListener(job, "EventManager(encoder)", encoder, name, type);
        if (core.isFn(encoder, "addEventListener")) addListener(job, "encoder", encoder, name, type);
      }
      if (project) {
        const name = nameOf(ppro.OperationCompleteEvent || {}, "EVENT_EXPORT_MEDIA_COMPLETE",
          tryGetSync(() => ppro.Constants.OperationCompleteEvent) || {}, "EXPORT_MEDIA_COMPLETE",
          "OperationCompleteEvent.ExportMediaComplete");
        if (em) addListener(job, "EventManager(project)", project, name, "exportMediaComplete");
        if (core.isFn(project, "addEventListener")) addListener(job, "project", project, name, "exportMediaComplete");
      }
    },

    /* Settle the job from exportSequence's promise; resolves (never rejects) with the job's state once it settled. */
    track(job, promise) {
      return promise.then((v) => promiseSettled(job, true, v), (e) => promiseSettled(job, false, e))
        .catch((err) => { swallowed("promise", err); return job.state; });
    },

    get(jobId) { return list.find((j) => j.jobId === jobId) || null; },
    all() { return list.slice(); },

    /* A job still rendering (or whose exportSequence has not returned): one render at a time. */
    busy() { return list.find((j) => j.state === "running" || j._pending) || null; },

    remove(job) {
      removeListeners(job);
      const i = list.indexOf(job);
      if (i >= 0) list.splice(i, 1);
    },

    /* What export_status reports (plain data). */
    view(job) {
      const end = job.finishedAt || Date.now();
      const out = {
        jobId: job.jobId, kind: job.kind || "render", state: job.state, progress: job.progress,
        progressRaw: job.progressRaw,
        elapsedSeconds: Math.round((end - job.startedAt) / 100) / 10, outputPath: job.outputPath, preset: job.preset,
        workAreaOnly: job.workAreaOnly, sequence: job.sequence, startedAt: job.startedAt, finishedAt: job.finishedAt,
        lastEventAt: job.lastEventAt, endEvent: job.endEvent ? Object.assign({}, job.endEvent) : null, error: job.error,
        settledBy: job.settledBy, returned: job.returned,
        pending: job._pending, events: Object.assign({}, job.events), routes: Object.assign({}, job.routes),
        eventKeys: JSON.parse(JSON.stringify(job.eventKeys)), eventSample: JSON.parse(JSON.stringify(job.eventSample)),
        listeners: job.listeners.slice(), listenerErrors: job.listenerErrors.slice(),
      };
      if (job.options) out.options = JSON.parse(JSON.stringify(job.options));
      if (job.hint && job.state === "running" && end - job.startedAt >= (job.hintAfterMs || 0)) out.hint = job.hint;
      return out;
    },

    /* Panel unload: every listener off now (never throws: teardown goes on to close the socket). */
    dispose() {
      for (const job of list) {
        try { removeListeners(job); } catch (err) { swallowed("dispose", err); }
      }
    },
  };
};
