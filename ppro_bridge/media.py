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


def _seconds(text) -> float | None:
    try:
        value = float(text)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def probe_media(path: str | None) -> dict:
    """Width/height/hasAudio, the file's duration, and the video stream's frame count, native fps and own length
    (videoDuration: only when the stream reports one, and never for a still), plus the codec names, via ffprobe.

    {} whenever ffprobe cannot tell: not on PATH, exits non-zero (missing or unreadable file), prints no JSON, or
    finds no streams. Callers treat a missing "hasAudio" as unknown — never as "silent"."""
    if not path or not shutil.which("ffprobe"):
        return {}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,nb_frames,duration,r_frame_rate,avg_frame_rate"
             ":format=duration",
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
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    info = {"hasAudio": bool(audio)}
    if audio:
        info["audioCodecs"] = [s.get("codec_name") for s in audio if s.get("codec_name")]
    try:
        info["duration"] = float((doc.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        pass
    if video:
        info["width"], info["height"] = int(video["width"]), int(video["height"])
        if video.get("codec_name"):
            info["videoCodec"] = video["codec_name"]
        fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
        if fps:
            info["fps"] = fps
            frames, length = video.get("nb_frames"), _seconds(video.get("duration"))
            counted = str(frames).isdigit() and int(frames) > 0
            if counted:
                info["frames"] = int(frames)
            elif length:
                info["frames"] = round(length * fps)
            elif info.get("duration"):
                info["frames"] = round(info["duration"] * fps)
            # The picture can end before the file does (the audio running on). A still has one frame and is
            # placed for any length, so it gets no picture length.
            if info.get("frames", 0) > 1 and (length or counted):
                info["videoDuration"] = length or info["frames"] / fps
    return info


# Codecs Premiere 27.1 does not import: YouTube downloads (VP9 / AV1 video with Opus audio in .mp4, 2 Nov 2026) got
# an empty "file import failed" dialog, the import request timed out and the panel stayed stuck until a Reload.
# The bridge refuses them before sending anything.
UNIMPORTABLE_VIDEO = {"vp8", "vp9", "av1"}
UNIMPORTABLE_AUDIO = {"opus", "vorbis"}


def unimportable(info: dict) -> list[str]:
    """The codecs in `info` (from probe_media) that Premiere refuses; [] when none, or when ffprobe could not tell."""
    video = [info["videoCodec"]] if str(info.get("videoCodec", "")).lower() in UNIMPORTABLE_VIDEO else []
    return video + [c for c in info.get("audioCodecs") or [] if str(c).lower() in UNIMPORTABLE_AUDIO]


def transcode_command(path, info: dict) -> str:
    """An ffmpeg command that turns the file into something Premiere imports: H.264 + AAC, or only the audio
    re-encoded when the picture is fine."""
    src = os.path.abspath(str(path))
    out = os.path.splitext(src)[0] + "_premiere.mp4"
    video = str(info.get("videoCodec", "")).lower() in UNIMPORTABLE_VIDEO
    v = "-c:v libx264 -crf 16 -g 50" if video else "-c:v copy"
    return f'ffmpeg -i "{src}" {v} -c:a aac -b:a 320k "{out}"'


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
