/*
 * timeline: tracks and track items of a sequence (counts, lookups, JSON descriptions).
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

  const trackLabel = (kind, index) => (kind === "video" ? "V" : "A") + (index + 1);

  async function trackCount(sequence, kind) {
    return (await tryGet(() => (kind === "video" ? sequence.getVideoTrackCount() : sequence.getAudioTrackCount()), 0)) || 0;
  }

  /* A track that must exist: the bridge never adds one (Premiere would, for an index past the end). */
  async function getTrack(sequence, kind, index) {
    const count = await trackCount(sequence, kind);
    if (index < 0 || index >= count) {
      throw new BridgeError("NOT_FOUND", `${trackLabel(kind, index)} does not exist (sequence has ${count} ${kind} tracks).`);
    }
    return kind === "video" ? sequence.getVideoTrack(index) : sequence.getAudioTrack(index);
  }

  async function clipItems(track) {
    return (await tryGet(() => track.getTrackItems(TRACK_ITEM_CLIP, false), [])) || [];
  }

  async function transitionItems(track) {
    return (await tryGet(() => track.getTrackItems(TRACK_ITEM_TRANSITION, false), [])) || [];
  }

  async function describeTracks(sequence, kind) {
    const out = [];
    const count = await trackCount(sequence, kind);
    for (let i = 0; i < count; i++) {
      const t = await getTrack(sequence, kind, i);
      out.push({ index: i, label: trackLabel(kind, i), name: t.name, clipCount: (await clipItems(t)).length });
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
    for (const it of await clipItems(track)) {
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
    TRACK_ITEM_CLIP, TRACK_ITEM_TRANSITION, trackLabel, trackCount, getTrack, clipItems, transitionItems,
    describeTracks, describeItem, requireSpan, overlaps, itemAt, clipAt,
  };
};
