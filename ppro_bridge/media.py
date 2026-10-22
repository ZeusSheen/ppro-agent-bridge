"""Local media facts (ffprobe) and fit/fill scale arithmetic."""

import json
import os
import shutil
import subprocess


def _rate(text) -> float | None:
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den or 1)
        return value if value > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(path: str | None) -> dict:
    """Width/height/hasAudio, plus duration, frame count and native fps of the video stream, via ffprobe.

    {} whenever ffprobe cannot tell: not on PATH, exits non-zero (missing or unreadable file), prints no JSON, or
    finds no streams. Callers treat a missing "hasAudio" as unknown — never as "silent"."""
    if not path or not shutil.which("ffprobe"):
        return {}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,nb_frames,r_frame_rate,avg_frame_rate:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        doc = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    streams = doc.get("streams") if isinstance(doc, dict) else None
    if not streams:
        return {}
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width")), None)
    info = {"hasAudio": any(s.get("codec_type") == "audio" for s in streams)}
    try:
        info["duration"] = float((doc.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        pass
    if video:
        info["width"], info["height"] = int(video["width"]), int(video["height"])
        fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
        if fps:
            info["fps"] = fps
            frames = video.get("nb_frames")
            if str(frames).isdigit() and int(frames) > 0:
                info["frames"] = int(frames)
            elif info.get("duration"):
                info["frames"] = round(info["duration"] * fps)
    return info


def fit_scale(mode: str, media: dict, seq: dict) -> float | None:
    """Percent that fits (letterbox) or fills (crop) the media into the sequence frame."""
    if not media.get("width") or not seq.get("width"):
        return None
    sx, sy = seq["width"] / media["width"], seq["height"] / media["height"]
    return round((min(sx, sy) if mode == "fit" else max(sx, sy)) * 100, 3)


WAV_PRESET_PARTS = ("MediaIO", "systempresets", "3F3F3F3F_57415645", "Waveform Audio 48kHz 16-bit.epr")


def premiere_executables() -> list[str]:
    """Paths of running Premiere Pro executables (Windows; [] elsewhere or when none runs)."""
    if os.name != "nt":
        return []
    cmd = ("Get-Process | Where-Object { $_.ProcessName -like 'Adobe Premiere Pro*' } "
           "| ForEach-Object { $_.Path }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def find_wav_preset() -> str | None:
    """Premiere's own 'Waveform Audio 48kHz 16-bit' export preset: PPRO_WAV_PRESET, else next to the running
    Premiere executable. None when it cannot be found (pass --preset)."""
    env = os.environ.get("PPRO_WAV_PRESET")
    if env and os.path.isfile(env):
        return env
    for exe in premiere_executables():
        candidate = os.path.join(os.path.dirname(exe), *WAV_PRESET_PARTS)
        if os.path.isfile(candidate):
            return candidate
    return None
