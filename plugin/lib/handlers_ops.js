/*
 * handlers_ops: the whitelisted generic layer (uxp-port plan, section 2.1).
 *  - apply_ops: several small writes ("ops") in ONE transaction with one undo label. An op is a key of OPS below and
 *    a variant of apply_ops' params.ops.items in actions.json (its param spec, checked by core.checkParams before any
 *    handler runs). Per op: plan() resolves the target and checks it (async reads, nothing built), build() creates its
 *    Action(s) inside the transaction callback (sync), readBack() reads the result after the commit. Every op is
 *    planned before the first is built, and every Action is built before one is added (core.runTransaction): one
 *    missing target, one refused value or one Action that cannot be built commits nothing. A "direct" op
 *    (sequence.set_player_position: Premiere offers no Action for it) runs after the transaction. Never eval, never a
 *    call by name: an op name is only ever a key of OPS.
 *  - query: whitelisted reads (QUERIES), through the timeline / effects / project readers the other handlers use.
 * Ops are only calls api_smoke passed live on Premiere 27.1 b21 (22 Oct 2026; each variant's `evidence` in
 * actions.json names it). Clip and track ops are video-only until their audio counterparts are tested live.
 * broker/fake_panel.py mirrors both actions (h_apply_ops, h_query); keep the two in step.
 * Factory module returning {actionName: async function (params, ctx)}.
 * deps: { ppro, core, timeline, projectLib, effects, capabilities }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const timeline = deps.timeline;
  const projectLib = deps.projectLib;
  const effects = deps.effects;
  const capabilities = deps.capabilities;
  const BridgeError = core.BridgeError;
  const secs = core.secs;
  const tryGet = core.tryGet;
  const tryGetSync = core.tryGetSync;
  const TPS = core.TICKS_PER_SECOND;

  const MAX_OPS = 200;
  const UNSET_BELOW = -1000;  // an unset sequence in / out point reads as -400000 s (live 27.1)
  const VALUE_TOL = 1e-4;
  const TRACK_RE = /^([VvAa])([1-9][0-9]*)$/;
  const PENDING_AUDIO = "verified live on video only (api_smoke, 27.1 b21); audio is pending a live test";

  const bad = (message) => new BridgeError("BAD_PARAMS", message);
  const round3 = (s) => (typeof s === "number" ? Math.round(s * 1000) / 1000 : s);
  const near = (a, b, tol) => typeof a === "number" && typeof b === "number" && Math.abs(a - b) <= tol;

  function requireName(value, param) {
    if (typeof value !== "string" || value === "") throw bad(`${param} must not be empty`);
    return value;
  }

  /* "V2" -> {kind: "video", index: 1, label: "V2"}; anything else is BAD_PARAMS. */
  function parseTrack(text) {
    const m = TRACK_RE.exec(String(text));
    if (!m) throw bad(`track "${text}" is not a track label (V1, V2, ... or A1, A2, ...)`);
    const kind = m[1].toUpperCase() === "V" ? "video" : "audio";
    const index = Number(m[2]) - 1;
    return { kind, index, label: timeline.trackLabel(kind, index) };
  }

  /* ---------- markers ---------- */

  /* One marker as JSON (list_markers' shape; the getters are synchronous on 27.1). */
  function describeMarker(m) {
    const start = secs(tryGetSync(() => m.getStart()));
    const duration = secs(tryGetSync(() => m.getDuration()));
    const type = tryGetSync(() => m.getType());
    const colorIndex = tryGetSync(() => m.getColorIndex());
    return {
      guid: core.readGuid(m),
      name: tryGetSync(() => m.getName()),
      comments: tryGetSync(() => m.getComments(), ""),
      start, duration,
      end: start !== null && duration !== null ? start + duration : null,
      type: type === null ? null : String(type),
      colorIndex: typeof colorIndex === "number" ? colorIndex : null,
    };
  }

  /* The Markers object and [{m, d}] in start order, read strictly (a failed read is an error, never "no markers"). */
  async function readMarkers(sequence) {
    try {
      const obj = await ppro.Markers.getMarkers(sequence);
      const list = (obj.getMarkers() || []).map((m) => ({ m, d: describeMarker(m) }));
      list.sort((a, b) => (a.d.start || 0) - (b.d.start || 0));
      return { obj, list };
    } catch (e) {
      throw new BridgeError("INTERNAL", `[read] could not read the sequence's markers: ${core.errorMessage(e)}`);
    }
  }

  /* Markers as they were before the request (plan phase) and after the commit (read-back phase): one read each. */
  async function markersOf(env) {
    if (!env.cache.markers) env.cache.markers = await readMarkers(env.sequence);
    return env.cache.markers;
  }
  async function markersAfter(env) {
    if (!env.after.markers) env.after.markers = await readMarkers(env.sequence);
    return env.after.markers.list;
  }

  const briefMarker = (d) => ({ guid: d.guid, name: d.name, start: d.start });
  const markerKey = (d) => `marker:${d.guid || `${d.name}@${d.start}`}`;

  /* The one marker an op means: guid, exact name, start within half a frame (remove_marker's rules). */
  async function markerTarget(env, op) {
    const { list } = await markersOf(env);
    let hits = list;
    if (typeof op.guid === "string" && op.guid !== "") hits = hits.filter((h) => h.d.guid === op.guid);
    if (typeof op.name === "string") hits = hits.filter((h) => h.d.name === op.name);
    if (typeof op.atSeconds === "number") {
      hits = hits.filter((h) => h.d.start !== null && Math.abs(h.d.start - op.atSeconds) <= env.half);
    }
    const what = [op.guid ? `guid ${op.guid}` : null, typeof op.name === "string" ? `named "${op.name}"` : null,
      typeof op.atSeconds === "number" ? `at ${op.atSeconds}s` : null].filter(Boolean).join(", ");
    if (!hits.length) {
      throw new BridgeError("NOT_FOUND", `No marker ${what} (${list.length} marker(s)).`,
        { data: { markers: list.slice(0, 50).map((h) => briefMarker(h.d)) } });
    }
    if (hits.length > 1) {
      throw new BridgeError("AMBIGUOUS", `${hits.length} markers match (${what}): at ` +
        hits.map((h) => `${round3(h.d.start)}s`).join(", ") + ". Pass atSeconds or guid.",
        { data: { matches: hits.map((h) => briefMarker(h.d)) } });
    }
    return hits[0];
  }

  /* The target marker after the commit: by guid; on a build without guids, the same Marker object. */
  async function markerNow(env, t) {
    if (t.d.guid) {
      const hit = (await markersAfter(env)).find((h) => h.d.guid === t.d.guid);
      return hit ? hit.d : null;
    }
    return describeMarker(t.m);
  }

  // Marker setters: op -> the field it writes (describeMarker's name), its param, the Marker method.
  const MARKER_SETTERS = {
    "marker.set_name": { field: "name", param: "newName", method: "createSetNameAction" },
    "marker.set_comment": { field: "comments", param: "comments", method: "createSetCommentsAction" },
    "marker.set_color": { field: "colorIndex", param: "colorIndex", method: "createSetColorByIndexAction" },
    "marker.set_duration": { field: "duration", param: "durationSeconds", method: "createSetDurationAction", time: true },
    "marker.set_type": { field: "type", param: "type", method: "createSetTypeAction" },
  };

  function markerSetter(s) {
    return {
      needs: "sequence",
      async plan(env, op) {
        const t = await markerTarget(env, op);
        const want = op[s.param];
        if (s.field === "name") requireName(want, "newName");
        if (!core.isFn(t.m, s.method)) throw new BridgeError("UNSUPPORTED", `Marker#${s.method} is not available on this Premiere build.`);
        return {
          target: briefMarker(t.d), keys: [[markerKey(t.d), s.field]],
          before: { [s.field]: t.d[s.field] }, after: { [s.field]: want },
          build: () => t.m[s.method](s.time ? ppro.TickTime.createWithSeconds(want) : want),
          async readBack() {
            const d = await markerNow(env, t);
            if (!d) return { readBack: { found: false }, verified: false };
            const got = d[s.field];
            return { readBack: { [s.field]: got }, verified: s.time ? near(got, want, env.half) : got === want };
          },
        };
      },
    };
  }

  /* ---------- clips, params, tracks ---------- */

  /* The clip under op.atSeconds on op.track (videoOnly: refuse an audio track before reading anything). */
  async function clipTarget(env, op, videoOnly, opName) {
    const tr = parseTrack(op.track);
    if (videoOnly && tr.kind !== "video") {
      throw new BridgeError("UNSUPPORTED", `${opName} is ${PENDING_AUDIO}; ${tr.label} is an audio track.`);
    }
    const it = await timeline.itemAt(env.sequence, tr.kind, tr.index, op.atSeconds);
    const d = timeline.requireSpan(await timeline.describeItem(it), tr.label);
    return { tr, it, d, key: `clip:${tr.label}@${d.start}`,
             target: { track: tr.label, name: d.name, start: d.start, end: d.end } };
  }

  /* The clip again after the commit (no op moves a clip, so it is still under the same time). */
  const clipNow = (env, c, op) => timeline.itemAt(env.sequence, c.tr.kind, c.tr.index, op.atSeconds);

  /* param.set's value: a number, a boolean or a point -> {kind, n (effects.normalizeValue form), plain}. */
  function opValue(v, where) {
    if (typeof v === "number" && Number.isFinite(v)) return { kind: "number", n: { plain: v }, plain: v };
    if (typeof v === "boolean") return { kind: "boolean", n: { plain: v }, plain: v };
    const arr = Array.isArray(v) && v.length === 2 && v.every((x) => typeof x === "number" && Number.isFinite(x));
    const obj = core.isPlainObject(v) && Object.keys(v).length === 2 && typeof v.x === "number" && typeof v.y === "number"
      && Number.isFinite(v.x) && Number.isFinite(v.y);
    if (arr || obj) {
      const p = arr ? { x: v[0], y: v[1] } : { x: v.x, y: v.y };
      return { kind: "point", n: { point: [p.x, p.y] }, plain: p };
    }
    throw bad(`${where}: value ${JSON.stringify(v)} is not a number, a boolean or a point ([x, y] or {x, y}); strings ` +
      "and colours are not ops (unverified over UXP)");
  }

  function kindOf(v) {
    if (typeof v === "number") return "number";
    if (typeof v === "boolean") return "boolean";
    if (core.isPlainObject(v) && typeof v.x === "number" && typeof v.y === "number") return "point";
    return null;
  }

  function sameValue(got, want) {
    if (want.kind === "number") return typeof got === "number" && Math.abs(got - want.plain) <= VALUE_TOL * Math.max(1, Math.abs(want.plain));
    if (want.kind === "boolean") return got === want.plain;
    return kindOf(got) === "point" && Math.abs(got.x - want.plain.x) <= VALUE_TOL && Math.abs(got.y - want.plain.y) <= VALUE_TOL;
  }

  /* The param an op names on a clip: {comp, param, names} or NOT_FOUND. */
  async function findParam(c, op, where) {
    const chain = await tryGet(() => c.it.getComponentChain());
    if (!chain) throw new BridgeError("NOT_FOUND", `${c.tr.label} @${op.atSeconds}s has no component chain.`);
    const names = await effects.chainMatchNames(chain);
    const occ = op.occurrence || 1;
    const idxs = effects.indexesOf(names, op.matchName);
    if (occ > idxs.length) {
      throw new BridgeError("NOT_FOUND", `${c.tr.label} @${op.atSeconds}s has ${idxs.length} ${op.matchName} ` +
        `component(s) (chain: ${names.join(", ")}); param.set writes components the clip already has (add effects ` +
        "with set_params or add_audio_effect).", { data: { chain: names } });
    }
    const comp = tryGetSync(() => chain.getComponentAtIndex(idxs[occ - 1]));
    const count = comp ? tryGetSync(() => comp.getParamCount(), 0) || 0 : 0;
    const param = comp && op.index < count ? tryGetSync(() => comp.getParam(op.index)) : null;
    if (!param) throw new BridgeError("NOT_FOUND", `${where}: ${op.matchName}[${occ}] has no param #${op.index} (it has ${count}).`);
    return { comp, param, names };
  }

  async function paramValue(param) {
    const kf = await tryGet(() => param.getStartValue());
    return effects.plainValue(kf ? kf.value : await tryGet(() => param.getValueAtTime(ppro.TickTime.createWithSeconds(0))));
  }

  /* ---------- bins ---------- */

  const trimPath = (p) => String(p || "").split("/").filter((s) => s !== "").join("/");
  const joinPath = (a, b) => (a ? `${a}/${b}` : b);

  function noSlash(name, param) {
    requireName(name, param);
    if (name.indexOf("/") >= 0) throw bad(`${param} "${name}" contains "/", which bin paths use`);
    return name;
  }

  /* The bins directly inside a folder: [{name, folder}]. */
  async function binsIn(folder) {
    const out = [];
    for (const it of (await folder.getItems()) || []) {
      const f = tryGetSync(() => ppro.FolderItem.cast(it));
      if (f) out.push({ name: f.name, folder: f });
    }
    return out;
  }

  /* The bin at a path ("" = the root), or NOT_FOUND / AMBIGUOUS. */
  async function binAt(project, path) {
    let folder = await project.getRootItem();
    let at = "";
    for (const seg of path ? path.split("/") : []) {
      const hits = (await binsIn(folder)).filter((b) => b.name === seg);
      at = joinPath(at, seg);
      if (!hits.length) throw new BridgeError("NOT_FOUND", `No bin "${at}" in the project.`);
      if (hits.length > 1) throw new BridgeError("AMBIGUOUS", `${hits.length} bins are called "${at}"; rename one first.`);
      folder = hits[0].folder;
    }
    return folder;
  }

  const where = (path) => (path ? `bin "${path}"` : "the root bin");

  /* ---------- the op table ---------- */

  const OPS = {
    "marker.add": {
      needs: "sequence",
      async plan(env, op) {
        requireName(op.name, "name");
        const atTicks = core.seqTicks(op.atSeconds, env.frameTicks);
        const start = atTicks / TPS;
        const duration = typeof op.durationSeconds === "number" ? op.durationSeconds : 0;
        const type = op.type || "Comment";
        const comments = op.comments || "";
        const { obj, list } = await markersOf(env);
        if (!core.isFn(obj, "createAddMarkerAction")) throw new BridgeError("UNSUPPORTED", "Markers#createAddMarkerAction is not available on this Premiere build.");
        const guids = new Set(list.map((h) => h.d.guid).filter(Boolean));
        const same = (d) => d.name === op.name && d.start !== null && Math.abs(d.start - start) <= env.half;
        const sameBefore = list.filter((h) => same(h.d)).length;
        return {
          target: { marker: "new", name: op.name, start }, keys: [[`marker:new:${op.name}@${start}`, "add"]],
          after: { name: op.name, start, duration, type, comments },
          build: () => obj.createAddMarkerAction(op.name, type, core.tickTimeFromTicks(atTicks),
            ppro.TickTime.createWithSeconds(duration), comments),
          async readBack() {
            const after = await markersAfter(env);
            const fresh = after.filter((h) => same(h.d) && !(h.d.guid && guids.has(h.d.guid)));
            const d = fresh.length ? fresh[fresh.length - 1].d : null;
            if (!d) return { readBack: { found: false }, verified: false };
            const grew = after.filter((h) => same(h.d)).length > sameBefore;
            return { readBack: d, verified: grew && near(d.duration, duration, env.half) && d.type === type && d.comments === comments };
          },
        };
      },
    },

    "marker.remove": {
      needs: "sequence",
      removes: true,
      async plan(env, op) {
        const t = await markerTarget(env, op);
        const { obj, list } = await markersOf(env);
        if (!core.isFn(obj, "createRemoveMarkerAction")) throw new BridgeError("UNSUPPORTED", "Markers#createRemoveMarkerAction is not available on this Premiere build.");
        const same = (d) => d.name === t.d.name && d.start === t.d.start;
        const sameBefore = list.filter((h) => same(h.d)).length;
        return {
          target: briefMarker(t.d), keys: [[markerKey(t.d), "*"]], before: t.d, after: { removed: true },
          build: () => obj.createRemoveMarkerAction(t.m),
          async readBack() {
            const after = await markersAfter(env);
            const gone = t.d.guid ? !after.some((h) => h.d.guid === t.d.guid) : after.filter((h) => same(h.d)).length < sameBefore;
            return { readBack: { removed: gone, markerCount: after.length }, verified: gone };
          },
        };
      },
    },

    "marker.set_name": markerSetter(MARKER_SETTERS["marker.set_name"]),
    "marker.set_comment": markerSetter(MARKER_SETTERS["marker.set_comment"]),
    "marker.set_color": markerSetter(MARKER_SETTERS["marker.set_color"]),
    "marker.set_duration": markerSetter(MARKER_SETTERS["marker.set_duration"]),
    "marker.set_type": markerSetter(MARKER_SETTERS["marker.set_type"]),

    "marker.move": {
      needs: "sequence",
      async plan(env, op) {
        const t = await markerTarget(env, op);
        const { obj } = await markersOf(env);
        if (!core.isFn(obj, "createMoveMarkerAction")) throw new BridgeError("UNSUPPORTED", "Markers#createMoveMarkerAction is not available on this Premiere build.");
        const toTicks = core.seqTicks(op.toSeconds, env.frameTicks);
        const to = toTicks / TPS;
        return {
          target: briefMarker(t.d), keys: [[markerKey(t.d), "start"]], before: { start: t.d.start }, after: { start: to },
          build: () => obj.createMoveMarkerAction(t.m, core.tickTimeFromTicks(toTicks)),
          async readBack() {
            const d = await markerNow(env, t);
            return d ? { readBack: { start: d.start }, verified: near(d.start, to, env.half) } : { readBack: { found: false }, verified: false };
          },
        };
      },
    },

    "clip.set_name": {
      needs: "sequence",
      async plan(env, op) {
        const c = await clipTarget(env, op, true, "clip.set_name");
        requireName(op.name, "name");
        if (!core.isFn(c.it, "createSetNameAction")) throw new BridgeError("UNSUPPORTED", "TrackItem#createSetNameAction is not available on this Premiere build.");
        return {
          target: c.target, keys: [[c.key, "name"]], before: { name: c.d.name }, after: { name: op.name },
          build: () => c.it.createSetNameAction(op.name),
          async readBack() {
            const got = await tryGet(() => clipNow(env, c, op).then((it) => it.getName()));
            return { readBack: { name: got }, verified: got === op.name };
          },
        };
      },
    },

    "clip.set_disabled": {
      needs: "sequence",
      async plan(env, op) {
        const c = await clipTarget(env, op, true, "clip.set_disabled");
        if (capabilities.flag("trackItemDisable") === false || !core.isFn(c.it, "createSetDisabledAction")) {
          throw new BridgeError("UNSUPPORTED", "clip.set_disabled needs the trackItemDisable capability " +
            "(VideoClipTrackItem#createSetDisabledAction), which this Premiere build lacks.");
        }
        return {
          target: c.target, keys: [[c.key, "disabled"]], before: { disabled: c.d.disabled }, after: { disabled: op.disabled },
          build: () => c.it.createSetDisabledAction(op.disabled),
          async readBack() {
            const got = await tryGet(() => clipNow(env, c, op).then((it) => it.isDisabled()));
            return { readBack: { disabled: got }, verified: got === op.disabled };
          },
        };
      },
    },

    "param.set": {
      needs: "sequence",
      async plan(env, op) {
        const c = await clipTarget(env, op, false, "param.set");
        const occ = op.occurrence || 1;
        const here = `${c.tr.label} @${op.atSeconds}s ${op.matchName}[${occ}]#${op.index}`;
        const want = opValue(op.value, here);
        const { param } = await findParam(c, op, here);
        // A key list that cannot be read counts as keyed: never write over keys unseen. Animation on with no keys (an
        // added audio effect's params, live 27.1) hides nothing: switched off in the same transaction.
        const varying = !!tryGetSync(() => param.isTimeVarying(), false);
        const keyList = varying ? tryGetSync(() => param.getKeyframeListAsTickTimes(), null) : [];
        const keyed = varying && !(Array.isArray(keyList) && keyList.length === 0);
        if (keyed) {
          throw new BridgeError("KEYFRAMED", `${here} is keyframed; a static value would be hidden by its keyframes ` +
            "(remove_keyframes takes them off).");
        }
        const current = await paramValue(param);
        const have = kindOf(current);
        if (have && have !== want.kind) throw bad(`${here}: the param holds a ${have}, not a ${want.kind} (${JSON.stringify(op.value)})`);
        return {
          target: Object.assign({}, c.target, { matchName: op.matchName, occurrence: occ, index: op.index, param: param.displayName }),
          keys: [[`${c.key}:${op.matchName}[${occ}]#${op.index}`, "value"]],
          before: { value: current, animated: varying }, after: { value: want.plain },
          build: () => {
            const out = [];
            if (varying) out.push(param.createSetTimeVaryingAction(false));
            out.push(param.createSetValueAction(param.createKeyframe(effects.toParamValue(want.n)), true));
            return out;
          },
          async readBack() {
            const it = await clipNow(env, c, op);
            const again = await findParam({ tr: c.tr, it }, op, here);
            const got = await paramValue(again.param);
            const animated = !!tryGetSync(() => again.param.isTimeVarying(), false);
            return { readBack: { value: got, animated }, verified: !animated && sameValue(got, want) };
          },
        };
      },
    },

    "sequence.set_in_out": {
      needs: "sequence",
      async plan(env, op) {
        const seq = env.sequence;
        if (!core.isFn(seq, "createSetInPointAction") || !core.isFn(seq, "createSetOutPointAction")) {
          throw new BridgeError("UNSUPPORTED", "Sequence#createSetInPointAction / createSetOutPointAction are not available on this Premiere build.");
        }
        const inT = typeof op.inSeconds === "number" ? core.seqTicks(op.inSeconds, env.frameTicks) : null;
        const outT = typeof op.outSeconds === "number" ? core.seqTicks(op.outSeconds, env.frameTicks) : null;
        if (inT !== null && outT !== null && outT <= inT) throw bad("outSeconds must be after inSeconds (on the frame grid)");
        const point = async (fn) => { const s = secs(await tryGet(fn)); return s === null || s < UNSET_BELOW ? null : s; };
        const before = { inPoint: await point(() => seq.getInPoint()), outPoint: await point(() => seq.getOutPoint()) };
        const after = {};
        const keys = [];
        if (inT !== null) { after.inPoint = inT / TPS; keys.push(["sequence", "inPoint"]); }
        if (outT !== null) { after.outPoint = outT / TPS; keys.push(["sequence", "outPoint"]); }
        return {
          target: { sequence: seq.name }, keys, before, after,
          build: () => {
            const out = [];
            if (inT !== null) out.push(seq.createSetInPointAction(core.tickTimeFromTicks(inT)));
            if (outT !== null) out.push(seq.createSetOutPointAction(core.tickTimeFromTicks(outT)));
            return out;
          },
          async readBack() {
            const got = { inPoint: await point(() => seq.getInPoint()), outPoint: await point(() => seq.getOutPoint()) };
            const ok = Object.keys(after).every((k) => near(got[k], after[k], env.half));
            return { readBack: got, verified: ok };
          },
        };
      },
    },

    "sequence.set_player_position": {
      needs: "sequence",
      direct: true,
      async plan(env, op) {
        const seq = env.sequence;
        if (!core.isFn(seq, "setPlayerPosition")) throw new BridgeError("UNSUPPORTED", "Sequence#setPlayerPosition is not available on this Premiere build.");
        const ticks = core.seqTicks(op.atSeconds, env.frameTicks);
        return {
          target: { sequence: seq.name }, keys: [["sequence", "playerPosition"]],
          before: { playerPositionSeconds: secs(await tryGet(() => seq.getPlayerPosition())) },
          after: { playerPositionSeconds: ticks / TPS },
          async run() {
            if ((await seq.setPlayerPosition(core.tickTimeFromTicks(ticks))) === false) {
              throw new BridgeError("TRANSACTION_FAILED", "Sequence#setPlayerPosition returned false.");
            }
          },
          async readBack() {
            const got = secs(await tryGet(() => seq.getPlayerPosition()));
            return { readBack: { playerPositionSeconds: got }, verified: near(got, ticks / TPS, env.half) };
          },
        };
      },
    },

    "track.rename": {
      needs: "sequence",
      async plan(env, op) {
        const tr = parseTrack(op.track);
        if (tr.kind !== "video") throw new BridgeError("UNSUPPORTED", `track.rename is ${PENDING_AUDIO}; ${tr.label} is an audio track.`);
        requireName(op.name, "name");
        const track = await timeline.getTrack(env.sequence, "video", tr.index);
        if (!core.isFn(track, "createSetNameAction")) throw new BridgeError("UNSUPPORTED", "VideoTrack#createSetNameAction is not available on this Premiere build.");
        return {
          target: { track: tr.label }, keys: [[`track:${tr.label}`, "name"]], before: { name: track.name }, after: { name: op.name },
          build: () => track.createSetNameAction(op.name),
          async readBack() {
            const got = await tryGet(async () => (await timeline.getTrack(env.sequence, "video", tr.index)).name);
            return { readBack: { name: got }, verified: got === op.name };
          },
        };
      },
    },

    "bin.create": {
      needs: "project",
      async plan(env, op) {
        noSlash(op.name, "name");
        const parent = trimPath(op.parent);
        const folder = await binAt(env.project, parent);
        const count = async () => (await binsIn(folder)).filter((b) => b.name === op.name).length;
        if (await count()) {
          throw new BridgeError("PRECONDITION_FAILED", `${where(parent)} already holds a bin named "${op.name}".`);
        }
        if (!core.isFn(folder, "createBinAction")) throw new BridgeError("UNSUPPORTED", "FolderItem#createBinAction is not available on this Premiere build.");
        const path = joinPath(parent, op.name);
        return {
          target: { parent }, keys: [[`bin:${path}`, "create"]], after: { path },
          build: () => folder.createBinAction(op.name, false),
          async readBack() {
            const n = await tryGet(count, 0);
            return { readBack: { path, found: n === 1 }, verified: n === 1 };
          },
        };
      },
    },

    "bin.rename": {
      needs: "project",
      async plan(env, op) {
        const path = trimPath(op.bin);
        if (!path) throw bad("bin: the root bin cannot be renamed");
        noSlash(op.newName, "newName");
        const parent = path.split("/").slice(0, -1).join("/");
        const oldName = path.split("/").slice(-1)[0];
        const bin = await binAt(env.project, path);
        const parentFolder = await binAt(env.project, parent);
        if (op.newName !== oldName && (await binsIn(parentFolder)).some((b) => b.name === op.newName)) {
          throw new BridgeError("PRECONDITION_FAILED", `${where(parent)} already holds a bin named "${op.newName}".`);
        }
        if (!core.isFn(bin, "createRenameBinAction")) throw new BridgeError("UNSUPPORTED", "FolderItem#createRenameBinAction is not available on this Premiere build.");
        const to = joinPath(parent, op.newName);
        return {
          target: { bin: path }, keys: [[`bin:${path}`, "name"], [`bin:${to}`, "create"]],
          before: { path }, after: { path: to },
          build: () => bin.createRenameBinAction(op.newName),
          async readBack() {
            const names = (await tryGet(() => binsIn(parentFolder), [])).map((b) => b.name);
            const ok = names.indexOf(op.newName) >= 0 && (op.newName === oldName || names.indexOf(oldName) < 0);
            return { readBack: { path: to, found: names.indexOf(op.newName) >= 0 }, verified: ok };
          },
        };
      },
    },
  };

  /* Two ops writing one property of one target, or touching a target another op removes ("*"): BAD_PARAMS. */
  function checkConflicts(planned) {
    const seen = [];
    for (const p of planned) {
      for (const [target, prop] of p.keys) {
        const clash = seen.find((s) => s.target === target && (s.prop === prop || s.prop === "*" || prop === "*"));
        if (clash) {
          const how = clash.prop === "*" || prop === "*" ? "touch" : `set ${prop} of`;
          throw bad(`ops[${clash.index}] (${clash.op}) and ops[${p.index}] (${p.op}) both ${how} ${target}; split them ` +
            "into two requests");
        }
      }
      for (const [target, prop] of p.keys) seen.push({ index: p.index, op: p.op, target, prop });
    }
  }

  function defaultLabel(list) {
    let names = Array.from(new Set(list.map((o) => o.op))).join(", ");
    if (names.length > 80) names = names.slice(0, 77) + "...";
    return `Agent: ${list.length} op${list.length === 1 ? "" : "s"} (${names})`;
  }

  /* One op as the reply lists it (before the read-back). */
  function resultOf(p) {
    const r = { index: p.index, op: p.op, ok: true, target: p.target };
    if (p.before !== undefined) r.before = p.before;
    r.after = p.after;
    if (p.direct) r.undoable = false;
    return r;
  }

  /* ---------- queries ---------- */

  const unsetPoint = (t) => { const s = secs(t); return s === null || s < UNSET_BELOW ? null : s; };

  const QUERIES = {
    async clips(p, ctx) {
      const hasAt = typeof p.atSeconds === "number";
      const hasRange = typeof p.fromSeconds === "number";
      if (hasAt && hasRange) throw bad("Give atSeconds or fromSeconds + toSeconds, not both.");
      if (hasRange && p.toSeconds < p.fromSeconds) throw bad("toSeconds must not be before fromSeconds.");
      const wanted = Array.isArray(p.tracks) && p.tracks.length ? p.tracks.map(parseTrack) : null;
      const { sequence } = await core.requireSequence(ctx);
      let list = wanted;
      if (!list) {
        list = [];
        for (const kind of ["video", "audio"]) {
          const n = await timeline.trackCount(sequence, kind);
          for (let i = 0; i < n; i++) list.push({ kind, index: i, label: timeline.trackLabel(kind, i) });
        }
      }
      const tracks = [];
      const done = new Set();
      for (const t of list) {
        if (done.has(t.label)) continue;
        done.add(t.label);
        const track = await timeline.getTrack(sequence, t.kind, t.index);
        const clips = [];
        for (const it of await timeline.clipItems(track, t.label)) {
          const d = await timeline.describeItem(it);
          if (hasAt && !timeline.overlaps(d, p.atSeconds, p.atSeconds)) continue;
          if (hasRange && !timeline.overlaps(d, p.fromSeconds, p.toSeconds)) continue;
          clips.push(d);
        }
        clips.sort((a, b) => (a.start || 0) - (b.start || 0));
        tracks.push({ label: t.label, kind: t.kind, index: t.index, name: track.name, clips });
      }
      return { what: "clips", sequence: sequence.name, tracks };
    },

    async clip(p, ctx) {
      const tr = parseTrack(p.track);
      const { sequence } = await core.requireSequence(ctx);
      const it = await timeline.itemAt(sequence, tr.kind, tr.index, p.atSeconds);
      const d = await timeline.describeItem(it);
      d.components = await effects.describeChain(it, p.maxKeyframes || 50);
      return { what: "clip", sequence: sequence.name, track: tr.label, clip: d };
    },

    async markers(p, ctx) {
      const { sequence } = await core.requireSequence(ctx);
      const markers = (await readMarkers(sequence)).list.map((h) => h.d);
      return { what: "markers", sequence: sequence.name, count: markers.length, markers };
    },

    async bins(p, ctx) {
      const project = await core.requireProject(ctx);
      const root = trimPath(p.bin);
      const tree = await projectLib.walkTree(project);
      if (root && !tree.some((e) => e.kind === "bin" && e.path === root)) {
        throw new BridgeError("NOT_FOUND", `No bin "${root}" in the project. Bins: ` +
          (tree.filter((e) => e.kind === "bin").map((e) => e.path).join(" | ") || "none"));
      }
      const entries = [];
      for (const e of tree) {
        if (root && e.path.indexOf(root + "/") !== 0) continue;
        const row = { path: e.path, kind: e.kind, name: e.name, bin: e.binPath };
        if (e.kind === "clip") row.mediaPath = await tryGet(() => e.clip.getMediaFilePath());
        entries.push(row);
      }
      return { what: "bins", bin: root, count: entries.length, entries };
    },

    async sequence(p, ctx) {
      const { sequence } = await core.requireSequence(ctx);
      const size = await tryGet(() => sequence.getFrameSize());
      const timebase = await tryGet(() => sequence.getTimebase());
      const tb = Number(timebase);
      return {
        what: "sequence", sequence: sequence.name, guid: core.readGuid(sequence),
        frameSize: size ? { width: tryGetSync(() => size.width), height: tryGetSync(() => size.height) } : null,
        timebase: timebase === null ? null : String(timebase),
        fps: tb > 0 ? Math.round((TPS / tb) * 1000) / 1000 : null,
        durationSeconds: secs(await tryGet(() => sequence.getEndTime())),
        playerPositionSeconds: secs(await tryGet(() => sequence.getPlayerPosition())),
        inPoint: unsetPoint(await tryGet(() => sequence.getInPoint())),
        outPoint: unsetPoint(await tryGet(() => sequence.getOutPoint())),
        videoTracks: await timeline.describeTracks(sequence, "video"),
        audioTracks: await timeline.describeTracks(sequence, "audio"),
      };
    },
  };

  return {
    /*
     * Whitelisted ops in one transaction (see the header).   params: { ops: [{op, ...}], undoLabel?, dryRun? }
     * -> { dryRun, count, results: [{index, op, ok, target, before?, after, readBack?, verified?}], verified?,
     *      undoLabel, notUndoable? }. A read-back mismatch is VERIFY_FAILED (committed) with that reply as data.
     */
    async apply_ops(params, ctx) {
      const list = params.ops;
      if (list.length > MAX_OPS) throw bad(`apply_ops takes at most ${MAX_OPS} ops in one request (${list.length} given); split it.`);
      const needsSequence = list.some((op) => OPS[op.op].needs === "sequence");
      const env = { project: null, sequence: null, frameTicks: null, half: core.EPS, cache: {}, after: {} };
      if (needsSequence) {
        Object.assign(env, await core.requireSequence(ctx));
        env.frameTicks = await core.frameTicksOf(env.sequence);
        env.half = env.frameTicks / TPS / 2;
      } else {
        env.project = await core.requireProject(ctx);
      }

      const planned = [];
      for (let i = 0; i < list.length; i++) {
        const op = list[i];
        let p;
        try {
          p = await OPS[op.op].plan(env, op);
        } catch (e) {
          throw core.prefixError(e, `ops[${i}] ${op.op}: `);
        }
        planned.push(Object.assign(p, { index: i, op: op.op, direct: OPS[op.op].direct === true }));
      }
      checkConflicts(planned);

      const label = params.undoLabel || defaultLabel(list);
      const tx = planned.filter((p) => !p.direct);
      const direct = planned.filter((p) => p.direct);
      const out = { dryRun: !!params.dryRun, count: list.length, results: planned.map(resultOf), undoLabel: label };
      if (direct.length) out.notUndoable = direct.map((p) => p.op);
      if (params.dryRun) return out;

      if (tx.length) {
        core.runTransaction(env.project, label, () => {
          const actions = [];
          for (const p of tx) {
            let built;
            try {
              built = p.build();
            } catch (e) {
              throw core.prefixError(e, `ops[${p.index}] ${p.op}: its action could not be built, so nothing was committed: `);
            }
            for (const a of Array.isArray(built) ? built : [built]) actions.push(a);
          }
          return actions;
        }, ctx);
      }
      const completed = tx.map((p) => p.index);
      for (const p of direct) {
        try {
          ctx.sideEffect();
          await p.run();
          completed.push(p.index);
        } catch (e) {
          const opts = { data: { completed, undoLabel: label } };
          if (completed.length) opts.outcome = "partial";
          throw new BridgeError(core.codeOf(e), `ops[${p.index}] ${p.op}: ${core.errorMessage(e)}` +
            (tx.length ? ` The transaction "${label}" had committed ops ${tx.map((t) => t.index).join(", ")}.` : ""), opts);
        }
      }

      for (let k = 0; k < planned.length; k++) {
        let rb;
        try {
          rb = await planned[k].readBack();
        } catch (e) {
          rb = { readBack: { error: core.errorMessage(e) }, verified: false };
        }
        out.results[k].readBack = rb.readBack;
        out.results[k].verified = rb.verified === true;
      }
      out.verified = out.results.every((r) => r.verified);
      if (!out.verified) {
        const off = out.results.filter((r) => !r.verified).map((r) => `ops[${r.index}] ${r.op}`);
        throw new BridgeError("VERIFY_FAILED", `${off.join(", ")} did not read back as asked (results[].readBack). ` +
          `The ops committed as "${label}": look before retrying (Edit > Undo takes the step back).`,
          { outcome: "committed", data: out });
      }
      return out;
    },

    /* Whitelisted reads by name (QUERIES).   params: { what, ...that query's params } */
    async query(params, ctx) {
      return QUERIES[params.what](params, ctx);
    },
  };
};
