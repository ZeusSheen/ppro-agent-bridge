/*
 * handlers_markers: sequence markers (list, remove), the project's sequences (list, clone as a restore point) and
 * saving the project. Writes go through core.runTransaction with a labelled undo step, except the two calls Premiere
 * only offers directly: Project#deleteSequence (pruning old restore points) and Project#save.
 *
 * Restore points: a clone of a sequence, renamed "<name> [restore YYYY-MM-DD HH:MM]" and tagged with a persistent
 * sequence custom property (RESTORE_KEY, a JSON record). The record names the clone's OWN guid and the name the
 * bridge gave it, so a duplicate of a restore point (its guid differs) does not count as one, and one the user
 * renamed is reported but never pruned. Calls as live-verified on 27.1 (smoke.js "sequences" / "properties"):
 * Sequence#createCloneAction (the copy is "<name> 副本" on a Chinese UI and is not made active), the new sequence found
 * by a before/after guid diff of Project#getSequences, ProjectItem#createSetNameAction on Sequence#getProjectItem,
 * Properties#createSetValueAction(key, string, PERSISTENT), Project#deleteSequence (no dialog).
 * Factory module returning {actionName: async function (params, ctx)}.
 * deps: { ppro, core }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const BridgeError = core.BridgeError;
  const secs = core.secs;
  const tryGet = core.tryGet;
  const tryGetSync = core.tryGetSync;

  const RESTORE_KEY = "AgentBridge.restorePoint";
  const DEFAULT_KEEP = 5;
  const NOT_AN_UNDO_STEP = "Project#deleteSequence is a direct call, not a labelled undo step: pruned restore points " +
    "may not come back with Edit > Undo.";

  const round3 = (s) => Math.round(s * 1000) / 1000;
  const verifyFailed = (message, data) => new BridgeError("VERIFY_FAILED", message, { outcome: "committed", data });

  function persistent() {
    const t = ppro.Constants && ppro.Constants.PropertyType;
    return t && t.PERSISTENT !== undefined ? t.PERSISTENT : 1;
  }

  /* ---------- markers ---------- */

  /* The sequence's Markers object and its markers, read strictly: a failed read is an error, never "no markers". */
  async function readMarkers(sequence) {
    try {
      const markers = await ppro.Markers.getMarkers(sequence);
      return { markers, list: markers.getMarkers() || [] };
    } catch (e) {
      throw new BridgeError("INTERNAL", `[read] could not read the sequence's markers: ${core.errorMessage(e)}`);
    }
  }

  /* One marker as JSON (the getters are synchronous on 27.1); times are sequence seconds. */
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

  /* Half a frame of the sequence (the tolerance for a marker's start), or 1 ms where the timebase is unreadable. */
  async function halfFrame(sequence) {
    const n = Number(await tryGet(() => sequence.getTimebase()));
    return Number.isFinite(n) && n > 0 ? n / core.TICKS_PER_SECOND / 2 : core.EPS;
  }

  const brief = (d) => ({ guid: d.guid, name: d.name, start: d.start });

  /* ---------- sequences and restore points ---------- */

  /* Every sequence of the project, read strictly: [{seq, name, guid}] in Premiere's order. */
  async function sequencesOf(project) {
    if (!core.isFn(project, "getSequences")) {
      throw new BridgeError("UNSUPPORTED", "This Premiere build cannot list the project's sequences over UXP.");
    }
    let list;
    try {
      list = (await project.getSequences()) || [];
    } catch (e) {
      throw new BridgeError("INTERNAL", `[read] could not list the project's sequences: ${core.errorMessage(e)}`);
    }
    return list.map((s) => ({ seq: s, name: tryGetSync(() => s.name), guid: core.readGuid(s) }));
  }

  /*
   * The restore-point record on a sequence, or null: no tag, an unreadable one, or one that names another guid (a
   * duplicate of a restore point carries its original's record). A read that fails counts as "not a restore point",
   * which only ever keeps a sequence from being pruned.
   * -> { sourceGuid, sourceName, created (ISO time), renamed (the name is no longer the one the bridge gave it) }
   */
  async function restoreTag(entry) {
    if (!entry.guid || !ppro.Properties || !core.isFn(ppro.Properties, "getProperties")) return null;
    const props = await tryGet(() => ppro.Properties.getProperties(entry.seq));
    if (!props || !(await tryGet(() => props.hasValue(RESTORE_KEY), false))) return null;
    const raw = await tryGet(() => props.getValue(RESTORE_KEY));
    let tag = null;
    try { tag = typeof raw === "string" ? JSON.parse(raw) : null; } catch (e) { tag = null; }
    if (!core.isPlainObject(tag) || tag.guid !== entry.guid || typeof tag.sourceGuid !== "string") return null;
    return {
      sourceGuid: tag.sourceGuid,
      sourceName: typeof tag.sourceName === "string" ? tag.sourceName : null,
      created: typeof tag.created === "string" ? tag.created : "",
      renamed: tag.name !== entry.name,
    };
  }

  /* Local date and time for a restore point's name: "YYYY-MM-DD HH:MM" and the same with seconds. */
  function stamps(d) {
    const p = (n) => (n < 10 ? "0" : "") + n;
    const minute = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    return { minute, second: `${minute}:${p(d.getSeconds())}` };
  }

  return {
    /*
     * The active sequence's markers in start order: guid, name, comments, start / duration / end (sequence s), type,
     * colorIndex.   params: {}
     */
    async list_markers(params, ctx) {
      const { sequence } = await core.requireSequence(ctx);
      const { list } = await readMarkers(sequence);
      const markers = list.map(describeMarker).sort((a, b) => (a.start || 0) - (b.start || 0));
      return { sequence: sequence.name, count: markers.length, markers };
    },

    /*
     * Remove ONE sequence marker, by guid or by exact name; atSeconds (the marker's start, half a frame of tolerance)
     * narrows a name several markers share. No match (NOT_FOUND) or several (AMBIGUOUS) is refused before anything
     * changes. One undo step; read back afterwards (the marker must be gone: else VERIFY_FAILED, committed).
     * params: { guid?, name?, atSeconds?, dryRun?, undoLabel? }
     */
    async remove_marker(params, ctx) {
      const { project, sequence } = await core.requireSequence(ctx);
      const { markers, list } = await readMarkers(sequence);
      const tol = await halfFrame(sequence);
      const all = list.map((m) => ({ m, d: describeMarker(m) })).sort((a, b) => (a.d.start || 0) - (b.d.start || 0));
      let hits = all;
      if (params.guid) hits = hits.filter((h) => h.d.guid === params.guid);
      if (typeof params.name === "string") hits = hits.filter((h) => h.d.name === params.name);
      if (typeof params.atSeconds === "number") {
        hits = hits.filter((h) => h.d.start !== null && Math.abs(h.d.start - params.atSeconds) <= tol);
      }
      const what = [params.guid ? `guid ${params.guid}` : null, typeof params.name === "string" ? `named "${params.name}"` : null,
        typeof params.atSeconds === "number" ? `at ${params.atSeconds}s` : null].filter(Boolean).join(", ");
      if (!hits.length) {
        throw new BridgeError("NOT_FOUND", `No marker ${what} on "${sequence.name}" (${all.length} marker(s)).`,
          { data: { markers: all.slice(0, 50).map((h) => brief(h.d)) } });
      }
      if (hits.length > 1) {
        throw new BridgeError("AMBIGUOUS", `${hits.length} markers match (${what}): at ` +
          hits.map((h) => `${round3(h.d.start)}s`).join(", ") + ". Pass atSeconds or guid; nothing was removed.",
          { data: { matches: hits.map((h) => brief(h.d)) } });
      }
      const target = hits[0].d;
      if (params.dryRun) return { dryRun: true, removed: [target], markerCount: { before: all.length } };

      const label = params.undoLabel || `Agent: remove marker "${target.name}" @${round3(target.start)}s`;
      core.runTransaction(project, label, () => markers.createRemoveMarkerAction(hits[0].m), ctx);
      let after;
      try {
        after = (await readMarkers(sequence)).list.map(describeMarker);
      } catch (e) {
        throw verifyFailed(`${core.errorMessage(e)} after removing "${target.name}"; whether it is gone is unknown.`, { removed: [target] });
      }
      // By guid where the build has one; else one fewer marker with that name and start.
      const same = (d) => d.name === target.name && d.start === target.start;
      const still = target.guid ? after.some((d) => d.guid === target.guid)
        : after.filter(same).length >= all.filter((h) => same(h.d)).length;
      if (still) {
        throw verifyFailed(`Premiere still lists the marker "${target.name}" at ${round3(target.start)}s after the remove transaction.`,
          { removed: [], target, undoLabel: label });
      }
      return { dryRun: false, removed: [target], markerCount: { before: all.length, after: after.length }, verified: true, undoLabel: label };
    },

    /*
     * Every sequence of the active project: name, guid, active, and restorePoint (the bridge's record when it is a
     * restore point made by clone_sequence, else null).   params: {}
     */
    async list_sequences(params, ctx) {
      const project = await core.requireProject(ctx);
      const all = await sequencesOf(project);
      const active = await tryGet(() => project.getActiveSequence());
      const activeGuid = core.readGuid(active);
      const sequences = [];
      for (const e of all) {
        const isActive = !!active && (e.seq === active || (!!e.guid && e.guid === activeGuid));
        sequences.push({ name: e.name, guid: e.guid, active: isActive, restorePoint: await restoreTag(e) });
      }
      return { activeSequence: active ? tryGetSync(() => active.name) : null, count: sequences.length, sequences };
    },

    /*
     * A restore point before a risky edit: clone the active sequence (or the one named by guid / name), rename the clone
     * "<name> [restore YYYY-MM-DD HH:MM]" and tag it (two undo steps: [1/2 clone], [2/2 name + tag]); it is not made
     * active. Then delete the oldest restore points of that same source beyond `keep` (the new one counts) with
     * Project#deleteSequence: only sequences tagged for that source, never the active one or one the user renamed.
     * dryRun: the clone's name and what pruning would delete. A clone that cannot be found, named or tagged
     * afterwards is VERIFY_FAILED (committed): nothing is pruned then.
     * params: { name?, guid?, keep = 5, dryRun?, undoLabel? }
     */
    async clone_sequence(params, ctx) {
      const project = await core.requireProject(ctx);
      const before = await sequencesOf(project);
      if (before.some((e) => !e.guid)) {
        throw new BridgeError("UNSUPPORTED", "Sequence guids are unreadable on this build, so the clone could not be told " +
          "apart from the other sequences; nothing was cloned.");
      }
      const active = await tryGet(() => project.getActiveSequence());
      const activeGuid = core.readGuid(active);
      const isActive = (e) => !!active && (e.seq === active || e.guid === activeGuid);
      let source;
      if (params.guid) {
        source = before.find((e) => e.guid === params.guid);
      } else if (params.name) {
        const named = before.filter((e) => e.name === params.name);
        if (named.length > 1) {
          throw new BridgeError("AMBIGUOUS", `${named.length} sequences are named "${params.name}": pass guid.`,
            { data: { sequences: named.map((e) => ({ name: e.name, guid: e.guid })) } });
        }
        source = named[0];
      } else {
        if (!active) throw new BridgeError("NO_SEQUENCE", "Project has no active sequence: name the one to clone.");
        source = before.find(isActive);
      }
      const plainName = typeof params.asName === "string" ? params.asName.trim() : "";
      if (plainName && before.some((e) => e.name === plainName)) {
        throw new BridgeError("PRECONDITION_FAILED", `A sequence named "${plainName}" already exists; nothing was cloned.`);
      }
      if (!source) {
        throw new BridgeError("NOT_FOUND", `No sequence ${params.guid ? "with guid " + params.guid : '"' + (params.name || "active") + '"'} in this project.`,
          { data: { sequences: before.map((e) => e.name) } });
      }
      if (!core.isFn(source.seq, "createCloneAction")) {
        throw new BridgeError("UNSUPPORTED", "This Premiere build cannot clone a sequence over UXP (Sequence#createCloneAction).");
      }
      if (!core.isFn(project, "deleteSequence") || !ppro.Properties || !core.isFn(ppro.Properties, "getProperties")) {
        throw new BridgeError("UNSUPPORTED", "This Premiere build lacks Project#deleteSequence or sequence custom " +
          "properties, which restore points need; nothing was cloned.");
      }

      // Restore points of this source, oldest first; the active one and renamed ones are never pruned.
      const keep = typeof params.keep === "number" ? params.keep : DEFAULT_KEEP;
      const points = [];
      for (const e of before) {
        const tag = await restoreTag(e);
        if (tag && tag.sourceGuid === source.guid && !tag.renamed && !isActive(e)) points.push({ e, tag });
      }
      points.sort((a, b) => (a.tag.created < b.tag.created ? -1 : a.tag.created > b.tag.created ? 1 : 0));
      const doomed = plainName ? [] : points.slice(0, Math.max(0, points.length + 1 - keep));
      const prune = doomed.map((p) => ({ name: p.e.name, guid: p.e.guid, created: p.tag.created }));

      const now = new Date();
      const st = stamps(now);
      const taken = new Set(before.map((e) => e.name));
      let cloneName = plainName || `${source.name} [restore ${st.minute}]`;
      if (taken.has(cloneName)) cloneName = `${source.name} [restore ${st.second}]`;
      for (let n = 2; taken.has(cloneName); n++) cloneName = `${source.name} [restore ${st.second} #${n}]`;

      const label = params.undoLabel || (plainName ? `Agent: copy of "${source.name}"` : `Agent: restore point of "${source.name}"`);
      const steps = [`${label} [1/2 clone]`, plainName ? `${label} [2/2 name]` : `${label} [2/2 name + tag]`];
      const out = { dryRun: !!params.dryRun, source: { name: source.name, guid: source.guid }, cloneName, keep, prune, undoSteps: steps };
      if (params.dryRun) {
        if (prune.length) out.notes = NOT_AN_UNDO_STEP;
        return out;
      }

      core.runTransaction(project, steps[0], () => source.seq.createCloneAction(), ctx);
      let fresh;
      try {
        const after = await sequencesOf(project);
        fresh = after.filter((e) => !before.some((b) => b.guid === e.guid));
      } catch (e) {
        throw verifyFailed(`${core.errorMessage(e)} after cloning "${source.name}"; look for the copy in the Project panel. ` +
          "Nothing was renamed or pruned.", { completed: [steps[0]] });
      }
      if (fresh.length !== 1) {
        throw verifyFailed(`The clone of "${source.name}" could not be found afterwards (${fresh.length} new sequences` +
          (fresh.length ? `: ${fresh.map((e) => `"${e.name}"`).join(", ")}` : "") + "). Look in the Project panel (Premiere " +
          "names the copy in its UI language). Nothing was renamed or pruned.",
          { completed: [steps[0]], newSequences: fresh.map((e) => ({ name: e.name, guid: e.guid })) });
      }
      const clone = fresh[0];
      const premiereName = clone.name;
      const tag = { v: 1, guid: clone.guid, name: cloneName, sourceGuid: source.guid, sourceName: source.name, created: now.toISOString() };
      try {
        const item = await clone.seq.getProjectItem();
        const props = plainName ? null : await ppro.Properties.getProperties(clone.seq);
        if (!item || (!plainName && !props)) throw new BridgeError("INTERNAL", "the clone's project item or custom properties are unreadable");
        core.runTransaction(project, steps[1], () => (plainName ? [item.createSetNameAction(cloneName)] : [
          item.createSetNameAction(cloneName),
          props.createSetValueAction(RESTORE_KEY, JSON.stringify(tag), persistent()),
        ]), ctx);
      } catch (e) {
        throw new BridgeError(core.codeOf(e), `[name + tag] ${core.errorMessage(e)}. The clone "${premiereName}" ` +
          `(${clone.guid}) exists but is not named or tagged as a restore point, so it will never be pruned.`,
          { outcome: "partial", data: { completed: [steps[0]], clone: { name: premiereName, guid: clone.guid } } });
      }

      let back = null;
      let readError = null;
      try {
        back = (await sequencesOf(project)).find((e) => e.guid === clone.guid) || null;
      } catch (e) {
        readError = core.errorMessage(e);
      }
      const backTag = back && !plainName ? await restoreTag(back) : null;
      const problems = [];
      if (readError) problems.push(readError);
      else if (!back) problems.push("it is gone from the sequence list");
      else {
        if (back.name !== cloneName) problems.push(`its name reads "${back.name}"`);
        if (!plainName && (!backTag || backTag.sourceGuid !== source.guid)) problems.push("its restore-point tag does not read back");
      }
      if (problems.length) {
        throw verifyFailed(`Restore point of "${source.name}" cloned (${clone.guid}), but ${problems.join(" and ")}. ` +
          "Nothing was pruned.", { completed: steps, clone: { name: back ? back.name : premiereName, guid: clone.guid } });
      }
      out.clone = { name: cloneName, guid: clone.guid, premiereName };
      out.verified = true;
      out.undoLabel = label;

      // Pruning: each doomed restore point is checked again right before it goes (still there, still tagged for this
      // source, not renamed, not active), then deleted; one read-back at the end says which ones went.
      out.pruned = [];
      if (doomed.length) {
        out.notes = NOT_AN_UNDO_STEP;
        const activeNow = core.readGuid(await tryGet(() => project.getActiveSequence()));
        const failed = [];
        let current = [];
        try { current = await sequencesOf(project); } catch (e) { current = null; }
        const attempted = [];
        for (const p of doomed) {
          const d = { name: p.e.name, guid: p.e.guid, created: p.tag.created };
          const e = current ? current.find((x) => x.guid === p.e.guid) : null;
          const tagNow = e ? await restoreTag(e) : null;
          if (!e || !tagNow || tagNow.renamed || tagNow.sourceGuid !== source.guid || e.guid === activeNow) {
            failed.push(Object.assign(d, { reason: current ? "no longer a prunable restore point of this sequence" : "the sequence list was unreadable" }));
            continue;
          }
          ctx.sideEffect();
          try {
            if ((await project.deleteSequence(e.seq)) === false) d.reason = "deleteSequence returned false";
          } catch (err) {
            d.reason = core.errorMessage(err);
          }
          attempted.push(d);
        }
        let left = null;
        try { left = new Set((await sequencesOf(project)).map((e) => e.guid)); } catch (e) { left = null; }
        for (const d of attempted) {
          if (left && !left.has(d.guid)) { delete d.reason; out.pruned.push(d); } else {
            failed.push(Object.assign(d, { reason: d.reason || (left ? "still in the project after deleteSequence" : "could not read back") }));
          }
        }
        if (failed.length) {
          out.pruneFailed = failed;
          out.warning = `${failed.length} old restore point(s) were not deleted (pruneFailed); the new one is in place.`;
        }
      }
      return out;
    },

    /*
     * Save the active project (Project#save: a direct call, not an undo step). The CLI compares the file's time stamp
     * before and after.   params: {}
     */
    async save_project(params, ctx) {
      const project = await core.requireProject(ctx);
      if (!core.isFn(project, "save")) throw new BridgeError("UNSUPPORTED", "This Premiere build cannot save the project over UXP.");
      const path = tryGetSync(() => project.path);
      const name = tryGetSync(() => project.name);
      ctx.sideEffect();
      const ok = await project.save();
      if (ok === false) {
        throw new BridgeError("TRANSACTION_FAILED", `Project#save returned false for "${name}"; the file may not have been written.`);
      }
      return { saved: true, name, path: path === null ? null : String(path) };
    },
  };
};
