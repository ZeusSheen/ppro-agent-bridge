"""Local media facts (ffprobe) and fit/fill scale arithmetic."""

import json
import os
import shutil
import subprocess
import tempfile


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


def _rotation(stream: dict) -> int:
    """The stream's display rotation in degrees, 0-359 (0 when none or unreadable). ffprobe gives it as the
    display matrix's "rotation" in side_data_list (-90 for a clip shot one way, 90 the other) or, from older
    muxers, as a "rotate" tag. Only multiples of 90 matter for the frame size; others are rounded to the nearest."""
    values = [d.get("rotation") for d in stream.get("side_data_list") or [] if isinstance(d, dict)]
    values.append((stream.get("tags") or {}).get("rotate"))
    for value in values:
        try:
            turn = int(round(float(value))) % 360
        except (TypeError, ValueError):
            continue
        return 90 * round(turn / 90) % 360
    return 0


def probe_media(path: str | None) -> dict:
    """Width/height/hasAudio, the file's duration, and the video stream's frame count, native fps and own length
    (videoDuration: only when the stream reports one, and never for a still), plus the codec names, via ffprobe.
    Cover art (an attached picture in a FLAC, MP3 or M4A) is not a picture: such a file is audio only, with
    "coverArt": True.
    width/height are the picture as Premiere shows it: a phone or Sony vertical clip stored 3840x2160 with a 90-degree
    rotation (display matrix side data, or the older "rotate" tag) is 2160x3840, with "rotation" (0-359, clockwise as
    ffprobe reports it) and "codedSize" [3840, 2160]. fit_scale uses width/height, so fit and fill come out right.

    {} whenever ffprobe cannot tell: not on PATH, exits non-zero (missing or unreadable file), prints no JSON, or
    finds no streams. Callers treat a missing "hasAudio" as unknown — never as "silent"."""
    if not path or not shutil.which("ffprobe"):
        return {}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,nb_frames,duration,r_frame_rate,avg_frame_rate"
             ":stream_disposition=attached_pic:stream_side_data=rotation:stream_tags=rotate:format=duration",
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
    cover = [s for s in streams if s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic")]
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width") and s not in cover), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    info = {"hasAudio": bool(audio)}
    if cover:
        info["coverArt"] = True
    if audio:
        info["audioCodecs"] = [s.get("codec_name") for s in audio if s.get("codec_name")]
    try:
        info["duration"] = float((doc.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        pass
    if video:
        info["width"], info["height"] = int(video["width"]), int(video["height"])
        turn = _rotation(video)
        if turn:
            info["rotation"] = turn
            if turn % 180 == 90:
                info["codedSize"] = [info["width"], info["height"]]
                info["width"], info["height"] = info["height"], info["width"]
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
# FLAC too: a Universal Production Music download (FLAC 44.1 kHz stereo with cover art, 6 Nov 2026) got a modal
# "文件格式不受支持" (file format not supported) dialog, and importFiles never settled, even after OK — PANEL_TIMEOUT,
# ping "stuck", a panel Reload. The same file as a 48 kHz 24-bit WAV imported fine.
# Files with such a picture are refused before anything is sent; audio-only files are converted to WAV.
UNIMPORTABLE_VIDEO = {"vp8", "vp9", "av1"}
UNIMPORTABLE_AUDIO = {"opus", "vorbis", "flac"}

WAV_RATE = 48000
WAV_CODEC = "pcm_s24le"
WAV_LENGTH_SLACK = 0.05  # seconds: an existing WAV this close to the source's length holds the same audio


def unimportable(info: dict) -> list[str]:
    """The codecs in `info` (from probe_media) that Premiere refuses; [] when none, or when ffprobe could not tell."""
    video = [info["videoCodec"]] if str(info.get("videoCodec", "")).lower() in UNIMPORTABLE_VIDEO else []
    return video + [c for c in info.get("audioCodecs") or [] if str(c).lower() in UNIMPORTABLE_AUDIO]


def audio_only(info: dict) -> bool:
    """ffprobe found audio and no picture (cover art is no picture). False when it could not tell."""
    return bool(info.get("hasAudio")) and not any(k in info for k in ("videoCodec", "width", "frames", "fps"))


def wav_convertible(info: dict) -> bool:
    """An audio-only file in a codec Premiere refuses (FLAC, Opus, Vorbis): import_media and apply_plan turn it
    into a WAV and import that instead."""
    return audio_only(info) and bool(unimportable(info))


# Characters a shell still expands inside double quotes: $ and ` (bash, PowerShell), % and ! (cmd). Windows file names
# may hold every one of them (a " cannot occur). An agent is told to run these suggested commands, so a file name
# must never become shell syntax: such a path is left out of the command line (shell_path).
SHELL_EXPANDS = set("$`%!")


def shell_path(path, role: str) -> str:
    """`path` in double quotes for a suggested command line, or - when it holds a character a shell would expand
    inside them - a placeholder naming its role ("<input file>"), with nothing of the name in it."""
    text = os.path.abspath(str(path))
    if SHELL_EXPANDS & set(text):
        return f'"<{role}: its name holds $ ` % or !, which a shell would expand; pass the path as one argument>"'
    return f'"{text}"'


def wav_command_line(src, out) -> str:
    """The ffmpeg command, as a person would type it, that writes `src` as the WAV Premiere imports (shell_path: a
    name a shell would expand is not written into it)."""
    return (f'ffmpeg -i {shell_path(src, "input file")} -vn -af aresample={WAV_RATE}:resampler=soxr '
            f'-c:a {WAV_CODEC} {shell_path(out, "output file")}')


def transcode_command(path, info: dict) -> str:
    """An ffmpeg command that turns the file into something Premiere imports: a 48 kHz 24-bit WAV for an audio-only
    file, else H.264 + AAC, or only the audio re-encoded when the picture is fine."""
    src = os.path.abspath(str(path))
    if audio_only(info):
        return wav_command_line(src, os.path.splitext(src)[0] + ".wav")
    out = os.path.splitext(src)[0] + "_premiere.mp4"
    video = str(info.get("videoCodec", "")).lower() in UNIMPORTABLE_VIDEO
    v = "-c:v libx264 -crf 16 -g 50" if video else "-c:v copy"
    return f'ffmpeg -i {shell_path(src, "input file")} {v} -c:a aac -b:a 320k {shell_path(out, "output file")}'


def _same_audio(wav_info: dict, src_info: dict) -> bool:
    """`wav_info` is a PCM file as long as the source (an earlier conversion of it)."""
    codecs = wav_info.get("audioCodecs") or []
    a, b = wav_info.get("duration"), src_info.get("duration")
    return (bool(codecs) and all(str(c).startswith("pcm_") for c in codecs) and not wav_info.get("videoCodec")
            and a is not None and b is not None and abs(a - b) <= WAV_LENGTH_SLACK)


def wav_target(src, info: dict, probe=None) -> dict:
    """Where the WAV for `src` goes: "<name>.wav" beside it, else "<name> (48k).wav". Reads only.
    {"to", "reuse"}: reuse when that file already holds this audio as PCM (an earlier conversion, the user's own
    included); {"error"} when both names hold something else."""
    probe = probe or probe_media
    base = os.path.splitext(os.path.abspath(str(src)))[0]
    taken = []
    for out in (base + ".wav", base + " (48k).wav"):
        if not os.path.lexists(out):  # a link of that name, even a dangling one, counts as taken
            return {"to": out, "reuse": False}
        if os.path.isfile(out) and _same_audio(probe(out) or {}, info):
            return {"to": out, "reuse": True}
        taken.append(out)
    return {"error": f"{' and '.join(taken)} already exist and hold other audio; convert it by hand"}


def _ffmpeg_wav(src, out, run) -> str | None:
    """Write `src` as a 48 kHz 24-bit WAV at `out` (overwritten). None on success, else ffmpeg's complaint."""
    err = None
    for resampler in (f"aresample={WAV_RATE}:resampler=soxr", f"aresample={WAV_RATE}"):
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-sn",
               "-af", resampler, "-c:a", WAV_CODEC, "-f", "wav", str(out)]
        try:
            proc = run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        except (OSError, subprocess.SubprocessError) as e:
            return f"ffmpeg did not run: {e}"
        if proc.returncode == 0:
            return None
        lines = (proc.stderr or "").strip().splitlines()
        err = lines[-1] if lines else f"ffmpeg exited with {proc.returncode}"
        if "soxr" not in (proc.stderr or "").lower():  # only a build without libsoxr is worth a second try
            break
    return err


def convert_to_wav(src, info: dict, *, probe=None, run=None) -> dict:
    """Turn an audio-only file Premiere refuses (wav_convertible) into a 48 kHz 24-bit WAV beside it, or reuse an
    earlier conversion (wav_target). Written to a ".part" file that is renamed only once ffprobe finds it PCM and as
    long as the source, so a broken conversion never passes for one later.
    {"from", "to", "reused", "codecs", "command"}; on failure {"from", "error", "command"} and nothing is left."""
    probe = probe or probe_media
    run = run or subprocess.run
    src = os.path.abspath(str(src))
    target = wav_target(src, info, probe)
    if "error" in target:
        return {"from": src, "error": target["error"], "command": transcode_command(src, info)}
    out = target["to"]
    done = {"from": src, "to": out, "reused": target["reuse"], "codecs": unimportable(info),
            "command": wav_command_line(src, out)}
    if target["reuse"]:
        return done
    if not shutil.which("ffmpeg") and run is subprocess.run:
        return {"from": src, "error": "ffmpeg is not on PATH (install ffmpeg)", "command": done["command"]}
    if os.path.lexists(out):  # wav_target saw nothing there: a link (even a dangling one) or a race - never written
        return {"from": src, "error": f"{out} appeared while converting; nothing was written",
                "command": done["command"]}
    # ffmpeg writes a file this call has just created (O_EXCL, a random name beside the target): never a name that
    # someone could have planted beforehand, such as a link "<name>.wav.part" pointing at another file, which
    # ffmpeg -y would overwrite through.
    try:
        fd, part = tempfile.mkstemp(prefix=os.path.basename(base_of(out)) + ".", suffix=".wav.part",
                                    dir=os.path.dirname(out))
        os.close(fd)
    except OSError as e:
        return {"from": src, "error": f"cannot write next to {src}: {e}", "command": done["command"]}
    why = _ffmpeg_wav(src, part, run)
    if why is None:
        written = probe(part) or {}
        if not _same_audio(written, info):
            why = (f"the WAV ffmpeg wrote does not check out (ffprobe: {written.get('audioCodecs')}, "
                   f"{written.get('duration')} s against {info.get('duration')} s)")
        elif os.path.lexists(out):
            why = f"{out} appeared while converting; the conversion was not moved over it"
        else:
            try:
                os.replace(part, out)
                return done
            except OSError as e:
                why = f"cannot rename {part}: {e}"
    try:
        os.remove(part)
    except OSError:
        pass
    return {"from": src, "error": why, "command": done["command"]}


def base_of(path: str) -> str:
    """"x.wav" -> "x" (the name the temporary file starts with)."""
    return os.path.splitext(path)[0]


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
