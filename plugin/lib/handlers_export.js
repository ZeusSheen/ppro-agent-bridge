/*
 * handlers_export: what Premiere can add to a clip — the effect lists (list_effects) and audio effects
 * added by their localised display name (add_audio_effect). Rendering is export_sequence (handlers_read);
 * the CLI's list_presets / export_video find the .epr presets on disk and check the rendered file.
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

  const MAX_CLOSE = 8;        // close names listed when a name is unknown
  const SPOT_CHECKS = 3;      // video filters created to check that match and display names pair up
  // A match name, not a display name: a GUID (Premiere's audio effects, e.g. "c6762838-e451-...") or "Vendor.Name".
  const MATCH_NAME = /^([0-9a-f]{8}-[0-9a-f]{4}-|[A-Za-z]+\.\S)/i;

  /* ---------- names ---------- */

  const norm = (s) => String(s).toLowerCase().replace(/\s+/g, " ").trim();

  function bigrams(s) {
    const t = norm(s).replace(/ /g, "");
    if (t.length < 2) return t ? [t] : [];
    const out = [];
    for (let i = 0; i < t.length - 1; i++) out.push(t.slice(i, i + 2));
    return out;
  }

  /* 1 for the same name, 0.9 when one contains the other ("降噪" in "自适应降噪"), else the bigram Dice coefficient.
   * broker/fake_panel.py's similarity is the same function. */
  function similarity(a, b) {
    const x = norm(a);
    const y = norm(b);
    if (!x || !y) return 0;
    if (x === y) return 1;
    if (x.indexOf(y) >= 0 || y.indexOf(x) >= 0) return 0.9;
    const bx = bigrams(x);
    const by = bigrams(y);
    const pool = by.slice();
    let common = 0;
    for (const g of bx) {
      const i = pool.indexOf(g);
      if (i >= 0) { common += 1; pool.splice(i, 1); }
    }
    return bx.length + by.length ? (2 * common) / (bx.length + by.length) : 0;
  }

  function closeNames(query, names) {
    return names.map((n, i) => ({ n, i, s: similarity(query, n) }))
      .filter((c) => c.s >= 0.34)
      .sort((a, b) => b.s - a.s || a.i - b.i)
      .slice(0, MAX_CLOSE)
      .map((c) => c.n);
  }

  async function nameList(factory, fn, what) {
    if (!core.isFn(factory, fn)) {
      throw new BridgeError("UNSUPPORTED", `${what}.${fn} is not available on this Premiere build.`);
    }
    const list = await factory[fn]();
    return Array.isArray(list) ? list.map(String) : Array.from(list || [], String);
  }

  /*
   * The display name `query` stands for: exact, then ignoring case (unique), then — for something shaped like a
   * match name — the effect whose factory component has that match name (each listed effect is created for the
   * clip, which commits nothing). -> { displayName, matchedBy, comp? }
   */
  async function resolveAudioName(query, names, trackItem) {
    if (names.indexOf(query) >= 0) return { displayName: query, matchedBy: "displayName" };
    const loose = names.filter((n, i) => norm(n) === norm(query) && names.indexOf(n) === i);
    if (loose.length === 1) return { displayName: loose[0], matchedBy: "displayNameIgnoringCase" };
    if (loose.length > 1) {
      throw new BridgeError("AMBIGUOUS", `"${query}" matches ${loose.length} audio effects when case is ignored: ` +
        `${loose.join(", ")}. Give the exact name; nothing was changed.`, { data: { matches: loose } });
    }
    let readable = 0;
    if (MATCH_NAME.test(query)) {
      for (const n of names) {
        let comp = null;
        try { comp = await ppro.AudioFilterFactory.createComponentByDisplayName(n, trackItem); } catch (e) { comp = null; }
        const m = comp ? await tryGet(() => comp.getMatchName()) : null;
        if (m) readable += 1;
        if (m && norm(m) === norm(query)) return { displayName: n, matchedBy: "matchName", comp };
      }
    }
    const close = closeNames(query, names);
    const why = MATCH_NAME.test(query) && !readable
      ? " (no audio effect's match name could be read before adding it: give the display name)" : "";
    throw new BridgeError("NOT_FOUND", `No audio effect "${query}" in Premiere's list (${names.length} names, in the ` +
      `UI language)${why}` + (close.length ? `; close: ${close.join(", ")}` : "") + ". Nothing was changed.",
      { data: { closeNames: close, count: names.length } });
  }

  return {
    /*
     * The effects Premiere can add. Video: VideoFilterFactory's match names, paired with its display names by list
     * position when both lists have the same length and a few created filters confirm the pairing (creating a
     * filter commits nothing). Audio: AudioFilterFactory's display names (localised; it lists no match names).
     * params: { kind? = both }
     */
    async list_effects(params, ctx) {
      await core.checkGuard(ctx);
      const kinds = params.kind ? [params.kind] : ["video", "audio"];
      const out = { effects: [], counts: {} };
      const notes = [];
      if (kinds.indexOf("video") >= 0) {
        const factory = ppro.VideoFilterFactory;
        const matchNames = await nameList(factory, "getMatchNames", "VideoFilterFactory");
        const displayNames = core.isFn(factory, "getDisplayNames") ? await tryGet(() => factory.getDisplayNames(), null) : null;
        const shown = Array.isArray(displayNames) ? displayNames.map(String) : null;
        let paired = !!shown && shown.length === matchNames.length;
        let checked = 0;
        if (paired && matchNames.length && core.isFn(factory, "createComponent")) {
          const picks = [0, Math.floor(matchNames.length / 2), matchNames.length - 1].filter((v, i, a) => a.indexOf(v) === i);
          for (const i of picks.slice(0, SPOT_CHECKS)) {
            let comp = null;
            try { comp = await factory.createComponent(matchNames[i]); } catch (e) { comp = null; }
            const name = comp ? await tryGet(() => comp.getDisplayName()) : null;
            if (name === null || name === undefined) continue;
            checked += 1;
            if (String(name) !== shown[i]) { paired = false; break; }
          }
        }
        for (let i = 0; i < matchNames.length; i++) {
          out.effects.push({ kind: "video", matchName: matchNames[i], displayName: paired ? shown[i] : null });
        }
        out.counts.video = matchNames.length;
        if (!paired && shown) {
          out.videoDisplayNames = shown;
          notes.push(`Video match names (${matchNames.length}) and display names (${shown.length}) do not pair up ` +
            "by list position here, so they are listed apart (videoDisplayNames).");
        } else if (paired && !checked && matchNames.length) {
          notes.push("Video display names are paired with match names by list position (no filter could be created to check it).");
        }
      }
      if (kinds.indexOf("audio") >= 0) {
        const names = await nameList(ppro.AudioFilterFactory, "getDisplayNames", "AudioFilterFactory");
        for (const n of names) out.effects.push({ kind: "audio", matchName: null, displayName: n });
        out.counts.audio = names.length;
        notes.push("Audio effects are listed by display name only, in Premiere's UI language: add one with " +
          "add_audio_effect by that name; its match name shows in inspect_items once it is on a clip.");
      }
      if (notes.length) out.notes = notes;
      return out;
    },

    /*
     * Append an audio effect to the one audio clip under atSeconds (AudioFilterFactory.createComponentByDisplayName,
     * then AudioComponentChain#createAppendComponentAction: the calls api_smoke ran on 27.1, where "降噪" was added
     * and its match name is a GUID). Everything is resolved before the one transaction; the chain is read back.
     * params: { audioTrack, atSeconds, name, dryRun?, undoLabel? }
     */
    async add_audio_effect(params, ctx) {
      core.requireText(params, ["name"]);
      const { project, sequence } = await core.requireSequence(ctx);
      const factory = ppro.AudioFilterFactory;
      for (const fn of ["getDisplayNames", "createComponentByDisplayName"]) {
        if (!core.isFn(factory, fn)) throw new BridgeError("UNSUPPORTED", `AudioFilterFactory.${fn} is not available on this Premiere build.`);
      }
      const tl = timeline.trackLabel("audio", params.audioTrack);
      const it = await timeline.itemAt(sequence, "audio", params.audioTrack, params.atSeconds);
      const d = timeline.requireSpan(await timeline.describeItem(it), tl);
      const names = await nameList(factory, "getDisplayNames", "AudioFilterFactory");
      const pick = await resolveAudioName(params.name, names, it);

      // The chain is read strictly: the append and its verification both depend on it.
      const chain = await it.getComponentChain();
      if (!chain || !core.isFn(chain, "createAppendComponentAction")) {
        throw new BridgeError("UNSUPPORTED", `${tl} @${params.atSeconds}s has no component chain that takes effects on this build.`);
      }
      const before = await effects.chainMatchNames(chain);
      const comp = pick.comp || await factory.createComponentByDisplayName(pick.displayName, it);
      if (!comp) {
        throw new BridgeError("NOT_FOUND", `Premiere made no "${pick.displayName}" effect for this clip (the factory ` +
          "returned nothing). Nothing was changed.");
      }
      const label = params.undoLabel || `Agent: add ${pick.displayName} on ${tl} @${params.atSeconds}s`;
      const out = {
        dryRun: !!params.dryRun,
        track: tl,
        clip: { name: d.name, start: d.start, end: d.end },
        displayName: pick.displayName,
        matchedBy: pick.matchedBy,
        matchName: await tryGet(() => comp.getMatchName()),
        before,
      };
      if (params.dryRun) return out;

      core.runTransaction(project, label, () => chain.createAppendComponentAction(comp), ctx);
      const after = (await effects.describeChain(it, 5)) || [];
      const added = after.length === before.length + 1 ? after[after.length - 1] : null;
      out.after = after.map((c) => c.matchName);
      out.verified = !!added && (added.displayName === pick.displayName || (!!out.matchName && added.matchName === out.matchName));
      if (added) {
        out.matchName = added.matchName || out.matchName;
        out.component = added;
      }
      out.undoLabel = label;
      return out;
    },
  };
};
