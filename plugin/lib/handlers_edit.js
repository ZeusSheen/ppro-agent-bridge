/*
 * handlers_edit: project and timeline writes (import media, place clips, interpret footage,
 * markers, remove clips). Every write goes through core.runTransaction with a labelled undo step.
 * Factory module returning {actionName: async function (params, ctx)}.
 * deps: { ppro, core, timeline, projectLib }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const timeline = deps.timeline;
  const projectLib = deps.projectLib;
  const BridgeError = core.BridgeError;
  const secs = core.secs;
  const atTime = core.atTime;    // source in/out points only
  const seqTime = core.seqTime;  // sequence positions, ends, offsets: whole frames, exact ticks
  const EPS = core.EPS;

  return {
    /*
     * Import media files into the project root, or into a named top-level bin.
     * params: { paths: [absolute file paths], bin?: "bin name" (created if missing) }
     */
    async import_media(params, ctx) {
      const project = await core.requireProject(ctx);
      const names = async () => (await projectLib.walkProject(project)).map((e) => e.clip.name);
      const before = await names();

      // importFiles(paths, suppressUI, targetBin: ProjectItem, asNumberedStills).
      // Premiere 27.x type-checks targetBin strictly — passing `undefined` throws
      // "Illegal Parameter type" — so resolve a real bin. Root first; some builds
      // only accept the ProjectItem from getInsertionBin(), so fall back to it.
      let ok;
      if (params.bin) {
        const bin = await projectLib.resolveBin(project, params.bin, ctx);
        ctx.sideEffect();
        try {
          ok = await project.importFiles(params.paths, true, bin, false);
        } catch (e) {
          throw new BridgeError("IMPORT_FAILED", `importFiles failed: ${core.errorMessage(e)}`);
        }
      } else {
        ctx.sideEffect();
        try {
          ok = await project.importFiles(params.paths, true, await project.getRootItem(), false);
        } catch (eRoot) {
          try {
            ok = await project.importFiles(params.paths, true, await project.getInsertionBin(), false);
          } catch (eIns) {
            throw new BridgeError("IMPORT_FAILED", `importFiles failed (root bin: ${core.errorMessage(eRoot)}; ` +
              `insertion bin: ${core.errorMessage(eIns)})`);
          }
        }
      }
      if (!ok) throw new BridgeError("IMPORT_FAILED", "importFiles returned false.");

      // Report the project item names that appeared, so callers can address them in insert_clip.
      // Multiset diff: a re-imported file shows up again and signals a duplicate name.
      const pool = [...before];
      const newItems = (await names()).filter((n) => {
        const i = pool.indexOf(n);
        if (i >= 0) { pool.splice(i, 1); return false; }
        return true;
      });
      return { imported: true, count: params.paths.length, bin: params.bin || null, newItems };
    },

    /*
     * Place a clip (or a source range of it — a "sentence") on the timeline.
     * params: { itemName, binPath?, atSeconds = 0, videoTrack = 0, audioTrack = 0 (0-based, must exist),
     *           mode = "overwrite" | "insert", inSeconds?, outSeconds? (source range), undoLabel? }
     * The clip starts on the frame nearest atSeconds (exact ticks); in/out are source times.
     * With in/out given, runs three transactions: set source in/out -> place -> clear in/out.
     * Each is one undo step, labelled. The range is cleared even when placing fails, so the
     * source item is never left trimmed.
     */
    async insert_clip(params, ctx) {
      core.requireText(params, ["itemName"]);
      const hasRange = typeof params.inSeconds === "number" && typeof params.outSeconds === "number";
      if (hasRange && params.outSeconds <= params.inSeconds) {
        throw new BridgeError("BAD_PARAMS", "outSeconds must be greater than inSeconds.");
      }
      const { project, sequence } = await core.requireSequence(ctx);
      const hit = await projectLib.resolveClip(project, params.itemName, params.binPath);

      const atSeconds = params.atSeconds || 0;
      const vTrack = params.videoTrack || 0;
      const aTrack = params.audioTrack || 0;
      const mode = params.mode === "insert" ? "insert" : "overwrite";
      const label = params.undoLabel || `Agent: place ${params.itemName} @${atSeconds}s`;

      // Premiere adds a track for an index past the end; refuse instead, before any transaction.
      for (const [kind, idx] of [["video", vTrack], ["audio", aTrack]]) {
        const count = await timeline.trackCount(sequence, kind);
        if (idx < 0 || idx >= count) {
          throw new BridgeError("NOT_FOUND", `${timeline.trackLabel(kind, idx)} does not exist (sequence has ${count} ${kind} tracks).`);
        }
      }
      const frameTicks = await core.frameTicksOf(sequence);

      const step = (name, fn) => {
        try { return fn(); } catch (e) { throw core.prefixError(e, `[${name}] `); }
      };

      let rangeOk = false;
      if (hasRange) {
        step("set source in/out", () => core.runTransaction(project, label + " [1/3 set range]",
          () => hit.clip.createSetInOutPointsAction(atTime(params.inSeconds), atTime(params.outSeconds)), ctx));
        rangeOk = true;
      }

      const sequenceEditor = ppro.SequenceEditor.getEditor(sequence);
      let placeError = null;
      try {
        step("place on timeline", () => core.runTransaction(project, label + (hasRange ? " [2/3 place]" : ""), () => {
          const at = seqTime(atSeconds, frameTicks);
          return (mode === "insert")
            ? sequenceEditor.createInsertProjectItemAction(hit.item, at, vTrack, aTrack, true)
            : sequenceEditor.createOverwriteItemAction(hit.item, at, vTrack, aTrack);
        }, ctx));
      } catch (e) {
        placeError = e;
      } finally {
        if (rangeOk) {
          try {
            step("clear source in/out", () => core.runTransaction(project, label + " [3/3 clear range]",
              () => hit.clip.createClearInOutPointsAction(), ctx));
          } catch (e) {
            // The clear did not land: the source item stays trimmed (outcome by the side-effect rule).
            const msg = `${core.errorMessage(e)} (the source range is still set on "${params.itemName}")`;
            if (!placeError) throw new BridgeError(core.codeOf(e), msg);
            throw new BridgeError(core.codeOf(placeError), `${core.errorMessage(placeError)}; also ${msg}`);
          }
        }
      }
      if (placeError) {
        // Place failed. With a range, both range steps committed and cancel out: the timeline and the
        // source item are as before (only undo history entries remain).
        const opts = rangeOk ? { outcome: "not_committed" } : undefined;
        throw new BridgeError(core.codeOf(placeError),
          core.errorMessage(placeError) + (rangeOk ? " (committed: [1/3 set range], [3/3 clear range])" : ""), opts);
      }

      return {
        placed: true, itemName: params.itemName, mode,
        atSeconds, videoTrack: vTrack, audioTrack: aTrack,
        sourceRange: hasRange ? [params.inSeconds, params.outSeconds] : "whole clip",
        undoLabel: label,
      };
    },

    /*
     * Interpret footage: override the frame rate of project items (e.g. 60 -> 59.94).
     * Interpretation changes every use of the item in every sequence, so each
     * reference must name exactly one item; same-named items need a binPath.
     * params: { items: [{ name, binPath? }] (or legacy itemNames: [...]), fps, undoLabel? }
     */
    async set_fps(params, ctx) {
      const refs = Array.isArray(params.items) && params.items.length ? params.items
        : (Array.isArray(params.itemNames) ? params.itemNames.map((name) => ({ name })) : []);
      const project = await core.requireProject(ctx);
      const entries = await projectLib.walkProject(project);
      const hits = [];
      const notFound = [];
      for (const r of refs) {
        const matches = projectLib.matchClips(entries, r.name, r.binPath);
        if (!matches.length) notFound.push(r.name);
        else if (matches.length > 1) {
          throw new BridgeError("AMBIGUOUS", `${matches.length} project items are named "${r.name}" (` +
            matches.map(projectLib.itemRef).join(", ") + "). Name one with its bin path.");
        } else hits.push(matches[0]);
      }
      if (notFound.length) throw new BridgeError("NOT_FOUND", `No clip named: ${notFound.join(", ")}`);
      for (const h of hits) {
        if (projectLib.jobs.runningFor(await projectLib.clipKey(h))) {
          throw new BridgeError("TRANSCRIBE_RUNNING", `Speech-to-Text is running on "${projectLib.itemRef(h)}"; wait for it.`);
        }
      }
      const label = params.undoLabel || `Agent: interpret ${hits.length} clip(s) at ${params.fps} fps`;
      core.runTransaction(project, label, () => hits.map((h) => h.clip.createSetOverrideFrameRateAction(params.fps)), ctx);
      return { fps: params.fps, items: hits.map(projectLib.itemRef), undoLabel: label };
    },

    /*
     * Add a sequence marker (e.g. a fact-check flag the editor sees in the timeline).
     * params: { atSeconds, name, comments?, durationSeconds = 0,
     *           type = "Comment" | "Chapter" | "Segmentation" | "WebLink", colorIndex?, undoLabel? }
     */
    async add_marker(params, ctx) {
      core.requireText(params, ["name"]);
      const { project, sequence } = await core.requireSequence(ctx);
      const type = params.type || "Comment";
      const dur = typeof params.durationSeconds === "number" ? params.durationSeconds : 0;
      const frameTicks = await core.frameTicksOf(sequence);
      const atTicks = core.seqTicks(params.atSeconds, frameTicks);  // on a frame, like every sequence edge
      const markers = await ppro.Markers.getMarkers(sequence);
      const label = params.undoLabel || `Agent: marker "${params.name}" @${params.atSeconds}s`;
      core.runTransaction(project, label, () => markers.createAddMarkerAction(
        params.name, type,
        core.tickTimeFromTicks(atTicks),
        ppro.TickTime.createWithSeconds(dur),
        params.comments || ""
      ), ctx);

      let colorSet = false;
      if (typeof params.colorIndex === "number") {
        const fresh = await ppro.Markers.getMarkers(sequence);
        const mine = (fresh.getMarkers() || []).filter((m) =>
          m.getName() === params.name && Math.abs(secs(m.getStart()) - atTicks / core.TICKS_PER_SECOND) < EPS);
        const m = mine[mine.length - 1];
        if (m) {
          core.runTransaction(project, label + " [color]", () => m.createSetColorByIndexAction(params.colorIndex), ctx);
          colorSet = true;
        }
      }
      return { added: true, name: params.name, atSeconds: params.atSeconds, durationSeconds: dur, type, colorSet, undoLabel: label };
    },

    /*
     * Remove clips from the given tracks within [fromSeconds, toSeconds].
     * mode "inside" (default) only takes clips entirely inside the range;
     * "overlap" takes any clip touching it. One undo step.
     * params: { videoTracks: [], audioTracks: [], fromSeconds, toSeconds, mode?, ripple = false, dryRun?, undoLabel? }
     */
    async remove_items(params, ctx) {
      const vts = params.videoTracks || [];
      const ats = params.audioTracks || [];
      const { project, sequence } = await core.requireSequence(ctx);
      const mode = params.mode === "overlap" ? "overlap" : "inside";

      const targets = [];
      for (const [kind, list] of [["video", vts], ["audio", ats]]) {
        for (const idx of list) {
          const track = await timeline.getTrack(sequence, kind, idx);
          const label = timeline.trackLabel(kind, idx);
          for (const it of await timeline.clipItems(track)) {
            const d = timeline.requireSpan(await timeline.describeItem(it), label);
            const hit = mode === "inside"
              ? d.start >= params.fromSeconds - EPS && d.end <= params.toSeconds + EPS
              : timeline.overlaps(d, params.fromSeconds, params.toSeconds);
            if (hit) targets.push({ it, desc: Object.assign({ track: label }, d) });
          }
        }
      }
      const removed = targets.map((t) => t.desc);
      if (params.dryRun || !targets.length) return { dryRun: !!params.dryRun, mode, removed };

      const label = params.undoLabel || `Agent: remove ${targets.length} clip(s) ${params.fromSeconds}–${params.toSeconds}s`;
      const editor = ppro.SequenceEditor.getEditor(sequence);
      const mediaAny = (ppro.Constants && ppro.Constants.MediaType && ppro.Constants.MediaType.ANY);
      let err = null;
      // The selection only lives inside this callback — no awaits in here.
      ppro.TrackItemSelection.createEmptySelection((sel) => {
        try {
          for (const t of targets) sel.addItem(t.it, true);
          core.runTransaction(project, label, () => editor.createRemoveItemsAction(sel, !!params.ripple, mediaAny, false), ctx);
        } catch (e) { err = e; }
      });
      if (err) throw err;
      return { dryRun: false, mode, removed, undoLabel: label };
    },

    /*
     * Clone the one clip under atSeconds (Premiere's SequenceEditor clone: the copy keeps the clip's
     * effects, volume and scaling) to offsetSeconds (whole frames) later, optionally trackOffset tracks up. Like an
     * overwrite edit, the copy replaces whatever is at the destination. Linked items are not cloned
     * with it: clone video and audio separately. One undo step.
     * params: { kind: "video"|"audio", track, atSeconds, offsetSeconds, trackOffset = 0, undoLabel? }
     */
    async clone_clip(params, ctx) {
      const { project, sequence } = await core.requireSequence(ctx);
      const kind = params.kind;
      const tOff = params.trackOffset || 0;
      const it = await timeline.itemAt(sequence, kind, params.track, params.atSeconds);
      const before = timeline.requireSpan(await timeline.describeItem(it), timeline.trackLabel(kind, params.track));
      await timeline.getTrack(sequence, kind, params.track + tOff);  // the destination must exist
      if (before.start + params.offsetSeconds < -EPS) {
        throw new BridgeError("BAD_PARAMS", `The copy would start at ${(before.start + params.offsetSeconds).toFixed(3)}s, before 0s.`);
      }
      const frameTicks = await core.frameTicksOf(sequence);
      const editor = ppro.SequenceEditor.getEditor(sequence);
      const label = params.undoLabel ||
        `Agent: clone ${before.name} ${timeline.trackLabel(kind, params.track)} by ${params.offsetSeconds}s`;
      // A whole-frame offset: the copy keeps the clip's position relative to the frame grid.
      core.runTransaction(project, label, () => editor.createCloneTrackItemAction(
        it, seqTime(params.offsetSeconds, frameTicks), kind === "video" ? tOff : 0, kind === "audio" ? tOff : 0, true, false), ctx);
      return {
        cloned: true, kind, track: params.track + tOff, source: before,
        start: before.start + params.offsetSeconds, end: before.end + params.offsetSeconds, undoLabel: label,
      };
    },

    /*
     * Trim or move the one clip under atSeconds: start / end (sequence seconds; head / tail trim),
     * inPoint / outPoint (source seconds; also a head / tail trim — the content stays anchored to the
     * timeline), or moveTo (absolute new start, same length; alone). Sequence times land on the nearest
     * whole frame. One undo step; read back afterwards.
     * params: { kind: "video"|"audio", track, atSeconds, start?, end?, inPoint?, outPoint?, moveTo?, undoLabel? }
     */
    async trim_clip(params, ctx) {
      const { project, sequence } = await core.requireSequence(ctx);
      const kind = params.kind;
      const fields = ["start", "end", "inPoint", "outPoint", "moveTo"].filter((k) => typeof params[k] === "number");
      if (!fields.length) throw new BridgeError("BAD_PARAMS", "trim_clip needs start, end, inPoint, outPoint or moveTo.");
      const it = await timeline.itemAt(sequence, kind, params.track, params.atSeconds);
      const tl = timeline.trackLabel(kind, params.track);
      const before = timeline.requireSpan(await timeline.describeItem(it), tl);
      // Measured on 27.1: SetStart / SetInPoint trim the head (start and in point move together, end
      // stays); SetEnd / SetOutPoint trim the tail; createMoveAction moves BY its time (relative), so
      // moveTo (an absolute new start) is turned into the delta from the clip's current start.
      if (fields.includes("moveTo") && fields.length > 1) {
        throw new BridgeError("BAD_PARAMS", "moveTo cannot be combined with trims in one call (the move is computed from the clip's start before the call).");
      }
      const newStart = fields.includes("moveTo") ? params.moveTo : fields.includes("start") ? params.start : before.start;
      if (newStart < -EPS) throw new BridgeError("BAD_PARAMS", `The clip would start at ${newStart.toFixed(3)}s, before 0s.`);
      const frameTicks = await core.frameTicksOf(sequence);
      // start / end / moveTo land on whole frames (exact ticks); the move is the tick distance from the clip's
      // own start, so the moved clip starts exactly on its frame. inPoint / outPoint are source times (nudged):
      // they move the sequence edge by the same source delta, so callers that need the grid use start / end.
      const startTicks = fields.includes("moveTo") ? core.ticksOf(await it.getStartTime()) : null;
      if (fields.includes("moveTo") && startTicks === null) {
        throw new BridgeError("INTERNAL", `[read] ${tl} item "${before.name}" returned no start time in ticks`);
      }
      const make = {
        start: () => it.createSetStartAction(seqTime(params.start, frameTicks)),
        end: () => it.createSetEndAction(seqTime(params.end, frameTicks)),
        inPoint: () => it.createSetInPointAction(atTime(params.inPoint)),
        outPoint: () => it.createSetOutPointAction(atTime(params.outPoint)),
        moveTo: () => it.createMoveAction(core.tickTimeFromTicks(core.seqTicks(params.moveTo, frameTicks) - startTicks)),
      };
      const label = params.undoLabel || `Agent: trim ${before.name} on ${tl} (${fields.join(", ")})`;
      core.runTransaction(project, label, () => fields.map((k) => make[k]()), ctx);
      const after = await timeline.describeItem(it);
      return { trimmed: true, kind, track: params.track, fields, before, after, undoLabel: label };
    },

    /*
     * Shift every clip that starts at or after fromSeconds on the given tracks by deltaSeconds (whole frames), in ONE
     * transaction (one undo step) — the sync-keeping ripple after a cut. Moving left needs the room:
     * refused when a clip that is not moved overlaps [from + delta, from) on a shifted track. Moves run
     * in an order that never overlaps (left shift: earliest first; right shift: latest first).
     * params: { fromSeconds, deltaSeconds, videoTracks?, audioTracks? (default: all tracks), undoLabel? }
     */
    async shift_items(params, ctx) {
      const { project, sequence } = await core.requireSequence(ctx);
      const delta = params.deltaSeconds;
      const from = params.fromSeconds;
      const frameTicks = await core.frameTicksOf(sequence);
      const half = frameTicks / core.TICKS_PER_SECOND / 2;
      const picks = [];
      for (const kind of ["video", "audio"]) {
        const asked = kind === "video" ? params.videoTracks : params.audioTracks;
        const count = await timeline.trackCount(sequence, kind);
        const idxs = Array.isArray(asked) ? asked : [...Array(count).keys()];
        for (const idx of idxs) picks.push([kind, idx]);
      }
      const moves = [];
      for (const [kind, idx] of picks) {
        const track = await timeline.getTrack(sequence, kind, idx);
        const tl = timeline.trackLabel(kind, idx);
        for (const it of await timeline.clipItems(track)) {
          const d = timeline.requireSpan(await timeline.describeItem(it), tl);
          if (d.start >= from - half) {
            moves.push({ it, track: tl, name: d.name, from: d.start, to: d.start + delta });
          } else if (delta < 0 && d.end > from + delta + half) {
            throw new BridgeError("PRECONDITION_FAILED",
              `${tl}: "${d.name}" (${d.start.toFixed(3)}–${d.end.toFixed(3)}s) is in the way of a ${delta}s shift from ${from}s.`);
          }
        }
      }
      if (!moves.length) return { shifted: [], deltaSeconds: delta, undoLabel: null };
      moves.sort((a, b) => (delta < 0 ? a.from - b.from : b.from - a.from));
      const first = delta < 0 ? moves[0] : moves[moves.length - 1];
      if (first.to < -EPS) {
        throw new BridgeError("PRECONDITION_FAILED",
          `${first.track}: "${first.name}" would start at ${first.to.toFixed(3)}s, before 0s.`);
      }
      const label = params.undoLabel || `Agent: shift ${moves.length} clip(s) from ${from}s by ${delta}s`;
      // A whole-frame delta for every clip: their positions relative to each other (and to the grid) stay exact.
      core.runTransaction(project, label, () => moves.map((m) => m.it.createMoveAction(seqTime(delta, frameTicks))), ctx);
      return {
        shifted: moves.map((m) => ({ track: m.track, name: m.name, from: m.from, to: m.to })),
        deltaSeconds: delta, undoLabel: label,
      };
    },
  };
};
