"""The listening report: let an agent hear its own edit, and lint the timeline's structure.

Two halves:

- `lint_timeline(items_doc, fps=…)` reads list_items data only (pure): slivers, cuts too close together, gaps,
  overlaps, V1/A1 cut points that nearly-but-not-quite agree, clips past the sequence end, clip edges drifted a
  few microseconds off the frame grid.
- `review_cuts(rpc, out_dir=…)` listens: it rebuilds the dialogue track from the source files with ffmpeg (the
  "shadow mix", `shadow_mix`) or, with render=True, has Premiere render the real mix (`export_sequence`), and
  measures every cut point on the dialogue track: how much quiet is left before the cut (the outgoing breath room)
  and after it (the incoming one), words cut off, room-tone jumps and clicks. The shadow mix refuses (NO_MEDIA)
  when a clip has no media file to decode — Enhance Speech and other generated items expose none.
- `pad_cut_points(words, keep)` is the fix side: cut ranges with breathing room, chosen from word timings.

Audio is decoded by ffmpeg to mono 16-bit PCM (16 kHz for analysis) and analysed with the standard library
(`wave`, `array`; numpy speeds the window maths up when it is importable). Measurements are made on 10 ms windows
anchored at the cut: a window's level is its AC RMS (mean removed, so a DC offset is not "sound") in dBFS; a side's
noise floor is the 10th percentile of its windows within 3 s of the cut (inside that clip); speech is 30 ms in a
row above floor + min(12 dB, half the floor-to-peak range).

Report (review_cuts):
  {"sequence", "fps", "source": "shadow"|"render", "tracks": ["A1"], "wav": analysed WAV,
   "cuts": [{"track", "at", "edge"?: "head"|"tail"|"gap-out"|"gap-in", "outgoing": clip|None, "incoming": clip|None,
             "silenceBefore", "silenceAfter" (s, None when that side has no clip),
             "floorBefore", "floorAfter", "levelJump" (dB), "clickRatio"?, "words"?, "unheard"? (past the mix's end),
             "flags": [TIGHT_OUT, TIGHT_IN, CLIPPED, LEVEL_JUMP, CLICK, CUT_IN_WORD, LOW_CONF_WORD],
             "details": {flag: "what was measured and what to do"}}],
   "lint": [issues as lint_timeline], "loudness"?: {...} (render only), "notes": [what it could not hear, …],
   "thresholds": {...}, "summary": {"cuts", "flagged", "flags": {code: n}, "lint": {code: n}, "throughEdits"},
   "clean": bool (no flags, no error-severity lint)}
"""

import math
import os
import re
import shutil
import subprocess
import sys
import wave
from array import array
from operator import mul
from pathlib import Path

from . import framegrid, items, media, transcript
from .errors import BridgeError

RATE = 16000              # analysis sample rate
WINDOW = 0.010            # energy window (s)
LOOK = 3.0                # how far from the cut a side is measured (s)
SPEECH_WINDOWS = 3        # windows in a row above threshold that count as speech (30 ms)
EDGE_WINDOWS = 2          # ...or this many touching the cut: a syllable chopped right at the cut
CLIP_WINDOW = 0.030       # speech this close to the cut = a word is cut off
SPEECH_MARGIN_DB = 12.0   # threshold above the noise floor (capped at half the floor-to-peak range, min 3 dB)
MIN_THRESHOLD_DB = -70.0  # never call anything quieter than this speech
FALLBACK_THRESHOLD_DB = -50.0
LEVEL_JUMP_DB = 6.0
GRID_TOL = 0.5e-6         # s: an edge further than this from a frame boundary has drifted off it (float noise is ~1e-12)
GRID_DRIFT_FRAMES = 0.01  # ...up to this much of a frame; further off it is a sub-frame time placed on purpose
INAUDIBLE_DB = -80.0      # floors both below this: a level difference cannot be heard
CLICK_RATIO = 3.0         # sample step at the cut vs the largest step nearby
CLICK_MIN = 655           # and at least 2 % of full scale (s16)
DB_FLOOR = -100.0
FULL_SCALE_SQ = 32768.0 ** 2

LINT_CODES = ("SLIVER", "CUTS_TOO_CLOSE", "GAP", "OVERLAP", "AV_CUT_MISMATCH", "PAST_END", "OFF_GRID")
CUT_FLAGS = ("CLIPPED", "TIGHT_OUT", "TIGHT_IN", "LEVEL_JUMP", "CLICK", "CUT_IN_WORD", "LOW_CONF_WORD")

_NUMPY = []  # [module or None] once looked up; tests may set it to [None] to force the stdlib path


def _np():
    if not _NUMPY:
        try:
            import numpy
        except ImportError:
            numpy = None
        _NUMPY.append(numpy)
    return _NUMPY[0]


def _bad(message: str, **data) -> BridgeError:
    return BridgeError(message, code="BAD_PARAMS", outcome="not_committed", data=data or None)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _r(v, nd=3):
    return None if v is None else round(v, nd)


# ---------------------------------------------------------------- timeline data

def _track_clips(items_doc: dict, kind: str) -> list[tuple[str, list[dict]]]:
    """[(label, clips sorted by start)] for "video" or "audio"; clips without numeric start/end are left out."""
    out = []
    for t in (items_doc or {}).get(kind) or []:
        label = t.get("label") or f"{'V' if kind == 'video' else 'A'}{int(t.get('index') or 0) + 1}"
        clips = sorted((c for c in t.get("items") or [] if _num(c.get("start")) and _num(c.get("end"))),
                       key=lambda c: (c["start"], c["end"]))
        out.append((label, clips))
    return out


def _media(c: dict) -> str:
    return os.path.normcase(str(c.get("mediaPath") or ""))


def _speed(c: dict) -> float:
    s = c.get("speed")
    return float(s) if _num(s) else 1.0


def _through_edit(a: dict, b: dict, half: float) -> bool:
    """b continues a without a visible/audible change: abutting, same media, same speed, source continuous."""
    return (abs(b["start"] - a["end"]) <= half and bool(_media(a)) and _media(a) == _media(b)
            and _speed(a) == _speed(b) and bool(a.get("disabled")) == bool(b.get("disabled"))
            and _num(a.get("outPoint")) and _num(b.get("inPoint"))
            and abs(b["inPoint"] - a["outPoint"]) <= half * abs(_speed(a)))


def _runs(clips: list[dict], half: float) -> list[list[dict]]:
    """Clips joined by through edits (e.g. left by a razor-free split) form one run of continuous material."""
    runs: list[list[dict]] = []
    for c in clips:
        if runs and _through_edit(runs[-1][-1], c, half):
            runs[-1].append(c)
        else:
            runs.append([c])
    return runs


def _edit_points(runs: list[list[dict]], half: float) -> list[float]:
    """Real cut points of a track (through edits excluded), merged within half a frame."""
    pts = sorted(t for run in runs for t in (run[0]["start"], run[-1]["end"]))
    merged: list[float] = []
    for t in pts:
        if merged and t - merged[-1] <= half:
            continue
        merged.append(t)
    return merged


def _names(run: list[dict]) -> list[str]:
    return [c.get("name") or "?" for c in run]


def _frames(seconds: float, fps: float) -> int:
    return int(round(seconds * fps))


def _same_span(spans, p: float, q: float, half: float) -> bool:
    return any(abs(a - p) <= half and abs(b - q) <= half for a, b in spans)


def _grid_drift(clips: list[dict], fps: float) -> list[tuple[dict, str, float, float]]:
    """(clip, "start"|"end", time, offset s) for every edge a hair off its nearest frame boundary: further than
    GRID_TOL, closer than GRID_DRIFT_FRAMES. Premiere keeps sequence times as given and shows frame k from its start
    time, so an end just past a boundary shows the clip for one more frame and a start just past one hides its first."""
    out = []
    for c in clips:
        for edge in ("start", "end"):
            t = c[edge]
            off = t - round(t * fps) / fps
            if GRID_TOL < abs(off) < GRID_DRIFT_FRAMES / fps:
                out.append((c, edge, t, off))
    return sorted(out, key=lambda d: d[2])


# ---------------------------------------------------------------- lint

def _covered_by_other(items_doc: dict, kind: str, label: str, a: float, b: float, half: float) -> bool:
    """[a, b] is filled by enabled clips on `kind` tracks other than `label` (an end card above V1, music under a
    pause in the dialogue)."""
    spans = sorted((float(c["start"]), float(c["end"])) for t in items_doc.get(kind) or []
                   if t.get("label") != label for c in t.get("items") or []
                   if _num(c.get("start")) and _num(c.get("end")) and not c.get("disabled"))
    pos = a
    for s0, e0 in spans:
        if s0 > pos + half:
            break
        pos = max(pos, e0)
        if pos >= b - half:
            return True
    return pos >= b - half


def _gap_effect(kind: str, is_base: bool, covered: bool = False) -> str:
    """What a short hole on a track looks or sounds like."""
    if covered:
        return ("the track above covers it" if kind == "video"
                else "a pause in the dialogue (other audio plays under it)")
    if kind == "video":
        return "a flash of black" if is_base else "the tracks below show through (fine for a caption or a breath)"
    return "a moment of dead air" if is_base else "this track drops out for a moment"


def lint_timeline(items_doc: dict, *, fps: float, sliver: float = 0.5, min_gap_frames: int = 1,
                  duration: float | None = None, base_tracks=("V1", "A1")) -> dict:
    """Structural problems in list_items data (pure; times in sequence seconds).

    Issues ({code, track, at, severity, detail, …}, sorted by time):
      SLIVER           material shorter than `sliver` s between two cuts: one clip, or several joined by through
                       edits (a through edit — same media continuing — is not a cut). error on base tracks.
      CUTS_TOO_CLOSE   two consecutive real cuts closer than `sliver` s. A sliver or a flash gap therefore shows
                       twice: as SLIVER/GAP (what is between the cuts) and here; `between` names that issue.
      GAP              a hole between consecutive clips of at least `min_gap_frames` frames: every hole on the base
                       tracks (V1/A1: black / dead air, a warning instead when another track of the same kind
                       covers it — music under a dialogue pause; also a late start and, with `duration`, an early end of
                       the base video track — dialogue ending before music/picture is normal), only
                       holes shorter than `sliver` elsewhere (a flash between two B-roll clips — longer ones are
                       normal on upper tracks). `flash: true` when shorter than `sliver`.
      OVERLAP          consecutive clips on one track overlapping by at least `min_gap_frames` frames.
      AV_CUT_MISMATCH  a cut on the base video track and one on the base audio track that miss each other by more
                       than half a frame but less than `sliver` s — usually an accident (a deliberate J/L cut is
                       normally longer; check before "fixing" it). Cuts with no partner nearby are not reported.
      PAST_END         a clip ending after `duration` (the sequence end), when given.
      OFF_GRID         clip edges a few microseconds off the frame grid (one warning per track, `edges` / `late` /
                       `examples`): an edge just past a frame boundary shows the picture a frame late (an end shows
                       the clip one more frame, a start hides its first frame) while the sound cuts on time; the
                       half-frame read-backs cannot see it. Panels before frame-exact sequence times nudged every
                       sequence time +1 us, and moves added it up.
    """
    if not _num(fps) or fps <= 0:
        raise _bad(f"fps must be a positive number, not {fps!r}")
    if not _num(sliver) or sliver < 0:
        raise _bad(f"sliver must be >= 0 seconds, not {sliver!r}")
    half = 0.5 / fps
    gap_min = max(float(min_gap_frames) - 0.5, 0.5) / fps
    base = {str(b).upper() for b in base_tracks or ()}
    issues: list[dict] = []
    points: dict[str, list[float]] = {}
    counted = {"tracks": 0, "clips": 0}

    for kind in ("video", "audio"):
        for label, clips in _track_clips(items_doc, kind):
            counted["tracks"] += 1
            counted["clips"] += len(clips)
            if not clips:
                continue
            is_base = label.upper() in base
            runs = _runs(clips, half)
            pts = _edit_points(runs, half)
            points[label] = pts
            sliver_spans = []
            for run in runs:
                length = run[-1]["end"] - run[0]["start"]
                if length < sliver - half:
                    sliver_spans.append((run[0]["start"], run[-1]["end"]))
                    what = (f'clip "{run[0].get("name")}"' if len(run) == 1
                            else f'{len(run)} clips joined by through edits ({", ".join(_names(run))})')
                    issues.append({"code": "SLIVER", "track": label, "at": _r(run[0]["start"]),
                                   "end": _r(run[-1]["end"]), "duration": _r(length),
                                   "frames": _frames(length, fps), "clips": _names(run),
                                   "severity": "error" if is_base else "warning",
                                   "detail": f"{what} lasts {_frames(length, fps)} frames ({length:.3f} s) between "
                                             f"two cuts: remove it or extend a neighbour over it"})
            gap_spans, small_gaps, overlap_spans = [], [], []
            for prev, nxt in zip(clips, clips[1:]):
                d = nxt["start"] - prev["end"]
                if half < d < gap_min:
                    small_gaps.append((prev["end"], nxt["start"]))  # below min_gap_frames: ignored everywhere
                elif d <= -half:
                    overlap_spans.append((nxt["start"], prev["end"]))
                if d >= gap_min:
                    flash = d < sliver - half
                    gap_spans.append((prev["end"], nxt["start"]))
                    if is_base or flash:
                        covered = is_base and _covered_by_other(items_doc, kind, label, prev["end"], nxt["start"], half)
                        issues.append({"code": "GAP", "track": label, "at": _r(prev["end"]), "end": _r(nxt["start"]),
                                       "duration": _r(d), "frames": _frames(d, fps), "flash": flash,
                                       "covered": covered,
                                       "severity": "error" if is_base and flash and not covered else "warning",
                                       "detail": f'{_frames(d, fps)}-frame gap ({d:.3f} s) between '
                                                 f'"{prev.get("name")}" and "{nxt.get("name")}"'
                                                 + (": " + _gap_effect(kind, is_base, covered) if flash else "")})
                elif d <= -gap_min:
                    issues.append({"code": "OVERLAP", "track": label, "at": _r(nxt["start"]), "end": _r(prev["end"]),
                                   "duration": _r(-d), "frames": _frames(-d, fps), "severity": "error",
                                   "detail": f'"{nxt.get("name")}" starts {-d:.3f} s before "{prev.get("name")}" ends'})
            if is_base and clips[0]["start"] >= gap_min:
                s0 = clips[0]["start"]
                issues.append({"code": "GAP", "track": label, "at": 0.0, "end": _r(s0), "duration": _r(s0),
                               "frames": _frames(s0, fps), "flash": s0 < sliver - half, "severity": "warning",
                               "detail": f"{label} starts at {s0:.3f} s: "
                                         + ("black" if kind == "video" else "silence") + " at the head"})
            drift = _grid_drift(clips, fps)
            if drift:
                late = sum(1 for d in drift if d[3] > 0)
                worst = max(drift, key=lambda d: abs(d[3]))[3]
                issues.append({"code": "OFF_GRID", "track": label, "at": _r(drift[0][2]), "edges": len(drift),
                               "late": late, "maxOffsetUs": round(worst * 1e6, 3), "severity": "warning",
                               "examples": [{"clip": c.get("name"), "edge": edge, "at": round(t, 6),
                                             "offsetUs": round(off * 1e6, 3)} for c, edge, t, off in drift[:5]],
                               "detail": f"{len(drift)} clip edge(s) sit off the frame grid by up to "
                                         f"{abs(worst) * 1e6:.1f} us, {late} of them past a frame boundary: there "
                                         "the picture cuts a frame after the sound (an end shows the clip one more "
                                         "frame, a start hides its first frame). Reload the panel if it predates "
                                         "frame-exact sequence times; an edge keeps its offset until a trim sets it "
                                         "again (whole-frame moves keep it)"})
            last_end = max(c["end"] for c in clips)
            if _num(duration):
                if (is_base and kind == "video" and duration - last_end >= gap_min
                        and not _covered_by_other(items_doc, "video", label, last_end, duration, half)):
                    tail = duration - last_end  # (an end card on an upper track covering the tail is not black)
                    issues.append({"code": "GAP", "track": label, "at": _r(last_end), "end": _r(duration),
                                   "duration": _r(tail), "frames": _frames(tail, fps), "flash": tail < sliver - half,
                                   "severity": "warning",
                                   "detail": f"{label} ends {tail:.3f} s before the sequence end: black at the tail"})
                for c in clips:
                    if c["end"] > duration + half:
                        issues.append({"code": "PAST_END", "track": label, "at": _r(c["end"]),
                                       "severity": "warning", "clips": [c.get("name")],
                                       "detail": f'"{c.get("name")}" ends at {c["end"]:.3f} s, after the sequence end '
                                                 f"({duration:.3f} s)"})
            for p, q in zip(pts, pts[1:]):
                if q - p >= sliver - half or _same_span(small_gaps, p, q, half):
                    continue
                between = ("SLIVER" if _same_span(sliver_spans, p, q, half)
                           else "GAP" if _same_span(gap_spans, p, q, half)
                           else "OVERLAP" if _same_span(overlap_spans, p, q, half) else None)
                what = {"SLIVER": "the sliver between them", "GAP": "a gap between them",
                        "OVERLAP": "overlapping clips"}.get(between, "material between them")
                issues.append({"code": "CUTS_TOO_CLOSE", "track": label, "at": _r(p), "end": _r(q),
                               "spacing": _r(q - p), "frames": _frames(q - p, fps), "between": between,
                               "severity": "warning",
                               "detail": f"cuts at {p:.3f} s and {q:.3f} s are only {_frames(q - p, fps)} frames "
                                         f"apart ({what}): one of them is almost certainly unintended"})

    vlabel = next((b for b in base_tracks or () if str(b).upper().startswith("V")), None)
    alabel = next((b for b in base_tracks or () if str(b).upper().startswith("A")), None)
    vpts, apts = points.get(str(vlabel).upper()), points.get(str(alabel).upper())
    if vpts and apts:
        reported = set()
        for src, dst, slabel, dlabel in ((vpts, apts, vlabel, alabel), (apts, vpts, alabel, vlabel)):
            for t in src:
                near = min(dst, key=lambda x: abs(x - t))
                off = near - t
                key = tuple(sorted((round(t, 4), round(near, 4))))
                if half < abs(off) < sliver and key not in reported:
                    reported.add(key)
                    v, a = (t, near) if slabel == vlabel else (near, t)
                    issues.append({"code": "AV_CUT_MISMATCH", "track": f"{vlabel}/{alabel}", "at": _r(min(v, a)),
                                   "video": _r(v), "audio": _r(a), "offset": _r(a - v),
                                   "frames": _frames(a - v, fps), "severity": "warning",
                                   "detail": f"{vlabel} cuts at {v:.3f} s, {alabel} at {a:.3f} s "
                                             f"({_frames(a - v, fps):+d} frames): align them unless this is a "
                                             "deliberate J/L cut"})
    issues.sort(key=lambda i: (i["at"] if i.get("at") is not None else 0, i["track"], LINT_CODES.index(i["code"])))
    counts: dict[str, int] = {}
    for i in issues:
        counts[i["code"]] = counts.get(i["code"], 0) + 1
    return {"issues": issues, "counts": counts, "fps": round(fps, 6), "sliver": sliver,
            "checked": counted}


def lint_sequence(rpc, *, sliver: float = 0.5, min_gap_frames: int = 1, base_tracks=("V1", "A1")) -> dict:
    """lint_timeline on the active sequence (get_sequence + list_items)."""
    seq = rpc("get_sequence")
    fps = framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or 30.0)
    doc = rpc("list_items", {"kinds": ["video", "audio"]})
    result = lint_timeline(doc, fps=fps, sliver=sliver, min_gap_frames=min_gap_frames,
                           duration=seq.get("durationSeconds"), base_tracks=base_tracks)
    result["sequence"] = seq.get("name")
    return result


# ---------------------------------------------------------------- ffmpeg / WAV

def _require_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise BridgeError("ffmpeg is not on PATH: the listening report decodes audio with it. Install ffmpeg "
                          "(lint_timeline works without it).", code="MISSING_DEPENDENCY", outcome="not_committed")
    return exe


def _clean_path(p) -> str:
    s = str(p)
    return s[4:] if s.startswith("\\\\?\\") else s


def _pcm(raw: bytes) -> array:
    a = array("h")
    a.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _decode(ffmpeg: str, path: str, start: float, seconds: float, rate: int) -> tuple[array, float]:
    """Mono s16 samples decoded from `begin` = max(0, start - 0.1 s) to start + seconds + 0.1 s, and `begin`. The
    margin keeps resampler edges away from the wanted range; the CALLER trims (skip (start - begin) * rate samples)."""
    margin = 0.1
    begin = max(0.0, start - margin)
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{begin:.6f}", "-t", f"{seconds + (start - begin) + margin:.6f}",
           "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg failed"]
        raise OSError(tail[0])
    return _pcm(proc.stdout), begin


def read_wav(path) -> tuple[array, int]:
    """(mono s16 samples, sample rate) of a 16-bit PCM WAV; channels are averaged."""
    with wave.open(str(path), "rb") as w:
        channels, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise BridgeError(f"{Path(path).name} is {8 * width}-bit; the analysis reads 16-bit PCM WAV.",
                          code="BAD_PARAMS", outcome="not_committed")
    samples = _pcm(raw)
    if channels > 1:
        lanes = [samples[i::channels] for i in range(channels)]
        samples = array("h", (sum(v) // channels for v in zip(*lanes)))
    return samples, rate


def write_wav(path, samples: array, rate: int) -> None:
    data = array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def to_analysis_wav(src, dst, *, rate: int = RATE) -> Path:
    """Any audio/video file -> mono 16-bit WAV at `rate` (ffmpeg)."""
    ffmpeg = _require_ffmpeg()
    dst = Path(dst)
    proc = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-i", _clean_path(src), "-map", "0:a:0",
                           "-ac", "1", "-ar", str(rate), "-acodec", "pcm_s16le", str(dst)],
                          capture_output=True, timeout=600)
    if proc.returncode != 0 or not dst.is_file():
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise BridgeError(f"ffmpeg could not convert {Path(str(src)).name} to WAV: {err}", code="IO_ERROR",
                          outcome="not_committed")
    return dst


def _audio_labels(tracks) -> list[str]:
    if tracks is None or tracks == "" or tracks == ():
        tracks = ("A1",)
    if isinstance(tracks, (str, int)):
        tracks = [tracks]
    labels = []
    for t in tracks:
        try:
            kind, idx = items.parse_track(t)
        except ValueError as e:
            raise _bad(str(e)) from e
        if kind != "audio":
            raise _bad(f"The listening report reads audio tracks (A1, A2, …), not {t}.")
        label = f"A{idx + 1}"
        if label not in labels:
            labels.append(label)
    return labels


def _clip_brief(c: dict | None) -> dict | None:
    if c is None:
        return None
    return {"name": c.get("name"), "start": _r(c.get("start")), "end": _r(c.get("end")),
            "inPoint": _r(c.get("inPoint")), "outPoint": _r(c.get("outPoint"))}


def _no_media(labels: list[str], by_label: dict) -> list[dict]:
    """Enabled clips on `labels` whose source the shadow mix cannot read: no mediaPath (a generated item — Premiere's
    Enhance Speech result, a nested sequence) or a path with no file behind it (offline, moved). Disabled clips are
    silent in Premiere too, so they need no media."""
    out = []
    for label in labels:
        for c in by_label[label]:
            if c.get("disabled"):
                continue
            path = _clean_path(c.get("mediaPath") or "")
            if path and os.path.isfile(path):
                continue
            out.append({"track": label, "name": c.get("name"), "start": _r(c["start"]), "end": _r(c["end"]),
                        "mediaPath": c.get("mediaPath") or "", "reason": "file not found" if path else "no media path"})
    return out


def shadow_mix(items_doc: dict, *, tracks=("A1",), out_wav, rate: int = RATE) -> dict:
    """Rebuild the dialogue from the source files: each clip's source range (mediaPath, inPoint..outPoint) decoded
    by ffmpeg and placed at its sequence start, tracks summed → one mono 16-bit WAV. No Premiere needed.

    NOT applied: clip/track volume and keyframes, audio effects, pan, mute/solo, transitions. Disabled clips are
    silent (as in Premiere); speed-changed clips are left out (silence) and listed in `notes`. An enabled clip whose
    media cannot be read (no mediaPath — Enhance Speech results and other generated items, nested sequences — or a
    file that is not there) refuses the whole mix with NO_MEDIA before anything is decoded or written: silence in
    its place would be measured as a pause. `data.clips` lists them; render=True hears Premiere's own mix."""
    ffmpeg = _require_ffmpeg()
    labels = _audio_labels(tracks)
    by_label = dict(_track_clips(items_doc, "audio"))
    missing = [t for t in labels if t not in by_label]
    if missing:
        raise BridgeError(f"No audio track {', '.join(missing)} in this sequence "
                          f"(it has {', '.join(by_label) or 'none'}).", code="NOT_FOUND", outcome="not_committed")
    unreadable = _no_media(labels, by_label)
    if unreadable:
        enabled = sum(1 for t in labels for c in by_label[t] if not c.get("disabled"))
        listed = "; ".join(f'{u["track"]} "{u["name"]}" at {u["start"]:.3f} s ({u["reason"]})' for u in unreadable[:3])
        more = f"; {len(unreadable) - 3} more in data.clips" if len(unreadable) > 3 else ""
        raise BridgeError(f"The shadow mix cannot read {len(unreadable)} of {enabled} enabled clip(s) on "
                          f"{', '.join(labels)}: {listed}{more}. A generated item (Enhance Speech, a nested sequence) "
                          "has no media file and a missing file cannot be decoded, so the mix would be silent there. "
                          "Use --render (Premiere's own mix), or relink the media.",
                          code="NO_MEDIA", outcome="not_committed",
                          hint="Run it again with --render: Premiere renders its own mix, generated items included.",
                          data={"tracks": labels, "enabled": enabled, "clips": unreadable})
    clips = [(t, c) for t in labels for c in by_label[t]]
    end = max([c["end"] for _, c in clips] or [0.0])
    total = int(math.ceil(end * rate))
    lanes: list[array] = []
    report, notes = [], []
    for label in labels:
        lane = array("h", bytes(2 * total))
        for c in by_label[label]:
            row = {"track": label, "name": c.get("name"), "start": _r(c["start"]), "end": _r(c["end"]),
                   "inPoint": _r(c.get("inPoint")), "outPoint": _r(c.get("outPoint")), "mixed": False}
            report.append(row)
            where = f'{label} clip "{c.get("name")}" at {c["start"]:.3f} s'
            path = _clean_path(c.get("mediaPath") or "")
            a, b = int(round(c["start"] * rate)), int(round(c["end"] * rate))
            if c.get("disabled"):
                row["skipped"] = "disabled"
                notes.append(f"{where} is disabled: silent here, as in Premiere.")
                continue
            if abs(_speed(c) - 1.0) > 1e-6:
                row["skipped"] = "speed"
                notes.append(f"{where} plays at {_speed(c) * 100:.0f} % speed: not rebuilt "
                             "(silence in the shadow mix).")
                continue
            if not _num(c.get("inPoint")) or b <= a:
                row["skipped"] = "no source range"
                notes.append(f"{where} has no usable inPoint/outPoint; silence in the shadow mix.")
                continue
            try:
                pcm, began = _decode(ffmpeg, path, c["inPoint"], (b - a) / rate, rate)
            except (OSError, subprocess.SubprocessError) as e:
                row["skipped"] = "decode failed"
                notes.append(f"{where}: ffmpeg could not decode its audio ({e}); silence in the shadow mix.")
                continue
            off = int(round((c["inPoint"] - began) * rate))
            seg = pcm[off: off + (b - a)]
            if len(seg) < b - a:
                short = (b - a - len(seg)) / rate
                notes.append(f"{where}: the media ends {short:.3f} s before the clip's out point; padded with silence.")
                seg.extend(array("h", bytes(2 * (b - a - len(seg)))))
            lane[a:b] = seg
            row["mixed"] = True
        lanes.append(lane)
    mixed = lanes[0] if len(lanes) == 1 else _sum_lanes(lanes, total)
    out = Path(out_wav).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    write_wav(out, mixed, rate)
    return {"wav": str(out), "rate": rate, "seconds": _r(total / rate), "tracks": labels, "clips": report,
            "notes": notes}


def _sum_lanes(lanes: list[array], total: int) -> array:
    np = _np()
    if np is not None:
        acc = np.zeros(total, dtype=np.int32)
        for lane in lanes:
            acc += np.frombuffer(lane, dtype=np.int16)
        return array("h", np.clip(acc, -32768, 32767).astype(np.int16).tobytes())
    out = array("h", lanes[0])
    for lane in lanes[1:]:
        out = array("h", (max(-32768, min(32767, x + y)) for x, y in zip(out, lane)))
    return out


def loudness(path) -> dict | None:
    """Integrated loudness (LUFS), loudness range (LU) and true peak (dBTP) of a file via ffmpeg ebur128; None
    when ffmpeg cannot measure it."""
    ffmpeg = _require_ffmpeg()
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-i", _clean_path(path),
                               "-filter_complex", "ebur128=peak=true", "-f", "null", "-"],
                              capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stderr or b"").decode("utf-8", "replace")
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else ""
    if proc.returncode != 0 or not summary:
        return None

    def grab(pattern):  # None for "-inf" (digital silence): JSON has no infinity
        m = re.search(pattern, summary)
        value = float(m.group(1)) if m else None
        return value if value is not None and math.isfinite(value) else None

    result = {"integratedLufs": grab(r"\bI:[ \t]*(-?[\d.]+|-inf)\s*LUFS"), "loudnessRange": grab(r"LRA:\s*(-?[\d.]+)\s*LU"),
              "truePeakDb": grab(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS")}
    if result["truePeakDb"] is not None and result["truePeakDb"] > -1.0:
        result["warning"] = f"True peak {result['truePeakDb']:.1f} dBTP is above -1 dBTP: platforms may distort it."
    return result


# ---------------------------------------------------------------- per-cut analysis (pure)

def _db_series(samples: array, n_at: int, count: int, win: int, backward: bool) -> list[float]:
    """AC RMS (dBFS) of `count` windows of `win` samples running away from sample n_at (backward = before it)."""
    if count <= 0 or not 0 <= n_at <= len(samples):
        return []  # e.g. a cut past the end of a render that came out shorter than the timeline
    count = min(count, n_at // win if backward else (len(samples) - n_at) // win)
    if count <= 0:
        return []
    lo = n_at - count * win if backward else n_at
    hi = n_at if backward else n_at + count * win
    np = _np()
    if np is not None:
        seg = np.frombuffer(samples, dtype=np.int16)[lo:hi].astype(np.float64).reshape(count, win)
        var = seg.var(axis=1)
        with np.errstate(divide="ignore"):
            dbs = np.maximum(10.0 * np.log10(var / FULL_SCALE_SQ), DB_FLOOR)
        dbs = [float(x) for x in dbs]
    else:
        dbs = []
        for k in range(count):
            seg = samples[lo + k * win: lo + (k + 1) * win]
            s = sum(seg)
            var = sum(map(mul, seg, seg)) / win - (s / win) ** 2
            dbs.append(max(DB_FLOOR, 10.0 * math.log10(var / FULL_SCALE_SQ)) if var > 0 else DB_FLOOR)
    return dbs[::-1] if backward else dbs  # index 0 = the window touching the cut


def _pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))]


def _threshold(floor: float | None, peak: float | None) -> float:
    if floor is None:
        return FALLBACK_THRESHOLD_DB
    margin = max(3.0, min(SPEECH_MARGIN_DB, 0.5 * ((peak if peak is not None else floor) - floor)))
    return max(floor + margin, MIN_THRESHOLD_DB)


def _quiet(dbs: list[float], thr: float) -> tuple[float, bool]:
    """(seconds of quiet from the cut to the first speech, whether speech was found at all). Sound in the
    EDGE_WINDOWS touching the cut counts as speech at the cut even when the burst is shorter than SPEECH_WINDOWS:
    a syllable cut off 20 ms in, after a stop closure ("hospi-t|"), is a clipped word, not quiet."""
    if len(dbs) >= EDGE_WINDOWS and all(db >= thr for db in dbs[:EDGE_WINDOWS]):
        return 0.0, True
    run = 0
    for k, db in enumerate(dbs):
        run = run + 1 if db >= thr else 0
        if run >= SPEECH_WINDOWS:
            return (k - SPEECH_WINDOWS + 1) * WINDOW, True
    return len(dbs) * WINDOW, False


def _side(samples: array, rate: int, at: float, span: tuple[float, float], backward: bool) -> dict:
    """Windows of one side of the cut: up to LOOK s away (the listener hears across clip boundaries), and the
    noise floor / peak of the part that lies inside the side's own clip (`span`)."""
    win = max(1, int(round(WINDOW * rate)))
    n_at = int(round(at * rate))
    look = int(round(LOOK * rate))
    room = min(n_at, look) if backward else min(len(samples) - n_at, look)
    dbs = _db_series(samples, n_at, max(0, room // win), win, backward)
    inside = (n_at - int(round(span[0] * rate))) if backward else (int(round(span[1] * rate)) - n_at)
    own = dbs[: max(0, inside // win)]
    floor = peak = None
    reliable = False
    if len(own) >= 5:
        floor, peak = _pct(own, 10), _pct(own, 95)
        # A real pause (>= 150 ms within 3 dB of the floor) is needed to call it room tone: a short clip that is
        # speech throughout only yields the dips between syllables.
        run = best = 0
        for d in own:
            run = run + 1 if d <= floor + 3.0 else 0
            best = max(best, run)
        reliable = best * WINDOW >= 0.15
    return {"dbs": dbs, "floor": floor, "peak": peak, "floorReliable": reliable}


def _measure(side: dict, thr: float) -> dict:
    quiet, speech = _quiet(side["dbs"], thr)
    return dict(side, silence=quiet, speech=speech, threshold=thr)


def _click(samples: array, n: int, rate: int) -> float | None:
    """Sample step at the cut divided by the largest step within 20 ms around it (cut ±2 samples excluded);
    None when the step is too small to hear."""
    if n < 3 or n > len(samples) - 3:
        return None
    step = max(abs(samples[k] - samples[k - 1]) for k in (n - 1, n, n + 1))
    if step < CLICK_MIN:
        return None
    reach = max(4, int(round(0.020 * rate)))
    ref = 1
    for k in range(max(1, n - reach), min(len(samples), n + reach)):
        if n - 2 <= k <= n + 2:
            continue
        d = abs(samples[k] - samples[k - 1])
        if d > ref:
            ref = d
    return step / ref


def analyse_cut(samples: array, rate: int, at: float, *, outgoing=None, incoming=None, pre_roll: float = 0.25,
                post_roll: float = 0.35, edge: str | None = None) -> dict:
    """Listen to one cut point in a mono s16 sample array (pure).

    `outgoing` / `incoming` are the (start, end) sequence-second spans of the clips before and after the cut (None
    where there is none: program head/tail or a gap). A side's noise floor (and so its speech threshold) comes from
    the part of its own clip within 3 s of the cut; its quiet is measured as heard, up to 3 s, across clip
    boundaries. TIGHT_* need a word on that side within 3 s (no word, nothing to breathe after).
    `edge`: "head"/"tail" (program start/end: only CLIPPED and CLICK are checked) or None.
    Returns {silenceBefore, silenceAfter, floorBefore, floorAfter, levelJump, clickRatio?, flags, details}."""
    res: dict = {"silenceBefore": None, "silenceAfter": None, "floorBefore": None, "floorAfter": None,
                 "levelJump": None, "flags": [], "details": {}}
    flags, details = res["flags"], res["details"]
    before = _side(samples, rate, at, outgoing, True) if outgoing else None
    after = _side(samples, rate, at, incoming, False) if incoming else None
    thr_b = _threshold(before["floor"], before["peak"]) if before and before["floor"] is not None else None
    thr_a = _threshold(after["floor"], after["peak"]) if after and after["floor"] is not None else None
    if before:
        before = _measure(before, thr_b if thr_b is not None else (thr_a or FALLBACK_THRESHOLD_DB))
    if after:
        after = _measure(after, thr_a if thr_a is not None else (thr_b or FALLBACK_THRESHOLD_DB))
    clipped = []
    if before:
        res["silenceBefore"], res["floorBefore"] = _r(before["silence"]), _r(before["floor"], 1)
        if before["speech"] and before["silence"] < CLIP_WINDOW:
            clipped.append("the outgoing clip is still speaking at the cut")
    if after:
        res["silenceAfter"], res["floorAfter"] = _r(after["silence"]), _r(after["floor"], 1)
        if after["speech"] and after["silence"] < CLIP_WINDOW:
            clipped.append("the incoming clip starts mid-word")
    if clipped:
        flags.append("CLIPPED")
        details["CLIPPED"] = ("; ".join(clipped) + f" (speech within {CLIP_WINDOW * 1000:.0f} ms of the cut): "
                              "move the cut into the pause between words")
    if edge not in ("head", "tail"):
        if before and before["speech"] and before["silence"] < post_roll:
            flags.append("TIGHT_OUT")
            details["TIGHT_OUT"] = (f"only {before['silence']:.2f} s of quiet after the outgoing words (want "
                                    f"{post_roll:.2f} s): extend the out point by "
                                    f"{post_roll - before['silence']:.2f} s if the source pause allows")
        if after and after["speech"] and after["silence"] < pre_roll:
            flags.append("TIGHT_IN")
            details["TIGHT_IN"] = (f"only {after['silence']:.2f} s of quiet before the incoming words (want "
                                   f"{pre_roll:.2f} s): move the in point {pre_roll - after['silence']:.2f} s earlier "
                                   "if the source pause allows")
        if before and after and before["floor"] is not None and after["floor"] is not None:
            jump = after["floor"] - before["floor"]
            res["levelJump"] = _r(jump, 1)
            if not (before.get("floorReliable") and after.get("floorReliable")):
                res["levelJumpUncertain"] = True  # no pause on one side: its "floor" is speech, not room tone
            elif abs(jump) > LEVEL_JUMP_DB and max(before["floor"], after["floor"]) > INAUDIBLE_DB:
                flags.append("LEVEL_JUMP")
                details["LEVEL_JUMP"] = (f"room tone jumps {jump:+.1f} dB across the cut ({before['floor']:.0f} -> "
                                         f"{after['floor']:.0f} dBFS): match levels, or cover with room tone / a "
                                         "short crossfade")
    ratio = _click(samples, int(round(at * rate)), rate)
    if ratio is not None:
        res["clickRatio"] = round(ratio, 1)
        if ratio > CLICK_RATIO:
            flags.append("CLICK")
            details["CLICK"] = (f"the waveform jumps at the cut ({ratio:.0f}x the largest step around it): add a "
                                "2-4 frame audio crossfade or move the cut to a zero crossing")
    return res


def cut_points(clips: list[dict], fps: float) -> tuple[list[dict], int]:
    """Cut points of one audio track's clips (sorted by start): [{at, edge, outgoing, incoming, gap?}] and the
    number of through edits skipped. Disabled clips are treated as absent (they are silent)."""
    half = 0.5 / fps
    live = [c for c in sorted(clips, key=lambda c: (c["start"], c["end"])) if not c.get("disabled")]
    out, through = [], 0
    if not live:
        return out, through
    out.append({"at": live[0]["start"], "edge": "head", "outgoing": None, "incoming": live[0]})
    for prev, nxt in zip(live, live[1:]):
        d = nxt["start"] - prev["end"]
        if d > half:
            out.append({"at": prev["end"], "edge": "gap-out", "outgoing": prev, "incoming": None, "gap": d})
            out.append({"at": nxt["start"], "edge": "gap-in", "outgoing": None, "incoming": nxt, "gap": d})
        elif _through_edit(prev, nxt, half):
            through += 1
        else:
            out.append({"at": nxt["start"], "edge": None, "outgoing": prev, "incoming": nxt})
    out.append({"at": live[-1]["end"], "edge": "tail", "outgoing": live[-1], "incoming": None})
    return out, through


# ---------------------------------------------------------------- words

def word_timings(words) -> list[dict]:
    """[{index, text, start, end, confidence}] from an Adobe transcript (dict or file path; punctuation tokens
    dropped), or from a list of {text?, start, end | duration} dicts or (start, end) pairs. Indices are what
    pad_cut_points' `keep` refers to."""
    if isinstance(words, (str, Path)):
        _, words = transcript.read_transcript(words)
    if isinstance(words, dict):
        flat = []
        for seg in words.get("segments") or []:
            for w in seg.get("words") or []:
                if w.get("type") not in (None, "word"):
                    continue
                flat.append(w)
        words = flat
    out = []
    for i, w in enumerate(words or []):
        if isinstance(w, dict):
            start = w.get("start")
            end = w.get("end") if _num(w.get("end")) else (start + w["duration"] if _num(start) and _num(
                w.get("duration")) else None)
            text, conf = w.get("text"), w.get("confidence")
        else:
            try:
                start, end = float(w[0]), float(w[1])
            except (TypeError, ValueError, IndexError) as e:
                raise _bad(f"word {i}: expected (start, end) or a dict with start and end/duration") from e
            text, conf = None, None
        if not _num(start) or not _num(end) or end < start:
            raise _bad(f"word {i} has no usable start/end ({w!r})")
        out.append({"index": len(out), "text": text, "start": float(start), "end": float(end),
                    "confidence": conf if _num(conf) else None})
    return out


def word_checks(words: list[dict], at: float, *, tolerance: float = 0.05, near: float = 0.3,
                low_conf: float = 0.5) -> dict:
    """Word context of a cut at `at` (words in the same time base as `at`): the word before/after with the gap to
    the cut, a word the cut lands inside (more than `tolerance` s from both its ends), low-confidence words within
    `near` s (a chopped word often transcribes badly)."""
    inside = [w for w in words if w["start"] + tolerance < at < w["end"] - tolerance]
    before = max((w for w in words if w["end"] <= at + tolerance and w["start"] < at), key=lambda w: w["end"],
                 default=None)
    after = min((w for w in words if w["start"] >= at - tolerance and w["end"] > at), key=lambda w: w["start"],
                default=None)
    shaky = [w for w in words if w["confidence"] is not None and w["confidence"] < low_conf
             and w["end"] > at - near and w["start"] < at + near]
    res = {"before": None if before is None else {"text": before["text"], "end": _r(before["end"]),
                                                   "gap": _r(at - before["end"])},
           "after": None if after is None else {"text": after["text"], "start": _r(after["start"]),
                                                 "gap": _r(after["start"] - at)},
           "flags": [], "details": {}}
    if inside:
        w = inside[0]
        res["inside"] = {"text": w["text"], "start": _r(w["start"]), "end": _r(w["end"])}
        res["flags"].append("CUT_IN_WORD")
        res["details"]["CUT_IN_WORD"] = (f'the cut lands inside "{w["text"]}" ({w["start"]:.2f}-{w["end"]:.2f} s '
                                         "in the transcript)")
    if shaky:
        res["flags"].append("LOW_CONF_WORD")
        res["details"]["LOW_CONF_WORD"] = ("low-confidence word(s) next to the cut: "
                                           + ", ".join(f'"{w["text"]}" ({w["confidence"]:.2f})' for w in shaky)
                                           + " — listen for a chopped or garbled word")
    return res


def pad_cut_points(words, keep, *, pre_roll: float = 0.25, post_roll: float = 0.35, max_gap_share: float = 0.6,
                   media_end: float | None = None) -> list[tuple[float, float]]:
    """Cut ranges (source seconds) with breathing room for `keep` = [(first word index, last word index), …].

    Each range runs from up to `pre_roll` before its first word to up to `post_roll` after its last word, and never
    into a neighbouring word: when the pause to a neighbour is shorter than pre_roll + post_roll it is shared — the
    outgoing tail (the side after a word) gets `max_gap_share` of it, the incoming head the rest. So two ranges
    that meet across one pause never overlap. The head stops at 0 and the tail at `media_end` when given.
    `words`: anything word_timings accepts (indices count words only, punctuation tokens dropped)."""
    w = word_timings(words)
    for name, v in (("pre_roll", pre_roll), ("post_roll", post_roll)):
        if not _num(v) or v < 0:
            raise _bad(f"{name} must be >= 0 seconds, not {v!r}")
    if not _num(max_gap_share) or not 0.0 <= max_gap_share <= 1.0:
        raise _bad(f"max_gap_share must be between 0 and 1, not {max_gap_share!r}")
    want = pre_roll + post_roll
    out = []
    for n, pair in enumerate(keep or []):
        try:
            first, last = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError) as e:
            raise _bad(f"keep[{n}]: expected (first word index, last word index), got {pair!r}") from e
        if not 0 <= first <= last < len(w):
            raise _bad(f"keep[{n}] = ({first}, {last}) is outside the {len(w)} words (or first > last)")
        s, e = w[first]["start"], w[last]["end"]
        if first > 0:
            pause = max(0.0, s - w[first - 1]["end"])
            lead = pre_roll if pause >= want else min(pre_roll, (1.0 - max_gap_share) * pause)
        else:
            lead = min(pre_roll, max(0.0, s))
        if last + 1 < len(w):
            pause = max(0.0, w[last + 1]["start"] - e)
            tail = post_roll if pause >= want else min(post_roll, max_gap_share * pause)
        else:
            tail = post_roll if media_end is None else min(post_roll, max(0.0, media_end - e))
        out.append((round(s - lead, 3), round(e + tail, 3)))
    return out


# ---------------------------------------------------------------- orchestration

def resolve_preset(preset=None) -> str:
    """The encoder preset for export_sequence: `preset`, else Premiere's own WAV preset (media.find_wav_preset:
    PPRO_WAV_PRESET or next to the running Premiere). FILE_NOT_FOUND when there is none or it does not exist —
    Premiere would only say "Unable to initialize export!"."""
    path = _clean_path(preset) if preset else media.find_wav_preset()
    if not path:
        raise BridgeError("Premiere's 'Waveform Audio 48kHz 16-bit' preset was not found next to a running Premiere: "
                          "pass the .epr as preset (CLI --preset) or set PPRO_WAV_PRESET.", code="FILE_NOT_FOUND",
                          outcome="not_committed")
    if not os.path.isfile(path):
        raise BridgeError(f"Preset not found: {path}", code="FILE_NOT_FOUND", outcome="not_committed")
    return str(path)


def export_audio(rpc, *, output, tracks=("A1",), render: bool = False, preset=None, rate: int = RATE,
                 items_doc: dict | None = None) -> dict:
    """The audio of the active sequence as a mono 16-bit WAV at `output`: the shadow mix of `tracks` (default), or
    with render=True Premiere's own render (export_sequence, every audible track) converted for analysis; the
    full-rate render is kept next to it (`preset`: see resolve_preset). Returns {wav, source, notes, …}. The shadow
    mix refuses clips without readable media (NO_MEDIA, nothing written): it never falls back to the render."""
    _require_ffmpeg()
    out = Path(output).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    labels = _audio_labels(tracks)
    if not render:
        doc = items_doc if items_doc is not None else rpc("list_items", {"kinds": ["audio"]})
        mix = shadow_mix(doc, tracks=labels, out_wav=out, rate=rate)
        mix["source"] = "shadow"
        mix["notes"] = [f"Shadow mix of {', '.join(labels)} rebuilt from the source files ({rate} Hz mono): clip and "
                        "track volume, keyframes, audio effects, pan, mute/solo and transitions are NOT applied, so "
                        "levels are the raw recording levels.",
                        f"Only {', '.join(labels)} heard: music, SFX and other audio tracks are not in this mix "
                        "(render=True analyses Premiere's own mix)."] + mix["notes"]
        return mix
    rendered = out.with_name(out.stem + "_render.wav")
    reply = rpc("export_sequence", {"outputPath": str(rendered), "presetPath": resolve_preset(preset)})
    got = next((Path(_clean_path(p)) for p in (reply.get("outputPath"), rendered)
                if p and Path(_clean_path(p)).is_file()), None)
    if got is None:
        raise BridgeError("export_sequence reported success but no file was written at "
                          f"{reply.get('outputPath') or rendered}.", code="EXPORT_FAILED", outcome="not_committed",
                          data={"reply": reply})
    to_analysis_wav(got, out, rate=rate)
    return {"wav": str(out), "rate": rate, "source": "render", "rendered": str(got), "preset": reply.get("preset"),
            "tracks": labels,
            "notes": ["Analysed Premiere's render of the whole sequence: every audible track, volumes and effects "
                      "applied. Music/SFX under the dialogue raise the measured noise floor, so breathing-room and "
                      "room-tone figures are less certain where they play.",
                      f"Cut points come from {', '.join(labels)} in list_items; the render is assumed to start at "
                      "sequence time 0."]}


def review_cuts(rpc, *, tracks=("A1",), pre_roll: float = 0.25, post_roll: float = 0.35, words=None, out_dir,
                render: bool = False, preset=None, sliver: float = 0.5) -> dict:
    """The listening report for the active sequence (see the module docstring for the schema).

    list_items → lint_timeline → shadow mix of `tracks` (or, render=True, Premiere's render via export_sequence
    with `preset` or the WAV preset next to the running Premiere, plus loudness) → every cut point on `tracks`
    measured: TIGHT_OUT (< post_roll s of quiet before the cut),
    TIGHT_IN (< pre_roll after it), CLIPPED (speech within 30 ms of the cut), LEVEL_JUMP (noise floor differs by
    > 6 dB), CLICK (a sample step at the cut). `words`: a transcript of THIS mix in sequence seconds (Adobe JSON
    path/dict or a word list — e.g. transcribe.py run on the report's WAV) adds CUT_IN_WORD / LOW_CONF_WORD.
    Program head/tail only get CLIPPED/CLICK; gap edges are measured on their one side.
    Without render, a clip on `tracks` with no readable media (an Enhance Speech result) refuses the report with
    NO_MEDIA (shadow_mix) rather than measuring silence; it does not switch to the render by itself, because the
    render hears every track and needs Premiere's WAV preset — the caller asks for render=True."""
    labels = _audio_labels(tracks)
    for name, v in (("pre_roll", pre_roll), ("post_roll", post_roll)):
        if not _num(v) or v < 0:
            raise _bad(f"{name} must be >= 0 seconds, not {v!r}")
    _require_ffmpeg()
    word_list = word_timings(words) if words is not None else None
    if render:
        preset = resolve_preset(preset)
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    seq = rpc("get_sequence")
    fps = framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or 30.0)
    doc = rpc("list_items", {"kinds": ["video", "audio"]})
    lint = lint_timeline(doc, fps=fps, sliver=sliver, duration=seq.get("durationSeconds"))
    by_label = dict(_track_clips(doc, "audio"))
    missing = [t for t in labels if t not in by_label]
    if missing:
        raise BridgeError(f"No audio track {', '.join(missing)} in this sequence "
                          f"(it has {', '.join(by_label) or 'none'}).", code="NOT_FOUND", outcome="not_committed")

    wav_name = "review_mix.wav" if render else "review_shadow.wav"  # render: full-rate file is review_mix_render.wav
    audio = export_audio(rpc, output=out / wav_name, tracks=labels, render=render, preset=preset, items_doc=doc)
    samples, rate = read_wav(audio["wav"])
    notes = list(audio["notes"])
    heard = len(samples) / rate if rate else 0.0
    last = max((c["end"] for label in labels for c in by_label[label]), default=0.0)
    if last - heard > 0.5:
        notes.append(f"The mix is {heard:.2f} s but the audio on {', '.join(labels)} runs to {last:.2f} s: cuts after "
                     f"{heard:.2f} s were not heard (marked unheard).")

    cuts, through = [], 0
    for label in labels:
        points, skipped = cut_points(by_label[label], fps)
        through += skipped
        for cp in points:
            o, i = cp["outgoing"], cp["incoming"]
            at = cp["at"]
            res = analyse_cut(samples, rate, at, outgoing=(o["start"], at) if o else None,
                              incoming=(at, i["end"]) if i else None, pre_roll=pre_roll, post_roll=post_roll,
                              edge=cp["edge"] if cp["edge"] in ("head", "tail") else None)
            entry = {"track": label, "at": _r(at)}
            if at > heard + 0.5 / fps:
                entry["unheard"] = True
            if cp["edge"]:
                entry["edge"] = cp["edge"]
            if cp.get("gap") is not None:
                entry["gap"] = _r(cp["gap"])
            entry.update({"outgoing": _clip_brief(o), "incoming": _clip_brief(i)})
            entry.update(res)
            if word_list is not None:
                wc = word_checks(word_list, at)
                entry["flags"] = entry["flags"] + wc.pop("flags")
                entry["details"].update(wc.pop("details"))
                entry["words"] = wc
            cuts.append(entry)
    cuts.sort(key=lambda c: (c["at"], c["track"]))

    report = {"sequence": seq.get("name"), "fps": round(fps, 6), "source": audio["source"], "tracks": labels,
              "wav": audio["wav"], "cuts": cuts, "lint": lint["issues"]}
    if render:
        report["rendered"] = audio.get("rendered")
        report["loudness"] = loudness(audio["rendered"])
        if report["loudness"] is None:
            notes.append("Loudness could not be measured (ffmpeg ebur128 failed).")
    notes.append("Silence is measured on 10 ms windows against each clip's own noise floor near the cut (speech = "
                 "30 ms above floor + up to 12 dB); breaths and lip noise count as sound. Picture is not heard: video "
                 "problems show only in the lint.")
    if word_list is None:
        notes.append(f"No transcript given: word-level checks were skipped. Transcribe {Path(audio['wav']).name} "
                     "with transcribe.py and pass it as words to add them.")
    else:
        notes.append("Word times were read as sequence seconds (a transcript of this mix, not of the source clips).")
    report["notes"] = notes
    flag_counts: dict[str, int] = {}
    for c in cuts:
        for f in c["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    report["thresholds"] = {"preRoll": pre_roll, "postRoll": post_roll, "clippedWithin": CLIP_WINDOW,
                            "levelJumpDb": LEVEL_JUMP_DB, "clickRatio": CLICK_RATIO, "windowSeconds": WINDOW,
                            "sliver": sliver}
    report["summary"] = {"cuts": len(cuts), "flagged": sum(1 for c in cuts if c["flags"]), "flags": flag_counts,
                         "lint": lint["counts"], "throughEdits": through}
    report["clean"] = not flag_counts and not any(i.get("severity") == "error" for i in lint["issues"])
    return report
