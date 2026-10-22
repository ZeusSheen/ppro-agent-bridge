"""
transcribe — local Whisper transcription to Adobe transcript JSON.

Runs faster-whisper on a media file entirely on this machine (no cloud, no API key; model weights download once
from Hugging Face on first run) and writes Adobe-format transcript JSON. The output feeds transcript_digest.py
directly, and can be attached to a clip in Premiere via:
    python ppro.py import_transcript --item "<clip name>" --file <output.json>

Usage:
  python transcribe.py video.mp4 --output tr.json
  python transcribe.py video.mp4 --output tr.json --language zh --model medium
  python transcribe.py video.mp4 --output tr.json --digest      # also print sentences

Install (one-time):  pip install faster-whisper
GPU: NVIDIA + CUDA is auto-detected; otherwise runs on CPU (pick a smaller --model like "medium" or "small" if
CPU is too slow).
"""

import argparse
import json
import os
import sys
import unicodedata
import uuid
from pathlib import Path

from . import transcript
from .console import utf8_stdio
from .errors import BridgeError

ADOBE_LANG = transcript.ADOBE_LANG  # 0.3.3 name (Whisper code -> Adobe transcript code)

SENTENCE_ENDINGS = tuple(".!?。！？…")


def _is_punctuation(text: str) -> bool:
    return bool(text) and all(unicodedata.category(ch).startswith("P") for ch in text)


def to_adobe(segments, language: str, speaker_name: str) -> dict:
    """Convert faster-whisper segments (word_timestamps=True) to an Adobe transcript. Tokens made only of
    punctuation are typed "punctuation"; segments without a single word are left out."""
    spk_id = str(uuid.uuid4())
    out_segments = []

    for seg in segments:
        words = []
        seg_words = list(getattr(seg, "words", None) or [])
        for i, w in enumerate(seg_words):
            text = (getattr(w, "word", "") or "").strip()
            if not text:
                continue
            is_last = i == len(seg_words) - 1
            start = max(float(w.start), 0.0)
            words.append({
                "text": text,
                "start": round(start, 3),
                "duration": round(max(float(w.end) - start, 0.0), 3),
                "eos": bool(text.endswith(SENTENCE_ENDINGS) or is_last),
                "confidence": round(min(max(float(getattr(w, "probability", 1.0)), 0.0), 1.0), 3),
                "tags": [],
                "type": "punctuation" if _is_punctuation(text) else "word",
            })
        if not any(w["type"] == "word" for w in words):
            continue
        out_segments.append({
            "start": words[0]["start"],
            "duration": round(max(words[-1]["start"] + words[-1]["duration"] - words[0]["start"], 0.0), 3),
            "language": language,
            "speaker": spk_id,
            "words": words,
        })

    return {
        "language": language,
        "segments": out_segments,
        "speakers": [{"id": spk_id, "name": speaker_name}],
    }


def _register_cuda_dlls() -> list[str] | None:
    """On Windows, cuBLAS/cuDNN installed via pip (nvidia-cublas-cu12, nvidia-cudnn-cu12) live inside
    site-packages, off the DLL search path. ctranslate2 loads them with classic LoadLibrary, which only honors PATH
    (os.add_dll_directory is ignored by that call style) — so we do both: prepend the bin dirs to PATH and register
    them the modern way too. Returns the directories found (None off Windows)."""
    if sys.platform != "win32":
        return None
    import site
    roots = list(site.getsitepackages())
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    found = []
    for root in roots:
        nvidia = Path(root) / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            if any(bin_dir.glob("*.dll")):
                found.append(str(bin_dir))
                try:
                    os.add_dll_directory(str(bin_dir))
                except OSError:
                    pass
    if found:
        os.environ["PATH"] = os.pathsep.join(found) + os.pathsep + os.environ.get("PATH", "")
    return found


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="transcribe", description="Local Whisper transcription to Adobe transcript JSON")
    ap.add_argument("media", help="Audio/video file to transcribe")
    ap.add_argument("--output", required=True, help="Where to write Adobe transcript JSON")
    ap.add_argument("--model", default="large-v3",
                    help="Whisper model: large-v3 (best), medium, small (fastest). Default: large-v3")
    ap.add_argument("--language", default=None,
                    help="Source language code for Whisper, e.g. en, zh, yue. Default: auto-detect")
    ap.add_argument("--adobe-language", default=None,
                    help="Transcript language code to stamp in the JSON (e.g. en-gb, cmn-hant). "
                         "Default: mapped from the Whisper language, ??-?? when there is no mapping")
    ap.add_argument("--speaker", default="Speaker 1", help="Speaker name to stamp (Whisper does not diarize)")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--digest", action="store_true", help="Also print the sentence digest after transcribing")
    return ap


def _run(args) -> int:
    media = Path(args.media).expanduser().resolve()
    if not media.is_file():
        raise BridgeError(f"File not found: {media}", code="FILE_NOT_FOUND", outcome="not_committed")
    override = None
    if args.adobe_language:
        override = args.adobe_language.strip().lower()
        if override not in transcript.LANGUAGES:
            raise BridgeError(f"--adobe-language {args.adobe_language!r} is not a transcript language code; use one "
                              f"of {', '.join(transcript.LANGUAGES)}.", code="BAD_PARAMS", outcome="not_committed")

    found = _register_cuda_dlls()  # Windows: make pip-installed NVIDIA DLLs findable
    if found:
        print(f"CUDA DLL dirs on PATH: {len(found)}", file=sys.stderr)
    elif found is not None:
        print("Warning: no pip-installed NVIDIA DLLs found (pip install nvidia-cublas-cu12 nvidia-cudnn-cu12). "
              "If GPU loading fails, run with --device cpu.", file=sys.stderr)

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise BridgeError("faster-whisper not installed. Run: pip install faster-whisper",
                          code="MISSING_DEPENDENCY", outcome="not_committed") from e

    device = args.device
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = "int8" if device == "cpu" else "float16"

    print(f"Loading model {args.model} on {device} ({compute}) ... first run downloads weights, please wait.",
          file=sys.stderr)
    model = WhisperModel(args.model, device=device, compute_type=compute)
    segments, info = model.transcribe(str(media), language=args.language, word_timestamps=True, vad_filter=True)

    detected = args.language or getattr(info, "language", None)
    adobe_lang = override or transcript.adobe_language(detected)
    if adobe_lang == transcript.FALLBACK_LANGUAGE:
        print(f"Note: no transcript language code for Whisper language {detected!r}; stamping "
              f"{transcript.FALLBACK_LANGUAGE} (pass --adobe-language to choose one).", file=sys.stderr)
    probability = getattr(info, "language_probability", None)
    print(f"Transcribing ({detected}" + (f", confidence {probability:.2f}" if probability is not None else "")
          + ") ...", file=sys.stderr)
    doc = to_adobe(segments, adobe_lang, args.speaker)

    n_words = transcript.count_words(doc)
    if not n_words:
        raise BridgeError(f"Whisper found no speech in {media.name}; nothing was written.", code="NO_SPEECH",
                          outcome="not_committed")

    out = Path(args.output).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"))

    result = {"ok": True, "savedTo": str(out), "language": adobe_lang, "segments": len(doc["segments"]),
              "words": n_words, "hint": f"python transcript_digest.py {out}"}
    problems = transcript.validate(doc)
    if problems:
        result["warning"] = "The transcript fails the format check: " + "; ".join(problems[:5])
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.digest:
        from . import digest
        digest.main([str(out)])
    return 0


def main(argv=None) -> int:
    if argv is None:  # run as a program; an in-process call leaves the host's stdio alone
        utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except BridgeError as e:
        print(json.dumps(e.to_reply(), ensure_ascii=False, indent=2))
        return 1
