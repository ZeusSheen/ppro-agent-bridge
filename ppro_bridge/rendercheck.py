"""Render check: does every piece an edit made, trimmed or moved really play in Premiere's own render?

list_items and inspect_items cannot tell. On Premiere 27.1 a createMoveAction that carried a clip past another clip
on its track left the moved clip - and once the clip it passed - silent (-180 dB) or blank (0 opaque px) in every
render, while both reads looked right (B1, measured 3 Nov 2026). split_clip no longer moves clips and trim_clip refuses
such a move; this check is the evidence. Opt-in on split_clip / cut_range / extend_cut (--render-check), because it
costs a render.

Audio: ONE render of the whole sequence (export_sequence with Premiere's WAV preset, as review_cuts --render: about
5 s for a 253 s sequence), analysed at 16 kHz mono. Each audio piece is compared with its own source, decoded by
ffmpeg over the piece's in..out:
  * silent     the render is at or below SILENT_DB over the piece while the source has sound;
  * missing    the render has sound there, but not this piece's: the best normalised correlation with the source
               (lags within +-LAG_SECONDS, which covers the 21 ms AAC offset measured on 27.1) is below CORR_MISSING
               - music or another track playing over a silent piece cannot hide it;
  * uncertain  correlation between CORR_MISSING and CORR_OK (heavy effects, or a piece turned far down under music);
  * ok         correlation >= CORR_OK; gainDb is the piece's level in the render relative to its source;
  * quietSource / tooShort / unverified (no media file or ffmpeg failed) are not judged; notRendered: the piece lies
               past the end of the render (the B1 ghost: Premiere's sequence end did not count a moved clip).
Video: one exported frame in the middle of each piece must not be empty: 0 opaque pixels (alpha > 8) in an RGBA
frame, or nothing brighter than frames.BLACK_MAX in an RGB one -> blank. A frame at or past Premiere's sequence end
is notRendered (export_frame would return some other frame). When another video track has a clip at that time
(`covered`, above or below) a drawn frame does not prove this piece drew anything: uncertain (a warning), not ok.
A disabled clip is not judged (`disabled`): Premiere renders it silent / blank on purpose. A clip turned down to
silence on purpose (volume, a fade over the whole piece) still reads as silent.
Needs ffmpeg (audio), Pillow (video; without it a video piece is unverified) and the WAV preset (review.resolve_preset).
"""

import math
import subprocess
import tempfile
import time
from array import array
from pathlib import Path

from . import framegrid, frames, paths, review
from .errors import BridgeError

RATE = review.RATE        # analysis sample rate (16 kHz)
EDGE = 0.1                # s left out at each end of a piece (resampler edges, a frame of slop)
MIN_SECONDS = 0.1         # shorter analysis windows are not judged
CORR_SECONDS = 2.0        # longest stretch the correlation looks at (the middle of the piece)
LAG_SECONDS = 0.05        # +- lag searched between render and source (Premiere's AAC decode leads ffmpeg by 21.4 ms)
SILENT_DB = -60.0         # a render this quiet over a piece whose source has sound: silent
SOURCE_QUIET_DB = -50.0   # a source quieter than this has nothing to hear: not judged
CORR_OK = 0.5
CORR_MISSING = 0.2
ALPHA_MIN = 8             # an RGBA pixel with alpha above this is drawn
PROBLEMS = ("silent", "missing", "blank", "notRendered")


def _db(samples) -> float | None:
    if not len(samples):
        return None
    ms = sum(x * x for x in samples) / len(samples)
    return round(10 * math.log10(ms / review.FULL_SCALE_SQ), 1) if ms else -180.0


def _decimate(samples, step: int) -> list[float]:
    return [sum(samples[i:i + step]) / step for i in range(0, len(samples) - step + 1, step)]


def best_correlation(render, source, lag: int) -> tuple[float, int, float]:
    """(rho, lag in samples, gain) of the best match of `render` (n samples) inside `source` (n + 2*lag samples,
    `source[lag:lag + n]` aligned with it at zero lag): rho is the normalised correlation, gain the least-squares
    factor render ~ gain * source. Positive lag: the render plays the source later than ffmpeg decodes it."""
    n = len(render)
    np = review._np()
    if np is not None:
        r = np.asarray(render, dtype=np.float64)
        x = np.asarray(source, dtype=np.float64)
        dots = np.correlate(x, r, mode="valid")
        cum = np.concatenate(([0.0], np.cumsum(x * x)))
        ex = cum[n:] - cum[:len(cum) - n]
        er = float(r @ r)
        rho = dots / np.sqrt(np.maximum(ex * er, 1e-9))
        k = int(np.argmax(rho))
        return float(rho[k]), lag - k, float(dots[k] / ex[k]) if ex[k] > 0 else 0.0
    best = (-2.0, 0, 0.0)
    er = sum(v * v for v in render)
    for k in range(2 * lag + 1):
        seg = source[k:k + n]
        dot = sum(a * b for a, b in zip(render, seg))
        ex = sum(b * b for b in seg)
        rho = dot / math.sqrt(max(ex * er, 1e-9))
        if rho > best[0]:
            best = (rho, lag - k, dot / ex if ex > 0 else 0.0)
    return best


def _source(path: str, t0: float, t1: float, rate: int) -> array:
    """Mono samples of `path` from t0 to t1 (t0 may be negative: zeros before the media's start), padded to length."""
    ffmpeg = review._require_ffmpeg()
    want = int(round((t1 - t0) * rate))
    lead = max(0, int(round(-t0 * rate)))
    start = max(0.0, t0)
    pcm, began = review._decode(ffmpeg, review._clean_path(path), start, max(0.0, t1 - start), rate)
    skip = int(round((start - began) * rate))
    out = array("h", bytes(2 * lead)) + pcm[skip:skip + want - lead]
    if len(out) < want:
        out.extend(array("h", bytes(2 * (want - len(out)))))
    return out


def check_audio(piece: dict, samples, rate: int, *, source=None) -> dict:
    """One audio piece against the render's samples (mono, `rate`). `source(path, t0, t1, rate)` decodes its media
    (default: ffmpeg via _source)."""
    source = source or _source
    out = {"status": None}
    margin = min(EDGE, (piece["end"] - piece["start"]) / 4)
    a, b = piece["start"] + margin, piece["end"] - margin
    if b - a < MIN_SECONDS:
        return dict(out, status="tooShort")
    if b * rate > len(samples):
        return dict(out, status="notRendered", renderedSeconds=round(len(samples) / rate, 3),
                    detail="past the end of Premiere's render: Premiere does not count this clip in the sequence")
    win = samples[int(round(a * rate)):int(round(b * rate))]
    out["renderDb"] = _db(win)
    if not piece.get("mediaPath") or piece.get("inPoint") is None:
        return dict(out, status="unverified", detail="no media file to compare with")
    s0 = piece["inPoint"] + margin
    lag = int(LAG_SECONDS * rate)
    try:  # one decode: the piece's source range plus `lag` samples on each side for the correlation
        ext = source(piece["mediaPath"], s0 - lag / rate, s0 + (len(win) + lag) / rate, rate)
    except (OSError, subprocess.SubprocessError, BridgeError) as e:
        return dict(out, status="unverified", detail=f"could not decode the source: {e}")
    out["sourceDb"] = _db(ext[lag:lag + len(win)])
    if out["sourceDb"] is None or out["sourceDb"] < SOURCE_QUIET_DB:
        return dict(out, status="quietSource")
    if out["renderDb"] is not None and out["renderDb"] <= SILENT_DB:
        return dict(out, status="silent",
                    detail=f"render {out['renderDb']} dBFS over a source at {out['sourceDb']} dBFS")
    # correlate the middle CORR_SECONDS (numpy) or a quarter of that at a quarter of the rate (stdlib)
    np = review._np()
    n = min(len(win), int((CORR_SECONDS if np is not None else CORR_SECONDS / 4) * rate))
    off = (len(win) - n) // 2
    r, x, step = win[off:off + n], ext[off:off + n + 2 * lag], 1
    if np is None:
        step = 4
        r, x, lag = _decimate(r, step), _decimate(x, step), lag // step
    rho, k, gain = best_correlation(r, x[:len(r) + 2 * lag], lag)
    out.update(correlation=round(rho, 3), lagMs=round(k * step / rate * 1000, 2),
               gainDb=round(20 * math.log10(gain), 1) if gain > 0 else None)
    if rho >= CORR_OK:
        out["status"] = "ok"
    elif rho < CORR_MISSING:
        out.update(status="missing", detail=f"the render has sound here ({out['renderDb']} dBFS) but not this clip's "
                                            f"(correlation {rho:.2f} with its source)")
    else:
        out.update(status="uncertain", detail=f"correlation {rho:.2f} with its source")
    return out


def frame_stats(path) -> dict | None:
    """{"opaquePixels"} of an RGBA PNG or {"brightPixels"} of an RGB one; None without Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None
    with Image.open(path) as im:
        if im.mode in ("RGBA", "LA") or "transparency" in im.info:
            hist = im.convert("RGBA").getchannel("A").histogram()
            return {"opaquePixels": sum(hist[ALPHA_MIN + 1:]), "pixels": im.width * im.height}
        hist = im.convert("L").histogram()
        return {"brightPixels": sum(hist[frames.BLACK_MAX + 1:]), "pixels": im.width * im.height}


def check_video(rpc, piece: dict, directory: Path, duration: float, fps: float, covered: list[str], *,
                export=None) -> dict:
    export = export or frames.export_one_frame
    at = (piece["start"] + piece["end"]) / 2
    out = {"at": round(at, 6), "status": None}
    if covered:
        out["covered"] = covered
    if at >= duration - 0.5 / fps:
        return dict(out, status="notRendered", sequenceSeconds=duration,
                    detail="past the end Premiere reports for the sequence: Premiere does not count this clip")
    directory.mkdir(parents=True, exist_ok=True)
    data = export(rpc, directory, at, f"{piece['track']}_{piece['start']:.3f}.png")
    stats = frame_stats(data["resolvedPath"]) if data.get("resolvedPath") else None
    out["frame"] = data.get("resolvedPath")
    if stats is None:
        return dict(out, status="unverified", detail="no frame file" if not data.get("resolvedPath") else
                    "Pillow is not installed: the frame was not measured")
    out.update(stats)
    drawn = stats.get("opaquePixels", stats.get("brightPixels"))
    if drawn and covered:
        return dict(out, status="uncertain", detail=f"{', '.join(covered)} draws here too; this piece itself was not "
                                                    "measured (look at the frame)")
    if drawn:
        return dict(out, status="ok")
    return dict(out, status="blank", detail="the frame in the middle of the piece is empty" +
                (f" (although {', '.join(covered)} has a clip there too)" if covered else ""))


def _covered(items_doc: dict, piece: dict, at: float) -> list[str]:
    """Other video tracks with a clip under `at`."""
    return [t.get("label") for t in items_doc.get("video") or [] if t.get("label") != piece["track"]
            and any((it.get("start") or 0) <= at < (it.get("end") or 0) for it in t.get("items") or [])]


def check(rpc, pieces: list[dict], *, out_dir=None, preset=None, source=None, export=None) -> dict:
    """Render-check `pieces` ({track "V1"/"A2", name, start, end, inPoint, outPoint, mediaPath}): one audio render
    for every audio piece, one frame per video piece. Returns {"ok", "dir", "pieces": [... status ...], "problems",
    "warnings", "seconds", "notes"}; ok is False when a piece is silent, missing, blank or notRendered. A render that
    cannot run raises (BridgeError from export_sequence / ffmpeg)."""
    started = time.monotonic()
    # absolute: the panel hands export_frame's dir to Premiere, which resolves a relative one against its own cwd
    directory = paths.resolved(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="ppro_render_check_"))
    directory.mkdir(parents=True, exist_ok=True)
    seq = rpc("get_sequence")
    duration = float(seq.get("durationSeconds") or 0.0)
    fps = framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or None) or 30.0
    audio = [p for p in pieces if p["track"].startswith("A")]
    video = [p for p in pieces if p["track"].startswith("V")]
    report = {"dir": str(directory), "sequenceSeconds": duration, "pieces": [], "problems": [], "warnings": [],
              "notes": []}
    for p in [p for p in pieces if p.get("disabled")]:  # listed, not measured: Premiere mutes / hides it on purpose
        report["pieces"].append(dict(_brief(p), status="disabled"))
    audio, video = [p for p in audio if not p.get("disabled")], [p for p in video if not p.get("disabled")]
    if audio:
        t = time.monotonic()
        mix = review.export_audio(rpc, output=str(directory / "render_check.wav"), render=True, preset=preset)
        samples, rate = review.read_wav(mix["wav"])
        report["render"] = {"wav": mix["wav"], "rendered": mix.get("rendered"), "renderedSeconds":
                            round(len(samples) / rate, 3), "seconds": round(time.monotonic() - t, 2)}
        for p in audio:
            report["pieces"].append(dict(_brief(p), **check_audio(p, samples, rate, source=source)))
        report["notes"].append("Audio: each piece is compared with its own source over its in..out (ffmpeg), in "
                               "Premiere's render of the whole sequence; music on other tracks cannot hide a silent "
                               "piece (the source's waveform must be in the render).")
    if video:
        doc = rpc("list_items", {"kinds": ["video"]})
        for p in video:
            at = (p["start"] + p["end"]) / 2
            res = check_video(rpc, p, directory / "frames", duration, fps, _covered(doc, p, at), export=export)
            report["pieces"].append(dict(_brief(p), **res))
        report["notes"].append("Video: one frame in the middle of each piece must not be empty; where another video "
                               "track has a clip too (covered) a drawn frame proves nothing: uncertain.")
    for r in report["pieces"]:
        where = f"{r['track']} {r['start']:.3f}-{r['end']:.3f}s {r['name']}"
        if r["status"] in PROBLEMS:
            report["problems"].append(f"{where}: {r['status']} ({r.get('detail', '')})")
        elif r["status"] in ("uncertain", "unverified"):
            report["warnings"].append(f"{where}: {r['status']} ({r.get('detail', '')})")
    report["ok"] = not report["problems"]
    if report["ok"] and report.get("render", {}).get("rendered"):
        Path(report["render"]["rendered"]).unlink(missing_ok=True)  # 48 kHz stereo: ~11 MB a minute; kept on failure
        report["render"]["rendered"] = None
    report["seconds"] = round(time.monotonic() - started, 2)
    return report


def _brief(p: dict) -> dict:
    return {"track": p["track"], "name": p.get("name"), "start": round(p["start"], 6), "end": round(p["end"], 6),
            "inPoint": None if p.get("inPoint") is None else round(p["inPoint"], 6),
            "outPoint": None if p.get("outPoint") is None else round(p["outPoint"], 6)}


def repair_hint() -> str:
    return ("A blank video piece: a one-frame nudge and back repaired the picture live on 27.1 where the track had "
            "room (trim_clip moveTo refuses a nudge onto a neighbour); otherwise re-place it like audio. A silent "
            "audio piece: a nudge did NOT repair it; remove it and place the same source range again (insert_clip "
            "--in/--out on its track), then restore its volume and effects (set_volume, set_params). Check again "
            "with --render-check, or review_cuts --render and export_frames.")
