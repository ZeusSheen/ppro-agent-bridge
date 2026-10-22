#!/usr/bin/env python3
"""
Fake Premiere panel for testing the broker + CLI without Premiere.

Speaks the panel's protocol (ppro_bridge/actions.json, protocol 2) for every action, from an in-memory project:
a 1080x1920 / 59.94 fps sequence "Sequence 01" with 4 video and 4 audio tracks. interview.mp4 (with a
transcript) runs 0-60 s on V1/A1 like a talking-head A-roll; broll_city.mp4 and two title.mov (an EN and a CN
render: same name, different media) wait in the project panel. Imports, placements, effect writes, transitions,
markers, removals, clones, trims and Speech-to-Text jobs change that state, so multi-step flows (apply_plan ->
list_items -> remove_items, replace_clip, split by clone + trim, transcribe_clip -> list_transcripts ->
export_transcript) can be tested end to end. Trim semantics and the audio chain follow live probes of Premiere 27.1
build 17 (see apply_trim and AUDIO_INTRINSIC); export_sequence writes a silent WAV instead of a render.

`dispatch(msg)` is the panel's request handling without the socket, in the panel's order: unknown action, stuck,
denied host, the spec's param check (ppro_bridge.spec.check_params), capability, then the handler under the
request's guard; errors get a code and an outcome by the outcome rule (explicit wins, else not_committed while the
request committed nothing, else unknown; reads never unknown). Tests call it in-process; run as a script it
registers with a broker instead of (not alongside) the real panel. For an isolated test, point broker, panel and
CLI at another port:

  set PPRO_BRIDGE_PORT=8018                 (broker)
  set PPRO_BRIDGE_URL=ws://127.0.0.1:8018   (this script and the CLI)

Test switches (read when used, so in-process tests can set them in os.environ):
  PPRO_FAKE_DELAY=<ms>            every request takes that long (script mode)
  PPRO_FAKE_CONCURRENT=1          run requests side by side, as an unqueued panel would; ping's maxConcurrent
                                  counts the overlap, which must stay 1 behind a broker that serialises
  PPRO_FAKE_MEDIA_SECONDS=<s>     duration given to imported media (default 10)
  PPRO_FAKE_PROJECT_NAME=<name>   project name (default "Smoke Test.prproj")
  PPRO_FAKE_CAPS=<json object>    capability flags merged over all-true, e.g. {"transcribe": false}
  PPRO_FAKE_DENIED=<reason>       this host build is denied: ping only, everything else HOST_DENIED
  PPRO_FAKE_STT_POLLS=<n>         list_transcripts calls until a Speech-to-Text job ends (default 1)
  PPRO_FAKE_STT_RESULT=done|fail|reject   a job attaches a transcript / fails later / is rejected at once
  PPRO_FAKE_TRANSCRIPT_IMPORT=static|callback|empty   import_transcript works with the static form / only with
                                  the callback form / not at all (Premiere attaches an empty shell)
  PPRO_FAKE_MISSING_LANGS=<a,b>   Speech-to-Text languages whose pack is not installed (default ko-KR)
  PPRO_FAKE_EXPORT_SECONDS=<s>    length of the silent WAV export_sequence writes (default: the sequence)
  PPRO_FAKE_SOURCE_FLOOR=1        insert_clip floors a source in/out point to the item's frame, as Premiere does
                                  (see source_frame); off by default, so source times stay as sent
In-process hooks: STATE (the project; clips may be bare dicts, components and transitions default lazily; an audio
clip with "channels": 2 gets the stereo volume component, its name verified on 27.1),
STUCK (set a dict {action, id, since} to make the panel stuck), FAIL_TRANSACTIONS (label substrings whose
transaction returns false), TRANSACTIONS (labels committed since reset()), LAST_CTX (the last request's guard and
side-effect count), reset().
"""

import asyncio
import copy
import json
import math
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(1, str(REPO))
from ppro_bridge import spec  # noqa: E402  (stdlib only)

BROKER_URL = os.environ.get("PPRO_BRIDGE_URL", "ws://127.0.0.1:8017")
VERSION = "0.5.0"
FPS = 60000 / 1001
TIMEBASE = "4237833600"  # ticks per frame at 59.94
TICKS_PER_SECOND = 254016000000
MAX_MEDIA_SECONDS = 32400  # 9 h: a keyframe past this is a unit mistake, not a time
EPS = 1e-3
DELAY = float(os.environ.get("PPRO_FAKE_DELAY", "0")) / 1000
CONCURRENT = os.environ.get("PPRO_FAKE_CONCURRENT") == "1"
STATS = {"received": 0, "inFlight": 0, "maxConcurrent": 0}
LOADED_AT = int(time.time() * 1000)
HOST = {"name": "premierepro", "version": "27.1.0", "uiLocale": "en_US"}
DEFAULT_PROJECT_NAME = "Smoke Test.prproj"


class Coded(ValueError):
    """A panel error with a code from actions.json. An explicit `outcome` is reported as is; without one
    (None) dispatch applies the outcome rule: not_committed while the request committed nothing, else unknown."""

    def __init__(self, code, message, outcome=None, data=None):
        super().__init__(message)
        self.code = code
        self.outcome = outcome
        self.data = data


def solid_png(w: int, h: int, rgb=(40, 70, 120)) -> bytes:
    """A valid solid-colour PNG (no Pillow needed) for export_frame to write."""
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


SPEAKER_ID = "5f0c1d7e-2a4b-4c9d-8e1f-3a2b1c0d9e8f"  # UUID v4, as Premiere writes them
FAKE_TRANSCRIPT = {
    "language": "en-us",
    "speakers": [{"id": SPEAKER_ID, "name": "Speaker 1"}],
    "segments": [
        {"start": 0.5, "duration": 4.0, "language": "en-us", "speaker": SPEAKER_ID, "words": [
            {"text": "The", "start": 0.5, "duration": 0.2, "eos": False, "confidence": 0.98, "tags": [], "type": "word"},
            {"text": "new", "start": 0.7, "duration": 0.5, "eos": False, "confidence": 0.97, "tags": [], "type": "word"},
            {"text": "bridge", "start": 1.2, "duration": 0.2, "eos": False, "confidence": 0.99, "tags": [], "type": "word"},
            {"text": "opened.", "start": 1.4, "duration": 0.5, "eos": True, "confidence": 0.96, "tags": [], "type": "word"},
            {"text": "Local", "start": 2.2, "duration": 0.3, "eos": False, "confidence": 0.92, "tags": [], "type": "word"},
            {"text": "traffic", "start": 2.5, "duration": 0.3, "eos": False, "confidence": 0.91, "tags": [], "type": "word"},
            {"text": "should", "start": 2.8, "duration": 0.5, "eos": False, "confidence": 0.88, "tags": [], "type": "word"},
            {"text": "improve.", "start": 3.3, "duration": 0.6, "eos": True, "confidence": 0.90, "tags": [], "type": "word"},
        ]},
    ],
}
# What some Premiere builds attach when an import "succeeds" without words (~170 bytes, language und-zz).
EMPTY_SHELL = {"language": "und-zz", "segments": [{"start": 0, "duration": 0, "language": "und-zz",
                                                   "speaker": SPEAKER_ID, "words": []}],
               "speakers": [{"id": SPEAKER_ID, "name": "Unknown"}]}

# Effects: (display name, [(param name, default value)]). Opacity and Motion are on every video clip, in this
# chain order (ExtendScript and UXP both list Opacity first); filters are what VideoFilterFactory can create.
INTRINSIC = {
    "AE.ADBE Opacity": ("Opacity", [("Opacity", 100.0), ("Blend Mode", 0)]),
    "AE.ADBE Motion": ("Motion", [("Position", {"x": 0.5, "y": 0.5}), ("Scale", 100.0), ("Scale Width", 100.0),
                                  ("Uniform Scale", True), ("Rotation", 0.0), ("Anchor Point", {"x": 0.5, "y": 0.5}),
                                  ("Anti-flicker Filter", 0.0)]),
}
FILTERS = {
    "AE.ADBE Gaussian Blur 2": ("Gaussian Blur", [("Blurriness", 0.0), ("Blur Dimensions", 1),
                                                  ("Repeat Edge Pixels", False)]),
    "AE.ADBE Crop": ("Crop", [("Left", 0.0), ("Top", 0.0), ("Right", 0.0), ("Bottom", 0.0), ("Zoom", False),
                              ("Edge Feather", 0.0)]),
    "AE.ADBE Tint": ("Tint", [("Map Black To", {"red": 0.0, "green": 0.0, "blue": 0.0, "alpha": 1.0}),
                              ("Map White To", {"red": 1.0, "green": 1.0, "blue": 1.0, "alpha": 1.0}),
                              ("Amount to Tint", 100.0)]),
    "AE.ADBE Black&White": ("Black & White", []),
}
# Audio clips' chain. MEASURED on one mono clip (Premiere 27.1 build 17): one component, "Internal Volume Mono",
# param 0 a boolean (bypass), param 1 Level in Premiere's linear unit (dB = 20 * log10(v) + 15). PROVISIONAL: the
# display names are guesses, and "Internal Volume Stereo" (a clip with "channels": 2) is UNVERIFIED. No Channel
# Volume / Panner on that mono clip. Adding audio effects is UNSUPPORTED (the panel refuses it too).
LEVEL_0DB = 0.17782793939113617  # Level at 0 dB as Premiere reports it (float32 of 10 ** (-15 / 20))
AUDIO_INTRINSIC = {
    "Internal Volume Mono": ("Volume", [("Bypass", False), ("Level", LEVEL_0DB)]),
    "Internal Volume Stereo": ("Volume", [("Bypass", False), ("Level", LEVEL_0DB)]),  # live: names localised
}


def level_from_db(db: float) -> float:
    """Premiere's linear Level for a gain in dB (0 dB -> 0.1778, -6 dB -> 0.0891)."""
    return 10 ** ((db - 15) / 20)


def db_from_level(level: float) -> float:
    return 20 * math.log10(level) + 15


TRANSITION_NAMES = {
    "AE.ADBE Cross Dissolve New": "Cross Dissolve",
    "AE.ADBE Dip To Black": "Dip to Black",
    "AE.ADBE Dip To White": "Dip to White",
    "AE.ADBE Film Dissolve": "Film Dissolve",
    "AE.ADBE Additive Dissolve": "Additive Dissolve",
}
STT_LANGUAGES = ["en-US", "en-GB", "de-DE", "es-ES", "fr-FR", "it-IT", "ja-JP", "ko-KR", "pt-BR", "ru-RU",
                 "zh-CN", "zh-TW", "nl-NL", "da-DK", "sv-SE", "nb-NO", "pl-PL", "tr-TR", "hi-IN"]

STATE = {
    "project": {"guid": "fake-project-guid", "path": "/tmp/fake.prproj"},  # "name": overrides the env/default
    "sequence": {"name": "Sequence 01", "guid": "fake-sequence-guid"},
    "items": [  # project panel; the two title.mov are an EN and a CN render with one name
        {"name": "interview.mp4", "bin": "", "mediaPath": "/fake/interview.mp4", "hasTranscript": True, "duration": 60.0},
        {"name": "broll_city.mp4", "bin": "", "mediaPath": "/fake/broll_city.mp4", "hasTranscript": False, "duration": 12.0},
        {"name": "title.mov", "bin": "EN", "mediaPath": "/fake/en/title.mov", "hasTranscript": False, "duration": 5.0},
        {"name": "title.mov", "bin": "CN", "mediaPath": "/fake/cn/title.mov", "hasTranscript": False, "duration": 5.0},
    ],
    # timeline: the interview runs the full length on V1/A1, like a talking-head A-roll
    "video": [[{"name": "interview.mp4", "start": 0.0, "end": 60.0, "inPoint": 0.0, "outPoint": 60.0, "scale": 100.0}],
              [], [], []],
    "audio": [[{"name": "interview.mp4", "start": 0.0, "end": 60.0, "inPoint": 0.0, "outPoint": 60.0}], [], [], []],
    "markers": [],
    "transitions": [],  # [{videoTrack, matchName, name, start, end, cut}] — sequence seconds
    "jobs": [],         # Speech-to-Text jobs (lost on reset, like on a panel reload)
}
_INITIAL = copy.deepcopy(STATE)

STUCK = None               # {action, id, since}: refuse everything but ping, like a panel whose watchdog fired
FAIL_TRANSACTIONS = set()  # label substrings: such a transaction "returns false" (TRANSACTION_FAILED)
TRANSACTIONS = []          # labels of the transactions committed since reset()
PROBED_AT = None


def _fresh_ctx():
    return {"action": None, "id": None, "guard": {}, "sideEffects": 0}


CTX = _fresh_ctx()       # the request being handled
LAST_CTX = _fresh_ctx()  # the one handled before (tests read sideEffects / guard from it)


def now_ms() -> int:
    return int(time.time() * 1000)


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def js_num(x) -> str:
    """A number as a JavaScript template string prints it (10.0 -> "10", 2.5 -> "2.5")."""
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    return str(x)


def snap(t):
    return round(t * FPS) / FPS


def ticks(seconds) -> int:
    return round(seconds * TICKS_PER_SECOND)


# ---------------------------------------------------------------- context: side effects, transactions, guard

def side_effect():
    """Something in Premiere is about to change (the panel's ctx.sideEffect())."""
    CTX["sideEffects"] += 1


def transact(label):
    """The panel's runTransaction: the side effect counts before executeTransaction, which may return false."""
    side_effect()
    if any(s and s in label for s in FAIL_TRANSACTIONS):
        raise Coded("TRANSACTION_FAILED", f'Transaction "{label}" returned false.')
    TRANSACTIONS.append(label)


def project_name() -> str:
    proj = STATE.get("project") or {}
    return proj.get("name") or os.environ.get("PPRO_FAKE_PROJECT_NAME") or DEFAULT_PROJECT_NAME


def _guid(v) -> str:
    return "null" if v is None else str(v)  # String(guid) in the panel


def _guard() -> dict:
    return {k: v for k, v in (CTX.get("guard") or {}).items() if v is not None}


GUARD_KEYS = ("project", "projectPrefix", "projectGuid", "sequence", "sequenceGuid")


def _check_sequence_guard(seq):
    g = _guard()
    if "sequence" not in g and "sequenceGuid" not in g:
        return
    if seq is None:
        raise Coded("SEQUENCE_MISMATCH", "The project has no active sequence; the request is pinned to one.")
    if "sequence" in g and g["sequence"] != seq["name"]:
        raise Coded("SEQUENCE_MISMATCH", f'The active sequence is "{seq["name"]}", not "{g["sequence"]}".')
    if "sequenceGuid" in g and g["sequenceGuid"] != _guid(seq.get("guid")):
        raise Coded("SEQUENCE_MISMATCH", f'The active sequence "{seq["name"]}" is not the one this request was '
                                         "pinned to (guid differs).")


def require_project():
    """The active project, checked against the request's guard (its sequence keys too, when present)."""
    proj = _active_project()
    _check_sequence_guard(STATE.get("sequence"))
    return proj


def require_sequence():
    """(project, active sequence), both checked against the request's guard."""
    proj = _active_project()
    seq = STATE.get("sequence")
    if seq is None:
        raise Coded("NO_SEQUENCE", "Project has no active sequence.")
    _check_sequence_guard(seq)
    return proj, seq


def _active_project():
    proj = STATE.get("project")
    if proj is None:
        raise Coded("NO_PROJECT", "No active project open in Premiere.")
    g, name = _guard(), project_name()
    if "project" in g and g["project"] != name:
        raise Coded("PROJECT_MISMATCH", f'The active project is "{name}", not "{g["project"]}".')
    if "projectPrefix" in g and not name.startswith(g["projectPrefix"]):
        raise Coded("PROJECT_MISMATCH", f'The active project "{name}" does not start with "{g["projectPrefix"]}".')
    if "projectGuid" in g and g["projectGuid"] != _guid(proj.get("guid")):
        raise Coded("PROJECT_MISMATCH", f'The active project "{name}" is not the one this request was pinned to '
                                        "(guid differs).")
    return proj


def require_text(p, names):
    """Required strings must not be empty either (the spec only types them)."""
    empty = [n for n in names if p.get(n) == ""]
    if empty:
        raise Coded("BAD_PARAMS", "; ".join(f"params.{n}: must not be empty" for n in empty))


def check_guard_only():
    """For actions that need no project (list_transition_names, list_transcripts jobsOnly): a guard, when one
    was sent, must still match."""
    if _guard():
        require_project()


# ---------------------------------------------------------------- project items

def item_ref(it) -> str:
    return f"{it['bin']}/{it['name']}" if it["bin"] else it["name"]


def match_items(name, bin_path=None):
    return [i for i in STATE["items"] if i["name"] == name and (not isinstance(bin_path, str) or i["bin"] == bin_path)]


def resolve(name, bin_path=None, unique=False):
    """The panel's resolveClip: same name + different media is ambiguous (with `unique`, any duplicate is)."""
    hits = match_items(name, bin_path)
    if not hits:
        where = f' in bin "{bin_path}"' if isinstance(bin_path, str) else ""
        raise Coded("NOT_FOUND", f'No clip named "{name}"{where}. Clips: '
                    + " | ".join(item_ref(i) for i in STATE["items"]))
    if len(hits) > 1 and (unique or len({i["mediaPath"] for i in hits}) > 1):
        raise Coded("AMBIGUOUS", f'{len(hits)} project items are named "{name}" with different media ('
                    + ", ".join(item_ref(i) for i in hits) + "). Name one with its bin path.")
    return hits[0]


def word_count(doc) -> int:
    """Words in an Adobe transcript: tokens typed "word" or untyped (punctuation tokens don't count)."""
    if not isinstance(doc, dict):
        return 0
    n = 0
    for seg in doc.get("segments") or []:
        if isinstance(seg, dict):
            n += sum(1 for w in seg.get("words") or [] if isinstance(w, dict) and w.get("type", "word") == "word")
    return n


def transcript_of(item):
    """The item's transcript doc (None without one); hasTranscript items without a doc get FAKE_TRANSCRIPT."""
    if item.get("transcript") is not None:
        return item["transcript"]
    if item.get("hasTranscript"):
        item["transcript"] = copy.deepcopy(FAKE_TRANSCRIPT)
        return item["transcript"]
    return None


def set_transcript(item, doc):
    item["transcript"] = doc
    item["hasTranscript"] = True


def running_job(item):
    return next((j for j in STATE.setdefault("jobs", []) if j["_item"] is item and j["state"] == "running"), None)


UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
LANG_CODE = re.compile(r"^(\?\?-\?\?|[a-z]{2,3}(-[a-z0-9]{2,4})?)$")


def transcript_problems(doc) -> list[str]:
    """The fake's own check of the Adobe transcript shape — what Premiere would import words from."""
    if not isinstance(doc, dict):
        return ["not a JSON object"]
    probs = [f"unknown key {k}" for k in doc if k not in ("language", "segments", "speakers")]
    if not isinstance(doc.get("language"), str) or not LANG_CODE.match(doc["language"]):
        probs.append("language: not a lowercase Adobe code")
    speakers = doc.get("speakers")
    if not isinstance(speakers, list):
        probs.append("speakers: expected a list")
        speakers = []
    ids = set()
    for i, s in enumerate(speakers):
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not UUID4.match(s["id"]):
            probs.append(f"speakers[{i}].id: expected a UUID v4")
        else:
            ids.add(s["id"])
    segments = doc.get("segments")
    if not isinstance(segments, list) or not segments:
        probs.append("segments: needs at least one segment")
        segments = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            probs.append(f"segments[{i}]: expected an object")
            continue
        for key in ("start", "duration"):
            if not is_num(seg.get(key)) or seg[key] < 0:
                probs.append(f"segments[{i}].{key}: expected a number >= 0")
        if not isinstance(seg.get("language"), str):
            probs.append(f"segments[{i}].language: missing")
        if seg.get("speaker") not in ids:
            probs.append(f"segments[{i}].speaker: not in speakers")
        words = seg.get("words")
        if not isinstance(words, list) or not words:
            probs.append(f"segments[{i}].words: needs at least one word")
            continue
        for k, w in enumerate(words):
            where = f"segments[{i}].words[{k}]"
            if not isinstance(w, dict):
                probs.append(f"{where}: expected an object")
                continue
            if not isinstance(w.get("text"), str):
                probs.append(f"{where}.text: missing")
            if not is_num(w.get("start")) or not is_num(w.get("duration")):
                probs.append(f"{where}: start/duration must be numbers")
            if not isinstance(w.get("eos"), bool):
                probs.append(f"{where}.eos: expected a boolean")
            if not is_num(w.get("confidence")) or not 0 <= w["confidence"] <= 1:
                probs.append(f"{where}.confidence: expected 0..1")
            if "tags" in w and (not isinstance(w["tags"], list) or set(w["tags"]) - {"profanity", "filler"}):
                probs.append(f"{where}.tags: only profanity, filler")
            if "type" in w and w["type"] not in ("word", "punctuation"):
                probs.append(f"{where}.type: word or punctuation")
    return probs


# ---------------------------------------------------------------- timeline

def label_of(kind, idx) -> str:
    return ("V" if kind == "video" else "A") + str(idx + 1)


def track(kind, idx):
    tracks = STATE[kind]
    if not isinstance(idx, int) or idx < 0 or idx >= len(tracks):
        raise Coded("NOT_FOUND", f"{label_of(kind, idx if isinstance(idx, int) else -1)} does not exist "
                                 f"(sequence has {len(tracks)} {kind} tracks).")
    return tracks[idx]


def describe(it):
    media = it.get("mediaPath") or next((p["mediaPath"] for p in STATE["items"] if p["name"] == it["name"]), None)
    return {"name": it["name"], "start": it["start"], "end": it["end"], "inPoint": it["inPoint"],
            "outPoint": it["outPoint"], "speed": it.get("speed", 1), "disabled": bool(it.get("disabled", False)),
            "projectItem": it["name"], "mediaPath": media}


def overlaps(it, frm, to):
    """[start, end) overlaps [frm, to]; frm == to means "the clip under that instant"."""
    return it["start"] <= to + EPS and it["end"] > frm + EPS


def item_at(kind, idx, at):
    """The single clip on a video or audio track under `at` (the panel's timeline.itemAt)."""
    hits = [it for it in track(kind, idx) if overlaps(it, at, at)]
    if not hits:
        raise Coded("NOT_FOUND", f"No clip on {label_of(kind, idx)} at {js_num(at)}s.")
    if len(hits) > 1:
        raise Coded("AMBIGUOUS", f"Expected one clip on {label_of(kind, idx)} at {js_num(at)}s, found {len(hits)}.")
    return hits[0]


def clip_at(video_track, at):
    return item_at("video", video_track, at)


def transitions():
    out = STATE.setdefault("transitions", [])
    for t in out:  # tests may append bare dicts
        t.setdefault("videoTrack", 0)
        t.setdefault("name", TRANSITION_NAMES.get(t.get("matchName"), t.get("matchName")))
        t.setdefault("cut", t["start"])
    return out


def drop_transitions(video_track, start, end):
    """Transitions on a video track that overlap [start, end] go with the clips there."""
    STATE["transitions"] = [t for t in transitions() if not (
        t["videoTrack"] == video_track and t["start"] < end - EPS and t["end"] > start + EPS)]


def shift_transitions(video_track, at, by):
    for t in transitions():
        if t["videoTrack"] == video_track and t["start"] >= at - EPS:
            t["start"], t["end"], t["cut"] = t["start"] + by, t["end"] + by, t["cut"] + by


def overwrite(kind, idx, start, end, item):
    """Overwrite edit: trim/split whatever is under [start, end), then add the item."""
    out = []
    for it in track(kind, idx):
        if it["end"] <= start + EPS or it["start"] >= end - EPS:
            out.append(it)
            continue
        if it["start"] < start - EPS:
            left = copy.deepcopy(it)
            left.update(end=start, outPoint=it["inPoint"] + (start - it["start"]))
            out.append(left)
        if it["end"] > end + EPS:
            right = copy.deepcopy(it)
            right.update(start=end, inPoint=it["inPoint"] + (end - it["start"]))
            out.append(right)
    out.append(item)
    STATE[kind][idx] = sorted(out, key=lambda x: x["start"])
    if kind == "video":
        drop_transitions(idx, start, end)


def insert_edit(kind, idx, at, length, item):
    """Insert edit: split whatever is under `at`, push everything from there right by `length`."""
    out = []
    for it in track(kind, idx):
        if it["end"] <= at + EPS:
            out.append(it)
        elif it["start"] >= at - EPS:
            moved = copy.deepcopy(it)
            moved.update(start=it["start"] + length, end=it["end"] + length)
            out.append(moved)
        else:
            left, right = copy.deepcopy(it), copy.deepcopy(it)
            left.update(end=at, outPoint=it["inPoint"] + (at - it["start"]))
            right.update(start=at + length, end=it["end"] + length, inPoint=it["inPoint"] + (at - it["start"]))
            out += [left, right]
    out.append(item)
    STATE[kind][idx] = sorted(out, key=lambda x: x["start"])
    if kind == "video":
        STATE["transitions"] = [t for t in transitions()  # a transition across the split point goes
                                if not (t["videoTrack"] == idx and t["start"] < at - EPS and t["end"] > at + EPS)]
        shift_transitions(idx, at, length)


# ---------------------------------------------------------------- effects

def make_component(match_name):
    display, params = INTRINSIC.get(match_name) or AUDIO_INTRINSIC.get(match_name) or FILTERS[match_name]
    return {"matchName": match_name, "displayName": display,
            "params": [{"name": n, "value": copy.deepcopy(v), "keyframed": False, "keyframes": []} for n, v in params]}


def chain(clip, kind="video"):
    """A clip's component chain, created on first use: video (Opacity, Motion, then added filters) or audio (the
    provisional volume component; see AUDIO_INTRINSIC)."""
    if "components" not in clip:
        if kind == "audio":
            stereo = clip.get("channels", 1) == 2
            clip["components"] = [make_component("Internal Volume Stereo" if stereo else "Internal Volume Mono")]
        else:
            comps = [make_component("AE.ADBE Opacity"), make_component("AE.ADBE Motion")]
            comps[1]["params"][1]["value"] = float(clip.get("scale", 100.0))
            clip["components"] = comps
    return clip["components"]


def start_value(param):
    """getStartValue(): a keyframed param reports its first keyframe's value."""
    if param.get("keyframed") and param.get("keyframes"):
        return param["keyframes"][0]["value"]
    return param.get("value")


def value_at(param, t):
    """getValueAtTime(media seconds): linear between numeric keyframes, held otherwise."""
    kfs = param.get("keyframes") or []
    if not param.get("keyframed") or not kfs:
        return param.get("value")
    if t <= kfs[0]["t"]:
        return kfs[0]["value"]
    for a, b in zip(kfs, kfs[1:]):
        if t < b["t"]:
            if is_num(a["value"]) and is_num(b["value"]) and b["t"] > a["t"]:
                return a["value"] + (b["value"] - a["value"]) * (t - a["t"]) / (b["t"] - a["t"])
            return a["value"]
    return kfs[-1]["value"]


def motion_scale(clip):
    for comp in chain(clip):
        if comp["matchName"] == "AE.ADBE Motion":
            params = comp["params"]
            return next((p for p in params if p["name"] == "Scale"), params[1] if len(params) > 1 else None)
    return None


def inspect_chain(clip, max_kf, kind="video"):
    out, seen = [], {}
    for i, comp in enumerate(chain(clip, kind)):
        seen[comp["matchName"]] = seen.get(comp["matchName"], 0) + 1
        params = []
        for k, prm in enumerate(comp["params"]):
            pr = {"index": k, "name": prm["name"], "value": copy.deepcopy(start_value(prm)),
                  "keyframed": bool(prm.get("keyframed"))}
            if pr["keyframed"]:
                kfs = prm.get("keyframes") or []
                pr["timeBase"] = "media"
                pr["keyframes"] = [{"t": kf["t"], "value": copy.deepcopy(kf["value"])} for kf in kfs[:max_kf]]
                if len(kfs) > max_kf:
                    pr["keyframesTruncated"] = len(kfs)
            params.append(pr)
        out.append({"index": i, "matchName": comp["matchName"], "displayName": comp["displayName"],
                    "occurrence": seen[comp["matchName"]], "params": params})
    return out


def _is_point(v):
    return isinstance(v, dict) and is_num(v.get("x")) and is_num(v.get("y"))


def _is_color(v):
    return isinstance(v, dict) and all(is_num(v.get(c)) for c in ("red", "green", "blue"))


def to_param_value(v, where):
    """The panel's toParamValue: {value: v} unwrapped, [x, y] / {x, y} become a point; numbers, strings, booleans
    and colours pass; anything else is refused."""
    if isinstance(v, dict) and not _is_point(v) and not is_num(v.get("red")) and "value" in v:
        return to_param_value(v["value"], where)
    if isinstance(v, list) and len(v) == 2 and all(is_num(n) for n in v):
        return {"x": v[0], "y": v[1]}
    if _is_point(v):
        return {"x": v["x"], "y": v["y"]}
    if isinstance(v, dict) and is_num(v.get("red")):
        return copy.deepcopy(v)
    if isinstance(v, (str, bool)) or is_num(v):
        return v
    raise Coded("BAD_PARAMS", f"{where}: unsupported value {json.dumps(v)} (number, string, boolean, [x, y], "
                              "{x, y} or a colour)")


def value_problem(current, new):
    """The fake is stricter than the panel here: Premiere itself rejects a value of the wrong kind."""
    if isinstance(current, bool):
        ok, want = isinstance(new, bool), "a boolean"
    elif is_num(current):
        ok, want = is_num(new), "a number"
    elif _is_point(current):
        ok, want = _is_point(new), "a point [x, y] or {x, y}"
    elif _is_color(current):
        ok, want = _is_color(new), "a colour {red, green, blue, alpha}"
    else:
        ok, want = True, ""
    return None if ok else f"expected {want}, got {json.dumps(new)}"


MAX_INPUT_SECONDS = 35000  # no time input may exceed ~9.7 h before the conversion
MAX_CREATED = 16           # effect instances one set_params request may append (guards a typo in occurrence)


def media_seconds(clip, frame_seconds, time_base, t):
    """A keyframe time in `time_base` -> media seconds, the way the panel converts it (integer ticks).
    media: t; clip: inPoint + t; sequence: inPoint + (t - start). Allowed: [inPoint - frame/2, outPoint + frame/2];
    clip/sequence need speed 1; past 9 h of media time is refused."""
    speed = clip.get("speed", 1)
    if time_base in ("clip", "sequence") and is_num(speed) and abs(speed - 1) > 1e-6:
        raise Coded("UNSUPPORTED", f"timeBase {time_base} needs a clip at speed 1 (this one plays at "
                                   f"{js_num(speed)}x); send media times instead.")
    for v in (t, clip["inPoint"], clip["start"] if time_base == "sequence" else 0):
        if abs(v) > MAX_INPUT_SECONDS:
            raise Coded("BAD_PARAMS", f"time {js_num(v)}s is beyond the 9 h limit")
    in_t, out_t = ticks(clip["inPoint"]), ticks(clip["outPoint"])
    if time_base == "media":
        m = ticks(t)
    elif time_base == "clip":
        m = in_t + ticks(t)
    elif time_base == "sequence":
        m = in_t + ticks(t) - ticks(clip["start"])
    else:
        raise Coded("BAD_PARAMS", f"timeBase must be media, clip or sequence, not {time_base!r}.")
    if m > MAX_MEDIA_SECONDS * TICKS_PER_SECOND:
        raise Coded("BAD_PARAMS", f"Keyframe at {time_base} time {js_num(t)}s is {m / TICKS_PER_SECOND:.3f}s of "
                                  "media time — past 9 h; are these seconds?")
    half = ticks(frame_seconds) / 2
    if m < in_t - half or m > out_t + half:
        raise Coded("KEYFRAME_OUT_OF_RANGE",
                    f"Keyframe at {time_base} time {js_num(t)}s is media time {m / TICKS_PER_SECOND:.4f}s, outside the "
                    f"clip's {clip['inPoint']:.4f}-{clip['outPoint']:.4f}s (Premiere would clamp it onto the edge).")
    return m / TICKS_PER_SECOND


# ---------------------------------------------------------------- handlers (the whitelist)

def stuck_text():
    return f'Stuck on "{STUCK.get("action")}" — save your project, then Reload this panel in UXP Developer Tool'


def stuck_view():
    if not STUCK:
        return False
    out = {"seconds": round((now_ms() - (STUCK.get("since") or now_ms())) / 1000)}
    out.update(STUCK)
    return out


def denied_view():
    reason = os.environ.get("PPRO_FAKE_DENIED")
    return {"match": "27.1.*", "reason": reason} if reason else False


def capabilities():
    caps = {name: True for name in spec.load()["capabilities"]}
    raw = os.environ.get("PPRO_FAKE_CAPS")
    if raw:
        try:
            extra = json.loads(raw)
        except ValueError:
            extra = None
        if isinstance(extra, dict):
            caps.update(extra)
    return caps


def h_ping(p):
    global PROBED_AT
    if p.get("probe") and not STUCK:
        PROBED_AT = now_ms()
    out = {"pong": True, "host": f"{HOST['name']} {HOST['version']}", "hostInfo": dict(HOST),
           "plugin": f"fake-panel {VERSION}", "version": VERSION, "protocol": spec.protocol(), "modules": "fake",
           "actions": list(HANDLERS), "stuck": stuck_view(), "denied": denied_view(),
           "capabilities": capabilities(), "loadedAt": LOADED_AT}
    if PROBED_AT:
        out["capabilitiesProbedAt"] = PROBED_AT
    out.update(received=STATS["received"], maxConcurrent=STATS["maxConcurrent"])
    return out


def h_get_project(p):
    proj = require_project()
    seq = STATE.get("sequence")
    return {"name": project_name(), "path": proj.get("path"), "activeSequence": seq["name"] if seq else None,
            "guid": proj.get("guid")}


def _tracks(kind):
    return [{"index": i, "label": label_of(kind, i), "name": f"{'Video' if kind == 'video' else 'Audio'} {i + 1}",
             "clipCount": len(t)} for i, t in enumerate(STATE[kind])]


def h_get_sequence(p):
    _, seq = require_sequence()
    end = max([it["end"] for t in STATE["video"] + STATE["audio"] for it in t] or [0])
    return {"name": seq["name"], "playerPositionSeconds": 4.2, "width": 1080, "height": 1920,
            "timebase": TIMEBASE, "fps": round(FPS, 3), "durationSeconds": end,
            "videoTracks": _tracks("video"), "audioTracks": _tracks("audio"), "captionTrackCount": 1,
            "guid": seq.get("guid")}


def h_list_items(p):
    _, seq = require_sequence()
    kinds = p.get("kinds") if isinstance(p.get("kinds"), list) and p.get("kinds") else ["video", "audio"]
    out = {"sequence": seq["name"]}
    for kind in ("video", "audio"):
        if kind not in kinds:
            continue
        out[kind] = [{"index": i, "label": label_of(kind, i),
                      "name": f"{'Video' if kind == 'video' else 'Audio'} {i + 1}",
                      "items": [describe(it) for it in sorted(t, key=lambda x: x["start"])]}
                     for i, t in enumerate(STATE[kind])]
    return out


def h_inspect_items(p):
    """Video tracks (videoTracks, default all) and, on request, audio tracks (audioTracks; given alone, audio only)."""
    _, seq = require_sequence()
    vts = p.get("videoTracks") or []
    ats = p.get("audioTracks")
    jobs = [("video", i) for i in (vts or range(len(STATE["video"])))]
    if isinstance(ats, list):
        jobs = ([] if ats and not vts else jobs) + [("audio", i) for i in ats]
    frm = p["fromSeconds"] if is_num(p.get("fromSeconds")) else -math.inf
    to = p["toSeconds"] if is_num(p.get("toSeconds")) else math.inf
    max_kf = p.get("maxKeyframes") or 50
    out = {"sequence": seq["name"], "tracks": []}
    for kind, idx in jobs:
        clips = []
        for it in sorted(track(kind, idx), key=lambda x: x["start"]):
            d = describe(it)
            if not (d["start"] <= to + EPS and d["end"] >= frm - EPS):
                continue
            d["components"] = inspect_chain(it, max_kf, kind)
            clips.append(d)
        trans = [{"name": t["name"], "matchName": t["matchName"], "start": t["start"], "end": t["end"],
                  "duration": t["end"] - t["start"]}
                 for t in sorted(transitions(), key=lambda t: t["start"])
                 if kind == "video" and t["videoTrack"] == idx and t["start"] <= to + EPS and t["end"] >= frm - EPS]
        out["tracks"].append({"index": idx, "kind": kind, "label": label_of(kind, idx),
                              "name": f"{'Video' if kind == 'video' else 'Audio'} {idx + 1}", "clips": clips,
                              "transitions": trans})
    return out


def h_list_project_items(p):
    require_project()
    return {"items": [{"name": i["name"], "bin": i["bin"], "mediaPath": i["mediaPath"]} for i in STATE["items"]]}


def h_export_frame(p):
    require_text(p, ["dir"])
    require_sequence()
    name = p.get("name") or "frame.png"
    width, height = p.get("width") or 1080, p.get("height") or 1920
    folder = Path(p["dir"])
    if not p["dir"] or not folder.is_dir():
        raise Coded("EXPORT_FAILED", f"exportSequenceFrame returned false ({p['dir']!r} is not a folder).")
    (folder / name).write_bytes(solid_png(108, 192))
    return {"exported": True, "dir": p["dir"], "candidates": [name, name + ".png"], "width": width, "height": height,
            "atSeconds": p["atSeconds"] if is_num(p.get("atSeconds")) else "playerPosition"}


def _rt(seconds):
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": FPS, "value": seconds * FPS}


def _range(start, duration):
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start), "duration": _rt(duration)}


def otio_doc() -> dict:
    """The active sequence as minimal OTIO JSON: gaps, clips (source range, media path) and transitions
    (in/out offsets around the cut, PremierePro_OTIO.MatchName) — what replace.otio_transitions reads."""
    stack = []
    for kind in ("video", "audio"):
        for idx, items in enumerate(STATE[kind]):
            trans = sorted((t for t in transitions() if kind == "video" and t["videoTrack"] == idx),
                           key=lambda t: t["cut"])
            children, pos = [], 0.0

            def flush(upto, children=children, trans=trans):
                while trans and trans[0]["cut"] <= upto + EPS:
                    t = trans.pop(0)
                    children.append({"OTIO_SCHEMA": "Transition.1", "name": t["name"],
                                     "transition_type": "SMPTE_Dissolve", "in_offset": _rt(t["cut"] - t["start"]),
                                     "out_offset": _rt(t["end"] - t["cut"]),
                                     "metadata": {"PremierePro_OTIO": {"MatchName": t["matchName"]}}})

            for it in sorted(items, key=lambda x: x["start"]):
                if it["start"] > pos + EPS:
                    flush(pos)
                    children.append({"OTIO_SCHEMA": "Gap.1", "name": "",
                                     "source_range": _range(0.0, it["start"] - pos)})
                    pos = it["start"]
                flush(pos)
                media = describe(it)["mediaPath"]
                children.append({"OTIO_SCHEMA": "Clip.2", "name": it["name"],
                                 "source_range": _range(it["inPoint"], it["end"] - it["start"]),
                                 "media_reference": {"OTIO_SCHEMA": "ExternalReference.1",
                                                     "target_url": f"file://{media}" if media else None},
                                 "metadata": {}})
                pos = it["end"]
            flush(math.inf)
            stack.append({"OTIO_SCHEMA": "Track.1", "name": f"{'Video' if kind == 'video' else 'Audio'} {idx + 1}",
                          "kind": "Video" if kind == "video" else "Audio", "children": children, "metadata": {}})
    return {"OTIO_SCHEMA": "Timeline.1", "name": STATE["sequence"]["name"], "global_start_time": _rt(0.0),
            "metadata": {}, "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": stack}}


def h_export_otio(p):
    require_text(p, ["outputPath"])
    require_sequence()
    out = Path(p["outputPath"])
    if not p["outputPath"] or not out.parent.is_dir():
        raise Coded("EXPORT_FAILED", f"exportAsOpenTimelineIO returned false ({out.parent} is not a folder).")
    out.write_bytes(json.dumps(otio_doc(), indent=1).encode("utf-8"))
    return {"exported": True, "outputPath": p["outputPath"]}


def _stt_transcript(language):
    doc = copy.deepcopy(FAKE_TRANSCRIPT)
    code = (language or "en-US").lower()
    doc["language"] = code
    for seg in doc["segments"]:
        seg["language"] = code
    return doc


def _advance_jobs():
    """One poll: running jobs count it; after PPRO_FAKE_STT_POLLS polls they end."""
    need = int(os.environ.get("PPRO_FAKE_STT_POLLS", "1"))
    for job in STATE.setdefault("jobs", []):
        if job["state"] != "running":
            continue
        job["_polls"] += 1
        if job["_polls"] < need:
            continue
        job["finishedAt"] = now_ms()
        if job["_result"] == "fail":
            job.update(state="failed", error="Speech-to-Text failed (fake panel: PPRO_FAKE_STT_RESULT=fail).")
        else:
            set_transcript(job["_item"], _stt_transcript(job["language"]))
            job["state"] = "done"


def _job_view(job):
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    out["hasTranscript"] = bool(job["_item"].get("hasTranscript"))
    return out


def h_list_transcripts(p):
    if p.get("jobsOnly"):
        check_guard_only()  # the job table needs no project walk
    else:
        require_project()
    _advance_jobs()
    out = {"jobs": [_job_view(j) for j in STATE["jobs"]]}
    if not p.get("jobsOnly"):
        out["clips"] = [{"name": i["name"], "bin": i["bin"], "hasTranscript": bool(i.get("hasTranscript"))}
                        for i in STATE["items"]]
    return out


def h_export_transcript(p):
    require_project()
    name, bin_path = p.get("itemName"), p.get("binPath")
    if isinstance(name, str):
        item = resolve(name, bin_path)
        doc = transcript_of(item)
        if doc is None:
            raise Coded("NO_TRANSCRIPT", f'Clip "{item_ref(item)}" has no transcript. Run Speech to Text in the Text '
                                         "panel (or transcribe_clip) first.")
        if word_count(doc) == 0:
            raise Coded("NO_TRANSCRIPT", f'Clip "{item_ref(item)}" has only an empty placeholder transcript.')
    else:
        found = [i for i in STATE["items"]
                 if (not isinstance(bin_path, str) or i["bin"] == bin_path) and word_count(transcript_of(i)) > 0]
        if not found:
            raise Coded("NO_TRANSCRIPT", "No clip in this project has a transcript. Run Speech to Text in the Text "
                                         "panel (or transcribe_clip) first.")
        item = found[0]
    return {"itemName": item["name"], "binPath": item["bin"],
            "transcriptJson": json.dumps(transcript_of(item), ensure_ascii=False)}


def h_import_transcript(p):
    """Static import first; if the clip still has no words, the callback form once (it exists in the fake).
    Premiere keeps the words of a well-formed doc; anything else (also a doc without words) ends up as an empty
    shell."""
    require_text(p, ["itemName", "transcriptJson"])
    require_project()
    item = resolve(p["itemName"], p.get("binPath"))
    try:
        doc = json.loads(p["transcriptJson"])
    except ValueError as e:
        raise Coded("BAD_PARAMS", f"transcriptJson is not valid JSON: {e}") from None
    if not isinstance(doc, dict):
        raise Coded("BAD_PARAMS", "transcriptJson must be a JSON object.")
    words_in = word_count(doc)
    had_words = word_count(transcript_of(item)) > 0
    if had_words and not p.get("replace"):
        raise Coded("TRANSCRIPT_EXISTS", f'Clip "{item_ref(item)}" already has a transcript; pass replace to '
                                         "overwrite it.")
    if running_job(item):
        raise Coded("TRANSCRIBE_RUNNING", f'Speech-to-Text is running on "{item_ref(item)}"; wait for it.')
    mode = os.environ.get("PPRO_FAKE_TRANSCRIPT_IMPORT") or "static"
    valid = not transcript_problems(doc)
    label = f"Import transcript onto {p['itemName']}"
    transact(label)
    set_transcript(item, copy.deepcopy(doc) if valid and mode == "static" else copy.deepcopy(EMPTY_SHELL))
    method = "static"
    if word_count(transcript_of(item)) == 0:
        method = "callback"
        if valid and mode == "callback":
            transact(label + " [callback]")
            set_transcript(item, copy.deepcopy(doc))
    words_out = word_count(transcript_of(item))
    out = {"imported": words_out > 0, "itemName": item["name"], "binPath": item["bin"], "method": method,
           "replaced": had_words, "wordsIn": words_in, "wordsOut": words_out}
    if words_out == 0:
        raise Coded("TRANSCRIPT_NOT_IMPORTED",
                    f'Premiere ran the import but "{item_ref(item)}" still has no words (a known Premiere bug on some '
                    "builds).", outcome="committed", data=out)
    if words_out != words_in:
        out["warning"] = f"Premiere kept {words_out} of {words_in} words."
    return out


def h_transcribe_clip(p):
    require_text(p, ["itemName"])
    require_project()
    language = p.get("language")
    if language is not None:
        canon = next((code for code in STT_LANGUAGES if code.lower() == language.lower()), None)
        if canon is None:
            raise Coded("BAD_PARAMS", f"Unknown Speech-to-Text language {language!r}. Supported: "
                                      + ", ".join(STT_LANGUAGES))
        missing = {c.strip().lower() for c in os.environ.get("PPRO_FAKE_MISSING_LANGS", "ko-KR").split(",")}
        if canon.lower() in missing:
            raise Coded("LANGUAGE_UNAVAILABLE", f"The Speech-to-Text language pack for {canon} is not installed "
                                                "(install it in Premiere's Text panel first).")
        language = canon
    item = resolve(p["itemName"], p.get("binPath"))
    if item.get("isSequence"):
        raise Coded("UNSUPPORTED", f'"{item_ref(item)}" is a sequence; Speech-to-Text runs on clips.')
    if word_count(transcript_of(item)) > 0:
        raise Coded("TRANSCRIPT_EXISTS", f'Clip "{item_ref(item)}" already has a transcript.')
    if running_job(item):
        raise Coded("TRANSCRIBE_RUNNING", f'Speech-to-Text is already running on "{item_ref(item)}".')
    result = os.environ.get("PPRO_FAKE_STT_RESULT") or "done"
    side_effect()
    if result == "reject":
        raise Coded("TRANSCRIBE_FAILED", "Speech-to-Text rejected the clip at once (fake panel: "
                                         "PPRO_FAKE_STT_RESULT=reject).", outcome="not_committed")
    jobs = STATE.setdefault("jobs", [])
    job = {"jobId": f"stt-{len(jobs) + 1}", "itemName": item["name"], "binPath": item["bin"], "language": language,
           "state": "running", "error": None, "startedAt": now_ms(), "finishedAt": None,
           "_item": item, "_polls": 0, "_result": result}
    jobs.append(job)
    return {"started": True, "jobId": job["jobId"], "itemName": item["name"], "binPath": item["bin"],
            "language": language}


def h_import_media(p):
    require_project()
    paths = p["paths"]
    if not paths:
        raise Coded("BAD_PARAMS", "import_media requires params.paths (non-empty array).")
    if any(not isinstance(x, str) or not x.strip() for x in paths):
        raise Coded("IMPORT_FAILED", "importFiles returned false (an empty path).")
    bin_name = p.get("bin") or ""
    if bin_name and not any(i["bin"] == bin_name for i in STATE["items"]):
        transact(f'Agent: create bin "{bin_name}"')
    side_effect()  # importFiles is not a transaction, but it changes the project
    new = []
    for path in paths:
        name = Path(path).name
        STATE["items"].append({"name": name, "bin": bin_name, "mediaPath": path, "hasTranscript": False,
                               "duration": float(os.environ.get("PPRO_FAKE_MEDIA_SECONDS", "10"))})
        new.append(name)
    return {"imported": True, "count": len(paths), "bin": p.get("bin") or None, "newItems": new}


SILENT_SUFFIXES = (".mov", ".png")  # pretend stills/graphics are silent


def source_frame(seconds, item):
    """Where Premiere puts a source in/out point: the start of the item's frame under it, after the panel's 1 us
    nudge (core.atTime) — 5 us under frame 273 lands on frame 272. The item's frames: its set_fps rate, else the
    sequence's."""
    rate = item.get("fps") or FPS
    return math.floor((seconds + 1e-6) * rate + 1e-9) / rate


def h_insert_clip(p):
    require_text(p, ["itemName"])
    if is_num(p.get("inSeconds")) != is_num(p.get("outSeconds")):
        raise Coded("BAD_PARAMS", "inSeconds and outSeconds come as a pair.")
    has_range = is_num(p.get("inSeconds")) and is_num(p.get("outSeconds"))
    if has_range and p["outSeconds"] <= p["inSeconds"]:
        raise Coded("BAD_PARAMS", "outSeconds must be greater than inSeconds.")
    require_sequence()
    item = resolve(p["itemName"], p.get("binPath"))
    at = p.get("atSeconds") or 0
    v, a = p.get("videoTrack") or 0, p.get("audioTrack") or 0
    mode = "insert" if p.get("mode") == "insert" else "overwrite"
    label = p.get("undoLabel") or f"Agent: place {p['itemName']} @{js_num(at)}s"
    track("video", v), track("audio", a)  # Premiere would add a track past the end; refuse before anything changes
    src_in = p["inSeconds"] if has_range else 0.0
    src_out = p["outSeconds"] if has_range else item["duration"]
    if has_range and os.environ.get("PPRO_FAKE_SOURCE_FLOOR") == "1":
        src_in, src_out = source_frame(src_in, item), source_frame(src_out, item)
    start, end = snap(at), snap(at + (src_out - src_in))
    clip = {"name": item["name"], "start": start, "end": end, "inPoint": src_in, "outPoint": src_out, "scale": 100.0,
            "mediaPath": item["mediaPath"]}

    def place():
        legs = [("video", v, copy.deepcopy(clip))]
        if not item["name"].endswith(SILENT_SUFFIXES):
            keys = ("name", "start", "end", "inPoint", "outPoint", "mediaPath")
            legs.append(("audio", a, {k: clip[k] for k in keys}))
        for kind, idx, leg in legs:
            if mode == "insert":
                insert_edit(kind, idx, start, end - start, leg)
            else:
                overwrite(kind, idx, start, end, leg)

    if not has_range:
        transact(label)
        place()
    else:
        transact(label + " [1/3 set range]")
        place_error = None
        try:
            transact(label + " [2/3 place]")
            place()
        except Coded as e:
            place_error = e
        try:
            transact(label + " [3/3 clear range]")
        except Coded as e:
            msg = f'[clear source in/out] {e} (the source range is still set on "{p["itemName"]}")'
            raise Coded("TRANSACTION_FAILED", f"[place on timeline] {place_error}; also {msg}" if place_error
                        else msg) from None
        if place_error:  # range set and cleared, nothing placed: the timeline is unchanged
            raise Coded(place_error.code, f"[place on timeline] {place_error} (committed: [1/3 set range], "
                                          "[3/3 clear range])", outcome="not_committed")
    return {"placed": True, "itemName": p["itemName"], "mode": mode, "atSeconds": at, "videoTrack": v,
            "audioTrack": a, "sourceRange": [p["inSeconds"], p["outSeconds"]] if has_range else "whole clip",
            "undoLabel": label}


def h_set_scale(p):
    dry = bool(p.get("dryRun"))
    if not dry and not is_num(p.get("value")):
        raise Coded("BAD_PARAMS", "set_scale requires params.value (percent)")
    require_sequence()
    vt, frm = p["videoTrack"], p["fromSeconds"]
    to = p["toSeconds"] if is_num(p.get("toSeconds")) else frm
    targets = []
    for it in track("video", vt):
        if not overlaps(it, frm, to):
            continue
        d = describe(it)
        scale = motion_scale(it)
        d.update(scaleParam=scale["name"] if scale else None,
                 scaleBefore=copy.deepcopy(value_at(scale, it["inPoint"])) if scale else None,
                 scaleKeyframed=bool(scale.get("keyframed")) if scale else None)
        targets.append((it, d, scale))
    if not targets:
        raise Coded("NOT_FOUND", f"No clip on V{vt + 1} overlaps {js_num(frm)}–{js_num(to)}s.")
    missing = [d["name"] for _, d, s in targets if not s]
    if missing:
        raise Coded("NOT_FOUND", f"Could not find Motion > Scale on: {', '.join(missing)}")
    keyed = [d["name"] for _, d, _ in targets if d["scaleKeyframed"]]
    if keyed:
        raise Coded("KEYFRAMED", f"Scale is keyframed on: {', '.join(keyed)} — not changing it.")
    if not dry:
        transact(p.get("undoLabel") or f"Agent: scale {js_num(p['value'])}% on V{vt + 1}")
        for it, _, scale in targets:
            scale["value"] = p["value"]
            it["scale"] = p["value"]
    return {"dryRun": dry, "value": p.get("value"), "clips": [d for _, d, _ in targets]}


def h_set_params(p):
    """Validate the whole request (clip, components, occurrences, param indexes, values, time bases, ranges,
    KEYFRAMED), then append missing effects in one transaction and write the params in another. Exactly one of
    videoTrack / audioTrack; on an audio clip only components it already has (its volume) can be written."""
    require_sequence()
    has_v, has_a = is_num(p.get("videoTrack")), is_num(p.get("audioTrack"))
    if has_v == has_a:
        raise Coded("BAD_PARAMS", "set_params needs exactly one of videoTrack or audioTrack.")
    kind = "audio" if has_a else "video"
    idx, at = (p["audioTrack"] if has_a else p["videoTrack"]), p["atSeconds"]
    clip = item_at(kind, idx, at)
    label = p.get("undoLabel") or f"Agent: set params on {label_of(kind, idx)} @{js_num(at)}s"
    comps = chain(clip, kind)
    frame = 1 / FPS
    reqs = [(c["matchName"], c.get("occurrence") or 1, c.get("params") or []) for c in p.get("components") or []]
    need = {}  # per match name (in order of first mention): the highest occurrence asked for
    for m, occ, _ in reqs:
        need[m] = max(need.get(m, 0), occ)
    created = [m for m, n in need.items() for _ in range(sum(c["matchName"] == m for c in comps), n)]
    if created and kind == "audio":
        raise Coded("UNSUPPORTED", f"Adding audio effects is not supported yet ({', '.join(created)} is not on the "
                                   "clip); write params of components the audio clip already has (e.g. its Volume).")
    if len(created) > MAX_CREATED:
        raise Coded("BAD_PARAMS", f"The request would add {len(created)} effect instances (limit {MAX_CREATED}); "
                                  "check occurrence.")
    for m in dict.fromkeys(created):
        if m in INTRINSIC:
            raise Coded("NOT_FOUND", f"Cannot add effect {m}: it is built into every clip, once.")
        if m not in FILTERS:
            raise Coded("NOT_FOUND", f"Cannot add effect {m} (unknown match name, or not an addable filter).")
    new_comps = [make_component(m) for m in created]
    planned = comps + new_comps  # the chain after this request's appends
    jobs, seen = [], set()
    for m, occ, prms in reqs:
        comp = [c for c in planned if c["matchName"] == m][occ - 1]
        for prm in prms:
            idx = prm["index"]
            key = f"{m}[{occ}]#{idx}"
            if key in seen:
                raise Coded("BAD_PARAMS", f"{key} is listed twice.")
            seen.add(key)
            if idx >= len(comp["params"]):
                raise Coded("NOT_FOUND", f"{m} has no param #{idx} (it has {len(comp['params'])}).")
            cur = comp["params"][idx]
            where = f"{key} {cur['name']}"
            kfs = prm.get("keyframes") or []
            if kfs:
                tb = prm.get("timeBase")
                if not tb:
                    raise Coded("BAD_PARAMS", f"{where}: keyframes need a timeBase (media, clip or sequence).")
                if cur.get("keyframed"):
                    raise Coded("KEYFRAMED", f"{where} is already keyframed; not merging new keyframes into it.")
                conv = []
                for i, kf in enumerate(kfs):
                    secs = media_seconds(clip, frame, tb, kf["t"])
                    value = to_param_value(kf["value"], f"{where} keyframe {i}")
                    problem = value_problem(cur["value"], value)
                    if problem:
                        raise Coded("BAD_PARAMS", f"{where} keyframe {i}: {problem}")
                    conv.append((secs, value))
                jobs.append({"param": cur, "where": where, "keys": conv, "matchName": m, "occurrence": occ,
                             "index": idx, "timeBase": tb})
            else:
                if prm.get("value") is None:
                    raise Coded("BAD_PARAMS", f"{where}: needs a value or keyframes.")
                if cur.get("keyframed"):
                    raise Coded("KEYFRAMED", f"{where} is keyframed; a static value would be hidden by the keyframes.")
                value = to_param_value(prm["value"], where)
                problem = value_problem(cur["value"], value)
                if problem:
                    raise Coded("BAD_PARAMS", f"{where}: {problem}")
                jobs.append({"param": cur, "where": where, "value": value})

    keyframes = [{"matchName": j["matchName"], "occurrence": j["occurrence"], "index": j["index"],
                  "timeBase": j["timeBase"], "mediaSeconds": [secs for secs, _ in j["keys"]]}
                 for j in jobs if "keys" in j]
    if p.get("dryRun"):  # validated like a real run; the effects to create were never appended
        return {"dryRun": True, "set": [j["where"] for j in jobs], "createdEffects": created, "keyframes": keyframes,
                "undoLabel": label}
    if created:
        transact(label + " [add effects]")
        comps.extend(new_comps)
    if jobs:
        transact(label)
        for job in jobs:
            param = job["param"]
            if "keys" in job:
                by_tick = {}
                for secs, value in job["keys"]:
                    by_tick[ticks(secs)] = {"t": secs, "value": value}  # same tick: the later one wins
                param["keyframed"] = True
                param["keyframes"] = [by_tick[k] for k in sorted(by_tick)]
            else:
                param["value"] = job["value"]
        scale = motion_scale(clip) if kind == "video" else None
        if scale and is_num(start_value(scale)):
            clip["scale"] = start_value(scale)
    return {"set": [j["where"] for j in jobs], "createdEffects": created, "keyframes": keyframes, "undoLabel": label}


def h_list_transition_names(p):
    check_guard_only()
    return {"names": list(TRANSITION_NAMES)}


def h_add_transition(p):
    require_text(p, ["matchName"])
    require_sequence()
    vt, at, m = p["videoTrack"], p["atSeconds"], p["matchName"]
    clip = clip_at(vt, at)
    if m not in TRANSITION_NAMES:
        raise Coded("NOT_FOUND", f"Unknown transition {m} (list_transition_names lists them).")
    frames = math.floor(p["durationSeconds"] * FPS + 1e-9)  # Premiere floors the duration to whole frames
    if frames < 1:
        raise Coded("BAD_PARAMS", "durationSeconds is shorter than one frame.")
    dur = frames / FPS
    at_start = bool(p.get("applyToStart"))
    single = p.get("forceSingleSided") is not False
    cut = clip["start"] if at_start else clip["end"]
    neighbour = any(abs((o["end"] if at_start else o["start"]) - cut) <= EPS
                    for o in track("video", vt) if o is not clip)
    if single or not neighbour:
        start, end = (cut, cut + dur) if at_start else (cut - dur, cut)
    else:
        start, end = cut - dur / 2, cut + dur / 2
    label = p.get("undoLabel") or f"Agent: {m} at {'start' if at_start else 'end'} of V{vt + 1} @{js_num(at)}s"
    on_track = lambda: sum(t["videoTrack"] == vt for t in transitions())  # noqa: E731
    before = on_track()
    transact(label)
    # Premiere 27.1 keeps a transition already at the edit point and adds nothing (live, 19 Oct 2026)
    if not any(t["videoTrack"] == vt and abs(t["cut"] - cut) < EPS for t in transitions()):
        drop_transitions(vt, start, end)
        STATE["transitions"].append({"videoTrack": vt, "matchName": m, "name": TRANSITION_NAMES[m], "start": start,
                                     "end": end, "cut": cut})
    after = on_track()
    return {"added": after > before, "matchName": m, "applyToStart": at_start, "durationSeconds": p["durationSeconds"],
            "transitionCount": {"before": before, "after": after}, "undoLabel": label}


def h_set_fps(p):
    require_project()
    refs = p.get("items") or [{"name": n} for n in p.get("itemNames") or []]
    if not refs:
        raise Coded("BAD_PARAMS", "set_fps requires params.items (or itemNames) and params.fps (number)")
    hits, not_found = [], []
    for r in refs:
        matches = match_items(r["name"], r.get("binPath"))
        if not matches:
            not_found.append(r["name"])
        elif len(matches) > 1:  # interpretation changes every use: each reference must name one item
            raise Coded("AMBIGUOUS", f'{len(matches)} project items are named "{r["name"]}" ('
                        + ", ".join(item_ref(i) for i in matches) + "). Name one with its bin path.")
        else:
            hits.append(matches[0])
    if not_found:
        raise Coded("NOT_FOUND", f"No clip named: {', '.join(not_found)}")
    busy = [item_ref(h) for h in hits if running_job(h)]
    if busy:
        raise Coded("TRANSCRIBE_RUNNING", f"Speech-to-Text is running on {', '.join(busy)}; wait for it.")
    label = p.get("undoLabel") or f"Agent: interpret {len(hits)} clip(s) at {js_num(p['fps'])} fps"
    transact(label)
    for h in hits:
        h["fps"] = p["fps"]
    return {"fps": p["fps"], "items": [item_ref(h) for h in hits], "undoLabel": label}


def h_add_marker(p):
    require_text(p, ["name"])
    require_sequence()
    kind = p.get("type") or "Comment"
    dur = p["durationSeconds"] if is_num(p.get("durationSeconds")) else 0
    label = p.get("undoLabel") or f'Agent: marker "{p["name"]}" @{js_num(p["atSeconds"])}s'
    transact(label)
    marker = dict(copy.deepcopy(p), type=kind, durationSeconds=dur, guid=f"fake-marker-{len(STATE['markers']) + 1}")
    STATE["markers"].append(marker)
    color_set = False
    if is_num(p.get("colorIndex")):
        transact(label + " [color]")
        color_set = True
    return {"added": True, "name": p["name"], "atSeconds": p["atSeconds"], "durationSeconds": dur, "type": kind,
            "colorSet": color_set, "undoLabel": label}


def h_remove_items(p):
    require_sequence()
    frm, to = p["fromSeconds"], p["toSeconds"]
    mode = "overlap" if p.get("mode") == "overlap" else "inside"
    wanted = [(k, i) for k, idxs in (("video", p.get("videoTracks") or []), ("audio", p.get("audioTracks") or []))
              for i in dict.fromkeys(idxs)]
    if not wanted:
        raise Coded("BAD_PARAMS", "remove_items requires videoTracks and/or audioTracks")
    removed, plan = [], {}
    for kind, idx in wanted:
        kept, gone = [], []
        for it in sorted(track(kind, idx), key=lambda x: x["start"]):
            inside = it["start"] >= frm - EPS and it["end"] <= to + EPS
            hit = inside if mode == "inside" else overlaps(it, frm, to)
            if hit:
                d = {"track": label_of(kind, idx)}
                d.update(describe(it))
                removed.append(d)
                gone.append(it)
            else:
                kept.append(it)
        plan[(kind, idx)] = (kept, gone)
    if p.get("dryRun") or not removed:
        return {"dryRun": bool(p.get("dryRun")), "mode": mode, "removed": removed}
    label = p.get("undoLabel") or f"Agent: remove {len(removed)} clip(s) {js_num(frm)}–{js_num(to)}s"
    transact(label)
    for (kind, idx), (kept, gone) in plan.items():
        if kind == "video":
            # Transitions go with their clip, and so does one at its edit points: removing the clip after a super
            # took the super's outgoing-only fade-out with it (live 27.1, 19 Oct 2026).
            for it in gone:
                drop_transitions(idx, it["start"], it["end"])
                STATE["transitions"] = [t for t in transitions() if not (
                    t["videoTrack"] == idx and min(abs(t["cut"] - it["start"]), abs(t["cut"] - it["end"])) < EPS)]
        if p.get("ripple") and gone:  # close the gaps on that track
            cuts = [(it["start"], it["end"]) for it in gone]
            for it in kept:
                shift = sum(e - s for s, e in cuts if e <= it["start"] + EPS)
                it["start"], it["end"] = snap(it["start"] - shift), snap(it["end"] - shift)
            if kind == "video":
                for t in transitions():
                    if t["videoTrack"] == idx:
                        shift = sum(e - s for s, e in cuts if e <= t["start"] + EPS)
                        t["start"], t["end"], t["cut"] = t["start"] - shift, t["end"] - shift, t["cut"] - shift
        STATE[kind][idx] = kept
    return {"dryRun": False, "mode": mode, "removed": removed, "undoLabel": label}


def h_clone_clip(p):
    """Premiere's SequenceEditor clone: an exact copy of the one clip under atSeconds (effects, volume and scale
    travel with it; in/out unchanged) offsetSeconds later, trackOffset tracks away, overwriting what is there.
    Only that item is cloned (measured: a video clone does not bring its linked audio)."""
    require_sequence()
    kind, idx, at, off = p["kind"], p["track"], p["atSeconds"], p["offsetSeconds"]
    dest = idx + (p.get("trackOffset") or 0)
    src = item_at(kind, idx, at)
    before = describe(src)
    track(kind, dest)  # the destination must exist
    if before["start"] + off < -EPS:
        raise Coded("BAD_PARAMS", f"The copy would start at {before['start'] + off:.3f}s, before 0s.")
    label = p.get("undoLabel") or f"Agent: clone {before['name']} {label_of(kind, idx)} by {js_num(off)}s"
    transact(label)
    chain(src, kind)  # materialise the chain so the copy carries it
    clone = copy.deepcopy(src)
    start = snap(src["start"] + off)  # frame-exact on the live build
    clone.update(start=start, end=start + (src["end"] - src["start"]))
    overwrite(kind, dest, clone["start"], clone["end"], clone)
    # Like the panel: the reply is computed from the source, the timeline holds Premiere's (snapped) position.
    return {"cloned": True, "kind": kind, "track": dest, "source": before, "start": before["start"] + off,
            "end": before["end"] + off, "undoLabel": label}


# ---------------------------------------------------------------- trims: the one place their semantics live
#
# MEASURED on Premiere 27.1 build 17 (track item createSetStart/End/InPoint/OutPoint/MoveAction):
#   start    head trim: start moves to the new time, inPoint moves by the same delta, end stays
#   inPoint  ALSO a head trim: inPoint moves to the new time, start moves by the same delta, end stays
#   end      tail trim: end moves to the new time, outPoint moves by the same delta, start stays
#   outPoint tail trim: outPoint moves to the new time, end moves by the same delta, start stays
#   moveTo   absolute new start, length and source range kept (Premiere's createMoveAction moves BY a time; the
#            panel converts), and never combined with other fields (the panel refuses that)
# Sequence times land on the frame grid (live: moveTo 60 -> start 59.9933); source-time deltas are whole frames.
# NOT probed, the fake's own choice (stricter than the panel, which leaves it to Premiere): a trim that leaves less
# than a frame, a negative in point, an out point past the media's end, or an overlap with another clip on the
# track counts as Premiere's executeTransaction returning false (TRANSACTION_FAILED).

TRIM_FIELDS = ("start", "end", "inPoint", "outPoint", "moveTo")  # applied in this order, one transaction


def apply_trim(clip, field, t):
    """One trim_clip field on a clip dict, in place (see the table above)."""
    whole_frames = lambda d: round(d * FPS) / FPS  # noqa: E731
    if field == "start":
        d = snap(t) - clip["start"]
        clip["start"], clip["inPoint"] = clip["start"] + d, clip["inPoint"] + d
    elif field == "inPoint":
        d = whole_frames(t - clip["inPoint"])
        clip["start"], clip["inPoint"] = clip["start"] + d, clip["inPoint"] + d
    elif field == "end":
        d = snap(t) - clip["end"]
        clip["end"], clip["outPoint"] = clip["end"] + d, clip["outPoint"] + d
    elif field == "outPoint":
        d = whole_frames(t - clip["outPoint"])
        clip["end"], clip["outPoint"] = clip["end"] + d, clip["outPoint"] + d
    elif field == "moveTo":
        d = snap(t) - clip["start"]
        clip["start"], clip["end"] = clip["start"] + d, clip["end"] + d
    else:
        raise ValueError(f"unknown trim field {field!r}")


def media_duration(clip):
    path = clip.get("mediaPath")
    for it in STATE["items"]:
        if (it["mediaPath"] == path) if path else (it["name"] == clip["name"]):
            return it.get("duration")
    return None


def trim_problem(kind, idx, clip, after):
    """Why Premiere would (presumably) refuse the trimmed clip `after`; None when it fits. Unprobed, see above."""
    if after["end"] - after["start"] < 1 / FPS - EPS:
        return "the clip would be shorter than one frame"
    if after["inPoint"] < -EPS:
        return f"the in point would be {after['inPoint']:.4f}s, before the media's start"
    dur = media_duration(clip)
    if is_num(dur) and after["outPoint"] > dur + EPS:
        return f"the out point would be {after['outPoint']:.4f}s, past the media's end ({js_num(dur)}s)"
    for other in track(kind, idx):
        if other is not clip and other["start"] < after["end"] - EPS and other["end"] > after["start"] + EPS:
            return f"it would overlap {other['name']} ({other['start']:.4f}-{other['end']:.4f}s)"
    return None


def h_trim_clip(p):
    require_sequence()
    kind, idx, at = p["kind"], p["track"], p["atSeconds"]
    fields = [k for k in TRIM_FIELDS if is_num(p.get(k))]
    if not fields:
        raise Coded("BAD_PARAMS", "trim_clip needs start, end, inPoint, outPoint or moveTo.")
    clip = item_at(kind, idx, at)
    tl = label_of(kind, idx)
    before = describe(clip)
    if "moveTo" in fields and len(fields) > 1:
        raise Coded("BAD_PARAMS", "moveTo cannot be combined with trims in one call (the move is computed from the "
                                  "clip's start before the call).")
    new_start = p["moveTo"] if "moveTo" in fields else p["start"] if "start" in fields else before["start"]
    if new_start < -EPS:
        raise Coded("BAD_PARAMS", f"The clip would start at {new_start:.3f}s, before 0s.")
    label = p.get("undoLabel") or f"Agent: trim {before['name']} on {tl} ({', '.join(fields)})"
    after = {k: clip[k] for k in ("start", "end", "inPoint", "outPoint")}
    for k in fields:
        apply_trim(after, k, p[k])
    problem = trim_problem(kind, idx, clip, after)
    transact(label)
    if problem:  # counted as a side effect first, like a panel whose executeTransaction returns false
        raise Coded("TRANSACTION_FAILED", f'Transaction "{label}" returned false (fake panel: {problem}).')
    if kind == "video":  # unprobed: a move takes the clip's edge transitions along, a trim drops the moved edge's
        delta = after["start"] - clip["start"]
        keep = []
        for t in transitions():
            on_start = t["videoTrack"] == idx and abs(t["cut"] - clip["start"]) < EPS
            on_end = t["videoTrack"] == idx and abs(t["cut"] - clip["end"]) < EPS
            if (on_start or on_end) and "moveTo" in fields:
                t["start"], t["end"], t["cut"] = t["start"] + delta, t["end"] + delta, t["cut"] + delta
            elif (on_start and abs(delta) > EPS) or (on_end and abs(after["end"] - clip["end"]) > EPS):
                continue
            keep.append(t)
        STATE["transitions"] = keep
    clip.update(after)
    STATE[kind][idx] = sorted(STATE[kind][idx], key=lambda x: x["start"])
    return {"trimmed": True, "kind": kind, "track": idx, "fields": fields, "before": before, "after": describe(clip),
            "undoLabel": label}


def h_shift_items(p):
    """Move every clip that starts at or after fromSeconds (half a frame tolerance) on the given tracks (absent: all
    tracks of that kind; [] : none) by deltaSeconds in one transaction - the sync-keeping ripple after a cut. A left
    shift needs the room: a clip that stays but reaches past fromSeconds + deltaSeconds -> PRECONDITION_FAILED.
    Moves run left shift earliest first, right shift latest first; each lands on the frame grid like moveTo."""
    require_sequence()
    frm, delta = p["fromSeconds"], p["deltaSeconds"]
    half = 0.5 / FPS
    picks = []
    for kind in ("video", "audio"):
        asked = p.get("videoTracks" if kind == "video" else "audioTracks")
        picks += [(kind, i) for i in (asked if isinstance(asked, list) else range(len(STATE[kind])))]
    moves = []
    for kind, idx in picks:
        tl = label_of(kind, idx)
        for it in sorted(track(kind, idx), key=lambda x: x["start"]):
            if it["start"] >= frm - half:
                moves.append((kind, idx, it))
            elif delta < 0 and it["end"] > frm + delta + half:
                raise Coded("PRECONDITION_FAILED", f'{tl}: "{it["name"]}" ({it["start"]:.3f}–{it["end"]:.3f}s) is in '
                                                   f"the way of a {js_num(delta)}s shift from {js_num(frm)}s.")
    if not moves:
        return {"shifted": [], "deltaSeconds": delta, "undoLabel": None}
    moves.sort(key=lambda m: m[2]["start"], reverse=delta > 0)  # stable: ties keep track order, like the panel
    kind, idx, first = moves[0] if delta < 0 else moves[-1]
    if first["start"] + delta < -EPS:
        raise Coded("PRECONDITION_FAILED", f'{label_of(kind, idx)}: "{first["name"]}" would start at '
                                           f"{first['start'] + delta:.3f}s, before 0s.")
    label = p.get("undoLabel") or f"Agent: shift {len(moves)} clip(s) from {js_num(frm)}s by {js_num(delta)}s"
    transact(label)
    shifted = []
    for kind, idx, it in moves:
        shifted.append({"track": label_of(kind, idx), "name": it["name"], "from": it["start"], "to": it["start"] + delta})
        apply_trim(it, "moveTo", it["start"] + delta)
    for kind, idx in dict.fromkeys((k, i) for k, i, _ in moves):
        STATE[kind][idx] = sorted(STATE[kind][idx], key=lambda x: x["start"])
        if kind == "video":
            shift_transitions(idx, frm - half, delta)
    return {"shifted": shifted, "deltaSeconds": delta, "undoLabel": label}


def silent_wav(path, seconds, rate=48000, channels=2):
    """A silent 16-bit PCM WAV (what export_sequence writes instead of a render)."""
    import wave
    frames = max(1, int(round(seconds * rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * channels * frames)


def h_export_sequence(p):
    """Writes a silent 48 kHz stereo 16-bit WAV (the live render's format) as long as the sequence - or
    PPRO_FAKE_EXPORT_SECONDS - to outputPath whatever the preset or workAreaOnly: not a render. Fails like Premiere
    for a missing output folder (exportSequence returns false) and for a preset path containing /no/such/
    (exportSequence throws "Unable to initialize export!")."""
    require_text(p, ["outputPath", "presetPath"])
    require_sequence()
    out, preset = Path(p["outputPath"]), p["presetPath"]
    if re.search(r"[\\/]no[\\/]such[\\/]", preset):
        raise Coded("EXPORT_FAILED", f"exportSequence failed (Unable to initialize export!; preset {preset})")
    if not out.parent.is_dir():
        raise Coded("EXPORT_FAILED", f"exportSequence returned false (preset {preset}; {out.parent} is not a folder)")
    seconds = os.environ.get("PPRO_FAKE_EXPORT_SECONDS")
    if seconds is None:
        seconds = max([it["end"] for t in STATE["video"] + STATE["audio"] for it in t] or [1.0])
    try:
        silent_wav(out, float(seconds))
    except OSError as e:
        raise Coded("EXPORT_FAILED", f"exportSequence returned false (preset {preset}; {e})") from None
    return {"exported": True, "outputPath": p["outputPath"], "preset": preset}



# ---------------------------------------------------------------- probe_api / api_smoke (no Premiere API here)

FAKE_API = {  # a small, fixed slice of the premierepro module, in probe_api's notation
    "Project": {"type": "class", "arity": 0, "static": ["getActiveProject(0)", "open(2)"],
                "proto": ["executeTransaction(2)", "getActiveSequence(0)", "getRootItem(0)", "lockedAccess(1)",
                          "name{get}"]},
    "SequenceEditor": {"type": "class", "arity": 0, "static": ["getEditor(1)"],
                       "proto": ["createCloneTrackItemAction(6)", "createOverwriteItemAction(4)"]},
    "Constants": {"type": "object", "members": ["TrackItemType:object"],
                  "nested": {"TrackItemType": ["CLIP=1", "EMPTY=0", "TRANSITION=2"]}},
}


def h_probe_api(p):
    """A canned inventory: the fake has no UXP module to walk."""
    out = {"host": dict(HOST), "premierepro": copy.deepcopy(FAKE_API), "uxp": {}, "problems": []}
    if p.get("instances") is not False:
        require_project()
        out["instances"] = {"project": FAKE_API["Project"]["proto"]}
    elif _guard():
        require_project()
    out["counts"] = {"exported": len(FAKE_API), "members": sum(len(v.get("static", [])) + len(v.get("proto", []))
                                                            + len(v.get("members", [])) for v in FAKE_API.values()),
                     "instances": len(out.get("instances") or {})}
    return out


SMOKE_NEEDS = {  # mirrors requireInputs in plugin/lib/smoke.js
    "transitions": ["videoTrack", "atSeconds"], "components": ["videoTrack", "atSeconds"],
    "keyframes": ["videoTrack", "atSeconds"], "lumetri": ["videoTrack", "atSeconds"],
    "blend_mode": ["videoTrack", "atSeconds"], "selection": ["videoTrack", "atSeconds"],
    "clip_props": ["videoTrack", "atSeconds"], "track": ["videoTrack"], "project_items": ["itemName"],
    "source_monitor": ["itemName"], "relink": ["itemName", "copyPath"], "converter_exports": ["outputDir"],
    "cleanup_bin": ["binName"],
}


def h_api_smoke(p):
    """The fake has no Premiere API to exercise: it checks the same inputs as the panel and skips every step.
    Unlike the panel it runs in any project (the PAB_TEST gate is the panel's; tests/panel covers it)."""
    require_sequence()
    case = p["case"]
    needs = list(SMOKE_NEEDS.get(case, []))
    if case == "audio_filter" and p.get("displayName"):
        needs = ["audioTrack", "atSeconds"]
    if case == "mogrt" and p.get("mogrtPath"):
        needs = ["videoTrack", "audioTrack", "atSeconds"]
    if case == "implicit_track" and p.get("allowTrackAdd"):
        needs = ["atSeconds", "itemName"]
    missing = [n for n in needs if p.get(n) is None or p.get(n) == ""]
    if missing:
        raise Coded("BAD_PARAMS", f"Case {case} needs {', '.join(missing)}.")
    if case == "cleanup_bin" and not str(p["binName"]).startswith("PAB_API_"):
        raise Coded("BAD_PARAMS", f'cleanup_bin only removes bins named PAB_API_*, not "{p["binName"]}".')
    if "videoTrack" in needs and "atSeconds" in needs:
        item_at("video", p["videoTrack"], p["atSeconds"])
    steps = [{"api": case, "status": "skipped", "detail": "fake panel: no Premiere API", "ms": 0}]
    return {"case": case, "project": project_name(), "steps": steps,
            "summary": {"pass": 0, "fail": 0, "absent": 0, "error": 0, "skipped": 1}, "leftovers": []}


HANDLERS = {  # in actions.json order
    "ping": h_ping,
    "get_project": h_get_project,
    "get_sequence": h_get_sequence,
    "list_items": h_list_items,
    "inspect_items": h_inspect_items,
    "list_project_items": h_list_project_items,
    "export_frame": h_export_frame,
    "export_otio": h_export_otio,
    "list_transcripts": h_list_transcripts,
    "export_transcript": h_export_transcript,
    "import_transcript": h_import_transcript,
    "transcribe_clip": h_transcribe_clip,
    "import_media": h_import_media,
    "insert_clip": h_insert_clip,
    "set_scale": h_set_scale,
    "set_params": h_set_params,
    "list_transition_names": h_list_transition_names,
    "add_transition": h_add_transition,
    "set_fps": h_set_fps,
    "add_marker": h_add_marker,
    "remove_items": h_remove_items,
    "clone_clip": h_clone_clip,
    "trim_clip": h_trim_clip,
    "export_sequence": h_export_sequence,
    "shift_items": h_shift_items,
    "probe_api": h_probe_api,
    "api_smoke": h_api_smoke,
}
_DEFAULT_HANDLERS = dict(HANDLERS)


def reset():
    """A fresh in-memory project and a freshly loaded panel (tests call this between cases)."""
    global STUCK, PROBED_AT, CTX, LAST_CTX
    STATE.clear()
    STATE.update(copy.deepcopy(_INITIAL))
    HANDLERS.clear()
    HANDLERS.update(_DEFAULT_HANDLERS)
    STUCK, PROBED_AT = None, None
    FAIL_TRANSACTIONS.clear()
    TRANSACTIONS.clear()
    CTX, LAST_CTX = _fresh_ctx(), _fresh_ctx()


def dispatch(msg: dict) -> dict:
    """One request envelope in, one reply envelope out (the panel's handleMessage, without the socket)."""
    global CTX, LAST_CTX
    msg_id, action, params = msg.get("id"), msg.get("action"), msg.get("params")
    LAST_CTX = {"action": action, "id": msg_id, "guard": {}, "sideEffects": 0}  # until the handler runs
    a = spec.action(action) if isinstance(action, str) else None
    handler = HANDLERS.get(action) if isinstance(action, str) else None

    def fail(code, error, data=None):
        reply = {"id": msg_id, "ok": False, "error": error, "code": code, "outcome": "not_committed"}
        if data is not None:
            reply["data"] = data
        return reply

    if a is None or handler is None:
        return fail("UNKNOWN_ACTION", f'Unknown action "{action}". Whitelist: {", ".join(HANDLERS)}')
    if STUCK and not a["allowedWhenStuck"]:
        return fail("PANEL_STUCK", stuck_text())
    denied = os.environ.get("PPRO_FAKE_DENIED")
    if denied and not a["allowedWhenDenied"]:
        return fail("HOST_DENIED", f"This Premiere build is on the panel's denylist ({denied}); it answers ping only.")
    problems = spec.check_params(action, params)
    guard = msg.get("guard") if action != "ping" else None  # ping ignores guards
    if guard is not None and not isinstance(guard, dict):
        problems.append(f"guard: expected object, got {type(guard).__name__}")
    elif guard:
        problems += [f"guard.{k}: unknown key" for k in guard if k not in GUARD_KEYS]
        problems += [f"guard.{k}: expected string, got {type(v).__name__}" for k, v in guard.items()
                     if k in GUARD_KEYS and v is not None and not isinstance(v, str)]
        problems += [f"guard.{k}: must not be empty (an empty prefix would match every project)"
                     for k, v in guard.items() if k in GUARD_KEYS and v == ""]
    if problems:
        return fail("BAD_PARAMS", f"Bad params for {action}: " + "; ".join(problems), data={"problems": problems})
    cap = a.get("capability")
    if cap and capabilities().get(cap) is False:
        return fail("UNSUPPORTED", f"{action} needs the {cap} capability, which this Premiere build lacks.")

    CTX = {"action": action, "id": msg_id, "guard": dict(guard or {}), "sideEffects": 0}
    try:
        reply = {"id": msg_id, "ok": True, "data": handler(copy.deepcopy(params) if params else {})}
    except Exception as e:  # mirror the panel: errors become ok:false with a code and an outcome
        code = getattr(e, "code", None)
        known = isinstance(code, str) and code in spec.load()["errors"]
        outcome = getattr(e, "outcome", None) if known else None
        if outcome not in spec.load()["outcomes"]:
            outcome = "not_committed" if CTX["sideEffects"] == 0 else "unknown"
        if a["kind"] == "read" and outcome == "unknown":
            outcome = "not_committed"
        reply = {"id": msg_id, "ok": False, "error": str(e) if known else f"{type(e).__name__}: {e}",
                 "code": code if known else "INTERNAL", "outcome": outcome}
        if known and getattr(e, "data", None) is not None:
            reply["data"] = e.data
    finally:
        LAST_CTX, CTX = CTX, _fresh_ctx()
    return reply


async def answer(ws, msg):
    action = msg.get("action")
    STATS["received"] += 1
    STATS["inFlight"] += 1
    STATS["maxConcurrent"] = max(STATS["maxConcurrent"], STATS["inFlight"])
    try:
        if DELAY:
            await asyncio.sleep(DELAY)
        reply = dispatch(msg)
    finally:
        STATS["inFlight"] -= 1
    await ws.send(json.dumps(reply))
    print(f"answered {action}: {'ok' if reply['ok'] else reply['error']}", flush=True)


async def main():
    import websockets

    async with websockets.connect(BROKER_URL) as ws:
        await ws.send(json.dumps({"role": "panel"}))
        print(f"fake panel {VERSION} registered at {BROKER_URL}", flush=True)
        tasks = set()
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                print(f"bad JSON from broker: {raw!r}", flush=True)
                continue
            if not isinstance(msg, dict) or not msg.get("id") or not msg.get("action"):
                continue  # the panel drops these too
            if CONCURRENT:  # like an unqueued panel: start each request as it arrives
                task = asyncio.get_running_loop().create_task(answer(ws, msg))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            else:
                await answer(ws, msg)


if __name__ == "__main__":
    asyncio.run(main())
