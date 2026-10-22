"""
transcript_digest — turn Adobe transcript JSON into a sentence list.

This is the artifact an agent reads for text-domain editing: every sentence with speaker, start/end in seconds,
and average ASR confidence, so cut decisions can be made in text and mapped straight back to timecodes.

Sentence boundaries come from the word-level `eos` flag in Adobe's format; segment boundaries are a fallback when
a segment ends without one. Punctuation tokens are attached to the word before them. Times are seconds relative
to the SOURCE CLIP (not the sequence).

Usage:
  python transcript_digest.py transcript.json            # human-readable
  python transcript_digest.py transcript.json --json     # machine-readable
  python transcript_digest.py transcript.json --min-conf 0.8   # flag shaky ASR
"""

import argparse
import json

from .console import utf8_stdio
from .errors import BridgeError
from .transcript import read_transcript


def tc(seconds: float) -> str:
    """mm:ss.s — compact timecode for humans."""
    m, s = divmod(max(seconds, 0.0), 60.0)
    return f"{int(m):02d}:{s:04.1f}"


def digest(doc: dict) -> dict:
    speakers = {s.get("id"): s.get("name") or s.get("id") for s in doc.get("speakers", [])}
    lang = (doc.get("language") or "").lower()
    joiner = "" if lang.startswith(("zh", "ja", "cmn", "yue", "th")) else " "
    sentences = []

    def flush(speaker, first_start, end, words, confs):
        sentences.append({
            "speaker": speaker,
            "start": round(first_start, 3),
            "end": round(end, 3),
            "text": joiner.join(words),
            "avgConfidence": round(sum(confs) / len(confs), 3) if confs else None,
        })

    for seg in doc.get("segments", []):
        speaker = speakers.get(seg.get("speaker"), seg.get("speaker") or "Unknown")
        words, confs = [], []
        first_start, end = None, 0.0

        for w in seg.get("words", []):
            text = (w.get("text") or "").strip()
            kind = w.get("type")
            if kind == "punctuation":
                if words and text:  # belongs to the word before it; a sentence never starts with it
                    words[-1] += text
                    if w.get("eos"):
                        flush(speaker, first_start, end, words, confs)
                        words, confs, first_start = [], [], None
                continue
            if kind and kind != "word":
                continue  # skip other non-word tokens if present
            if not text:
                continue
            if first_start is None:
                first_start = w.get("start", seg.get("start", 0.0))
            words.append(text)
            if isinstance(w.get("confidence"), (int, float)):
                confs.append(w["confidence"])
            end = w.get("start", 0.0) + w.get("duration", 0.0)

            if w.get("eos"):
                flush(speaker, first_start, end, words, confs)
                words, confs, first_start = [], [], None

        if words:  # segment ended without an eos — flush as a sentence anyway
            flush(speaker, first_start, end, words, confs)

    total = sentences[-1]["end"] if sentences else 0.0
    return {
        "language": doc.get("language"),
        "speakers": sorted(set(s["speaker"] for s in sentences)),
        "sentenceCount": len(sentences),
        "durationSeconds": round(total, 3),
        "sentences": sentences,
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="transcript_digest", description="Adobe transcript JSON -> sentence list")
    ap.add_argument("path", help="Adobe transcript JSON file (from transcribe.py or ppro.py export_transcript)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ap.add_argument("--min-conf", type=float, default=None,
                    help="Mark sentences whose average ASR confidence is below this")
    return ap


def main(argv=None) -> int:
    if argv is None:  # run as a program; an in-process call leaves the host's stdio alone
        utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        _, doc = read_transcript(args.path)
    except BridgeError as e:
        reply = e.to_reply()
        reply["error"] = f"Cannot read transcript: {reply['error']}"
        print(json.dumps(reply, ensure_ascii=False))
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
