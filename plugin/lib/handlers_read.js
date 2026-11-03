/*
 * handlers_read: read-only actions (project / sequence info, timeline items, effect chains,
 * project items, frame and OTIO export, transition names), plus set_active_sequence (switches the
 * Timeline) and select_items (sets the Timeline selection): both change nothing in the project.
 * Factory module returning {actionName: async function (params, ctx)}.
 * deps: { ppro, core, timeline, projectLib, effects, exportJobs }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const timeline = deps.timeline;
  const projectLib = deps.projectLib;
  const effects = deps.effects;
  const exportJobs = deps.exportJobs;
  const BridgeError = core.BridgeError;
  const secs = core.secs;
  const tryGet = core.tryGet;
  const EPS = core.EPS;
  const TICKS_PER_SECOND = core.TICKS_PER_SECOND;
  const EXPORT_EARLY_MS = 1000;  // export_sequence {wait: false} waits this long for an early failure (or the end)

  return {
    /* Basic project info (+ guid where the build has one). */
    async get_project(params, ctx) {
      const project = await core.requireProject(ctx);
      const out = { name: project.name };
      try { out.path = project.path; } catch (e) { out.path = null; }
      try {
        const seq = await project.getActiveSequence();
        out.activeSequence = seq ? seq.name : null;
      } catch (e) {
        out.activeSequence = null;
      }
      out.guid = core.readGuid(project);
      return out;
    },

    /* Active sequence info: player position, frame size, frame rate, duration, tracks, guid. */
    async get_sequence(params, ctx) {
      const { sequence } = await core.requireSequence(ctx);
      const out = { name: sequence.name };
      out.playerPositionSeconds = secs(await tryGet(() => sequence.getPlayerPosition()));

      const size = await tryGet(() => sequence.getFrameSize()); // RectF
      out.width = size ? size.width : null;
      out.height = size ? size.height : null;

      // getTimebase() is ticks per frame as a string (e.g. "4237833600" = 59.94 fps).
      const timebase = await tryGet(() => sequence.getTimebase());
      const ticksPerFrame = Number(timebase);
      out.timebase = timebase;
      out.fps = ticksPerFrame > 0 ? Math.round((TICKS_PER_SECOND / ticksPerFrame) * 1000) / 1000 : null;
      out.durationSeconds = secs(await tryGet(() => sequence.getEndTime()));

      out.videoTracks = await timeline.describeTracks(sequence, "video");
      out.audioTracks = await timeline.describeTracks(sequence, "audio");
      out.captionTrackCount = await tryGet(() => sequence.getCaptionTrackCount());
      out.guid = core.readGuid(sequence);
      return out;
    },

    /*
     * Make another sequence the active one (Project#setActiveSequence opens it in the Timeline), by exact name or
     * guid, and read the switch back. Not a read: it changes what every later request works on, but nothing in the
     * project.   params: { name | guid }
     */
    async set_active_sequence(params, ctx) {
      const project = await core.requireProject(ctx);
      if (!core.isFn(project, "getSequences") || !core.isFn(project, "setActiveSequence")) {
        throw new BridgeError("UNSUPPORTED", "This Premiere build cannot list or switch sequences over UXP.");
      }
      const all = ((await project.getSequences()) || []).map((s) => ({ seq: s, name: core.tryGetSync(() => s.name), guid: core.readGuid(s) }));
      const want = params.guid ? all.filter((s) => s.guid === params.guid) : all.filter((s) => s.name === params.name);
      if (!want.length) {
        throw new BridgeError("NOT_FOUND", `No sequence ${params.guid ? "with guid " + params.guid : '"' + params.name + '"'} in this project.`,
          { data: { sequences: all.map((s) => s.name) } });
      }
      if (want.length > 1) {
        throw new BridgeError("AMBIGUOUS", `${want.length} sequences are named "${params.name}": pass guid.`,
          { data: { sequences: want.map((s) => ({ name: s.name, guid: s.guid })) } });
      }
      const target = want[0];
      const before = await tryGet(() => project.getActiveSequence());
      const previous = before ? before.name : null;
      const same = (seq) => !!seq && (seq === target.seq || (!!target.guid && core.readGuid(seq) === target.guid));
      if (same(before)) return { switched: false, activeSequence: target.name, previous, guid: target.guid };
      ctx.sideEffect();
      const ok = await project.setActiveSequence(target.seq);
      const now = await tryGet(() => project.getActiveSequence());
      if (ok === false || !same(now)) {
        throw new BridgeError("TRANSACTION_FAILED", `Premiere did not switch to "${target.name}" (active: ${now ? '"' + now.name + '"' : "none"}).`,
          { outcome: now && previous === now.name ? "not_committed" : "unknown" });
      }
      return { switched: true, activeSequence: now.name, previous, guid: core.readGuid(now) };
    },

    /*
     * Select clips in the Timeline, replacing the selection (Sequence#setSelection inside the TrackItemSelection
     * callback; synchronous and true on 27.1): on the given tracks, the one clip under atSeconds, or the clips in
     * [fromSeconds, toSeconds] (mode inside / overlap, as remove_items); clear empties it. For what only Premiere's
     * panels do (Properties > Enhance Audio acts on every selected clip). Nothing in the project changes and there
     * is no undo step; the selection is read back and compared clip by clip.
     * params: { videoTracks?, audioTracks?, atSeconds? | fromSeconds + toSeconds, mode?, clear?, dryRun? }
     */
    async select_items(params, ctx) {
      const vts = [...new Set(params.videoTracks || [])];
      const ats = [...new Set(params.audioTracks || [])];
      const hasAt = typeof params.atSeconds === "number";
      const hasRange = typeof params.fromSeconds === "number";
      if (params.clear) {
        if (vts.length || ats.length || hasAt || hasRange) {
          throw new BridgeError("BAD_PARAMS", "clear empties the selection: give it no tracks or times.");
        }
      } else {
        if (!vts.length && !ats.length) throw new BridgeError("BAD_PARAMS", "select_items needs videoTracks and/or audioTracks (or clear).");
        if (hasAt === hasRange) throw new BridgeError("BAD_PARAMS", "Give atSeconds or fromSeconds + toSeconds (one of the two).");
      }
      const { sequence } = await core.requireSequence(ctx);
      for (const fn of ["getSelection", "setSelection", "clearSelection"]) {
        if (!core.isFn(sequence, fn)) throw new BridgeError("UNSUPPORTED", `Sequence#${fn} is not available on this Premiere build.`);
      }
      if (!params.clear && !(ppro.TrackItemSelection && core.isFn(ppro.TrackItemSelection, "createEmptySelection"))) {
        throw new BridgeError("UNSUPPORTED", "TrackItemSelection.createEmptySelection is not available on this Premiere build.");
      }
      const mode = params.clear ? "clear" : hasAt ? "at" : params.mode === "overlap" ? "overlap" : "inside";

      const targets = [];
      for (const [kind, list] of [["video", vts], ["audio", ats]]) {
        for (const idx of list) {
          const label = timeline.trackLabel(kind, idx);
          if (mode === "at") {
            const it = await timeline.itemAt(sequence, kind, idx, params.atSeconds);
            targets.push({ it, desc: Object.assign({ track: label }, timeline.requireSpan(await timeline.describeItem(it), label)) });
            continue;
          }
          const track = await timeline.getTrack(sequence, kind, idx);
          for (const it of await timeline.clipItems(track, label)) {
            const d = timeline.requireSpan(await timeline.describeItem(it), label);
            const hit = mode === "inside"
              ? d.start >= params.fromSeconds - EPS && d.end <= params.toSeconds + EPS
              : timeline.overlaps(d, params.fromSeconds, params.toSeconds);
            if (hit) targets.push({ it, desc: Object.assign({ track: label }, d) });
          }
        }
      }
      if ((mode === "inside" || mode === "overlap") && !targets.length) {
        const labels = vts.map((i) => timeline.trackLabel("video", i)).concat(ats.map((i) => timeline.trackLabel("audio", i)));
        throw new BridgeError("NOT_FOUND", `No clip ${mode === "inside" ? "entirely inside" : "touching"} ` +
          `${params.fromSeconds}–${params.toSeconds}s on ${labels.join(", ")}` +
          (mode === "inside" ? " (mode overlap also takes clips crossing its edges)" : "") + "; the selection is unchanged.");
      }
      const selected = targets.map((t) => ({ track: t.desc.track, name: t.desc.name, start: t.desc.start, end: t.desc.end }));
      const current = async () => {
        const sel = await sequence.getSelection();
        const list = sel ? await sel.getTrackItems() : [];
        return Array.isArray(list) ? list : list ? Array.from(list) : [];
      };
      const was = await tryGet(current);
      const before = was ? was.length : null;
      if (params.dryRun) return { dryRun: true, mode, selected, before };

      ctx.sideEffect();
      let returned = null;
      if (mode === "clear") {
        returned = await sequence.clearSelection();
      } else {
        let err = null;
        // The selection only lives inside this callback: no awaits in here.
        ppro.TrackItemSelection.createEmptySelection((sel) => {
          try {
            for (const t of targets) sel.addItem(t.it, true);
            returned = sequence.setSelection(sel);
          } catch (e) { err = e; }
        });
        if (err) throw err;
      }
      // Compared by name and span: the selection's items are other wrappers of the same clips.
      const key = (d) => `${d.name}|${core.fixed6(d.start)}|${core.fixed6(d.end)}`;
      let now;
      try { now = await current(); } catch (e) {
        throw new BridgeError("VERIFY_FAILED", `[read] the selection could not be read back: ${core.errorMessage(e)}`,
          { outcome: "unknown", data: { selected } });
      }
      const got = [];
      for (const it of now) got.push(key(await timeline.describeItem(it)));
      const want = targets.map((t) => key(t.desc));
      if (got.length !== want.length || got.sort().join("\n") !== want.sort().join("\n")) {
        throw new BridgeError("VERIFY_FAILED", `Premiere's selection holds ${got.length} clip(s) after selecting ` +
          `${want.length} (setSelection returned ${returned}).`, { outcome: "committed", data: { selected, after: got.length } });
      }
      return { dryRun: false, mode, selected, before, after: got.length, verified: true };
    },

    /*
     * Every clip on the active sequence's tracks, as JSON. Times are sequence seconds;
     * inPoint/outPoint are source seconds.   params: { kinds = ["video", "audio"] }
     */
    async list_items(params, ctx) {
      const { sequence } = await core.requireSequence(ctx);
      const kinds = Array.isArray(params.kinds) && params.kinds.length ? params.kinds : ["video", "audio"];
      const out = { sequence: sequence.name };
      for (const kind of ["video", "audio"]) {
        if (kinds.includes(kind)) out[kind] = await timeline.listTracks(sequence, kind);  // strict: a failed read is an error
      }
      return out;
    },

    /*
     * Read-only deep look at clips: the whole effect chain (components with occurrence, every
     * param's value, keyframed flag, keyframes in media time) plus the transitions on each track.
     * params: { videoTracks?: [idx] (default all), fromSeconds?, toSeconds?, maxKeyframes = 50 }
     */
    async inspect_items(params, ctx) {
      const { sequence } = await core.requireSequence(ctx);
      const p = params;
      const count = await timeline.trackCount(sequence, "video");
      const idxs = Array.isArray(p.videoTracks) && p.videoTracks.length ? p.videoTracks : [...Array(count).keys()];
      const from = typeof p.fromSeconds === "number" ? p.fromSeconds : -Infinity;
      const to = typeof p.toSeconds === "number" ? p.toSeconds : Infinity;
      const maxKf = p.maxKeyframes || 50;
      const out = { sequence: sequence.name, tracks: [] };
      // Audio tracks only when asked for (their chains hold Volume, Channel Volume, Panner, filters).
      const jobs = idxs.map((i) => ["video", i]);
      if (Array.isArray(p.audioTracks)) for (const i of p.audioTracks) jobs.push(["audio", i]);
      if (Array.isArray(p.audioTracks) && p.audioTracks.length && !(Array.isArray(p.videoTracks) && p.videoTracks.length)) {
        jobs.splice(0, idxs.length);  // audioTracks alone: no video tracks
      }
      for (const [kind, idx] of jobs) {
        const track = await timeline.getTrack(sequence, kind, idx);
        const tl = timeline.trackLabel(kind, idx);
        const clips = [];
        for (const it of await timeline.clipItems(track, tl)) {
          const d = await timeline.describeItem(it);
          if (!(d.start <= to + EPS && d.end >= from - EPS)) continue;
          d.components = await effects.describeChain(it, maxKf);
          clips.push(d);
        }
        const transitions = [];
        for (const tr of kind === "video" ? await timeline.transitionItems(track, tl) : []) {
          const t = {
            name: await tryGet(() => tr.getName()),
            matchName: await tryGet(() => tr.getMatchName()),
            start: secs(await tryGet(() => tr.getStartTime())),
            end: secs(await tryGet(() => tr.getEndTime())),
            duration: secs(await tryGet(() => tr.getDuration())),
          };
          if (t.start === null || (t.start <= to + EPS && t.end >= from - EPS)) transitions.push(t);
        }
        out.tracks.push({ index: idx, kind, label: tl, name: track.name, clips, transitions });
      }
      return out;
    },

    /* Every clip in the project panel with its bin path and media file path. */
    async list_project_items(params, ctx) {
      const project = await core.requireProject(ctx);
      const out = [];
      for (const e of await projectLib.walkProject(project)) {
        out.push({
          name: e.clip.name,
          bin: e.binPath,
          mediaPath: await tryGet(() => e.clip.getMediaFilePath()),
        });
      }
      return { items: out };
    },

    /*
     * Export one frame of the active sequence as an image.
     * params: { dir, name = "frame.png", atSeconds = player position,
     *           width/height = the sequence frame size (so vertical sequences stay vertical) }
     * Known Adobe bug (the official sample says "We do double extension"): the file may land as
     * <name>.png even when <name> ends in .png; both candidates are returned for the CLI to resolve.
     */
    async export_frame(params, ctx) {
      core.requireText(params, ["dir"]);
      const { sequence } = await core.requireSequence(ctx);

      const name = params.name || "frame.png";
      let width = params.width;
      let height = params.height;
      if (!width || !height) {
        const size = await tryGet(() => sequence.getFrameSize());
        width = width || (size && size.width) || 640;
        height = height || (size && size.height) || 360;
      }

      let tick;
      if (typeof params.atSeconds === "number") {
        tick = core.atTime(params.atSeconds);
      } else {
        tick = await sequence.getPlayerPosition();
      }

      const ok = await ppro.Exporter.exportSequenceFrame(sequence, tick, name, params.dir, width, height);
      if (!ok) throw new BridgeError("EXPORT_FAILED", "exportSequenceFrame returned false");

      return {
        exported: true,
        dir: params.dir,
        candidates: [name, name + ".png"], // double-extension bug tolerance
        width, height,
        atSeconds: typeof params.atSeconds === "number" ? params.atSeconds : "playerPosition",
      };
    },

    /* Export the active sequence as OpenTimelineIO.   params: { outputPath (full path ending .otio) } */
    async export_otio(params, ctx) {
      core.requireText(params, ["outputPath"]);
      const { sequence } = await core.requireSequence(ctx);
      const ok = await ppro.ProjectConverter.exportAsOpenTimelineIO(sequence, params.outputPath);
      if (!ok) throw new BridgeError("EXPORT_FAILED", "exportAsOpenTimelineIO returned false");
      return { exported: true, outputPath: params.outputPath };
    },

    /*
     * Render the active sequence to a file with an encoder preset (.epr), inside Premiere
     * (EncoderManager, export immediately; the project is not changed). Measured on 27.1: a 67 s
     * sequence renders to a 48 kHz WAV in ~1.5 s with "Waveform Audio 48kHz 16-bit.epr", a 253 s one to
     * H.264 in 79 s (the promise settled when the file was done).
     * wait (default): the request lasts as long as the render (its timeout: 600 s). wait false: an export job
     * (exportjobs.js) that settles from the promise (events add progress and say why it failed); the reply comes
     * once the render ended, or after EXPORT_EARLY_MS (state "running": poll export_status). A render that fails
     * within EXPORT_EARLY_MS is EXPORT_FAILED and leaves no job, as with wait. One render at a time.
     * params: { outputPath, presetPath, workAreaOnly? = false, wait? = true }
     */
    async export_sequence(params, ctx) {
      // presetPath is required: deriving it from uxp.host.applicationPath failed on 27.1 ("Unable to
      // initialize export!"); the CLI finds Premiere's own WAV preset next to the running executable.
      core.requireText(params, ["outputPath", "presetPath"]);
      const { project, sequence } = await core.requireSequence(ctx);
      const preset = params.presetPath;
      const busy = exportJobs.busy();
      if (busy) {
        throw new BridgeError("EXPORT_RUNNING", `Export job ${busy.jobId} is still rendering ${busy.outputPath} ` +
          `(${busy.state}${busy.state === "running" ? "" : ", exportSequence has not returned"}): wait for it ` +
          "(export_status), or Reload the panel if Premiere is no longer rendering.",
          { data: { jobId: busy.jobId, outputPath: busy.outputPath, state: busy.state } });
      }
      const em = ppro.EncoderManager.getManager();
      const types = ppro.Constants && ppro.Constants.ExportType;
      const type = types && types.IMMEDIATELY !== undefined ? types.IMMEDIATELY : ppro.EncoderManager.EXPORT_IMMEDIATELY;
      if (params.wait === false) {
        const job = exportJobs.create({ outputPath: params.outputPath, preset, workAreaOnly: !!params.workAreaOnly,
          sequence: core.tryGetSync(() => sequence.name) });
        exportJobs.listen(job, em, project);
        let promise;
        try {
          promise = Promise.resolve(em.exportSequence(sequence, type, params.outputPath, preset, !params.workAreaOnly));
        } catch (e) {
          exportJobs.remove(job);
          throw new BridgeError("EXPORT_FAILED", `exportSequence failed (${core.errorMessage(e)}; preset ${preset})`);
        }
        const settled = exportJobs.track(job, promise);
        let timer = null;
        await Promise.race([settled, new Promise((resolve) => { timer = setTimeout(resolve, EXPORT_EARLY_MS); })]);
        clearTimeout(timer);
        if (job.state === "failed" || job.state === "cancelled") {  // refused at once: no job, as with wait
          exportJobs.remove(job);
          throw new BridgeError("EXPORT_FAILED", job.error);
        }
        return { exported: job.state === "done", outputPath: params.outputPath, preset, jobId: job.jobId,
          started: true, state: job.state };
      }
      let ok;
      try {
        ok = await em.exportSequence(sequence, type, params.outputPath, preset, !params.workAreaOnly);
      } catch (e) {
        throw new BridgeError("EXPORT_FAILED", `exportSequence failed (${core.errorMessage(e)}; preset ${preset})`);
      }
      if (!ok) throw new BridgeError("EXPORT_FAILED", `exportSequence returned false (preset ${preset})`);
      return { exported: true, outputPath: params.outputPath, preset };
    },

    /*
     * Export jobs of this panel (export_sequence {wait: false}): state running | done | failed | cancelled,
     * progress 0-1 (null until a progress event carried one), what fired (events, routes, eventKeys / eventSample of
     * each type's first event, endEvent). Makes no Premiere call and checks no guard, like ping: it touches no project
     * or sequence, and a job keeps rendering what it started when the user switches sequences (a guarded poll would
     * fail there). A panel reload forgets its jobs.   params: { jobId? }
     */
    async export_status(params) {
      if (typeof params.jobId === "string") {
        const job = exportJobs.get(params.jobId);
        if (!job) {
          const ids = exportJobs.all().map((j) => j.jobId);
          throw new BridgeError("NOT_FOUND", `No export job ${params.jobId} in this panel (${ids.length ? "jobs: " +
            ids.join(", ") : "none"}); a panel reload forgets its jobs.`, { data: { jobs: ids } });
        }
        return { jobs: [exportJobs.view(job)] };
      }
      return { jobs: exportJobs.all().map((j) => exportJobs.view(j)) };
    },

    /* Video transition match names available in this Premiere (e.g. "AE.Impact_Film_Dissolve"). */
    async list_transition_names(params, ctx) {
      await core.checkGuard(ctx);
      return { names: await ppro.TransitionFactory.getVideoTransitionMatchNames() };
    },
  };
};
