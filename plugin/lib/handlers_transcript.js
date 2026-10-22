/*
 * handlers_transcript: Premiere transcripts (list, export, import with read-back verification) and
 * Speech-to-Text jobs (transcribe_clip starts one and returns; list_transcripts reports the job table).
 * Factory module returning {actionName: async function (params, ctx)}.
 * deps: { ppro, core, projectLib }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const projectLib = deps.projectLib;
  const BridgeError = core.BridgeError;
  const tryGet = core.tryGet;
  const jobs = projectLib.jobs;

  const IMPORT_POLL_MS = 100;    // hasTranscript poll interval after an import
  const IMPORT_WAIT_MS = 2000;   // how long an import may take to show up
  const CALLBACK_WAIT_MS = 2000; // TextSegments.importFromJSON(json, cb): how long to wait for cb
  const STT_EARLY_MS = 1500;     // transcribe_clip waits this long for an early rejection

  async function hasTranscript(clip) {
    try { return !!(await ppro.Transcript.hasTranscript(clip)); } catch (e) { return false; }
  }

  /* The clip's transcript as exported by Premiere: {json, parsed, words}; json null when nothing came back. */
  async function exported(clip) {
    let json = null;
    try { json = await ppro.Transcript.exportToJSON(clip); } catch (e) { json = null; }
    if (json === null || json === undefined || json === "") return { json: null, parsed: false, words: 0 };
    json = String(json);
    let doc = null;
    try { doc = JSON.parse(json); } catch (e) { doc = null; }
    return { json, parsed: doc !== null, words: doc ? core.countWords(doc) : 0 };
  }

  /* The transcript the clip already has: {words, json} (0 / null for none; an empty placeholder has 0 words). */
  async function existing(clip) {
    if (!(await hasTranscript(clip))) return { words: 0, json: null };
    const ex = await exported(clip);
    return { words: ex.words, json: ex.json };
  }

  async function existingWords(clip) {
    return (await existing(clip)).words;
  }

  /* After an import: wait up to IMPORT_WAIT_MS for hasTranscript (and, when the clip had a transcript,
   * for the export to change), then read it back. `unchanged`: the export still equals `previousJson`. */
  async function settle(clip, previousJson) {
    let waited = 0;
    for (; waited < IMPORT_WAIT_MS && !(await hasTranscript(clip)); waited += IMPORT_POLL_MS) {
      await core.sleep(IMPORT_POLL_MS);
    }
    let ex = await exported(clip);
    while (previousJson && ex.json === previousJson && waited < IMPORT_WAIT_MS) {
      await core.sleep(IMPORT_POLL_MS);
      waited += IMPORT_POLL_MS;
      ex = await exported(clip);
    }
    ex.unchanged = !!previousJson && ex.json === previousJson;
    return ex;
  }

  const callbackFormAvailable = () => !!ppro.TextSegments && core.isFn(ppro.TextSegments, "importFromJSON");

  /* The typed callback form TextSegments.importFromJSON(json, cb): resolves with what cb (or a
   * returned object / Promise) delivers, undefined when nothing arrives in CALLBACK_WAIT_MS. */
  function callbackImport(json) {
    return new Promise((resolve, reject) => {
      let done = false;
      let timer = null;
      const finish = (v) => { if (!done) { done = true; if (timer) clearTimeout(timer); resolve(v); } };
      const fail = (e) => { if (!done) { done = true; if (timer) clearTimeout(timer); reject(e); } };
      timer = setTimeout(() => finish(undefined), CALLBACK_WAIT_MS);
      try {
        const ret = ppro.TextSegments.importFromJSON(json, (result) => finish(result));
        if (ret && typeof ret.then === "function") ret.then(finish, fail);
        else if (ret && typeof ret === "object") finish(ret);
      } catch (e) {
        fail(e);
      }
    });
  }

  function languageCode(entry) {
    if (typeof entry === "string") return entry;
    if (entry && typeof entry === "object") {
      for (const k of ["code", "languageCode", "id", "locale"]) if (typeof entry[k] === "string") return entry[k];
    }
    return null;
  }

  /* Canonical Premiere language code (case-insensitive match), null when omitted. */
  async function canonicalLanguage(lang) {
    if (lang === undefined || lang === null) return null;
    const T = ppro.Transcript;
    let canonical = lang;
    if (core.isFn(T, "querySupportedLanguages")) {
      const supported = await tryGet(() => T.querySupportedLanguages(), null);
      if (supported && typeof supported.length === "number") {
        const codes = Array.from(supported).map(languageCode).filter((c) => c);
        const hit = codes.find((c) => c.toLowerCase() === String(lang).toLowerCase());
        if (!hit) {
          throw new BridgeError("BAD_PARAMS", `Unknown Speech-to-Text language "${lang}". Supported: ${codes.join(", ")}`);
        }
        canonical = hit;
      }
    }
    if (core.isFn(T, "isLanguagePackAvailable")) {
      const available = await tryGet(() => T.isLanguagePackAvailable(canonical), null);
      if (available === false) {
        throw new BridgeError("LANGUAGE_UNAVAILABLE", `The Speech-to-Text language pack for ${canonical} is not installed ` +
          "(install it in Premiere's Text panel first).");
      }
    }
    return canonical;
  }

  async function refuseRunning(entry) {
    if (jobs.runningFor(await projectLib.clipKey(entry))) {
      throw new BridgeError("TRANSCRIBE_RUNNING", `Speech-to-Text is running on "${projectLib.itemRef(entry)}"; wait for it.`);
    }
  }

  return {
    /*
     * Speech-to-Text jobs (always) and every project clip with its bin and hasTranscript flag
     * (unless jobsOnly). Jobs report hasTranscript live from their clip; no project walk for them.
     */
    async list_transcripts(params, ctx) {
      const out = { jobs: [] };
      if (params.jobsOnly) {
        await core.checkGuard(ctx);
      } else {
        const project = await core.requireProject(ctx);
        const clips = [];
        for (const e of await projectLib.walkProject(project)) {
          clips.push({ name: e.clip.name, bin: e.binPath, hasTranscript: await hasTranscript(e.clip) });
        }
        out.clips = clips;
      }
      for (const j of jobs.all()) {
        out.jobs.push({
          jobId: j.jobId, itemName: j.itemName, binPath: j.binPath, language: j.language, state: j.state,
          error: j.error, startedAt: j.startedAt, finishedAt: j.finishedAt, hasTranscript: await hasTranscript(j.clip),
        });
      }
      return out;
    },

    /*
     * A clip's transcript as Adobe Transcript JSON (string; the CLI writes it to disk).
     * params: { itemName?, binPath? }  Without itemName: the first clip (in binPath, if given) whose
     * transcript has words. An empty placeholder counts as no transcript.
     */
    async export_transcript(params, ctx) {
      const project = await core.requireProject(ctx);
      const entries = await projectLib.walkProject(project);
      if (typeof params.itemName === "string") {
        const hit = await projectLib.resolveClip(project, params.itemName, params.binPath, entries);
        const ref = projectLib.itemRef(hit);
        if (!(await hasTranscript(hit.clip))) {
          throw new BridgeError("NO_TRANSCRIPT", `Clip "${ref}" has no transcript. Run Speech to Text (Text panel or transcribe_clip) first.`);
        }
        const ex = await exported(hit.clip);
        if (!ex.json) throw new BridgeError("NO_TRANSCRIPT", `exportToJSON returned nothing for "${ref}".`);
        if (ex.parsed && ex.words === 0) {
          throw new BridgeError("NO_TRANSCRIPT", `Clip "${ref}" has only an empty transcript placeholder (no words).`);
        }
        return { itemName: hit.clip.name, binPath: hit.binPath, transcriptJson: ex.json };
      }
      for (const e of entries) {
        if (typeof params.binPath === "string" && e.binPath !== params.binPath) continue;
        if (!(await hasTranscript(e.clip))) continue;
        const ex = await exported(e.clip);
        if (ex.json && (!ex.parsed || ex.words > 0)) return { itemName: e.clip.name, binPath: e.binPath, transcriptJson: ex.json };
      }
      throw new BridgeError("NO_TRANSCRIPT", "No clip in this project has a transcript. Run Speech to Text (Text panel or transcribe_clip) first.");
    },

    /*
     * Attach an Adobe-format transcript JSON to a clip and verify it by exporting it back.
     * params: { itemName, binPath?, transcriptJson, replace = false }
     * Static Transcript.importFromJSON first; if the clip still has no words, the typed callback form
     * TextSegments.importFromJSON(json, cb) once. Still no words -> TRANSCRIPT_NOT_IMPORTED (committed).
     */
    async import_transcript(params, ctx) {
      core.requireText(params, ["itemName", "transcriptJson"]);
      const project = await core.requireProject(ctx);
      const hit = await projectLib.resolveClip(project, params.itemName, params.binPath);
      let doc;
      try {
        doc = JSON.parse(params.transcriptJson);
      } catch (e) {
        throw new BridgeError("BAD_PARAMS", `transcriptJson is not valid JSON: ${core.errorMessage(e)}`);
      }
      if (!core.isPlainObject(doc)) throw new BridgeError("BAD_PARAMS", "transcriptJson must be a JSON object.");
      const wordsIn = core.countWords(doc);

      const prior = await existing(hit.clip);
      const before = prior.words;
      if (before > 0 && !params.replace) {
        throw new BridgeError("TRANSCRIPT_EXISTS", `Clip "${projectLib.itemRef(hit)}" already has a transcript (${before} words); ` +
          "pass replace to overwrite it.");
      }
      await refuseRunning(hit);

      const label = `Import transcript onto ${params.itemName}`;
      core.runTransaction(project, label, () => ppro.Transcript.createImportTextSegmentsAction(
        ppro.Transcript.importFromJSON(params.transcriptJson),
        hit.clip
      ), ctx);
      let method = "static";
      let result = await settle(hit.clip, prior.json);
      let callbackError = null;
      if (result.words === 0 && callbackFormAvailable()) {
        method = "callback";
        try {
          const segments = await callbackImport(params.transcriptJson);
          if (segments && typeof segments === "object") {
            core.runTransaction(project, label + " [callback]",
              () => ppro.Transcript.createImportTextSegmentsAction(segments, hit.clip), ctx);
          }
        } catch (e) {
          callbackError = core.errorMessage(e);
        }
        result = await settle(hit.clip, prior.json);
      }
      const out = {
        imported: result.words > 0, itemName: hit.clip.name, binPath: hit.binPath, method,
        replaced: before > 0, wordsIn, wordsOut: result.words,
      };
      if (result.words === 0) {
        if (callbackError) out.callbackError = callbackError;
        throw new BridgeError("TRANSCRIPT_NOT_IMPORTED", `Premiere ran the import but "${projectLib.itemRef(hit)}" still has no words ` +
          "(a known Premiere bug on some builds).", { outcome: "committed", data: out });
      }
      if (result.unchanged) out.warning = "The clip's transcript reads back unchanged after the import (the old one may still be attached).";
      else if (result.words !== wordsIn) out.warning = `Premiere kept ${result.words} of ${wordsIn} words.`;
      return out;
    },

    /*
     * Start Premiere Speech-to-Text on one clip and return at once (poll list_transcripts {jobsOnly}).
     * params: { itemName, binPath?, language? }  The promise is never awaited in the request queue:
     * the job table is updated when it settles; only an early rejection (<= 1.5 s) fails the request.
     */
    async transcribe_clip(params, ctx) {
      core.requireText(params, ["itemName"]);
      const project = await core.requireProject(ctx);
      const language = await canonicalLanguage(params.language);
      const hit = await projectLib.resolveClip(project, params.itemName, params.binPath);
      const ref = projectLib.itemRef(hit);
      if (core.isFn(hit.clip, "isSequence") && (await tryGet(() => hit.clip.isSequence(), false))) {
        throw new BridgeError("UNSUPPORTED", `"${ref}" is a sequence; Speech-to-Text runs on clips.`);
      }
      const words = await existingWords(hit.clip);
      if (words > 0) {
        throw new BridgeError("TRANSCRIPT_EXISTS", `Clip "${ref}" already has a transcript (${words} words).`);
      }
      const key = await projectLib.clipKey(hit);
      if (jobs.runningFor(key)) throw new BridgeError("TRANSCRIBE_RUNNING", `Speech-to-Text is already running on "${ref}".`);

      const job = jobs.add({ key, clip: hit.clip, itemName: hit.clip.name, binPath: hit.binPath, language });
      ctx.sideEffect();
      let promise;
      try {
        promise = Promise.resolve(ppro.Transcript.transcribeClipProjectItem(hit.clip, language ? { language } : {}));
      } catch (e) {
        jobs.remove(job);
        throw new BridgeError("TRANSCRIBE_FAILED", `Speech-to-Text did not start: ${core.errorMessage(e)}`, { outcome: "not_committed" });
      }
      const settled = promise.then(
        (v) => {
          job.finishedAt = Date.now();
          if (v === false) { job.state = "failed"; job.error = "transcribeClipProjectItem returned false"; }
          else job.state = "done";
          return job.state;
        },
        (e) => {
          job.finishedAt = Date.now();
          job.state = "failed";
          job.error = core.errorMessage(e);
          return job.state;
        });
      let timer = null;
      const first = await Promise.race([settled, new Promise((resolve) => { timer = setTimeout(() => resolve("running"), STT_EARLY_MS); })]);
      clearTimeout(timer);
      if (first === "failed") {
        jobs.remove(job);
        throw new BridgeError("TRANSCRIBE_FAILED", `Speech-to-Text failed at once: ${job.error}`, { outcome: "not_committed" });
      }
      return { started: true, jobId: job.jobId, itemName: hit.clip.name, binPath: hit.binPath, language };
    },
  };
};
