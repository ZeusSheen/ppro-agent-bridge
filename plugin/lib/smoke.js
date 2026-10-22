/*
 * smoke: api_smoke, fixed test cases for UXP calls that Adobe documents but the bridge has never run on a
 * real Premiere (removing markers / transitions / effects / keyframes, cloning and activating sequences,
 * audio effects by display name, bins, relinking, custom properties, MOGRT insertion, ...). The answers
 * decide which ported tools are real (see the uxp-port plan). Not an editing command.
 *
 * Safety: runs only when the active project's name starts with PAB_TEST, whatever the guard says. A case
 * works on the scratch clip the runner placed (videoTrack / audioTrack at atSeconds), on project items the
 * runner names, or on objects it creates itself; it reads every change back and puts back what it changed
 * (cleanup steps). Whatever it could not put back is listed in `leftovers`. Opt-ins: allowTrackAdd.
 *
 * A step is one API under test: pass (it did what the docs say, read back), fail (it ran, the read-back
 * disagrees), absent (no such method on this build), error (it threw), skipped (an input or opt-in is
 * missing). A case never throws for a step: only the PAB_TEST gate and missing inputs refuse the request.
 * Factory module.   deps: { ppro, core, timeline, projectLib, effects }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const timeline = deps.timeline;
  const projectLib = deps.projectLib;
  const effects = deps.effects;
  const BridgeError = core.BridgeError;
  const tryGet = core.tryGet;
  const tryGetSync = core.tryGetSync;
  const secs = core.secs;

  const PREFIX = "PAB_TEST";
  const LABEL = "PAB_API";
  const OPACITY = "AE.ADBE Opacity";
  const BLUR = "AE.ADBE Gaussian Blur 2";
  const LUMETRI = "AE.ADBE Lumetri";
  const DISSOLVE = "AE.ADBE Cross Dissolve New";

  class Absent extends Error {}

  /* obj[name] must be a method on this build; otherwise the step is "absent". */
  function need(obj, name, what) {
    if (!core.isFn(obj, name)) throw new Absent(`${what || "object"}.${name} is not a function on this build`);
    return obj;
  }

  const constant = (group, key, fallback) => {
    const g = ppro.Constants && ppro.Constants[group];
    return g && g[key] !== undefined ? g[key] : fallback;
  };
  const tt = (s) => ppro.TickTime.createWithSeconds(s);
  const unwrap = (v) => effects.plainValue(v);
  const near = (a, b, tol) => typeof a === "number" && typeof b === "number" && Math.abs(a - b) <= (tol || 1e-3);
  const samePath = (a, b) => String(a || "").replace(/\\/g, "/").toLowerCase() === String(b || "").replace(/\\/g, "/").toLowerCase();

  /* ---------- the per-request recorder ---------- */

  function recorder() {
    const steps = [];
    const cleanups = [];
    const leftovers = [];
    async function step(api, fn) {
      const t0 = Date.now();
      const entry = { api, status: "pass" };
      try {
        const r = await fn();
        if (r && r.skip) { entry.status = "skipped"; entry.detail = r.skip; }
        else if (r === false || (r && r.pass === false)) entry.status = "fail";
        if (r && r.detail !== undefined) entry.detail = r.detail;
      } catch (e) {
        entry.status = e instanceof Absent ? "absent" : "error";
        entry.detail = core.errorMessage(e);
      }
      entry.ms = Date.now() - t0;
      steps.push(entry);
      return entry.status === "pass";
    }
    /* Undo work for the end of the case; `what` goes to leftovers when it fails. */
    function undo(what, fn) { cleanups.push({ what, fn }); }
    async function runCleanups() {
      while (cleanups.length) {
        const c = cleanups.pop();
        const ok = await step(`cleanup: ${c.what}`, c.fn);
        if (!ok) leftovers.push(c.what);
      }
    }
    return { steps, leftovers, step, undo, runCleanups };
  }

  /* ---------- shared lookups ---------- */

  function tx(env, label, build) {
    return core.runTransaction(env.project, `${LABEL} ${env.name}: ${label}`, build, env.ctx);
  }

  function requireInputs(params, names, caseName) {
    const missing = names.filter((n) => params[n] === undefined || params[n] === null || params[n] === "");
    if (missing.length) throw new BridgeError("BAD_PARAMS", `Case ${caseName} needs ${missing.join(", ")}.`);
  }

  async function scratchClip(env, kind) {
    const idx = kind === "video" ? env.params.videoTrack : env.params.audioTrack;
    return timeline.itemAt(env.sequence, kind, idx, env.params.atSeconds);
  }

  async function componentsOf(chain) {
    const out = [];
    const n = tryGetSync(() => chain.getComponentCount(), 0) || 0;
    for (let i = 0; i < n; i++) {
      const c = tryGetSync(() => chain.getComponentAtIndex(i));
      out.push({ index: i, component: c, matchName: c ? await tryGet(() => c.getMatchName()) : null });
    }
    return out;
  }

  async function paramList(comp, max) {
    const out = [];
    const n = tryGetSync(() => comp.getParamCount(), 0) || 0;
    for (let i = 0; i < n && i < (max || 80); i++) {
      const p = tryGetSync(() => comp.getParam(i));
      if (!p) { out.push({ index: i, error: "no param" }); continue; }
      const start = await tryGet(() => p.getStartValue());
      let raw = start && start.value !== undefined ? start.value : start;
      while (raw && typeof raw === "object" && Object.keys(raw).length === 1 && raw.value !== undefined) raw = raw.value;
      let type = raw === null || raw === undefined ? "none" : typeof raw;
      if (raw && typeof raw === "object") type = ppro.MogrtText && raw instanceof ppro.MogrtText ? "MogrtText" : "object";
      out.push({ index: i, displayName: tryGetSync(() => p.displayName), type, value: type === "object" || type === "MogrtText" ? null : unwrap(raw),
                 keyframed: !!tryGetSync(() => p.isTimeVarying(), false) });
    }
    return out;
  }

  const findComponent = async (chain, matchName) => (await componentsOf(chain)).filter((c) => c.matchName === matchName);

  function markerList(markers) { return (tryGetSync(() => markers.getMarkers(), []) || []); }

  function findMarker(markers, guid, name) {
    for (const m of markerList(markers)) {
      if (guid && tryGetSync(() => String(m.guid)) === guid) return m;
      if (!guid && tryGetSync(() => m.getName()) === name) return m;
    }
    return null;
  }

  async function sequencesOf(project) {
    const list = (await project.getSequences()) || [];
    return list.map((s) => ({ seq: s, name: tryGetSync(() => s.name), guid: core.readGuid(s) }));
  }

  async function entryFor(env) {
    requireInputs(env.params, ["itemName"], env.name);
    return projectLib.resolveClip(env.project, env.params.itemName, env.params.binPath);
  }

  async function findInProject(project, name) {
    return (await projectLib.walkProject(project)).filter((e) => e.clip.name === name);
  }

  async function findBin(folder, name) {
    for (const it of (await tryGet(() => folder.getItems(), [])) || []) {
      const f = tryGetSync(() => ppro.FolderItem.cast(it));
      if (f && f.name === name) return f;
    }
    return null;
  }

  function removeTrackItems(env, items, label) {
    const editor = ppro.SequenceEditor.getEditor(env.sequence);
    const mediaAny = constant("MediaType", "ANY", undefined);
    let err = null;
    ppro.TrackItemSelection.createEmptySelection((sel) => {
      try {
        for (const it of items) sel.addItem(it, true);
        tx(env, label, () => editor.createRemoveItemsAction(sel, false, mediaAny, false));
      } catch (e) { err = e; }
    });
    if (err) throw err;
    return true;
  }

  /* ---------- the cases ---------- */

  const CASES = {
    /* Sequence markers: add, every setter, move, remove. */
    async markers(env) {
      const { rec, sequence } = env;
      const at = env.params.atSeconds || 0;
      const name = `${LABEL} marker ${env.stamp}`;
      let markers = null;
      await rec.step("Markers.getMarkers(sequence)", async () => {
        markers = await need(ppro.Markers, "getMarkers", "Markers").getMarkers(sequence);
        return { pass: !!markers, detail: { count: markers ? markerList(markers).length : null } };
      });
      if (!markers) return;
      let guid = null;
      let marker = null;
      const refind = async () => { markers = await ppro.Markers.getMarkers(sequence); marker = findMarker(markers, guid, name); return marker; };
      await rec.step("Markers#createAddMarkerAction", async () => {
        need(markers, "createAddMarkerAction", "Markers");
        tx(env, "add marker", () => markers.createAddMarkerAction(name, "Comment", tt(at), tt(0), "pab api"));
        await refind();
        if (marker) guid = tryGetSync(() => String(marker.guid)) || null;
        return { pass: !!marker, detail: { guid } };
      });
      if (!marker) return;
      let removed = false;
      rec.undo(`marker "${name}"`, async () => {
        if (removed) return true;
        await refind();
        if (!marker) return true;
        tx(env, "remove marker (cleanup)", () => need(markers, "createRemoveMarkerAction", "Markers").createRemoveMarkerAction(marker));
        return !(await refind());
      });
      await rec.step("Marker getters", async () => {
        const read = {};
        for (const g of ["getName", "getComments", "getStart", "getDuration", "getType", "getColor", "getColorIndex", "getUrl", "getTarget"]) {
          try { const v = core.isFn(marker, g) ? marker[g]() : undefined; read[g] = v === undefined ? "absent" : (secs(v) !== null ? secs(v) : unwrap(v)); }
          catch (e) { read[g] = `error: ${core.errorMessage(e)}`; }
        }
        read.guid = guid;
        return { detail: read };
      });
      const setters = [
        ["createSetNameAction", `${name} renamed`, (m) => m.getName(), (a, b) => a === b],
        ["createSetCommentsAction", "pab api comments", (m) => m.getComments(), (a, b) => a === b],
        ["createSetDurationAction", 0.5, (m) => secs(m.getDuration()), (a, b) => near(a, b, 0.02)],
        ["createSetTypeAction", "Chapter", (m) => m.getType(), (a, b) => String(a) === b],
        ["createSetColorByIndexAction", 3, (m) => m.getColorIndex(), (a, b) => a === b],
      ];
      for (const [method, value, read, same] of setters) {
        await rec.step(`Marker#${method}`, async () => {
          need(marker, method, "Marker");
          const arg = method === "createSetDurationAction" ? tt(value) : value;
          tx(env, method, () => marker[method](arg));
          if (!(await refind())) return { pass: false, detail: "marker gone after the write" };
          const got = tryGetSync(() => read(marker));
          return { pass: same(got, value), detail: { want: value, got } };
        });
      }
      await rec.step("Markers#createMoveMarkerAction", async () => {
        need(markers, "createMoveMarkerAction", "Markers");
        tx(env, "move marker", () => markers.createMoveMarkerAction(marker, tt(at + 1)));
        await refind();
        const got = marker ? secs(tryGetSync(() => marker.getStart())) : null;
        return { pass: near(got, at + 1, 0.02), detail: { want: at + 1, got } };
      });
      await rec.step("Markers#createRemoveMarkerAction", async () => {
        need(markers, "createRemoveMarkerAction", "Markers");
        await refind();
        tx(env, "remove marker", () => markers.createRemoveMarkerAction(marker));
        removed = !(await refind());
        return { pass: removed, detail: { remaining: markerList(markers).length } };
      });
    },

    /* Video transitions at the end and the start of the scratch clip: add (verified), then remove. */
    async transitions(env) {
      requireInputs(env.params, ["videoTrack", "atSeconds"], env.name);
      const { rec } = env;
      const clip = await scratchClip(env, "video");
      const track = await timeline.getTrack(env.sequence, "video", env.params.videoTrack);
      const frameTicks = await core.frameTicksOf(env.sequence);
      const names = (await tryGet(() => ppro.TransitionFactory.getVideoTransitionMatchNames(), [])) || [];
      const matchName = names.indexOf(DISSOLVE) >= 0 ? DISSOLVE : names[0];
      const count = async () => (await timeline.transitionItems(track)).length;
      for (const edge of ["END", "START"]) {
        const before = await count();
        let added = false;
        await rec.step(`VideoClipTrackItem#createAddVideoTransitionAction (${edge.toLowerCase()}, baseline)`, async () => {
          const transition = ppro.TransitionFactory.createVideoTransition(matchName);
          let opts = new ppro.AddTransitionOptions();
          opts = opts.setApplyToStart(edge === "START") || opts;
          opts = opts.setDuration(core.tickTimeFromTicks(Math.round(15.1 * frameTicks))) || opts;
          opts = opts.setForceSingleSided(true) || opts;
          tx(env, `add ${edge.toLowerCase()} transition`, () => clip.createAddVideoTransitionAction(transition, opts));
          added = (await count()) > before;
          return { pass: added, detail: { matchName, before, after: await count() } };
        });
        if (!added) continue;
        let removed = false;
        rec.undo(`${edge.toLowerCase()} transition on V${env.params.videoTrack + 1}`, async () => removed || (await count()) <= before);
        await rec.step(`VideoClipTrackItem#createRemoveVideoTransitionAction(${edge})`, async () => {
          need(clip, "createRemoveVideoTransitionAction", "VideoClipTrackItem");
          const pos = constant("TransitionPosition", edge, tryGetSync(() => ppro.VideoTransition[`TRANSITIONPOSITION_${edge}`]));
          tx(env, `remove ${edge.toLowerCase()} transition`, () => clip.createRemoveVideoTransitionAction(pos));
          removed = (await count()) <= before;
          return { pass: removed, detail: { position: pos === undefined ? "undefined" : pos, after: await count() } };
        });
      }
    },

    /* Effect lists, insert at an index, remove (Gaussian Blur on the scratch clip). */
    async components(env) {
      requireInputs(env.params, ["videoTrack", "atSeconds"], env.name);
      const { rec } = env;
      for (const fn of ["getMatchNames", "getDisplayNames"]) {
        await rec.step(`VideoFilterFactory.${fn}`, async () => {
          const list = (await need(ppro.VideoFilterFactory, fn, "VideoFilterFactory")[fn]()) || [];
          return { pass: list.length > 0, detail: { count: list.length, names: list.slice(0, 400) } };
        });
      }
      const clip = await scratchClip(env, "video");
      const chainOf = async () => clip.getComponentChain();
      const n0 = (await componentsOf(await chainOf())).length;
      let inserted = false;
      await rec.step("VideoComponentChain#createInsertComponentAction(blur, 2)", async () => {
        const chain = await chainOf();
        need(chain, "createInsertComponentAction", "VideoComponentChain");
        const comp = await ppro.VideoFilterFactory.createComponent(BLUR);
        tx(env, "insert blur at 2", () => chain.createInsertComponentAction(comp, 2));
        const after = await componentsOf(await chainOf());
        inserted = after.length === n0 + 1;
        return { pass: inserted && after[2] && after[2].matchName === BLUR, detail: after.map((c) => c.matchName) };
      });
      if (!inserted) return;
      let removed = false;
      rec.undo("blur on the scratch clip", async () => removed || (await componentsOf(await chainOf())).length === n0);
      await rec.step("VideoComponentChain#createRemoveComponentAction", async () => {
        const chain = await chainOf();
        need(chain, "createRemoveComponentAction", "VideoComponentChain");
        const blur = (await findComponent(chain, BLUR)).pop();
        tx(env, "remove blur", () => chain.createRemoveComponentAction(blur.component));
        const after = await componentsOf(await chainOf());
        removed = after.length === n0;
        return { pass: removed, detail: after.map((c) => c.matchName) };
      });
    },

    /* Opacity keyframes: interpolation, remove one, remove a range, switch animation off. */
    async keyframes(env) {
      requireInputs(env.params, ["videoTrack", "atSeconds"], env.name);
      const { rec } = env;
      const clip = await scratchClip(env, "video");
      const opacity = (await findComponent(await clip.getComponentChain(), OPACITY))[0];
      if (!opacity) return rec.step("Opacity component", async () => ({ pass: false, detail: "no Opacity on the scratch clip" }));
      const param = opacity.component.getParam(0);
      if (tryGetSync(() => param.isTimeVarying(), false)) {
        return rec.step("Opacity keyframes", async () => ({ skip: "Opacity is already keyframed on the scratch clip" }));
      }
      const inPoint = secs(await clip.getInPoint());
      const times = [inPoint + 0.2, inPoint + 0.6, inPoint + 1.0];
      const keys = () => (tryGetSync(() => param.getKeyframeListAsTickTimes(), []) || []).map(secs);
      const add = async () => {
        tx(env, "add 3 keyframes", () => {
          const actions = [param.createSetTimeVaryingAction(true)];
          times.forEach((t, i) => {
            const kf = param.createKeyframe(i * 50);
            kf.position = tt(t);
            actions.push(param.createAddKeyframeAction(kf));
          });
          return actions;
        });
        return keys();
      };
      rec.undo("Opacity keyframes on the scratch clip", async () => {
        if (tryGetSync(() => param.isTimeVarying(), false)) tx(env, "animation off (cleanup)", () => param.createSetTimeVaryingAction(false));
        tx(env, "opacity 100 (cleanup)", () => param.createSetValueAction(param.createKeyframe(100), true));
        return !tryGetSync(() => param.isTimeVarying(), false);
      });
      let ok = false;
      await rec.step("ComponentParam#createAddKeyframeAction (baseline)", async () => {
        const got = await add();
        ok = got.length === 3;
        return { pass: ok, detail: { keys: got } };
      });
      if (!ok) return;
      await rec.step("ComponentParam#createSetInterpolationAtKeyframeAction(HOLD)", async () => {
        need(param, "createSetInterpolationAtKeyframeAction", "ComponentParam");
        const hold = constant("InterpolationMode", "HOLD", undefined);
        tx(env, "hold at key 2", () => param.createSetInterpolationAtKeyframeAction(tt(times[1]), hold, false));
        // a held key keeps its value up to the next key (linear would be half way to 100 here)
        const mid = unwrap(await tryGet(() => param.getValueAtTime(tt(times[1] + 0.2))));
        return { pass: near(mid, 50, 0.5), detail: { hold: hold === undefined ? "undefined" : hold, valueAfterKey2: mid, linearWouldBe: 75 } };
      });
      await rec.step("ComponentParam#createRemoveKeyframeAction", async () => {
        need(param, "createRemoveKeyframeAction", "ComponentParam");
        tx(env, "remove key 2", () => param.createRemoveKeyframeAction(tt(times[1]), false));
        const got = keys();
        return { pass: got.length === 2, detail: { keys: got } };
      });
      await rec.step("ComponentParam#createRemoveKeyframeRangeAction", async () => {
        need(param, "createRemoveKeyframeRangeAction", "ComponentParam");
        tx(env, "remove key 1 by range", () => param.createRemoveKeyframeRangeAction(tt(times[0] - 0.05), tt(times[0] + 0.05), false));
        const got = keys();
        return { pass: got.length === 1, detail: { keys: got } };
      });
      await rec.step("ComponentParam#createSetTimeVaryingAction(false)", async () => {
        tx(env, "animation off", () => param.createSetTimeVaryingAction(false));
        const got = keys();
        const varying = tryGetSync(() => param.isTimeVarying(), null);
        return { pass: varying === false && got.length === 0, detail: { timeVarying: varying, keys: got } };
      });
    },

    /* Audio effects by (localised) display name: list; with displayName also add to the scratch audio clip, read, remove. */
    async audio_filter(env) {
      const { rec } = env;
      await rec.step("AudioFilterFactory.getDisplayNames", async () => {
        const list = (await need(ppro.AudioFilterFactory, "getDisplayNames", "AudioFilterFactory").getDisplayNames()) || [];
        return { pass: list.length > 0, detail: { count: list.length, names: list.slice(0, 400) } };
      });
      if (!env.params.displayName) {
        return rec.step("AudioFilterFactory.createComponentByDisplayName", async () => ({ skip: "pass displayName (one of the names above)" }));
      }
      requireInputs(env.params, ["audioTrack", "atSeconds"], env.name);
      const clip = await scratchClip(env, "audio");
      const chainOf = async () => clip.getComponentChain();
      const n0 = (await componentsOf(await chainOf())).length;
      let added = null;
      await rec.step(`AudioFilterFactory.createComponentByDisplayName + append ("${env.params.displayName}")`, async () => {
        const comp = await need(ppro.AudioFilterFactory, "createComponentByDisplayName", "AudioFilterFactory")
          .createComponentByDisplayName(env.params.displayName, clip);
        if (!comp) return { pass: false, detail: "no component for that name" };
        const chain = await chainOf();
        need(chain, "createAppendComponentAction", "AudioComponentChain");
        tx(env, "append audio effect", () => chain.createAppendComponentAction(comp));
        const after = await componentsOf(await chainOf());
        if (after.length !== n0 + 1) return { pass: false, detail: after.map((c) => c.matchName) };
        added = after[after.length - 1];
        return { detail: { matchName: added.matchName, displayName: await tryGet(() => added.component.getDisplayName()),
                           params: await paramList(added.component) } };
      });
      if (!added) return;
      let removed = false;
      rec.undo(`audio effect "${env.params.displayName}" on the scratch clip`, async () => removed || (await componentsOf(await chainOf())).length === n0);
      await rec.step("AudioComponentChain#createRemoveComponentAction", async () => {
        const chain = await chainOf();
        need(chain, "createRemoveComponentAction", "AudioComponentChain");
        const last = (await componentsOf(chain)).pop();
        tx(env, "remove audio effect", () => chain.createRemoveComponentAction(last.component));
        removed = (await componentsOf(await chainOf())).length === n0;
        return { pass: removed };
      });
    },

    /* Lumetri Color: add, list every param (index, localised name, type, value), set one slider, remove. */
    async lumetri(env) {
      requireInputs(env.params, ["videoTrack", "atSeconds"], env.name);
      const { rec } = env;
      const clip = await scratchClip(env, "video");
      const chainOf = async () => clip.getComponentChain();
      const n0 = (await componentsOf(await chainOf())).length;
      let lum = null;
      await rec.step("VideoFilterFactory.createComponent(AE.ADBE Lumetri) + append", async () => {
        const comp = await ppro.VideoFilterFactory.createComponent(LUMETRI);
        if (!comp) return { pass: false, detail: "createComponent returned nothing" };
        const chain = await chainOf();
        tx(env, "append Lumetri", () => chain.createAppendComponentAction(comp));
        lum = (await findComponent(await chainOf(), LUMETRI)).pop() || null;
        return { pass: !!lum, detail: lum ? { params: await paramList(lum.component, 200) } : null };
      });
      if (!lum) return;
      let removed = false;
      rec.undo("Lumetri on the scratch clip", async () => {
        if (removed) return true;
        const chain = await chainOf();
        const l = (await findComponent(chain, LUMETRI)).pop();
        if (l) tx(env, "remove Lumetri (cleanup)", () => need(chain, "createRemoveComponentAction", "VideoComponentChain").createRemoveComponentAction(l.component));
        return (await componentsOf(await chainOf())).length === n0;
      });
      await rec.step("Lumetri slider write", async () => {
        const list = await paramList(lum.component, 200);
        const pick = typeof env.params.paramIndex === "number"
          ? list.find((p) => p.index === env.params.paramIndex)
          : list.find((p) => p.type === "number" && /exposure|曝光/i.test(String(p.displayName)));
        if (!pick) return { skip: "no numeric Exposure param found; pass paramIndex" };
        const value = typeof env.params.value === "number" ? env.params.value : 1;
        const param = lum.component.getParam(pick.index);
        tx(env, `Lumetri #${pick.index} = ${value}`, () => param.createSetValueAction(param.createKeyframe(value), true));
        const got = unwrap(await tryGet(() => param.getStartValue()));
        return { pass: near(got, value, 1e-3), detail: { index: pick.index, displayName: pick.displayName, before: pick.value, want: value, got } };
      });
      await rec.step("VideoComponentChain#createRemoveComponentAction(Lumetri)", async () => {
        const chain = await chainOf();
        need(chain, "createRemoveComponentAction", "VideoComponentChain");
        const l = (await findComponent(chain, LUMETRI)).pop();
        tx(env, "remove Lumetri", () => chain.createRemoveComponentAction(l.component));
        removed = (await componentsOf(await chainOf())).length === n0;
        return { pass: removed };
      });
    },

    /* Opacity > Blend Mode as a popup index. */
    async blend_mode(env) {
      requireInputs(env.params, ["videoTrack", "atSeconds"], env.name);
      const { rec } = env;
      const clip = await scratchClip(env, "video");
      const opacity = (await findComponent(await clip.getComponentChain(), OPACITY))[0];
      if (!opacity) return rec.step("Opacity component", async () => ({ pass: false, detail: "no Opacity on the scratch clip" }));
      const param = opacity.component.getParam(1);
      const before = unwrap(await tryGet(() => param.getStartValue()));
      rec.undo("blend mode of the scratch clip", async () => {
        tx(env, "blend mode back (cleanup)", () => param.createSetValueAction(param.createKeyframe(before), true));
        return unwrap(await tryGet(() => param.getStartValue())) === before;
      });
      await rec.step("ComponentParam#createSetValueAction(Blend Mode = 2)", async () => {
        tx(env, "blend mode 2", () => param.createSetValueAction(param.createKeyframe(2), true));
        const got = unwrap(await tryGet(() => param.getStartValue()));
        return { pass: got === 2, detail: { displayName: tryGetSync(() => param.displayName), before, got } };
      });
    },

    /* Sequences: list, clone, rename the clone, its settings (frame size), activate it and back, delete it. */
    async sequences(env) {
      const { rec, project, sequence } = env;
      const origGuid = core.readGuid(sequence);
      let before = [];
      const listed = await rec.step("Project#getSequences", async () => {
        before = await sequencesOf(need(project, "getSequences", "Project"));
        return { pass: before.length > 0, detail: before.map((s) => ({ name: s.name, guid: s.guid })) };
      });
      if (!listed) return;  // a clone could not be found again, so none is made
      let clone = null;
      await rec.step("Sequence#createCloneAction", async () => {
        need(sequence, "createCloneAction", "Sequence");
        tx(env, "clone sequence", () => sequence.createCloneAction());
        const after = await sequencesOf(project);
        const fresh = after.filter((s) => !before.some((b) => b.guid === s.guid));
        clone = fresh.length === 1 ? fresh[0] : null;
        return { pass: !!clone, detail: { new: fresh.map((s) => ({ name: s.name, guid: s.guid })) } };
      });
      if (!clone) return;
      let deleted = false;
      rec.undo(`cloned sequence "${clone.name}"`, async () => {
        if (deleted) return true;
        await need(project, "deleteSequence", "Project").deleteSequence(clone.seq);
        return !(await sequencesOf(project)).some((s) => s.guid === clone.guid);
      });
      const newName = `${LABEL} clone ${env.stamp}`;
      await rec.step("Sequence#getProjectItem + ProjectItem#createSetNameAction", async () => {
        const item = await need(clone.seq, "getProjectItem", "Sequence").getProjectItem();
        need(item, "createSetNameAction", "ProjectItem");
        tx(env, "rename clone", () => item.createSetNameAction(newName));
        const got = (await sequencesOf(project)).find((s) => s.guid === clone.guid);
        return { pass: !!got && got.name === newName, detail: { got: got ? got.name : null } };
      });
      await rec.step("Sequence#getSettings + createSetSettingsAction (frame 1080x1350 on a populated clone)", async () => {
        const s = await need(clone.seq, "getSettings", "Sequence").getSettings();
        need(clone.seq, "createSetSettingsAction", "Sequence");
        const read = {};
        for (const g of ["getVideoFrameRect", "getVideoFrameRate", "getVideoPixelAspectRatio", "getAudioSampleRate", "getVideoFieldType"]) {
          read[g] = core.isFn(s, g) ? unwrap(tryGetSync(() => s[g]())) : "absent";
        }
        if (!core.isFn(s, "getVideoFrameRect") || !core.isFn(s, "setVideoFrameRect")) return { pass: false, detail: { read, why: "no frame rect get/set" } };
        const rect = s.getVideoFrameRect();
        rect.width = 1080;
        rect.height = 1350;
        s.setVideoFrameRect(rect);
        tx(env, "clone frame 1080x1350", () => clone.seq.createSetSettingsAction(s));
        const size = await tryGet(() => clone.seq.getFrameSize());
        return { pass: !!size && size.width === 1080 && size.height === 1350, detail: { read, after: size ? { width: size.width, height: size.height } : null } };
      });
      await rec.step("Project#setActiveSequence (clone, then back)", async () => {
        need(project, "setActiveSequence", "Project");
        let onClone = null;
        try {
          await project.setActiveSequence(clone.seq);
          onClone = core.readGuid(await project.getActiveSequence());
        } finally {
          await tryGet(() => project.setActiveSequence(sequence));
        }
        const back = core.readGuid(await project.getActiveSequence());
        return { pass: onClone === clone.guid && back === origGuid, detail: { activeWhileSwitched: onClone, activeAfter: back } };
      });
      await rec.step("Project#deleteSequence", async () => {
        await need(project, "deleteSequence", "Project").deleteSequence(clone.seq);
        deleted = !(await sequencesOf(project)).some((s) => s.guid === clone.guid);
        return { pass: deleted };
      });
    },

    /* Player position, sequence in/out, work area: set, read back, put back. */
    async sequence_points(env) {
      const { rec, sequence } = env;
      const at = env.params.atSeconds || 1;
      await rec.step("Sequence#setPlayerPosition", async () => {
        need(sequence, "setPlayerPosition", "Sequence");
        const old = await sequence.getPlayerPosition();
        try {
          await sequence.setPlayerPosition(tt(at));
          const got = secs(await sequence.getPlayerPosition());
          return { pass: near(got, at, 0.02), detail: { want: at, got } };
        } finally { await tryGet(() => sequence.setPlayerPosition(old)); }
      });
      await rec.step("Sequence#createSetInPointAction / createSetOutPointAction", async () => {
        need(sequence, "createSetInPointAction", "Sequence");
        need(sequence, "createSetOutPointAction", "Sequence");
        const oldIn = await tryGet(() => sequence.getInPoint());
        const oldOut = await tryGet(() => sequence.getOutPoint());
        rec.undo("sequence in/out points", async () => {
          if (!oldIn || !oldOut) return false;
          tx(env, "in/out back (cleanup)", () => [sequence.createSetInPointAction(oldIn), sequence.createSetOutPointAction(oldOut)]);
          return near(secs(await sequence.getInPoint()), secs(oldIn), 0.02);
        });
        tx(env, "set in/out", () => [sequence.createSetInPointAction(tt(at)), sequence.createSetOutPointAction(tt(at + 1))]);
        const gotIn = secs(await tryGet(() => sequence.getInPoint()));
        const gotOut = secs(await tryGet(() => sequence.getOutPoint()));
        return { pass: near(gotIn, at, 0.02) && near(gotOut, at + 1, 0.02), detail: { before: [secs(oldIn), secs(oldOut)], got: [gotIn, gotOut] } };
      });
      await rec.step("WorkAreaUtils.setWorkAreaInOutPoints", async () => {
        const W = ppro.WorkAreaUtils;
        need(W, "setWorkAreaInOutPoints", "WorkAreaUtils");
        const oldIn = await tryGet(() => W.getWorkAreaInPoint(sequence));
        const oldOut = await tryGet(() => W.getWorkAreaOutPoint(sequence));
        try {
          await W.setWorkAreaInOutPoints(sequence, tt(at), tt(at + 2));
          const got = [secs(await tryGet(() => W.getWorkAreaInPoint(sequence))), secs(await tryGet(() => W.getWorkAreaOutPoint(sequence)))];
          return { pass: near(got[0], at, 0.02) && near(got[1], at + 2, 0.02), detail: { before: [secs(oldIn), secs(oldOut)], got } };
        } finally { if (oldIn && oldOut) await tryGet(() => W.setWorkAreaInOutPoints(sequence, oldIn, oldOut)); }
      });
    },

    /* Timeline selection: read, set to the scratch clip, clear. */
    async selection(env) {
      requireInputs(env.params, ["videoTrack", "atSeconds"], env.name);
      const { rec, sequence } = env;
      const clip = await scratchClip(env, "video");
      const count = async () => {
        const sel = await sequence.getSelection();
        return ((await tryGet(() => sel.getTrackItems(), [])) || []).length;
      };
      await rec.step("Sequence#getSelection", async () => ({ detail: { selected: await count() } }));
      await rec.step("Sequence#setSelection", async () => {
        need(sequence, "setSelection", "Sequence");
        let ok = null;
        ppro.TrackItemSelection.createEmptySelection((sel) => { sel.addItem(clip, true); ok = sequence.setSelection(sel); });
        const n = await count();
        return { pass: n === 1, detail: { returned: ok && typeof ok.then === "function" ? "promise" : ok, selected: n } };
      });
      await rec.step("Sequence#clearSelection", async () => {
        await need(sequence, "clearSelection", "Sequence").clearSelection();
        const n = await count();
        return { pass: n === 0, detail: { selected: n } };
      });
    },

    /* Track rename (26.3) and mute (direct, not undoable): change, read back, put back. */
    async track(env) {
      requireInputs(env.params, ["videoTrack"], env.name);
      const { rec, sequence } = env;
      const idx = env.params.videoTrack;
      const trackOf = () => timeline.getTrack(sequence, "video", idx);
      const old = (await trackOf()).name;
      await rec.step("VideoTrack#createSetNameAction", async () => {
        const track = await trackOf();
        need(track, "createSetNameAction", "VideoTrack");
        rec.undo(`name of V${idx + 1}`, async () => {
          const t = await trackOf();
          if (t.name !== old) tx(env, "track name back (cleanup)", () => t.createSetNameAction(old));
          return (await trackOf()).name === old;
        });
        tx(env, "rename track", () => track.createSetNameAction(`${LABEL} track`));
        const got = (await trackOf()).name;
        return { pass: got === `${LABEL} track`, detail: { before: old, got } };
      });
      await rec.step("VideoTrack#setMute / isMuted", async () => {
        const track = await trackOf();
        need(track, "setMute", "VideoTrack");
        const was = await tryGet(() => track.isMuted(), null);
        try {
          await track.setMute(!was);
          const got = await tryGet(() => (async () => (await trackOf()).isMuted())(), null);
          return { pass: got === !was, detail: { before: was, got } };
        } finally { await tryGet(() => track.setMute(!!was)); }
      });
    },

    /* Clip instance: disable and rename (put back), plus the documented reads. */
    async clip_props(env) {
      requireInputs(env.params, ["videoTrack", "atSeconds"], env.name);
      const { rec } = env;
      const clip = await scratchClip(env, "video");
      await rec.step("VideoClipTrackItem#createSetDisabledAction", async () => {
        need(clip, "createSetDisabledAction", "VideoClipTrackItem");
        const was = !!(await tryGet(() => clip.isDisabled(), false));
        rec.undo("enabled state of the scratch clip", async () => {
          if (!!(await tryGet(() => clip.isDisabled(), false)) !== was) tx(env, "enable back (cleanup)", () => clip.createSetDisabledAction(was));
          return !!(await tryGet(() => clip.isDisabled(), false)) === was;
        });
        tx(env, "toggle disabled", () => clip.createSetDisabledAction(!was));
        const got = await tryGet(() => clip.isDisabled());
        return { pass: got === !was, detail: { before: was, got } };
      });
      await rec.step("VideoClipTrackItem#createSetNameAction", async () => {
        need(clip, "createSetNameAction", "VideoClipTrackItem");
        const was = await tryGet(() => clip.getName());
        rec.undo("name of the scratch clip", async () => {
          if ((await tryGet(() => clip.getName())) !== was) tx(env, "clip name back (cleanup)", () => clip.createSetNameAction(was));
          return (await tryGet(() => clip.getName())) === was;
        });
        tx(env, "rename clip", () => clip.createSetNameAction(`${LABEL} renamed`));
        const got = await tryGet(() => clip.getName());
        return { pass: got === `${LABEL} renamed`, detail: { before: was, got } };
      });
      await rec.step("VideoClipTrackItem documented reads", async () => {
        const read = {};
        for (const g of ["getTrackIndex", "getType", "getMatchName", "getMediaType", "getIsSelected", "isAdjustmentLayer", "isSpeedReversed", "getDuration"]) {
          if (!core.isFn(clip, g)) { read[g] = "absent"; continue; }
          try { const v = await clip[g](); read[g] = secs(v) !== null ? secs(v) : unwrap(v); } catch (e) { read[g] = `error: ${core.errorMessage(e)}`; }
        }
        return { detail: read };
      });
    },

    /* Bins and project items: create, rename, colour label, subclip, move, remove. */
    async project_items(env) {
      const { rec, project } = env;
      const entry = await entryFor(env);
      const root = await project.getRootItem();
      const nameA = `${LABEL} bin ${env.stamp}`;
      const nameB = `${nameA} renamed`;
      let bin = null;
      await rec.step("FolderItem#createBinAction (baseline)", async () => {
        tx(env, "create bin", () => root.createBinAction(nameA, true));
        bin = await findBin(root, nameA);
        return { pass: !!bin };
      });
      if (!bin) return;
      rec.undo("test bin", async () => {
        const b = (await findBin(root, nameB)) || (await findBin(root, nameA));
        if (b) tx(env, "remove test bin (cleanup)", () => need(root, "createRemoveItemAction", "FolderItem").createRemoveItemAction(b));
        return !(await findBin(root, nameB)) && !(await findBin(root, nameA));
      });
      await rec.step("FolderItem#createRenameBinAction", async () => {
        need(bin, "createRenameBinAction", "FolderItem");
        tx(env, "rename bin", () => bin.createRenameBinAction(nameB));
        const got = await findBin(root, nameB);
        if (got) bin = got;
        return { pass: !!got };
      });
      await rec.step("ProjectItem#createSetColorLabelAction", async () => {
        need(bin, "createSetColorLabelAction", "FolderItem");
        tx(env, "bin colour label 5", () => bin.createSetColorLabelAction(5));
        const got = core.isFn(bin, "getColorLabelIndex") ? await tryGet(() => bin.getColorLabelIndex()) : "absent";
        return { pass: got === 5, detail: { got } };
      });
      const subName = `${LABEL} subclip ${env.stamp}`;
      let sub = null;
      await rec.step("ClipProjectItem#createSubClipAction", async () => {
        need(entry.clip, "createSubClipAction", "ClipProjectItem");
        let form = "positional";
        try {
          tx(env, "subclip", () => entry.clip.createSubClipAction(subName, tt(0.5), tt(1.5), true, true, true));
        } catch (e) {
          form = `object (positional threw: ${core.errorMessage(e)})`;
          tx(env, "subclip", () => entry.clip.createSubClipAction(subName, tt(0.5), tt(1.5), true, { takeVideo: true, takeAudio: true }));
        }
        const found = await findInProject(project, subName);
        sub = found.length ? found[0] : null;
        return { pass: !!sub, detail: { form, binPath: sub ? sub.binPath : null } };
      });
      if (sub) {
        rec.undo(`subclip "${subName}"`, async () => {
          const left = await findInProject(project, subName);
          for (const s of left) {
            const parent = core.isFn(s.item, "getParentBin") ? tryGetSync(() => s.item.getParentBin()) : root;
            tx(env, "remove subclip (cleanup)", () => (parent || root).createRemoveItemAction(s.item));
          }
          return !(await findInProject(project, subName)).length;
        });
        await rec.step("FolderItem#createMoveItemAction (subclip into the test bin)", async () => {
          const parent = core.isFn(sub.item, "getParentBin") ? tryGetSync(() => sub.item.getParentBin()) : root;
          need(parent || root, "createMoveItemAction", "FolderItem");
          tx(env, "move subclip", () => (parent || root).createMoveItemAction(sub.item, bin));
          const found = (await findInProject(project, subName))[0];
          return { pass: !!found && found.binPath === nameB, detail: { binPath: found ? found.binPath : null } };
        });
        await rec.step("FolderItem#createRemoveItemAction (subclip)", async () => {
          const found = (await findInProject(project, subName))[0];
          const parent = found && core.isFn(found.item, "getParentBin") ? tryGetSync(() => found.item.getParentBin()) : bin;
          need(parent || bin, "createRemoveItemAction", "FolderItem");
          tx(env, "remove subclip", () => (parent || bin).createRemoveItemAction(found.item));
          const gone = !(await findInProject(project, subName)).length;
          const source = await tryGet(() => entry.clip.getMediaFilePath());
          return { pass: gone, detail: { sourceStillLinked: source } };
        });
      }
      await rec.step("FolderItem#createRemoveItemAction (bin)", async () => {
        need(root, "createRemoveItemAction", "FolderItem");
        tx(env, "remove bin", () => root.createRemoveItemAction(bin));
        return { pass: !(await findBin(root, nameB)) };
      });
    },

    /* Relink a project item to a copy of its file and back; interpretation and metadata reads. */
    async relink(env) {
      requireInputs(env.params, ["itemName", "copyPath"], env.name);
      const { rec } = env;
      const clip = (await entryFor(env)).clip;
      const old = await clip.getMediaFilePath();
      await rec.step("ClipProjectItem#canChangeMediaPath", async () => ({ detail: { value: await need(clip, "canChangeMediaPath", "ClipProjectItem").canChangeMediaPath() } }));
      let moved = false;
      rec.undo(`media path of "${clip.name}"`, async () => {
        if (samePath(await clip.getMediaFilePath(), old)) return true;
        await clip.changeMediaFilePath(old, true);
        return samePath(await clip.getMediaFilePath(), old);
      });
      await rec.step("ClipProjectItem#changeMediaFilePath (to a copy)", async () => {
        const ok = await need(clip, "changeMediaFilePath", "ClipProjectItem").changeMediaFilePath(env.params.copyPath, true);
        const got = await clip.getMediaFilePath();
        moved = samePath(got, env.params.copyPath);
        return { pass: moved, detail: { returned: ok, got } };
      });
      await rec.step("ClipProjectItem#refreshMedia", async () => ({ detail: { returned: await need(clip, "refreshMedia", "ClipProjectItem").refreshMedia() } }));
      if (moved) {
        await rec.step("ClipProjectItem#changeMediaFilePath (back)", async () => {
          await clip.changeMediaFilePath(old, true);
          return { pass: samePath(await clip.getMediaFilePath(), old) };
        });
      }
      await rec.step("ClipProjectItem#isOffline", async () => ({ detail: { value: await need(clip, "isOffline", "ClipProjectItem").isOffline() } }));
      await rec.step("ClipProjectItem#getFootageInterpretation", async () => {
        const fi = await need(clip, "getFootageInterpretation", "ClipProjectItem").getFootageInterpretation();
        const read = {};
        for (const g of ["getFrameRate", "getPixelAspectRatio", "getFieldType", "getAlphaUsage", "getIgnoreAlpha", "getRemovePullDown"]) {
          read[g] = fi && core.isFn(fi, g) ? unwrap(tryGetSync(() => fi[g]())) : "absent";
        }
        return { detail: read };
      });
      const item = (await entryFor(env)).item;
      for (const fn of ["getXMPMetadata", "getProjectMetadata"]) {
        await rec.step(`Metadata.${fn}`, async () => {
          need(ppro.Metadata, fn, "Metadata");
          let form = "ProjectItem";
          let s;
          try { s = await ppro.Metadata[fn](item); } catch (e) {
            form = `ClipProjectItem (ProjectItem threw: ${core.errorMessage(e)})`;
            s = await ppro.Metadata[fn](clip);
          }
          return { pass: typeof s === "string", detail: { form, length: typeof s === "string" ? s.length : null } };
        });
      }
    },

    /* Custom properties on the sequence: set, read, clear. */
    async properties(env) {
      const { rec, sequence } = env;
      const key = `${LABEL}.smoke`;
      let props = null;
      await rec.step("Properties.getProperties(sequence)", async () => {
        props = await need(ppro.Properties, "getProperties", "Properties").getProperties(sequence);
        return { pass: !!props };
      });
      if (!props) return;
      const persistent = constant("PropertyType", "PERSISTENT", 1);
      rec.undo(`sequence property ${key}`, async () => {
        const p = await ppro.Properties.getProperties(sequence);
        if (tryGetSync(() => p.hasValue(key), false)) tx(env, "clear property (cleanup)", () => p.createClearValueAction(key));
        const after = await ppro.Properties.getProperties(sequence);
        return tryGetSync(() => after.hasValue(key), null) === false;
      });
      await rec.step("Properties#createSetValueAction", async () => {
        need(props, "createSetValueAction", "Properties");
        tx(env, "set property", () => props.createSetValueAction(key, "v1", persistent));
        const p = await ppro.Properties.getProperties(sequence);
        const got = tryGetSync(() => p.getValue(key));
        return { pass: got === "v1", detail: { got } };
      });
      await rec.step("Properties#createClearValueAction", async () => {
        const p = await ppro.Properties.getProperties(sequence);
        need(p, "createClearValueAction", "Properties");
        tx(env, "clear property", () => p.createClearValueAction(key));
        const after = await ppro.Properties.getProperties(sequence);
        const has = tryGetSync(() => after.hasValue(key), null);
        return { pass: has === false, detail: { hasValue: has } };
      });
    },

    /* Project#save on the PAB_TEST project (the runner compares the file's time stamp). */
    async save(env) {
      await env.rec.step("Project#save", async () => {
        const ok = await need(env.project, "save", "Project").save();
        return { pass: ok !== false, detail: { returned: ok, path: tryGetSync(() => env.project.path) } };
      });
    },

    /* FCP XML and AAF exports into outputDir (the runner checks the files). */
    async converter_exports(env) {
      requireInputs(env.params, ["outputDir"], env.name);
      const { rec, sequence } = env;
      const dir = String(env.params.outputDir).replace(/[\\/]+$/, "");
      const sep = dir.indexOf("\\") >= 0 ? "\\" : "/";
      await rec.step("ProjectConverter.exportAsFinalCutProXML", async () => {
        const path = `${dir}${sep}pab_api_${env.stamp}.xml`;
        const ok = await need(ppro.ProjectConverter, "exportAsFinalCutProXML", "ProjectConverter").exportAsFinalCutProXML(sequence, path, true);
        return { pass: ok !== false, detail: { returned: ok, path } };
      });
      await rec.step("ProjectConverter.exportAAF", async () => {
        if (!env.params.allowAaf) {
          return { skip: "opt-in: allowAaf (27.1 b21: a modal 'AAF export log' blocks the call when an effect or " +
                         "transition cannot go into the AAF, until someone clicks OK)" };
        }
        const path = `${dir}${sep}pab_api_${env.stamp}.aaf`;
        const opts = typeof ppro.AAFExportOptions === "function" ? tryGetSync(() => new ppro.AAFExportOptions()) : undefined;
        const ok = await need(ppro.ProjectConverter, "exportAAF", "ProjectConverter").exportAAF(sequence, path, opts);
        return { pass: ok !== false, detail: { returned: ok, path, options: opts ? "AAFExportOptions" : "none" } };
      });
    },

    /* MOGRT: the installed templates folder; with mogrtPath also insert one, list its params (text untouched), remove it. */
    async mogrt(env) {
      const { rec, sequence } = env;
      await rec.step("SequenceEditor.getInstalledMogrtPath", async () => {
        const p = await need(ppro.SequenceEditor, "getInstalledMogrtPath", "SequenceEditor").getInstalledMogrtPath();
        return { pass: typeof p === "string" && p !== "", detail: { path: p } };
      });
      if (!env.params.mogrtPath) {
        return rec.step("SequenceEditor#insertMogrtFromPath", async () => ({ skip: "pass mogrtPath (a .mogrt file)" }));
      }
      requireInputs(env.params, ["videoTrack", "audioTrack", "atSeconds"], env.name);
      const editor = ppro.SequenceEditor.getEditor(sequence);
      let items = [];
      await rec.step("SequenceEditor#insertMogrtFromPath", async () => {
        need(editor, "insertMogrtFromPath", "SequenceEditor");
        let r = null;
        env.project.lockedAccess(() => {
          r = editor.insertMogrtFromPath(env.params.mogrtPath, tt(env.params.atSeconds), env.params.videoTrack, env.params.audioTrack);
        });
        env.ctx.sideEffect();
        if (r && typeof r.then === "function") r = await r;
        items = Array.isArray(r) ? r : [];
        const described = [];
        for (const it of items) {
          const d = await timeline.describeItem(it);
          const chain = await tryGet(() => it.getComponentChain());
          const comps = chain ? await componentsOf(chain) : [];
          const graphic = comps.find((c) => c.matchName && !/Motion|Opacity|Volume/.test(c.matchName));
          described.push({ name: d.name, start: d.start, end: d.end, components: comps.map((c) => c.matchName),
                           params: graphic ? await paramList(graphic.component, 60) : [] });
        }
        return { pass: items.length > 0, detail: { returned: items.length, items: described } };
      });
      if (items.length) {
        let gone = false;
        rec.undo("inserted MOGRT clips", async () => gone);
        await rec.step("remove the inserted MOGRT clips", async () => { gone = removeTrackItems(env, items, "remove MOGRT"); return { pass: gone }; });
      }
    },

    /* Source monitor: open a project item, move its position, close it. */
    async source_monitor(env) {
      const { rec } = env;
      const entry = await entryFor(env);
      const SM = ppro.SourceMonitor;
      await rec.step("SourceMonitor.openProjectItem + getProjectItem", async () => {
        need(SM, "openProjectItem", "SourceMonitor");
        const tried = [];
        let pi = null;
        for (const [form, arg] of [["ClipProjectItem", entry.clip], ["ProjectItem", entry.item]]) {
          const r = await tryGet(() => SM.openProjectItem(arg), "threw");
          for (let i = 0; i < 10 && !pi; i++) {
            await core.sleep(100);
            pi = await tryGet(() => SM.getProjectItem());
          }
          tried.push({ form, returned: r, opened: pi ? pi.name : null });
          if (pi) break;
        }
        return { pass: !!pi && pi.name === entry.clip.name, detail: { tried } };
      });
      await rec.step("SourceMonitor.setPosition / getPosition", async () => {
        await need(SM, "setPosition", "SourceMonitor").setPosition(tt(1));
        const got = secs(await tryGet(() => SM.getPosition()));
        return { pass: near(got, 1, 0.05), detail: { got } };
      });
      await rec.step("SourceMonitor.closeClip", async () => ({ detail: { returned: await need(SM, "closeClip", "SourceMonitor").closeClip() } }));
    },

    /* Opt-in (allowTrackAdd): an insert past the last track index; the docs say Premiere adds the tracks. No API removes them. */
    async implicit_track(env) {
      const { rec, sequence } = env;
      if (!env.params.allowTrackAdd) {
        return rec.step("SequenceEditor#createInsertProjectItemAction past the last track", async () => ({ skip: "opt-in: allowTrackAdd (adds tracks that no API can remove)" }));
      }
      requireInputs(env.params, ["atSeconds"], env.name);
      const entry = await entryFor(env);
      const counts = async () => ({ video: await timeline.trackCount(sequence, "video"), audio: await timeline.trackCount(sequence, "audio") });
      const before = await counts();
      await rec.step("SequenceEditor#createInsertProjectItemAction past the last track", async () => {
        const editor = ppro.SequenceEditor.getEditor(sequence);
        tx(env, "insert past the last track", () => editor.createInsertProjectItemAction(entry.item, tt(env.params.atSeconds), before.video, before.audio, true));
        const after = await counts();
        return { pass: after.video > before.video, detail: { before, after } };
      });
      const after = await counts();
      if (after.video > before.video || after.audio > before.audio) {
        rec.undo(`tracks added: ${after.video - before.video} video, ${after.audio - before.audio} audio (no API removes tracks)`, async () => false);
      }
    },

    /* The runner's own test bin (binName starting with PAB_API_, top level) and the media it imported: removed. */
    async cleanup_bin(env) {
      requireInputs(env.params, ["binName"], env.name);
      if (String(env.params.binName).indexOf(`${LABEL}_`) !== 0) {
        throw new BridgeError("BAD_PARAMS", `cleanup_bin only removes bins named ${LABEL}_*, not "${env.params.binName}".`);
      }
      const root = await env.project.getRootItem();
      const bin = await findBin(root, env.params.binName);
      if (!bin) throw new BridgeError("NOT_FOUND", `No top-level bin "${env.params.binName}".`);
      await env.rec.step("FolderItem#createRemoveItemAction (the runner's bin)", async () => {
        need(root, "createRemoveItemAction", "FolderItem");
        const inside = ((await tryGet(() => bin.getItems(), [])) || []).map((it) => it.name);
        tx(env, `remove bin ${env.params.binName}`, () => root.createRemoveItemAction(bin));
        return { pass: !(await findBin(root, env.params.binName)), detail: { items: inside } };
      });
    },
  };

  const handlers = {
    /* params: { case, videoTrack?, audioTrack?, atSeconds?, itemName?, binPath?, copyPath?, outputDir?,
     *           displayName?, paramIndex?, value?, mogrtPath?, allowTrackAdd?, binName?, allowProject? }
     * allowProject: the exact name of one non-PAB_TEST project the user agreed to test in. */
    async api_smoke(params, ctx) {
      const { project, sequence } = await core.requireSequence(ctx);
      const projectName = String(tryGetSync(() => project.name, ""));
      if (projectName.indexOf(PREFIX) !== 0 && !(params.allowProject && params.allowProject === projectName)) {
        throw new BridgeError("PRECONDITION_FAILED", `api_smoke runs only in a ${PREFIX}* project (or the one named by ` +
          `allowProject); the open project is "${projectName}".`);
      }
      const rec = recorder();
      const d = new Date();
      const pad = (n) => (n < 10 ? "0" : "") + n;
      const stamp = `${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
      const env = { name: params.case, params, ctx, project, sequence, rec, stamp };
      try {
        await CASES[params.case](env);
      } catch (e) {
        if (e instanceof BridgeError && e.code === "BAD_PARAMS" && !ctx.sideEffects) throw e;
        if (e instanceof BridgeError && (e.code === "NOT_FOUND" || e.code === "AMBIGUOUS") && !ctx.sideEffects) throw e;
        rec.steps.push({ api: "case", status: "error", detail: core.errorMessage(e), ms: 0 });
      } finally {
        await rec.runCleanups();
      }
      const summary = { pass: 0, fail: 0, absent: 0, error: 0, skipped: 0 };
      for (const s of rec.steps) summary[s.status] = (summary[s.status] || 0) + 1;
      return { case: params.case, project: projectName, steps: rec.steps, summary, leftovers: rec.leftovers };
    },
  };
  // The case names for tests; not enumerable, so main.js does not take it for a handler.
  Object.defineProperty(handlers, "CASES", { value: Object.keys(CASES), enumerable: false });
  return handlers;
};
