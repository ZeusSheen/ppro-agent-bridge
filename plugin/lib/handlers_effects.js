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
  const MAX_CREATED = 16; // effect instances one set_params request may add (guards a typo in occurrence)
  // A graphic gets its layer components about a second after it is placed (Basic Title on 27.1: 0.9 s). An effect
  // added before then ends up after them once they arrive, where it does nothing: set_params waits for them, this long
  // at most (then it appends and says so: a graphic may have no layers at all).
  const GRAPHIC_LAYERS_WAIT_MS = 2000;
  const GRAPHIC_LAYERS_POLL_MS = 100;

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
      for (const it of await timeline.clipItems(track, `V${params.videoTrack + 1}`)) {
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
     *           components: [{ matchName, occurrence = 1, params: [{ index, value?, timeBase?, keyframes?: [{ t, value }],
     *                                                                interpolation? }] }] }
     * `occurrence` is the n-th instance of matchName in chain order; missing instances are added in request
     * order (occurrence 2 on a clip without the effect creates two): appended, or on a graphic right before its first
     * layer component (effects.insertionIndex; a graphic just placed gets up to GRAPHIC_LAYERS_WAIT_MS for its layers:
     * layerWait, and warning when none came), and listed with their chain index in createdComponents (read back;
     * dryRun: planned). Audio effects are never added here. A param with keyframes
     * needs a timeBase (media | clip | sequence) and becomes time-varying with exactly those keyframes; the
     * keyframes win over value. `interpolation` (effects.interpolationMode) is the temporal interpolation
     * of the keyframes given (one more undo step, "[interpolation]"), or alone, of every keyframe the param
     * already has; the reply samples the curve it gives (effects.curveSamples).
     * The WHOLE request is validated before the first transaction.
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
      let present = await effects.chainMatchNames(chain);

      const reqs = params.components.map((c) => ({
        matchName: c.matchName,
        occurrence: c.occurrence || 1,
        params: Array.isArray(c.params) ? c.params : [],
      }));

      // Instances to add, in request order: each component up to its occurrence, counting what the chain and the
      // components before it in the request have of that match name ([A 1, B 1, A 2] adds A, B, A).
      const planCreate = () => {
        const out = [];
        const have = {};
        for (const r of reqs) {
          if (!core.hasOwn(have, r.matchName)) have[r.matchName] = effects.indexesOf(present, r.matchName).length;
          for (; have[r.matchName] < r.occurrence; have[r.matchName]++) out.push(r.matchName);
        }
        return out;
      };
      let toCreate = planCreate();
      if (toCreate.length && kind === "audio") {
        throw new BridgeError("UNSUPPORTED", `set_params does not add audio effects (${toCreate.join(", ")} is not on the clip): ` +
          "add it with add_audio_effect (by display name) first, then write its params.");
      }
      if (toCreate.length > MAX_CREATED) {
        throw new BridgeError("BAD_PARAMS", `The request would add ${toCreate.length} effect instances (limit ${MAX_CREATED}); check occurrence.`);
      }
      // On a graphic, new effects go right before its first layer component (effects.insertionIndex), which needs the
      // factory list to tell layers from effects, and the layers themselves: on a graphic just placed, wait for them.
      let place = { index: present.length, tail: [], graphic: false, layers: 0 };
      let layerWait = null;
      if (toCreate.length) {
        const listed = await tryGet(() => ppro.VideoFilterFactory.getMatchNames(), null);
        const addable = Array.isArray(listed) ? listed.map(String) : null;
        const locate = () => effects.insertionIndex(present, addable ? addable.concat(toCreate) : null);
        place = locate();
        if (place.graphic && !addable) {
          throw new BridgeError("UNSUPPORTED", `${tl} @${params.atSeconds}s is a graphic, and VideoFilterFactory.getMatchNames ` +
            "could not be read to tell its layer components from its effects: a new effect could land after the layers, " +
            "where it does nothing. Nothing was changed.");
        }
        if (place.graphic && !place.layers) {
          const t0 = Date.now();
          while (!place.layers && Date.now() - t0 < GRAPHIC_LAYERS_WAIT_MS) {
            await core.sleep(GRAPHIC_LAYERS_POLL_MS);
            chain = (await tryGet(() => it.getComponentChain())) || chain;
            present = await effects.chainMatchNames(chain);
            place = locate();
          }
          layerWait = { seconds: Math.round((Date.now() - t0) / 100) / 10, layers: place.layers };
          toCreate = planCreate();
          place = locate();
        }
        // An instance must follow the ones of its match name (occurrence order): with one already after the layers, a
        // new one would go there too, where it does nothing.
        const behind = toCreate.filter((m, i) => toCreate.indexOf(m) === i &&
          effects.indexesOf(present, m).some((k) => k >= place.index && place.tail.length));
        if (behind.length) {
          throw new BridgeError("UNSUPPORTED", `${tl} @${params.atSeconds}s has ${behind.join(", ")} after the graphic's layer ` +
            `components (chain ${present.join(", ")}), where it does nothing, and a new instance would have to follow it. ` +
            "Remove it (remove_effect), then add the effects again. Nothing was changed.", { data: { chain: present } });
        }
        if (place.tail.length && !core.isFn(chain, "createInsertComponentAction")) {
          throw new BridgeError("UNSUPPORTED", `${tl} @${params.atSeconds}s ends in ${place.tail.join(", ")} (a graphic's ` +
            "layers), and this Premiere build cannot insert an effect before them (no createInsertComponentAction): " +
            "appended after them it would not act on the graphic. Nothing was changed.");
        }
      }
      // A graphic still without layers after the wait gets the effects appended (a graphic may have none); the reply
      // says so, since layers Premiere adds later would go before them.
      const withLayerWait = (reply) => {
        if (!layerWait) return reply;
        reply.layerWait = layerWait;
        if (!layerWait.layers) {
          reply.warning = `${tl} @${params.atSeconds}s is a graphic without layer components after ${layerWait.seconds} s: ` +
            "the effects were appended. Layers Premiere adds later go before them, and then the effects do nothing: " +
            "check the chain with inspect_items.";
        }
        return reply;
      };
      // Factory components are plain objects until added (no Action, nothing committed yet);
      // their param lists validate the request for effects the clip does not have yet.
      const created = [];
      for (const m of toCreate) {
        let comp = null;
        try { comp = await ppro.VideoFilterFactory.createComponent(m); } catch (e) { comp = null; }
        if (!comp) throw new BridgeError("NOT_FOUND", `Cannot add effect ${m} (unknown match name, or not an addable filter).`);
        created.push({ matchName: m, comp });
      }
      // At consecutive indexes in one transaction: its actions run in order (27.1: [blur 2, tint] landed at 4, 5).
      const seenCreated = {};
      const plannedComponents = created.map((c, k) => {
        seenCreated[c.matchName] = (seenCreated[c.matchName] || 0) + 1;
        return { matchName: c.matchName, occurrence: effects.indexesOf(present, c.matchName).length + seenCreated[c.matchName],
                 index: place.index + k };
      });
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
          // Premiere arms some params with animation on and no keyframes (every param of an added audio effect such as
          // 降噪, live 27.1): nothing would hide a value there, so they are not KEYFRAMED, and a static value switches
          // their animation off first. A key list that cannot be read counts as keyed: never write over keys unseen.
          const varying = inst.existing && !!tryGetSync(() => param.isTimeVarying(), false);
          const keyList = varying ? tryGetSync(() => param.getKeyframeListAsTickTimes(), null) : [];
          const keyed = varying && !(Array.isArray(keyList) && keyList.length === 0);
          const armed = varying && !keyed;
          const job = { matchName: r.matchName, occurrence: r.occurrence, index: p.index, where,
                        name: param ? param.displayName : null };
          const interp = p.interpolation === undefined || p.interpolation === null ? null
            : effects.interpolationMode(p.interpolation, where);
          if (interp && !(Array.isArray(p.keyframes) && p.keyframes.length) && (p.value === undefined || p.value === null)) {
            // interpolation only: the keyframes the param already has
            if (!keyed) throw new BridgeError("BAD_PARAMS", `${where} is not keyframed; interpolation applies to keyframes.`);
            job.interpolation = interp;
            job.interpolationOnly = true;
          } else if (Array.isArray(p.keyframes) && p.keyframes.length) {
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
            job.interpolation = interp;
          } else {
            if (p.value === undefined || p.value === null) {
              throw new BridgeError("BAD_PARAMS", `${where}: needs a value, keyframes or an interpolation.`);
            }
            if (interp) throw new BridgeError("BAD_PARAMS", `${where}: interpolation applies to keyframes, not to a static value.`);
            if (keyed) throw new BridgeError("KEYFRAMED", `${where} is keyframed; a static value would be hidden by its keyframes.`);
            job.value = effects.normalizeValue(p.value, where);
            job.disarm = armed;
          }
          jobs.push(job);
        }
      }

      const keyframeReport = () => jobs.filter((j) => j.keyframes).map((j) => ({
        matchName: j.matchName, occurrence: j.occurrence, index: j.index, timeBase: j.timeBase,
        mediaSeconds: j.keyframes.map((k) => k.mediaSeconds),
      }));
      const interpJobs = jobs.filter((j) => j.interpolation);
      const interpReport = () => interpJobs.map((j) => {
        const out = { matchName: j.matchName, occurrence: j.occurrence, index: j.index,
                      mode: j.interpolation.mode, name: j.interpolation.name };
        if (j.samples) { out.keyframeCount = j.keyframeCount; out.samples = j.samples; }
        return out;
      });
      const withInterp = (reply) => (interpJobs.length ? Object.assign(reply, { interpolation: interpReport() }) : reply);
      if (params.dryRun) {
        return withLayerWait(withInterp({ dryRun: true, set: jobs.map((j) => (j.name ? `${j.where} ${j.name}` : j.where)),
                                          createdEffects: toCreate, createdComponents: plannedComponents,
                                          keyframes: keyframeReport(), undoLabel: label }));
      }

      // Commit: add missing instances (one undo step), then every param write (one undo step).
      if (created.length) {
        core.runTransaction(project, label + " [add effects]", () => created.map((c, k) => (place.tail.length
          ? chain.createInsertComponentAction(c.comp, place.index + k) : chain.createAppendComponentAction(c.comp))), ctx);
        chain = await it.getComponentChain();
      }
      const live = await effects.chainMatchNames(chain);
      // Where the new instances are now (read back); plannedIndex only where Premiere put one elsewhere.
      const createdComponents = plannedComponents.map((c) => {
        const at = effects.indexesOf(live, c.matchName)[c.occurrence - 1];
        const got = typeof at === "number" ? at : null;
        return got === c.index ? c : Object.assign({}, c, { index: got, plannedIndex: c.index });
      });
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
        if (job.interpolationOnly) job.times = tryGetSync(() => job.param.getKeyframeListAsTickTimes(), []) || [];
      }
      if (jobs.length) {
        core.runTransaction(project, label, () => {
          const actions = [];
          for (const job of jobs) {
            const param = job.param;
            if (job.interpolationOnly) {
              for (const t of job.times) actions.push(param.createSetInterpolationAtKeyframeAction(t, job.interpolation.mode, false));
            } else if (job.keyframes) {
              actions.push(param.createSetTimeVaryingAction(true));
              for (const k of job.keyframes) {
                const kf = param.createKeyframe(effects.toParamValue(k.value));
                kf.position = effects.tickTimeFromTicks(k.ticks);
                if (job.interpolation && core.isFn(kf, "setTemporalInterpolationMode")) kf.setTemporalInterpolationMode(job.interpolation.mode);
                actions.push(param.createAddKeyframeAction(kf));
              }
            } else {
              if (job.disarm) actions.push(param.createSetTimeVaryingAction(false));
              actions.push(param.createSetValueAction(param.createKeyframe(effects.toParamValue(job.value)), true));
            }
          }
          return actions;
        }, ctx);
      }
      // Interpolation of keyframes added above: set on the keys once they exist (one more undo step), the call
      // api_smoke verified on 27.1; whether a mode given to the Keyframe before adding it sticks is unmeasured.
      const onNew = interpJobs.filter((j) => j.keyframes);
      if (onNew.length) {
        const keys = onNew.map((j) => ({ job: j, times: tryGetSync(() => j.param.getKeyframeListAsTickTimes(), []) || [] }));
        core.runTransaction(project, label + " [interpolation]", () => keys.reduce((acc, k) => acc.concat(k.times.map((t) =>
          k.job.param.createSetInterpolationAtKeyframeAction(t, k.job.interpolation.mode, false))), []), ctx);
      }
      for (const j of interpJobs) {
        j.keyframeCount = (tryGetSync(() => j.param.getKeyframeListAsTickTimes(), []) || []).length;
        j.samples = await effects.curveSamples(j.param);
      }
      return withLayerWait(withInterp({
        set: jobs.map((j) => `${j.where} ${j.param.displayName}`),
        createdEffects: toCreate,
        createdComponents,
        keyframes: keyframeReport(),
        undoLabel: label,
      }));
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
      const tl = `V${params.videoTrack + 1}`;
      const before = (await timeline.transitionItems(track, tl)).length;  // strict: a failed read must not count as 0
      core.runTransaction(project, label, () => it.createAddVideoTransitionAction(transition, opts), ctx);
      const after = (await timeline.transitionItems(track, tl)).length;
      return { added: after > before, matchName: params.matchName, applyToStart: !!params.applyToStart,
               durationSeconds: params.durationSeconds, transitionCount: { before, after }, undoLabel: label };
    },
  };
};
