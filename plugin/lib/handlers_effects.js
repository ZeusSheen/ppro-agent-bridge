/*
 * handlers_effects: effect params on track items (Motion > Scale, any component param with
 * keyframes in an explicit time base) and video transitions.
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
  const MAX_CREATED = 16; // effect instances one set_params request may append (guards a typo in occurrence)

  return {
    /*
     * Set Motion > Scale (percent) on the clips of one video track that overlap
     * [fromSeconds, toSeconds] (sequence seconds; from == to targets the clip
     * under that instant). One undo step.
     * params: { videoTrack, fromSeconds, toSeconds?, value, dryRun?, undoLabel? }
     */
    async set_scale(params, ctx) {
      const dryRun = !!params.dryRun;
      if (!dryRun && typeof params.value !== "number") {
        throw new BridgeError("BAD_PARAMS", "set_scale requires params.value (percent) unless dryRun");
      }
      const { project, sequence } = await core.requireSequence(ctx);
      const to = typeof params.toSeconds === "number" ? params.toSeconds : params.fromSeconds;
      const track = await timeline.getTrack(sequence, "video", params.videoTrack);

      const targets = [];
      for (const it of await timeline.clipItems(track)) {
        const d = timeline.requireSpan(await timeline.describeItem(it), `V${params.videoTrack + 1}`);
        if (!timeline.overlaps(d, params.fromSeconds, to)) continue;
        const scale = await effects.findMotionScale(it);
        d.scaleParam = scale ? scale.param.displayName : null;
        // getValueAtTime gives Premiere's {value} wrapper on 27.1: report the plain number.
        d.scaleBefore = scale ? effects.plainValue(await tryGet(() => scale.param.getValueAtTime(ppro.TickTime.createWithSeconds(d.inPoint || 0)))) : null;
        d.scaleKeyframed = scale ? !!tryGetSync(() => scale.param.isTimeVarying()) : null;
        targets.push({ desc: d, scale });
      }
      if (!targets.length) throw new BridgeError("NOT_FOUND", `No clip on V${params.videoTrack + 1} overlaps ${params.fromSeconds}–${to}s.`);
      const missing = targets.filter((t) => !t.scale).map((t) => t.desc.name);
      if (missing.length) throw new BridgeError("NOT_FOUND", `Could not find Motion > Scale on: ${missing.join(", ")}`);
      const keyed = targets.filter((t) => t.desc.scaleKeyframed).map((t) => t.desc.name);
      if (keyed.length) throw new BridgeError("KEYFRAMED", `Scale is keyframed on: ${keyed.join(", ")} — not changing it.`);

      if (!dryRun) {
        const label = params.undoLabel || `Agent: scale ${params.value}% on V${params.videoTrack + 1}`;
        core.runTransaction(project, label, () => targets.map((t) =>
          t.scale.param.createSetValueAction(t.scale.param.createKeyframe(params.value), true)), ctx);
      }
      return { dryRun, value: typeof params.value === "number" ? params.value : null, clips: targets.map((t) => t.desc) };
    },

    /*
     * Write effect params on the one clip under atSeconds (video track).
     * params: { videoTrack, atSeconds, undoLabel?, dryRun?,
     *           components: [{ matchName, occurrence = 1, params: [{ index, value?, timeBase?, keyframes?: [{ t, value }] }] }] }
     * `occurrence` is the n-th instance of matchName in chain order; missing instances are appended
     * (occurrence 2 on a clip without the effect creates two). A param with keyframes needs a
     * timeBase (media | clip | sequence) and becomes time-varying with exactly those keyframes; the
     * keyframes win over value. The WHOLE request is validated before the first transaction.
     * dryRun: validate the same way (missing effects become factory components, which commit nothing)
     * and return before the first transaction; replace_clip asks this before it removes a clip.
     */
    async set_params(params, ctx) {
      const { project, sequence } = await core.requireSequence(ctx);
      const hasV = typeof params.videoTrack === "number";
      const hasA = typeof params.audioTrack === "number";
      if (hasV === hasA) throw new BridgeError("BAD_PARAMS", "set_params needs exactly one of videoTrack or audioTrack.");
      const kind = hasA ? "audio" : "video";
      const trackIdx = hasA ? params.audioTrack : params.videoTrack;
      const tl = timeline.trackLabel(kind, trackIdx);
      const it = await timeline.itemAt(sequence, kind, trackIdx, params.atSeconds);
      const d = await timeline.describeItem(it);
      const label = params.undoLabel || `Agent: set params on ${tl} @${params.atSeconds}s`;
      const frameTicks = Number(await tryGet(() => sequence.getTimebase())) || 0;

      let chain = await tryGet(() => it.getComponentChain());
      if (!chain) throw new BridgeError("NOT_FOUND", `${tl} @${params.atSeconds}s has no component chain.`);
      const present = await effects.chainMatchNames(chain);

      const reqs = params.components.map((c) => ({
        matchName: c.matchName,
        occurrence: c.occurrence || 1,
        params: Array.isArray(c.params) ? c.params : [],
      }));

      // Instances to append: per match name, the highest occurrence asked for minus what the chain has.
      const need = {};
      const order = [];
      for (const r of reqs) {
        if (!core.hasOwn(need, r.matchName)) { need[r.matchName] = 0; order.push(r.matchName); }
        need[r.matchName] = Math.max(need[r.matchName], r.occurrence);
      }
      const toCreate = [];
      for (const m of order) {
        for (let i = effects.indexesOf(present, m).length; i < need[m]; i++) toCreate.push(m);
      }
      if (toCreate.length && kind === "audio") {
        throw new BridgeError("UNSUPPORTED", `Adding audio effects is not supported yet (${toCreate.join(", ")} is not on the clip); ` +
          "write params of components the audio clip already has (e.g. its Volume).");
      }
      if (toCreate.length > MAX_CREATED) {
        throw new BridgeError("BAD_PARAMS", `The request would add ${toCreate.length} effect instances (limit ${MAX_CREATED}); check occurrence.`);
      }
      // Factory components are plain objects until appended (no Action, nothing committed yet);
      // their param lists validate the request for effects the clip does not have yet.
      const created = [];
      for (const m of toCreate) {
        let comp = null;
        try { comp = await ppro.VideoFilterFactory.createComponent(m); } catch (e) { comp = null; }
        if (!comp) throw new BridgeError("NOT_FOUND", `Cannot add effect ${m} (unknown match name, or not an addable filter).`);
        created.push({ matchName: m, comp });
      }
      const instance = (m, occ) => {
        const idxs = effects.indexesOf(present, m);
        if (occ <= idxs.length) return { existing: true, comp: chain.getComponentAtIndex(idxs[occ - 1]) };
        return { existing: false, comp: created.filter((c) => c.matchName === m)[occ - idxs.length - 1].comp };
      };

      // Validate everything: param indexes, time bases, conversions, ranges, KEYFRAMED.
      const jobs = [];
      const seen = {};
      for (const r of reqs) {
        const inst = instance(r.matchName, r.occurrence);
        const count = tryGetSync(() => inst.comp.getParamCount(), 0) || 0;
        // A factory component may not list its params before it is appended (unverified on 27.1):
        // then its indexes are checked after the append, before any param is written.
        const deferIndex = !inst.existing && count === 0;
        for (const p of r.params) {
          const where = `${r.matchName}[${r.occurrence}]#${p.index}`;
          if (seen[where]) throw new BridgeError("BAD_PARAMS", `${where} is listed twice.`);
          seen[where] = true;
          const param = deferIndex ? null : (p.index < count ? tryGetSync(() => inst.comp.getParam(p.index)) : null);
          if (!param && !deferIndex) throw new BridgeError("NOT_FOUND", `${r.matchName} has no param #${p.index} (it has ${count}).`);
          const keyed = inst.existing && !!tryGetSync(() => param.isTimeVarying(), false);
          const job = { matchName: r.matchName, occurrence: r.occurrence, index: p.index, where,
                        name: param ? param.displayName : null };
          if (Array.isArray(p.keyframes) && p.keyframes.length) {
            if (typeof p.timeBase !== "string") {
              throw new BridgeError("BAD_PARAMS", `${where}: keyframes need a timeBase (media, clip or sequence).`);
            }
            if (keyed) throw new BridgeError("KEYFRAMED", `${where} is already keyframed; not adding keyframes to it.`);
            if (param && core.isFn(param, "areKeyframesSupported") && tryGetSync(() => param.areKeyframesSupported()) === false) {
              throw new BridgeError("UNSUPPORTED", `${where} does not support keyframes.`);
            }
            job.timeBase = p.timeBase;
            job.keyframes = p.keyframes.map((k, i) => {
              const conv = effects.convertKeyframeTime({
                timeBase: p.timeBase, t: k.t, start: d.start, inPoint: d.inPoint, outPoint: d.outPoint,
                speed: d.speed, frameTicks, where: `${where} keyframe ${i}`,
              });
              return { ticks: conv.mediaTicks, mediaSeconds: conv.mediaSeconds, value: effects.normalizeValue(k.value, `${where} keyframe ${i}`) };
            });
          } else {
            if (p.value === undefined || p.value === null) {
              throw new BridgeError("BAD_PARAMS", `${where}: needs a value or keyframes.`);
            }
            if (keyed) throw new BridgeError("KEYFRAMED", `${where} is keyframed; a static value would be hidden by its keyframes.`);
            job.value = effects.normalizeValue(p.value, where);
          }
          jobs.push(job);
        }
      }

      const keyframeReport = () => jobs.filter((j) => j.keyframes).map((j) => ({
        matchName: j.matchName, occurrence: j.occurrence, index: j.index, timeBase: j.timeBase,
        mediaSeconds: j.keyframes.map((k) => k.mediaSeconds),
      }));
      if (params.dryRun) {
        return { dryRun: true, set: jobs.map((j) => (j.name ? `${j.where} ${j.name}` : j.where)),
                 createdEffects: toCreate, keyframes: keyframeReport(), undoLabel: label };
      }

      // Commit: append missing instances (one undo step), then every param write (one undo step).
      if (created.length) {
        core.runTransaction(project, label + " [add effects]", () => created.map((c) => chain.createAppendComponentAction(c.comp)), ctx);
        chain = await it.getComponentChain();
      }
      const live = await effects.chainMatchNames(chain);
      // After an append, a failure here leaves the effects added but no param written: say so.
      const afterAppend = created.length ? { outcome: "partial" } : undefined;
      for (const job of jobs) {
        const idxs = effects.indexesOf(live, job.matchName);
        if (job.occurrence > idxs.length) {
          throw new BridgeError("NOT_FOUND", `${job.matchName}[${job.occurrence}] is not on the clip after adding effects.`, afterAppend);
        }
        const comp = chain.getComponentAtIndex(idxs[job.occurrence - 1]);
        const count = tryGetSync(() => comp.getParamCount(), 0) || 0;
        job.param = comp && job.index < count ? tryGetSync(() => comp.getParam(job.index)) : null;
        if (!job.param) {
          throw new BridgeError("NOT_FOUND", `${job.where} not found after adding effects (the effects were added; ` +
            "no param was written).", afterAppend);
        }
        if (job.keyframes && core.isFn(job.param, "areKeyframesSupported") && tryGetSync(() => job.param.areKeyframesSupported()) === false) {
          throw new BridgeError("UNSUPPORTED", `${job.where} does not support keyframes (the effects were added; no param was written).`, afterAppend);
        }
      }
      if (jobs.length) {
        core.runTransaction(project, label, () => {
          const actions = [];
          for (const job of jobs) {
            const param = job.param;
            if (job.keyframes) {
              actions.push(param.createSetTimeVaryingAction(true));
              for (const k of job.keyframes) {
                const kf = param.createKeyframe(effects.toParamValue(k.value));
                kf.position = effects.tickTimeFromTicks(k.ticks);
                actions.push(param.createAddKeyframeAction(kf));
              }
            } else {
              actions.push(param.createSetValueAction(param.createKeyframe(effects.toParamValue(job.value)), true));
            }
          }
          return actions;
        }, ctx);
      }
      return {
        set: jobs.map((j) => `${j.where} ${j.param.displayName}`),
        createdEffects: toCreate,
        keyframes: keyframeReport(),
        undoLabel: label,
      };
    },

    /*
     * Add a video transition to the start or end of the clip under atSeconds.
     * params: { videoTrack, atSeconds, matchName, durationSeconds, applyToStart,
     *           forceSingleSided = true, alignment?, undoLabel? }
     * `added` compares the track's transition count before and after: Premiere 27.1 keeps a
     * transition already at that edit point and adds nothing (19 Oct 2026). Transition items give no
     * name or times over UXP, so where and how long it is comes from export_otio (ops.add_transition).
     */
    async add_transition(params, ctx) {
      core.requireText(params, ["matchName"]);
      const { project, sequence } = await core.requireSequence(ctx);
      const track = await timeline.getTrack(sequence, "video", params.videoTrack);
      const it = await timeline.clipAt(sequence, params.videoTrack, params.atSeconds);
      const transition = ppro.TransitionFactory.createVideoTransition(params.matchName);
      if (!transition) throw new BridgeError("NOT_FOUND", `Unknown transition ${params.matchName}`);
      let opts = new ppro.AddTransitionOptions();
      opts = opts.setApplyToStart(!!params.applyToStart) || opts;
      opts = opts.setDuration(ppro.TickTime.createWithSeconds(params.durationSeconds)) || opts;
      opts = opts.setForceSingleSided(params.forceSingleSided !== false) || opts;
      if (typeof params.alignment === "number") opts = opts.setTransitionAlignment(params.alignment) || opts;
      const label = params.undoLabel ||
        `Agent: ${params.matchName} at ${params.applyToStart ? "start" : "end"} of V${params.videoTrack + 1} @${params.atSeconds}s`;
      const before = (await timeline.transitionItems(track)).length;
      core.runTransaction(project, label, () => it.createAddVideoTransitionAction(transition, opts), ctx);
      const after = (await timeline.transitionItems(track)).length;
      return { added: after > before, matchName: params.matchName, applyToStart: !!params.applyToStart,
               durationSeconds: params.durationSeconds, transitionCount: { before, after }, undoLabel: label };
    },
  };
};
