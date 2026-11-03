/*
 * handlers_items: project item writes — subclips and relinking media. A subclip goes through core.runTransaction
 * (labelled undo steps); relinking cannot: ClipProjectItem#changeMediaFilePath is a direct call, not an action.
 * Calling conventions are the live-verified ones of lib/smoke.js (Premiere 27.1, 22 Oct 2026).
 * Factory module returning {actionName: async function (params, ctx)}.
 * deps: { ppro, core, timeline, projectLib, effects }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const timeline = deps.timeline;
  const projectLib = deps.projectLib;
  const effects = deps.effects;
  const BridgeError = core.BridgeError;
  const secs = core.secs;
  const tryGet = core.tryGet;

  const mediaType = (key) => (ppro.Constants && ppro.Constants.MediaType ? ppro.Constants.MediaType[key] : undefined);
  /* Windows paths compare case-insensitively, with either slash, without the long-path prefix. */
  const normPath = (p) => String(p || "").replace(/^\\\\\?\\/, "").replace(/\\/g, "/").toLowerCase();
  const samePath = (a, b) => normPath(a) !== "" && normPath(a) === normPath(b);
  const trimBin = (p) => String(p).replace(/^\/+|\/+$/g, "");

  /* A stable key for a raw ProjectItem: ProjectItem#getId, else name + media path (remove_project_items' rule). */
  async function keyOf(item) {
    const id = core.isFn(item, "getId") ? await tryGet(() => item.getId()) : null;
    if (id !== null && id !== undefined && id !== "") return `id:${id}`;
    const clip = ppro.ClipProjectItem.cast(item);
    return `ref:${clip ? clip.name : item.name}|${clip ? await tryGet(() => clip.getMediaFilePath(), "") : "bin"}`;
  }

  /* The clip entry (walkTree) a request names; resolveClip's rules (same name + other media is ambiguous). With
   * `unique`, any second item of that name is ambiguous: the write changes that one item only. */
  async function pickClip(project, clips, name, binPath, unique) {
    const matches = projectLib.matchClips(clips, name, binPath);
    if (unique && matches.length > 1) {
      throw new BridgeError("AMBIGUOUS", `${matches.length} project items are named "${name}" (` +
        matches.map((m) => m.path).join(", ") + "). Name one with its bin path.");
    }
    return projectLib.resolveClip(project, name, binPath, clips);
  }

  async function refuseSequence(entry, what) {
    let isSeq;
    try { isSeq = await entry.clip.isSequence(); } catch (e) { isSeq = null; }
    if (isSeq) throw new BridgeError("UNSUPPORTED", `"${entry.path}" is a sequence: ${what}. Nothing was changed.`);
  }

  /* The item's frame rate as interpreted (FootageInterpretation#getFrameRate, read live on 27.1), or null. */
  async function interpretedFps(clip) {
    if (!core.isFn(clip, "getFootageInterpretation")) return null;
    const fi = await tryGet(() => clip.getFootageInterpretation());
    const v = fi && core.isFn(fi, "getFrameRate") ? effects.plainValue(core.tryGetSync(() => fi.getFrameRate())) : null;
    return typeof v === "number" && Number.isFinite(v) && v > 0 ? v : null;
  }

  /* The media's length in seconds (ClipProjectItem#getMedia -> Media#getDuration; documented, not measured). */
  async function mediaSeconds(clip) {
    if (!core.isFn(clip, "getMedia")) return null;
    const media = await tryGet(() => clip.getMedia());
    if (!media || !core.isFn(media, "getDuration")) return null;
    return secs(await tryGet(() => media.getDuration()));
  }

  /* A project item's own in / out point (ClipProjectItem#getInPoint / getOutPoint(MediaType); documented). */
  async function itemRange(clip, type) {
    if (!core.isFn(clip, "getInPoint") || !core.isFn(clip, "getOutPoint")) return null;
    const a = secs(await tryGet(() => clip.getInPoint(type)));
    const b = secs(await tryGet(() => clip.getOutPoint(type)));
    return a === null || b === null ? null : { inSeconds: a, outSeconds: b };
  }

  /* Where the item with `key` is used, per sequence of the project. Informative only (no write depends on it): a
   * read that fails ends the scan with complete: false and the error, never as "not used". */
  async function usesOf(project, key) {
    const out = { clips: 0, sequences: [], complete: true };
    try {
      for (const s of (await project.getSequences()) || []) {
        const row = { name: s.name, video: 0, audio: 0 };
        for (const kind of ["video", "audio"]) {
          const count = kind === "video" ? await s.getVideoTrackCount() : await s.getAudioTrackCount();
          for (let i = 0; i < count; i++) {
            const track = kind === "video" ? await s.getVideoTrack(i) : await s.getAudioTrack(i);
            for (const it of await timeline.clipItems(track)) {
              const pi = await it.getProjectItem();
              if (pi && (await keyOf(pi)) === key) row[kind] += 1;
            }
          }
        }
        if (row.video || row.audio) {
          out.sequences.push(row);
          out.clips += row.video + row.audio;
        }
      }
    } catch (e) {
      out.complete = false;
      out.error = core.errorMessage(e);
    }
    return out;
  }

  return {
    /*
     * A subclip of a project item: source in/out (seconds, already on the item's frames — the CLI snaps them) with
     * hard boundaries, video and/or audio. It lands in `bin` (a bin path; a missing top-level bin is created in the
     * same undo step), by default the source item's own bin; when Premiere puts it elsewhere it is moved there (a
     * second undo step). A clip of that name already in the target bin is refused (later commands would find two)
     * unless allowDuplicateName. Read back: the new item, its bin and its own in / out point.
     * params: { itemName, binPath?, name, inSeconds, outSeconds, bin?, takeVideo = true, takeAudio = true,
     *           allowDuplicateName?, dryRun?, undoLabel? }
     */
    async subclip(params, ctx) {
      core.requireText(params, ["itemName", "name"]);
      if (params.outSeconds <= params.inSeconds) {
        throw new BridgeError("BAD_PARAMS", "outSeconds must be greater than inSeconds.");
      }
      const takeVideo = params.takeVideo !== false;
      const takeAudio = params.takeAudio !== false;
      if (!takeVideo && !takeAudio) throw new BridgeError("BAD_PARAMS", "takeVideo and takeAudio cannot both be false.");
      const project = await core.requireProject(ctx);
      const tree = await projectLib.walkTree(project);
      const clips = tree.filter((e) => e.kind === "clip");
      const src = await pickClip(project, clips, params.itemName, params.binPath, false);
      await refuseSequence(src, "subclips are made from media items");
      if (!core.isFn(src.clip, "createSubClipAction")) {
        throw new BridgeError("UNSUPPORTED", "ClipProjectItem#createSubClipAction is missing on this build.");
      }
      const fps = await interpretedFps(src.clip);
      const length = await mediaSeconds(src.clip);
      if (length !== null && params.outSeconds > length + (fps ? 0.5 / fps : core.EPS)) {
        throw new BridgeError("BAD_PARAMS", `outSeconds ${params.outSeconds} is past the end of "${src.path}" ` +
          `(${length.toFixed(3)}s). Nothing was changed.`);
      }

      const target = typeof params.bin === "string" ? trimBin(params.bin) : src.binPath;
      const bins = tree.filter((e) => e.kind === "bin");
      let createsBin = false;
      if (target && !bins.some((e) => e.path === target)) {
        if (target.indexOf("/") >= 0) {
          throw new BridgeError("NOT_FOUND", `No bin "${target}" (only a missing top-level bin is created). Bins: ` +
            (bins.map((e) => e.path).join(" | ") || "none"));
        }
        createsBin = true;
      }
      const clash = clips.filter((e) => e.binPath === target && e.name === params.name).map((e) => e.path);
      if (clash.length && !params.allowDuplicateName) {
        throw new BridgeError("PRECONDITION_FAILED", `"${params.name}" already exists in ${target ? `bin "${target}"` :
          "the root bin"}: a second item of that name would make later commands ambiguous. Choose another name (or ` +
          "allow the duplicate). Nothing was changed.", { data: { existing: clash } });
      }
      const label = params.undoLabel || `Agent: subclip "${params.name}" of ${src.path}`;
      const out = {
        dryRun: !!params.dryRun, created: false, source: src.path, name: params.name, binPath: target,
        inSeconds: params.inSeconds, outSeconds: params.outSeconds, takeVideo, takeAudio, hardBoundaries: true,
        createsBin, frameRate: fps, mediaSeconds: length,
        sameNameElsewhere: clips.filter((e) => e.binPath !== target && e.name === params.name).map((e) => e.path),
      };
      if (params.dryRun) return out;

      const before = new Set();
      for (const e of clips) before.add(await keyOf(e.item));
      const root = await project.getRootItem();
      // Live on 27.1: only the object form of the last argument works (the positional booleans threw).
      core.runTransaction(project, label, () => (createsBin ? [root.createBinAction(target, false)] : []).concat([
        src.clip.createSubClipAction(params.name, core.atTime(params.inSeconds), core.atTime(params.outSeconds), true,
          { takeVideo, takeAudio })]), ctx);
      const steps = [label];
      Object.assign(out, { moved: false, undoLabel: label, undoSteps: steps });
      const fresh = async () => {
        const made = [];
        for (const e of await projectLib.walkTree(project)) {
          if (e.kind === "clip" && !before.has(await keyOf(e.item))) made.push(e);
        }
        out.newItems = made.map((e) => e.path);
        return made.find((e) => e.name === params.name) || null;
      };
      let hit = await fresh();
      if (!hit) return out;  // created: false -> the CLI reports VERIFY_FAILED (committed); newItems says what came

      if (hit.binPath !== target) {
        let folder = root;
        if (target) {
          const dest = (await projectLib.walkTree(project)).find((e) => e.kind === "bin" && e.path === target);
          folder = dest ? ppro.FolderItem.cast(dest.item) : null;
        }
        try {
          if (!folder) throw new BridgeError("INTERNAL", `bin "${target}" is not there after the subclip step`);
          const moving = hit;
          core.runTransaction(project, `${label} [move to bin]`,
            () => moving.folder.createMoveItemAction(moving.item, folder), ctx);
        } catch (e) {
          throw new BridgeError(core.codeOf(e), `[move to bin] ${core.errorMessage(e)}; the subclip was made in ` +
            `${hit.binPath ? `bin "${hit.binPath}"` : "the root bin"} (${label})`,
          { outcome: "partial", data: { completed: [label], item: hit.path } });
        }
        steps.push(`${label} [move to bin]`);
        out.moved = true;
        hit = await fresh();
        if (!hit) return out;
      }
      return Object.assign(out, {
        created: true, item: hit.path, binPath: hit.binPath,
        readBack: await itemRange(hit.clip, mediaType(takeVideo ? "VIDEO" : "AUDIO")),
      });
    },

    /*
     * Point a project item at another media file (changeMediaFilePath with the compatibility override, as verified
     * live, then refreshMedia) and read its media path back. The CLI compares the two files first (ffprobe); the
     * panel refuses an item Premiere says cannot be relinked, a sequence, an ambiguous name and a running
     * Speech-to-Text job. Every sequence that uses the item follows: `uses` counts its clips per sequence, and
     * `sameMedia` lists other items (subclips, duplicates) on the old file with their path afterwards. Not an undo
     * step: relink back to previousPath to revert.
     * params: { itemName, binPath?, newPath, dryRun? }
     */
    async relink(params, ctx) {
      core.requireText(params, ["itemName", "newPath"]);
      const project = await core.requireProject(ctx);
      const clips = (await projectLib.walkTree(project)).filter((e) => e.kind === "clip");
      const hit = await pickClip(project, clips, params.itemName, params.binPath, true);
      await refuseSequence(hit, "a sequence has no media file to relink");
      const clip = hit.clip;
      if (!core.isFn(clip, "changeMediaFilePath")) {
        throw new BridgeError("UNSUPPORTED", "ClipProjectItem#changeMediaFilePath is missing on this build.");
      }
      let can;
      try { can = core.isFn(clip, "canChangeMediaPath") ? await clip.canChangeMediaPath() : null; } catch (e) { can = null; }
      if (can !== true) {
        throw new BridgeError("UNSUPPORTED", can === false
          ? `Premiere says the media path of "${hit.path}" cannot be changed (a merged, multicam or generated item?). ` +
            "Nothing was changed."
          : `Could not ask Premiere whether "${hit.path}" can be relinked (canChangeMediaPath); nothing was changed.`);
      }
      if (projectLib.jobs.runningFor(await projectLib.clipKey(hit))) {
        throw new BridgeError("TRANSCRIBE_RUNNING", `Speech-to-Text is running on "${hit.path}"; wait for it.`);
      }
      const previous = await tryGet(() => clip.getMediaFilePath(), null);
      const onOld = [];
      for (const e of clips) {
        if (e !== hit && samePath(await tryGet(() => e.clip.getMediaFilePath(), null), previous)) onOld.push(e);
      }
      const listSame = async (after) => {
        const rows = [];
        for (const e of onOld) {
          rows.push(after ? { item: e.path, mediaPathAfter: await tryGet(() => e.clip.getMediaFilePath(), null) }
            : { item: e.path });
        }
        return rows;
      };
      const out = {
        dryRun: !!params.dryRun, relinked: false, item: hit.path, previousPath: previous, newPath: params.newPath,
        current: {
          mediaPath: previous, offline: await tryGet(() => clip.isOffline(), null),
          frameRate: await interpretedFps(clip), mediaSeconds: await mediaSeconds(clip),
        },
        uses: await usesOf(project, await keyOf(hit.item)),
      };
      if (params.dryRun) return Object.assign(out, { sameMedia: await listSame(false) });
      if (samePath(previous, params.newPath)) {
        return Object.assign(out, { alreadyLinked: true, mediaPath: previous, sameMedia: await listSame(false) });
      }

      ctx.sideEffect();
      let returned;
      try {
        returned = await clip.changeMediaFilePath(params.newPath, true);
      } catch (e) {
        throw new BridgeError("INTERNAL", `[changeMediaFilePath] ${core.errorMessage(e)}`);
      }
      const refreshed = core.isFn(clip, "refreshMedia") ? await tryGet(() => clip.refreshMedia(), null) : null;
      const after = await tryGet(() => clip.getMediaFilePath(), null);
      return Object.assign(out, {
        relinked: samePath(after, params.newPath), returned: returned === undefined ? null : returned, refreshed,
        mediaPath: after, offline: await tryGet(() => clip.isOffline(), null), sameMedia: await listSame(true),
      });
    },
  };
};
