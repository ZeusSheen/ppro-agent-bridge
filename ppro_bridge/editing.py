"""
Razor-free editing: split_clip, cut_range and conform_captions.

Premiere 27.1 has no razor over UXP, and rebuilding a cut-down from source-range placements loses every clip
attribute (clip volume, scaling, effects). These edits are built from two panel primitives instead, so every piece
of a clip IS the clip, with all its attributes:
  * clone_clip — Premiere's track-item clone (the copy keeps effects, volume, scaling; linked items are not cloned,
    so video and audio are separate clips here, each handled on its own track);
  * trim_clip  — a new start / end (sequence seconds), or a move to an absolute new start.

split_clip        clone the clip into free timeline after the sequence end, trim the original's tail to the cut,
                  trim the copy's head to its own equivalent of the cut, move the copy to the cut.
cut_range         remove [start, end) from the chosen tracks (default all) and keep them in sync: a clip crossing
                  both edges is split at `end` (its left part trimmed to `start`), a clip crossing one edge is
                  trimmed, clips inside are removed (remove_items, mode inside, no ripple); then every clip at/after
                  `end` on those tracks moves left by the same amount, in ascending start order. Premiere's own
                  ripple works per track and desyncs tracks that have gaps; this does not. Markers are not moved
                  (the bridge cannot read them) — the report says so.
extend_cut        the opposite of cut_range: open the edit point at `at` up by out_by / in_by seconds of the clips'
                  own source (breathing room): everything after it moves right in one shift_items step, then the
                  clip ending there gets a longer tail and the clip starting there a longer head.
conform_captions  a pre-rendered caption movie cut into pieces, its lines from an SRT: trim heads and tails so a
                  line never flashes for a few frames at a cut.

All times are snapped to the sequence frame grid (fps from get_sequence's timebase). Every write carries an undo
label ("<label>: <step>"). Every step is read back and checked to half a frame, and after each phase the whole
timeline is compared with the expected result. A failure after the first write raises BridgeError with outcome
"partial": data.completed lists the undo labels that landed, data.parked where a parked copy still sits (so it can
be removed). dry_run=True plans the same steps against an in-memory model and writes nothing.
"""

import math
import re
from pathlib import Path

from . import framegrid, items
from .errors import BridgeError

CLONE_MARGIN = 10.0      # s of empty timeline between the sequence end and a parked copy
HEAD_GRACE_FRAMES = 2    # a caption piece may start this many frames into a cue and still count as a clean start
TAIL_FLASH = 0.5         # a line shown for less than this at a piece's start or end is a flash
VERIFY_CODE = "VERIFY_FAILED"  # a read-back after a write did not match (actions.json errors)
FIELDS = ("start", "end", "inPoint", "outPoint")


# ================================================================ the primitives: ASSUMED semantics
#
# Everything this module believes about clone_clip and trim_clip lives in this section: what to send for an intent,
# and what the clip looks like afterwards. Planning (also in a dry run) and read-back verification both use it, so a
# primitive that behaves differently is caught by the verification and fixed here.
# Live probe, Premiere 27.1.0 build 17:
#   A1 clone_clip {kind, track, atSeconds, offsetSeconds}: an exact copy of the one clip under atSeconds lands at
#      start + offset on the same track, same in/out (so same length), with all its components; it overwrites the
#      destination; linked clips are NOT cloned with it.                                       (confirmed)
#   A2 trim_clip start=s: head trim — start becomes s, inPoint moves by the same delta, end/outPoint stay.  (confirmed)
#   A3 trim_clip end=e: tail trim — end becomes e, outPoint moves by the same delta, start/inPoint stay.    (confirmed)
#   A4 trim_clip moveTo=m: m is the ABSOLUTE new start, length and in/out kept (the panel converts it to the delta
#      Premiere's createMoveAction wants and refuses moveTo combined with other fields).      (confirmed via panel)
#   A5 trim_clip inPoint/outPoint: NOT used here (inPoint turned out to be a head trim too, not "keep start").
#   A6 the clip under atSeconds: start <= at < end. This module always addresses a clip by its midpoint.
#   A7 speed 1 only: source and sequence deltas are equal. Speed-changed clips are refused before any write.

HEAD, TAIL, MOVE = "head", "tail", "move"


def trim_fields(op: str, value: float) -> dict:
    """The trim_clip fields for an intent: head trim to a new start, tail trim to a new end, move to a new start."""
    return {HEAD: {"start": value}, TAIL: {"end": value}, MOVE: {"moveTo": value}}[op]


def predict_trim(clip: dict, op: str, value: float) -> dict:
    """{start, end, inPoint, outPoint} of `clip` after trim_clip(trim_fields(op, value)) — A2/A3/A4."""
    s, e, i, o = (clip[k] for k in FIELDS)
    if op == HEAD:
        return {"start": value, "end": e, "inPoint": _add(i, value - s), "outPoint": o}
    if op == TAIL:
        return {"start": s, "end": value, "inPoint": i, "outPoint": _add(o, value - e)}
    if op == MOVE:
        return {"start": value, "end": e + (value - s), "inPoint": i, "outPoint": o}
    raise ValueError(f"unknown trim op {op!r}")


def predict_clone(clip: dict, offset: float) -> dict:
    """{start, end, inPoint, outPoint} of the copy clone_clip(offsetSeconds=offset) makes — A1."""
    return {"start": clip["start"] + offset, "end": clip["end"] + offset,
            "inPoint": clip["inPoint"], "outPoint": clip["outPoint"]}


def _add(t, delta):
    return None if t is None else t + delta


# ================================================================ small helpers

class _Grid:
    """The sequence frame grid: snapping and half-a-frame comparisons."""

    def __init__(self, fps: float):
        self.fps = float(fps)
        self.frame = 1.0 / self.fps
        self.tol = self.frame / 2

    def snap(self, t) -> float:
        return round(float(t) * self.fps) / self.fps

    def ceil(self, t) -> float:
        return math.ceil(float(t) * self.fps - 1e-4) / self.fps

    def floor(self, t) -> float:
        return math.floor(float(t) * self.fps + 1e-4) / self.fps

    def same(self, a, b) -> bool:
        return a is not None and b is not None and abs(a - b) <= self.tol


def _grid_of(seq: dict) -> _Grid:
    fps = framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or None)
    if not fps:
        raise BridgeError("get_sequence returned neither a timebase nor an fps; cannot snap to the frame grid.",
                          code="INTERNAL", outcome="not_committed")
    return _Grid(fps)


def _bad(message: str) -> BridgeError:
    return BridgeError(message, code="BAD_PARAMS", outcome="not_committed")


def _need_label(label):
    if not isinstance(label, str) or not label.strip():
        raise _bad("label is required: it names the undo steps in Premiere's history.")


def _label(kind: str, idx: int) -> str:
    return ("V" if kind == "video" else "A") + str(idx + 1)


def _order(key):
    return (0 if key[0] == "video" else 1, key[1])


def _track_key(value, kind=None) -> tuple[str, int]:
    """("video"|"audio", 0-based index) from "V3" / "A1", an int with `kind`, or a (kind, index) tuple."""
    if kind is not None and kind not in ("video", "audio"):
        raise _bad(f'kind must be "video" or "audio", not {kind!r}')
    if isinstance(value, tuple) and len(value) == 2:
        k, i = value
    elif isinstance(value, int) and not isinstance(value, bool):
        if kind is None:
            raise _bad(f"track {value} needs a kind (video or audio), or name it like V1 / A1")
        k, i = kind, value
    else:
        try:
            k, i = items.parse_track(value)
        except ValueError as e:
            raise _bad(str(e)) from e
    if k not in ("video", "audio") or not isinstance(i, int) or i < 0:
        raise _bad(f"bad track {value!r}")
    if kind is not None and k != kind:
        raise _bad(f"track {value!r} is not a {kind} track")
    return k, i


def _t(x: float) -> str:
    return f"{x:.3f}"


def _r(x):
    return None if x is None else round(x, 6)


def _view(clip: dict) -> dict:
    return {k: _r(clip.get(k)) for k in FIELDS}


def _span(clip: dict) -> str:
    i, o = clip.get("inPoint"), clip.get("outPoint")
    src = f" (source {_t(i)}-{_t(o)})" if i is not None and o is not None else ""
    return f"{clip.get('name')} {_t(clip['start'])}-{_t(clip['end'])}{src}"


def _mid(clip: dict) -> float:
    return (clip["start"] + clip["end"]) / 2


def _clip(it: dict) -> dict:
    return {"name": it.get("name"), "start": it.get("start"), "end": it.get("end"),
            "inPoint": it.get("inPoint"), "outPoint": it.get("outPoint"), "speed": it.get("speed", 1),
            "mediaPath": it.get("mediaPath")}


def _speed_changed(clip: dict) -> bool:
    sp = clip.get("speed")
    return isinstance(sp, (int, float)) and not isinstance(sp, bool) and abs(sp - 1) > 1e-6


def _matches(got: dict, want: dict, grid: _Grid) -> bool:
    """Same position and source range to half a frame (names are not compared: positions identify a clip)."""
    return all(want.get(k) is None or grid.same(want[k], got.get(k)) for k in FIELDS)


class _Timeline:
    """What the timeline should look like: list_items data, updated by every planned step."""

    def __init__(self, doc: dict):
        self.tracks: dict[tuple[str, int], list[dict]] = {}
        self.unreadable = 0
        for kind in ("video", "audio"):
            for t in doc.get(kind) or []:
                clips = []
                for it in t.get("items") or []:
                    if it.get("start") is None or it.get("end") is None:
                        self.unreadable += 1
                        continue
                    clips.append(_clip(it))
                self.tracks[(kind, t["index"])] = sorted(clips, key=lambda c: c["start"])

    def clips(self, key) -> list[dict]:
        return self.tracks.get(key, [])

    def end(self) -> float:
        return max((c["end"] for cs in self.tracks.values() for c in cs), default=0.0)

    def resort(self, key):
        self.tracks[key].sort(key=lambda c: c["start"])


def _diff(model: _Timeline, doc: dict, grid: _Grid) -> list[str]:
    """Where the timeline in `doc` (list_items, both kinds) differs from the model."""
    actual = _Timeline(doc)
    problems = []
    for key in sorted(set(model.tracks) | set(actual.tracks), key=_order):
        want, got = model.clips(key), actual.clips(key)
        if len(want) != len(got):
            problems.append(f"{_label(*key)} holds {len(got)} clip(s), expected {len(want)}: "
                            f"[{'; '.join(_span(c) for c in got[:8])}] vs [{'; '.join(_span(c) for c in want[:8])}]")
            continue
        for w, g in zip(want, got):
            if not _matches(g, w, grid):
                problems.append(f"{_label(*key)}: expected {_span(w)}, found {_span(g)}")
    return problems


class _Run:
    """One edit session: the rpc, the frame grid, the expected timeline and every write made so far."""

    def __init__(self, rpc, grid: _Grid, model: _Timeline, label: str, *, dry: bool, seq_end: float):
        self.rpc, self.grid, self.model, self.label, self.dry = rpc, grid, model, label.strip(), dry
        self.seq_end = seq_end
        self.steps: list[dict] = []      # every write attempted (planned, in a dry run)
        self.completed: list[str] = []   # undo labels of the writes that landed
        self.parked: list[tuple] = []    # (track key, clip) copies sitting in the parking area

    def write(self, action: str, params: dict, what: str) -> dict:
        undo = f"{self.label}: {what}"
        self.steps.append({"action": action, "undoLabel": undo})
        if self.dry:
            return {}
        try:
            reply = self.rpc(action, dict(params, undoLabel=undo))
        except BridgeError as e:
            if e.outcome == "unknown" and not e.hint:
                e.hint = f'The step "{undo}" may still have landed: list_items before retrying anything.'
            raise
        self.completed.append(undo)
        return reply or {}

    def verify(self, key, want: dict, after: dict | None = None):
        """One clip must now sit where the model says: the reply's own read-back, else a fresh list_items."""
        if self.dry or (after and _matches(after, want, self.grid)):
            return
        doc = self.rpc("list_items", {"kinds": [key[0]]})
        track = next((t for t in doc.get(key[0]) or [] if t.get("index") == key[1]), None)
        found = [_clip(it) for it in (track or {}).get("items") or [] if it.get("start") is not None]
        if any(_matches(c, want, self.grid) for c in found):
            return
        near = [c for c in found if c["end"] >= want["start"] - 5 and c["start"] <= want["end"] + 5] or found[:8]
        raise BridgeError(f"[verify] {_label(*key)}: expected {_span(want)}, but the track holds "
                          f"{'; '.join(_span(c) for c in near) or 'nothing there'}.", code=VERIFY_CODE,
                          data={"track": _label(*key), "expected": _view(want), "found": [_view(c) for c in near]})

    def check_all(self, phase: str):
        """The whole timeline (every track, both kinds) must match the model."""
        if self.dry:
            return
        problems = _diff(self.model, self.rpc("list_items", {"kinds": ["video", "audio"]}), self.grid)
        if problems:
            raise BridgeError(f"[verify] after {phase} the timeline differs from the expected result: "
                              + " | ".join(problems[:4]), code=VERIFY_CODE, data={"problems": problems})


def _partial(command: str, error: Exception, run: _Run, report: dict) -> BridgeError:
    data = {"completed": list(run.completed), "report": report}
    if run.steps:
        data["lastStep"] = run.steps[-1]["undoLabel"]
        data["lastStepLanded"] = len(run.steps) == len(run.completed)
    if run.parked:
        data["parked"] = [dict(track=_label(*key), **_view(c), name=c["name"]) for key, c in run.parked]
    if getattr(error, "outcome", None) not in (None, "not_committed"):
        data["failedStepOutcome"] = error.outcome  # "unknown": that step itself may have landed too
    if getattr(error, "data", None) is not None:
        data["errorData"] = error.data
    where = (" A parked copy is still at " + ", ".join(f"{p['track']} {_t(p['start'])}-{_t(p['end'])}s"
                                                       for p in data["parked"]) + " (remove it with remove_items)."
             if run.parked else "")
    return BridgeError(f"{command} stopped after {len(run.completed)} step(s). {error}{where} Undo the completed "
                       "steps in Premiere (Edit > Undo, see data.completed) or finish by hand.",
                       code=getattr(error, "code", None) or "INTERNAL", outcome="partial", data=data,
                       hint=getattr(error, "hint", None))


def _refuse_speed(entries, what: str):
    bad = [f"{_label(*key)} {c['name']} ({c['speed']:g}x)" for key, c in entries if _speed_changed(c)]
    if bad:
        raise BridgeError(f"Cannot {what} speed-changed clips (their source and sequence times do not map 1:1): "
                          + ", ".join(bad), code="UNSUPPORTED", outcome="not_committed")


# ================================================================ steps (each verified; the model follows)

def _trim(run: _Run, key, clip: dict, op: str, value: float, what: str):
    want = predict_trim(clip, op, value)
    reply = run.write("trim_clip", dict({"kind": key[0], "track": key[1], "atSeconds": _mid(clip)},
                                        **trim_fields(op, value)), what)
    run.verify(key, dict(want, name=clip["name"]), reply.get("after"))
    clip.update(want)
    run.model.resort(key)


def _clone(run: _Run, key, clip: dict, offset: float, what: str) -> dict:
    run.write("clone_clip", {"kind": key[0], "track": key[1], "atSeconds": _mid(clip), "offsetSeconds": offset},
              what)
    copy = dict(clip, **predict_clone(clip, offset))
    run.model.tracks[key].append(copy)
    run.model.resort(key)
    run.parked.append((key, copy))
    run.verify(key, copy)  # clone replies are computed, not read back: always re-list
    return copy


def _split(run: _Run, key, clip: dict, at: float, left_end: float | None = None) -> tuple[dict, dict]:
    """Split the model's `clip` at `at` (a frame on the grid, strictly inside it) -> (left, right).
    left_end: trim the left part to this instead of `at` (cut_range drops what lies between)."""
    grid, s0 = run.grid, clip["start"]
    left_end = at if left_end is None else left_end
    park = grid.ceil(max(run.model.end(), run.seq_end or 0.0) + CLONE_MARGIN)
    tag = f"split {_label(*key)} {clip['name']} at {_t(at)}s"
    copy = _clone(run, key, clip, park - s0, f"{tag} [1/4] clone to {_t(park)}s")
    _trim(run, key, clip, TAIL, left_end, f"{tag} [2/4] trim end to {_t(left_end)}s")
    _trim(run, key, copy, HEAD, copy["start"] + (at - s0), f"{tag} [3/4] trim head of the copy")
    _trim(run, key, copy, MOVE, at, f"{tag} [4/4] move the copy to {_t(at)}s")
    run.parked.remove((key, copy))
    run.check_all(tag)
    return clip, copy


def _clip_at(clips: list[dict], at: float, grid: _Grid):
    """(clip strictly containing `at`, clip with an edge at `at`) — at most one of them is set."""
    for c in clips:
        if grid.same(c["start"], at) or grid.same(c["end"], at):
            return None, c
    for c in clips:
        if c["start"] < at < c["end"]:
            return c, None
    return None, None


def _start(rpc, label: str, dry_run: bool):
    _need_label(label)
    seq = rpc("get_sequence")
    grid = _grid_of(seq)
    model = _Timeline(rpc("list_items", {"kinds": ["video", "audio"]}))
    run = _Run(rpc, grid, model, label, dry=bool(dry_run), seq_end=float(seq.get("durationSeconds") or 0.0))
    return run


def _notes(run: _Run) -> list[str]:
    if run.model.unreadable:
        return [f"{run.model.unreadable} timeline item(s) reported no start/end and were left out of the checks."]
    return []


# ================================================================ split_clip

def split_clip(rpc, *, kind=None, track, at, label, dry_run=False) -> dict:
    """Split the clip under `at` (sequence s, snapped to the frame grid) on one track, keeping every attribute.
    `track`: "V1" / "A2", or a 0-based index with `kind` ("video" / "audio"). At a clip edge it is a no-op."""
    key = _track_key(track, kind)
    run = _start(rpc, label, dry_run)
    at_s = run.grid.snap(at)
    if key not in run.model.tracks:
        raise BridgeError(f"{_label(*key)} does not exist.", code="NOT_FOUND", outcome="not_committed")
    report = {"kind": key[0], "track": _label(*key), "at": _r(at_s), "dryRun": run.dry, "notes": _notes(run)}
    clip, edge = _clip_at(run.model.clips(key), at_s, run.grid)
    if edge is not None:
        report.update(split=False, noop=True, name=edge["name"],
                      reason=f"{_t(at_s)}s is already an edge of {_span(edge)} on {_label(*key)}: nothing to split.")
        return report
    if clip is None:
        raise BridgeError(f"No clip on {_label(*key)} at {_t(at_s)}s.", code="NOT_FOUND", outcome="not_committed")
    _refuse_speed([(key, clip)], "split")
    report.update(name=clip["name"], clip=_view(clip))
    try:
        left, right = _split(run, key, clip, at_s)
    except Exception as e:
        if run.dry or not run.completed:
            raise
        raise _partial("split_clip", e, run, report) from e
    report.update(split=not run.dry, noop=False, left=_view(left), right=_view(right),
                  steps=[s["undoLabel"] for s in run.steps], undoSteps=len(run.steps))
    if not run.dry:
        report["verified"] = True
    return report


# ================================================================ cut_range

def cut_range(rpc, *, start, end, tracks=None, ripple=True, captions=None, label, dry_run=False) -> dict:
    """Remove [start, end) (sequence s, snapped) from `tracks` ("V1", "A2", … or (kind, index); default every
    track) keeping them in sync; with ripple, everything at/after `end` on those tracks moves left by end − start.
    `captions`: {"track": "V3", "srt_path": "...", "offset": 0.0} conforms that caption track afterwards
    (see conform_captions)."""
    run = _start(rpc, label, dry_run)
    grid, model = run.grid, run.model
    a, b = grid.snap(start), grid.snap(end)
    if b - a < grid.frame - 1e-9:
        raise _bad(f"end ({_t(b)}s) must be at least one frame after start ({_t(a)}s) on the frame grid.")
    keys = _selected(model, tracks)
    cap = _captions_cfg(captions)
    if cap and cap[0] not in model.tracks:
        raise BridgeError(f"Caption track {_label(*cap[0])} does not exist.", code="NOT_FOUND", outcome="not_committed")
    shift, tol = b - a, grid.tol

    splits, tails, heads, inside, after = [], [], [], [], []
    for key in keys:
        for c in model.clips(key):
            s, e = c["start"], c["end"]
            if e <= a + tol:
                continue
            if s >= b - tol:
                after.append((key, c))
            elif s < a - tol and e > b + tol:
                splits.append((key, c))
            elif s < a - tol:
                tails.append((key, c))
            elif e > b + tol:
                heads.append((key, c))
            else:
                inside.append((key, c))
    _refuse_speed(splits + tails + heads, "split or trim")
    selected = set(keys)
    not_moved = [{"track": _label(*key), "name": c["name"], "start": _r(c["start"])}
                 for key in sorted(model.tracks, key=_order) if key not in selected
                 for c in model.clips(key) if c["start"] >= b - tol] if ripple else []

    report = {"dryRun": run.dry, "start": _r(a), "end": _r(b), "removedSeconds": _r(shift), "ripple": bool(ripple),
              "tracks": [_label(*k) for k in keys], "splits": [], "trims": [], "removals": [], "moves": [],
              "notMoved": not_moved, "notes": _notes(run),
              "markers": (f"Sequence markers are not moved (the bridge cannot read them): markers after {_t(b)}s now "
                          f"sit {_t(shift)}s late; move them in Premiere." if ripple else
                          "Sequence markers are untouched.")}
    if not_moved:
        report["notes"].append(f"{len(not_moved)} clip(s) after the cut on tracks outside `tracks` were not moved and "
                               "are now out of sync with the moved ones (see notMoved).")
    try:
        for key, c in splits:
            span = [_r(c["start"]), _r(c["end"])]
            _, right = _split(run, key, c, b, left_end=a)
            report["splits"].append({"track": _label(*key), "name": c["name"], "clip": span, "at": _r(b),
                                     "left": [_r(c["start"]), _r(a)], "right": [_r(b), span[1]]})
            after.append((key, right))
        for key, c in tails:
            report["trims"].append({"track": _label(*key), "name": c["name"], "edge": "tail",
                                    "from": [_r(c["start"]), _r(c["end"])], "to": [_r(c["start"]), _r(a)]})
            _trim(run, key, c, TAIL, a, f"trim {_label(*key)} {c['name']} end to {_t(a)}s")
        for key, c in heads:
            report["trims"].append({"track": _label(*key), "name": c["name"], "edge": "head",
                                    "from": [_r(c["start"]), _r(c["end"])], "to": [_r(b), _r(c["end"])]})
            _trim(run, key, c, HEAD, b, f"trim {_label(*key)} {c['name']} head to {_t(b)}s")
            after.append((key, c))
        if inside:
            report["removals"] = [dict(track=_label(*key), name=c["name"], start=_r(c["start"]), end=_r(c["end"]))
                                  for key, c in inside]
            _remove(run, inside, a, b)
        if ripple and after:
            ordered = sorted(after, key=lambda kc: (kc[1]["start"], _order(kc[0])))
            for key, c in ordered:
                report["moves"].append({"track": _label(*key), "name": c["name"], "from": _r(c["start"]),
                                        "to": _r(grid.snap(c["start"] - shift))})
            _ripple(run, keys, ordered, b, shift)
        if cap:
            report["captions"] = _conform(run, *cap)
        run.check_all("cut_range")
    except Exception as e:
        if run.dry or not run.completed:
            raise
        raise _partial("cut_range", e, run, report) from e
    report["steps"] = [s["undoLabel"] for s in run.steps]
    report["undoSteps"] = len(run.steps)
    if not run.dry:
        report["verified"] = True
    return report


# ================================================================ extend_cut

SILENT_DB = -60.0         # a held Volume > Level at or below this is silence
INVISIBLE_OPACITY = 1.0   # a held Opacity (%) at or below this is invisible


def _level_db(level: float) -> float:
    """Volume > Level (linear in UXP) as the dB Premiere shows: 20·log10(level) + 15 (see ops.db_from_level)."""
    return 20 * math.log10(level) + 15.0 if level > 0 else float("-inf")


def held_keyframes(rpc, grid: _Grid, todo: list) -> list[dict]:
    """Keyframed Volume > Level and Opacity on the clips extend_cut is about to make longer. `todo`: [(key, clip,
    edge)] with edge "tail" or "head". Keyframes are media time and stay where they are, so source added after the
    last keyframe (tail) or before the first (head) holds that keyframe's value: a 2-frame fade-out to 0 at the old
    end leaves the whole extension silent (seen on a real job). One entry per such param; `silent` when the held
    value is silence (Level <= SILENT_DB) or invisible (Opacity <= INVISIBLE_OPACITY)."""
    if not todo:
        return []
    params = {"fromSeconds": min(c["start"] for _, c, _ in todo), "toSeconds": max(c["end"] for _, c, _ in todo),
              "maxKeyframes": 1000}
    for kind, name in (("video", "videoTracks"), ("audio", "audioTracks")):
        idx = sorted({k[1] for k, _, _ in todo if k[0] == kind})
        if idx:
            params[name] = idx
    doc = rpc("inspect_items", params)
    chains = {((t.get("kind") or "video"), t["index"]): t.get("clips") or [] for t in doc.get("tracks") or []}
    out = []
    for key, c, edge in todo:
        got = next((x for x in chains.get(key, []) if grid.same(x["start"], c["start"]) and grid.same(x["end"], c["end"])),
                   None)
        for comp in (got or {}).get("components") or []:
            match = str(comp.get("matchName", ""))
            if match.startswith("Internal Volume"):
                ps = comp.get("params") or []
                p = next((x for x in ps if x.get("name") == "Level"), None) or next(
                    (x for x in ps if x.get("index") == 1), None)
                what = "Volume > Level"
            elif match == "AE.ADBE Opacity":
                p = next((x for x in comp.get("params") or [] if x.get("index") == 0), None)
                what = "Opacity"
            else:
                continue
            kfs = sorted((k for k in (p or {}).get("keyframes") or [] if isinstance(k.get("t"), (int, float))),
                         key=lambda k: k["t"])
            if not (p or {}).get("keyframed") or not kfs:
                continue
            entry = {"track": _label(*key), "name": c["name"], "edge": edge, "param": what}
            if p.get("keyframesTruncated"):
                out.append(dict(entry, silent=None, reason=f"{p['keyframesTruncated']} keyframes; not checked"))
                continue
            if edge == "tail" and kfs[-1]["t"] <= c["outPoint"] + grid.tol:
                kf = kfs[-1]
            elif edge == "head" and kfs[0]["t"] >= c["inPoint"] - grid.tol:
                kf = kfs[0]
            else:
                continue  # keyframes run on into the added source: it follows them
            value = kf["value"].get("value") if isinstance(kf["value"], dict) else kf["value"]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            entry.update(keyframe=_r(kf["t"]), heldValue=value)
            if what == "Opacity":
                entry["silent"] = value <= INVISIBLE_OPACITY
            else:
                db = _level_db(value)
                entry.update(heldDb=round(db, 2) if math.isfinite(db) else "-inf", silent=db <= SILENT_DB)
            out.append(entry)
    return out


def extend_cut(rpc, *, at, out_by=0.0, in_by=0.0, tracks=None, extend=None, captions=None, label, dry_run=False,
               media_seconds=None, allow_silent=False) -> dict:
    """Open up the edit point at `at` (sequence s, snapped) — breathing room, the opposite of cut_range.

    Everything at/after `at` on `tracks` (default every track) moves right by out_by + in_by in one shift_items
    step; then, on the `extend` tracks, the clip that ENDS at `at` gets out_by more of its own source after its out
    point and the clip that STARTS there in_by more before its in point. `extend` defaults to every selected track
    with a clip edge at `at`; a clip without that much source (in point near 0, out point near the media's end)
    is then left alone and listed under `gaps`. Named `extend` tracks must have the source, or nothing is done.
    Clips crossing `at` (music under the cut, a title) are not touched. `media_seconds(clip) -> float | None`
    gives a clip's media length for the out-point check (None: Premiere checks it). Values snap to whole frames.
    An extension that would hold a keyframed Level at silence or an Opacity at 0 (see held_keyframes) is listed
    under `heldKeyframes` and refused, before anything is written, unless allow_silent."""
    run = _start(rpc, label, dry_run)
    grid, model = run.grid, run.model
    t0 = grid.snap(at)
    try:
        out_s, in_s = grid.snap(float(out_by or 0.0)), grid.snap(float(in_by or 0.0))
    except (TypeError, ValueError):
        raise _bad(f"out_by / in_by must be numbers of seconds, not {out_by!r} / {in_by!r}") from None
    if out_s < 0 or in_s < 0:
        raise _bad("out_by and in_by open a cut up and must be >= 0 (cut_range tightens one).")
    delta = out_s + in_s
    if delta < grid.frame - 1e-9:
        raise _bad(f"out_by + in_by must add at least one frame ({_t(grid.frame)}s) once snapped to whole frames.")
    keys = _selected(model, tracks)
    named = None if extend is None else _selected(model, extend)
    if named and set(named) - set(keys):
        raise _bad("extend tracks must also be in tracks: " + ", ".join(_label(*k) for k in set(named) - set(keys)))
    cap = _captions_cfg(captions)
    tol = grid.tol

    left, right, crossing, after = {}, {}, [], []
    for key in keys:
        for c in model.clips(key):
            if grid.same(c["end"], t0):
                left[key] = c
            if grid.same(c["start"], t0):
                right[key] = c
            if c["start"] >= t0 - tol:
                after.append((key, c))
            elif c["end"] > t0 + tol:
                crossing.append((key, c))
    if not left and not right:
        raise BridgeError(f"No clip starts or ends at {_t(t0)}s on {', '.join(_label(*k) for k in keys)}: not an "
                          "edit point.", code="NOT_FOUND", outcome="not_committed")
    if named:
        missing = [_label(*k) for k in named if not ((out_s and k in left) or (in_s and k in right))]
        if missing:
            raise BridgeError(f"{', '.join(missing)}: no clip edge at {_t(t0)}s to extend.", code="NOT_FOUND",
                              outcome="not_committed")
    pool = named if named is not None else keys

    def short(key, c, edge):
        if _speed_changed(c):
            return f"speed-changed ({c['speed']:g}x)"
        if edge == "tail":
            length = media_seconds(c) if media_seconds else None
            if length is not None and c.get("outPoint") is not None and c["outPoint"] + out_s > length + tol:
                return f"its media ends {_t(length - c['outPoint'])}s after the out point"
        elif c.get("inPoint") is None or c["inPoint"] - in_s < -tol:
            return f"only {_t(max(0.0, c.get('inPoint') or 0.0))}s of source before its in point"
        return None

    tails, heads, gaps, refused = [], [], [], []
    for key in pool:
        for edge, side, amount, todo in (("tail", left, out_s, tails), ("head", right, in_s, heads)):
            c = side.get(key)
            if c is None or not amount:
                continue
            why = short(key, c, edge)
            if why is None:
                todo.append((key, c))
            elif named is not None:
                refused.append(f"{_label(*key)} {c['name']} ({edge}): {why}")
            else:
                gaps.append({"track": _label(*key), "name": c["name"], "edge": edge, "reason": why})
    if refused:
        raise BridgeError("Cannot extend: " + "; ".join(refused) + " (nothing was changed).", code="UNSUPPORTED",
                          outcome="not_committed", data={"refused": refused})
    held = held_keyframes(rpc, grid, [(k, c, "tail") for k, c in tails] + [(k, c, "head") for k, c in heads])
    silent = [h for h in held if h.get("silent")]
    if silent and not run.dry and not allow_silent:
        what = "; ".join(f"{h['track']} {h['name']} ({h['edge']}): {h['param']} holds its keyframe at "
                         f"{h['keyframe']}s" for h in silent)
        raise BridgeError(f"The extension would be silent or invisible: {what}. Keyframes stay at their media time, "
                          "so the added source holds the fade's last value. Move or delete those keyframes in "
                          "Premiere first, or pass --allow-silent; nothing was changed.", code="KEYFRAMED",
                          outcome="not_committed", data={"heldKeyframes": held})
    for key in keys:
        if key in pool:
            continue
        for edge, side, amount in (("tail", left, out_s), ("head", right, in_s)):
            if side.get(key) is not None and amount:
                gaps.append({"track": _label(*key), "name": side[key]["name"], "edge": edge,
                             "reason": "not in extend"})

    report = {"dryRun": run.dry, "at": _r(t0), "outBy": _r(out_s), "inBy": _r(in_s), "addedSeconds": _r(delta),
              "tracks": [_label(*k) for k in keys], "extended": [], "gaps": gaps, "moves": [],
              "crossing": [{"track": _label(*k), "name": c["name"], "start": _r(c["start"]), "end": _r(c["end"])}
                           for k, c in crossing],
              "notes": _notes(run),
              "markers": f"Sequence markers are not moved (the bridge cannot read them): markers after {_t(t0)}s now "
                         f"sit {_t(delta)}s early; move them in Premiere."}
    if gaps:
        report["notes"].append(f"{len(gaps)} clip edge(s) at the cut were not extended, so those tracks have a "
                               f"{_t(delta)}s gap there (see gaps).")
    if held:
        report["heldKeyframes"] = held
    if silent:
        report["notes"].append(f"{len(silent)} extension(s) hold a fade's keyframe at silence or 0 opacity (see "
                               "heldKeyframes)" + (": allowed by --allow-silent." if allow_silent else
                                                   "; a real run refuses unless --allow-silent."))
    selected = set(keys)
    not_moved = [{"track": _label(*key), "name": c["name"], "start": _r(c["start"])}
                 for key in sorted(model.tracks, key=_order) if key not in selected
                 for c in model.clips(key) if c["start"] >= t0 - tol]
    if not_moved:
        report["notMoved"] = not_moved
        report["notes"].append(f"{len(not_moved)} clip(s) after the cut on tracks outside `tracks` were not moved "
                               "and are now out of sync (see notMoved).")
    try:
        if after:
            ordered = sorted(after, key=lambda kc: (-kc[1]["start"], _order(kc[0])))
            for key, c in ordered:
                report["moves"].append({"track": _label(*key), "name": c["name"], "from": _r(c["start"]),
                                        "to": _r(grid.snap(c["start"] + delta))})
            _shift(run, keys, ordered, t0, delta)
        for key, c in tails:
            new_end = t0 + out_s
            report["extended"].append({"track": _label(*key), "name": c["name"], "edge": "tail",
                                       "from": [_r(c["start"]), _r(c["end"])], "to": [_r(c["start"]), _r(new_end)]})
            _trim(run, key, c, TAIL, new_end, f"extend {_label(*key)} {c['name']} end to {_t(new_end)}s")
        for key, c in heads:
            new_start = t0 + out_s  # the clip now starts at t0 + delta; its head comes back by in_s
            report["extended"].append({"track": _label(*key), "name": c["name"], "edge": "head",
                                       "from": [_r(c["start"]), _r(c["end"])], "to": [_r(new_start), _r(c["end"])]})
            _trim(run, key, c, HEAD, new_start, f"extend {_label(*key)} {c['name']} head to {_t(new_start)}s")
        if cap:
            report["captions"] = _conform(run, *cap)
        run.check_all("extend_cut")
    except Exception as e:
        if run.dry or not run.completed:
            raise
        raise _partial("extend_cut", e, run, report) from e
    report["steps"] = [s["undoLabel"] for s in run.steps]
    report["undoSteps"] = len(run.steps)
    if not run.dry:
        report["verified"] = True
    return report


# ================================================================ snap_edges

SNAP_TOLERANCE = 0.5e-6  # s: an edge further than this from its frame is off the grid (lint OFF_GRID)


def snap_edges(rpc, *, tracks=None, label, dry_run=False) -> dict:
    """Put clip edges that drifted off the frame grid back on it (lint OFF_GRID): an edge a microsecond past a frame
    boundary makes Premiere show that clip for the whole next frame, so the picture cuts a frame after the sound.

    Per track, left to right: a clip whose start is off the grid moves onto its nearest frame (in/out unchanged, so
    it still shows the same source frames), then an end still off the grid is trimmed onto its frame. Needs a panel
    that writes exact frame ticks (0.4.0 with the frame fix): an older one lands every edge a microsecond late again,
    and the read-back reports that (`residual`). One undo step per write."""
    run = _start(rpc, label, dry_run)
    grid, model = run.grid, run.model
    keys = _selected(model, tracks)

    def off(t):
        return t - grid.snap(t)

    plan = []
    for key in keys:
        for c in model.clips(key):
            if abs(off(c["start"])) > SNAP_TOLERANCE or abs(off(c["end"])) > SNAP_TOLERANCE:
                plan.append((key, c))
    _refuse_speed(plan, "snap")
    report = {"dryRun": run.dry, "tracks": [_label(*k) for k in keys], "clips": len(plan), "moved": [], "trimmed": [],
              "notes": _notes(run)}
    try:
        for key, c in plan:  # model.clips() is sorted by start, so each track goes left to right
            if abs(off(c["start"])) > SNAP_TOLERANCE:
                to = grid.snap(c["start"])
                report["moved"].append({"track": _label(*key), "name": c["name"], "from": c["start"], "to": _r(to)})
                _trim(run, key, c, MOVE, to, f"snap {_label(*key)} {c['name']} start to frame {round(to * grid.fps)}")
            if abs(off(c["end"])) > SNAP_TOLERANCE:
                to = grid.snap(c["end"])
                report["trimmed"].append({"track": _label(*key), "name": c["name"], "from": c["end"], "to": _r(to)})
                _trim(run, key, c, TAIL, to, f"snap {_label(*key)} {c['name']} end to frame {round(to * grid.fps)}")
        run.check_all("snap_edges")
    except Exception as e:
        if run.dry or not run.completed:
            raise
        raise _partial("snap_edges", e, run, report) from e
    report["steps"] = [s["undoLabel"] for s in run.steps]
    report["undoSteps"] = len(run.steps)
    if run.dry:
        return report
    # check_all compares to half a frame; the point here is microseconds
    doc = rpc("list_items", {"kinds": ["video", "audio"]})
    selected = set(keys)
    report["residual"] = [{"track": t.get("label"), "name": it.get("name"), "edge": edge,
                           "offsetMicroseconds": round(off(it[edge]) * 1e6, 2)}
                          for kind in ("video", "audio") for t in doc.get(kind) or []
                          if (kind, t.get("index")) in selected for it in t.get("items") or []
                          for edge in ("start", "end") if _num_edge(it.get(edge)) and abs(off(it[edge])) > SNAP_TOLERANCE]
    if report["residual"]:
        worst = max(abs(r["offsetMicroseconds"]) for r in report["residual"])
        raise BridgeError(f"[verify] {len(report['residual'])} edge(s) are still off the frame grid after snapping "
                          f"(largest {worst} us): the loaded panel still nudges sequence times - Reload it.",
                          code=VERIFY_CODE, outcome="committed", data=report)
    report["verified"] = True
    return report


def _num_edge(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _shift(run: _Run, keys, ordered, frm: float, delta: float):
    """One shift_items step: every clip at/after `frm` on the selected tracks moves by `delta` (the model follows)."""
    grid = run.grid
    vt = sorted(k[1] for k in keys if k[0] == "video")
    at = sorted(k[1] for k in keys if k[0] == "audio")
    run.write("shift_items", {"fromSeconds": frm, "deltaSeconds": delta, "videoTracks": vt, "audioTracks": at},
              f"move {len(ordered)} clip(s) {'right' if delta > 0 else 'left'} {_t(abs(delta))}s")
    for key, c in ordered:
        c.update(start=grid.snap(c["start"] + delta), end=grid.snap(c["end"] + delta))
    for key in {k for k, _ in ordered}:
        run.model.resort(key)


def _ripple(run: _Run, keys, ordered, b: float, shift: float):
    """Move every clip at/after `b` on the selected tracks left by `shift`: one shift_items transaction (one undo
    step); a panel without shift_items gets one trim_clip moveTo per clip, earliest first."""
    grid = run.grid
    vt = sorted(k[1] for k in keys if k[0] == "video")
    at = sorted(k[1] for k in keys if k[0] == "audio")
    try:
        run.write("shift_items", {"fromSeconds": b, "deltaSeconds": -shift, "videoTracks": vt, "audioTracks": at},
                  f"ripple {len(ordered)} clip(s) left {_t(shift)}s")
    except BridgeError as e:
        if e.code != "UNKNOWN_ACTION":
            raise
        run.steps.pop()  # not sent: fall back to per-clip moves
        for key, c in ordered:
            old = c["start"]
            _trim(run, key, c, MOVE, grid.snap(old - shift), f"ripple {_label(*key)} {c['name']} {_t(old)}s to "
                                                             f"{_t(grid.snap(old - shift))}s")
        return
    for key, c in ordered:
        c.update(start=grid.snap(c["start"] - shift), end=grid.snap(c["end"] - shift))
    for key in {k for k, _ in ordered}:
        run.model.resort(key)


def _selected(model: _Timeline, tracks) -> list[tuple[str, int]]:
    if tracks is None:
        return sorted(model.tracks, key=_order)
    if isinstance(tracks, (str, tuple)):
        tracks = [tracks]
    keys = []
    for t in tracks:
        key = _track_key(t)
        if key not in model.tracks:
            raise BridgeError(f"{_label(*key)} does not exist.", code="NOT_FOUND", outcome="not_committed")
        if key not in keys:
            keys.append(key)
    if not keys:
        raise _bad("tracks is empty; pass None for every track")
    return sorted(keys, key=_order)


def _remove(run: _Run, inside: list, a: float, b: float):
    """remove_items (mode inside, never ripple) over [a, b]: its dry run must hit exactly the planned clips."""
    grid = run.grid
    span = {"fromSeconds": a - grid.frame / 4, "toSeconds": b + grid.frame / 4, "mode": "inside"}
    vts = sorted({key[1] for key, _ in inside if key[0] == "video"})
    ats = sorted({key[1] for key, _ in inside if key[0] == "audio"})
    if vts:
        span["videoTracks"] = vts
    if ats:
        span["audioTracks"] = ats
    if not run.dry:
        hit = run.rpc("remove_items", dict(span, dryRun=True)).get("removed") or []
        left = [(_label(*key), c) for key, c in inside]
        for h in hit:
            m = next((x for x in left if x[0] == h.get("track") and grid.same(x[1]["start"], h.get("start"))), None)
            if m is None:
                left = None
                break
            left.remove(m)
        if left is None or left:
            raise BridgeError(f"The removal over {_t(a)}-{_t(b)}s would take "
                              f"{[(h.get('track'), h.get('name'), _r(h.get('start'))) for h in hit]}, not the planned "
                              f"{[(_label(*k), c['name'], _r(c['start'])) for k, c in inside]} — not removing anything.",
                              code="PRECONDITION_FAILED", outcome="not_committed")
    reply = run.write("remove_items", span, f"remove {len(inside)} clip(s) {_t(a)}-{_t(b)}s")
    if not run.dry and len(reply.get("removed") or []) != len(inside):
        raise BridgeError(f"[verify] remove_items reported {len(reply.get('removed') or [])} removed clip(s), "
                          f"expected {len(inside)}.", code=VERIFY_CODE)
    for key, c in inside:
        run.model.tracks[key].remove(c)
    run.check_all("the removal")


# ================================================================ captions

_SRT_TIME = r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
_SRT_TIMING = re.compile(rf"^\s*{_SRT_TIME}\s*-->\s*{_SRT_TIME}")


def _srt_seconds(h, m, s, frac) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[dict]:
    """SRT text -> cues [{index, start, end, text}] (seconds), sorted by start. Tolerates a BOM, CRLF, "." as the
    millisecond separator, position specs after the timing and missing index lines."""
    cues, cur, prev = [], None, ""
    for line in text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = _SRT_TIMING.match(line)
        if m:
            g = m.groups()
            index = int(prev.strip()) if prev.strip().isdigit() else len(cues) + 1
            cur = {"index": index, "start": _srt_seconds(*g[:4]), "end": _srt_seconds(*g[4:]), "text": ""}
            cues.append(cur)
        elif not line.strip():
            cur = None
        elif cur is not None:
            cur["text"] = (cur["text"] + "\n" + line.strip()).strip()
        prev = line
    cues.sort(key=lambda c: (c["start"], c["end"]))
    return cues


def read_srt(path) -> list[dict]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise BridgeError(f"SRT file not found: {p}", code="FILE_NOT_FOUND", outcome="not_committed")
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise BridgeError(f"Cannot read {p.name} as UTF-8 text: {e}", code="IO_ERROR", outcome="not_committed") from e
    cues = parse_srt(text)
    if not cues:
        raise _bad(f"{p.name} contains no SRT cues (expected lines like 00:00:01,000 --> 00:00:03,500).")
    return cues


def _captions_cfg(captions):
    if captions is None:
        return None
    if not isinstance(captions, dict):
        raise _bad('captions must be {"track": "V3", "srt_path": "...", "offset": 0.0}')
    unknown = set(captions) - {"track", "srt_path", "srt", "offset"}
    if unknown:
        raise _bad(f"captions: unknown keys {sorted(unknown)}")
    srt = captions.get("srt_path") or captions.get("srt")
    if captions.get("track") is None or not srt:
        raise _bad("captions needs a track and an srt_path")
    return _track_key(captions["track"], "video"), read_srt(srt), float(captions.get("offset") or 0.0), str(srt)


def _cue_view(cue: dict | None) -> dict | None:
    if cue is None:
        return None
    text = cue["text"].replace("\n", " ")
    return {"index": cue["index"], "start": cue["start"], "end": cue["end"],
            "text": text if len(text) <= 60 else text[:57] + "..."}


def _piece_fixes(piece: dict, cues: list[dict], grid: _Grid, offset: float):
    """([(op, new sequence time, change info)], flag or None) for one caption piece. Cue times are SRT times; the
    movie's source time is SRT time − offset."""
    name, s, e, i, o = piece["name"], piece["start"], piece["end"], piece.get("inPoint"), piece.get("outPoint")
    if _speed_changed(piece):
        return [], {"piece": name, "at": _r(s), "reason": "speed-changed piece: source and cue times do not map 1:1"}
    if i is None or o is None:
        return [], {"piece": name, "at": _r(s), "reason": "the piece reported no in/out point"}
    tol, frame = grid.tol, grid.frame

    def src(t):
        return t - offset

    fixes, flag = [], None
    new_s, new_i = s, i
    # head: the piece starts inside a cue that began more than HEAD_GRACE_FRAMES before it and shows less than
    # TAIL_FLASH of that line -> it would flash (a line that still runs on for longer is simply joined in progress)
    cur = next((k for k, c in enumerate(cues)
                if i - src(c["start"]) > HEAD_GRACE_FRAMES * frame + 1e-6 and src(c["end"]) > i + tol
                and min(src(c["end"]), o) - i < TAIL_FLASH), None)
    if cur is not None:
        c = cues[cur]
        nxt = next((n for n in cues[cur + 1:] if src(n["start"]) > i + tol), None)
        if nxt is not None and src(nxt["start"]) <= o - frame:
            target, until = src(nxt["start"]), f"cue {nxt['index']}'s start"
        elif src(c["end"]) <= o - frame:
            target, until, nxt = src(c["end"]), f"the end of cue {c['index']}", None
        else:
            target = None
        if target is not None:
            cand = grid.ceil(s + (target - i))
            if cand > e - frame + 1e-9:
                target = None
        if target is None:
            flag = {"piece": name, "at": _r(s), "cue": _cue_view(c),
                    "reason": f"starts inside cue {c['index']} and shows nothing else; left alone (re-cut or remove "
                              "it by hand)"}
        else:
            new_s = cand
            new_i = i + (new_s - s)
            frames = round((i - src(c["start"])) * grid.fps)
            info = {"piece": name, "at": _r(s), "edge": "head", "from": _view(piece),
                    "to": _view(dict(piece, start=new_s, inPoint=new_i)), "trimmedSeconds": _r(new_s - s),
                    "trimmedFrames": round((new_s - s) * grid.fps), "cue": _cue_view(c), "until": _cue_view(nxt),
                    "reason": f"starts {frames} frame(s) into cue {c['index']}, whose line would flash; head trimmed "
                              f"to {until}"}
            fixes.append((HEAD, new_s, info))
    # tail: the piece ends inside a cue that started less than TAIL_FLASH before its end -> that line would flash
    ends_in = [c for c in cues if new_i + tol < src(c["start"]) < o - tol and src(c["end"]) > o + tol
               and o - src(c["start"]) < TAIL_FLASH]
    if ends_in:
        c = max(ends_in, key=lambda c: c["start"])
        # The movie first shows the cue's line on the first frame starting at or after the cue start (measured on
        # 27.1 with a rendered caption movie), so the tail keeps every frame that starts before it: snap UP.
        new_e = grid.ceil(e - (o - src(c["start"])))
        if new_e >= e - tol:
            pass  # the line would start on no frame of this piece: nothing flashes
        elif new_e - new_s < frame - 1e-9:
            flag = flag or {"piece": name, "at": _r(s), "cue": _cue_view(c),
                            "reason": f"cue {c['index']} starts in its last {o - src(c['start']):.2f}s but trimming "
                                      "would leave less than a frame; left alone"}
        else:
            before = dict(piece, start=new_s, inPoint=new_i)
            fixes.append((TAIL, new_e, {
                "piece": name, "at": _r(s), "edge": "tail", "from": _view(before),
                "to": _view(dict(before, end=new_e, outPoint=o + (new_e - e))), "trimmedSeconds": _r(e - new_e),
                "trimmedFrames": round((e - new_e) * grid.fps), "cue": _cue_view(c),
                "reason": f"cue {c['index']} starts {o - src(c['start']):.3f}s before the piece ends, so its line "
                          "would flash; tail trimmed to its start"}))
    return fixes, flag


def plan_caption_fixes(pieces: list[dict], cues: list[dict], *, fps: float, offset: float = 0.0) -> dict:
    """Pure: the head/tail trims conform_captions would make on `pieces` (list_items items of the captions track)
    for `cues` (parse_srt), as {"changes": [...], "flagged": [...]}."""
    grid = _Grid(fps)
    changes, flagged = [], []
    for p in sorted((_clip(p) for p in pieces if p.get("start") is not None), key=lambda c: c["start"]):
        fixes, flag = _piece_fixes(p, cues, grid, offset)
        changes += [info for _, _, info in fixes]
        if flag:
            flagged.append(flag)
    return {"changes": changes, "flagged": flagged}


def _conform(run: _Run, key, cues: list[dict], offset: float, srt: str) -> dict:
    pieces = list(run.model.clips(key))
    report = {"track": _label(*key), "srt": srt, "offset": offset, "cues": len(cues), "pieces": len(pieces),
              "changes": [], "flagged": [], "dryRun": run.dry}
    for piece in pieces:
        fixes, flag = _piece_fixes(piece, cues, run.grid, offset)
        if flag:
            report["flagged"].append(flag)
        for op, value, info in fixes:
            old = piece["start"] if op == HEAD else piece["end"]
            report["changes"].append(info)
            _trim(run, key, piece, op, value,
                  f"caption {piece['name']} on {_label(*key)}: {info['edge']} {_t(old)}s to {_t(value)}s")
    return report


def conform_captions(rpc, *, track, srt_path, offset=0.0, dry_run=False, label) -> dict:
    """Trim the pieces of a pre-rendered caption movie on `track` ("V3" or a video track index) so no SRT line
    flashes at a cut: a piece that starts more than HEAD_GRACE_FRAMES into a cue and would show less than
    TAIL_FLASH of that line gets its head trimmed to the next cue start (a line with longer to run is joined in
    progress, untouched); a piece ending less than TAIL_FLASH after a cue started gets its tail trimmed to that cue's
    start.
    SRT time = the movie's source time + offset. Every change is reported; pieces that cannot be fixed by a trim are
    listed under flagged."""
    key = _track_key(track, "video")
    cues = read_srt(srt_path)
    run = _start(rpc, label, dry_run)
    if key not in run.model.tracks:
        raise BridgeError(f"{_label(*key)} does not exist.", code="NOT_FOUND", outcome="not_committed")
    try:
        report = _conform(run, key, cues, float(offset or 0.0), str(srt_path))
        run.check_all("conform_captions")
    except Exception as e:
        if run.dry or not run.completed:
            raise
        raise _partial("conform_captions", e, run, {"track": _label(*key), "srt": str(srt_path)}) from e
    report["notes"] = _notes(run)
    report["steps"] = [s["undoLabel"] for s in run.steps]
    report["undoSteps"] = len(run.steps)
    if not run.dry:
        report["verified"] = True
    return report
