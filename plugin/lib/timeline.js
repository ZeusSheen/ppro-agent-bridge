/*
 * timeline: tracks and track items of a sequence (counts, lookups, JSON descriptions, the fingerprint).
 * Track counts, tracks and their item lists are read strictly: a read that throws (or returns no list) is
 * INTERNAL "[read] ..." naming what could not be read, never 0 / [] — an unreadable track must not look like an
 * empty one that a write then acts on. Callers that may skip what they cannot read (capability probes) wrap the
 * call in core.tryGet themselves.
 * Factory module.   deps: { ppro, core }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const BridgeError = core.BridgeError;
  const secs = core.secs;
  const tryGet = core.tryGet;
  const EPS = core.EPS;

  const TRACK_ITEM_CLIP =
    (ppro.Constants && ppro.Constants.TrackItemType && ppro.Constants.TrackItemType.CLIP) || 1;
  const TRACK_ITEM_TRANSITION =
    (ppro.Constants && ppro.Constants.TrackItemType && ppro.Constants.TrackItemType.TRANSITION) || 2;
  // list_items fields that make up the fingerprint, in ppro_bridge/items.py FINGERPRINT_FIELDS order
  const FINGERPRINT_FIELDS = ["name", "start", "end", "inPoint", "outPoint", "speed", "disabled", "mediaPath"];

  const trackLabel = (kind, index) => (kind === "video" ? "V" : "A") + (index + 1);
  const sequenceName = (sequence) => String(core.tryGetSync(() => sequence.name, "?"));

  /* A read an edit or a listing depends on: its value, or INTERNAL "[read] <what> could not be read: ...". */
  async function strictRead(what, fn) {
    try {
      return await fn();
    } catch (e) {
      throw new BridgeError("INTERNAL", `[read] ${what} could not be read: ${core.errorMessage(e)}`);
    }
  }

  async function trackCount(sequence, kind) {
    const what = `the ${kind} track count of sequence "${sequenceName(sequence)}"`;
    const v = await strictRead(what, () => (kind === "video" ? sequence.getVideoTrackCount() : sequence.getAudioTrackCount()));
    const n = v === null || v === undefined || v === "" ? NaN : Number(v);
    if (!Number.isInteger(n) || n < 0) throw new BridgeError("INTERNAL", `[read] ${what} came back as ${v}`);
    return n;
  }

  /* A track that must exist: the bridge never adds one (Premiere would, for an index past the end). */
  async function getTrack(sequence, kind, index) {
    const count = await trackCount(sequence, kind);
    if (index < 0 || index >= count) {
      throw new BridgeError("NOT_FOUND", `${trackLabel(kind, index)} does not exist (sequence has ${count} ${kind} tracks).`);
    }
    const what = `${trackLabel(kind, index)} of sequence "${sequenceName(sequence)}"`;
    const track = await strictRead(what, () => (kind === "video" ? sequence.getVideoTrack(index) : sequence.getAudioTrack(index)));
    if (!track) throw new BridgeError("INTERNAL", `[read] ${what} came back empty`);
    return track;
  }

  /* The items of one type on a track; `where` names the track in errors (its label, e.g. "V2"). A read that throws
   * is a failure; null / undefined is an empty track (the 0.5 panel read it that way, and whether 27.1 returns [] or
   * null for an empty track is not measured yet). Right after clips are removed, 27.1 can list a null in their place
   * for a moment (live, 2 Nov 2026: a scan straight after remove_items failed on it, one a few seconds later did not):
   * such a list is read once more after 200 ms, and a slot still empty then is not a clip. */
  async function trackItems(track, type, what, where) {
    const name = where || `track "${String(core.tryGetSync(() => track.name, "?"))}"`;
    const desc = `the ${what} on ${name}`;
    const read = async () => {
      const list = await strictRead(desc, () => track.getTrackItems(type, false));
      if (Array.isArray(list)) return list;
      if (list === null || list === undefined) return [];
      if (typeof list.length === "number") return Array.from(list);
      throw new BridgeError("INTERNAL", `[read] ${desc} came back as ${typeof list}, not a list`);
    };
    const hasHole = (list) => list.some((it) => it === null || it === undefined);
    let list = await read();
    if (hasHole(list)) {
      await core.sleep(200);
      list = await read();
    }
    return hasHole(list) ? list.filter((it) => it !== null && it !== undefined) : list;
  }

  async function clipItems(track, where) {
    return trackItems(track, TRACK_ITEM_CLIP, "clips", where);
  }

  async function transitionItems(track, where) {
    return trackItems(track, TRACK_ITEM_TRANSITION, "transitions", where);
  }

  async function describeTracks(sequence, kind) {
    const out = [];
    const count = await trackCount(sequence, kind);
    for (let i = 0; i < count; i++) {
      const t = await getTrack(sequence, kind, i);
      out.push({ index: i, label: trackLabel(kind, i), name: t.name, clipCount: (await clipItems(t, trackLabel(kind, i))).length });
    }
    return out;
  }

  /* One clip as JSON. Each field is read leniently: a field that cannot be read is null, which list_items reports
   * as it is (generated items have no project item or media path at all), and every write that needs a position
   * goes through requireSpan, which refuses a clip without start / end. */
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

  /* Every track of one kind as list_items reports it: {index, label, name, items (in start order)}. */
  async function listTracks(sequence, kind) {
    const tracks = [];
    const count = await trackCount(sequence, kind);
    for (let i = 0; i < count; i++) {
      const track = await getTrack(sequence, kind, i);
      const items = [];
      for (const it of await clipItems(track, trackLabel(kind, i))) items.push(await describeItem(it));
      items.sort((a, b) => (a.start || 0) - (b.start || 0));
      tracks.push({ index: i, label: trackLabel(kind, i), name: track.name, items });
    }
    return tracks;
  }

  /*
   * The timeline fingerprint, exactly as ppro_bridge/items.py fingerprint() computes it from list_items and
   * get_sequence: compact JSON of [sequence guid, [[kind, index, [[FINGERPRINT_FIELDS of each clip]]] per track,
   * video then audio]], numbers as core.fixed6, then the first 16 hex digits of its SHA-256. Built from the same
   * reads as list_items (listTracks); tests/test_panel.py checks it against the Python value. Keep both in step.
   */
  async function fingerprint(sequence) {
    const norm = (v) => {
      if (typeof v === "number") return Number.isFinite(v) ? core.fixed6(v) : null;  // JSON sends NaN as null
      return v === undefined ? null : v;
    };
    const tracks = [];
    for (const kind of ["video", "audio"]) {
      for (const t of await listTracks(sequence, kind)) {
        tracks.push([kind, t.index, t.items.map((it) => FINGERPRINT_FIELDS.map((k) => norm(it[k])))]);
      }
    }
    return core.sha256Hex(JSON.stringify([core.readGuid(sequence), tracks])).slice(0, 16);
  }

  /* A target whose position could not be read must stop the edit, not be skipped silently. */
  function requireSpan(d, label) {
    if (d.start === null || d.end === null) {
      throw new BridgeError("INTERNAL", `[read] ${label} item "${d.name}" returned no start/end`);
    }
    return d;
  }

  /* [start, end) overlaps [from, to]; from == to means "the clip under that instant". */
  function overlaps(d, from, to) {
    return d.start !== null && d.end !== null && d.start <= to + EPS && d.end > from + EPS;
  }

  /* The single clip on a video or audio track under `at` (sequence seconds). */
  async function itemAt(sequence, kind, index, at) {
    const label = trackLabel(kind, index);
    const track = await getTrack(sequence, kind, index);
    const hits = [];
    for (const it of await clipItems(track, label)) {
      const d = requireSpan(await describeItem(it), label);
      if (overlaps(d, at, at)) hits.push(it);
    }
    if (!hits.length) throw new BridgeError("NOT_FOUND", `No clip on ${label} at ${at}s.`);
    if (hits.length > 1) throw new BridgeError("AMBIGUOUS", `Expected one clip on ${label} at ${at}s, found ${hits.length}.`);
    return hits[0];
  }

  /* The single clip on a video track under `at` (sequence seconds). */
  async function clipAt(sequence, videoTrack, at) {
    return itemAt(sequence, "video", videoTrack, at);
  }

  return {
    TRACK_ITEM_CLIP, TRACK_ITEM_TRANSITION, FINGERPRINT_FIELDS, trackLabel, trackCount, getTrack, clipItems,
    transitionItems, describeTracks, describeItem, listTracks, fingerprint, requireSpan, overlaps, itemAt, clipAt,
  };
};
