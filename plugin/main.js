/*
 * Agent Bridge — UXP panel for Premiere Pro (>= 25.6)
 *
 * Role: dumb executor. Connects OUT to a local broker (UXP plugins cannot
 * listen on sockets), receives whitelisted JSON commands, runs them against
 * the Premiere UXP DOM, returns JSON results.
 *
 * Security posture:
 *  - Command whitelist only. There is deliberately NO eval / runScript action.
 *  - Broker is expected to bind 127.0.0.1 only.
 *
 * API usage patterns verified against AdobeDocs/uxp-premiere-pro-samples.
 */

const ppro = require("premierepro");

const VERSION = "0.3.2";
const BROKER_URL = "ws://127.0.0.1:8017";
const RECONNECT_MS = 3000;
const TICKS_PER_SECOND = 254016000000;
const EPS = 1e-3; // seconds; tolerance for timeline comparisons
const TRACK_ITEM_CLIP =
  (ppro.Constants && ppro.Constants.TrackItemType && ppro.Constants.TrackItemType.CLIP) || 1;

let ws = null;
let reconnectTimer = null;

/* ---------- tiny UI helpers ---------- */

const dot = document.getElementById("dot");
const statusText = document.getElementById("statusText");
const logEl = document.getElementById("log");

function setStatus(state, text) {
  dot.className = state; // "", "connecting", "connected"
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

/* ---------- command handlers (the whitelist) ---------- */

async function requireProject() {
  const project = await ppro.Project.getActiveProject();
  if (!project) throw new Error("No active project open in Premiere.");
  return project;
}

async function requireSequence() {
  const project = await requireProject();
  const sequence = await project.getActiveSequence();
  if (!sequence) throw new Error("Project has no active sequence.");
  return { project, sequence };
}

const handlers = {
  /* Liveness check + version handshake: the CLI compares `version` and `actions`. */
  async ping() {
    const uxp = require("uxp");
    return {
      pong: true,
      host: uxp.host ? `${uxp.host.name} ${uxp.host.version}` : "unknown",
      plugin: `ppro-agent-bridge ${VERSION}`,
      version: VERSION,
      actions: Object.keys(handlers),
    };
  },

  /* Basic project info — the Phase 1 acceptance test. */
  async get_project() {
    const project = await requireProject();
    const out = { name: project.name };
    try { out.path = project.path; } catch (e) { out.path = null; }
    try {
      const seq = await project.getActiveSequence();
      out.activeSequence = seq ? seq.name : null;
    } catch (e) {
      out.activeSequence = null;
    }
    return out;
  },

  /* Active sequence info: player position, frame size, frame rate, duration, tracks. */
  async get_sequence() {
    const { sequence } = await requireSequence();
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

    out.videoTracks = await describeTracks(sequence, "video");
    out.audioTracks = await describeTracks(sequence, "audio");
    out.captionTrackCount = await tryGet(() => sequence.getCaptionTrackCount());
    return out;
  },

  /*
   * Every clip on the active sequence's tracks, as JSON — the structure layer
   * without an OTIO round-trip. Times are sequence seconds; inPoint/outPoint
   * are source seconds.
   * params: { kinds = ["video", "audio"] }
   */
  async list_items(params) {
    const { sequence } = await requireSequence();
    const kinds = (params && Array.isArray(params.kinds) && params.kinds.length) ? params.kinds : ["video", "audio"];
    const out = { sequence: sequence.name };
    for (const kind of ["video", "audio"]) {
      if (!kinds.includes(kind)) continue;
      const tracks = [];
      const count = await trackCount(sequence, kind);
      for (let i = 0; i < count; i++) {
        const track = await getTrack(sequence, kind, i);
        const items = [];
        for (const it of await clipItems(track)) items.push(await describeItem(it));
        items.sort((a, b) => (a.start || 0) - (b.start || 0));
        tracks.push({ index: i, label: (kind === "video" ? "V" : "A") + (i + 1), name: track.name, items });
      }
      out[kind] = tracks;
    }
    return out;
  },

  /*
   * Read-only deep look at clips: the whole effect chain (Motion, Opacity, Time
   * Remapping, added filters) with every param's value, keyframed flag and
   * keyframes, plus the transitions on each track — i.e. what an editor changed.
   * params: { videoTracks?: [idx] (default all), fromSeconds?, toSeconds?, maxKeyframes = 50 }
   */
  async inspect_items(params) {
    const { sequence } = await requireSequence();
    const p = params || {};
    const count = await trackCount(sequence, "video");
    const idxs = Array.isArray(p.videoTracks) && p.videoTracks.length ? p.videoTracks : [...Array(count).keys()];
    const from = typeof p.fromSeconds === "number" ? p.fromSeconds : -Infinity;
    const to = typeof p.toSeconds === "number" ? p.toSeconds : Infinity;
    const maxKf = p.maxKeyframes || 50;
    const out = { sequence: sequence.name, tracks: [] };
    for (const idx of idxs) {
      const track = await getTrack(sequence, "video", idx);
      const clips = [];
      for (const it of await clipItems(track)) {
        const d = await describeItem(it);
        if (!(d.start <= to + EPS && d.end >= from - EPS)) continue;
        d.components = await describeChain(it, maxKf);
        clips.push(d);
      }
      const transitions = [];
      const TRANSITION = (ppro.Constants && ppro.Constants.TrackItemType && ppro.Constants.TrackItemType.TRANSITION) || 2;
      for (const tr of (await tryGet(() => track.getTrackItems(TRANSITION, false), [])) || []) {
        const t = {
          name: await tryGet(() => tr.getName()),
          matchName: await tryGet(() => tr.getMatchName()),
          start: secs(await tryGet(() => tr.getStartTime())),
          end: secs(await tryGet(() => tr.getEndTime())),
          duration: secs(await tryGet(() => tr.getDuration())),
        };
        if (t.start === null || (t.start <= to + EPS && t.end >= from - EPS)) transitions.push(t);
      }
      out.tracks.push({ index: idx, label: "V" + (idx + 1), name: track.name, clips, transitions });
    }
    return out;
  },

  /*
   * Every clip in the project panel with its bin path and media file path.
   * Lets callers match files on disk to project item names before placing.
   */
  async list_project_items() {
    const out = [];
    for (const e of await walkProject()) {
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
   * params: { dir (required), name = "frame.png",
   *           atSeconds = player position,
   *           width/height = the sequence frame size (so vertical sequences stay vertical) }
   *
   * Known Adobe bug (confirmed in official sample, comment reads
   * "We do double extension"): output may land as <name>.png even when
   * <name> already ends in .png. We return both candidate paths and let
   * the CLI resolve which one exists.
   */
  async export_frame(params) {
    if (!params || !params.dir) throw new Error("export_frame requires params.dir");
    const { sequence } = await requireSequence();

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
      tick = ppro.TickTime.createWithSeconds(params.atSeconds);
    } else {
      tick = await sequence.getPlayerPosition();
    }

    const ok = await ppro.Exporter.exportSequenceFrame(
      sequence, tick, name, params.dir, width, height
    );
    if (!ok) throw new Error("exportSequenceFrame returned false");

    return {
      exported: true,
      dir: params.dir,
      candidates: [name, name + ".png"], // double-extension bug tolerance
      width, height,
      atSeconds: typeof params.atSeconds === "number" ? params.atSeconds : "playerPosition",
    };
  },

  /*
   * Export the active sequence as OpenTimelineIO — the structure layer.
   * params: { outputPath (required, full file path ending .otio) }
   */
  async export_otio(params) {
    if (!params || !params.outputPath) throw new Error("export_otio requires params.outputPath");
    const { sequence } = await requireSequence();
    const ok = await ppro.ProjectConverter.exportAsOpenTimelineIO(
      sequence, params.outputPath
    );
    if (!ok) throw new Error("exportAsOpenTimelineIO returned false");
    return { exported: true, outputPath: params.outputPath };
  },

  /*
   * List every clip in the project and whether it carries a transcript.
   * Walks the project panel tree (folders included), same traversal as
   * Adobe's official sample.
   */
  async list_transcripts() {
    const entries = await findClips();
    const out = [];
    for (const e of entries) {
      let has = false;
      try { has = ppro.Transcript.hasTranscript(e.clip); } catch (err) { /* leave false */ }
      out.push({ name: e.clip.name, hasTranscript: has });
    }
    return { clips: out };
  },

  /*
   * Export a clip's transcript as Adobe Transcript JSON — the semantic layer.
   * params: { itemName? }  If omitted, uses the first clip that has one.
   * Returns the raw JSON string; the CLI writes it to disk.
   */
  async export_transcript(params) {
    const entries = await findClips();
    let target = null;

    if (params && params.itemName) {
      const hit = entries.find((e) => e.clip.name === params.itemName) || null;
      if (!hit) {
        throw new Error(
          `No clip named "${params.itemName}". Clips: ` +
          entries.map((e) => e.clip.name).join(" | ")
        );
      }
      if (!ppro.Transcript.hasTranscript(hit.clip)) {
        throw new Error(`Clip "${params.itemName}" has no transcript. Run Speech to Text in the Text panel first.`);
      }
      target = hit.clip;
    } else {
      for (const e of entries) {
        try {
          if (ppro.Transcript.hasTranscript(e.clip)) { target = e.clip; break; }
        } catch (err) { /* skip */ }
      }
      if (!target) throw new Error("No clip in this project has a transcript. Run Speech to Text in the Text panel first.");
    }

    const transcriptJson = await ppro.Transcript.exportToJSON(target);
    if (!transcriptJson) throw new Error("exportToJSON returned nothing.");
    return { itemName: target.name, transcriptJson: String(transcriptJson) };
  },

  /*
   * Import an Adobe-format transcript JSON onto a clip (e.g. produced by
   * local Whisper via transcribe.py). params: { itemName, transcriptJson }
   */
  async import_transcript(params) {
    if (!params || !params.itemName || !params.transcriptJson) {
      throw new Error("import_transcript requires params.itemName and params.transcriptJson");
    }
    const project = await requireProject();
    const entries = await findClips();
    const hit = entries.find((e) => e.clip.name === params.itemName);
    if (!hit) {
      throw new Error(`No clip named "${params.itemName}". Clips: ` + entries.map((e) => e.clip.name).join(" | "));
    }
    let success = false;
    project.lockedAccess(() => {
      success = project.executeTransaction((compoundAction) => {
        const action = ppro.Transcript.createImportTextSegmentsAction(
          ppro.Transcript.importFromJSON(params.transcriptJson),
          hit.clip
        );
        compoundAction.addAction(action);
      }, `Import transcript onto ${params.itemName}`);
    });
    if (!success) throw new Error("Import transaction returned false.");
    return { imported: true, itemName: params.itemName };
  },

  /* ============ Phase 3: write operations ============ */

  /*
   * Import media files into the project root, or into a named top-level bin.
   * params: { paths: [absolute file paths], bin?: "bin name" (created if missing) }
   */
  async import_media(params) {
    if (!params || !Array.isArray(params.paths) || params.paths.length === 0) {
      throw new Error("import_media requires params.paths (non-empty array).");
    }
    const project = await requireProject();
    const names = async () => (await findClips()).map((e) => e.clip.name);
    const before = await names();

    // importFiles(paths, suppressUI, targetBin: ProjectItem, asNumberedStills).
    // Premiere 27.x type-checks targetBin strictly — passing `undefined` throws
    // "Illegal Parameter type" — so resolve a real bin. Root first; some builds
    // only accept the ProjectItem from getInsertionBin(), so fall back to it.
    let ok;
    if (params.bin) {
      const bin = await resolveBin(project, params.bin);
      ok = await project.importFiles(params.paths, true, bin, false);
    } else try {
      ok = await project.importFiles(params.paths, true, await project.getRootItem(), false);
    } catch (eRoot) {
      try {
        ok = await project.importFiles(params.paths, true, await project.getInsertionBin(), false);
      } catch (eIns) {
        throw new Error(`importFiles failed (root bin: ${eRoot && eRoot.message ? eRoot.message : eRoot}; ` +
          `insertion bin: ${eIns && eIns.message ? eIns.message : eIns})`);
      }
    }
    if (!ok) throw new Error("importFiles returned false.");

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
   * params: {
   *   itemName (required)      — project item name, exact match
   *   atSeconds = 0            — sequence time to place at
   *   videoTrack = 0, audioTrack = 0   (0-based: 0 = V1/A1)
   *   mode = "overwrite" | "insert"
   *   inSeconds?, outSeconds?  — source range; omit both to place whole clip
   *   undoLabel?               — name shown in Premiere's undo history
   * }
   * With in/out given, runs three transactions: set source in/out →
   * place → clear in/out. Each is one undo step, labelled.
   */
  async insert_clip(params) {
    if (!params || !params.itemName) throw new Error("insert_clip requires params.itemName");
    const project = await requireProject();
    const sequence = await project.getActiveSequence();
    if (!sequence) throw new Error("No active sequence.");

    const entries = await findClips();
    const hit = entries.find((e) => e.clip.name === params.itemName);
    if (!hit) {
      throw new Error(`No clip named "${params.itemName}". Clips: ` + entries.map((e) => e.clip.name).join(" | "));
    }

    const atSeconds = params.atSeconds || 0;
    const vTrack = params.videoTrack || 0;
    const aTrack = params.audioTrack || 0;
    const mode = params.mode === "insert" ? "insert" : "overwrite";
    const label = params.undoLabel || `Agent: place ${params.itemName} @${atSeconds}s`;
    const hasRange = typeof params.inSeconds === "number" && typeof params.outSeconds === "number";

    if (hasRange && params.outSeconds <= params.inSeconds) {
      throw new Error("outSeconds must be greater than inSeconds.");
    }

    let success = false;

    const step = (name, fn) => {
      try { return fn(); }
      catch (e) { throw new Error(`[${name}] ${e && e.message ? e.message : e}`); }
    };

    if (hasRange) {
      let rangeOk = false;
      step("set source in/out", () => {
        project.lockedAccess(() => {
          rangeOk = project.executeTransaction((compoundAction) => {
            compoundAction.addAction(hit.clip.createSetInOutPointsAction(
              ppro.TickTime.createWithSeconds(params.inSeconds),
              ppro.TickTime.createWithSeconds(params.outSeconds)
            ));
          }, label + " [1/3 set range]");
        });
      });
      if (!rangeOk) throw new Error("[set source in/out] transaction returned false.");
    }

    const sequenceEditor = ppro.SequenceEditor.getEditor(sequence);
    step("place on timeline", () => {
      project.lockedAccess(() => {
        success = project.executeTransaction((compoundAction) => {
          const at = ppro.TickTime.createWithSeconds(atSeconds);
          const action = (mode === "insert")
            ? sequenceEditor.createInsertProjectItemAction(hit.item, at, vTrack, aTrack, true)
            : sequenceEditor.createOverwriteItemAction(hit.item, at, vTrack, aTrack);
          compoundAction.addAction(action);
        }, label + (hasRange ? " [2/3 place]" : ""));
      });
    });

    if (hasRange) {
      step("clear source in/out", () => {
        project.lockedAccess(() => {
          project.executeTransaction((compoundAction) => {
            compoundAction.addAction(hit.clip.createClearInOutPointsAction());
          }, label + " [3/3 clear range]");
        });
      });
    }

    if (!success) throw new Error("Placing the clip failed (transaction returned false).");
    return {
      placed: true, itemName: params.itemName, mode,
      atSeconds, videoTrack: vTrack, audioTrack: aTrack,
      sourceRange: hasRange ? [params.inSeconds, params.outSeconds] : "whole clip",
      undoLabel: label,
    };
  },

  /* ============ v0.3: finishing tools ============ */

  /*
   * Set Motion > Scale (percent) on the clips of one video track that overlap
   * [fromSeconds, toSeconds] (sequence seconds; from == to targets the clip
   * under that instant). One undo step.
   * params: { videoTrack, fromSeconds, toSeconds?, value, dryRun?, undoLabel? }
   */
  async set_scale(params) {
    if (!params || typeof params.videoTrack !== "number" || typeof params.fromSeconds !== "number") {
      throw new Error("set_scale requires params.videoTrack and params.fromSeconds");
    }
    const dryRun = !!params.dryRun;
    if (!dryRun && typeof params.value !== "number") throw new Error("set_scale requires params.value (percent)");
    const { project, sequence } = await requireSequence();
    const to = typeof params.toSeconds === "number" ? params.toSeconds : params.fromSeconds;
    const track = await getTrack(sequence, "video", params.videoTrack);

    const targets = [];
    for (const it of await clipItems(track)) {
      const d = await describeItem(it);
      if (!overlaps(d, params.fromSeconds, to)) continue;
      const scale = await findMotionScale(it);
      d.scaleParam = scale ? scale.param.displayName : null;
      d.scaleBefore = scale ? await tryGet(() => scale.param.getValueAtTime(ppro.TickTime.createWithSeconds(d.inPoint || 0))) : null;
      d.scaleKeyframed = scale ? !!tryGetSync(() => scale.param.isTimeVarying()) : null;
      targets.push({ desc: d, scale });
    }
    if (!targets.length) throw new Error(`No clip on V${params.videoTrack + 1} overlaps ${params.fromSeconds}–${to}s.`);
    const missing = targets.filter((t) => !t.scale).map((t) => t.desc.name);
    if (missing.length) throw new Error(`Could not find Motion > Scale on: ${missing.join(", ")}`);
    const keyed = targets.filter((t) => t.desc.scaleKeyframed).map((t) => t.desc.name);
    if (keyed.length) throw new Error(`Scale is keyframed on: ${keyed.join(", ")} — not changing it.`);

    if (!dryRun) {
      const label = params.undoLabel || `Agent: scale ${params.value}% on V${params.videoTrack + 1}`;
      runTransaction(project, label, (ca) => {
        for (const t of targets) {
          ca.addAction(t.scale.param.createSetValueAction(t.scale.param.createKeyframe(params.value), true));
        }
      });
    }
    return { dryRun, value: params.value, clips: targets.map((t) => t.desc) };
  },

  /*
   * Write effect params on the clip under atSeconds (video track). Components are
   * matched by match name; missing ones (added filters) are created first.
   * A param with `keyframes` becomes time-varying with those keyframes.
   * params: { videoTrack, atSeconds, undoLabel?,
   *           components: [{ matchName, params: [{ index, value, keyframes?: [{ t, value }] }] }] }
   */
  async set_params(params) {
    if (!params || typeof params.videoTrack !== "number" || typeof params.atSeconds !== "number"
        || !Array.isArray(params.components)) {
      throw new Error("set_params requires params.videoTrack, params.atSeconds and params.components");
    }
    const { project, sequence } = await requireSequence();
    const it = await clipAt(sequence, params.videoTrack, params.atSeconds);
    const label = params.undoLabel || `Agent: set params on V${params.videoTrack + 1} @${params.atSeconds}s`;

    let chain = await it.getComponentChain();
    const matchNames = async () => {
      const out = [];
      for (let i = 0; i < chain.getComponentCount(); i++) out.push(await chain.getComponentAtIndex(i).getMatchName());
      return out;
    };
    const present = await matchNames();
    const toCreate = params.components.map((c) => c.matchName).filter((m) => !present.includes(m));
    if (toCreate.length) {
      const created = [];
      for (const m of toCreate) created.push(await ppro.VideoFilterFactory.createComponent(m));
      runTransaction(project, label + " [add effects]", (ca) => {
        for (const comp of created) ca.addAction(chain.createAppendComponentAction(comp));
      });
      chain = await it.getComponentChain();
    }

    const names = await matchNames();
    const jobs = [];
    for (const c of params.components) {
      const ci = names.indexOf(c.matchName);
      if (ci < 0) throw new Error(`Component ${c.matchName} not on the clip after creation.`);
      const comp = chain.getComponentAtIndex(ci);
      for (const p of c.params || []) {
        const param = comp.getParam(p.index);
        if (!param) throw new Error(`${c.matchName} has no param #${p.index}`);
        jobs.push({ param, p, where: `${c.matchName}#${p.index} ${param.displayName}` });
      }
    }
    runTransaction(project, label, (ca) => {
      for (const { param, p } of jobs) {
        if (Array.isArray(p.keyframes) && p.keyframes.length) {
          ca.addAction(param.createSetTimeVaryingAction(true));
          for (const k of p.keyframes) {
            const kf = param.createKeyframe(toParamValue(k.value));
            kf.position = ppro.TickTime.createWithSeconds(k.t);
            ca.addAction(param.createAddKeyframeAction(kf));
          }
        } else {
          ca.addAction(param.createSetValueAction(param.createKeyframe(toParamValue(p.value)), true));
        }
      }
    });
    return { set: jobs.map((j) => j.where), createdEffects: toCreate, undoLabel: label };
  },

  /* Video transition match names available in this Premiere (e.g. "AE.Impact_Film_Dissolve"). */
  async list_transition_names() {
    return { names: await ppro.TransitionFactory.getVideoTransitionMatchNames() };
  },

  /*
   * Add a video transition to the start or end of the clip under atSeconds.
   * params: { videoTrack, atSeconds, matchName, durationSeconds, applyToStart,
   *           forceSingleSided = true, alignment?, undoLabel? }
   */
  async add_transition(params) {
    if (!params || typeof params.videoTrack !== "number" || typeof params.atSeconds !== "number"
        || !params.matchName || typeof params.durationSeconds !== "number") {
      throw new Error("add_transition requires videoTrack, atSeconds, matchName, durationSeconds");
    }
    const { project, sequence } = await requireSequence();
    const it = await clipAt(sequence, params.videoTrack, params.atSeconds);
    const transition = ppro.TransitionFactory.createVideoTransition(params.matchName);
    if (!transition) throw new Error(`Unknown transition ${params.matchName}`);
    let opts = new ppro.AddTransitionOptions();
    opts = opts.setApplyToStart(!!params.applyToStart) || opts;
    opts = opts.setDuration(ppro.TickTime.createWithSeconds(params.durationSeconds)) || opts;
    opts = opts.setForceSingleSided(params.forceSingleSided !== false) || opts;
    if (typeof params.alignment === "number") opts = opts.setTransitionAlignment(params.alignment) || opts;
    const label = params.undoLabel ||
      `Agent: ${params.matchName} at ${params.applyToStart ? "start" : "end"} of V${params.videoTrack + 1} @${params.atSeconds}s`;
    runTransaction(project, label, (ca) => ca.addAction(it.createAddVideoTransitionAction(transition, opts)));
    return { added: true, matchName: params.matchName, applyToStart: !!params.applyToStart,
             durationSeconds: params.durationSeconds, undoLabel: label };
  },

  /*
   * Interpret footage: override the frame rate of project items (e.g. 60 → 59.94).
   * params: { itemNames: [...], fps, undoLabel? }
   */
  async set_fps(params) {
    if (!params || !Array.isArray(params.itemNames) || !params.itemNames.length || typeof params.fps !== "number") {
      throw new Error("set_fps requires params.itemNames (array) and params.fps (number)");
    }
    const project = await requireProject();
    const entries = await findClips();
    const hits = [];
    const notFound = [];
    for (const n of params.itemNames) {
      const matches = entries.filter((e) => e.clip.name === n);
      if (!matches.length) notFound.push(n);
      hits.push(...matches);
    }
    if (notFound.length) throw new Error(`No clip named: ${notFound.join(", ")}`);
    const label = params.undoLabel || `Agent: interpret ${hits.length} clip(s) at ${params.fps} fps`;
    runTransaction(project, label, (ca) => {
      for (const h of hits) ca.addAction(h.clip.createSetOverrideFrameRateAction(params.fps));
    });
    return { fps: params.fps, items: hits.map((h) => h.clip.name), undoLabel: label };
  },

  /*
   * Add a sequence marker (e.g. a fact-check flag the editor sees in the timeline).
   * params: { atSeconds, name, comments?, durationSeconds = 0,
   *           type = "Comment" | "Chapter" | "Segmentation" | "WebLink", colorIndex? }
   */
  async add_marker(params) {
    if (!params || typeof params.atSeconds !== "number" || !params.name) {
      throw new Error("add_marker requires params.atSeconds and params.name");
    }
    const { project, sequence } = await requireSequence();
    const type = params.type || "Comment";
    const dur = typeof params.durationSeconds === "number" ? params.durationSeconds : 0;
    const markers = await ppro.Markers.getMarkers(sequence);
    const label = params.undoLabel || `Agent: marker "${params.name}" @${params.atSeconds}s`;
    runTransaction(project, label, (ca) => {
      ca.addAction(markers.createAddMarkerAction(
        params.name, type,
        ppro.TickTime.createWithSeconds(params.atSeconds),
        ppro.TickTime.createWithSeconds(dur),
        params.comments || ""
      ));
    });

    let colorSet = false;
    if (typeof params.colorIndex === "number") {
      const fresh = await ppro.Markers.getMarkers(sequence);
      const mine = (fresh.getMarkers() || []).filter((m) =>
        m.getName() === params.name && Math.abs(secs(m.getStart()) - params.atSeconds) < EPS);
      const m = mine[mine.length - 1];
      if (m) {
        runTransaction(project, label + " [color]", (ca) => ca.addAction(m.createSetColorByIndexAction(params.colorIndex)));
        colorSet = true;
      }
    }
    return { added: true, name: params.name, atSeconds: params.atSeconds, durationSeconds: dur, type, colorSet, undoLabel: label };
  },

  /*
   * Remove clips from the given tracks within [fromSeconds, toSeconds].
   * mode "inside" (default) only takes clips entirely inside the range;
   * "overlap" takes any clip touching it. One undo step.
   * params: { videoTracks: [], audioTracks: [], fromSeconds, toSeconds,
   *           mode?, ripple = false, dryRun?, undoLabel? }
   */
  async remove_items(params) {
    if (!params || typeof params.fromSeconds !== "number" || typeof params.toSeconds !== "number") {
      throw new Error("remove_items requires params.fromSeconds and params.toSeconds");
    }
    const vts = params.videoTracks || [];
    const ats = params.audioTracks || [];
    if (!vts.length && !ats.length) throw new Error("remove_items requires videoTracks and/or audioTracks");
    const { project, sequence } = await requireSequence();
    const mode = params.mode === "overlap" ? "overlap" : "inside";

    const targets = [];
    for (const [kind, list] of [["video", vts], ["audio", ats]]) {
      for (const idx of list) {
        const track = await getTrack(sequence, kind, idx);
        for (const it of await clipItems(track)) {
          const d = await describeItem(it);
          const hit = mode === "inside"
            ? d.start >= params.fromSeconds - EPS && d.end <= params.toSeconds + EPS
            : overlaps(d, params.fromSeconds, params.toSeconds);
          if (hit) targets.push({ it, desc: Object.assign({ track: (kind === "video" ? "V" : "A") + (idx + 1) }, d) });
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
        runTransaction(project, label, (ca) => {
          ca.addAction(editor.createRemoveItemsAction(sel, !!params.ripple, mediaAny, false));
        });
      } catch (e) { err = e; }
    });
    if (err) throw err;
    return { dryRun: false, mode, removed, undoLabel: label };
  },
};

/* ---------- helpers ---------- */

const secs = (t) => (t && typeof t.seconds === "number" ? t.seconds : null);

async function tryGet(fn, fallback = null) {
  try { const v = await fn(); return v === undefined ? fallback : v; } catch (e) { return fallback; }
}

function tryGetSync(fn, fallback = null) {
  try { const v = fn(); return v === undefined ? fallback : v; } catch (e) { return fallback; }
}

/* lockedAccess + executeTransaction; throws if Premiere reports failure. */
function runTransaction(project, label, build) {
  let ok = false;
  project.lockedAccess(() => {
    ok = project.executeTransaction((compoundAction) => build(compoundAction), label);
  });
  if (!ok) throw new Error(`Transaction "${label}" returned false.`);
}

async function trackCount(sequence, kind) {
  return (await tryGet(() => (kind === "video" ? sequence.getVideoTrackCount() : sequence.getAudioTrackCount()), 0)) || 0;
}

async function getTrack(sequence, kind, index) {
  const count = await trackCount(sequence, kind);
  if (index < 0 || index >= count) {
    throw new Error(`${kind === "video" ? "V" : "A"}${index + 1} does not exist (sequence has ${count} ${kind} tracks).`);
  }
  return kind === "video" ? sequence.getVideoTrack(index) : sequence.getAudioTrack(index);
}

async function clipItems(track) {
  return (await tryGet(() => track.getTrackItems(TRACK_ITEM_CLIP, false), [])) || [];
}

async function describeTracks(sequence, kind) {
  const out = [];
  const count = await trackCount(sequence, kind);
  for (let i = 0; i < count; i++) {
    const t = await getTrack(sequence, kind, i);
    out.push({ index: i, label: (kind === "video" ? "V" : "A") + (i + 1), name: t.name, clipCount: (await clipItems(t)).length });
  }
  return out;
}

async function describeItem(it) {
  const pi = await tryGet(() => it.getProjectItem());
  const clip = pi ? ppro.ClipProjectItem.cast(pi) : null;
  return {
    name: await tryGet(() => it.getName()),
    start: secs(await tryGet(() => it.getStartTime())),
    end: secs(await tryGet(() => it.getEndTime())),
    inPoint: secs(await tryGet(() => it.getInPoint())),
    outPoint: secs(await tryGet(() => it.getOutPoint())),
    speed: await tryGet(() => it.getSpeed()),
    disabled: await tryGet(() => it.isDisabled()),
    projectItem: clip ? clip.name : null,
    mediaPath: clip ? await tryGet(() => clip.getMediaFilePath()) : null,
  };
}

/* The single clip on a video track under `at` (sequence seconds). */
async function clipAt(sequence, videoTrack, at) {
  const track = await getTrack(sequence, "video", videoTrack);
  const hits = [];
  for (const it of await clipItems(track)) {
    const d = await describeItem(it);
    if (overlaps(d, at, at)) hits.push(it);
  }
  if (hits.length !== 1) throw new Error(`Expected one clip on V${videoTrack + 1} at ${at}s, found ${hits.length}.`);
  return hits[0];
}

/* Inverse of plainValue: [x, y] / {x, y} → PointF; everything else passes through. */
function toParamValue(v) {
  if (Array.isArray(v) && v.length === 2 && v.every((n) => typeof n === "number")) return new ppro.PointF(v[0], v[1]);
  if (v && typeof v === "object" && typeof v.x === "number" && typeof v.y === "number") return new ppro.PointF(v.x, v.y);
  return v;
}

/* JSON-safe copy of a param value (numbers, strings, bools, PointF, Color). */
function plainValue(v) {
  if (v === null || v === undefined) return null;
  if (typeof v !== "object") return v;
  if (typeof v.x === "number" && typeof v.y === "number") return { x: v.x, y: v.y };
  if (typeof v.red === "number") return { red: v.red, green: v.green, blue: v.blue, alpha: v.alpha };
  if (v.value !== undefined) return plainValue(v.value);
  try { return JSON.parse(JSON.stringify(v)); } catch (e) { return String(v); }
}

/* The clip's effect chain: components → params → value / keyframes. */
async function describeChain(trackItem, maxKf) {
  const chain = await tryGet(() => trackItem.getComponentChain());
  if (!chain) return null;
  const comps = [];
  const n = tryGetSync(() => chain.getComponentCount(), 0);
  for (let i = 0; i < n; i++) {
    const comp = chain.getComponentAtIndex(i);
    const c = {
      index: i,
      matchName: await tryGet(() => comp.getMatchName()),
      displayName: await tryGet(() => comp.getDisplayName()),
      params: [],
    };
    const pc = tryGetSync(() => comp.getParamCount(), 0);
    for (let k = 0; k < pc; k++) {
      const param = comp.getParam(k);
      if (!param) continue;
      const keyed = !!tryGetSync(() => param.isTimeVarying(), false);
      const startKf = await tryGet(() => param.getStartValue());
      const pr = {
        index: k,
        name: param.displayName,
        value: plainValue(startKf ? startKf.value : await tryGet(() => param.getValueAtTime(ppro.TickTime.createWithSeconds(0)))),
        keyframed: keyed,
      };
      if (keyed) {
        const times = tryGetSync(() => param.getKeyframeListAsTickTimes(), []) || [];
        pr.keyframes = [];
        for (const t of times.slice(0, maxKf)) {
          pr.keyframes.push({ t: secs(t), value: plainValue(await tryGet(() => param.getValueAtTime(t))) });
        }
        if (times.length > maxKf) pr.keyframesTruncated = times.length;
      }
      c.params.push(pr);
    }
    comps.push(c);
  }
  return comps;
}

/* [start, end) overlaps [from, to]; from == to means "the clip under that instant". */
function overlaps(d, from, to) {
  return d.start !== null && d.end !== null && d.start <= to + EPS && d.end > from + EPS;
}

/* Motion's Scale param. Display names are localized, so match the component by
 * match name and the param by known names, falling back to index 1 (Position, Scale, …). */
const SCALE_NAMES = ["Scale", "缩放", "縮放", "スケール", "Skalierung", "Échelle", "Escala", "Scala", "Масштаб", "비율 조정"];
async function findMotionScale(trackItem) {
  const chain = await tryGet(() => trackItem.getComponentChain());
  if (!chain) return null;
  const n = tryGetSync(() => chain.getComponentCount(), 0);
  for (let i = 0; i < n; i++) {
    const comp = chain.getComponentAtIndex(i);
    if ((await tryGet(() => comp.getMatchName())) !== "AE.ADBE Motion") continue;
    const pc = tryGetSync(() => comp.getParamCount(), 0);
    for (let p = 0; p < pc; p++) {
      const param = comp.getParam(p);
      if (param && SCALE_NAMES.includes(param.displayName)) return { comp, param };
    }
    const fallback = pc > 1 ? comp.getParam(1) : null;
    return fallback ? { comp, param: fallback } : null;
  }
  return null;
}

/* Find a top-level bin by name, creating it if missing. */
async function resolveBin(project, binName) {
  const root = await project.getRootItem();
  const find = async () => {
    for (const it of await root.getItems()) {
      const f = ppro.FolderItem.cast(it);
      if (f && f.name === binName) return f;
    }
    return null;
  };
  let bin = await find();
  if (!bin) {
    runTransaction(project, `Agent: create bin "${binName}"`, (ca) => ca.addAction(root.createBinAction(binName, false)));
    bin = await find();
    if (!bin) throw new Error(`Created bin "${binName}" but could not find it afterwards.`);
  }
  return bin;
}

/* Walk the project panel tree. Returns {item, clip, binPath} entries: `item` is
 * the raw ProjectItem (what sequence editor actions expect), `clip` is the
 * ClipProjectItem cast (what transcript / in-out actions expect), `binPath` is
 * "" for the root or "Bin/Sub bin". */
async function walkProject() {
  const project = await requireProject();
  const root = await project.getRootItem();
  const queue = (await root.getItems()).map((item) => ({ item, binPath: "" }));
  const found = [];
  for (let i = 0; i < queue.length; i++) {
    const { item, binPath } = queue[i];
    const clip = ppro.ClipProjectItem.cast(item);
    if (clip) {
      found.push({ item, clip, binPath });
    } else {
      const folder = ppro.FolderItem.cast(item);
      if (folder) {
        const path = binPath ? `${binPath}/${folder.name}` : folder.name;
        for (const sub of await folder.getItems()) queue.push({ item: sub, binPath: path });
      }
    }
  }
  return found;
}

async function findClips() {
  return walkProject();
}

/* ---------- message plumbing ---------- */

async function handleMessage(raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch (e) {
    log(`Bad JSON from broker: ${raw}`, "err");
    return;
  }
  if (!msg.id || !msg.action) return;

  log(`<- ${msg.action}`, "cmd");
  const reply = { id: msg.id };

  const handler = handlers[msg.action];
  if (!handler) {
    reply.ok = false;
    reply.error = `Unknown action "${msg.action}". Whitelist: ${Object.keys(handlers).join(", ")}`;
  } else {
    try {
      reply.ok = true;
      reply.data = await handler(msg.params || {});
    } catch (e) {
      reply.ok = false;
      reply.error = String(e && e.message ? e.message : e);
    }
  }

  log(reply.ok ? `-> ${msg.action} ok` : `-> ${msg.action} FAILED: ${reply.error}`, reply.ok ? "ok" : "err");
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(reply));
  }
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  setStatus("connecting", `Connecting to ${BROKER_URL} ...`);

  try {
    ws = new WebSocket(BROKER_URL);
  } catch (e) {
    setStatus("", "WebSocket unavailable — check manifest network permission");
    log(String(e), "err");
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    setStatus("connected", "Connected to broker");
    log("Connected. Registering as panel.", "ok");
    ws.send(JSON.stringify({ role: "panel" }));
  };

  ws.onmessage = (evt) => handleMessage(evt.data);

  ws.onclose = () => {
    setStatus("", "Disconnected — broker not running?");
    scheduleReconnect();
  };

  ws.onerror = () => {
    /* onclose follows; nothing extra to do */
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_MS);
}

document.getElementById("reconnectBtn").addEventListener("click", () => {
  if (ws) { try { ws.close(); } catch (e) { /* noop */ } }
  ws = null;
  connect();
});

log(`Agent Bridge ${VERSION} loaded. Whitelist: ` + Object.keys(handlers).join(", "));
connect();
