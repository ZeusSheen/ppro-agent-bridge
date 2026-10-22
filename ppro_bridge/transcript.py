"""Adobe transcript JSON: language codes, reading, a structural check and word counts.

The check is our own, written from the published facts about Premiere's transcript format (it is not Adobe's
schema file): top-level `language`, `segments`, `speakers` and nothing else; at least one segment, each with
`start`, `duration`, `language`, `speaker` and at least one word; words with `text`, `start`, `duration`, `eos`,
`confidence` (0..1), `tags` (a subset of profanity/filler) and `type` (word or punctuation); speaker ids are
UUID v4 strings listed in `speakers`; languages come from a fixed list of 30 codes (lowercase), "??-??" = unknown.

These JSON codes are not Premiere Speech-to-Text's `language` option (en-US style, from querySupportedLanguages):
transcribe_clip takes those; transcripts carry these.
"""

import json
import math
import re
from pathlib import Path

from .errors import BridgeError

FALLBACK_LANGUAGE = "??-??"

LANGUAGES = (
    "en-us", "en-gb", "zh-hk", "cmn-hans", "cmn-hant", "es-es", "de-de", "fr-fr", "ja-jp", "pt-pt",
    "pt-br", "ko-kr", "it-it", "ru-ru", "hi-in", "nb-no", "sv-se", "nl-nl", "da-dk", "id-id",
    "th-th", "vi-vn", "ms-my", "tr-tr", "pl-pl", "fil-ph", "te-in", "ml-in", "pa-in", FALLBACK_LANGUAGE,
)

# Whisper language code -> transcript language code. Anything not listed maps to "??-??".
ADOBE_LANG = {
    "en": "en-us", "zh": "cmn-hans", "yue": "zh-hk", "es": "es-es", "de": "de-de", "fr": "fr-fr",
    "ja": "ja-jp", "pt": "pt-br", "ko": "ko-kr", "it": "it-it", "ru": "ru-ru", "hi": "hi-in",
    "nb": "nb-no", "no": "nb-no", "nn": "nb-no", "sv": "sv-se", "nl": "nl-nl", "da": "da-dk",
    "id": "id-id", "th": "th-th", "vi": "vi-vn", "ms": "ms-my", "tr": "tr-tr", "pl": "pl-pl",
    "tl": "fil-ph", "te": "te-in", "ml": "ml-in", "pa": "pa-in",
}

TOP_KEYS = ("language", "segments", "speakers")
WORD_KEYS = ("text", "start", "duration", "eos", "confidence", "tags", "type")
TAGS = {"profanity", "filler"}
TYPES = {"word", "punctuation"}
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)


def adobe_language(code: str | None) -> str:
    """The transcript language for a Whisper code ("zh" -> "cmn-hans") or an already valid transcript code (any
    case); "??-??" when there is none."""
    if not code:
        return FALLBACK_LANGUAGE
    c = str(code).strip().lower().replace("_", "-")
    if c in LANGUAGES:
        return c
    return ADOBE_LANG.get(c, FALLBACK_LANGUAGE)


# Codes transcribe.py 0.3.x wrote that are not transcript codes: upgraded on import, with a warning.
LEGACY_LANGUAGES = {"zh-cn": "cmn-hans", "se-se": "sv-se", "fi-fi": FALLBACK_LANGUAGE, "ar-sa": FALLBACK_LANGUAGE}


def upgrade_legacy_languages(doc: dict) -> list[str]:
    """Rewrite 0.3.x language codes (top level and per segment) in place; returns what changed."""
    changed = set()
    holders = [doc] + [seg for seg in doc.get("segments") or [] if isinstance(seg, dict)]
    for h in holders:
        code = h.get("language")
        if isinstance(code, str) and code.lower() in LEGACY_LANGUAGES:
            h["language"] = LEGACY_LANGUAGES[code.lower()]
            changed.add(f"{code} -> {h['language']}")
    return sorted(changed)


def read_transcript(path) -> tuple[str, dict]:
    """(text, parsed object) of a transcript file (UTF-8, BOM tolerated). FILE_NOT_FOUND / TRANSCRIPT_INVALID
    (not JSON, or not a JSON object); IO_ERROR when it cannot be read."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise BridgeError(f"File not found: {p.resolve()}", code="FILE_NOT_FOUND", outcome="not_committed")
    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise BridgeError(f"{p.name} is not UTF-8 text ({e}).", code="TRANSCRIPT_INVALID",
                          outcome="not_committed") from e
    except OSError as e:
        raise BridgeError(f"Cannot read {p}: {e}", code="IO_ERROR", outcome="not_committed") from e
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise BridgeError(f"{p.name} is not valid JSON ({e}).", code="TRANSCRIPT_INVALID",
                          outcome="not_committed") from e
    if not isinstance(doc, dict):
        raise BridgeError(f"{p.name} is not a transcript (expected a JSON object).", code="TRANSCRIPT_INVALID",
                          outcome="not_committed")
    return text, doc


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate(doc) -> list[str]:
    """Problems that would keep Premiere from importing this transcript (empty list = looks fine)."""
    if not isinstance(doc, dict):
        return ["transcript: expected a JSON object"]
    problems = [f"{k}: required" for k in TOP_KEYS if k not in doc]
    problems += [f"{k}: unknown top-level key" for k in doc if k not in TOP_KEYS]

    if "language" in doc and doc["language"] not in LANGUAGES:
        problems.append(f"language: {doc['language']!r} is not a transcript language code")

    speaker_ids = set()
    speakers = doc.get("speakers")
    if "speakers" in doc:
        if not isinstance(speakers, list):
            problems.append("speakers: expected an array")
            speakers = []
        for i, s in enumerate(speakers):
            where = f"speakers[{i}]"
            if not isinstance(s, dict):
                problems.append(f"{where}: expected an object")
                continue
            sid = s.get("id")
            if not isinstance(sid, str) or not _UUID4.match(sid):
                problems.append(f"{where}.id: {sid!r} is not a UUID v4")
            else:
                speaker_ids.add(sid)
            if "name" in s and not isinstance(s["name"], str):
                problems.append(f"{where}.name: expected a string")

    segments = doc.get("segments")
    if "segments" in doc:
        if not isinstance(segments, list):
            problems.append("segments: expected an array")
            segments = []
        elif not segments:
            problems.append("segments: needs at least one segment")
        for i, seg in enumerate(segments):
            problems += _segment_problems(seg, f"segments[{i}]", speaker_ids)
    return problems


def _segment_problems(seg, where: str, speaker_ids: set) -> list[str]:
    if not isinstance(seg, dict):
        return [f"{where}: expected an object"]
    problems = []
    for key in ("start", "duration"):
        if key not in seg:
            problems.append(f"{where}.{key}: required")
        elif not _number(seg[key]) or seg[key] < 0:
            problems.append(f"{where}.{key}: expected a number >= 0")
    if "language" not in seg:
        problems.append(f"{where}.language: required")
    elif seg["language"] not in LANGUAGES:
        problems.append(f"{where}.language: {seg['language']!r} is not a transcript language code")
    if "speaker" not in seg:
        problems.append(f"{where}.speaker: required")
    elif not isinstance(seg["speaker"], str) or not _UUID4.match(seg["speaker"]):
        problems.append(f"{where}.speaker: {seg['speaker']!r} is not a UUID v4")
    elif seg["speaker"] not in speaker_ids:
        problems.append(f"{where}.speaker: {seg['speaker']} is not listed in speakers")
    words = seg.get("words")
    if "words" not in seg:
        problems.append(f"{where}.words: required")
    elif not isinstance(words, list):
        problems.append(f"{where}.words: expected an array")
    elif not words:
        problems.append(f"{where}.words: needs at least one word")
    else:
        for j, w in enumerate(words):
            problems += _word_problems(w, f"{where}.words[{j}]")
    return problems


def _word_problems(w, where: str) -> list[str]:
    if not isinstance(w, dict):
        return [f"{where}: expected an object"]
    problems = [f"{where}.{k}: required" for k in WORD_KEYS if k not in w]
    if "text" in w and not isinstance(w["text"], str):
        problems.append(f"{where}.text: expected a string")
    for key in ("start", "duration"):
        if key in w and (not _number(w[key]) or w[key] < 0):
            problems.append(f"{where}.{key}: expected a number >= 0")
    if "eos" in w and not isinstance(w["eos"], bool):
        problems.append(f"{where}.eos: expected true or false")
    if "confidence" in w and (not _number(w["confidence"]) or not 0 <= w["confidence"] <= 1):
        problems.append(f"{where}.confidence: expected a number from 0 to 1")
    if "tags" in w:
        if not isinstance(w["tags"], list):
            problems.append(f"{where}.tags: expected an array")
        else:
            bad = [t for t in w["tags"] if t not in TAGS]
            if bad:
                problems.append(f"{where}.tags: {bad} not in {sorted(TAGS)}")
    if "type" in w and w["type"] not in TYPES:
        problems.append(f"{where}.type: {w['type']!r} is not word or punctuation")
    return problems


def count_words(doc) -> int:
    """Words in a transcript: tokens typed "word" or untyped (what the panel compares after an import)."""
    if not isinstance(doc, dict):
        return 0
    n = 0
    for seg in doc.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        for w in seg.get("words") or []:
            if isinstance(w, dict) and w.get("type", "word") in ("word", None):
                n += 1
    return n
