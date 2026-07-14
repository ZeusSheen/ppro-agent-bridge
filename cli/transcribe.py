#!/usr/bin/env python3
"""
transcribe — local Whisper transcription to Adobe Transcript JSON.

Runs faster-whisper on a media file entirely on this machine (no cloud, no
API key; model weights download once from Hugging Face on first run) and
writes Adobe-spec transcript JSON. The output feeds transcript_digest.py
directly, and can be imported into Premiere via:
    python ppro.py import_transcript --item "<clip name>" --file <output.json>

Usage:
  python transcribe.py video.mp4 --output tr.json
  python transcribe.py video.mp4 --output tr.json --language zh --model medium
  python transcribe.py video.mp4 --output tr.json --digest      # also print sentences

Install (one-time):  pip install faster-whisper
GPU: NVIDIA + CUDA is auto-detected; otherwise runs on CPU (pick a smaller
--model like "medium" or "small" if CPU is too slow).
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

SENTENCE_ENDINGS = tuple(".!?。！？…")

# Whisper language -> Adobe language code (best-effort; override with --adobe-language)
ADOBE_LANG = {
    "en": "en-us", "zh": "zh-cn", "fr": "fr-fr", "de": "de-de", "es": "es-es",
    "it": "it-it", "ja": "ja-jp", "ko": "ko-kr", "pt": "pt-br", "ru": "ru-ru",
    "hi": "hi-in", "ar": "ar-sa", "nl": "nl-nl", "da": "da-dk", "sv": "se-se",
    "nb": "nb-no", "fi": "fi-fi", "pl": "pl-pl", "tr": "tr-tr",
}


def to_adobe(segments, language: str, speaker_name: str) -> dict:
    """Convert faster-whisper segments (word_timestamps=True) to Adobe spec."""
    spk_id = str(uuid.uuid4())
    out_segments = []

    for seg in segments:
        words = []
        seg_words = list(getattr(seg, "words", None) or [])
        for i, w in enumerate(seg_words):
            text = (w.word or "").strip()
            if not text:
                continue
            is_last = i == len(seg_words) - 1
            words.append({
                "text": text,
                "start": round(float(w.start), 3),
                "duration": round(max(float(w.end) - float(w.start), 0.0), 3),
                "eos": bool(text.endswith(SENTENCE_ENDINGS) or is_last),
                "confidence": round(float(getattr(w, "probability", 1.0)), 3),
                "tags": [],
                "type": "word",
            })
        if not words:
            continue
        out_segments.append({
            "start": words[0]["start"],
            "duration": round(words[-1]["start"] + words[-1]["duration"] - words[0]["start"], 3),
            "language": language,
            "speaker": spk_id,
            "words": words,
        })

    return {
        "language": language,
        "segments": out_segments,
        "speakers": [{"id": spk_id, "name": speaker_name}],
    }


def _register_cuda_dlls():
    """On Windows, cuBLAS/cuDNN installed via pip (nvidia-cublas-cu12,
    nvidia-cudnn-cu12) live inside site-packages, off the DLL search path.
    ctranslate2 loads them with classic LoadLibrary, which only honors PATH
    (os.add_dll_directory is ignored by that call style) — so we do both:
    prepend the bin dirs to PATH and register them the modern way too.
    Prints a warning if CUDA DLLs are nowhere to be found."""
    if sys.platform != "win32":
        return
    import os
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
        print(f"CUDA DLL dirs on PATH: {len(found)}", file=sys.stderr)
    else:
        print("Warning: no pip-installed NVIDIA DLLs found "
              "(pip install nvidia-cublas-cu12 nvidia-cudnn-cu12). "
              "If GPU loading fails, run with --device cpu.", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(prog="transcribe")
    ap.add_argument("media", help="Audio/video file to transcribe")
    ap.add_argument("--output", required=True, help="Where to write Adobe transcript JSON")
    ap.add_argument("--model", default="large-v3",
                    help="Whisper model: large-v3 (best), medium, small (fastest). Default: large-v3")
    ap.add_argument("--language", default=None,
                    help="Source language code for Whisper, e.g. en, zh. Default: auto-detect")
    ap.add_argument("--adobe-language", default=None,
                    help="Adobe language code to stamp in the JSON (e.g. en-gb). Default: mapped from detected language")
    ap.add_argument("--speaker", default="Speaker 1", help="Speaker name to stamp (Whisper does not diarize)")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--digest", action="store_true", help="Also print the sentence digest after transcribing")
    args = ap.parse_args()

    media = Path(args.media).expanduser().resolve()
    if not media.is_file():
        print(json.dumps({"ok": False, "error": f"File not found: {media}"}))
        return 1

    _register_cuda_dlls()  # Windows: make pip-installed NVIDIA DLLs findable

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(json.dumps({"ok": False, "error": "faster-whisper not installed. Run: pip install faster-whisper"}))
        return 1

    device = args.device
    compute = "float16"
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    if device == "cpu":
        compute = "int8"

    print(f"Loading model {args.model} on {device} ({compute}) ... "
          f"first run downloads weights, please wait.", file=sys.stderr)
    model = WhisperModel(args.model, device=device, compute_type=compute)

    segments, info = model.transcribe(
        str(media),
        language=args.language,
        word_timestamps=True,
        vad_filter=True,
    )

    detected = args.language or info.language
    adobe_lang = args.adobe_language or ADOBE_LANG.get(detected, "en-us")

    print(f"Transcribing ({detected}, confidence {info.language_probability:.2f}) ...", file=sys.stderr)
    doc = to_adobe(segments, adobe_lang, args.speaker)

    out = Path(args.output).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    n_words = sum(len(s["words"]) for s in doc["segments"])
    print(json.dumps({
        "ok": True, "savedTo": str(out), "language": adobe_lang,
        "segments": len(doc["segments"]), "words": n_words,
        "hint": f"python transcript_digest.py {out}",
    }, ensure_ascii=False, indent=2))

    if args.digest:
        import transcript_digest
        sys.argv = ["transcript_digest", str(out)]
        transcript_digest.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
