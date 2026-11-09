/*
 * handlers_removals: take one thing off a clip and leave the rest alone — the video transition at one edge, one added
 * effect (filter) from the chain, the keyframes of one param. Each finds its target strictly (a read that fails
 * refuses, it never counts as "nothing there"), refuses before changing anything when there is nothing to remove,
 * runs one labelled transaction (one undo step) and reads the result back. A dry run reports what would go, with
 * `wouldRefuse` + `reason` where a real run would refuse with NOT_FOUND (like remove_project_items' dry run).
 * The calls are the ones api_smoke verified on Premiere 27.1 b21 (plugin/lib/smoke.js, 22 Oct 2026):
 * createRemoveVideoTransitionAction(TransitionPosition START = 1 / END = 2), createRemoveComponentAction,
 * createRemoveKeyframeRangeAction, createSetTimeVaryingAction(false). Keyframes are read with
 * getKeyframeListAsTickTimes only.
 * Factory module returning {actionName: async function (params, ctx)}.
 * deps: { ppro, core, timeline, effects }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const timeline = deps.timeline;
  const effects = deps.effects;
  const BridgeError = core.BridgeError;
  const tryGet = core.tryGet;
  const tryGetSync = core.tryGetSync;
  const EPS = core.EPS;
  const TPS = core.TICKS_PER_SECOND;
  const NUDGE_TICKS = Math.round(core.TIME_NUDGE_S * TPS);  // 1 us either end of a keyframe range: typed times round

  // Built into every clip, never removed: Motion and Opacity (replace.py INTRINSIC) and Time Remapping (match name
  // assumed: no 27.1 chain lists it — 24 clips read [Opacity, Motion] on 3 Nov 2026, and a .prproj keeps time
  // remapping outside the chain, as a <TimeRemapping> object on the clip; kept in case a build shows it). An audio
  // clip's own components are "Internal ..." (Internal Volume Mono / Stereo, measured on 27.1); added audio effects
  // have other match names (a GUID for Premiere's own, e.g. DeNoise).
  const INTRINSIC = ["AE.ADBE Motion", "AE.ADBE Opacity", "AE.ADBE Time Remapping"];
  const isIntrinsic = (m) => INTRINSIC.indexOf(m) >= 0 || /^Internal /.test(String(m));

  const r6 = (x) => (typeof x === "number" ? Math.round(x * 1e6) / 1e6 : x);
  const clipView = (d) => ({ name: d.name, start: d.start, end: d.end });

  /* NOT_FOUND about what is on the clip: a dry run reports it (wouldRefuse), a real run refuses. */
  function refuse(out, dryRun, reason, data) {
    if (dryRun) return Object.assign(out, { wouldRefuse: "NOT_FOUND", reason });
    throw new BridgeError("NOT_FOUND", reason, data === undefined ? undefined : { data });
  }

  /* The one clip under atSeconds on videoTrack or audioTrack (exactly one of them). */
  async function targetClip(params, sequence, action) {
    const hasV = typeof params.videoTrack === "number";
    const hasA = typeof params.audioTrack === "number";
    if (hasV === hasA) throw new BridgeError("BAD_PARAMS", `${action} needs exactly one of videoTrack or audioTrack.`);
    const kind = hasA ? "audio" : "video";
    const index = hasA ? params.audioTrack : params.videoTrack;
    const label = timeline.trackLabel(kind, index);
    const it = await timeline.itemAt(sequence, kind, index, params.atSeconds);
    return { kind, index, label, it, desc: timeline.requireSpan(await timeline.describeItem(it), label) };
  }

  /* The clip's chain and its match names, read strictly: a name that does not read would shift the occurrence
   * of every instance after it, so nothing is removed on a guess. */
  async function readChain(it, where) {
    let chain = null;
    try { chain = await it.getComponentChain(); } catch (e) { chain = null; }
    if (!chain) throw new BridgeError("INTERNAL", `[read] ${where}: the effect chain did not read; nothing was changed.`);
    const names = await effects.chainMatchNames(chain);
    if (names.some((n) => typeof n !== "string" || n === "")) {
      throw new BridgeError("INTERNAL", `[read] ${where}: a component's match name did not read (${names.map(String).join(", ")}); ` +
        "nothing was changed.");
    }
    return { chain, names };
  }

  /* Constants.TransitionPosition (27.1: START = 1, END = 2, api_smoke), else VideoTransition's statics. */
  function transitionPosition(edge) {
    const key = edge === "start" ? "START" : "END";
    const c = ppro.Constants && ppro.Constants.TransitionPosition;
    if (c && c[key] !== undefined && c[key] !== null) return c[key];
    const v = ppro.VideoTransition ? tryGetSync(() => ppro.VideoTransition[`TRANSITIONPOSITION_${key}`]) : null;
    return v !== null && v !== undefined ? v : (key === "START" ? 1 : 2);
  }

  /* Two param values read back alike (Premiere stores floats: numbers within 1e-4 relative). */
  function sameValue(a, b) {
    const num = (x, y) => typeof x === "number" && typeof y === "number" && Math.abs(x - y) <= Math.max(1e-6, Math.abs(y) * 1e-4);
    if (typeof a === "number" || typeof b === "number") return num(a, b);
    if (a && b && typeof a === "object" && typeof b === "object") {
      return Object.keys(b).every((k) => (typeof b[k] === "number" ? num(a[k], b[k]) : a[k] === b[k]));
    }
    return a === b;
  }

  return {
    /*
     * Remove the video transition at the start or end edge of the clip under atSeconds. One undo step.
     * params: { videoTrack, atSeconds, edge: "start" | "end", dryRun?, undoLabel? }
     * Transition items expose no name or times over UXP, so the panel reads the track's transition count, strictly,
     * before and after: no transition on the track is NOT_FOUND before anything changes, and a removal that leaves
     * the count as it was is NOT_FOUND too (not_committed: nothing changed). An edit point holds one transition,
     * whichever clip it lies on; when the clip's own call removes nothing and one clip touches that edge, the
     * neighbour's opposite edge is asked (one more undo step, "[neighbour]"; which side Premiere files a
     * neighbour's single-sided fade under is unmeasured). Which transition it was (name, seconds either side of the
     * cut, owner) comes from OTIO: ops.remove_transition finds it there first and verifies it is gone.
     * byOtio: on 27.1 a transition added over UXP is not among the track's transition items (count 0, live), so the count
     * cannot refuse or prove anything: the call is made anyway and `removed: null` leaves the proof to the caller's OTIO
     * export. side "neighbour": only the neighbour's opposite edge is asked (the caller saw the first call change nothing).
     */
    async remove_transition(params, ctx) {
      const dryRun = !!params.dryRun;
      const { project, sequence } = await core.requireSequence(ctx);
      const label = timeline.trackLabel("video", params.videoTrack);
      const track = await timeline.getTrack(sequence, "video", params.videoTrack);
      const it = await timeline.clipAt(sequence, params.videoTrack, params.atSeconds);
      const d = timeline.requireSpan(await timeline.describeItem(it), label);
      const atStart = params.edge === "start";
      const cut = atStart ? d.start : d.end;
      // The clip on the other side of that edit point, when exactly one touches it.
      const touching = [];
      for (const o of await timeline.clipItems(track)) {
        const od = timeline.requireSpan(await timeline.describeItem(o), label);
        if (Math.abs(od.start - d.start) <= EPS && Math.abs(od.end - d.end) <= EPS) continue;  // the clip itself
        if (Math.abs((atStart ? od.end : od.start) - cut) <= EPS) touching.push({ it: o, desc: od });
      }
      const neighbour = touching.length === 1 ? touching[0] : null;
      if (!core.isFn(it, "createRemoveVideoTransitionAction")) {
        throw new BridgeError("UNSUPPORTED", "This Premiere build has no VideoClipTrackItem#createRemoveVideoTransitionAction; nothing was changed.");
      }
      const count = async () => (await timeline.transitionItems(track, label)).length;
      const before = await count();
      const out = { dryRun, track: label, edge: params.edge, cut, clip: clipView(d),
                    neighbour: neighbour ? clipView(neighbour.desc) : null, removed: false, via: null,
                    transitionCount: { before } };
      const byOtio = params.byOtio === true;
      if (before === 0 && !byOtio) return refuse(out, dryRun, `${label} has no transitions; nothing was changed.`);
      if (dryRun) return out;

      const undo = params.undoLabel || `Agent: remove the transition at the ${params.edge} of ${label} @${params.atSeconds}s`;
      const problems = [];
      const attempt = (item, edge, lbl) => {
        try {
          core.runTransaction(project, lbl, () => item.createRemoveVideoTransitionAction(transitionPosition(edge)), ctx);
        } catch (e) { problems.push(core.errorMessage(e)); }
      };
      if (params.side === "neighbour") {
        if (!neighbour) throw new BridgeError("NOT_FOUND", `No clip touches the ${params.edge} of ${d.name} on ${label}; nothing was changed.`);
        attempt(neighbour.it, atStart ? "end" : "start", undo + " [neighbour]");
        const n = await count();
        return Object.assign(out, { removed: n < before ? true : null, via: "neighbour", problems, transitionCount: { before, after: n }, undoLabel: undo });
      }
      attempt(it, params.edge, undo);
      let after = await count();
      let via = after < before ? "clip" : null;
      if (!via && byOtio && before === 0) {             // nothing to count: the caller's OTIO export says whether it went
        return Object.assign(out, { removed: null, via: "clip", problems, transitionCount: { before, after }, undoLabel: undo });
      }
      if (!via && neighbour && core.isFn(neighbour.it, "createRemoveVideoTransitionAction")) {
        attempt(neighbour.it, atStart ? "end" : "start", undo + " [neighbour]");
        after = await count();
        via = after < before ? "neighbour" : null;
      }
      if (!via) {
        // The count is the read-back: unchanged means the timeline is as it was.
        throw new BridgeError("NOT_FOUND", `No transition at the ${params.edge} of ${d.name} (${r6(cut)}s) on ${label}: ` +
          `the track still has ${after} transition(s)` + (problems.length ? ` (${problems.join("; ")})` : "") +
          ". Nothing was removed.", { outcome: "not_committed", data: { transitionCount: { before, after } } });
      }
      return Object.assign(out, { removed: true, via, transitionCount: { before, after }, undoLabel: undo });
    },

    /*
     * Remove one added component (filter) from the chain of the clip under atSeconds. One undo step.
     * params: { videoTrack | audioTrack, atSeconds, matchName, occurrence = 1, dryRun?, undoLabel? }
     * `occurrence` is the n-th instance of matchName in chain order (inspect_items lists it). The components built
     * into every clip (Motion, Opacity, Time Remapping, an audio clip's "Internal ..." volume) are refused
     * (UNSUPPORTED): set their params instead. Read back: the chain afterwards must be the chain before without
     * exactly that instance (`verified`).
     */
    async remove_effect(params, ctx) {
      core.requireText(params, ["matchName"]);
      const dryRun = !!params.dryRun;
      const { project, sequence } = await core.requireSequence(ctx);
      const t = await targetClip(params, sequence, "remove_effect");
      const m = params.matchName;
      const occ = params.occurrence || 1;
      const where = `${t.label} @${params.atSeconds}s (${t.desc.name})`;
      if (isIntrinsic(m)) {
        throw new BridgeError("UNSUPPORTED", `${m} is built into every clip and cannot be removed; set its params instead ` +
          "(set_params). Nothing was changed.");
      }
      const { chain, names } = await readChain(t.it, where);
      const out = { dryRun, track: t.label, clip: clipView(t.desc), matchName: m, occurrence: occ, chainBefore: names };
      const idxs = effects.indexesOf(names, m);
      if (occ > idxs.length) {
        return refuse(out, dryRun, `${where} has ${idxs.length ? `only ${idxs.length} ${m}` : `no ${m}`} (its chain: ` +
          `${names.join(", ")}); nothing was changed.`, { chain: names });
      }
      if (!core.isFn(chain, "createRemoveComponentAction")) {
        throw new BridgeError("UNSUPPORTED", "This Premiere build has no ComponentChain#createRemoveComponentAction; nothing was changed.");
      }
      const index = idxs[occ - 1];
      const comp = chain.getComponentAtIndex(index);
      if (!comp) throw new BridgeError("INTERNAL", `[read] ${where}: component ${index} did not read; nothing was changed.`);
      out.index = index;
      out.displayName = await tryGet(() => comp.getDisplayName());
      if (dryRun) return out;

      const label = params.undoLabel || `Agent: remove ${m}${occ > 1 ? ` [${occ}]` : ""} from ${t.label} @${params.atSeconds}s`;
      core.runTransaction(project, label, () => chain.createRemoveComponentAction(comp), ctx);
      const after = (await readChain(t.it, where)).names;
      const want = names.slice(0, index).concat(names.slice(index + 1));
      return Object.assign(out, { chain: after, verified: after.length === want.length && after.every((n, i) => n === want[i]),
                                  undoLabel: label });
    },

    /*
     * Remove keyframes of one param of the clip under atSeconds: every one (all: animation off), or those in
     * [fromSeconds, toSeconds] (timeBase media | clip | sequence, converted like set_params' keyframes; the range
     * may reach past the clip, where a trim left keyframes; 1 us of slack at either end). One undo step.
     * params: { videoTrack | audioTrack, atSeconds, matchName, occurrence = 1, paramIndex,
     *           all? | fromSeconds + toSeconds + timeBase, keepValue?, dryRun?, undoLabel? }
     * When every keyframe goes (all, or a range covering them all), animation is switched off
     * (createSetTimeVaryingAction(false)); which value Premiere then keeps is unmeasured (api_smoke checks only that
     * the keys are gone), so `valueAfter` reports it. keepValue: set the value the param had at the middle of the
     * clip (where fades at either end do not reach) in the same step. A partial range goes in one
     * createRemoveKeyframeRangeAction whose ends lie half way to the keys that stay, so the keys taken do not depend
     * on whether Premiere's range includes its ends. Read back: the keys left (and the value kept) -> `verified`.
     */
    async remove_keyframes(params, ctx) {
      core.requireText(params, ["matchName"]);
      const dryRun = !!params.dryRun;
      const all = params.all === true;
      const ranged = typeof params.fromSeconds === "number" && typeof params.toSeconds === "number";
      if (all === ranged) {
        throw new BridgeError("BAD_PARAMS", "remove_keyframes needs all: true, or fromSeconds / toSeconds with a timeBase — not both.");
      }
      if (ranged && typeof params.timeBase !== "string") {
        throw new BridgeError("BAD_PARAMS", "remove_keyframes: fromSeconds / toSeconds need a timeBase (media, clip or sequence).");
      }
      if (ranged && params.toSeconds < params.fromSeconds) {
        throw new BridgeError("BAD_PARAMS", `remove_keyframes: toSeconds (${params.toSeconds}) is before fromSeconds (${params.fromSeconds}).`);
      }
      const { project, sequence } = await core.requireSequence(ctx);
      const t = await targetClip(params, sequence, "remove_keyframes");
      const d = t.desc;
      const frameTicks = Number(await tryGet(() => sequence.getTimebase())) || 0;
      const m = params.matchName;
      const occ = params.occurrence || 1;
      const where = `${m}[${occ}]#${params.paramIndex}`;
      const { chain, names } = await readChain(t.it, `${t.label} @${params.atSeconds}s (${d.name})`);
      const out = { dryRun, track: t.label, clip: clipView(d), matchName: m, occurrence: occ, index: params.paramIndex,
                    mode: all ? "all" : "range", removed: [], remaining: [], animationOff: false };
      const idxs = effects.indexesOf(names, m);
      if (occ > idxs.length) {
        return refuse(out, dryRun, `${t.label} @${params.atSeconds}s (${d.name}) has ${idxs.length ? `only ${idxs.length} ${m}` : `no ${m}`} ` +
          `(its chain: ${names.join(", ")}); nothing was changed.`, { chain: names });
      }
      const comp = chain.getComponentAtIndex(idxs[occ - 1]);
      const pc = comp ? (tryGetSync(() => comp.getParamCount(), 0) || 0) : 0;
      const param = comp && params.paramIndex < pc ? tryGetSync(() => comp.getParam(params.paramIndex)) : null;
      if (!param) return refuse(out, dryRun, `${m} has no param #${params.paramIndex} (it has ${pc}); nothing was changed.`);
      out.name = tryGetSync(() => param.displayName);

      // The range in media ticks: input mistakes (time base, speed, 9 h) before anything about the keys.
      let lo = null;
      let hi = null;
      if (ranged) {
        const conv =(v, end) => effects.convertKeyframeTime({
          timeBase: params.timeBase, t: v, start: d.start, inPoint: d.inPoint, outPoint: d.outPoint, speed: d.speed,
          frameTicks, where: `${where} ${end}`, bounded: false,
        });
        const a = conv(params.fromSeconds, "fromSeconds");
        const b = conv(params.toSeconds, "toSeconds");
        lo = a.mediaTicks - NUDGE_TICKS;
        hi = b.mediaTicks + NUDGE_TICKS;
        out.range = { timeBase: params.timeBase, from: params.fromSeconds, to: params.toSeconds,
                      fromMediaSeconds: a.mediaSeconds, toMediaSeconds: b.mediaSeconds };
      }

      // The keys, strictly: a list that does not read is not "no keyframes".
      let keyed;
      try { keyed = !!param.isTimeVarying(); } catch (e) {
        throw new BridgeError("INTERNAL", `[read] ${where}: isTimeVarying did not read; nothing was changed.`);
      }
      let keys = [];
      if (keyed) {
        let list;
        try { list = param.getKeyframeListAsTickTimes() || []; } catch (e) {
          throw new BridgeError("INTERNAL", `[read] ${where}: the keyframe list did not read; nothing was changed.`);
        }
        keys = list.map((tt) => ({ tt, ticks: core.ticksOf(tt) }));
        if (keys.some((k) => k.ticks === null)) throw new BridgeError("INTERNAL", `[read] ${where}: a keyframe time did not read; nothing was changed.`);
        keys.sort((x, y) => x.ticks - y.ticks);
      }
      const seconds = (list) => list.map((k) => k.ticks / TPS);
      if (!keys.length) return refuse(out, dryRun, `${where} ${out.name} has no keyframes; nothing was changed.`);
      const doomed = ranged ? keys.filter((k) => k.ticks >= lo && k.ticks <= hi) : keys;
      if (!doomed.length) {
        return refuse(out, dryRun, `${where} ${out.name} has no keyframe between media ${r6(out.range.fromMediaSeconds)}s and ` +
          `${r6(out.range.toMediaSeconds)}s (its keyframes: ${seconds(keys).map(r6).join(", ")}s); nothing was changed.`);
      }
      const off = doomed.length === keys.length;
      const kept = keys.filter((k) => doomed.indexOf(k) < 0);
      Object.assign(out, { removed: seconds(doomed), remaining: seconds(kept), animationOff: off });

      // keepValue: the value at the middle of the clip (media time), written back once animation is off.
      const midTicks = Math.round(((d.inPoint + d.outPoint) / 2) * TPS);
      let keep = null;
      if (off && params.keepValue) {
        let raw;
        try { raw = await param.getValueAtTime(core.tickTimeFromTicks(midTicks)); } catch (e) { raw = undefined; }
        const plain = effects.plainValue(raw);
        if (plain === null || plain === undefined) {
          throw new BridgeError("INTERNAL", `[read] ${where}: the value at the middle of the clip did not read, so it cannot be kept; nothing was changed.`);
        }
        try { keep = effects.normalizeValue(plain, where); } catch (e) {
          throw new BridgeError("UNSUPPORTED", `${where} ${out.name}: its value ${JSON.stringify(plain)} cannot be written back ` +
            "(keepValue); nothing was changed.");
        }
        out.keptValue = plain;
      } else if (params.keepValue) {
        out.keptValue = null;  // keyframes stay, so there is no static value to keep
      }
      const needs = off ? ["createSetTimeVaryingAction"].concat(keep ? ["createSetValueAction", "createKeyframe"] : [])
        : ["createRemoveKeyframeRangeAction"];
      const absent = needs.filter((n) => !core.isFn(param, n));
      if (absent.length) throw new BridgeError("UNSUPPORTED", `This Premiere build has no ComponentParam#${absent.join(", #")}; nothing was changed.`);
      if (dryRun) return out;

      const label = params.undoLabel || `Agent: remove ${off ? "all keyframes" : `${doomed.length} keyframe(s)`} of ` +
        `${where} on ${t.label} @${params.atSeconds}s`;
      // A partial range: between the first / last key that goes and its neighbour that stays (half way), or half a
      // frame out where none stays on that side. The keys in it are contiguous by construction.
      const half = Math.max(1000, Math.round((frameTicks || TPS / 30) / 2));
      const first = doomed[0].ticks;
      const last = doomed[doomed.length - 1].ticks;
      const prev = keys.filter((k) => k.ticks < first).pop();
      const next = keys.filter((k) => k.ticks > last)[0];
      const a = prev ? first - Math.floor((first - prev.ticks) / 2) : first - half;
      const b = next ? last + Math.floor((next.ticks - last) / 2) : last + half;
      core.runTransaction(project, label, () => {
        if (!off) return param.createRemoveKeyframeRangeAction(core.tickTimeFromTicks(a), core.tickTimeFromTicks(b), false);
        const actions = [param.createSetTimeVaryingAction(false)];
        if (keep) actions.push(param.createSetValueAction(param.createKeyframe(effects.toParamValue(keep)), true));
        return actions;
      }, ctx);

      // Read back.
      const keyedAfter = tryGetSync(() => param.isTimeVarying(), null);
      const listAfter = keyedAfter === false ? [] : tryGetSync(() => param.getKeyframeListAsTickTimes(), null);
      const left = listAfter === null ? null : listAfter.map((tt) => core.ticksOf(tt)).sort((x, y) => x - y);
      out.remaining = left === null ? null : left.map((x) => (x === null ? null : x / TPS));
      let verified = left !== null && left.length === kept.length && left.every((x, i) => x !== null && Math.abs(x - kept[i].ticks) <= 1);
      if (off) {
        out.valueAfter = effects.plainValue(await tryGet(() => param.getValueAtTime(core.tickTimeFromTicks(midTicks))));
        verified = verified && keyedAfter === false && (!keep || sameValue(out.valueAfter, out.keptValue));
      }
      return Object.assign(out, { verified, undoLabel: label });
    },
  };
};
