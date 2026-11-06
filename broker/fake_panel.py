#!/usr/bin/env python3
"""
Fake Premiere panel for testing the broker + CLI without Premiere.

Speaks the panel's protocol (ppro_bridge/actions.json, protocol 2) for every action, from an in-memory project:
a 1080x1920 / 59.94 fps sequence "Sequence 01" with 4 video and 4 audio tracks. interview.mp4 (with a
transcript) runs 0-60 s on V1/A1 like a talking-head A-roll; broll_city.mp4 and two title.mov (an EN and a CN
render: same name, different media) wait in the project panel; a Comment marker "harness marker" sits at 1 s (as in
the panel harness's mock project). Imports, placements, effect writes, transitions, markers, removals, clones, trims,
subclips, relinks, restore points (sequence clones: a name, a guid and custom properties, no timeline of their own) and Speech-to-Text
jobs change that state, so multi-step flows (apply_plan ->
list_items -> remove_items, replace_clip, split by clone + trim, transcribe_clip -> list_transcripts ->
export_transcript) can be tested end to end. Trim semantics and the audio chain follow live probes of Premiere 27.1
build 17 (see apply_trim and AUDIO_INTRINSIC); export_sequence writes a silent WAV instead of a render.

`dispatch(msg)` is the panel's request handling without the socket, in the panel's order: unknown action, stuck,
denied host, the spec's param check (ppro_bridge.spec.check_params), capability, then the handler under the
request's guard; errors get a code and an outcome by the outcome rule (explicit wins, else not_committed while the
request committed nothing, else unknown; reads never unknown). Tests call it in-process; run as a script it
registers with a broker instead of (not alongside) the real panel: a broker refuses it while a live panel is
connected (close 4409). For an isolated test, point broker, panel and CLI at another port:

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
  PPRO_FAKE_RELEASE=1             a release build (plugin/lib/build.js dev false): ping says build.dev false and the
                                  devOnly actions (api_smoke) are not registered (UNKNOWN_ACTION)
  PPRO_FAKE_PACKAGED=1            an installed .ccx package: ping says build.packaged true
  PPRO_FAKE_STT_POLLS=<n>         list_transcripts calls until a Speech-to-Text job ends (default 1)
  PPRO_FAKE_STT_RESULT=done|fail|reject   a job attaches a transcript / fails later / is rejected at once
  PPRO_FAKE_TRANSCRIPT_IMPORT=static|callback|empty   import_transcript works with the static form / only with
                                  the callback form / not at all (Premiere attaches an empty shell)
  PPRO_FAKE_MISSING_LANGS=<a,b>   Speech-to-Text languages whose pack is not installed (default ko-KR)
  PPRO_FAKE_EXPORT_SECONDS=<s>    length of the silent WAV export_sequence writes (default: the sequence)
  PPRO_FAKE_EXPORT_POLLS=<n>      export_status calls until an export job (export_sequence wait false) ends
                                  (default 1; 0: it has ended when export_sequence replies)
  PPRO_FAKE_EXPORT_RESULT=done|fail|cancel|silent|stall   a job ends after progress events and a complete event /
                                  with a render error / cancelled / done without any event (the promise only) /
                                  never (progress frozen)
  PPRO_FAKE_AAF_RESULT=done|fail|nofile   an export_aaf job (ending after PPRO_FAKE_EXPORT_POLLS polls, like a render)
                                  writes a small placeholder .aaf / returns false / reports done but writes nothing
  PPRO_FAKE_SOURCE_FLOOR=1        insert_clip floors a source in/out point to the item's frame, as Premiere does
                                  (see source_frame); off by default, so source times stay as sent
In-process hooks: STATE (the project; clips may be bare dicts, components and transitions default lazily; an audio
clip with "channels": 2 gets the stereo volume component, its name verified on 27.1; a project item or video clip with
"graphic": True is a graphic or text clip: silent, with GRAPHIC_CHAIN; "layers": False on it too: a graphic whose
layer components never arrive),
STATE["aafDialog"] (True: Premiere's modal "AAF export log" is up - export_aaf jobs keep running, write nothing and
show their hint until a test sets it back to False, the user's OK click),
STUCK (set a dict {action, id, since} to make the panel stuck), FAIL_TRANSACTIONS (label substrings whose
transaction returns false), OP_FAULTS (apply_ops op name -> "build": its Action cannot be built, nothing is committed;
"ignore": it commits without effect, so the read-back fails), TRANSACTIONS (labels committed since reset()), LAST_CTX (the last request's guard and
side-effect count), reset().
"""

import asyncio
import copy
import datetime
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
from ppro_bridge import auth, items as pitems, spec  # noqa: E402  (stdlib only)

BROKER_URL = os.environ.get("PPRO_BRIDGE_URL", "ws://127.0.0.1:8017")
VERSION = "0.7.0"
PLUGIN_ID = "org.pproagentbridge.panel"  # the real panel's manifest id: its hello names it, as this one does
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
# A graphic or text clip's own components (not addable): its Vector Motion and a text layer. Measured on 27.1 (2 Nov,
# Basic Title.mogrt): Opacity, Motion, Graphic Group ("矢量运动"), Text ("文本", AE.ADBE Text) about 0.9 s after
# placing; effects go between Graphic Group and the layers (an effect after Text left the frame unchanged), so
# set_params puts new ones before the first layer (insertion_index).
GRAPHIC = {
    "AE.ADBE Graphic Group": ("Vector Motion", [("Position", {"x": 0.5, "y": 0.5}), ("Scale", 100.0),
                                                ("Rotation", 0.0)]),
    "AE.ADBE Text": ("Text", [("Source Text", "Title"), ("Transform Position", {"x": 0.5, "y": 0.5})]),
}
GRAPHIC_CHAIN = ("AE.ADBE Opacity", "AE.ADBE Motion", "AE.ADBE Graphic Group", "AE.ADBE Text")
# The panel's effects.TOP_INTRINSICS / EFFECT_CONTAINERS: new effects go after the last of these or of an added filter.
TOP_INTRINSICS = ("AE.ADBE Opacity", "AE.ADBE Motion", "AE.ADBE Graphic Group", "AE.ADBE Time Remapping")
EFFECT_CONTAINERS = ("AE.ADBE CompoundComponent",)
# Audio clips' chain. MEASURED on one mono clip (Premiere 27.1 build 17): one component, "Internal Volume Mono",
# param 0 a boolean (bypass), param 1 Level in Premiere's linear unit (dB = 20 * log10(v) + 15). PROVISIONAL: the
# display names are guesses, and "Internal Volume Stereo" (a clip with "channels": 2) is UNVERIFIED. No Channel
# Volume / Panner on that mono clip. set_params does not add audio effects (the panel refuses it too); add_audio_effect
# does, by display name (AUDIO_FILTERS).
LEVEL_0DB = 0.17782793939113617  # Level at 0 dB as Premiere reports it (float32 of 10 ** (-15 / 20))
AUDIO_INTRINSIC = {
    "Internal Volume Mono": ("Volume", [("Bypass", False), ("Level", LEVEL_0DB)]),
    "Internal Volume Stereo": ("Volume", [("Bypass", False), ("Level", LEVEL_0DB)]),  # live: names localised
}


# Audio effects AudioFilterFactory offers, by display name (it lists no match names): display name -> (match name,
# [(param name, default)], params time-varying from the start). The same two as the harness mocks. MEASURED on 27.1
# b21 (Chinese UI, 22 Oct 2026): "降噪" has a GUID match name and all six params report isTimeVarying right after
# adding (keyframe count unmeasured; none here), so set_params refuses static values on them (KEYFRAMED).
AUDIO_FILTERS = {
    "DeNoise": ("AE.ADBE DeNoise", [("Bypass", False), ("Amount", 40.0)], False),
    "降噪": ("c6762838-e451-4d22-9c90-b0eaa734605a", [("旁路", False), ("数量", 0.4), ("补充增益", 0.0),
                                                     ("自动增益", 0.0), ("搁架式", 0.0), ("输出差分信号", 0.0)], True),
}
MATCH_NAME = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-|[A-Za-z]+\.\S)", re.I)  # a GUID or "Vendor.Name", as the panel


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
    "markers": [{"name": "harness marker", "atSeconds": 1.0, "durationSeconds": 0, "type": "Comment", "comments": "",
                 "guid": "fake-marker-1"}],
    "otherSequences": [],  # the project's other sequences (clone_sequence): [{name, guid, props}], no timelines
    "transitions": [],  # [{videoTrack, matchName, name, start, end, cut}] — sequence seconds
    # "shiftKeepsTransitions": True — shift_items carries transitions along (27.1 drops them: B2)
    # "transitionsCentreAtNeighbours": True — add_transition at a cut with a neighbour centres it even when asked
    #   single-sided and keeps of the half over the neighbour only what its handle covers (27.1, live 6 Nov 2026)
    # "sequenceSwitchLag": n — set_active_sequence replies "switched" but the switch lands only after n more
    #   get_project reads (True: never), as two quick switches did live on 27.1 (6 Nov 2026)
    "jobs": [],         # Speech-to-Text jobs (lost on reset, like on a panel reload)
    "exports": [],      # export jobs (export_sequence wait false, export_aaf; lost on reset too)
    "aafDialog": False,  # True: the modal "AAF export log" holds every export_aaf job (a test clicks OK: False)
    "selection": [],    # the Timeline selection: clip dicts of the tracks above (select_items)
    "bins": [],         # empty bins apply_ops created (bin paths); the others are the items' bin paths
    "trackNames": {},   # label -> name, where apply_ops track.rename renamed a track ("Video 1" ... otherwise)
}
_INITIAL = copy.deepcopy(STATE)

STUCK = None               # {action, id, since}: refuse everything but ping, like a panel whose watchdog fired
FAIL_TRANSACTIONS = set()  # label substrings: such a transaction "returns false" (TRANSACTION_FAILED)
OP_FAULTS = {}             # apply_ops op name -> "build" (its Action cannot be built) | "ignore" (it commits, changes nothing)
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


def transact(label, actions=1):
    """The panel's runTransaction: more than MAX_TRANSACTION_ACTIONS actions are refused before anything happens;
    the side effect counts before executeTransaction, which may return false."""
    if actions > MAX_TRANSACTION_ACTIONS:
        raise Coded("BAD_PARAMS", f'Transaction "{label}" would hold {actions} actions; the panel runs at most '
                                  f"{MAX_TRANSACTION_ACTIONS} in one (a runaway guard). Nothing was changed: split "
                                  "the edit into smaller requests.",
                    data={"actions": actions, "max": MAX_TRANSACTION_ACTIONS})
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
    """The guard's project / sequence pins (a fingerprint alone pins neither: reads ignore it)."""
    return {k: v for k, v in (CTX.get("guard") or {}).items() if v is not None and k in PIN_KEYS}


PIN_KEYS = ("project", "projectPrefix", "projectGuid", "sequence", "sequenceGuid")
GUARD_KEYS = PIN_KEYS + ("fingerprint",)
FEATURES = ["fingerprintGuard", "shiftItemsPrecise", "exportJobs"]  # the panel's core.FEATURES (actions.json "features")
MAX_TRANSACTION_ACTIONS = 1000  # the panel's runaway guard per transaction (core.MAX_TRANSACTION_ACTIONS)


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


def check_fingerprint_guard(action_spec, params):
    """The panel's core.checkFingerprintGuard: a write that is not a dry run, pinned to a timeline fingerprint
    (items.fingerprint of list_items + get_sequence), is refused with PRECONDITION_FAILED unless the active sequence
    still has it. Checked once, before the handler."""
    want = (CTX.get("guard") or {}).get("fingerprint")
    if not want or action_spec["kind"] != "write" or (params or {}).get("dryRun") is True:
        return
    require_project()
    seq = STATE.get("sequence")
    if seq is None:
        raise Coded("PRECONDITION_FAILED", f"The project has no active sequence; the request is pinned to timeline "
                                           f"fingerprint {want}. Nothing was changed.",
                    data={"expected": want, "actual": None})
    actual = pitems.fingerprint(listing(["video", "audio"]), {"guid": seq.get("guid")})
    if actual != want:
        raise Coded("PRECONDITION_FAILED", f"The timeline changed since its fingerprint was read ({want}, now "
                                           f"{actual}): someone edited it, or another sequence is active. Nothing "
                                           "was changed; read it again (get_sequence) and re-plan.",
                    data={"expected": want, "actual": actual})


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


def track_name(kind, idx) -> str:
    """A track's name: "Video 1" / "Audio 1" unless apply_ops track.rename set another (STATE["trackNames"])."""
    return (STATE.get("trackNames") or {}).get(label_of(kind, idx)) or         f"{'Video' if kind == 'video' else 'Audio'} {idx + 1}"


def describe(it):
    media = it.get("mediaPath") or next((p["mediaPath"] for p in STATE["items"] if p["name"] == it["name"]), None)
    return {"name": it.get("clipName") or it["name"], "start": it["start"], "end": it["end"], "inPoint": it["inPoint"],
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
    display, params = (INTRINSIC.get(match_name) or AUDIO_INTRINSIC.get(match_name) or GRAPHIC.get(match_name)
                       or FILTERS[match_name])
    return {"matchName": match_name, "displayName": display,
            "params": [{"name": n, "value": copy.deepcopy(v), "keyframed": False, "keyframes": []} for n, v in params]}


def chain(clip, kind="video"):
    """A clip's component chain, created on first use: video (Opacity, Motion, then added filters; a graphic's
    GRAPHIC_CHAIN) or audio (the provisional volume component; see AUDIO_INTRINSIC)."""
    if "components" not in clip:
        if kind == "audio":
            stereo = clip.get("channels", 1) == 2
            clip["components"] = [make_component("Internal Volume Stereo" if stereo else "Internal Volume Mono")]
        else:
            names = GRAPHIC_CHAIN if clip.get("graphic") else ("AE.ADBE Opacity", "AE.ADBE Motion")
            if clip.get("graphic") and clip.get("layers") is False:
                names = GRAPHIC_CHAIN[:3]
            comps = [make_component(m) for m in names]
            comps[1]["params"][1]["value"] = float(clip.get("scale", 100.0))
            clip["components"] = comps
    return clip["components"]


def start_value(param):
    """getStartValue(): a keyframed param reports its first keyframe's value."""
    if param.get("keyframed") and param.get("keyframes"):
        return param["keyframes"][0]["value"]
    return param.get("value")


def value_at(param, t):
    """getValueAtTime(media seconds): linear between numeric keyframes (every non-hold mode), held otherwise."""
    kfs = param.get("keyframes") or []
    if not param.get("keyframed") or not kfs:
        return param.get("value")
    if t <= kfs[0]["t"]:
        return kfs[0]["value"]
    for a, b in zip(kfs, kfs[1:]):
        if t < b["t"]:
            if a.get("mode") != INTERPOLATION["HOLD"] and is_num(a["value"]) and is_num(b["value"]) and b["t"] > a["t"]:
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
MAX_CREATED = 16           # effect instances one set_params request may add (guards a typo in occurrence)
# Constants.InterpolationMode on 27.1 (1-3 are numbers UXP does not name)
INTERPOLATION = {"LINEAR": 0, "HOLD": 4, "BEZIER": 5, "TIME": 6, "TIME_TRANSITION_START": 7, "TIME_TRANSITION_END": 8}


def interpolation_mode(v, where):
    """A keyframe interpolation by name (any case, "-" or " " for "_") or Premiere's number 0-8 -> (mode, name)."""
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 8:
        return v, next((k for k, n in INTERPOLATION.items() if n == v), str(v))
    if isinstance(v, str):
        key = re.sub(r"[\s-]+", "_", v.strip().upper())
        if key in INTERPOLATION:
            return INTERPOLATION[key], key
    raise Coded("BAD_PARAMS", f"{where}: interpolation {json.dumps(v)} is not one of "
                              f"{', '.join(k.lower() for k in INTERPOLATION)} (or Premiere's number 0-8)")


def curve_samples(param):
    """The value at 0, 25, 50, 75 and 100 % of the way from the first keyframe to the last."""
    kfs = param.get("keyframes") or []
    if len(kfs) < 2:
        return []
    a, b = kfs[0]["t"], kfs[-1]["t"]
    return [{"at": f, "value": copy.deepcopy(value_at(param, a + (b - a) * f))} for f in (0, 0.25, 0.5, 0.75, 1)]


def media_seconds(clip, frame_seconds, time_base, t, bounded=True):
    """A keyframe time in `time_base` -> media seconds, the way the panel converts it (integer ticks).
    media: t; clip: inPoint + t; sequence: inPoint + (t - start). Allowed: [inPoint - frame/2, outPoint + frame/2]
    (any time with bounded=False: remove_keyframes); clip/sequence need speed 1; past 9 h of media time is refused."""
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
    if bounded and (m < in_t - half or m > out_t + half):
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


def release_build() -> bool:
    return os.environ.get("PPRO_FAKE_RELEASE") == "1"


def registered() -> list[str]:
    """The whitelist ping reports: HANDLERS, without the devOnly actions in a release build."""
    release = release_build()
    return [n for n in HANDLERS if not (release and (spec.action(n) or {}).get("devOnly"))]


def h_ping(p):
    global PROBED_AT
    if p.get("probe") and not STUCK:
        PROBED_AT = now_ms()
    out = {"pong": True, "host": f"{HOST['name']} {HOST['version']}", "hostInfo": dict(HOST),
           "plugin": f"fake-panel {VERSION}", "version": VERSION, "protocol": spec.protocol(), "modules": "fake",
           "actions": registered(), "stuck": stuck_view(), "denied": denied_view(),
           "capabilities": capabilities(), "features": list(FEATURES), "loadedAt": LOADED_AT,
           "build": {"dev": not release_build(), "packaged": os.environ.get("PPRO_FAKE_PACKAGED") == "1"}}
    if PROBED_AT:
        out["capabilitiesProbedAt"] = PROBED_AT
    out.update(received=STATS["received"], maxConcurrent=STATS["maxConcurrent"])
    return out


def h_get_project(p):
    proj = require_project()
    pending = STATE.get("pendingSwitch")  # sequenceSwitchLag: a switch that has not landed yet
    if pending and pending["reads"] is not True:
        if pending["reads"] <= 0:
            del STATE["pendingSwitch"]
            _switch_to(pending["target"])
        else:
            pending["reads"] -= 1
    seq = STATE.get("sequence")
    return {"name": project_name(), "path": proj.get("path"), "activeSequence": seq["name"] if seq else None,
            "guid": proj.get("guid")}


def _tracks(kind):
    return [{"index": i, "label": label_of(kind, i), "name": track_name(kind, i),
             "clipCount": len(t)} for i, t in enumerate(STATE[kind])]


def h_get_sequence(p):
    _, seq = require_sequence()
    end = max([it["end"] for t in STATE["video"] + STATE["audio"] for it in t] or [0])
    return {"name": seq["name"], "playerPositionSeconds": seq.get("playerPosition", 4.2), "width": 1080, "height": 1920,
            "timebase": TIMEBASE, "fps": round(FPS, 3), "durationSeconds": end,
            "videoTracks": _tracks("video"), "audioTracks": _tracks("audio"), "captionTrackCount": 1,
            "guid": seq.get("guid")}


SEQUENCE_TIMELINE = ("video", "audio", "transitions", "markers")  # what a sequence switch swaps


def _switch_to(target):
    """Make `target` (one of STATE["otherSequences"]) the active sequence: the active one keeps its timeline, the
    target brings its own (a sequence the fake never opened has as many empty tracks)."""
    seq, others = STATE.get("sequence"), STATE.setdefault("otherSequences", [])
    saved = target.pop("timeline", None) or {"video": [[] for _ in STATE["video"]],
                                             "audio": [[] for _ in STATE["audio"]], "transitions": [], "markers": []}
    if seq is not None:
        seq["timeline"] = {k: STATE[k] for k in SEQUENCE_TIMELINE}
        others.append(seq)
    others[:] = [s for s in others if s is not target]
    STATE.update(saved, sequence=target, selection=[])


def h_set_active_sequence(p):
    """Switch to a sequence of the project: the active one (a switch that changes nothing) or one of the others
    (clone_sequence's restore points; a switch swaps the timelines). STATE["sequenceSwitchLag"] = n: the reply says
    switched, but the switch lands only after n more get_project reads (True: never), as two quick switches did live
    on 27.1 (6 Nov 2026)."""
    require_project()
    seq = STATE.get("sequence")
    want = [s for s in _sequences() if (s.get("guid") == p["guid"] if p.get("guid") else s["name"] == p.get("name"))]
    if not want:
        what = f'with guid {p["guid"]}' if p.get("guid") else f'"{p.get("name")}"'
        raise Coded("NOT_FOUND", f"No sequence {what} in this project.",
                    data={"sequences": [s["name"] for s in _sequences()]})
    if len(want) > 1:
        raise Coded("AMBIGUOUS", f'{len(want)} sequences are named "{p.get("name")}": pass guid.',
                    data={"sequences": [{"name": s["name"], "guid": s.get("guid")} for s in want]})
    target = want[0]
    if target is seq:
        return {"switched": False, "activeSequence": seq["name"], "previous": seq["name"], "guid": seq.get("guid")}
    side_effect()
    if STATE.get("sequenceSwitchLag"):
        STATE["pendingSwitch"] = {"target": target, "reads": STATE["sequenceSwitchLag"]}
    else:
        _switch_to(target)
    return {"switched": True, "activeSequence": target["name"], "previous": seq["name"] if seq else None,
            "guid": target.get("guid")}


def h_select_items(p):
    """The panel's select_items: replace the selection with the clip under atSeconds or the clips in a range on the
    given tracks (clear: empty it). No undo step; nothing else changes."""
    vts, ats = list(dict.fromkeys(p.get("videoTracks") or [])), list(dict.fromkeys(p.get("audioTracks") or []))
    has_at, has_range = isinstance(p.get("atSeconds"), (int, float)), isinstance(p.get("fromSeconds"), (int, float))
    if p.get("clear"):
        if vts or ats or has_at or has_range:
            raise Coded("BAD_PARAMS", "clear empties the selection: give it no tracks or times.")
    else:
        if not vts and not ats:
            raise Coded("BAD_PARAMS", "select_items needs videoTracks and/or audioTracks (or clear).")
        if has_at == has_range:
            raise Coded("BAD_PARAMS", "Give atSeconds or fromSeconds + toSeconds (one of the two).")
    require_sequence()
    mode = "clear" if p.get("clear") else "at" if has_at else "overlap" if p.get("mode") == "overlap" else "inside"
    targets = []
    for kind, idxs in (("video", vts), ("audio", ats)):
        for idx in idxs:
            if mode == "at":
                targets.append((label_of(kind, idx), item_at(kind, idx, p["atSeconds"])))
                continue
            frm, to = p["fromSeconds"], p["toSeconds"]
            for it in sorted(track(kind, idx), key=lambda x: x["start"]):
                hit = it["start"] >= frm - EPS and it["end"] <= to + EPS if mode == "inside" else overlaps(it, frm, to)
                if hit:
                    targets.append((label_of(kind, idx), it))
    if mode in ("inside", "overlap") and not targets:
        labels = [label_of("video", i) for i in vts] + [label_of("audio", i) for i in ats]
        raise Coded("NOT_FOUND", f"No clip {'entirely inside' if mode == 'inside' else 'touching'} "
                                 f"{js_num(p['fromSeconds'])}–{js_num(p['toSeconds'])}s on {', '.join(labels)}"
                                 + (" (mode overlap also takes clips crossing its edges)" if mode == "inside" else "")
                                 + "; the selection is unchanged.")
    selected = [{"track": lab, "name": it["name"], "start": it["start"], "end": it["end"]} for lab, it in targets]
    before = len(STATE.setdefault("selection", []))
    if p.get("dryRun"):
        return {"dryRun": True, "mode": mode, "selected": selected, "before": before}
    side_effect()
    STATE["selection"] = [it for _, it in targets]
    return {"dryRun": False, "mode": mode, "selected": selected, "before": before, "after": len(STATE["selection"]),
            "verified": True}


def listing(kinds) -> dict:
    """The tracks of the given kinds as list_items reports them (also what the fingerprint guard hashes)."""
    return {kind: [{"index": i, "label": label_of(kind, i), "name": track_name(kind, i),
                    "items": [describe(it) for it in sorted(t, key=lambda x: x["start"])]}
                   for i, t in enumerate(STATE[kind])]
            for kind in ("video", "audio") if kind in kinds}


def h_list_items(p):
    _, seq = require_sequence()
    kinds = p.get("kinds") if isinstance(p.get("kinds"), list) and p.get("kinds") else ["video", "audio"]
    return {"sequence": seq["name"], **listing(kinds)}


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
                              "name": track_name(kind, idx), "clips": clips,
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
    src_in = p["inSeconds"] if has_range else item.get("clipIn", 0.0)  # a subclip placed whole: its own range
    src_out = p["outSeconds"] if has_range else item.get("clipOut", item["duration"])
    if has_range and os.environ.get("PPRO_FAKE_SOURCE_FLOOR") == "1":
        src_in, src_out = source_frame(src_in, item), source_frame(src_out, item)
    start, end = snap(at), snap(at + (src_out - src_in))
    clip = {"name": item["name"], "start": start, "end": end, "inPoint": src_in, "outPoint": src_out, "scale": 100.0,
            "mediaPath": item["mediaPath"]}
    if item.get("graphic"):
        clip["graphic"] = True
        if item.get("layers") is False:
            clip["layers"] = False

    def place():
        legs = [("video", v, copy.deepcopy(clip))]
        if not item["name"].endswith(SILENT_SUFFIXES) and not item.get("graphic"):
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


def insertion_index(names, addable) -> int:
    """The panel's effects.insertionIndex: on a graphic, the first layer component after its Graphic Group (neither a
    top intrinsic, an added filter nor an effect container); otherwise, or without layers, the chain's length."""
    if "AE.ADBE Graphic Group" not in names:
        return len(names)
    for i in range(names.index("AE.ADBE Graphic Group") + 1, len(names)):
        if names[i] not in TOP_INTRINSICS and names[i] not in EFFECT_CONTAINERS and names[i] not in addable:
            return i
    return len(names)


def h_set_params(p):
    """Validate the whole request (clip, components, occurrences, param indexes, values, time bases, ranges,
    KEYFRAMED), then add missing effects in one transaction (in request order, at insertion_index) and write the
    params in another. Exactly one of videoTrack / audioTrack; on an audio clip only components it already has
    (its volume) can be written."""
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
    have, created = {}, []  # in request order: each component up to its occurrence ([A 1, B 1, A 2] adds A, B, A)
    for m, occ, _ in reqs:
        have.setdefault(m, sum(c["matchName"] == m for c in comps))
        created += [m] * max(0, occ - have[m])
        have[m] = max(have[m], occ)
    if created and kind == "audio":
        raise Coded("UNSUPPORTED", f"set_params does not add audio effects ({', '.join(created)} is not on the clip): "
                                   "add it with add_audio_effect (by display name) first, then write its params.")
    if len(created) > MAX_CREATED:
        raise Coded("BAD_PARAMS", f"The request would add {len(created)} effect instances (limit {MAX_CREATED}); "
                                  "check occurrence.")
    for m in dict.fromkeys(created):
        if m in INTRINSIC:
            raise Coded("NOT_FOUND", f"Cannot add effect {m}: it is built into every clip, once.")
        if m not in FILTERS:
            raise Coded("NOT_FOUND", f"Cannot add effect {m} (unknown match name, or not an addable filter).")
    new_comps = [make_component(m) for m in created]
    names = [c["matchName"] for c in comps]
    at_index = insertion_index(names, set(FILTERS) | set(created))
    graphic = "AE.ADBE Graphic Group" in names
    here = f"{label_of(kind, idx)} @{js_num(at)}s"
    layer_wait = None
    if created and graphic and at_index == len(names):  # the panel waits up to 2 s for the layers; here none come
        layer_wait = {"seconds": 2.0, "layers": 0}
    behind = [m for m in dict.fromkeys(created) if at_index < len(names) and graphic
              and any(n == m for n in names[at_index:])]
    if behind:
        raise Coded("UNSUPPORTED", f"{here} has {', '.join(behind)} after the graphic's "
                                   "layer components, where it does nothing, and a new instance would have to follow it. "
                                   "Remove it (remove_effect), then add the effects again. Nothing was changed.",
                    data={"chain": names})
    planned = comps[:at_index] + new_comps + comps[at_index:]  # the chain after this request's additions
    created_components, counts = [], {}
    for k, m in enumerate(created):
        counts[m] = counts.get(m, sum(c["matchName"] == m for c in comps)) + 1
        created_components.append({"matchName": m, "occurrence": counts[m], "index": at_index + k})
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
            interp = None if prm.get("interpolation") is None else interpolation_mode(prm["interpolation"], where)
            if interp and not kfs and prm.get("value") is None:  # the keyframes the param already has
                if not cur.get("keyframed"):
                    raise Coded("BAD_PARAMS", f"{where} is not keyframed; interpolation applies to keyframes.")
                jobs.append({"param": cur, "where": where, "interp": interp, "matchName": m, "occurrence": occ,
                             "index": idx})
            elif kfs:
                tb = prm.get("timeBase")
                if not tb:
                    raise Coded("BAD_PARAMS", f"{where}: keyframes need a timeBase (media, clip or sequence).")
                if cur.get("keyframed") and cur.get("keyframes"):  # armed with no keys (an added audio effect): fine
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
                             "index": idx, "timeBase": tb, "interp": interp})
            else:
                if prm.get("value") is None:
                    raise Coded("BAD_PARAMS", f"{where}: needs a value, keyframes or an interpolation.")
                if interp:
                    raise Coded("BAD_PARAMS", f"{where}: interpolation applies to keyframes, not to a static value.")
                if cur.get("keyframed") and cur.get("keyframes"):  # armed with no keys: switched off, then set
                    raise Coded("KEYFRAMED", f"{where} is keyframed; a static value would be hidden by the keyframes.")
                value = to_param_value(prm["value"], where)
                problem = value_problem(cur["value"], value)
                if problem:
                    raise Coded("BAD_PARAMS", f"{where}: {problem}")
                jobs.append({"param": cur, "where": where, "value": value})

    keyframes = [{"matchName": j["matchName"], "occurrence": j["occurrence"], "index": j["index"],
                  "timeBase": j["timeBase"], "mediaSeconds": [secs for secs, _ in j["keys"]]}
                 for j in jobs if "keys" in j]
    interp_jobs = [j for j in jobs if j.get("interp")]

    def interpolation(read_back):
        out = []
        for j in interp_jobs:
            row = {"matchName": j["matchName"], "occurrence": j["occurrence"], "index": j["index"],
                   "mode": j["interp"][0], "name": j["interp"][1]}
            if read_back:
                row["keyframeCount"] = len(j["param"]["keyframes"])
                row["samples"] = curve_samples(j["param"])
            out.append(row)
        return out

    def reply(r, read_back):
        r = dict(r, interpolation=interpolation(read_back)) if interp_jobs else r
        if layer_wait:
            r = dict(r, layerWait=layer_wait, warning=f"{here} is a graphic without layer components after 2 s: the "
                                                       "effects were appended.")
        return r

    if p.get("dryRun"):  # validated like a real run; the effects to create were never appended
        return reply({"dryRun": True, "set": [j["where"] for j in jobs], "createdEffects": created,
                      "createdComponents": created_components, "keyframes": keyframes, "undoLabel": label}, False)
    if created:
        transact(label + " [add effects]")
        comps[at_index:at_index] = new_comps
    if jobs:
        transact(label)
        for job in jobs:
            param = job["param"]
            mode = job["interp"][0] if job.get("interp") else None
            if "keys" in job:
                by_tick = {}
                for secs, value in job["keys"]:
                    by_tick[ticks(secs)] = {"t": secs, "value": value}  # same tick: the later one wins
                    if mode is not None:
                        by_tick[ticks(secs)]["mode"] = mode
                param["keyframed"] = True
                param["keyframes"] = [by_tick[k] for k in sorted(by_tick)]
            elif mode is not None:
                for kf in param["keyframes"]:
                    kf["mode"] = mode
            else:
                param["value"] = job["value"]
                param["keyframed"] = False  # an armed param (animation on, no keys) is switched off first
        if any("keys" in j and j.get("interp") for j in jobs):  # set on the new keys once they exist: one more step
            transact(label + " [interpolation]")
        scale = motion_scale(clip) if kind == "video" else None
        if scale and is_num(start_value(scale)):
            clip["scale"] = start_value(scale)
    return reply({"set": [j["where"] for j in jobs], "createdEffects": created,
                  "createdComponents": created_components, "keyframes": keyframes, "undoLabel": label}, True)


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
    neighbour = next((o for o in track("video", vt) if o is not clip
                      and abs((o["end"] if at_start else o["start"]) - cut) <= EPS), None)
    if (single and not STATE.get("transitionsCentreAtNeighbours")) or not neighbour:
        start, end = (cut, cut + dur) if at_start else (cut - dur, cut)
    else:
        start, end = cut - dur / 2, cut + dur / 2
        if STATE.get("transitionsCentreAtNeighbours"):
            # 27.1, live 6 Nov 2026: centred even when asked single-sided; each half stays only as far as a clip has
            # media to cover it: the half before the cut needs the incoming clip's media before its in point, the half
            # after it the outgoing clip's media after its out point (whole frames; none when a length is unknown)
            incoming, outgoing = (clip, neighbour) if at_start else (neighbour, clip)
            length = media_duration(outgoing)
            tail = length - outgoing["outPoint"] if is_num(length) else 0.0
            frames = lambda sec: math.floor(max(sec, 0.0) * FPS + 1e-6) / FPS  # noqa: E731
            start, end = cut - min(dur / 2, frames(incoming["inPoint"])), cut + min(dur / 2, frames(tail))
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


# ---------------------------------------------------------------- removals (the panel's handlers_removals)

REMOVE_INTRINSIC = ("AE.ADBE Motion", "AE.ADBE Opacity", "AE.ADBE Time Remapping")  # + an audio clip's "Internal ..."
NUDGE_TICKS = 254016  # 1 us: the slack at either end of a remove_keyframes range


def _clip_view(clip):
    return {"name": clip["name"], "start": clip["start"], "end": clip["end"]}


def _refuse(out, dry, reason, data=None):
    """NOT_FOUND about what is on the clip: a dry run reports it (wouldRefuse), a real run refuses."""
    if dry:
        return dict(out, wouldRefuse="NOT_FOUND", reason=reason)
    raise Coded("NOT_FOUND", reason, data=data)


def _target(p, action):
    """(kind, index, clip): the one clip under atSeconds on videoTrack or audioTrack (exactly one of them)."""
    has_v, has_a = is_num(p.get("videoTrack")), is_num(p.get("audioTrack"))
    if has_v == has_a:
        raise Coded("BAD_PARAMS", f"{action} needs exactly one of videoTrack or audioTrack.")
    kind = "audio" if has_a else "video"
    idx = p["audioTrack"] if has_a else p["videoTrack"]
    return kind, idx, item_at(kind, idx, p["atSeconds"])


def h_remove_transition(p):
    """The transition at that edit point goes, whichever side of the cut it lies on (as the mocks model Premiere's
    documented call); the track's transition count before and after is the read-back, as in the panel."""
    require_sequence()
    vt, at, edge = p["videoTrack"], p["atSeconds"], p["edge"]
    clip = clip_at(vt, at)
    dry = bool(p.get("dryRun"))
    cut = clip["start"] if edge == "start" else clip["end"]
    touching = [o for o in track("video", vt)
                if o is not clip and abs((o["end"] if edge == "start" else o["start"]) - cut) <= EPS]
    neighbour = touching[0] if len(touching) == 1 else None
    on_track = lambda: sum(t["videoTrack"] == vt for t in transitions())  # noqa: E731
    before = on_track()
    out = {"dryRun": dry, "track": label_of("video", vt), "edge": edge, "cut": cut, "clip": _clip_view(clip),
           "neighbour": _clip_view(neighbour) if neighbour else None, "removed": False, "via": None,
           "transitionCount": {"before": before}}
    if before == 0:
        return _refuse(out, dry, f"V{vt + 1} has no transitions; nothing was changed.")
    if dry:
        return out
    label = p.get("undoLabel") or f"Agent: remove the transition at the {edge} of V{vt + 1} @{js_num(at)}s"
    transact(label)
    hit = [t for t in transitions() if t["videoTrack"] == vt and abs(t["cut"] - cut) < EPS]
    if hit:
        STATE["transitions"] = [t for t in transitions() if not any(t is h for h in hit)]
    elif neighbour:
        transact(label + " [neighbour]")  # the panel asks the neighbour's opposite edge too; nothing there either
    after = on_track()
    if not hit:
        raise Coded("NOT_FOUND", f"No transition at the {edge} of {clip['name']} ({round(cut, 6)}s) on V{vt + 1}: the "
                                 f"track still has {after} transition(s). Nothing was removed.", outcome="not_committed",
                    data={"transitionCount": {"before": before, "after": after}})
    return dict(out, removed=True, via="clip", transitionCount={"before": before, "after": after}, undoLabel=label)


def h_remove_effect(p):
    """One added component (by match name and occurrence) leaves the clip's chain; built-in ones are refused."""
    require_text(p, ["matchName"])
    require_sequence()
    kind, idx, clip = _target(p, "remove_effect")
    dry = bool(p.get("dryRun"))
    m, occ, tl = p["matchName"], p.get("occurrence") or 1, label_of(kind, idx)
    where = f"{tl} @{js_num(p['atSeconds'])}s ({clip['name']})"
    if m in REMOVE_INTRINSIC or m.startswith("Internal "):
        raise Coded("UNSUPPORTED", f"{m} is built into every clip and cannot be removed; set its params instead "
                                   "(set_params). Nothing was changed.")
    comps = chain(clip, kind)
    names = [c["matchName"] for c in comps]
    out = {"dryRun": dry, "track": tl, "clip": _clip_view(clip), "matchName": m, "occurrence": occ,
           "chainBefore": names}
    idxs = [i for i, n in enumerate(names) if n == m]
    if occ > len(idxs):
        have = f"only {len(idxs)} {m}" if idxs else f"no {m}"
        return _refuse(out, dry, f"{where} has {have} (its chain: {', '.join(names)}); nothing was changed.",
                       {"chain": names})
    index = idxs[occ - 1]
    out.update(index=index, displayName=comps[index]["displayName"])
    if dry:
        return out
    label = p.get("undoLabel") or f"Agent: remove {m}{f' [{occ}]' if occ > 1 else ''} from {tl} @{js_num(p['atSeconds'])}s"
    transact(label)
    del comps[index]
    out.update(chain=[c["matchName"] for c in comps], verified=True, undoLabel=label)
    return out


def h_remove_keyframes(p):
    """Keyframes of one param go: all (animation off; the value the param had before it was keyframed stays, like
    the mocks; keepValue sets the value at the clip's middle) or those in a range (time base converted like
    set_params, unbounded, 1 us of slack either end)."""
    require_text(p, ["matchName"])
    dry, every = bool(p.get("dryRun")), p.get("all") is True
    ranged = is_num(p.get("fromSeconds")) and is_num(p.get("toSeconds"))
    if every == ranged:
        raise Coded("BAD_PARAMS", "remove_keyframes needs all: true, or fromSeconds / toSeconds with a timeBase — "
                                  "not both.")
    if ranged and not isinstance(p.get("timeBase"), str):
        raise Coded("BAD_PARAMS", "remove_keyframes: fromSeconds / toSeconds need a timeBase (media, clip or sequence).")
    if ranged and p["toSeconds"] < p["fromSeconds"]:
        raise Coded("BAD_PARAMS", f"remove_keyframes: toSeconds ({js_num(p['toSeconds'])}) is before fromSeconds "
                                  f"({js_num(p['fromSeconds'])}).")
    require_sequence()
    kind, idx, clip = _target(p, "remove_keyframes")
    m, occ, pi, tl = p["matchName"], p.get("occurrence") or 1, p["paramIndex"], label_of(kind, idx)
    where = f"{m}[{occ}]#{pi}"
    comps = chain(clip, kind)
    out = {"dryRun": dry, "track": tl, "clip": _clip_view(clip), "matchName": m, "occurrence": occ, "index": pi,
           "mode": "all" if every else "range", "removed": [], "remaining": [], "animationOff": False}
    hits = [c for c in comps if c["matchName"] == m]
    if occ > len(hits):
        have = f"only {len(hits)} {m}" if hits else f"no {m}"
        return _refuse(out, dry, f"{tl} @{js_num(p['atSeconds'])}s ({clip['name']}) has {have} (its chain: "
                                 f"{', '.join(c['matchName'] for c in comps)}); nothing was changed.")
    comp = hits[occ - 1]
    if pi >= len(comp["params"]):
        return _refuse(out, dry, f"{m} has no param #{pi} (it has {len(comp['params'])}); nothing was changed.")
    param = comp["params"][pi]
    out["name"] = param["name"]
    lo = hi = None
    if ranged:
        a = media_seconds(clip, 1 / FPS, p["timeBase"], p["fromSeconds"], bounded=False)
        b = media_seconds(clip, 1 / FPS, p["timeBase"], p["toSeconds"], bounded=False)
        lo, hi = ticks(a) - NUDGE_TICKS, ticks(b) + NUDGE_TICKS
        out["range"] = {"timeBase": p["timeBase"], "from": p["fromSeconds"], "to": p["toSeconds"],
                        "fromMediaSeconds": a, "toMediaSeconds": b}
    keys = sorted(param.get("keyframes") or [], key=lambda k: k["t"]) if param.get("keyframed") else []
    if not keys:
        return _refuse(out, dry, f"{where} {param['name']} has no keyframes; nothing was changed.")
    doomed = [k for k in keys if lo <= ticks(k["t"]) <= hi] if ranged else keys
    if not doomed:
        return _refuse(out, dry, f"{where} {param['name']} has no keyframe between media "
                                 f"{round(out['range']['fromMediaSeconds'], 6)}s and "
                                 f"{round(out['range']['toMediaSeconds'], 6)}s (its keyframes: "
                                 f"{', '.join(js_num(round(k['t'], 6)) for k in keys)}s); nothing was changed.")
    off = len(doomed) == len(keys)
    kept = [k for k in keys if not any(k is d for d in doomed)]
    out.update(removed=[k["t"] for k in doomed], remaining=[k["t"] for k in kept], animationOff=off)
    mid = (clip["inPoint"] + clip["outPoint"]) / 2
    keep = None
    if off and p.get("keepValue"):
        keep = copy.deepcopy(value_at(param, mid))
        out["keptValue"] = copy.deepcopy(keep)
    elif p.get("keepValue"):
        out["keptValue"] = None
    if dry:
        return out
    label = p.get("undoLabel") or (f"Agent: remove {'all keyframes' if off else f'{len(doomed)} keyframe(s)'} of "
                                   f"{where} on {tl} @{js_num(p['atSeconds'])}s")
    transact(label)
    if off:
        param["keyframed"], param["keyframes"] = False, []
        if keep is not None:
            param["value"] = keep
        out["valueAfter"] = copy.deepcopy(value_at(param, mid))
    else:
        param["keyframes"] = kept
    scale = motion_scale(clip) if kind == "video" else None
    if scale and is_num(start_value(scale)):
        clip["scale"] = start_value(scale)
    out.update(remaining=[k["t"] for k in param["keyframes"]], verified=True, undoLabel=label)
    return out


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


def _same_path(a, b) -> bool:
    norm = lambda p: str(p or "").replace("\\", "/").lower()  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def h_subclip(p):
    """The panel's subclip on the flat item list: the new item keeps its source's media, frame rate and media length
    and gets the range (clipIn / clipOut, used when it is placed whole). Like Premiere (the mocks' assumption) it is
    made next to its source; for another `bin` it is then moved there (a second step), a missing top-level bin
    coming with the first. A name already in the target bin is refused."""
    require_text(p, ["itemName", "name"])
    if p["outSeconds"] <= p["inSeconds"]:
        raise Coded("BAD_PARAMS", "outSeconds must be greater than inSeconds.")
    take_video, take_audio = p.get("takeVideo") is not False, p.get("takeAudio") is not False
    if not take_video and not take_audio:
        raise Coded("BAD_PARAMS", "takeVideo and takeAudio cannot both be false.")
    require_project()
    src = resolve(p["itemName"], p.get("binPath"))
    fps = src.get("fps") or FPS
    if p["outSeconds"] > src["duration"] + 0.5 / fps:
        raise Coded("BAD_PARAMS", f'outSeconds {js_num(p["outSeconds"])} is past the end of "{item_ref(src)}" '
                                  f'({src["duration"]:.3f}s). Nothing was changed.')
    target = str(p["bin"]).strip("/") if isinstance(p.get("bin"), str) else src["bin"]
    bins = sorted({b for i in STATE["items"] for b in _bin_chain(i["bin"])})
    creates = bool(target) and target not in bins
    if creates and "/" in target:
        raise Coded("NOT_FOUND", f'No bin "{target}" (only a missing top-level bin is created). Bins: '
                                 f'{" | ".join(bins) or "none"}')
    clash = [item_ref(i) for i in STATE["items"] if i["bin"] == target and i["name"] == p["name"]]
    if clash and not p.get("allowDuplicateName"):
        raise Coded("PRECONDITION_FAILED", f'"{p["name"]}" already exists in '
                    + (f'bin "{target}"' if target else "the root bin")
                    + ": a second item of that name would make later commands ambiguous. Nothing was changed.",
                    data={"existing": clash})
    label = p.get("undoLabel") or f'Agent: subclip "{p["name"]}" of {item_ref(src)}'
    out = {"dryRun": bool(p.get("dryRun")), "created": False, "source": item_ref(src), "name": p["name"],
           "binPath": target, "inSeconds": p["inSeconds"], "outSeconds": p["outSeconds"], "takeVideo": take_video,
           "takeAudio": take_audio, "hardBoundaries": True, "createsBin": creates,
           "sameNameElsewhere": [item_ref(i) for i in STATE["items"] if i["bin"] != target and i["name"] == p["name"]],
           "frameRate": src.get("fps") or FPS, "mediaSeconds": src["duration"]}
    if p.get("dryRun"):
        return out
    transact(label)
    new = {"name": p["name"], "bin": src["bin"], "mediaPath": src["mediaPath"], "hasTranscript": False,
           "duration": src["duration"], "clipIn": p["inSeconds"], "clipOut": p["outSeconds"]}
    if src.get("fps"):
        new["fps"] = src["fps"]
    STATE["items"].append(new)
    steps = [label]
    out.update(moved=False, undoLabel=label, undoSteps=steps, newItems=[item_ref(new)])
    if target != src["bin"]:
        try:
            transact(f"{label} [move to bin]")
        except Coded as e:
            raise Coded(e.code, f"[move to bin] {e}; the subclip was made in "
                        + (f'bin "{src["bin"]}"' if src["bin"] else "the root bin") + f" ({label})",
                        outcome="partial", data={"completed": [label], "item": item_ref(new)}) from None
        new["bin"] = target
        steps.append(f"{label} [move to bin]")
        out.update(moved=True, newItems=[item_ref(new)])
    out.update(created=True, item=item_ref(new), binPath=target,
               readBack={"inSeconds": p["inSeconds"], "outSeconds": p["outSeconds"]})
    return out


def h_relink(p):
    """The panel's relink: the item (any second item of that name is ambiguous) gets the new media path, and so do
    its clips on the timeline (they show their project item's file). uses counts them; sameMedia lists other items
    on the old file, which keep it here (whether subclips follow in Premiere is not measured)."""
    require_text(p, ["itemName", "newPath"])
    require_project()
    item = resolve(p["itemName"], p.get("binPath"), unique=True)
    if running_job(item):
        raise Coded("TRANSCRIBE_RUNNING", f"Speech-to-Text is running on {item_ref(item)}; wait for it.")
    old = item["mediaPath"]
    mine = [it for kind in ("video", "audio") for t in STATE[kind] for it in t
            if it["name"] == item["name"] and describe(it)["mediaPath"] == old]
    counts = {k: sum(1 for t in STATE[k] for it in t if any(it is m for m in mine)) for k in ("video", "audio")}
    uses = {"clips": len(mine), "sequences": [{"name": STATE["sequence"]["name"], **counts}] if mine and STATE.get(
        "sequence") else [], "complete": True}
    others = [i for i in STATE["items"] if i is not item and _same_path(i["mediaPath"], old)]
    out = {"dryRun": bool(p.get("dryRun")), "relinked": False, "item": item_ref(item), "previousPath": old,
           "newPath": p["newPath"], "current": {"mediaPath": old, "offline": False,
                                                "frameRate": item.get("fps") or FPS,
                                                "mediaSeconds": item["duration"]},
           "uses": uses, "sameMedia": [{"item": item_ref(i)} for i in others]}
    if p.get("dryRun"):
        return out
    if _same_path(old, p["newPath"]):
        return dict(out, alreadyLinked=True, mediaPath=old)
    side_effect()  # changeMediaFilePath is not a transaction, but it changes the project
    item["mediaPath"] = p["newPath"]
    for it in mine:
        it["mediaPath"] = p["newPath"]
    out.update(relinked=True, returned=True, refreshed=True, mediaPath=p["newPath"], offline=False,
               sameMedia=[{"item": item_ref(i), "mediaPathAfter": i["mediaPath"]} for i in others])
    return out


def h_add_marker(p):
    require_text(p, ["name"])
    require_sequence()
    kind = p.get("type") or "Comment"
    dur = p["durationSeconds"] if is_num(p.get("durationSeconds")) else 0
    label = p.get("undoLabel") or f'Agent: marker "{p["name"]}" @{js_num(p["atSeconds"])}s'
    transact(label)
    marker = dict(copy.deepcopy(p), type=kind, durationSeconds=dur, guid=_next_guid("fake-marker"))
    STATE["markers"].append(marker)
    color_set = False
    if is_num(p.get("colorIndex")):
        transact(label + " [color]")
        color_set = True
    return {"added": True, "name": p["name"], "atSeconds": p["atSeconds"], "durationSeconds": dur, "type": kind,
            "colorSet": color_set, "undoLabel": label}


def _next_guid(prefix: str) -> str:
    """A guid no marker or sequence of the fake project has yet ("fake-marker-2", ...)."""
    used = {m.get("guid") for m in STATE["markers"]} | {s.get("guid") for s in _sequences()}
    n = 1
    while f"{prefix}-{n}" in used:
        n += 1
    return f"{prefix}-{n}"


def _marker_view(m):
    """The panel's describeMarker: start on the frame grid (add_marker snaps it), colour index 0 unless set."""
    start = snap(m.get("atSeconds") or 0.0)
    dur = m["durationSeconds"] if is_num(m.get("durationSeconds")) else 0
    return {"guid": m.get("guid"), "name": m["name"], "comments": m.get("comments") or "", "start": start,
            "duration": dur, "end": start + dur, "type": m.get("type") or "Comment",
            "colorIndex": m["colorIndex"] if is_num(m.get("colorIndex")) else 0}


def h_list_markers(p):
    _, seq = require_sequence()
    markers = sorted((_marker_view(m) for m in STATE["markers"]), key=lambda d: d["start"])
    return {"sequence": seq["name"], "count": len(markers), "markers": markers}


def h_remove_marker(p):
    """The panel's remove_marker: filter by guid, name, start (half a frame); exactly one must match."""
    _, seq = require_sequence()
    views = sorted(((m, _marker_view(m)) for m in STATE["markers"]), key=lambda v: v[1]["start"])
    hits = views
    if p.get("guid"):
        hits = [h for h in hits if h[1]["guid"] == p["guid"]]
    if isinstance(p.get("name"), str):
        hits = [h for h in hits if h[1]["name"] == p["name"]]
    if is_num(p.get("atSeconds")):
        hits = [h for h in hits if abs(h[1]["start"] - p["atSeconds"]) <= 0.5 / FPS]

    def brief(d):
        return {"guid": d["guid"], "name": d["name"], "start": d["start"]}

    if not hits:
        raise Coded("NOT_FOUND", f'No such marker on "{seq["name"]}" ({len(views)} marker(s)).',
                    data={"markers": [brief(d) for _, d in views[:50]]})
    if len(hits) > 1:
        raise Coded("AMBIGUOUS", f"{len(hits)} markers match: at " + ", ".join(f"{round(d['start'], 3)}s" for _, d in hits)
                    + ". Pass atSeconds or guid; nothing was removed.", data={"matches": [brief(d) for _, d in hits]})
    marker, target = hits[0]
    if p.get("dryRun"):
        return {"dryRun": True, "removed": [target], "markerCount": {"before": len(views)}}
    label = p.get("undoLabel") or f'Agent: remove marker "{target["name"]}" @{js_num(round(target["start"], 3))}s'
    transact(label)
    STATE["markers"] = [m for m in STATE["markers"] if m is not marker]
    return {"dryRun": False, "removed": [target], "markerCount": {"before": len(views), "after": len(STATE["markers"])},
            "verified": True, "undoLabel": label}


RESTORE_KEY = "AgentBridge.restorePoint"
PRUNE_NOTE = ("Project#deleteSequence is a direct call, not a labelled undo step: pruned restore points may not come "
              "back with Edit > Undo.")


def _sequences():
    """The active sequence, then the others (restore points the fake made), as the panel's getSequences lists them."""
    seq = STATE.get("sequence")
    return ([seq] if seq else []) + STATE.setdefault("otherSequences", [])


def _restore_tag(s):
    """The panel's restoreTag: the JSON record when it names this very sequence's guid, else None."""
    try:
        tag = json.loads((s.get("props") or {}).get(RESTORE_KEY) or "null")
    except ValueError:
        return None
    if not isinstance(tag, dict) or tag.get("guid") != s.get("guid") or not isinstance(tag.get("sourceGuid"), str):
        return None
    return {"sourceGuid": tag["sourceGuid"], "sourceName": tag.get("sourceName"), "created": tag.get("created") or "",
            "renamed": tag.get("name") != s["name"]}


def h_list_sequences(p):
    require_project()
    active = STATE.get("sequence")
    return {"activeSequence": active["name"] if active else None, "count": len(_sequences()),
            "sequences": [{"name": s["name"], "guid": s.get("guid"), "active": s is active,
                           "restorePoint": _restore_tag(s)} for s in _sequences()]}


def h_clone_sequence(p):
    """The panel's clone_sequence: clone (named "<name> Copy" here; "<name> 副本" on a Chinese Premiere), rename and tag
    in two transactions, then delete the oldest untouched restore points of that source beyond keep."""
    require_project()
    before, active = _sequences(), STATE.get("sequence")
    if p.get("guid"):
        source = next((s for s in before if s.get("guid") == p["guid"]), None)
    elif p.get("name"):
        named = [s for s in before if s["name"] == p["name"]]
        if len(named) > 1:
            raise Coded("AMBIGUOUS", f'{len(named)} sequences are named "{p["name"]}": pass guid.')
        source = named[0] if named else None
    else:
        if active is None:
            raise Coded("NO_SEQUENCE", "Project has no active sequence: name the one to clone.")
        source = active
    if source is None:
        what = f'with guid {p["guid"]}' if p.get("guid") else f'"{p.get("name")}"'
        raise Coded("NOT_FOUND", f"No sequence {what} in this project.", data={"sequences": [s["name"] for s in before]})
    keep = p["keep"] if is_num(p.get("keep")) else 5
    points = [(s, t) for s in before for t in [_restore_tag(s)]
              if t and t["sourceGuid"] == source["guid"] and not t["renamed"] and s is not active]
    points.sort(key=lambda st: st[1]["created"])
    doomed = points[:max(0, len(points) + 1 - keep)]
    prune = [{"name": s["name"], "guid": s["guid"], "created": t["created"]} for s, t in doomed]
    now = time.localtime()
    minute, second = time.strftime("%Y-%m-%d %H:%M", now), time.strftime("%Y-%m-%d %H:%M:%S", now)
    taken = {s["name"] for s in before}
    clone_name = f'{source["name"]} [restore {minute}]'
    if clone_name in taken:
        clone_name = f'{source["name"]} [restore {second}]'
    n = 2
    while clone_name in taken:
        clone_name, n = f'{source["name"]} [restore {second} #{n}]', n + 1
    label = p.get("undoLabel") or f'Agent: restore point of "{source["name"]}"'
    steps = [f"{label} [1/2 clone]", f"{label} [2/2 name + tag]"]
    out = {"dryRun": bool(p.get("dryRun")), "source": {"name": source["name"], "guid": source["guid"]},
           "cloneName": clone_name, "keep": keep, "prune": prune, "undoSteps": steps}
    if p.get("dryRun"):
        if prune:
            out["notes"] = PRUNE_NOTE
        return out
    transact(steps[0])
    clone = {"name": f'{source["name"]} Copy', "guid": _next_guid("fake-sequence"),
             "props": dict(source.get("props") or {})}  # a copy keeps the source's custom properties (assumed)
    STATE["otherSequences"].append(clone)
    transact(steps[1])
    premiere_name, clone["name"] = clone["name"], clone_name
    created = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    clone["props"][RESTORE_KEY] = json.dumps({"v": 1, "guid": clone["guid"], "name": clone_name, "created": created,
                                              "sourceGuid": source["guid"], "sourceName": source["name"]})
    out.update(clone={"name": clone_name, "guid": clone["guid"], "premiereName": premiere_name}, verified=True,
               undoLabel=label, pruned=[])
    if doomed:
        out["notes"] = PRUNE_NOTE
        for s, t in doomed:
            side_effect()
            STATE["otherSequences"] = [x for x in STATE["otherSequences"] if x is not s]
            out["pruned"].append({"name": s["name"], "guid": s["guid"], "created": t["created"]})
    return out


def h_save_project(p):
    """Project#save: touches the project file when it exists (a test can point STATE["project"]["path"] at one)."""
    proj = require_project()
    side_effect()
    if any(s and s in "Project#save" for s in FAIL_TRANSACTIONS):
        raise Coded("TRANSACTION_FAILED", f'Project#save returned false for "{project_name()}".')
    path = proj.get("path")
    if path and os.path.isfile(path):
        os.utime(path, None)
    return {"saved": True, "name": project_name(), "path": path}


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


def _bin_chain(path: str) -> list[str]:
    """"A/B" -> ["A", "A/B"]: the bins an item's bin path implies."""
    parts = [x for x in path.split("/") if x]
    return ["/".join(parts[:i + 1]) for i in range(len(parts))]


def all_bins() -> set:
    """Every bin path: those the items' bin paths imply, and the empty bins apply_ops created (STATE["bins"])."""
    return {b for i in STATE["items"] for b in _bin_chain(i["bin"])} |         {b for path in STATE.get("bins") or [] for b in _bin_chain(path)}


def h_remove_project_items(p):
    """The panel's remove_project_items on the flat item list: bins are the items' bin paths; an item is in use when
    a timeline clip has its name and media path (the panel compares ProjectItem ids). No sequence items here."""
    require_project()
    items = STATE["items"]
    bins = sorted(all_bins())
    target_bins, target_items = [], []
    for raw in p.get("bins") or []:
        path = str(raw).strip("/")
        if not path:
            raise Coded("BAD_PARAMS", "The root bin cannot be removed: name a bin inside it.")
        if path not in bins:
            raise Coded("NOT_FOUND", f'No bin "{path}". Bins: {" | ".join(bins) or "none"}')
        target_bins.append(path)
    for ref in p.get("items") or []:
        hits = [i for i in items if i["name"] == ref["name"] and ("binPath" not in ref or i["bin"] == ref["binPath"])]
        if len(hits) > 1:
            raise Coded("AMBIGUOUS", f'{len(hits)} project items are named "{ref["name"]}" '
                                     f'({", ".join(item_ref(h) for h in hits)}). Name the one to remove with its bin path.')
        if not hits:
            raise Coded("NOT_FOUND", f'No clip named "{ref["name"]}".')
        target_items.append(hits[0])
    inside = lambda b, path: path == b or path.startswith(b + "/")
    gone_bins = [b for b in bins if any(inside(t, b) for t in target_bins)]
    gone = [i for i in items if any(inside(t, i["bin"]) for t in target_bins) or any(i is t for t in target_items)]
    listing = [{"ref": b, "kind": "bin", "mediaPath": None} for b in gone_bins] + \
        [{"ref": item_ref(i), "kind": "clip", "mediaPath": i["mediaPath"]} for i in gone]
    in_use = []
    for i in gone:
        for kind in ("video", "audio"):
            for idx, t in enumerate(STATE[kind]):
                for it in t:
                    d = describe(it)
                    if d["projectItem"] == i["name"] and d["mediaPath"] == i["mediaPath"]:
                        in_use.append({"item": item_ref(i), "sequence": STATE["sequence"]["name"],
                                       "track": label_of(kind, idx), "start": it["start"], "end": it["end"]})
    out = {"dryRun": bool(p.get("dryRun")), "items": listing, "inUse": in_use, "removed": [], "remaining": []}
    if p.get("dryRun"):
        if in_use:
            out["wouldRefuse"] = "IN_USE"
        return out
    if in_use:
        raise Coded("IN_USE", "Still used in a sequence: " + "; ".join(
            f'{u["item"]} on {u["sequence"]} {u["track"]} {js_num(u["start"])}s' for u in in_use[:6]) +
            ". Premiere would take those clips out of the sequence too; nothing was removed.")
    label = p.get("undoLabel") or "Agent: remove " + ", ".join(
        [b for b in target_bins] + [item_ref(i) for i in target_items]) + " from the project"
    transact(label)
    STATE["items"] = [i for i in items if not any(i is g for g in gone)]
    STATE["bins"] = [b for b in STATE.get("bins") or [] if b not in gone_bins]
    out["removed"] = [x["ref"] for x in listing]
    out["undoLabel"] = label
    return out


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
    if "moveTo" in fields:  # B1, like the panel: no move past or onto another clip of the track (half a frame)
        half, lo = 0.5 / FPS, min(before["start"], p["moveTo"])
        hi = max(before["end"], p["moveTo"] + before["end"] - before["start"])
        in_way = [describe(o) for o in track(kind, idx) if o is not clip and o["start"] < hi - half
                  and o["end"] > lo + half]
        if in_way:
            raise Coded("PRECONDITION_FAILED", f"Moving \"{before['name']}\" on {tl} to {p['moveTo']:.3f}s would pass "
                                               "over or onto " + ", ".join(f"\"{d['name']}\" ({d['start']:.3f}-"
                                                                         f"{d['end']:.3f}s)" for d in in_way)
                        + ": Premiere 27.1 renders a clip moved past another silent or blank (B1). Nothing was moved.",
                        data={"inWay": [{k: d[k] for k in ("name", "start", "end")} for d in in_way]})
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
    shift needs the room, per track (the panel's precise rule, feature shiftItemsPrecise): a clip that stays is in the
    way only when it ends after where that track's first moving clip lands -> PRECONDITION_FAILED; a track with
    nothing to move is left alone. Moves run left shift earliest first, right shift latest first; each lands on the
    frame grid like moveTo."""
    require_sequence()
    frm, delta = p["fromSeconds"], p["deltaSeconds"]
    half = 0.5 / FPS
    picks = []
    for kind in ("video", "audio"):
        asked = p.get("videoTracks" if kind == "video" else "audioTracks")
        picks += [(kind, i) for i in (asked if isinstance(asked, list) else range(len(STATE[kind])))]
    moves, blocking = [], []
    for kind, idx in picks:
        tl = label_of(kind, idx)
        clips = sorted(track(kind, idx), key=lambda x: x["start"])
        mine = [it for it in clips if it["start"] >= frm - half]
        if not mine:
            continue  # nothing moves here, so nothing here can be in the way
        lands = min(it["start"] for it in mine) + delta
        blocking += [{"track": tl, "name": it["name"], "start": it["start"], "end": it["end"], "firstLandsAt": lands}
                     for it in clips if delta < 0 and it["start"] < frm - half and it["end"] > lands + half]
        moves += [(kind, idx, it) for it in mine]
    if blocking:
        raise Coded("PRECONDITION_FAILED", f"In the way of a {js_num(delta)}s shift from {js_num(frm)}s: " + "; ".join(
            f'{b["track"]} "{b["name"]}" ({b["start"]:.3f}–{b["end"]:.3f}s) ends after {b["firstLandsAt"]:.3f}s, '
            "where that track's first moving clip lands" for b in blocking) + ". Nothing was moved.",
            data={"blocking": blocking})
    if not moves:
        return {"shifted": [], "deltaSeconds": delta, "undoLabel": None}
    moves.sort(key=lambda m: m[2]["start"], reverse=delta > 0)  # stable: ties keep track order, like the panel
    kind, idx, first = moves[0] if delta < 0 else moves[-1]
    if first["start"] + delta < -EPS:
        raise Coded("PRECONDITION_FAILED", f'{label_of(kind, idx)}: "{first["name"]}" would start at '
                                           f"{first['start'] + delta:.3f}s, before 0s.")
    label = p.get("undoLabel") or f"Agent: shift {len(moves)} clip(s) from {js_num(frm)}s by {js_num(delta)}s"
    transact(label, len(moves))
    shifted = []
    for kind, idx, it in moves:
        shifted.append({"track": label_of(kind, idx), "name": it["name"], "from": it["start"], "to": it["start"] + delta})
        apply_trim(it, "moveTo", it["start"] + delta)
    for kind, idx in dict.fromkeys((k, i) for k, i, _ in moves):
        STATE[kind][idx] = sorted(STATE[kind][idx], key=lambda x: x["start"])
        if kind == "video" and STATE.get("shiftKeepsTransitions"):
            shift_transitions(idx, frm - half, delta)
        elif kind == "video":
            # B2, live on 27.1 (5 Nov 2026): createMoveAction moves the clips, not their transitions; the OTIO export
            # afterwards shows none on the moved clips (a ghost may still play at the old time). So a transition that
            # lay on a moved clip goes (shiftKeepsTransitions in STATE: one that follows its clips, for the tests).
            was = [(it["start"] - delta, it["end"] - delta) for k, i, it in moves if (k, i) == (kind, idx)]
            STATE["transitions"] = [t for t in transitions() if not (t["videoTrack"] == idx and any(
                t["start"] < e - EPS and t["end"] > b + EPS for b, e in was))]
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


def _export_view(job):
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    end = job["finishedAt"] or now_ms()
    out["elapsedSeconds"] = round((end - job["startedAt"]) / 1000, 1)
    if job.get("kind") == "aaf" and job["state"] == "running" and (STATE.get("aafDialog") or
                                                                   end - job["startedAt"] >= AAF_HINT_MS):
        out["hint"] = AAF_HINT
    return copy.deepcopy(out)


def _export_event(job, kind, sample):
    """One encoder event as the panel records it: counted on both listener routes, keys of the first of its kind."""
    job["events"][kind] = job["events"].get(kind, 0) + 1
    for route in ("EventManager(encoder)", "encoder"):
        job["routes"][route] = job["routes"].get(route, 0) + 1
    job["eventKeys"].setdefault(kind, ["type", "target", "timeStamp"] + list(sample))
    job["eventSample"].setdefault(kind, dict(sample, type=kind))
    job["lastEventAt"] = now_ms()


def _end_export(job, state, error=None, end_event=None):
    """The promise settles the job (the panel's rule: events only say why it failed)."""
    if end_event:
        job["endEvent"] = dict(end_event, at=now_ms())
    job.update(state=state, settledBy="promise", error=error, finishedAt=now_ms(), pending=False,
               returned=state == "done")


def _advance_exports():
    """One export_status poll: running jobs count it; after PPRO_FAKE_EXPORT_POLLS polls they end as
    PPRO_FAKE_EXPORT_RESULT says (progress events on the way, except silent; stall never ends)."""
    need = int(os.environ.get("PPRO_FAKE_EXPORT_POLLS", "1"))
    for job in STATE.setdefault("exports", []):
        if job.get("kind") == "aaf":
            _advance_aaf(job, need)
            continue
        if job["state"] != "running" or job["_result"] == "stall":
            continue
        job["_polls"] += 1
        if job["_result"] != "silent":
            job["progress"] = round(min(1.0, job["_polls"] / max(need, 1)), 4)
            job["progressRaw"] = round(job["progress"] * 100, 2)
            _export_event(job, "progress", {"progress": job["progressRaw"]})
        if job["_polls"] < need:
            continue
        result = job["_result"]
        why = f"exportSequence returned false (preset {job['preset']})"
        if result == "fail":
            _export_event(job, "error", {"message": "Encoding failed (fake panel)"})
            _end_export(job, "failed", f"{why}; Premiere: Encoding failed (fake panel)",
                        {"type": "error", "message": "Encoding failed (fake panel)"})
        elif result == "cancel":
            _export_event(job, "cancel", {})
            _end_export(job, "cancelled", f"The render was cancelled in Premiere ({why}).",
                        {"type": "cancel", "message": None})
        elif result == "silent":
            _end_export(job, "done")
        else:
            _export_event(job, "complete", {})
            _end_export(job, "done", end_event={"type": "complete", "message": None})


def h_export_sequence(p):
    """Writes a silent 48 kHz stereo 16-bit WAV (the live render's format) as long as the sequence - or
    PPRO_FAKE_EXPORT_SECONDS - to outputPath whatever the preset or workAreaOnly: not a render. Fails like Premiere
    for a missing output folder (exportSequence returns false) and for a preset path containing /no/such/
    (exportSequence throws "Unable to initialize export!"). wait false: the file is written at once and an export
    job reports on it (export_status; PPRO_FAKE_EXPORT_POLLS / PPRO_FAKE_EXPORT_RESULT); one render at a time."""
    require_text(p, ["outputPath", "presetPath"])
    require_sequence()
    _refuse_busy()
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
    if p.get("wait") is not False:
        return {"exported": True, "outputPath": p["outputPath"], "preset": preset}
    jobs = STATE["exports"]
    events = ("RenderProgressEvent", "RenderCompleteEvent", "RenderErrorEvent", "RenderCancelEvent", "RenderQueueEvent")
    listeners = [f"{route} {name}" for name in events for route in ("EventManager(encoder)", "encoder")]
    listeners += [f"{route} OperationCompleteEvent.ExportMediaComplete" for route in ("EventManager(project)", "project")]
    job = {"jobId": f"export-{len(jobs) + 1}", "kind": "render", "state": "running", "progress": None,
           "progressRaw": None, "outputPath": p["outputPath"], "preset": preset, "workAreaOnly": bool(p.get("workAreaOnly")),
           "sequence": STATE["sequence"]["name"], "startedAt": now_ms(), "finishedAt": None, "lastEventAt": None,
           "endEvent": None, "error": None, "settledBy": None, "returned": None, "pending": True, "events": {}, "routes": {},
           "eventKeys": {}, "eventSample": {}, "listeners": listeners, "listenerErrors": [],
           "_polls": 0, "_result": os.environ.get("PPRO_FAKE_EXPORT_RESULT") or "done"}
    jobs.append(job)
    if int(os.environ.get("PPRO_FAKE_EXPORT_POLLS", "1")) <= 0:
        _advance_exports()  # ends within the panel's early window
        if job["state"] in ("failed", "cancelled"):  # failed at once: no job, as with wait
            jobs.remove(job)
            raise Coded("EXPORT_FAILED", job["error"])
    return {"exported": job["state"] == "done", "outputPath": p["outputPath"], "preset": preset,
            "jobId": job["jobId"], "started": True, "state": job["state"]}


AAF_HINT_MS = 3000  # the panel's: an AAF job running this long may be held by the modal export log
AAF_HINT = ('Premiere may be showing its "AAF export log" window (an effect or transition that cannot go into the AAF): '
            "exportAAF does not return until someone clicks OK there. Ask the user to click OK in Premiere.")
AAF_OPTIONS = ("embedAudio", "audioFormat", "sampleRate", "bitsPerSample", "handleFrames", "mixdownVideo",
               "renderAudioEffects", "trimSources")  # the panel's order (AAF_OPTIONS in handlers_read.js)
AAF_DEFAULTS = {"audioFileFormat": 1, "bitsPerSample": 16, "embedAudio": False, "explodeToMono": False,
                "handleFrames": 0, "interleaveWithoutEffects": False, "mixdownVideo": False,
                "preserveParentFolder": False, "renderAudioEffects": False, "sampleRate": 48000, "trimSources": False,
                "videoMixdownPresetPath": ""}  # the harness mock's (not measured)
AAF_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")  # a Structured Storage file, as an AAF is


def _refuse_busy():
    """The panel's refuseBusy: one export at a time."""
    busy = next((j for j in STATE.setdefault("exports", []) if j["state"] == "running"), None)
    if busy:
        aaf = busy.get("kind") == "aaf"
        raise Coded("EXPORT_RUNNING", f"Export job {busy['jobId']} is still "
                                      f"{'exporting an AAF to' if aaf else 'rendering'} {busy['outputPath']} "
                                      "(running): wait for it (export_status), or Reload the panel if Premiere is no "
                                      "longer exporting." + (" " + AAF_HINT if aaf else ""),
                    data={"jobId": busy["jobId"], "outputPath": busy["outputPath"], "state": busy["state"],
                          "kind": busy.get("kind", "render")})


def _write_aaf(path):
    path.write_bytes(AAF_MAGIC + b"\0" * 504 + b"fake AAF of " + STATE["sequence"]["name"].encode("utf-8"))


def _advance_aaf(job, need):
    """One export_status poll of an AAF job: held while STATE["aafDialog"] is up, else it ends after `need` polls as
    PPRO_FAKE_AAF_RESULT says (the file is written when exportAAF returns, never while the log is up)."""
    if job["state"] != "running" or STATE.get("aafDialog"):
        return
    job["_polls"] += 1
    if job["_polls"] < need:
        return
    result = job["_result"]
    if result == "fail":
        job.update(state="failed", settledBy="promise", error="exportAAF returned false", finishedAt=now_ms(),
                   pending=False, returned=False)
        return
    if result != "nofile":
        try:
            _write_aaf(Path(job["outputPath"]))
        except OSError as e:
            job.update(state="failed", settledBy="promise", error=f"exportAAF returned false ({e})",
                       finishedAt=now_ms(), pending=False, returned=False)
            return
    job.update(state="done", settledBy="promise", finishedAt=now_ms(), pending=False, returned=True)


def h_export_aaf(p):
    """An export job like export_sequence wait false, with no encoder events and no preset: the requested options are
    "set" (optionsSet, in the panel's order; optionValues as the harness mock reads them back). A missing output
    folder is exportAAF returning false at once (EXPORT_FAILED, no job). The .aaf is a small placeholder written when
    the job ends (PPRO_FAKE_EXPORT_POLLS / PPRO_FAKE_AAF_RESULT); STATE["aafDialog"] holds it running meanwhile."""
    require_text(p, ["outputPath"])
    require_sequence()
    _refuse_busy()
    out = Path(p["outputPath"])
    asked = [k for k in AAF_OPTIONS if p.get(k) is not None]
    values = dict(AAF_DEFAULTS)
    for k in asked:
        key = "audioFileFormat" if k == "audioFormat" else k
        values[key] = (0 if p[k] == "aiff" else 1) if k == "audioFormat" else p[k]
    if not out.parent.is_dir():
        raise Coded("EXPORT_FAILED", "exportAAF returned false")
    jobs = STATE.setdefault("exports", [])
    job = {"jobId": f"export-{len(jobs) + 1}", "kind": "aaf", "state": "running", "progress": None,
           "progressRaw": None, "outputPath": p["outputPath"], "preset": None, "workAreaOnly": False,
           "sequence": STATE["sequence"]["name"], "startedAt": now_ms(), "finishedAt": None, "lastEventAt": None,
           "endEvent": None, "error": None, "settledBy": None, "returned": None, "pending": True, "events": {},
           "routes": {}, "eventKeys": {}, "eventSample": {}, "listeners": [], "listenerErrors": [],
           "options": {"set": asked, "values": values},
           "_polls": 0, "_result": os.environ.get("PPRO_FAKE_AAF_RESULT") or "done"}
    jobs.append(job)
    if int(os.environ.get("PPRO_FAKE_EXPORT_POLLS", "1")) <= 0:
        _advance_aaf(job, 0)  # ends within the panel's early window (unless the log is up)
        if job["state"] == "failed":
            jobs.remove(job)
            raise Coded("EXPORT_FAILED", job["error"])
    return {"exported": job["state"] == "done", "outputPath": p["outputPath"], "jobId": job["jobId"],
            "started": True, "state": job["state"], "optionsSet": asked, "optionValues": values}


def h_export_xml(p):
    """A small FCP 7 XML document (xmeml version 4, as Premiere writes) at outputPath; a missing folder is
    exportAsFinalCutProXML returning false. Refused while an AAF job runs, like the panel."""
    require_text(p, ["outputPath"])
    require_sequence()
    if any(j.get("kind") == "aaf" and j["state"] == "running" for j in STATE.setdefault("exports", [])):
        _refuse_busy()
    out = Path(p["outputPath"])
    if not out.parent.is_dir():
        raise Coded("EXPORT_FAILED", "exportAsFinalCutProXML returned false")
    name = STATE["sequence"]["name"].replace("&", "&amp;").replace("<", "&lt;")
    out.write_bytes(('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">\n'
                     f'\t<sequence id="sequence-1">\n\t\t<name>{name}</name>\n\t</sequence>\n</xmeml>\n')
                    .encode("utf-8"))
    return {"exported": True, "outputPath": p["outputPath"]}


def h_export_status(p):
    """The job table; like the panel, no Premiere call and no guard check (it touches no project or sequence)."""
    _advance_exports()
    jobs = STATE.setdefault("exports", [])
    if isinstance(p.get("jobId"), str):
        job = next((j for j in jobs if j["jobId"] == p["jobId"]), None)
        if job is None:
            ids = [j["jobId"] for j in jobs]
            raise Coded("NOT_FOUND", f"No export job {p['jobId']} in this panel "
                                     f"({'jobs: ' + ', '.join(ids) if ids else 'none'}); a panel reload forgets its "
                                     "jobs.", data={"jobs": ids})
        return {"jobs": [_export_view(job)]}
    return {"jobs": [_export_view(j) for j in jobs]}



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


# ---------------------------------------------------------------- list_effects / add_audio_effect

def _norm_name(s) -> str:
    return re.sub(r"\s+", " ", str(s).lower()).strip()


def _bigrams(s):
    t = _norm_name(s).replace(" ", "")
    if len(t) < 2:
        return [t] if t else []
    return [t[i:i + 2] for i in range(len(t) - 1)]


def similarity(a, b) -> float:
    """The panel's similarity (handlers_export.js): 1 for the same name, 0.9 when one contains the other, else the
    bigram Dice coefficient."""
    x, y = _norm_name(a), _norm_name(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if x in y or y in x:
        return 0.9
    bx, pool = _bigrams(x), _bigrams(y)
    total, common = len(bx) + len(pool), 0
    for g in bx:
        if g in pool:
            common += 1
            pool.remove(g)
    return 2 * common / total if total else 0.0


def close_names(query, names):
    scored = [(-similarity(query, n), i, n) for i, n in enumerate(names)]
    return [n for s, _, n in sorted(scored) if -s >= 0.34][:8]


def h_list_effects(p):
    """VideoFilterFactory's filters (match + display name, paired) and AudioFilterFactory's display names."""
    check_guard_only()
    kinds = [p["kind"]] if p.get("kind") else ["video", "audio"]
    out, notes = {"effects": [], "counts": {}}, []
    if "video" in kinds:
        out["effects"] += [{"kind": "video", "matchName": m, "displayName": d} for m, (d, _) in FILTERS.items()]
        out["counts"]["video"] = len(FILTERS)
    if "audio" in kinds:
        out["effects"] += [{"kind": "audio", "matchName": None, "displayName": n} for n in AUDIO_FILTERS]
        out["counts"]["audio"] = len(AUDIO_FILTERS)
        notes.append("Audio effects are listed by display name only, in Premiere's UI language: add one with "
                     "add_audio_effect by that name; its match name shows in inspect_items once it is on a clip.")
    if notes:
        out["notes"] = notes
    return out


def _audio_filter(display):
    match_name, params, animated = AUDIO_FILTERS[display]
    return {"matchName": match_name, "displayName": display,
            "params": [{"name": n, "value": copy.deepcopy(v), "keyframed": animated, "keyframes": []} for n, v in params]}


def h_add_audio_effect(p):
    """The panel's add_audio_effect: resolve the name (exact display name, then ignoring case, then a match name),
    then append the effect to the audio clip under atSeconds in one transaction and read the chain back."""
    require_text(p, ["name"])
    require_sequence()
    idx, at, query = p["audioTrack"], p["atSeconds"], p["name"]
    label_ = label_of("audio", idx)
    clip = item_at("audio", idx, at)
    names = list(AUDIO_FILTERS)
    if query in names:
        display, how = query, "displayName"
    else:
        loose = [n for n in names if _norm_name(n) == _norm_name(query)]
        if len(loose) > 1:
            raise Coded("AMBIGUOUS", f'"{query}" matches {len(loose)} audio effects when case is ignored: '
                                     f'{", ".join(loose)}. Give the exact name; nothing was changed.',
                        data={"matches": loose})
        display, how = (loose[0], "displayNameIgnoringCase") if loose else (None, None)
        if display is None and MATCH_NAME.match(query):
            display = next((n for n in names if _norm_name(AUDIO_FILTERS[n][0]) == _norm_name(query)), None)
            how = "matchName"
        if display is None:
            close = close_names(query, names)
            raise Coded("NOT_FOUND", f'No audio effect "{query}" in Premiere\'s list ({len(names)} names, in the UI '
                                     "language)" + (f"; close: {', '.join(close)}" if close else "") +
                        ". Nothing was changed.", data={"closeNames": close, "count": len(names)})
    comps = chain(clip, "audio")
    before = [c["matchName"] for c in comps]
    label = p.get("undoLabel") or f"Agent: add {display} on {label_} @{js_num(at)}s"
    out = {"dryRun": bool(p.get("dryRun")), "track": label_,
           "clip": {"name": clip["name"], "start": clip["start"], "end": clip["end"]},
           "displayName": display, "matchedBy": how, "matchName": AUDIO_FILTERS[display][0], "before": before}
    if p.get("dryRun"):
        return out
    transact(label)
    comps.append(_audio_filter(display))
    after = inspect_chain(clip, 5, "audio")
    added = after[-1] if len(after) == len(before) + 1 else None
    out.update(after=[c["matchName"] for c in after], verified=bool(added and added["displayName"] == display),
               undoLabel=label)
    if added:
        out["component"] = added
    return out


# ---------------------------------------------------------------- apply_ops / query (the whitelisted generic layer)

# The panel's plugin/lib/handlers_ops.js on the fake project: each op is planned (target resolved and checked, nothing
# changed), all are planned before one is applied, then the transaction applies them in order (one undo label), a
# direct op (the playhead) runs after it, and every op is read back. OP_FAULTS makes an op's Action fail to build
# (nothing is committed) or commit without effect (VERIFY_FAILED).
OPS_MAX = 200
OPS_UNSET = None  # an unset sequence in / out point (the panel reports Premiere's -400000 s as null)
OPS_VALUE_TOL = 1e-4
TRACK_LABEL = re.compile(r"([VvAa])([1-9][0-9]*)")
PENDING_AUDIO = "verified live on video only (api_smoke, 27.1 b21); audio is pending a live test"
MARKER_SETTERS = {  # op -> (field in _marker_view, its param, the key in a STATE marker)
    "marker.set_name": ("name", "newName", "name"),
    "marker.set_comment": ("comments", "comments", "comments"),
    "marker.set_color": ("colorIndex", "colorIndex", "colorIndex"),
    "marker.set_duration": ("duration", "durationSeconds", "durationSeconds"),
    "marker.set_type": ("type", "type", "type"),
}


def _ops_name(value, param):
    if not isinstance(value, str) or value == "":
        raise Coded("BAD_PARAMS", f"{param} must not be empty")
    return value


def parse_track_label(text):
    """"V2" -> ("video", 1, "V2"); anything else is BAD_PARAMS (the panel's parseTrack)."""
    m = TRACK_LABEL.fullmatch(str(text))
    if not m:
        raise Coded("BAD_PARAMS", f'track "{text}" is not a track label (V1, V2, ... or A1, A2, ...)')
    kind = "video" if m.group(1).upper() == "V" else "audio"
    idx = int(m.group(2)) - 1
    return kind, idx, label_of(kind, idx)


def _near(a, b, tol):
    return is_num(a) and is_num(b) and abs(a - b) <= tol


def _ops_markers(env):
    if "markers" not in env:
        env["markers"] = sorted(((m, _marker_view(m)) for m in STATE["markers"]), key=lambda v: v[1]["start"])
    return env["markers"]


def _brief_marker(d):
    return {"guid": d["guid"], "name": d["name"], "start": d["start"]}


def _marker_key(d):
    return f"marker:{d['guid'] or str(d['name']) + '@' + str(d['start'])}"


def _marker_target(env, op):
    views = _ops_markers(env)
    hits = views
    if isinstance(op.get("guid"), str) and op["guid"]:
        hits = [h for h in hits if h[1]["guid"] == op["guid"]]
    if isinstance(op.get("name"), str):
        hits = [h for h in hits if h[1]["name"] == op["name"]]
    if is_num(op.get("atSeconds")):
        hits = [h for h in hits if abs(h[1]["start"] - op["atSeconds"]) <= env["half"]]
    what = ", ".join(x for x in (f"guid {op['guid']}" if op.get("guid") else None,
                                 f'named "{op["name"]}"' if isinstance(op.get("name"), str) else None,
                                 f"at {js_num(op['atSeconds'])}s" if is_num(op.get("atSeconds")) else None) if x)
    if not hits:
        raise Coded("NOT_FOUND", f"No marker {what} ({len(views)} marker(s)).",
                    data={"markers": [_brief_marker(d) for _, d in views[:50]]})
    if len(hits) > 1:
        raise Coded("AMBIGUOUS", f"{len(hits)} markers match ({what}): at "
                    + ", ".join(f"{round(d['start'], 3)}s" for _, d in hits) + ". Pass atSeconds or guid.",
                    data={"matches": [_brief_marker(d) for _, d in hits]})
    return hits[0]


def _marker_now(m):
    """The marker after the commit (the fake's markers are dicts that stay in STATE until removed)."""
    return _marker_view(m) if any(x is m for x in STATE["markers"]) else None


def _op_marker_add(env, op):
    _ops_name(op["name"], "name")
    start = snap(op["atSeconds"])
    dur = op["durationSeconds"] if is_num(op.get("durationSeconds")) else 0
    kind, comments = op.get("type") or "Comment", op.get("comments") or ""
    holder = {}

    def apply():
        holder["m"] = {"name": op["name"], "atSeconds": start, "durationSeconds": dur, "type": kind,
                       "comments": comments, "guid": _next_guid("fake-marker")}
        STATE["markers"].append(holder["m"])

    def read_back():
        d = _marker_now(holder["m"]) if holder.get("m") else None
        if d is None:
            return {"found": False}, False
        return d, _near(d["duration"], dur, env["half"]) and d["type"] == kind and d["comments"] == comments

    return {"target": {"marker": "new", "name": op["name"], "start": start},
            "keys": [(f"marker:new:{op['name']}@{start}", "add")],
            "after": {"name": op["name"], "start": start, "duration": dur, "type": kind, "comments": comments},
            "apply": apply, "read_back": read_back}


def _op_marker_remove(env, op):
    m, d = _marker_target(env, op)

    def apply():
        STATE["markers"] = [x for x in STATE["markers"] if x is not m]

    def read_back():
        gone = _marker_now(m) is None
        return {"removed": gone, "markerCount": len(STATE["markers"])}, gone

    return {"target": _brief_marker(d), "keys": [(_marker_key(d), "*")], "before": d, "after": {"removed": True},
            "apply": apply, "read_back": read_back}


def _op_marker_setter(name):
    field, param, key = MARKER_SETTERS[name]

    def plan(env, op):
        m, d = _marker_target(env, op)
        want = op[param]
        if field == "name":
            _ops_name(want, "newName")

        def apply():
            m[key] = want

        def read_back():
            now = _marker_now(m)
            if now is None:
                return {"found": False}, False
            got = now[field]
            return {field: got}, (_near(got, want, env["half"]) if field == "duration" else got == want)

        return {"target": _brief_marker(d), "keys": [(_marker_key(d), field)], "before": {field: d[field]},
                "after": {field: want}, "apply": apply, "read_back": read_back}

    return plan


def _op_marker_move(env, op):
    m, d = _marker_target(env, op)
    to = snap(op["toSeconds"])

    def apply():
        m["atSeconds"] = to

    def read_back():
        now = _marker_now(m)
        return ({"start": now["start"]}, _near(now["start"], to, env["half"])) if now else ({"found": False}, False)

    return {"target": _brief_marker(d), "keys": [(_marker_key(d), "start")], "before": {"start": d["start"]},
            "after": {"start": to}, "apply": apply, "read_back": read_back}


def _ops_clip(op, video_only, name):
    kind, idx, label = parse_track_label(op["track"])
    if video_only and kind != "video":
        raise Coded("UNSUPPORTED", f"{name} is {PENDING_AUDIO}; {label} is an audio track.")
    clip = item_at(kind, idx, op["atSeconds"])
    d = describe(clip)
    return {"kind": kind, "idx": idx, "label": label, "clip": clip, "d": d, "key": f"clip:{label}@{d['start']}",
            "target": {"track": label, "name": d["name"], "start": d["start"], "end": d["end"]}}


def _op_clip_set_name(env, op):
    c = _ops_clip(op, True, "clip.set_name")
    _ops_name(op["name"], "name")

    def apply():
        c["clip"]["clipName"] = op["name"]

    def read_back():
        got = describe(item_at(c["kind"], c["idx"], op["atSeconds"]))["name"]
        return {"name": got}, got == op["name"]

    return {"target": c["target"], "keys": [(c["key"], "name")], "before": {"name": c["d"]["name"]},
            "after": {"name": op["name"]}, "apply": apply, "read_back": read_back}


def _op_clip_set_disabled(env, op):
    c = _ops_clip(op, True, "clip.set_disabled")
    if capabilities().get("trackItemDisable") is False:
        raise Coded("UNSUPPORTED", "clip.set_disabled needs the trackItemDisable capability "
                                   "(VideoClipTrackItem#createSetDisabledAction), which this Premiere build lacks.")

    def apply():
        c["clip"]["disabled"] = op["disabled"]

    def read_back():
        got = describe(item_at(c["kind"], c["idx"], op["atSeconds"]))["disabled"]
        return {"disabled": got}, got == op["disabled"]

    return {"target": c["target"], "keys": [(c["key"], "disabled")], "before": {"disabled": c["d"]["disabled"]},
            "after": {"disabled": op["disabled"]}, "apply": apply, "read_back": read_back}


def _op_value(v, where):
    """param.set's value -> (kind, plain): a number, a boolean or a point; anything else is BAD_PARAMS."""
    if is_num(v):
        return "number", v
    if isinstance(v, bool):
        return "boolean", v
    if isinstance(v, list) and len(v) == 2 and all(is_num(x) for x in v):
        return "point", {"x": v[0], "y": v[1]}
    if isinstance(v, dict) and len(v) == 2 and is_num(v.get("x")) and is_num(v.get("y")):
        return "point", {"x": v["x"], "y": v["y"]}
    raise Coded("BAD_PARAMS", f"{where}: value {json.dumps(v)} is not a number, a boolean or a point ([x, y] or "
                              "{x, y}); strings and colours are not ops (unverified over UXP)")


def _kind_of(v):
    if isinstance(v, bool):
        return "boolean"
    if is_num(v):
        return "number"
    if _is_point(v):
        return "point"
    return None


def _same_value(got, kind, want):
    if kind == "number":
        return is_num(got) and abs(got - want) <= OPS_VALUE_TOL * max(1, abs(want))
    if kind == "boolean":
        return got is want
    return _is_point(got) and abs(got["x"] - want["x"]) <= OPS_VALUE_TOL and abs(got["y"] - want["y"]) <= OPS_VALUE_TOL


def _ops_param(c, op, here):
    comps = chain(c["clip"], c["kind"])
    names = [x["matchName"] for x in comps]
    occ = op.get("occurrence") or 1
    found = [x for x in comps if x["matchName"] == op["matchName"]]
    if occ > len(found):
        raise Coded("NOT_FOUND", f"{c['label']} @{js_num(op['atSeconds'])}s has {len(found)} {op['matchName']} "
                                 f"component(s) (chain: {', '.join(names)}); param.set writes components the clip "
                                 "already has (add effects with set_params or add_audio_effect).",
                    data={"chain": names})
    params = found[occ - 1]["params"]
    if op["index"] >= len(params):
        raise Coded("NOT_FOUND", f"{here}: {op['matchName']}[{occ}] has no param #{op['index']} (it has {len(params)}).")
    return params[op["index"]]


def _op_param_set(env, op):
    c = _ops_clip(op, False, "param.set")
    occ = op.get("occurrence") or 1
    here = f"{c['label']} @{js_num(op['atSeconds'])}s {op['matchName']}[{occ}]#{op['index']}"
    kind, want = _op_value(op["value"], here)
    prm = _ops_param(c, op, here)
    if prm.get("keyframed") and prm.get("keyframes"):
        raise Coded("KEYFRAMED", f"{here} is keyframed; a static value would be hidden by its keyframes "
                                 "(remove_keyframes takes them off).")
    current = start_value(prm)
    have = _kind_of(current)
    if have and have != kind:
        raise Coded("BAD_PARAMS", f"{here}: the param holds a {have}, not a {kind} ({json.dumps(op['value'])})")
    animated = bool(prm.get("keyframed"))

    def apply():
        prm["value"] = copy.deepcopy(want)
        prm["keyframed"] = False  # animation on with no keys: switched off in the same transaction
        scale = motion_scale(c["clip"]) if c["kind"] == "video" else None
        if scale is prm and is_num(want):
            c["clip"]["scale"] = want

    def read_back():
        again = _ops_param({"clip": item_at(c["kind"], c["idx"], op["atSeconds"]), "kind": c["kind"],
                            "label": c["label"]}, op, here)
        got, on = copy.deepcopy(start_value(again)), bool(again.get("keyframed"))
        return {"value": got, "animated": on}, not on and _same_value(got, kind, want)

    target = dict(c["target"], matchName=op["matchName"], occurrence=occ, index=op["index"], param=prm["name"])
    return {"target": target, "keys": [(f"{c['key']}:{op['matchName']}[{occ}]#{op['index']}", "value")],
            "before": {"value": copy.deepcopy(current), "animated": animated}, "after": {"value": want},
            "apply": apply, "read_back": read_back}


def _op_set_in_out(env, op):
    seq = STATE["sequence"]
    in_s = snap(op["inSeconds"]) if is_num(op.get("inSeconds")) else None
    out_s = snap(op["outSeconds"]) if is_num(op.get("outSeconds")) else None
    if in_s is not None and out_s is not None and out_s <= in_s:
        raise Coded("BAD_PARAMS", "outSeconds must be after inSeconds (on the frame grid)")
    before = {"inPoint": seq.get("inPoint", OPS_UNSET), "outPoint": seq.get("outPoint", OPS_UNSET)}
    after, keys = {}, []
    if in_s is not None:
        after["inPoint"] = in_s
        keys.append(("sequence", "inPoint"))
    if out_s is not None:
        after["outPoint"] = out_s
        keys.append(("sequence", "outPoint"))

    def apply():
        seq.update(after)

    def read_back():
        got = {"inPoint": seq.get("inPoint", OPS_UNSET), "outPoint": seq.get("outPoint", OPS_UNSET)}
        return got, all(_near(got[k], v, env["half"]) for k, v in after.items())

    return {"target": {"sequence": seq["name"]}, "keys": keys, "before": before, "after": after, "apply": apply,
            "read_back": read_back}


def _op_player_position(env, op):
    seq = STATE["sequence"]
    at = snap(op["atSeconds"])

    def apply():
        seq["playerPosition"] = at

    def read_back():
        got = seq.get("playerPosition", 4.2)
        return {"playerPositionSeconds": got}, _near(got, at, env["half"])

    return {"target": {"sequence": seq["name"]}, "keys": [("sequence", "playerPosition")],
            "before": {"playerPositionSeconds": seq.get("playerPosition", 4.2)}, "after": {"playerPositionSeconds": at},
            "apply": apply, "read_back": read_back}


def _op_track_rename(env, op):
    kind, idx, label = parse_track_label(op["track"])
    if kind != "video":
        raise Coded("UNSUPPORTED", f"track.rename is {PENDING_AUDIO}; {label} is an audio track.")
    _ops_name(op["name"], "name")
    track(kind, idx)  # NOT_FOUND past the last track
    before = track_name(kind, idx)

    def apply():
        STATE.setdefault("trackNames", {})[label] = op["name"]

    def read_back():
        got = track_name(kind, idx)
        return {"name": got}, got == op["name"]

    return {"target": {"track": label}, "keys": [(f"track:{label}", "name")], "before": {"name": before},
            "after": {"name": op["name"]}, "apply": apply, "read_back": read_back}


def _bin_path(p) -> str:
    return "/".join(s for s in str(p or "").split("/") if s)


def _join_bin(a, b) -> str:
    return f"{a}/{b}" if a else b


def _bin_where(path) -> str:
    return f'bin "{path}"' if path else "the root bin"


def _ops_bin_name(name, param):
    _ops_name(name, param)
    if "/" in name:
        raise Coded("BAD_PARAMS", f'{param} "{name}" contains "/", which bin paths use')
    return name


def _ops_require_bin(path):
    at = ""
    for seg in path.split("/") if path else []:
        at = _join_bin(at, seg)
        if at not in all_bins():
            raise Coded("NOT_FOUND", f'No bin "{at}" in the project.')


def _op_bin_create(env, op):
    _ops_bin_name(op["name"], "name")
    parent = _bin_path(op.get("parent"))
    _ops_require_bin(parent)
    path = _join_bin(parent, op["name"])
    if path in all_bins():
        raise Coded("PRECONDITION_FAILED", f'{_bin_where(parent)} already holds a bin named "{op["name"]}".')

    def apply():
        STATE.setdefault("bins", []).append(path)

    def read_back():
        found = path in all_bins()
        return {"path": path, "found": found}, found

    return {"target": {"parent": parent}, "keys": [(f"bin:{path}", "create")], "after": {"path": path},
            "apply": apply, "read_back": read_back}


def _op_bin_rename(env, op):
    path = _bin_path(op["bin"])
    if not path:
        raise Coded("BAD_PARAMS", "bin: the root bin cannot be renamed")
    _ops_bin_name(op["newName"], "newName")
    parent, old = path.rpartition("/")[0], path.rpartition("/")[2]
    _ops_require_bin(path)
    to = _join_bin(parent, op["newName"])
    if op["newName"] != old and to in all_bins():
        raise Coded("PRECONDITION_FAILED", f'{_bin_where(parent)} already holds a bin named "{op["newName"]}".')

    def moved(b):
        return to + b[len(path):] if b == path or b.startswith(path + "/") else b

    def apply():
        for it in STATE["items"]:
            it["bin"] = moved(it["bin"])
        STATE["bins"] = [moved(b) for b in STATE.get("bins") or []]

    def read_back():
        bins = all_bins()
        return {"path": to, "found": to in bins}, to in bins and (to == path or path not in bins)

    return {"target": {"bin": path}, "keys": [(f"bin:{path}", "name"), (f"bin:{to}", "create")],
            "before": {"path": path}, "after": {"path": to}, "apply": apply, "read_back": read_back}


OPS = {  # op -> (needs, plan, direct): the panel's OPS table, in actions.json order
    "marker.add": ("sequence", _op_marker_add, False),
    "marker.remove": ("sequence", _op_marker_remove, False),
    **{name: ("sequence", _op_marker_setter(name), False) for name in MARKER_SETTERS},
    "marker.move": ("sequence", _op_marker_move, False),
    "clip.set_name": ("sequence", _op_clip_set_name, False),
    "clip.set_disabled": ("sequence", _op_clip_set_disabled, False),
    "param.set": ("sequence", _op_param_set, False),
    "sequence.set_in_out": ("sequence", _op_set_in_out, False),
    "sequence.set_player_position": ("sequence", _op_player_position, True),
    "track.rename": ("sequence", _op_track_rename, False),
    "bin.create": ("project", _op_bin_create, False),
    "bin.rename": ("project", _op_bin_rename, False),
}


def _ops_conflicts(planned):
    seen = []
    for p in planned:
        for target, prop in p["keys"]:
            clash = next((s for s in seen if s["target"] == target and (s["prop"] == prop or "*" in (s["prop"], prop))),
                         None)
            if clash:
                how = "touch" if "*" in (clash["prop"], prop) else f"set {prop} of"
                raise Coded("BAD_PARAMS", f"ops[{clash['index']}] ({clash['op']}) and ops[{p['index']}] ({p['op']}) both "
                                          f"{how} {target}; split them into two requests")
        seen += [{"index": p["index"], "op": p["op"], "target": t, "prop": pr} for t, pr in p["keys"]]


def _ops_label(ops_list):
    names = ", ".join(dict.fromkeys(o["op"] for o in ops_list))
    if len(names) > 80:
        names = names[:77] + "..."
    return f"Agent: {len(ops_list)} op{'' if len(ops_list) == 1 else 's'} ({names})"


def h_apply_ops(p):
    ops_list = p["ops"]
    if len(ops_list) > OPS_MAX:
        raise Coded("BAD_PARAMS", f"apply_ops takes at most {OPS_MAX} ops in one request ({len(ops_list)} given); split it.")
    if any(OPS[o["op"]][0] == "sequence" for o in ops_list):
        require_sequence()
    else:
        require_project()
    env = {"half": 0.5 / FPS}
    planned = []
    for i, op in enumerate(ops_list):
        needs, plan, direct = OPS[op["op"]]
        try:
            step = plan(env, op)
        except Coded as e:
            raise Coded(e.code, f"ops[{i}] {op['op']}: {e}", e.outcome, e.data) from e
        planned.append(dict(step, index=i, op=op["op"], direct=direct))
    _ops_conflicts(planned)
    label = p.get("undoLabel") or _ops_label(ops_list)
    tx = [s for s in planned if not s["direct"]]
    direct = [s for s in planned if s["direct"]]

    def result(s):
        r = {"index": s["index"], "op": s["op"], "ok": True, "target": s["target"]}
        if "before" in s:
            r["before"] = copy.deepcopy(s["before"])
        r["after"] = copy.deepcopy(s["after"])
        if s["direct"]:
            r["undoable"] = False
        return r

    out = {"dryRun": bool(p.get("dryRun")), "count": len(ops_list), "results": [result(s) for s in planned],
           "undoLabel": label}
    if direct:
        out["notUndoable"] = [s["op"] for s in direct]
    if p.get("dryRun"):
        return out
    if tx:
        broken = next((s for s in tx if OP_FAULTS.get(s["op"]) == "build"), None)
        if broken:
            raise Coded("INTERNAL", f"ops[{broken['index']}] {broken['op']}: its action could not be built, so nothing "
                                    "was committed: fake build failure (OP_FAULTS)")
        transact(label, actions=len(tx))
        for s in tx:
            if OP_FAULTS.get(s["op"]) != "ignore":
                s["apply"]()
    for s in direct:
        side_effect()
        s["apply"]()
    for r, s in zip(out["results"], planned):
        read_back, verified = s["read_back"]()
        r["readBack"], r["verified"] = copy.deepcopy(read_back), verified is True
    out["verified"] = all(r["verified"] for r in out["results"])
    if not out["verified"]:
        off = ", ".join(f"ops[{r['index']}] {r['op']}" for r in out["results"] if not r["verified"])
        raise Coded("VERIFY_FAILED", f"{off} did not read back as asked (results[].readBack). The ops committed as "
                                     f'"{label}": look before retrying (Edit > Undo takes the step back).',
                    outcome="committed", data=out)
    return out


def _q_clips(p):
    has_at, has_range = is_num(p.get("atSeconds")), is_num(p.get("fromSeconds"))
    if has_at and has_range:
        raise Coded("BAD_PARAMS", "Give atSeconds or fromSeconds + toSeconds, not both.")
    if has_range and p["toSeconds"] < p["fromSeconds"]:
        raise Coded("BAD_PARAMS", "toSeconds must not be before fromSeconds.")
    wanted = [parse_track_label(t) for t in p.get("tracks") or []]
    _, seq = require_sequence()
    if not wanted:
        wanted = [(kind, i, label_of(kind, i)) for kind in ("video", "audio") for i in range(len(STATE[kind]))]
    tracks, done = [], set()
    for kind, idx, label in wanted:
        if label in done:
            continue
        done.add(label)
        clips = []
        for it in sorted(track(kind, idx), key=lambda x: x["start"]):
            if has_at and not overlaps(it, p["atSeconds"], p["atSeconds"]):
                continue
            if has_range and not overlaps(it, p["fromSeconds"], p["toSeconds"]):
                continue
            clips.append(describe(it))
        tracks.append({"label": label, "kind": kind, "index": idx, "name": track_name(kind, idx), "clips": clips})
    return {"what": "clips", "sequence": seq["name"], "tracks": tracks}


def _q_clip(p):
    kind, idx, label = parse_track_label(p["track"])
    _, seq = require_sequence()
    clip = item_at(kind, idx, p["atSeconds"])
    d = describe(clip)
    d["components"] = inspect_chain(clip, p.get("maxKeyframes") or 50, kind)
    return {"what": "clip", "sequence": seq["name"], "track": label, "clip": d}


def _q_markers(p):
    _, seq = require_sequence()
    markers = sorted((_marker_view(m) for m in STATE["markers"]), key=lambda d: d["start"])
    return {"what": "markers", "sequence": seq["name"], "count": len(markers), "markers": markers}


def _q_bins(p):
    require_project()
    root = _bin_path(p.get("bin"))
    bins = all_bins()
    if root and root not in bins:
        raise Coded("NOT_FOUND", f'No bin "{root}" in the project. Bins: ' + (" | ".join(sorted(bins)) or "none"))
    rows = [{"path": b, "kind": "bin", "name": b.rpartition("/")[2], "bin": b.rpartition("/")[0]} for b in bins]
    rows += [{"path": _join_bin(i["bin"], i["name"]), "kind": "clip", "name": i["name"], "bin": i["bin"],
              "mediaPath": i["mediaPath"]} for i in STATE["items"]]
    rows = [r for r in rows if not root or r["path"].startswith(root + "/")]
    rows.sort(key=lambda r: (r["path"].count("/"), r["kind"] != "bin", r["path"]))
    return {"what": "bins", "bin": root, "count": len(rows), "entries": rows}


def _q_sequence(p):
    _, seq = require_sequence()
    end = max([it["end"] for t in STATE["video"] + STATE["audio"] for it in t] or [0])
    return {"what": "sequence", "sequence": seq["name"], "guid": seq.get("guid"),
            "frameSize": {"width": 1080, "height": 1920}, "timebase": TIMEBASE, "fps": round(FPS, 3),
            "durationSeconds": end, "playerPositionSeconds": seq.get("playerPosition", 4.2),
            "inPoint": seq.get("inPoint", OPS_UNSET), "outPoint": seq.get("outPoint", OPS_UNSET),
            "videoTracks": _tracks("video"), "audioTracks": _tracks("audio")}


QUERIES = {"clips": _q_clips, "clip": _q_clip, "markers": _q_markers, "bins": _q_bins, "sequence": _q_sequence}


def h_query(p):
    return QUERIES[p["what"]](p)


HANDLERS = {  # in actions.json order
    "ping": h_ping,
    "get_project": h_get_project,
    "get_sequence": h_get_sequence,
    "set_active_sequence": h_set_active_sequence,
    "select_items": h_select_items,
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
    "remove_transition": h_remove_transition,
    "remove_effect": h_remove_effect,
    "remove_keyframes": h_remove_keyframes,
    "set_fps": h_set_fps,
    "subclip": h_subclip,
    "relink": h_relink,
    "add_marker": h_add_marker,
    "list_markers": h_list_markers,
    "remove_marker": h_remove_marker,
    "list_sequences": h_list_sequences,
    "clone_sequence": h_clone_sequence,
    "save_project": h_save_project,
    "remove_items": h_remove_items,
    "remove_project_items": h_remove_project_items,
    "clone_clip": h_clone_clip,
    "trim_clip": h_trim_clip,
    "export_sequence": h_export_sequence,
    "export_status": h_export_status,
    "export_aaf": h_export_aaf,
    "export_xml": h_export_xml,
    "list_effects": h_list_effects,
    "add_audio_effect": h_add_audio_effect,
    "shift_items": h_shift_items,
    "apply_ops": h_apply_ops,
    "query": h_query,
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
    OP_FAULTS.clear()
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

    if a is not None and a.get("devOnly") and release_build():
        return fail("UNKNOWN_ACTION", f'"{action}" is a test action that only dev builds register; this panel is a '
                                      "release build (load the panel from a dev checkout to run it).")
    if a is None or handler is None:
        return fail("UNKNOWN_ACTION", f'Unknown action "{action}". Whitelist: {", ".join(registered())}')
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
        fp = guard.get("fingerprint")
        if isinstance(fp, str) and fp and not re.fullmatch(r"[0-9a-f]{16}", fp):
            problems.append("guard.fingerprint: expected 16 lowercase hex digits (get_sequence's fingerprint)")
    if problems:
        return fail("BAD_PARAMS", f"Bad params for {action}: " + "; ".join(problems), data={"problems": problems})
    cap = a.get("capability")
    if cap and capabilities().get(cap) is False:
        return fail("UNSUPPORTED", f"{action} needs the {cap} capability, which this Premiere build lacks.")

    CTX = {"action": action, "id": msg_id, "guard": dict(guard or {}), "sideEffects": 0}
    try:
        check_fingerprint_guard(a, params)
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
    """Pair with the broker as the real panel does (ppro_bridge/auth.py "Pairing": the token file, read with
    auth.read_token, is proved and never sent), then answer requests, and only once the broker proved the token
    back. PPRO_FAKE_LEGACY_HELLO=1 says the hello of a 0.6 panel instead (no pairing; for the broker's legacy rule)."""
    import websockets

    legacy = os.environ.get("PPRO_FAKE_LEGACY_HELLO") == "1"
    token, nonce, broker_nonce = auth.read_token(), auth.new_nonce(), None
    proven = legacy  # a legacy hello gets no proof: this fake then trusts the broker, as a 0.6 panel did
    # The broker's limit for a paired client (16 MiB), not websockets' default 1 MiB: a 2 MB transcript import must
    # reach the fake as it reaches the panel (QA-7).
    async with websockets.connect(BROKER_URL, max_size=16 * 1024 * 1024) as ws:
        hello = {"role": "panel", "plugin": PLUGIN_ID, "version": VERSION}
        if not legacy:
            hello.update(auth=auth.SCHEME, nonce=nonce)
        await ws.send(json.dumps(hello))
        print(f"fake panel {VERSION} said hello at {BROKER_URL}", flush=True)
        tasks = set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    print(f"bad JSON from broker: {raw!r}", flush=True)
                    continue
                if isinstance(msg, dict) and msg.get("event") == "refused":
                    print(f"refused by the broker: {msg.get('error')}", flush=True)
                if isinstance(msg, dict) and msg.get("event") == "challenge" and not legacy and broker_nonce is None:
                    broker_nonce = msg.get("nonce") if auth.valid_nonce(msg.get("nonce")) else None
                    if broker_nonce is None or not token:
                        print("not paired: no usable challenge or no token", flush=True)
                        raise SystemExit(1)
                    await ws.send(json.dumps({"proof": auth.proof(token, "client", "panel", nonce, broker_nonce)}))
                    continue
                if isinstance(msg, dict) and msg.get("event") == "registered":
                    if not legacy and not auth.proof_matches(msg.get("proof"), token, "broker", "panel", nonce,
                                                             broker_nonce):
                        print("not paired: the broker did not prove the pairing token", flush=True)
                        raise SystemExit(1)
                    proven = True
                    print("registered by the broker", flush=True)  # tests/helpers.Bridge waits for this line
                if not isinstance(msg, dict) or not msg.get("id") or not msg.get("action"):
                    continue  # the panel drops these too (and shows broker notices: {"event": ...})
                if not proven:
                    print(f"dropped {msg.get('action')}: the broker has not proved the pairing token", flush=True)
                    continue
                if CONCURRENT:  # like an unqueued panel: start each request as it arrives
                    task = asyncio.get_running_loop().create_task(answer(ws, msg))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                else:
                    await answer(ws, msg)
        except websockets.ConnectionClosed as e:
            # 4403 / 4409: the broker refused this panel (another one is live); 4410: a newer panel replaced it
            code = e.rcvd.code if e.rcvd else None
            print(f"closed by the broker ({code}): {e.rcvd.reason if e.rcvd else e}", flush=True)
            raise SystemExit(1 if code in (4403, 4409, 4410) else 0)


if __name__ == "__main__":
    asyncio.run(main())
