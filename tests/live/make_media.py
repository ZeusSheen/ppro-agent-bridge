#!/usr/bin/env python3
"""Make the synthetic media tests/live/live_smoke.py expects, with ffmpeg (on PATH).

  python tests/live/make_media.py DIR [--fps 60000/1001] [--size 1080x1920] [--speech] [--force]

Writes into DIR:
  broll_audio.mp4   6 s test pattern with the frame number burned in, 440 Hz tone (video + audio)
  EN/title.mov      180 frames, blue, "EN <frame>" (no audio)
  CN/title.mov      180 frames, red, "CN <frame>" (same name as EN, different media; no audio)
  speech_en.wav     with --speech: a spoken English sentence from the Windows speech synthesizer (SAPI),
                    for Premiere Speech-to-Text
Frame numbers need a TrueType font (looked up in the Windows font folder, or --font); without one the files are
made without text. Existing files are kept unless --force.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SENTENCE = "This is a smoke test of the agent bridge. The second sentence ends here."


def find_font(explicit):
    if explicit:
        return Path(explicit) if Path(explicit).is_file() else None
    fonts = Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "") / "Fonts"
    for name in ("arial.ttf", "segoeui.ttf", "calibri.ttf"):
        if (fonts / name).is_file():
            return fonts / name
    return None


def drawtext(font, text, size):
    if not font:
        return None
    path = str(font).replace("\\", "/").replace(":", "\\:")  # ffmpeg filter escaping
    return (f"drawtext=fontfile='{path}':text='{text}':fontsize={size}:fontcolor=white:"
            "box=1:boxcolor=black@0.6:boxborderw=20:x=(w-text_w)/2:y=h/6")


def ffmpeg(args):
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"ffmpeg exited {proc.returncode}")


def make_broll(out, size, fps, font):
    vf = drawtext(font, "%{frame_num}", 160)
    ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "6", *(["-vf", vf] if vf else []),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", "-shortest", str(out)])


def make_title(out, size, fps, font, tag, colour):
    vf = drawtext(font, f"{tag} %{{frame_num}}", 200)
    ffmpeg(["-f", "lavfi", "-i", f"color=c={colour}:size={size}:rate={fps}", "-frames:v", "180",
            *(["-vf", vf] if vf else []), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out)])


def make_speech(out):
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        raise RuntimeError("PowerShell not found: the speech file needs the Windows speech synthesizer")
    script = ("Add-Type -AssemblyName System.Speech; "
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$s.SetOutputToWaveFile($env:PAB_SPEECH_OUT); $s.Speak($env:PAB_SPEECH_TEXT); $s.Dispose()")
    proc = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PAB_SPEECH_OUT=str(out), PAB_SPEECH_TEXT=SENTENCE))
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(proc.stderr.strip() or "the speech synthesizer wrote nothing")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="make_media", description=__doc__.split("\n\n")[0])
    p.add_argument("dir", help="output folder (created if missing)")
    p.add_argument("--fps", default="60000/1001", help="frame rate (default 60000/1001 = 59.94)")
    p.add_argument("--size", default="1080x1920", help="frame size WxH (default 1080x1920)")
    p.add_argument("--font", help="TrueType font for the frame numbers")
    p.add_argument("--speech", action="store_true", help="also write speech_en.wav (Windows SAPI)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    args = p.parse_args(argv)
    if not shutil.which("ffmpeg"):
        print("make_media: ffmpeg is not on PATH")
        return 2
    root = Path(args.dir)
    font = find_font(args.font)
    jobs = [("broll_audio.mp4", lambda o: make_broll(o, args.size, args.fps, font)),
            ("EN/title.mov", lambda o: make_title(o, args.size, args.fps, font, "EN", "0x1f4e9a")),
            ("CN/title.mov", lambda o: make_title(o, args.size, args.fps, font, "CN", "0x9a1f2a"))]
    if args.speech:
        jobs.append(("speech_en.wav", make_speech))
    failed = 0
    for rel, make in jobs:
        out = root / rel
        if out.is_file() and not args.force:
            print(f"kept   {out}")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            make(out)
            print(f"wrote  {out}")
        except (RuntimeError, OSError) as e:
            failed += 1
            print(f"FAILED {out}: {e}")
    if not font:
        print("note: no TrueType font found, the media has no frame numbers (pass --font)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
