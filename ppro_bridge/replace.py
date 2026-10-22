"""
replace_clip — Premiere's "Replace With Clip", keeping the editor's work.

Swaps the clip under a time on a video track for another project item while carrying over what was done to it:
  * source range (same in/out; shifted to the media end if it ran past it). An explicit in/out is snapped to the
    nearest source frame first: Premiere puts a source point on the start of the frame under it, so 4.554545 s
    (5 us short of frame 273 at 59.94) would land a frame short (sourceSnap in the report),
  * effect params — Motion, Opacity and any added filters (re-created by match name and occurrence), static
    values and keyframes (sent as media time and moved along with the in point),
  * the clip's transitions (read from an OTIO export, re-added by match name, duration and side).
Not carried: frame holds / time remapping set from the clip menu (not exposed to UXP) — the report says so when
the source range had to move. Speed-changed clips are refused.

Transitions are attributed by owner and side; OTIO gives the seconds of each one before and after its cut. At the
clip's start cut, all after the cut is the clip's own single-sided transition, all before it the previous clip's
outgoing-only one, both a shared one across the cut (mirrored at the end cut). A neighbour's transition is never
re-added on the new clip. Removing a clip also removes a transition at its edit point that belongs to the
neighbour (Premiere 27.1, 19 Oct 2026), so after the swap each one is checked and, when gone, put back exactly on
the neighbour (neighbourTransitions: kept / restored). A shared transition must be centred on the cut (UXP offers
no verified alignment control) and the new media must have the handle it covers, or the swap is refused.

Components the new clip cannot get are refused while planning (also in a dry run): Premiere 27's colour-tool
container (AE.ADBE CompoundComponent, whose settings UXP cannot read), and whatever else the panel cannot create —
asked as set_params dryRun on the old clip, which commits nothing. skip_effects names components to leave behind
instead (skippedEffects).

Keyframes that would land outside the new clip's source range are refused while planning (also in a dry run) with
KEYFRAME_OUT_OF_RANGE, unless drop_outside_keyframes leaves them out (reported as droppedKeyframes).

The new item's audio goes to the highest audio track that is free over the clip; when none is, the swap is refused
until --atrack names one, so music or dialogue underneath is never overwritten.

Every step is checked: the removal is dry-run first and must hit exactly the old clip, and after the swap params,
keyframes and transitions (the clip's and its neighbours') are read back and compared. A failure after the old
clip was removed is reported with outcome "partial" and data.completed listing the steps that did land.
"""

import json
import os
import tempfile
from pathlib import Path

from . import framegrid
from .errors import BridgeError

EPS = 1e-3
INTRINSIC = ("AE.ADBE Opacity", "AE.ADBE Motion")  # every video clip has them; never re-created
NOT_ADDABLE = {
    "AE.ADBE CompoundComponent": "Premiere 27's colour-tool container (Color And Contrast, Color Shift, Texture, "
                                 "Unsharp Mask): UXP shows none of its settings and the panel cannot create one",
}


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-4
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def _where(match_name: str, occurrence: int, index: int) -> str:
    return f"{match_name}#{index}" if occurrence == 1 else f"{match_name}[{occurrence}]#{index}"


def _inside(t: float, keep) -> bool:
    return keep is None or keep[0] <= t <= keep[1]


def _clip_under(rpc, track_idx: int, at: float) -> dict:
    data = rpc("inspect_items", {"videoTracks": [track_idx], "fromSeconds": at, "toSeconds": at})
    hits = [c for t in data["tracks"] for c in t["clips"] if c["start"] <= at + EPS and c["end"] > at + EPS]
    if len(hits) != 1:
        raise BridgeError(f"Expected one clip on V{track_idx + 1} at {at}s, found {len(hits)}",
                          code="NOT_FOUND" if not hits else "AMBIGUOUS", outcome="not_committed")
    return hits[0]


def otio_transitions(rpc, track_idx: int, doc: dict | None = None) -> list[dict]:
    """Transitions on one video track: {matchName, name, at, before, after} — `at` is the cut point in sequence
    seconds; before/after are the seconds on each side of it. Reads a fresh OTIO export, or `doc` (a saved export)
    when given."""
    if doc is None:
        tmp = Path(tempfile.gettempdir()) / f"ppro_replace_{os.getpid()}.otio"
        rpc("export_otio", {"outputPath": str(tmp)})
        try:
            doc = json.loads(tmp.read_text(encoding="utf-8-sig"))
        finally:
            tmp.unlink(missing_ok=True)
    video = [t for t in doc["tracks"]["children"] if t.get("kind") == "Video"]
    if track_idx >= len(video):
        return []
    out, pos = [], 0.0
    for c in video[track_idx]["children"]:
        if c.get("OTIO_SCHEMA", "").startswith("Transition"):
            rate = c["in_offset"]["rate"]
            meta = (c.get("metadata") or {}).get("PremierePro_OTIO") or {}
            out.append({"matchName": meta.get("MatchName"), "name": c.get("name"), "at": pos,
                        "before": c["in_offset"]["value"] / rate, "after": c["out_offset"]["value"] / rate})
            continue
        sr = c.get("source_range")
        if sr:
            pos += sr["duration"]["value"] / sr["duration"]["rate"]
    return out


def cut_transitions(transitions: list[dict], start: float, end: float, frame: float) -> list[dict]:
    """The transitions at a clip's start and end cut (from otio_transitions), each with `cut` (start | end) and
    `owner`: "clip" (single-sided on the clip: all after the start cut, all before the end cut), "previous" /
    "next" (the neighbour's single-sided one: all before the start cut / all after the end cut) or "shared"
    (across the cut)."""
    found = []
    for tr in transitions:
        d_start, d_end = abs(tr["at"] - start), abs(tr["at"] - end)
        if min(d_start, d_end) > 1.5 * frame:
            continue
        cut = "start" if d_start <= d_end else "end"
        before, after = tr["before"], tr["after"]
        away, on_clip = (before, after) if cut == "start" else (after, before)
        if on_clip < frame / 4 and away < frame / 4:
            continue  # nothing on either side
        if away < frame / 4:
            owner = "clip"
        elif on_clip < frame / 4:
            owner = "previous" if cut == "start" else "next"
        else:
            owner = "shared"
        found.append({"matchName": tr["matchName"], "name": tr["name"], "cut": cut, "owner": owner, "at": tr["at"],
                      "before": round(before, 6), "after": round(after, 6),
                      "durationSeconds": round(before + after, 6), "applyToStart": cut == "start",
                      "forceSingleSided": owner != "shared"})
    return found


def _matches(found: dict, want: dict, tol: float) -> bool:
    return (found["matchName"] == want["matchName"] and abs(found["before"] - want["before"]) <= tol
            and abs(found["after"] - want["after"]) <= tol)


def _snapped_duration(seconds: float, fps: float) -> float:
    """Whole frames, plus a tenth of a frame so Premiere's tick rounding can't drop one."""
    return (round(seconds * fps) + 0.1) / fps


def _snap_source(t: float, rate: float, snap: dict, key: str) -> float:
    v = round(t * rate) / rate
    if abs(v - t) > 1e-9:
        snap[key] = [round(t, 6), round(v, 6)]
    return v


def _unaddable(rpc, track_idx: int, at: float, old_comps, skip) -> tuple[list[dict], list[str]]:
    """Components of the old clip the new clip could not get: the known ones (NOT_ADDABLE), then whatever the panel
    cannot create — asked as set_params dryRun on the old clip for one more instance, which validates and commits
    nothing. A panel from before dryRun refuses the param (BAD_PARAMS): then only the known ones are found and a
    note says so. Components in `skip` are not asked about."""
    counts, display = {}, {}
    for c in old_comps or []:
        m = c["matchName"]
        if m not in INTRINSIC and m not in skip:
            counts[m] = counts.get(m, 0) + 1
            display.setdefault(m, c.get("displayName"))
    found, notes, ask = [], [], True
    for m, n in counts.items():
        row = {"matchName": m, "instances": n, "displayName": display[m]}
        if m in NOT_ADDABLE:
            found.append(dict(row, reason=NOT_ADDABLE[m]))
            continue
        if not ask:
            continue
        try:
            rpc("set_params", {"videoTrack": track_idx, "atSeconds": at, "dryRun": True,
                               "components": [{"matchName": m, "occurrence": n + 1, "params": []}]})
        except BridgeError as e:
            if e.code == "BAD_PARAMS" and "dryRun" in str(e):
                ask = False
                notes.append("the loaded panel cannot check effects before writing (its set_params has no dryRun): "
                             "Reload the panel; until then only known containers are caught here, and an effect "
                             "Premiere cannot create still fails after the old clip was removed")
            elif e.code == "NOT_FOUND" and "Cannot add effect" in str(e):
                found.append(dict(row, reason=f"the panel cannot create it ({e})"))
            else:
                raise
    return found, notes


def _pairs(old_comps, new_comps):
    """(old component, matching new component or None, occurrence), matched by match name + occurrence
    (the n-th instance of a match name in chain order, 1-based)."""
    seen = {}
    for oc in old_comps or []:
        n = seen.get(oc["matchName"], 0) + 1
        seen[oc["matchName"]] = n
        matches = [c for c in new_comps or [] if c["matchName"] == oc["matchName"]]
        yield oc, (matches[n - 1] if n <= len(matches) else None), n


def _param_diffs(old_comps, new_comps, delta: float = 0.0, keep=None) -> tuple[list[dict], list[dict]]:
    """set_params components ({matchName, occurrence, params}) where the new clip differs, and the components
    missing on it ([{matchName, occurrence}]). Keyframed params are always listed, as media time (timeBase
    "media") moved by `delta` (new in − old in); with `keep` = (lo, hi) only keyframes landing inside it are kept,
    and a param left without keyframes is compared as a static value."""
    out, missing = [], []
    for oc, nc, occ in _pairs(old_comps, new_comps):
        if nc is None:
            missing.append({"matchName": oc["matchName"], "occurrence": occ})
            continue
        new_by_idx = {p["index"]: p for p in nc["params"]}
        params = []
        for op in oc["params"]:
            np_ = new_by_idx.get(op["index"])
            if np_ is None or op.get("value") is None:
                continue
            if op.get("keyframed"):
                kfs = [{"t": round(k["t"] + delta, 6), "value": k["value"]}
                       for k in op.get("keyframes") or [] if _inside(k["t"] + delta, keep)]
                if kfs or keep is None:
                    params.append({"index": op["index"], "value": op["value"], "timeBase": "media",
                                   "keyframes": kfs})
                    continue
            if not _same(op["value"], np_.get("value")):
                params.append({"index": op["index"], "value": op["value"]})
        if params:
            out.append({"matchName": oc["matchName"], "occurrence": occ, "params": params})
    return out, missing


def _keyframe_mismatches(old_comps, new_comps, delta: float, tol: float, keep=None) -> tuple[list[str], list[str]]:
    """Keyframed params whose keyframes did not come across (times moved by delta, ±tol; only those inside
    `keep` when given); and those that could not be compared because inspect_items truncated the list."""
    bad, unchecked = [], []
    for oc, nc, occ in _pairs(old_comps, new_comps):
        new_by_idx = {p["index"]: p for p in (nc or {}).get("params", [])}
        for op in oc["params"]:
            if not op.get("keyframed"):
                continue
            where = _where(oc["matchName"], occ, op["index"])
            np_ = new_by_idx.get(op["index"])
            if op.get("keyframesTruncated") or (np_ or {}).get("keyframesTruncated"):
                unchecked.append(where)
                continue
            want = [k for k in op.get("keyframes") or [] if _inside(k["t"] + delta, keep)]
            got = (np_ or {}).get("keyframes") or []
            if len(want) != len(got) or not all(abs(g["t"] - (w["t"] + delta)) <= tol and _same(g["value"], w["value"])
                                                for w, g in zip(want, got)):
                bad.append(where)
    return bad, unchecked


def _keyframes_outside(old_comps, delta: float, lo: float, hi: float) -> list[dict]:
    """Old keyframes that, moved by delta, fall outside [lo, hi] (the planned new source range)."""
    out = []
    for oc, _, occ in _pairs(old_comps, []):
        for op in oc["params"]:
            if not op.get("keyframed") or op.get("value") is None:
                continue
            for k in op.get("keyframes") or []:
                t = k["t"] + delta
                if not lo <= t <= hi:
                    out.append({"matchName": oc["matchName"], "occurrence": occ, "index": op["index"],
                                "t": k["t"], "newT": round(t, 6), "value": k["value"]})
    return out


def free_audio_track(rpc, start: float, end: float) -> int | None:
    """Highest audio track with nothing on it over [start, end]."""
    tracks = rpc("list_items", {"kinds": ["audio"]}).get("audio", [])
    for t in sorted(tracks, key=lambda t: t["index"], reverse=True):
        if not any(i.get("start") is not None and i["start"] < end - EPS and i["end"] > start + EPS
                   for i in t["items"]):
            return t["index"]
    return None


def _partial(error: Exception, completed: list[str], report: dict) -> BridgeError:
    data = {"completed": list(completed), "report": report}
    if getattr(error, "outcome", None):
        data["failedStepOutcome"] = error.outcome  # "unknown": that step itself may have landed too
    if getattr(error, "data", None) is not None:
        data["errorData"] = error.data
    return BridgeError(f"replace_clip stopped after: {', '.join(completed)}. {error} The old clip is no longer on the "
                       "timeline: finish the swap by hand, or undo these steps in Premiere (Edit > Undo).",
                       code=getattr(error, "code", None) or "INTERNAL", outcome="partial", data=data)


def replace_clip(rpc, track_idx: int, at: float, new: dict, *, src_in=None, src_out=None, atrack=None,
                 keep_effects=True, keep_transitions=True, dry_run=False, label="Agent replace",
                 probe_media=None, transitions_snapshot: dict | None = None, drop_outside_keyframes=False,
                 skip_effects=None) -> dict:
    """`new`: the project item from list_project_items ({name, bin, mediaPath}).
    `transitions_snapshot`: a saved OTIO export to take the clip's transitions from (e.g. taken before an earlier,
    imperfect swap) instead of the live timeline. `probe_media`: ffprobe-like callable (None: media facts unknown).
    `skip_effects`: match names of components not to carry (left behind, listed in skippedEffects).
    keep_transitions=False carries none of the clip's own transitions; the neighbours' are protected either way."""
    seq = rpc("get_sequence")
    fps = framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or 30)
    frame = 1.0 / fps
    old = _clip_under(rpc, track_idx, at)
    start, end = old["start"], old["end"]
    length = end - start
    if isinstance(old.get("speed"), (int, float)) and abs(old["speed"] - 1) > 1e-6:
        raise BridgeError(f'{old["name"]} plays at {old["speed"]:g}x; speed-changed clips are not supported '
                          "(their source/sequence times don't map 1:1).", code="UNSUPPORTED", outcome="not_committed")

    new_name, new_bin = new["name"], new.get("bin") or ""
    info = probe_media(new.get("mediaPath")) if probe_media else {}
    notes = []
    s_in = old["inPoint"] if src_in is None else float(src_in)
    s_out = None if src_out is None else float(src_out)
    snap = {}
    if src_in is not None or src_out is not None:
        # Premiere puts a source point on the start of the frame under it: snap to the nearest frame first.
        # The old clip's own in point is Premiere's reading already and stays as it is.
        rate, grid = framegrid.source_rate(info, fps)
        snap = {"grid": grid, "fps": round(rate, 4)}
        if not info.get("fps"):
            notes.append("the new media's frame rate is unknown (ffprobe missing or could not read it): --in/--out "
                         "were snapped to the sequence's frames")
        if src_in is not None:
            s_in = _snap_source(s_in, rate, snap, "in")
        if s_out is not None:
            s_out = _snap_source(s_out, rate, snap, "out")
            if s_out <= s_in + EPS:
                raise BridgeError(f"--out ({s_out:.4f}s) must be at least a frame after --in ({s_in:.4f}s).",
                                  code="BAD_PARAMS", outcome="not_committed")
    if s_out is None:
        s_out = s_in + length
    dur = info.get("duration")
    if src_out is None and dur and s_out > dur + frame / 2:
        s_out, s_in = dur, max(0.0, dur - length)
        notes.append(f"source range ran past the new media's end ({dur:.3f}s) — shifted to {s_in:.3f}–{s_out:.3f}s "
                     "(a frame hold on the old clip is not carried over)")
    want_end = start + (s_out - s_in)

    skip = set(skip_effects or ())
    carried = [c for c in old.get("components") or [] if c["matchName"] not in skip]
    skipped, readd, dropped = [], [], []
    if keep_effects:
        outside = _keyframes_outside(carried, s_in - (old.get("inPoint") or 0.0), s_in - frame / 2, s_out + frame / 2)
        if outside and not drop_outside_keyframes:
            raise BridgeError(f"{len(outside)} keyframe(s) of {old['name']} would land outside the new source range "
                              f"{s_in:.3f}–{s_out:.3f}s (Premiere would clamp them onto the edge). Choose --in/--out "
                              "that cover them, or pass --drop-outside-keyframes to leave them out.",
                              code="KEYFRAME_OUT_OF_RANGE", outcome="not_committed",
                              data={"keyframes": outside, "source": [round(s_in, 4), round(s_out, 4)]})
        dropped = outside
        unaddable, probe_notes = _unaddable(rpc, track_idx, at, old.get("components"), skip)
        notes += probe_notes
        if unaddable:
            names = ", ".join(u["matchName"] + (f' ("{u["displayName"]}")' if u.get("displayName") else "")
                              for u in unaddable)
            first = unaddable[0]["matchName"]
            raise BridgeError(f"{old['name']} has {names}, which the new clip cannot get: "
                              f"{'; '.join(u['reason'] for u in unaddable)}. Nothing was changed. The bridge cannot "
                              "tell whether it holds a grade; look at the clip's Effect Controls in Premiere.",
                              code="UNSUPPORTED", outcome="not_committed", data={"effects": unaddable},
                              hint=f'If it changes nothing, pass --skip-effect "{first}" to swap without it (listed '
                                   "in skippedEffects); otherwise swap this clip in Premiere (Clip > Replace With "
                                   "Clip keeps its effects).")
        for oc, _, occ in _pairs(old.get("components"), []):
            if oc["matchName"] in skip:
                skipped.append({"matchName": oc["matchName"], "occurrence": occ, "displayName": oc.get("displayName"),
                                "reason": NOT_ADDABLE.get(oc["matchName"], "skipped on request")})
        readd = sorted({c["matchName"] for c in carried if c["matchName"] not in INTRINSIC})

    at_cuts = cut_transitions(otio_transitions(rpc, track_idx, transitions_snapshot), start, end, frame)
    unknown = [t["name"] for t in at_cuts if not t["matchName"]]
    if unknown:
        raise BridgeError(f"Transitions without a match name in the OTIO export: {unknown}",
                          code="UNSUPPORTED", outcome="not_committed")
    own = [t for t in at_cuts if t["owner"] in ("clip", "shared")]
    neighbours = [t for t in at_cuts if t["owner"] in ("previous", "next")]
    transitions = own if keep_transitions else []
    if own and not keep_transitions:
        notes.append(f"not carried (--no-transitions): {', '.join(t['name'] or t['matchName'] for t in own)}"
                     + (" — a shared one goes for the neighbour too" if any(t["owner"] == "shared" for t in own)
                        else ""))
    for t in transitions:
        if t["owner"] != "shared":
            continue
        if abs(t["before"] - t["after"]) > 1.1 * frame:
            raise BridgeError(f"{t['name']} across the {t['cut']} cut at {t['at']:.3f}s is not centred on it "
                              f"({t['before']:.3f}s before, {t['after']:.3f}s after): the bridge can only re-create "
                              "a centred one. Nothing was changed; pass --no-transitions and set it by hand.",
                              code="UNSUPPORTED", outcome="not_committed", data={"transition": t})
        handle, have = (t["before"], s_in) if t["cut"] == "start" else (t["after"], dur - s_out if dur else None)
        if have is None:
            notes.append(f"{t['name']} at the end needs {handle:.3f}s of the new media after its out point; its "
                         "length is unknown (ffprobe), so that was not checked")
        elif have < handle - frame / 2:
            where = "before its in point" if t["cut"] == "start" else "after its out point"
            raise BridgeError(f"{t['name']} across the {t['cut']} cut at {t['at']:.3f}s needs {handle:.3f}s of the new "
                              f"media {where} (a handle); the new range leaves {max(have, 0.0):.3f}s, so Premiere "
                              "would shorten it. Nothing was changed; choose --in/--out with that handle, or pass "
                              "--no-transitions.", code="UNSUPPORTED", outcome="not_committed", data={"transition": t})
    if neighbours:
        track = next((t for t in rpc("list_items", {"kinds": ["video"]}).get("video", []) if t["index"] == track_idx),
                     None)
        for t in neighbours:
            edge = "end" if t["owner"] == "previous" else "start"
            clip = next((i for i in (track or {}).get("items", [])
                         if i.get(edge) is not None and abs(i[edge] - t["at"]) <= frame / 2), None)
            t["neighbour"] = clip["name"] if clip else None
            t["neighbourAt"] = (clip["start"] + clip["end"]) / 2 if clip else None

    # A range covering the whole media is placed as the whole clip: setting the out point at the
    # very end gets clamped against the file's native duration and loses the last frame when the
    # item is interpreted at a different rate (e.g. a 60 fps file interpreted at 59.94). Unless the
    # whole clip, at its longest possible interpretation, would run into the next clip.
    whole = framegrid.covers_whole_media(s_in, s_out, dur, fps)
    if whole:
        extents = framegrid.whole_extents(info, fps)
        longest = start + max(extents) if extents else want_end
        track = next((t for t in rpc("list_items", {"kinds": ["video"]}).get("video", []) if t["index"] == track_idx), None)
        blocking = [i["name"] for i in (track or {}).get("items", [])
                    if i.get("start") is not None and i["start"] < longest - frame / 2 and i["start"] >= end - frame / 2]
        if longest > want_end + frame / 2 and blocking:
            whole = False
            notes.append(f"the whole clip could run into {', '.join(blocking)}; placing the source range instead, "
                         "which may end a frame short")

    has_audio = info.get("hasAudio")  # None: unknown (no ffprobe) — route it as if it had audio
    if atrack is not None:
        route = f"A{atrack + 1} (--atrack)"
    elif has_audio is False:
        atrack, route = 0, "none (no audio in the new media)"
    else:
        atrack = free_audio_track(rpc, start, want_end + frame)
        if atrack is None:
            raise BridgeError(f"{new_name} has audio and no audio track is free over {start:.3f}–{want_end:.3f}s; "
                              "pass --atrack to choose one (whatever is there will be overwritten).",
                              code="NO_FREE_AUDIO_TRACK", outcome="not_committed")
        route = f"A{atrack + 1} (highest free track)" + ("" if has_audio else ", audio unknown: ffprobe missing or could not read the file")

    report = {"track": f"V{track_idx + 1}", "start": start, "end": end, "old": old["name"],
              "new": (f"{new_bin}/" if new_bin else "") + new_name,
              "source": [round(s_in, 4), round(s_out, 4)], "placeWhole": whole, "audioRoute": route,
              "transitions": transitions, "neighbourTransitions": neighbours, "notes": notes, "dryRun": dry_run}
    if snap.get("in") or snap.get("out"):
        report["sourceSnap"] = snap
    if keep_effects:
        report["readdEffects"] = readd
    if skipped:
        report["skippedEffects"] = skipped
    if dropped:
        report["droppedKeyframes"] = dropped
    if dry_run:
        return report

    span = {"videoTracks": [track_idx], "audioTracks": [], "fromSeconds": start - frame / 4,
            "toSeconds": end + frame / 4, "mode": "inside"}
    hit = rpc("remove_items", dict(span, dryRun=True))["removed"]
    if len(hit) != 1 or hit[0]["name"] != old["name"]:
        raise BridgeError(f"Removal range would take {[h['name'] for h in hit]} — not touching it",
                          code="PRECONDITION_FAILED", outcome="not_committed")
    rpc("remove_items", dict(span, undoLabel=f"{label}: remove {old['name']}"))
    completed = [f"remove {old['name']}"]
    try:
        _swap_in(rpc, report, completed, old=old, carried=carried, new_name=new_name, new_bin=new_bin,
                 track_idx=track_idx, start=start, length=length, want_end=want_end, s_in=s_in, s_out=s_out,
                 whole=whole, atrack=atrack, frame=frame, fps=fps, keep_effects=keep_effects,
                 transitions=transitions, neighbours=neighbours, label=label,
                 drop_outside_keyframes=drop_outside_keyframes)
    except Exception as e:
        raise _partial(e, completed, report) from e
    return report


def _swap_in(rpc, report, completed, *, old, carried, new_name, new_bin, track_idx, start, length, want_end, s_in,
             s_out, whole, atrack, frame, fps, keep_effects, transitions, neighbours, label, drop_outside_keyframes):
    place = {"itemName": new_name, "binPath": new_bin, "atSeconds": start, "videoTrack": track_idx,
             "audioTrack": atrack, "mode": "overwrite", "undoLabel": f"{label}: {new_name}"}
    if not whole:
        place.update(inSeconds=s_in, outSeconds=s_out)
    rpc("insert_clip", place)
    completed.append(f"insert {new_name}")
    report["placedWhole"] = whole
    mid = start + length / 2

    delta, keep = 0.0, None
    if keep_effects:
        placed = _clip_under(rpc, track_idx, mid)
        new_in = placed.get("inPoint") or 0.0
        delta = new_in - (old.get("inPoint") or 0.0)
        if drop_outside_keyframes:
            new_out = placed.get("outPoint")
            keep = (new_in - frame / 2, (new_in + (s_out - s_in) if new_out is None else new_out) + frame / 2)
        diffs, missing = _param_diffs(carried, placed["components"], delta, keep)
        if missing:  # create the added filters first, then diff against their defaults
            need = {}
            for m in missing:
                need[m["matchName"]] = max(need.get(m["matchName"], 0), m["occurrence"])
            rpc("set_params", {"videoTrack": track_idx, "atSeconds": mid,
                               "components": [{"matchName": n, "occurrence": occ, "params": []}
                                              for n, occ in need.items()],
                               "undoLabel": f"{label}: re-add effects"})
            completed.append("re-add effects")
            placed = _clip_under(rpc, track_idx, mid)
            diffs, missing = _param_diffs(carried, placed["components"], delta, keep)
        if diffs:
            rpc("set_params", {"videoTrack": track_idx, "atSeconds": mid, "components": diffs,
                               "undoLabel": f"{label}: restore effect params"})
            completed.append("restore effect params")
        report["restoredParams"] = [_where(c["matchName"], c["occurrence"], p["index"])
                                    for c in diffs for p in c["params"]]
        if abs(delta) > EPS:
            report["keyframeShift"] = round(delta, 6)

    for t in transitions:
        edge = "start" if t["applyToStart"] else "end"
        rpc("add_transition", {"videoTrack": track_idx, "atSeconds": mid, "matchName": t["matchName"],
                               "durationSeconds": _snapped_duration(t["durationSeconds"], fps),
                               "applyToStart": t["applyToStart"], "forceSingleSided": t["forceSingleSided"],
                               "undoLabel": f"{label}: {t['name']} at {edge}"})
        completed.append(f"transition {t['name']} at {edge}")

    # The neighbours' transitions at the old clip's edit points: removing it may have taken them along.
    tol = 0.6 * frame  # under one frame: a one-frame slip is a real defect (gaps, flashes)
    if neighbours:
        now = otio_transitions(rpc, track_idx)
        for t in neighbours:
            here = [n for n in now if abs(n["at"] - t["at"]) <= frame]
            if any(_matches(n, t, tol) for n in here):
                t["status"] = "kept"
            elif here or t.get("neighbourAt") is None:
                t["status"] = "missing"  # something else sits there, or no clip to put it back on
            else:
                rpc("add_transition", {"videoTrack": track_idx, "atSeconds": t["neighbourAt"],
                                       "matchName": t["matchName"],
                                       "durationSeconds": _snapped_duration(t["durationSeconds"], fps),
                                       "applyToStart": t["owner"] == "next", "forceSingleSided": True,
                                       "undoLabel": f"{label}: restore {t['name']} on {t['neighbour']}"})
                completed.append(f"restore {t['name']} on {t['neighbour']}")
                t["status"] = "restored"

    # verify by reading everything back
    after = _clip_under(rpc, track_idx, mid)
    problems = []
    if after["name"] != new_name or abs(after["start"] - start) > tol or abs(after["end"] - want_end) > tol:
        problems.append(f"placement: got {after['name']} {after['start']:.3f}-{after['end']:.3f}, "
                        f"wanted {start:.3f}-{want_end:.3f}")
    if keep_effects:
        left, missing = _param_diffs(carried, after["components"], delta, keep)
        left = [_where(c["matchName"], c["occurrence"], p["index"]) for c in left for p in c["params"]
                if "keyframes" not in p]
        keyed, unchecked = _keyframe_mismatches(carried, after["components"], delta, frame / 2, keep)
        if left or missing or keyed:
            gone = [m["matchName"] + ("" if m["occurrence"] == 1 else f"[{m['occurrence']}]") for m in missing]
            problems.append(f"params still differ: {left} keyframes differ: {keyed} missing effects: {gone}")
        if unchecked:
            report["keyframesNotChecked"] = unchecked
    if transitions or neighbours:
        now = otio_transitions(rpc, track_idx)
        for t in transitions:
            cut = after["start"] if t["cut"] == "start" else after["end"]
            side_tol = tol if t["owner"] == "clip" else 1.1 * frame  # a centred split may round either way
            if not any(abs(n["at"] - cut) <= frame and _matches(n, t, side_tol) for n in now):
                problems.append(f"transition {t['name']} at {t['cut']} not found after adding "
                                f"({t['before']:.3f}s before / {t['after']:.3f}s after the cut)")
        for t in neighbours:
            if not any(abs(n["at"] - t["at"]) <= frame and _matches(n, t, tol) for n in now):
                t["status"] = "missing"
                problems.append(f"the {t['owner']} clip's {t['name']} at {t['at']:.3f}s ({t['before']:.3f}s before / "
                                f"{t['after']:.3f}s after the cut) is gone or changed")
    report["verified"] = not problems
    report["problems"] = problems
