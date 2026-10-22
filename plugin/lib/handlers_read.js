/*
 * handlers_read: read-only actions (project / sequence info, timeline items, effect chains,
 * project items, frame and OTIO export, transition names).
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
  const EPS = core.EPS;
  const TICKS_PER_SECOND = core.TICKS_PER_SECOND;

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
     * Every clip on the active sequence's tracks, as JSON. Times are sequence seconds;
     * inPoint/outPoint are source seconds.   params: { kinds = ["video", "audio"] }
     */
    async list_items(params, ctx) {
      const { sequence } = await core.requireSequence(ctx);
      const kinds = Array.isArray(params.kinds) && params.kinds.length ? params.kinds : ["video", "audio"];
      const out = { sequence: sequence.name };
      for (const kind of ["video", "audio"]) {
        if (!kinds.includes(kind)) continue;
        const tracks = [];
        const count = await timeline.trackCount(sequence, kind);
        for (let i = 0; i < count; i++) {
          const track = await timeline.getTrack(sequence, kind, i);
          const items = [];
          for (const it of await timeline.clipItems(track)) items.push(await timeline.describeItem(it));
          items.sort((a, b) => (a.start || 0) - (b.start || 0));
          tracks.push({ index: i, label: timeline.trackLabel(kind, i), name: track.name, items });
        }
        out[kind] = tracks;
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
        const clips = [];
        for (const it of await timeline.clipItems(track)) {
          const d = await timeline.describeItem(it);
          if (!(d.start <= to + EPS && d.end >= from - EPS)) continue;
          d.components = await effects.describeChain(it, maxKf);
          clips.push(d);
        }
        const transitions = [];
        for (const tr of kind === "video" ? await timeline.transitionItems(track) : []) {
          const t = {
            name: await tryGet(() => tr.getName()),
            matchName: await tryGet(() => tr.getMatchName()),
            start: secs(await tryGet(() => tr.getStartTime())),
            end: secs(await tryGet(() => tr.getEndTime())),
            duration: secs(await tryGet(() => tr.getDuration())),
          };
          if (t.start === null || (t.start <= to + EPS && t.end >= from - EPS)) transitions.push(t);
        }
        out.tracks.push({ index: idx, kind, label: timeline.trackLabel(kind, idx), name: track.name, clips, transitions });
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
     * sequence renders to a 48 kHz WAV in ~1.5 s with "Waveform Audio 48kHz 16-bit.epr".
     * params: { outputPath, presetPath, workAreaOnly? = false }
     */
    async export_sequence(params, ctx) {
      // presetPath is required: deriving it from uxp.host.applicationPath failed on 27.1 ("Unable to
      // initialize export!"); the CLI finds Premiere's own WAV preset next to the running executable.
      core.requireText(params, ["outputPath", "presetPath"]);
      const { sequence } = await core.requireSequence(ctx);
      const preset = params.presetPath;
      const em = ppro.EncoderManager.getManager();
      const types = ppro.Constants && ppro.Constants.ExportType;
      const type = types && types.IMMEDIATELY !== undefined ? types.IMMEDIATELY : ppro.EncoderManager.EXPORT_IMMEDIATELY;
      let ok;
      try {
        ok = await em.exportSequence(sequence, type, params.outputPath, preset, !params.workAreaOnly);
      } catch (e) {
        throw new BridgeError("EXPORT_FAILED", `exportSequence failed (${core.errorMessage(e)}; preset ${preset})`);
      }
      if (!ok) throw new BridgeError("EXPORT_FAILED", `exportSequence returned false (preset ${preset})`);
      return { exported: true, outputPath: params.outputPath, preset };
    },

    /* Video transition match names available in this Premiere (e.g. "AE.Impact_Film_Dissolve"). */
    async list_transition_names(params, ctx) {
      await core.checkGuard(ctx);
      return { names: await ppro.TransitionFactory.getVideoTransitionMatchNames() };
    },
  };
};
