"""
replace_clip — Premiere's "Replace With Clip", keeping the editor's work.

Swaps the clip under a time on a video track for another project item while
carrying over what was done to it:
  * source range (same in/out; shifted to the media end if it ran past it),
  * effect params — Motion, Opacity and any added filters (re-created by match
    name), static values and keyframes,
  * transitions on the clip's start/end (read from an OTIO export, re-added by
    match name and duration).
Not carried: frame holds / time remapping set from the clip menu (not exposed
to UXP) — the report says so when the source range had to move.

Every step is checked: the removal is dry-run first and must hit exactly the
old clip, and after the swap params and transitions are read back and compared.
"""

import json
import os
import tempfile
from pathlib import Path

EPS = 1e-3


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


def _clip_under(rpc, track_idx: int, at: float) -> dict:
    data = rpc("inspect_items", {"videoTracks": [track_idx], "fromSeconds": at, "toSeconds": at})
    hits = [c for t in data["tracks"] for c in t["clips"] if c["start"] <= at + EPS and c["end"] > at + EPS]
    if len(hits) != 1:
        raise ValueError(f"Expected one clip on V{track_idx + 1} at {at}s, found {len(hits)}")
    return hits[0]


def otio_transitions(rpc, track_idx: int, doc: dict | None = None) -> list[dict]:
    """Transitions on one video track: {matchName, name, at, before, after} — `at` is the
    cut point in sequence seconds; before/after are the seconds on each side of it.
    Reads a fresh OTIO export, or `doc` (a saved export) when given."""
    if doc is None:
        tmp = Path(tempfile.gettempdir()) / f"ppro_replace_{os.getpid()}.otio"
        rpc("export_otio", {"outputPath": str(tmp)})
        try:
            doc = json.loads(tmp.read_text(encoding="utf-8"))
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


def _edge_transitions(rpc, track_idx, start, end, frame, doc=None) -> list[dict]:
    found = []
    for tr in otio_transitions(rpc, track_idx, doc):
        if abs(tr["at"] - start) <= 1.5 * frame:
            found.append({"matchName": tr["matchName"], "name": tr["name"], "applyToStart": True,
                          "durationSeconds": round(tr["before"] + tr["after"], 6),
                          "forceSingleSided": tr["before"] < EPS, "at": tr["at"]})
        elif abs(tr["at"] - end) <= 1.5 * frame:
            found.append({"matchName": tr["matchName"], "name": tr["name"], "applyToStart": False,
                          "durationSeconds": round(tr["before"] + tr["after"], 6),
                          "forceSingleSided": tr["after"] < EPS, "at": tr["at"]})
    return found


def _param_diffs(old_comps, new_comps) -> tuple[list[dict], list[str]]:
    """Components/params (by match name + occurrence, param index) where the new clip differs."""
    out, missing = [], []
    seen = {}
    for oc in old_comps or []:
        n = seen.get(oc["matchName"], 0)
        seen[oc["matchName"]] = n + 1
        matches = [c for c in new_comps or [] if c["matchName"] == oc["matchName"]]
        if n >= len(matches):
            missing.append(oc["matchName"])
            continue
        nc = matches[n]
        new_by_idx = {p["index"]: p for p in nc["params"]}
        params = []
        for op in oc["params"]:
            np_ = new_by_idx.get(op["index"])
            if np_ is None or op.get("value") is None:
                continue
            if op.get("keyframed"):
                params.append({"index": op["index"], "value": op["value"], "keyframes": op.get("keyframes") or []})
            elif not _same(op["value"], np_.get("value")):
                params.append({"index": op["index"], "value": op["value"]})
        if params:
            out.append({"matchName": oc["matchName"], "params": params})
    return out, missing


def replace_clip(rpc, track_idx: int, at: float, new_item: str, *, src_in=None, src_out=None, atrack=None,
                 keep_effects=True, keep_transitions=True, dry_run=False, label="Agent replace",
                 media_duration=None, transitions_snapshot: dict | None = None) -> dict:
    """`transitions_snapshot`: a saved OTIO export to take the clip's transitions from
    (e.g. taken before an earlier, imperfect swap) instead of the live timeline."""
    seq = rpc("get_sequence")
    fps = seq.get("fps") or 30
    frame = 1.0 / fps
    old = _clip_under(rpc, track_idx, at)
    start, end = old["start"], old["end"]
    length = end - start

    items = {i["name"]: i for i in rpc("list_project_items")["items"]}
    if new_item not in items:
        raise ValueError(f'No project item named "{new_item}"')
    notes = []
    s_in = old["inPoint"] if src_in is None else float(src_in)
    s_out = s_in + length if src_out is None else float(src_out)
    dur = media_duration(items[new_item]["mediaPath"]) if media_duration else None
    if src_out is None and dur and s_out > dur + frame / 2:
        s_out, s_in = dur, max(0.0, dur - length)
        notes.append(f"source range ran past the new media's end ({dur:.3f}s) — shifted to {s_in:.3f}–{s_out:.3f}s "
                     "(a frame hold on the old clip is not carried over)")

    transitions = (_edge_transitions(rpc, track_idx, start, end, frame, transitions_snapshot)
                   if keep_transitions else [])
    unknown = [t["name"] for t in transitions if not t["matchName"]]
    if unknown:
        raise ValueError(f"Transitions without a match name in the OTIO export: {unknown}")
    if atrack is None:
        atrack = max(0, len(seq.get("audioTracks") or []) - 1)

    report = {"track": f"V{track_idx + 1}", "start": start, "end": end, "old": old["name"], "new": new_item,
              "source": [round(s_in, 4), round(s_out, 4)], "transitions": transitions, "notes": notes,
              "dryRun": dry_run}
    if dry_run:
        return report

    span = {"videoTracks": [track_idx], "audioTracks": [], "fromSeconds": start - frame / 4,
            "toSeconds": end + frame / 4, "mode": "inside"}
    hit = rpc("remove_items", dict(span, dryRun=True))["removed"]
    if len(hit) != 1 or hit[0]["name"] != old["name"]:
        raise ValueError(f"Removal range would take {[h['name'] for h in hit]} — not touching it")
    rpc("remove_items", dict(span, undoLabel=f"{label}: remove {old['name']}"))
    # A range covering the whole media is placed as the whole clip: setting the out point at the
    # very end gets clamped against the file's native duration and loses the last frame when the
    # item is interpreted at a different rate (e.g. a 60 fps file interpreted at 59.94).
    whole = dur is not None and s_in <= frame / 2 and s_out >= dur - 1.5 * frame
    place = {"itemName": new_item, "atSeconds": start, "videoTrack": track_idx, "audioTrack": atrack,
             "mode": "overwrite", "undoLabel": f"{label}: {new_item}"}
    if not whole:
        place.update(inSeconds=s_in, outSeconds=s_out)
    rpc("insert_clip", place)
    report["placedWhole"] = whole
    mid = start + length / 2

    if keep_effects:
        new = _clip_under(rpc, track_idx, mid)
        diffs, missing = _param_diffs(old["components"], new["components"])
        if missing:  # create the added filters first, then diff against their defaults
            rpc("set_params", {"videoTrack": track_idx, "atSeconds": mid,
                               "components": [{"matchName": m, "params": []} for m in missing],
                               "undoLabel": f"{label}: re-add effects"})
            new = _clip_under(rpc, track_idx, mid)
            diffs, missing = _param_diffs(old["components"], new["components"])
        if diffs:
            rpc("set_params", {"videoTrack": track_idx, "atSeconds": mid, "components": diffs,
                               "undoLabel": f"{label}: restore effect params"})
        report["restoredParams"] = [f"{c['matchName']}#{p['index']}" for c in diffs for p in c["params"]]

    for t in transitions:
        # Whole frames, plus a tenth of a frame so Premiere's tick rounding can't drop one.
        snapped = (round(t["durationSeconds"] * fps) + 0.1) / fps
        rpc("add_transition", {"videoTrack": track_idx, "atSeconds": mid, "matchName": t["matchName"],
                               "durationSeconds": snapped, "applyToStart": t["applyToStart"],
                               "forceSingleSided": t["forceSingleSided"],
                               "undoLabel": f"{label}: {t['name']} at {'start' if t['applyToStart'] else 'end'}"})

    # verify by reading everything back
    after = _clip_under(rpc, track_idx, mid)
    problems = []
    tol = 0.6 * frame  # under one frame: a one-frame slip is a real defect (gaps, flashes)
    want_end = start + (s_out - s_in)
    if after["name"] != new_item or abs(after["start"] - start) > tol or abs(after["end"] - want_end) > tol:
        problems.append(f"placement: got {after['name']} {after['start']:.3f}-{after['end']:.3f}, "
                        f"wanted {start:.3f}-{want_end:.3f}")
    if keep_effects:
        left, missing = _param_diffs(old["components"], after["components"])
        left = [f"{c['matchName']}#{p['index']}" for c in left for p in c["params"] if not p.get("keyframes")]
        if left or missing:
            problems.append(f"params still differ: {left} missing effects: {missing}")
    if transitions:
        now = _edge_transitions(rpc, track_idx, after["start"], after["end"], frame)
        for t in transitions:
            if not any(n["matchName"] == t["matchName"] and n["applyToStart"] == t["applyToStart"]
                       and abs(n["durationSeconds"] - t["durationSeconds"]) <= tol for n in now):
                problems.append(f"transition {t['name']} at {'start' if t['applyToStart'] else 'end'} not found after adding")
    report["verified"] = not problems
    report["problems"] = problems
    return report
