#!/usr/bin/env python3
"""
transcript_digest — turn Adobe Transcript JSON into a sentence list.

This is the artifact Claude reads for text-domain editing: every sentence
with speaker, start/end in seconds, and average ASR confidence, so cut
decisions can be made in text and mapped straight back to timecodes.

Sentence boundaries come from the word-level `eos` flag in Adobe's format;
segment boundaries are a fallback when a segment ends without one.
Times are seconds relative to the SOURCE CLIP (not the sequence).

Usage:
  python transcript_digest.py transcript.json            # human-readable
  python transcript_digest.py transcript.json --json     # machine-readable
  python transcript_digest.py transcript.json --min-conf 0.8   # flag shaky ASR
"""

import argparse
import json
import sys
from pathlib import Path


def tc(seconds: float) -> str:
    """mm:ss.s — compact timecode for humans."""
    m, s = divmod(max(seconds, 0.0), 60.0)
    return f"{int(m):02d}:{s:04.1f}"


def digest(doc: dict) -> dict:
    speakers = {s.get("id"): s.get("name") or s.get("id") for s in doc.get("speakers", [])}
    lang = (doc.get("language") or "").lower()
    joiner = "" if lang.startswith(("zh", "ja", "cmn", "yue")) else " "
    sentences = []

    for seg in doc.get("segments", []):
        speaker = speakers.get(seg.get("speaker"), seg.get("speaker") or "Unknown")
        words, confs = [], []
        first_start = None

        for w in seg.get("words", []):
            if w.get("type") and w["type"] != "word":
                continue  # skip non-word tokens if present
            text = (w.get("text") or "").strip()
            if not text:
                continue
            if first_start is None:
                first_start = w.get("start", seg.get("start", 0.0))
            words.append(text)
            if isinstance(w.get("confidence"), (int, float)):
                confs.append(w["confidence"])
            end = w.get("start", 0.0) + w.get("duration", 0.0)

            if w.get("eos"):
                sentences.append({
                    "speaker": speaker,
                    "start": round(first_start, 3),
                    "end": round(end, 3),
                    "text": joiner.join(words),
                    "avgConfidence": round(sum(confs) / len(confs), 3) if confs else None,
                })
                words, confs, first_start = [], [], None

        if words:  # segment ended without an eos — flush as a sentence anyway
            sentences.append({
                "speaker": speaker,
                "start": round(first_start, 3),
                "end": round(end, 3),
                "text": joiner.join(words),
                "avgConfidence": round(sum(confs) / len(confs), 3) if confs else None,
            })

    total = sentences[-1]["end"] if sentences else 0.0
    return {
        "language": doc.get("language"),
        "speakers": sorted(set(s["speaker"] for s in sentences)),
        "sentenceCount": len(sentences),
        "durationSeconds": round(total, 3),
        "sentences": sentences,
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="transcript_digest")
    ap.add_argument("path", help="Adobe Transcript JSON file (from ppro.py export_transcript)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ap.add_argument("--min-conf", type=float, default=None,
                    help="Mark sentences whose average ASR confidence is below this")
    args = ap.parse_args()

    try:
        doc = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": f"Cannot read transcript: {e}"}))
        return 1

    d = digest(doc)

    if args.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0

    print(f"Language: {d['language']}   Speakers: {', '.join(d['speakers']) or '-'}   "
          f"Sentences: {d['sentenceCount']}   Runs to: {tc(d['durationSeconds'])}")
    print("-" * 78)
    for i, s in enumerate(d["sentences"], 1):
        flag = ""
        if args.min_conf is not None and s["avgConfidence"] is not None and s["avgConfidence"] < args.min_conf:
            flag = f"  [low conf {s['avgConfidence']}]"
        print(f"{i:>3}. [{tc(s['start'])} - {tc(s['end'])}] {s['speaker']}: {s['text']}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
