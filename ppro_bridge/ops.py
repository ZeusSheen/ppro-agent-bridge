"""One function per CLI command — the surface the CLI (and later the MCP server) calls.

Every function takes the transport as its REQUIRED first positional argument `rpc` (`transport.rpc`,
`transport.make_rpc(...)`, or a test stub) and keyword-only arguments named like the CLI's argparse destinations.
It returns the reply data (a dict) or raises BridgeError (code, outcome, data). Nothing here prints or exits, and
nothing has a default transport: a forgotten injection fails instead of reaching a live Premiere.
"""

import json
import math
import os
import re
import sys
import time
from pathlib import Path

from . import paths, spec
from ._version import VERSION
from .errors import BridgeError


class _Lazy:
    """A module imported on first use, so a command loads only the modules it needs (`ping` used to load every
    submodule: about 150 ms of a CLI call). The first attribute read imports the module and puts the module itself
    in this namespace under `alias`, so from then on call sites (and tests patching e.g. ops.frames.POLL) use the
    module; an attribute set or deleted through the proxy goes to the module."""

    __slots__ = ("_name", "_alias")

    def __init__(self, name: str, alias: str):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_alias", alias)

    def _load(self):
        __import__(self._name)
        module = sys.modules[self._name]
        if globals().get(self._alias) is self:
            globals()[self._alias] = module
        return module

    def __getattr__(self, attr):
        return getattr(self._load(), attr)

    def __setattr__(self, attr, value):
        setattr(self._load(), attr, value)

    def __delattr__(self, attr):
        delattr(self._load(), attr)

    def __repr__(self):
        return f"<module {self._name}, not imported yet>"


shutil, tempfile = (_Lazy(n, n) for n in ("shutil", "tempfile"))
apiprobe, ccx, framegrid, frames, items, media, panelhash, transcript = (
    _Lazy(f"{__package__}.{n}", n)
    for n in ("apiprobe", "ccx", "framegrid", "frames", "items", "media", "panelhash", "transcript"))
_editing, _rendercheck, _review, _plan, _replace, _presets, _prfpset = (
    _Lazy(f"{__package__}.{n}", f"_{n}") for n in ("editing", "rendercheck", "review", "plan", "replace", "presets",
                                                   "prfpset"))

MARKER_COLORS = {"green": 0, "red": 1, "purple": 2, "magenta": 2, "orange": 3,
                 "yellow": 4, "white": 5, "blue": 6, "cyan": 7}
MARKER_COLOR_NAMES = {i: n for n, i in reversed(list(MARKER_COLORS.items()))}  # index -> first name (2: purple)

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"  # this checkout's panel, for the stale check
# its code hash, remembered with the files' sizes and times (panelhash.code_hash): Python's bytecode cache folder of
# this package, and only where Python writes one (not with PYTHONDONTWRITEBYTECODE or a pycache prefix)
CODE_HASH_CACHE = Path(__file__).resolve().parent / "__pycache__" / "panel-codehash.json"


# ---------------------------------------------------------------- helpers

def _bad(message: str, **data) -> BridgeError:
    return BridgeError(message, code="BAD_PARAMS", outcome="not_committed", data=data or None)


def _track(value, want: str | None = None, mismatch: str = "") -> tuple[str, int]:
    try:
        kind, idx = items.parse_track(value)
    except ValueError as e:
        raise _bad(str(e)) from e
    if want and kind != want:
        raise _bad(mismatch or f"Track {value} is not a {want} track.")
    return kind, idx


def _as_list(value) -> list | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, Path)) or not hasattr(value, "__iter__"):
        return [value]
    return list(value)


def _out_path(path) -> Path:
    out = paths.resolved(path)  # after paths.check: no link to a share on the way, a mapped drive keeps its letter
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _json_out(output, what: str = "--output") -> str:
    """A JSON file the CLI writes itself: checked as Premiere's paths are (no share, device, stream or link to one)
    and ending in .json, so a write can never replace a project, a script or a program."""
    return paths.check(output, what=what, ext=".json")


def _in_file(path, what: str) -> Path:
    """A file the CLI reads itself (plan, transcript, SRT, JSON params): checked before Python opens it - a share
    would get the user's credentials from this process as from Premiere."""
    return Path(paths.check(path, what=what))


def _sequence(rpc, expect_seq: str | None) -> dict:
    seq = rpc("get_sequence")
    if expect_seq is not None and seq.get("name") != expect_seq:
        raise BridgeError(f'The active sequence is "{seq.get("name")}", not "{expect_seq}"; nothing was changed.',
                          code="SEQUENCE_MISMATCH", outcome="not_committed")
    return seq


# ---------------------------------------------------------------- reads

def ping(rpc, *, probe=False) -> dict:
    """Liveness + version/protocol handshake; `compatible` and a `warning` say what to reload or restart."""
    data = rpc("ping", {"probe": True} if probe else {})
    data["cliVersion"] = VERSION
    data.setdefault("update", None)  # the broker's update check: {latest, url, published, newer, skipped, checkedAt}
    broker = data.get("brokerVersion")
    data["brokerVersion"] = broker
    protocol = data.get("protocol") or 1
    denied = data.get("denied")
    warnings = []
    if data.get("version") != VERSION:
        warnings.append(f"Panel is {data.get('plugin')}, CLI is {VERSION}: "
                        "reload the Agent Bridge panel in UXP Developer Tool.")
    elif protocol != spec.protocol():
        warnings.append(f"Panel speaks protocol {protocol}, CLI {spec.protocol()}: "
                        "reload the Agent Bridge panel in UXP Developer Tool.")
    if broker != VERSION:
        warnings.append(f"Broker is {broker or 'older than 0.3.3'}, CLI is {VERSION}: "
                        "stop broker.py and start it again.")
    if data.get("stuck"):
        stuck = data["stuck"] if isinstance(data["stuck"], dict) else {}
        warnings.append(f"The panel is stuck on {stuck.get('action')} and refuses everything but ping: "
                        "save the project, then reload the panel in UXP Developer Tool.")
    if denied:
        reason = denied.get("reason") if isinstance(denied, dict) else None
        warnings.append(f"This Premiere build is on the panel's denylist ({reason or 'no reason given'}): "
                        "the panel answers ping only.")
    local = panelhash.code_hash(PLUGIN_DIR, cache=None if sys.dont_write_bytecode or sys.pycache_prefix
                                else CODE_HASH_CACHE)
    if local:
        data["localCodeHash"] = local
        # A bundled panel (scripts/build_panel.py) has no lib/ folder, so its hash covers other files.
        if data.get("codeHash") and data.get("modules") != "bundle":
            data["stale"] = data["codeHash"] != local
            # A panel installed from a .ccx carries a rewritten manifest and build.js (ppro_bridge/ccx.py).
            if data["stale"] and data["codeHash"] in ccx.packaged_code_hashes(PLUGIN_DIR):
                data["stale"] = False
            elif data["stale"] and isinstance(data.get("build"), dict) and data["build"].get("packaged") is True:
                warnings.append("The panel is a .ccx install whose code differs from this checkout's plugin folder: "
                                "build it again (python scripts/build_ccx.py) and reinstall it with Premiere closed.")
            elif data["stale"]:
                warnings.append("The panel is stale — its code differs from this checkout's plugin folder: "
                                "Reload it in UXP Developer Tool.")
    data["compatible"] = (data.get("version") == VERSION and broker == VERSION
                          and protocol == spec.protocol() and not denied)
    if warnings:
        data["warning"] = " ".join(warnings)
    return data


def get_project(rpc) -> dict:
    return rpc("get_project")


def get_sequence(rpc) -> dict:
    """The active sequence, plus `fingerprint`: pass it back as --expect-fingerprint on a write to refuse the write
    if the timeline has changed in between (items.fingerprint)."""
    seq = rpc("get_sequence")
    seq["fingerprint"] = items.fingerprint(rpc("list_items", {"kinds": ["video", "audio"]}), seq)
    return seq


SWITCH_CONFIRM_DELAYS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.5)  # s before each get_project re-read: about 2 s in all


def set_active_sequence(rpc, *, name=None, guid=None, sleep=time.sleep) -> dict:
    """Open another sequence of the active project in the Timeline (by exact name or guid), so later commands work on
    it; the panel reads the switch back. Nothing in the project changes.
    Then get_project must report that sequence active (`confirmed`, `confirmReads`): live on 27.1 (6 Nov 2026) the
    second of two quick switches replied switched while Premiere stayed on the other sequence. It is read again for
    about 2 s (SWITCH_CONFIRM_DELAYS); still another one is VERIFY_FAILED, outcome committed (Premiere may yet
    switch), with what Premiere reports active."""
    if not name and not guid:
        raise _bad("name or guid is required")
    data = rpc("set_active_sequence", {"guid": guid} if guid else {"name": name})
    want = data.get("activeSequence")
    if not want:
        return data
    reads = []
    for delay in (0.0, *SWITCH_CONFIRM_DELAYS):
        if delay:
            sleep(delay)
        reads.append((rpc("get_project") or {}).get("activeSequence"))
        if reads[-1] == want:
            data.update(confirmed=True, confirmReads=len(reads))
            return data
    raise BridgeError(f'set_active_sequence replied that "{want}" is active, but get_project still reports '
                      f'"{reads[-1]}" after {len(reads)} reads over {sum(SWITCH_CONFIRM_DELAYS):.1f} s: Premiere did '
                      "not switch (yet).", code="VERIFY_FAILED", outcome="committed",
                      data={"reply": data, "activeSequence": reads[-1], "reads": reads},
                      hint="Run get_project to see which sequence is active, then set_active_sequence again (or ask "
                           "the user to open it in Premiere) before any write; --expect-seq / a sequence guard "
                           "refuses a write on the wrong one.")


def select_items(rpc, *, track=None, at=None, from_s=None, to_s=None, mode="inside", clear=False, dry_run=False,
                 expect_seq=None) -> dict:
    """Select clips in the Timeline, replacing the selection: on the `track`s ("V1", "A1", …), the clip under `at`, or
    the clips in `from_s`–`to_s` (`mode` inside: entirely inside; overlap: touching it); `clear` empties it. Only those
    clips: Premiere's linked partners (an A1 clip's video) are not added, so name both tracks for both. For what only
    Premiere's own panels do (Properties > Enhance Audio applies to every selected clip). Nothing in the project
    changes and it is not an undo step; the panel reads the selection back (`verified`)."""
    tracks = _as_list(track) or []
    if clear:
        if tracks or at is not None or from_s is not None or to_s is not None:
            raise _bad("--clear empties the selection: give it no --track, --at or --from/--to")
    else:
        if not tracks:
            raise _bad("Give at least one --track (or --clear)")
        if at is not None and (from_s is not None or to_s is not None):
            raise _bad("Give --at or --from/--to, not both")
        if at is None and (from_s is None or to_s is None):
            raise _bad("Give --at, or --from with --to")
        if at is None and to_s < from_s:
            raise _bad(f"--to {to_s} is before --from {from_s}")
    if mode not in RANGE_MODES:
        raise _bad(f'mode must be inside or overlap, not "{mode}"')
    vts, ats = [], []
    for value in tracks:
        kind, idx = _track(value)
        (vts if kind == "video" else ats).append(idx)
    items.require_command(rpc, "select_items")
    items.check_expect_seq(rpc, expect_seq)
    params = {"dryRun": bool(dry_run)}
    if clear:
        params["clear"] = True
    else:
        params.update(videoTracks=vts, audioTracks=ats)
        if at is not None:
            params["atSeconds"] = at
        else:
            params.update(fromSeconds=from_s, toSeconds=to_s, mode=mode)
    return rpc("select_items", params)


def list_items(rpc, *, kind="all", track=None) -> dict:
    if track:
        tkind, idx = _track(track)
        data = rpc("list_items", {"kinds": [tkind]})
        data[tkind] = [t for t in data.get(tkind, []) if t["index"] == idx]
        return data
    if kind not in ("all", "video", "audio"):
        raise _bad(f'kind must be all, video or audio, not "{kind}"')
    return rpc("list_items", {"kinds": ["video", "audio"] if kind == "all" else [kind]})


def inspect_items(rpc, *, track=None, from_s=None, to_s=None, output=None) -> dict:
    params = {}
    tracks = [_track(t) for t in _as_list(track) or []]
    if tracks:  # V… and A… labels; audio clips' chains hold the clip volume ("Internal Volume …", Level)
        vt = [i for k, i in tracks if k == "video"]
        at_ = [i for k, i in tracks if k == "audio"]
        if vt:
            params["videoTracks"] = vt
        if at_:
            params["audioTracks"] = at_
    if from_s is not None:
        params["fromSeconds"] = from_s
    if to_s is not None:
        params["toSeconds"] = to_s
    if output:
        _json_out(output)
    data = rpc("inspect_items", params)
    if output:
        out = _out_path(output)
        out.write_bytes(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))
        tracks_ = data.get("tracks") or []
        return {"savedTo": str(out), "clips": sum(len(t.get("clips") or []) for t in tracks_),
                "transitions": sum(len(t.get("transitions") or []) for t in tracks_)}
    return data


def list_project_items(rpc) -> dict:
    return rpc("list_project_items")


def export_frame(rpc, *, dir, name=None, at=None, width=None, height=None) -> dict:
    paths.check(dir, what="--dir")  # as typed, and every link on the way (a mapped drive is fine)
    out_dir = paths.resolved(dir)
    if name is not None:
        paths.check_name(name, what="--name", ext=".png")  # before any panel call (export_one_frame checks it again)
    if at is not None:
        frames.check_times_inside(rpc, [at])
    out_dir.mkdir(parents=True, exist_ok=True)
    return frames.export_one_frame(rpc, out_dir, at, name, width, height)


def export_frames(rpc, *, dir, at, width=None, height=None, sheet=None, cols=4, thumb_width=300) -> dict:
    """Several frames (`at`: "4,18.5,31.6" or a list of seconds), optionally a contact sheet (Pillow)."""
    try:
        if isinstance(at, str):
            times = [float(t) for t in at.split(",") if t.strip()]
        else:
            times = [float(t) for t in _as_list(at) or []]
    except (TypeError, ValueError) as e:
        raise _bad(f"--at must be comma-separated seconds ({e})") from e
    if not times:
        raise _bad("--at needs at least one time")
    paths.check(dir, what="--dir")
    out_dir = paths.resolved(dir)
    if sheet:
        paths.check(sheet, what="--sheet", ext=(".jpg", ".jpeg", ".png"))
    frames.check_times_inside(rpc, times)
    out_dir.mkdir(parents=True, exist_ok=True)
    shots, results, codes = [], [], []
    for t in times:
        try:
            one, error = frames.export_one_frame(rpc, out_dir, t, None, width, height), None
        except BridgeError as e:
            one, error = {}, str(e)
            codes.append(e.code)
        path = one.get("resolvedPath")
        results.append({"at": t, "path": path, "error": error,
                        **{k: one[k] for k in ("black", "exportSeconds") if k in one}})
        if path:
            shots.append((t, path))
    data = {"frames": results}
    black = [r["at"] for r in results if r.get("black")]
    if black:
        data["warning"] = f"All black at {', '.join(f'{t:g}' for t in black)}s. " + frames.BLACK_WARNING
    if sheet is not None:
        sheet_path = paths.resolved(sheet) if sheet else out_dir / "contact-sheet.jpg"
        made = frames.make_contact_sheet(shots, sheet_path, cols, thumb_width)
        data["sheet"] = made or "skipped (pip install pillow for contact sheets)"
    if any(r["error"] or not r["path"] for r in results):
        raise BridgeError("see data", code=next((c for c in codes if c), "EXPORT_FAILED"),
                          outcome="not_committed", data=data)
    return data


def export_otio(rpc, *, output) -> dict:
    paths.check(output, what="--output", ext=".otio")
    return rpc("export_otio", {"outputPath": str(_out_path(output))})


def probe_api(rpc, *, output=None, diff=None, no_instances=False, max_members=None) -> dict:
    """This Premiere build's UXP API: a summary with the watch list (calls the UXP port depends on); --output saves
    the full dump, --diff compares it with an earlier one (added / removed members)."""
    old = None
    if output:
        _json_out(output)
    if diff:
        p = _in_file(diff, "--diff").expanduser()
        if not p.is_file():
            raise BridgeError(f"File not found: {p.resolve()}", code="FILE_NOT_FOUND", outcome="not_committed")
        try:
            old = json.loads(p.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise _bad(f"{p.name} is not a probe_api dump ({e})") from e
    items.require_command(rpc, "probe_api")
    params = {"instances": not no_instances}
    if max_members:
        params["maxMembers"] = int(max_members)
    dump = rpc("probe_api", params)
    out = apiprobe.summarize(dump)
    if output:
        path = _out_path(output)
        path.write_bytes(json.dumps(dump, ensure_ascii=False, indent=1).encode("utf-8"))
        out["output"] = str(path)
    if old is not None:
        out["diff"] = apiprobe.diff(old, dump)
    return out


def list_transcripts(rpc, *, jobs_only=False) -> dict:
    return rpc("list_transcripts", {"jobsOnly": True} if jobs_only else {})


def list_transition_names(rpc) -> dict:
    return rpc("list_transition_names")


# ---------------------------------------------------------------- transcripts

def _export_transcript(rpc, output, item: dict | None) -> dict:
    params = {"itemName": item["name"], "binPath": item.get("bin") or ""} if item else {}
    data = rpc("export_transcript", params)
    raw = data.pop("transcriptJson", None)
    if raw:
        out = _out_path(output)
        encoded = raw.encode("utf-8")
        out.write_bytes(encoded)
        data["savedTo"] = str(out)
        data["bytes"] = len(encoded)
        data["hint"] = "Run: python transcript_digest.py " + str(out)
    return data


def export_transcript(rpc, *, output, item=None) -> dict:
    """Save a clip's Premiere transcript (`item`: name or "<bin path>/<name>"; default: the first clip with one)."""
    _json_out(output)
    items.require_command(rpc, "export_transcript")
    picked = items.resolve_item(item, rpc=rpc) if item else None
    return _export_transcript(rpc, output, picked)


def import_transcript(rpc, *, item, file, replace=False) -> dict:
    """Attach a transcript file to a clip after checking it locally (TRANSCRIPT_INVALID lists the problems)."""
    text, doc = transcript.read_transcript(file)
    upgraded = transcript.upgrade_legacy_languages(doc)
    if upgraded:  # written by transcribe.py 0.3.x: send the corrected document
        text = json.dumps(doc, ensure_ascii=False)
    problems = transcript.validate(doc)
    if problems:
        raise BridgeError(f"{Path(file).name} fails the transcript format check ({len(problems)} problem(s)): "
                          + "; ".join(problems[:5]), code="TRANSCRIPT_INVALID", outcome="not_committed",
                          data={"problems": problems[:100], "count": len(problems)})
    items.require_command(rpc, "import_transcript")
    picked = items.resolve_item(item, rpc=rpc)
    params = {"itemName": picked["name"], "binPath": picked.get("bin") or "", "transcriptJson": text}
    if replace:
        params["replace"] = True
    result = rpc("import_transcript", params)
    if upgraded:
        result.setdefault("warnings", []).append(
            f"Upgraded language codes written by transcribe.py 0.3.x: {', '.join(upgraded)} (re-run transcribe.py "
            "to get current codes).")
    return result


def transcribe_clip(rpc, *, item, language=None, wait=600.0, output=None, poll=5.0, sleep=time.sleep) -> dict:
    """Premiere Speech-to-Text (26.5+) on one clip: start it, then (unless wait is 0) poll the panel's job table
    every `poll` seconds until the job is done, and save the transcript to `output` when given."""
    wait = float(wait or 0)
    if wait > 0 and float(poll) <= 0:
        raise _bad("poll must be > 0 seconds")
    if output:  # a share, a device or a link to one: refused before Speech-to-Text starts
        _json_out(output)
    items.require_command(rpc, "transcribe_clip")
    picked = items.resolve_item(item, rpc=rpc)
    params = {"itemName": picked["name"], "binPath": picked.get("bin") or ""}
    if language:
        params["language"] = language
    if output:  # before anything starts: a bad output folder must not fail after Speech-to-Text is running
        _json_out(output)
        paths.resolved(output).parent.mkdir(parents=True, exist_ok=True)
    start = rpc("transcribe_clip", params)
    if wait <= 0:
        return start
    try:
        return _await_transcription(rpc, start, picked, wait, output, poll, sleep)
    except BridgeError as e:
        if e.outcome in (None, "not_committed"):  # the job was started: something did happen
            e.outcome = "committed"
        raise
    except OSError as e:
        raise BridgeError(f"Speech-to-Text ran, but saving the transcript failed: {e}", code="IO_ERROR",
                          outcome="committed", data={"jobId": start.get("jobId")}) from e


def _await_transcription(rpc, start, picked, wait, output, poll, sleep) -> dict:
    job_id = start.get("jobId")
    began, slept = time.monotonic(), 0.0
    while True:
        sleep(poll)
        slept += float(poll)
        waited = max(slept, time.monotonic() - began)
        jobs = rpc("list_transcripts", {"jobsOnly": True}).get("jobs") or []
        job = next((j for j in jobs if j.get("jobId") == job_id), None)
        where = {"jobId": job_id, "itemName": picked["name"], "binPath": picked.get("bin") or "",
                 "waitedSeconds": round(waited, 1)}
        if job is None:
            raise BridgeError(f"Speech-to-Text job {job_id} is no longer in the panel's job table (was the panel "
                              "reloaded?). Check list_transcripts for hasTranscript before starting it again.",
                              code="TRANSCRIBE_LOST", outcome="unknown", data=where)
        if job.get("state") == "failed":
            raise BridgeError(f"Speech-to-Text failed on {items.item_ref(picked)}: {job.get('error')}",
                              code="TRANSCRIBE_FAILED", outcome="unknown", data=dict(where, job=job))
        # Only the job's end counts: Premiere may attach an empty placeholder transcript while it runs.
        if job.get("state") == "done":
            break
        if waited >= wait:
            raise BridgeError(f"Speech-to-Text on {items.item_ref(picked)} still running after {waited:.0f}s; it may "
                              "still finish: poll list_transcripts --jobs, then export_transcript.",
                              code="TRANSCRIBE_TIMEOUT", outcome="unknown", data=where)

    result = dict(start)
    result.update(state=job.get("state"), hasTranscript=job.get("hasTranscript"), waitedSeconds=round(waited, 1),
                  job=job)
    if output:
        exported = _export_transcript(rpc, output, picked)
        result.update({k: exported[k] for k in ("savedTo", "bytes", "hint") if k in exported})
    return result


# ---------------------------------------------------------------- writes

def import_media(rpc, *, path, bin=None, force=False, probe_media=None, convert_audio=None) -> dict:
    """Import files into the root or a top-level bin. That import hangs the panel for a codec Premiere refuses, so:
    an audio-only file in one (FLAC, Opus, Vorbis) is converted to a 48 kHz 24-bit WAV beside it — or an earlier
    conversion reused — and the WAV is imported in its place (`converted`); a file with a picture in one (VP9, AV1,
    or Opus audio in a video) is refused first, with an ffmpeg command for each, and nothing is converted or
    imported. `force` sends every file as it is. UNC paths and data streams are refused as typed (paths.check).
    `convert_audio(path, info)` replaces media.convert_to_wav (tests)."""
    given = _as_list(path) or []
    for p in given:
        paths.check(p, what="--path")
    files = [paths.resolved(p) for p in given]
    if not files:
        raise _bad("Give at least one --path")
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise BridgeError("File(s) not found", code="FILE_NOT_FOUND", outcome="not_committed",
                          data={"missing": missing})
    converted = []
    if not force:
        probe = probe_media or media.probe_media
        refused, to_convert = [], []
        for p in files:
            info = probe(str(p)) or {}
            codecs = media.unimportable(info)
            if codecs and media.wav_convertible(info):
                to_convert.append((p, info))
            elif codecs:
                refused.append({"path": str(p), "codecs": codecs, "transcode": media.transcode_command(p, info)})
        if refused:  # before any conversion: a refused call writes nothing
            raise BridgeError(f"Premiere does not import {', '.join(sorted({c for r in refused for c in r['codecs']}))}"
                              " (the import would hang the panel). Nothing was imported.", code="UNSUPPORTED",
                              outcome="not_committed", data={"files": refused},
                              hint="Transcode with the ffmpeg command listed for each file and import the result "
                                   "(or pass --force to send the files anyway).")
        convert = convert_audio or media.convert_to_wav
        failed = []
        for p, info in to_convert:
            done = convert(str(p), info)
            if done.get("error"):
                failed.append({"path": str(p), "codecs": media.unimportable(info), "why": done["error"],
                               "transcode": done.get("command") or media.transcode_command(p, info)})
            else:
                converted.append(done)
        if failed:
            raise BridgeError(f"Premiere does not import {', '.join(sorted({c for f in failed for c in f['codecs']}))}"
                              " (the import would hang the panel), and converting to WAV failed: "
                              + "; ".join(f"{Path(f['path']).name}: {f['why']}" for f in failed)
                              + ". Nothing was imported.", code="UNSUPPORTED", outcome="not_committed",
                              data={"files": failed, "converted": converted},
                              hint="Run the ffmpeg command listed for each file and import the WAV (or pass --force "
                                   "to send the file as it is).")
    swap = {os.path.normcase(c["from"]): c["to"] for c in converted}
    send = []
    for p in files:
        target = swap.get(os.path.normcase(str(p)), str(p))
        if os.path.normcase(target) not in {os.path.normcase(s) for s in send}:  # the WAV given too: import it once
            send.append(target)
    params = {"paths": send}
    if bin:
        params["bin"] = bin
    try:
        reply = rpc("import_media", params)
    except BridgeError as e:
        if converted and (e.data is None or isinstance(e.data, dict)):  # the WAVs stay on disk: say where
            e.data = dict(e.data or {}, converted=converted)
        raise
    if converted:
        reply = dict(reply, converted=converted)
    return reply


def _free_audio_track(rpc, at, end, half) -> int:
    """The highest audio track with nothing between at and end (end None: to the end of the sequence)."""
    tracks = rpc("list_items", {"kinds": ["audio"]}).get("audio", [])
    for t in sorted(tracks, key=lambda t: t["index"], reverse=True):
        if not any(i.get("start") is not None and i.get("end") is not None and i["end"] > at + half
                   and (end is None or i["start"] < end - half) for i in t["items"]):
            return t["index"]
    raise BridgeError(f"--video-only: no audio track is free from {at}s, and the clip's audio has to land somewhere "
                      "before it is taken off. Name one with --atrack, or free a track.",
                      code="PRECONDITION_FAILED", outcome="not_committed")


def insert_clip(rpc, *, item, at=0.0, vtrack=0, atrack=None, mode="overwrite", in_s=None, out_s=None, label=None,
                expect_seq=None, video_only=False) -> dict:
    """Place a project item (or the source range in_s-out_s). video_only: the picture without its sound: the audio leg
    is placed on a free audio track (--atrack names another) and taken off again in one more undo step."""
    if (in_s is None) != (out_s is None):
        raise _bad("--in and --out must be given together.")
    if mode not in ("overwrite", "insert"):
        raise _bad(f'mode must be overwrite or insert, not "{mode}"')
    if video_only and mode != "overwrite":
        raise _bad("--video-only needs --mode overwrite: insert would push that audio track along for a leg that goes.")
    seq = items.check_expect_seq(rpc, expect_seq)
    picked = items.resolve_item(item, rpc=rpc)
    half = None
    if video_only:
        half = _half_frame(seq or rpc("get_sequence"))
        if atrack is None:
            length = out_s - in_s if in_s is not None else picked.get("duration")
            atrack = _free_audio_track(rpc, at, at + length if length else None, half)
    atrack = 0 if atrack is None else atrack
    params = {"itemName": picked["name"], "binPath": picked.get("bin") or "", "atSeconds": at,
              "videoTrack": vtrack, "audioTrack": atrack, "mode": mode}
    if in_s is not None:
        params["inSeconds"], params["outSeconds"] = in_s, out_s
    if label:
        params["undoLabel"] = label
    result = rpc("insert_clip", params)
    if not video_only:
        return result
    return _take_off_audio_leg(rpc, result, picked["name"], at, atrack, half, label)


def _take_off_audio_leg(rpc, result, name, at, atrack, half, label) -> dict:
    """After a --video-only placement: remove exactly the clip's own audio leg, found by track, position and name."""
    tracks = rpc("list_items", {"kinds": ["audio"]}).get("audio", [])
    on_track = next((t["items"] for t in tracks if t["index"] == atrack), [])
    legs = [i for i in on_track if i.get("start") is not None and abs(i["start"] - at) <= half
            and name in (i.get("projectItem"), i.get("name"))]
    result = dict(result, videoOnly=True, audioTrack=atrack, audioRemoved=False)
    if not legs:  # media without sound: nothing landed there
        return result
    leg = legs[0]
    try:
        gone = rpc("remove_items", {"videoTracks": [], "audioTracks": [atrack], "fromSeconds": leg["start"] - half,
                                    "toSeconds": leg["end"] + half, "mode": "inside", "ripple": False, "dryRun": False,
                                    "undoLabel": f"{label or 'Agent'}: picture only"})
    except BridgeError as e:
        raise BridgeError(f"[remove audio leg] {e} The picture is placed; its audio leg is still on A{atrack + 1}.",
                          code=e.code, outcome="partial", data={"completed": ["insert_clip"], "placed": result}) from e
    removed = gone.get("removed") or []
    if len(removed) != 1 or removed[0].get("name") != leg.get("name"):
        raise BridgeError(f"Removing the audio leg on A{atrack + 1} removed {len(removed)} clip(s), expected that one.",
                          code="VERIFY_FAILED", outcome="committed", data={"placed": result, "removed": removed})
    result["audioRemoved"] = True
    return result


def apply_plan(rpc, *, plan, dry_run=False, media_root=None, bin=None, no_bin=False, only=None, overwrite=False,
               any_sequence=False, notes_as_markers=False, report=None, probe_media=None, convert_audio=None) -> dict:
    """Run a JSON edit plan; the report's `ok` is False when any step is conflict/error/failed/unverified/aborted.
    An audio-only file Premiere refuses (FLAC …) is converted to a WAV beside it and that is imported (step
    `convert`; a dry run writes nothing)."""
    return _plan.run_plan(plan, rpc=rpc, dry_run=dry_run, media_root=media_root, bin=bin, no_bin=no_bin, only=only,
                          overwrite=overwrite, any_sequence=any_sequence, notes_as_markers=notes_as_markers,
                          report=report, probe_media=probe_media, convert_audio=convert_audio)


def replace_clip(rpc, *, track, at, with_item, in_s=None, out_s=None, atrack=None, expect_seq=None, no_effects=False,
                 no_transitions=False, drop_outside_keyframes=False, skip_effects=None, dry_run=False, label=None,
                 probe_media=None) -> dict:
    _, idx = _track(track, "video", "replace_clip works on video tracks (V1, V2, …)")
    aidx = None
    if atrack:
        _, aidx = _track(atrack, "audio", "--atrack must be an audio track (A1, A2, …)")
    items.require_command(rpc, "replace_clip")
    items.check_expect_seq(rpc, expect_seq)
    new = items.resolve_item(with_item, rpc=rpc)
    return _replace.replace_clip(rpc, idx, at, new, src_in=in_s, src_out=out_s, atrack=aidx,
                                 keep_effects=not no_effects, keep_transitions=not no_transitions, dry_run=dry_run,
                                 label=label or "Agent replace", probe_media=probe_media or media.probe_media,
                                 drop_outside_keyframes=drop_outside_keyframes, skip_effects=skip_effects)


RANGE_MODES = ("inside", "overlap")


def _half_frame(seq: dict) -> float:
    fps = framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or 30.0)
    return 0.5 / fps


def _split_inside(clips: list, frm: float, to: float, tol: float) -> tuple[list, list]:
    """(clips entirely inside [frm, to], with half a frame of slack; the neighbours that only overlap it)."""
    def within(c):
        s, e = c.get("start"), c.get("end")
        return isinstance(s, (int, float)) and isinstance(e, (int, float)) and s >= frm - tol and e <= to + tol
    inside = [c for c in clips if within(c)]
    return inside, [{"name": c.get("name"), "start": c.get("start"), "end": c.get("end")}
                    for c in clips if not within(c)]


def _none_inside(where: str, frm: float, to: float, skipped: list) -> BridgeError:
    names = ", ".join(f"{s['name']} ({s['start']}–{s['end']}s)" for s in skipped)
    return BridgeError(f"No clip on {where} lies entirely inside {frm}–{to}s" + (f"; only overlapping: {names}" if
                       names else "") + ".", code="NOT_FOUND", outcome="not_committed", data={"skipped": skipped},
                       hint="Widen the range, aim at one clip with --at, or pass --mode overlap to include clips "
                            "that only overlap it." if skipped else None)


def set_scale(rpc, *, track, at=None, from_s=None, to_s=None, value=None, fit=False, fill=False, mode="inside",
              dry_run=False, label=None, expect_seq=None, probe_media=None) -> dict:
    """Motion > Scale on the clip under `at`, or on the clips in [from, to]: by default only those entirely inside
    it (half a frame of slack), so the neighbours either side are left alone; mode="overlap" takes every clip that
    touches the range."""
    _, idx = _track(track, "video", "set_scale works on video tracks (V1, V2, …)")
    if sum([value is not None, bool(fit), bool(fill)]) != 1:
        raise _bad("Give exactly one of --value, --fit, --fill")
    if at is None and from_s is None:
        raise _bad("Give --at <sec> or --from <sec> [--to <sec>]")
    if mode not in RANGE_MODES:
        raise _bad(f'mode must be inside or overlap, not "{mode}"')
    probe = probe_media or media.probe_media
    frm = at if at is not None else from_s
    to = at if at is not None else (to_s if to_s is not None else from_s)
    base = {"videoTrack": idx, "fromSeconds": frm, "toSeconds": to}
    seq = items.check_expect_seq(rpc, expect_seq)
    skipped = []
    if at is None and to > frm and mode == "inside":
        tol = _half_frame(seq or rpc("get_sequence"))
        track_doc = next((t for t in rpc("list_items", {"kinds": ["video"]}).get("video", []) if t["index"] == idx),
                         None)
        clips = [it for it in (track_doc or {}).get("items", [])
                 if it.get("start") is not None and it.get("end") is not None
                 and it["start"] < to + tol and it["end"] > frm + tol]
        inside, skipped = _split_inside(clips, frm, to, tol)
        if not inside:
            raise _none_inside(f"V{idx + 1}", frm, to, skipped)
        # The panel takes every clip touching its range: narrow the range so that is exactly these clips (the
        # neighbours end at the first start or start at the last end), in the same single undo step.
        base.update(fromSeconds=min(c["start"] for c in inside),
                    toSeconds=max(c["end"] - min(tol, (c["end"] - c["start"]) / 2) for c in inside))
    ranged = {"rangeMode": mode, "skipped": skipped} if at is None and to > frm else {}

    if value is not None:
        params = dict(base, value=value, dryRun=bool(dry_run))
        if label:
            params["undoLabel"] = label
        return dict(rpc("set_scale", params), **ranged)

    how = "fit" if fit else "fill"
    seq = rpc("get_sequence")
    preview = rpc("set_scale", dict(base, dryRun=True))
    out = dict({"mode": how, "dryRun": bool(dry_run), "sequence": [seq.get("width"), seq.get("height")],
                "clips": []}, **ranged)
    for clip in preview["clips"]:
        info = probe(clip.get("mediaPath"))
        scale = media.fit_scale(how, info, seq)
        row = {"name": clip["name"], "start": clip["start"], "media": [info.get("width"), info.get("height")],
               "scaleBefore": clip.get("scaleBefore"), "value": scale}
        if info.get("rotation"):
            row["rotation"] = info["rotation"]  # media is the size Premiere shows (the rotation applied)
        if scale is None:
            row["error"] = "Could not read media size (is ffprobe on PATH?)"
        elif not dry_run:
            mid = (clip["start"] + clip["end"]) / 2
            undo = label or f"Agent: {how} {clip['name']} ({scale}%)"
            rpc("set_scale", {"videoTrack": idx, "fromSeconds": mid, "toSeconds": mid, "value": scale,
                              "undoLabel": undo})
            row["applied"] = True
        out["clips"].append(row)
    if any("error" in r for r in out["clips"]):
        applied = any(r.get("applied") for r in out["clips"])
        raise BridgeError("see data", code="MISSING_DEPENDENCY" if not shutil.which("ffprobe") else "IO_ERROR",
                          outcome="partial" if applied else "not_committed", data=out)
    return out


def set_fps(rpc, *, item, fps, dry_run=False, label=None) -> dict:
    """Interpret footage at `fps`. It changes the items everywhere they are used, so every reference must name
    exactly one item."""
    refs = _as_list(item) or []
    if not refs:
        raise _bad("Give at least one --item")
    project_items = rpc("list_project_items").get("items", [])
    picked = [items.resolve_item(ref, project_items, unique=True) for ref in refs]
    if dry_run:
        media_paths = {os.path.normcase(i.get("mediaPath") or "") for i in picked}
        timeline = rpc("list_items", {"kinds": ["video", "audio"]})
        uses = [{"track": t["label"], "name": it["name"], "start": it["start"], "end": it["end"]}
                for kind in ("video", "audio") for t in timeline.get(kind, []) for it in t["items"]
                if os.path.normcase(it.get("mediaPath") or "") in media_paths]
        return {"dryRun": True, "fps": fps, "items": [items.item_ref(i) for i in picked],
                "usesInActiveSequence": uses,
                "warning": "Interpretation changes these items in every sequence that uses them; "
                           "other sequences cannot be listed yet, and clip lengths there change too."}
    params = {"items": [{"name": i["name"], "binPath": i.get("bin") or ""} for i in picked], "fps": fps}
    if label:
        params["undoLabel"] = label
    return rpc("set_fps", params)


def subclip(rpc, *, item, in_s, out_s, name, bin=None, video_only=False, audio_only=False, allow_duplicate_name=False,
            dry_run=False, label=None, probe_media=None) -> dict:
    """A subclip of a project item (`item`: a name or "<bin path>/<name>") from `in_s` to `out_s` source seconds,
    named `name`, with hard boundaries, in `bin` (a bin path; default: the item's own bin, "" = root; a missing
    top-level bin is created). Both points are snapped to the item's frames first (sourceSnap): its interpreted rate
    as the panel reads it, else replace_clip's rule (ffprobe and the sequence rate). The panel refuses a name already
    in that bin (PRECONDITION_FAILED) unless allow_duplicate_name, before anything changes. One undo step, two when
    Premiere puts the subclip elsewhere and it is moved (undoSteps). Read back: the new item, its bin and its own
    in/out point; VERIFY_FAILED (committed) when one of them is not what was asked."""
    if video_only and audio_only:
        raise _bad("--video-only and --audio-only exclude each other")
    if not str(name or "").strip():
        raise _bad("--name must not be empty")
    try:
        in_s, out_s = float(in_s), float(out_s)
    except (TypeError, ValueError):
        raise _bad(f"--in and --out must be seconds, not {in_s!r} / {out_s!r}") from None
    if not (math.isfinite(in_s) and math.isfinite(out_s)) or in_s < 0 or out_s <= in_s:
        raise _bad(f"--out ({out_s}) must come after --in ({in_s}), both >= 0 source seconds")
    items.require_command(rpc, "subclip")
    src = items.resolve_item(item, rpc=rpc)
    params = {"itemName": src["name"], "binPath": src.get("bin") or "", "name": name, "inSeconds": in_s,
              "outSeconds": out_s, "takeVideo": not audio_only, "takeAudio": not video_only}
    if bin is not None:
        params["bin"] = str(bin).strip("/")
    if allow_duplicate_name:
        params["allowDuplicateName"] = True
    if label:
        params["undoLabel"] = label
    plan = rpc("subclip", dict(params, dryRun=True))  # the panel's refusals first, and the item's frame rate

    info = (probe_media or media.probe_media)(src.get("mediaPath")) or {}
    notes = []
    rate, grid = plan.get("frameRate"), "item"
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate <= 0:
        seq_fps = None
        try:
            seq = rpc("get_sequence")
            seq_fps = framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps"))
        except BridgeError as e:
            if e.code != "NO_SEQUENCE":
                raise
        if seq_fps:
            rate, grid = framegrid.source_rate(info, seq_fps)
        else:
            rate, grid = info.get("fps"), "media"
    snap = {}
    if rate:
        snap = {"grid": grid, "fps": round(rate, 4)}
        in_s, out_s = _replace._snap_source(in_s, rate, snap, "in"), _replace._snap_source(out_s, rate, snap, "out")
        if out_s < in_s + 1 / rate - 1e-9:
            raise _bad(f"--out must be at least one frame after --in on the item's frames ({snap['fps']} fps)")
    else:
        notes.append("the item's frame rate is unknown (the panel could not read it, ffprobe could not tell and "
                     "there is no active sequence): --in/--out were sent as given")
    tol = (0.5 / rate if rate else 1e-3) + 1e-6
    length = info.get("duration") or plan.get("mediaSeconds")
    if isinstance(length, (int, float)) and out_s > length + tol:
        raise _bad(f"--out ({out_s:.4f}s) is past the end of {items.item_ref(src)} ({length:.3f}s). Nothing was changed.")
    if audio_only and info.get("hasAudio") is False:
        raise _bad(f"{items.item_ref(src)} has no audio stream (ffprobe): --audio-only would make an empty subclip")
    if video_only and info and not info.get("width"):
        raise _bad(f"{items.item_ref(src)} has no video stream (ffprobe): --video-only would make an empty subclip")
    params.update(inSeconds=in_s, outSeconds=out_s)
    if plan.get("sameNameElsewhere"):
        notes.append(f'other items are named "{name}" too ({", ".join(plan["sameNameElsewhere"])}): address the '
                     'subclip as "<bin path>/<name>"')
    extra = {k: v for k, v in (("sourceSnap", snap if snap.get("in") or snap.get("out") else None),
                               ("notes", notes or None)) if v}
    if dry_run:
        return dict(plan, inSeconds=in_s, outSeconds=out_s, **extra)

    result = dict(rpc("subclip", dict(params, dryRun=False)), **extra)
    want_bin = plan.get("binPath")
    back = result.get("readBack")
    problems = []
    if not result.get("created"):
        problems.append(f'no new item named "{name}" after the subclip step (new items: {result.get("newItems") or []})')
    else:
        if (result.get("binPath") or "") != (want_bin or ""):
            problems.append(f'it is in bin "{result.get("binPath")}", not "{want_bin}"')
        # Premiere 27.1 answers a subclip's getInPoint / getOutPoint with -400000 s, its "no in/out set" value (live,
        # 2 Nov 2026): the range cannot be read over UXP, so only the new item and its bin are checked then.
        unreadable = back is not None and min(back["inSeconds"], back["outSeconds"]) <= -399999
        if back is None or unreadable:
            result.setdefault("notes", []).append(
                "Premiere does not report a subclip's in/out over UXP (it answers -400000 s, 'not set'): the subclip "
                "exists in its bin, its range is not verified - place it and check its length")
            result["rangeVerified"] = False
        elif abs(back["inSeconds"] - in_s) > tol or abs(back["outSeconds"] - out_s) > tol:
            problems.append(f"its in/out reads {back['inSeconds']:.4f}–{back['outSeconds']:.4f}s, not "
                            f"{in_s:.4f}–{out_s:.4f}s")
    result["verified"] = not problems
    if problems:
        raise BridgeError(f"The subclip step ran ({', '.join(result.get('undoSteps') or [])}), but " + "; ".join(problems)
                          + ". Check the project panel.", code="VERIFY_FAILED", outcome="committed", data=result)
    return result


def _media_facts(info: dict) -> dict:
    """What relink compares, from probe_media (None = unknown)."""
    return {"duration": info.get("duration"), "fps": info.get("fps"),
            "hasVideo": bool(info.get("width")) if info else None, "hasAudio": info.get("hasAudio")}


def compare_media(old: dict, new: dict, fps_tolerance: float = 0.01) -> tuple[list[str], list[str]]:
    """(differences, facts compared) between two _media_facts dicts: the duration within one frame (of the old
    media's rate, else the new one's, else 1/24 s), the frame rate within `fps_tolerance`, video and audio presence.
    A fact either side does not know is not compared."""
    diffs, checked = [], []
    fps = old.get("fps") or new.get("fps") or 24.0
    if old.get("duration") and new.get("duration"):
        checked.append("duration")
        if abs(old["duration"] - new["duration"]) > 1 / fps + 1e-6:
            diffs.append(f"duration {old['duration']:.3f}s -> {new['duration']:.3f}s (more than a frame)")
    if old.get("fps") and new.get("fps"):
        checked.append("fps")
        if abs(old["fps"] - new["fps"]) > fps_tolerance:
            diffs.append(f"frame rate {old['fps']:.3f} -> {new['fps']:.3f} fps")
    for key, what in (("hasVideo", "video"), ("hasAudio", "audio")):
        if old.get(key) is not None and new.get(key) is not None:
            checked.append(key)
            if old[key] != new[key]:
                diffs.append(f"{'has' if old[key] else 'no'} {what} -> {'has' if new[key] else 'no'} {what}")
    return diffs, checked


def relink(rpc, *, item, to, force=False, dry_run=False, probe_media=None) -> dict:
    """Point a project item (`item`: a name or "<bin path>/<name>"; it must name exactly one item) at the media file
    `to`. Before anything changes: `to` passes paths.check as typed and exists, is in a codec Premiere imports, and
    ffprobe finds it matches the item's media (compare_media against the old file, or against what Premiere reports
    when the old file cannot be read, e.g. offline) — a difference, or nothing to compare, is refused
    (PRECONDITION_FAILED, data.comparison) unless `force`. Read back: VERIFY_FAILED (committed) when the item's media
    path did not change. Every sequence using the item follows: `uses` counts its clips in all sequences. Not an undo
    step the bridge can label: relink back to previousPath to revert."""
    paths.check(to, what="--to")  # as typed, and every link on the way (a mapped drive is fine)
    new = paths.resolved(to)
    if not new.is_file():
        raise BridgeError(f"File not found: {new}", code="FILE_NOT_FOUND", outcome="not_committed")
    items.require_command(rpc, "relink")
    picked = items.resolve_item(item, rpc("list_project_items").get("items", []), unique=True)
    base = {"itemName": picked["name"], "binPath": picked.get("bin") or "", "newPath": str(new)}
    plan = rpc("relink", dict(base, dryRun=True))
    old_path = plan.get("previousPath") or picked.get("mediaPath")
    norm = lambda p: os.path.normcase(os.path.normpath(str(p).removeprefix("\\\\?\\"))) if p else ""  # noqa: E731
    if norm(old_path) == norm(new):
        return dict(plan, dryRun=bool(dry_run), alreadyLinked=True, verified=True,
                    note=f"{items.item_ref(picked)} already uses {new}; nothing was changed.")

    probe = probe_media or media.probe_media
    if probe_media is None and not shutil.which("ffprobe") and not force:
        raise BridgeError("ffprobe is needed to compare the new file with the old media (install ffmpeg), or pass "
                          "--force to relink without the check. Nothing was changed.", code="MISSING_DEPENDENCY",
                          outcome="not_committed")
    new_info = probe(str(new)) or {}
    codecs = media.unimportable(new_info)
    if codecs and not force:
        raise BridgeError(f"Premiere does not read {', '.join(codecs)} (it would hang the panel). Nothing was changed.",
                          code="UNSUPPORTED", outcome="not_committed",
                          data={"path": str(new), "codecs": codecs, "transcode": media.transcode_command(new, new_info)})
    old_info = probe(old_path) if old_path and os.path.isfile(str(old_path).removeprefix("\\\\?\\")) else {}
    current = plan.get("current") or {}
    old_facts = _media_facts(old_info) if old_info else {
        "duration": current.get("mediaSeconds"), "fps": current.get("frameRate"), "hasVideo": None, "hasAudio": None}
    new_facts = _media_facts(new_info)
    diffs, checked = compare_media(old_facts, new_facts)
    comparison = {"old": old_facts, "new": new_facts, "oldFrom": "file" if old_info else "premiere",
                  "checked": checked, "differences": diffs, "forced": bool(force and (diffs or not checked))}
    if not force and (diffs or not checked):
        why = ("; ".join(diffs) if diffs else
               "nothing could be compared (ffprobe could not read the new file, or neither the old file nor "
               "Premiere gave the old media's facts)")
        raise BridgeError(f"{new.name} does not match the media of {items.item_ref(picked)}: {why}. Nothing was "
                          "changed; pass --force to relink anyway.", code="PRECONDITION_FAILED",
                          outcome="not_committed", data={"comparison": comparison, "previousPath": old_path})
    uses = plan.get("uses") or {}
    note = (f"Every use of {items.item_ref(picked)} now shows the new file: {uses.get('clips', 0)} clip(s) in "
            f"{len(uses.get('sequences') or [])} sequence(s)"
            + ("" if uses.get("complete", True) else " (the count is incomplete: a sequence could not be read)")
            + f". Relinking is not a labelled undo step: relink to {old_path} to revert.")
    if dry_run:
        return dict(plan, comparison=comparison, note=note)

    result = rpc("relink", dict(base, dryRun=False))
    result.update(comparison=comparison, note=note)
    if not result.get("relinked") or norm(result.get("mediaPath")) != norm(new):
        raise BridgeError(f"relink ran, but {items.item_ref(picked)} reads {result.get('mediaPath')!r}, not {str(new)!r}"
                          + (" (Premiere returned false)" if result.get("returned") is False else "")
                          + ". Check the item in the project panel.", code="VERIFY_FAILED", outcome="committed",
                          data=result)
    result["verified"] = True
    return result


def add_marker(rpc, *, at, name, comment="", duration=0.0, type="Comment", color=None, label=None) -> dict:
    params = {"atSeconds": at, "name": name, "comments": comment or "", "durationSeconds": duration, "type": type}
    if color is not None:
        c = str(color).strip().lower()
        index = int(c) if c.isdigit() else MARKER_COLORS.get(c)
        if index is None or not 0 <= index <= 7:
            raise _bad(f"Unknown colour {color}; use {', '.join(MARKER_COLORS)} or 0–7")
        params["colorIndex"] = index
    if label:
        params["undoLabel"] = label
    return rpc("add_marker", params)


def _name_colours(markers) -> None:
    for m in markers or []:
        m["color"] = MARKER_COLOR_NAMES.get(m.get("colorIndex"))


def list_markers(rpc) -> dict:
    """The active sequence's markers in start order (guid, name, comments, start / duration / end in sequence seconds,
    type, colorIndex), each with `color`: the name add_marker --color takes for that index."""
    items.require_command(rpc, "list_markers")
    data = rpc("list_markers")
    _name_colours(data.get("markers"))
    return data


def remove_marker(rpc, *, guid=None, name=None, at=None, dry_run=False, label=None, expect_seq=None) -> dict:
    """Remove ONE sequence marker: by `guid` (list_markers), or by exact `name`, with `at` (its start, sequence
    seconds, half a frame of tolerance) when several share the name. The panel refuses before changing anything when
    none (NOT_FOUND, data.markers) or several (AMBIGUOUS, data.matches) match; one undo step, read back."""
    if not guid and not name:
        raise _bad("Give --guid, or --name (with --at when several markers share it); list_markers shows both")
    items.require_command(rpc, "remove_marker")
    items.check_expect_seq(rpc, expect_seq)
    params = {"dryRun": bool(dry_run)}
    if guid:
        params["guid"] = guid
    if name:
        params["name"] = name
    if at is not None:
        params["atSeconds"] = at
    if label:
        params["undoLabel"] = label
    result = rpc("remove_marker", params)
    _name_colours(result.get("removed"))
    return result


def list_sequences(rpc) -> dict:
    """Every sequence of the project: name, guid, active, and restorePoint (clone_sequence's record: sourceGuid,
    sourceName, created, renamed; null for every other sequence)."""
    items.require_command(rpc, "list_sequences")
    return rpc("list_sequences")


def clone_sequence(rpc, *, name=None, guid=None, keep=5, dry_run=False, label=None, as_name=None) -> dict:
    """A restore point before a risky edit: the panel clones the active sequence (or `name` / `guid`), renames the
    clone "<name> [restore YYYY-MM-DD HH:MM]", tags it, and deletes the oldest restore points of that same sequence
    beyond `keep` (the new one counts; never an untagged, renamed or active sequence). The clone is not made active:
    set_active_sequence --guid <clone.guid> opens it. A dry run gives the name and what pruning would delete.
    as_name: a plain copy under that name instead (another language version, say): not tagged, nothing pruned, refused
    when a sequence already has the name; two undo steps as well."""
    if keep is None or keep < 1:
        raise _bad("--keep must be at least 1: the new restore point always stays")
    items.require_command(rpc, "clone_sequence")
    params = {"keep": int(keep), "dryRun": bool(dry_run)}
    if as_name is not None:
        if not str(as_name).strip():
            raise _bad("--as is empty")
        params["asName"] = str(as_name).strip()
    if guid:
        params["guid"] = guid
    elif name:
        params["name"] = name
    if label:
        params["undoLabel"] = label
    return rpc("clone_sequence", params)


def _file_time(path) -> int | None:
    """A file's modification time in ns; None when it is missing or not readable from here."""
    try:
        return os.stat(path).st_mtime_ns
    except (OSError, TypeError, ValueError):
        return None


def _local_time(ns: int | None) -> str | None:
    if ns is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ns // 10 ** 9)) + f".{(ns // 10 ** 6) % 1000:03d}"


def save_project(rpc, *, wait=3.0, sleep=time.sleep) -> dict:
    """Save the active project (Project#save; not an undo step) and check the file: its modification time must change
    (polled for up to `wait` seconds), else VERIFY_FAILED (committed). A path this machine cannot read (a project never
    saved, another drive mapping) is reported in `note` and not checked."""
    items.require_command(rpc, "save_project")
    path = rpc("get_project").get("path") or None
    before = _file_time(path) if path else None
    try:
        result = rpc("save_project")
    except BridgeError as e:
        if before is not None and e.outcome == "unknown" and _file_time(path) == before:
            e.outcome = "not_committed"  # the file is untouched: nothing was written
        raise
    after, waited = (_file_time(path) if path else None), 0.0
    while before is not None and after in (None, before) and waited < wait:
        sleep(0.25)
        waited += 0.25
        after = _file_time(path)
    changed = None if before is None or after is None else after != before
    result.update(fileTimeBefore=_local_time(before), fileTimeAfter=_local_time(after), fileTimeChanged=changed)
    if before is None:
        result["note"] = f"The project file ({path}) is not readable from here, so its time stamp was not checked."
    elif not changed:
        why = "is gone" if after is None else "has the same time stamp"
        raise BridgeError(f"Premiere reported the project saved, but the file {path} {why} after {waited:g} s. Premiere "
                          "may skip writing a project without unsaved changes; check File > Save in Premiere before "
                          "relying on it.", code="VERIFY_FAILED", outcome="committed", data=result)
    return result


def _clip_keys(timeline: dict) -> dict:
    """(track, name, in, out) -> start, for spotting clips a ripple moved."""
    return {(t["label"], it["name"], round(it.get("inPoint") or 0, 4), round(it.get("outPoint") or 0, 4)): it["start"]
            for kind in ("video", "audio") for t in timeline.get(kind, []) for it in t["items"]
            if it.get("start") is not None}


def remove_items(rpc, *, track, from_s, to_s, mode="inside", ripple=False, confirm_ripple=False, dry_run=False,
                 label=None, expect_seq=None, only_name=None) -> dict:
    if ripple and not dry_run and not confirm_ripple:
        raise BridgeError("--ripple moves later clips, which can push graphics and music out of sync with the "
                          "picture. Run it with --dry-run to see what may move, then add --confirm-ripple.",
                          code="CONFIRM_REQUIRED", outcome="not_committed")
    if mode not in ("inside", "overlap"):
        raise _bad(f'mode must be inside or overlap, not "{mode}"')
    vts, ats = [], []
    for value in _as_list(track) or []:
        kind, idx = _track(value)
        (vts if kind == "video" else ats).append(idx)
    if not vts and not ats:
        raise _bad("Give at least one --track")
    params = {"videoTracks": vts, "audioTracks": ats, "fromSeconds": from_s, "toSeconds": to_s,
              "mode": mode, "ripple": bool(ripple), "dryRun": bool(dry_run)}
    if label:
        params["undoLabel"] = label
    items.check_expect_seq(rpc, expect_seq)  # a dry run against another sequence would plan the wrong timeline
    allowed = _as_list(only_name)
    if allowed:  # the user moves tracks while the agent works: a track number alone once deleted their graphic
        planned = rpc("remove_items", dict(params, dryRun=True)).get("removed") or []
        others = [r for r in planned if r.get("name") not in allowed]
        if others:
            raise BridgeError(f"--only-name: the range also holds {', '.join(sorted({r.get('name') or '?' for r in others}))}. "
                              "Nothing was removed; read the track again (the user may have moved clips).",
                              code="PRECONDITION_FAILED", outcome="not_committed", data={"others": others})
    if not ripple:
        return rpc("remove_items", params)

    before = rpc("list_items", {"kinds": ["video", "audio"]})
    result = rpc("remove_items", params)
    gone = {(r["track"], r["name"], r.get("start")) for r in result.get("removed", [])}
    if dry_run:
        result["mayShift"] = [{"track": t["label"], "name": it["name"], "start": it["start"]}
                              for kind in ("video", "audio") for t in before.get(kind, []) for it in t["items"]
                              if it.get("start") is not None and it["start"] >= from_s - 1e-3
                              and (t["label"], it["name"], it["start"]) not in gone]
        return result
    old, new = _clip_keys(before), _clip_keys(rpc("list_items", {"kinds": ["video", "audio"]}))
    result["shifted"] = [{"track": k[0], "name": k[1], "from": round(old[k], 4), "to": round(new[k], 4)}
                         for k in old if k in new and abs(new[k] - old[k]) > 1e-4]
    return result


def remove_project_items(rpc, *, item=None, bin=None, dry_run=False, label=None) -> dict:
    """Remove bins (`bin`: "Bin" or "Bin/Sub bin", repeatable; everything inside goes too) and/or project items
    (`item`: a name or "<bin path>/<name>", repeatable) from the project in one undo step. The media files stay on
    disk. The panel refuses before changing anything while a sequence uses one of the items (IN_USE, data.inUse) or
    a sequence would go with them (UNSUPPORTED); a dry run lists what would go, the uses and `wouldRefuse`."""
    bins = [str(b).strip("/") for b in _as_list(bin) or []]
    refs = _as_list(item) or []
    if not bins and not refs:
        raise _bad("Give at least one --bin or --item")
    if any(not b for b in bins):
        raise _bad("The root bin cannot be removed: name a bin inside it")
    items.require_command(rpc, "remove_project_items")
    params = {"dryRun": bool(dry_run)}
    if bins:
        params["bins"] = bins
    if refs:
        project_items = rpc("list_project_items").get("items", [])
        picked = [items.resolve_item(ref, project_items, unique=True) for ref in refs]
        params["items"] = [{"name": i["name"], "binPath": i.get("bin") or ""} for i in picked]
    if label:
        params["undoLabel"] = label
    result = rpc("remove_project_items", params)
    result["note"] = "Media files on disk are not touched; Edit > Undo in Premiere brings the items back."
    if not dry_run and result.get("remaining"):
        raise BridgeError(f"Still in the project after the removal: {', '.join(result['remaining'])}",
                          code="VERIFY_FAILED", outcome="committed", data=result)
    return result


def _read_components(json_file) -> list:
    p = _in_file(json_file, "--json").expanduser()
    if not p.is_file():
        raise BridgeError(f"File not found: {p.resolve()}", code="FILE_NOT_FOUND", outcome="not_committed")
    try:
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise _bad(f"{p.name} is not valid JSON ({e})") from e
    shape = 'expected a list of components or {"components": [...]}'
    if isinstance(doc, dict):
        if set(doc) != {"components"}:
            raise _bad(f"{p.name}: {shape}; unexpected keys: {sorted(set(doc) - {'components'}) or 'none'}")
        doc = doc["components"]
    if not isinstance(doc, list) or not doc:
        raise _bad(f"{p.name}: {shape}")
    return doc


def set_params(rpc, *, track, at, json_file, label=None, expect_seq=None, dry_run=False) -> dict:
    """Effect params/keyframes on the clip under `at`, from a JSON file (a components list or {"components": …}).
    Keyframes need a timeBase (media, clip or sequence); the whole request is checked before anything is sent."""
    kind, idx = _track(track)  # V… or A… (audio: components the clip already has, e.g. its Volume)
    components = _read_components(json_file)
    params = {("audioTrack" if kind == "audio" else "videoTrack"): idx, "atSeconds": at, "components": components}
    if label:
        params["undoLabel"] = label
    if dry_run:
        params["dryRun"] = True
    problems = spec.check_params("set_params", params)
    for ci, comp in enumerate(components):
        for pi, p in enumerate((comp.get("params") or []) if isinstance(comp, dict) else []):
            if isinstance(p, dict) and p.get("keyframes") and not p.get("timeBase"):
                problems.append(f"params.components[{ci}].params[{pi}]: keyframes need a timeBase "
                                "(media, clip or sequence)")
    if problems:
        raise BridgeError("set_params: " + "; ".join(problems), code="BAD_PARAMS", outcome="not_committed",
                          data={"problems": problems})
    items.require_command(rpc, "set_params")
    items.check_expect_seq(rpc, expect_seq)
    return rpc("set_params", params)


# Clip Volume > Level is linear in UXP; Premiere shows it as dB = 20·log10(level) + 15 (measured on 27.1:
# 0 dB = 0.17782794, -6 dB = 0.0891, the +15 dB maximum = 1.0).
VOLUME_MAX_DB = 15.0


def level_from_db(db: float) -> float:
    return 10 ** ((db - VOLUME_MAX_DB) / 20)


def db_from_level(level) -> float | None:
    if not isinstance(level, (int, float)) or isinstance(level, bool):
        return None
    return round(20 * math.log10(level) + VOLUME_MAX_DB, 2) + 0.0 if level > 0 else float("-inf")  # +0.0: no "-0.0"


def _volume_param(clip: dict):
    """(component, param) of the clip's own Volume > Level: the first "Internal Volume …" component (mono and
    stereo clips name it differently), its param called Level, else index 1."""
    for comp in clip.get("components") or []:
        if str(comp.get("matchName", "")).startswith("Internal Volume"):
            params = comp.get("params") or []
            level = next((p for p in params if p.get("name") == "Level"), None)
            level = level or next((p for p in params if p.get("index") == 1), None)
            if level is not None:
                return comp, level
    return None, None


def set_volume(rpc, *, track, db, at=None, from_s=None, to_s=None, mode="inside", dry_run=False, label=None,
               expect_seq=None) -> dict:
    """Clip volume (Volume > Level, in dB as Premiere shows it: 0 = as recorded, -6 = half, at most +15) on the
    audio clip under `at`, or on the clips in [from, to]: by default only those entirely inside it (half a frame of
    slack), so the neighbours either side keep their level; mode="overlap" takes every clip that touches the range.
    One undo step per clip; read back afterwards. A keyframed Level is refused before anything is written."""
    _, idx = _track(track, "audio", "set_volume works on audio tracks (A1, A2, …)")
    try:
        db = float(db)
    except (TypeError, ValueError):
        raise _bad(f"--db must be a number, not {db!r}") from None
    if not math.isfinite(db) or db > VOLUME_MAX_DB:
        raise _bad(f"--db must be a finite number up to +{VOLUME_MAX_DB:g} (Premiere's maximum), not {db}")
    if at is None and from_s is None:
        raise _bad("Give --at <sec> or --from <sec> [--to <sec>]")
    if mode not in RANGE_MODES:
        raise _bad(f'mode must be inside or overlap, not "{mode}"')
    frm = at if at is not None else from_s
    to = at if at is not None else (to_s if to_s is not None else from_s)
    items.require_command(rpc, "set_volume")
    seq = items.check_expect_seq(rpc, expect_seq)
    ranged = at is None and to > frm

    def read():
        doc = rpc("inspect_items", {"audioTracks": [idx], "fromSeconds": frm, "toSeconds": to})
        return [c for t in doc.get("tracks") or [] for c in t.get("clips") or []]

    clips, skipped = read(), []
    if ranged and mode == "inside":
        clips, skipped = _split_inside(clips, frm, to, _half_frame(seq or rpc("get_sequence")))
        if not clips:
            raise _none_inside(f"A{idx + 1}", frm, to, skipped)
    level = level_from_db(db)
    rows, problems = [], []
    for clip in clips:
        comp, param = _volume_param(clip)
        row = {"name": clip.get("name"), "start": clip.get("start"), "end": clip.get("end")}
        if param is None:
            problems.append(f"{clip.get('name')} at {clip.get('start')}s has no Volume > Level")
        elif param.get("keyframed"):
            problems.append(f"{clip.get('name')} at {clip.get('start')}s: Volume > Level is keyframed")
        else:
            row.update(dbBefore=db_from_level(param.get("value")), db=db, level=level,
                       _target=(comp.get("matchName"), comp.get("occurrence") or 1, param.get("index")))
        rows.append(row)
    if not rows:
        raise BridgeError(f"No clip on A{idx + 1} at {frm}" + (f"–{to}" if to != frm else "") + "s.",
                          code="NOT_FOUND", outcome="not_committed")
    if problems:
        raise BridgeError("; ".join(problems) + " (nothing was changed)", code="KEYFRAMED"
                          if all("keyframed" in p for p in problems) else "UNSUPPORTED", outcome="not_committed",
                          data={"problems": problems})
    out = {"track": f"A{idx + 1}", "db": db, "level": level, "dryRun": bool(dry_run), "clips": rows}
    if ranged:
        out.update(rangeMode=mode, skipped=skipped)
    done = []
    for row in rows:
        match_name, occurrence, index = row.pop("_target")
        if dry_run:
            continue
        mid = (row["start"] + row["end"]) / 2
        try:
            rpc("set_params", {"audioTrack": idx, "atSeconds": mid, "undoLabel": label or
                               f"Agent: volume {row['name']} {db:+g} dB",
                               "components": [{"matchName": match_name, "occurrence": occurrence,
                                               "params": [{"index": index, "value": level}]}]})
        except BridgeError as e:
            if done:
                e.outcome = "partial" if e.outcome == "not_committed" else e.outcome
                e.data = dict(e.data or {}, completed=done)
            raise
        done.append(f"{row['name']} at {row['start']}s")
    if dry_run:
        return out
    after = {(c.get("start"), c.get("name")): _volume_param(c)[1] for c in read()}
    for row in rows:
        p = after.get((row["start"], row["name"]))
        row["dbAfter"] = db_from_level(p.get("value")) if p else None
        row["verified"] = bool(p) and abs(p.get("value", 0) - level) <= max(1e-6, level * 1e-4)
    out["verified"] = all(r["verified"] for r in rows)
    if not out["verified"]:
        raise BridgeError("Volume read back differently (see data)", code="VERIFY_FAILED", outcome="committed",
                          data=out)
    return out


def add_transition(rpc, *, track, at, match_name, duration, edge, double_sided=False, label=None,
                   expect_seq=None) -> dict:
    """A video transition at the start or end of the clip under `at`; the duration is snapped to whole frames
    (+0.1 frame, so Premiere's tick rounding cannot drop one).

    Refused (TRANSITION_EXISTS, nothing written) where that edit point already has a transition, a neighbour's
    included: Premiere keeps the one there and changes nothing; remove_transition takes it off first. Read back from an
    OTIO export (transition items expose no times over UXP): `verified` and `transition` {matchName, name, at,
    before, after}; VERIFY_FAILED (committed) when no transition of that name and length is there afterwards."""
    _, idx = _track(track, "video", "add_transition works on video tracks (V1, V2, …)")
    if edge not in ("start", "end"):
        raise _bad(f'edge must be "start" or "end", not "{edge}"')
    if not match_name:
        raise _bad("match_name is required (see list_transition_names)")
    if duration is None or float(duration) <= 0:
        raise _bad("duration must be > 0 seconds")
    items.require_command(rpc, "add_transition")
    seq = _sequence(rpc, expect_seq)
    fps = framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or 30)
    frame = 1.0 / fps
    n_frames = round(float(duration) * fps)
    if n_frames < 1:
        raise _bad(f"duration {duration}s is shorter than one frame at {fps:.3f} fps")
    track_ = next((t for t in rpc("list_items", {"kinds": ["video"]}).get("video", []) if t["index"] == idx), None)
    hits = [i for i in (track_ or {}).get("items", []) if i.get("start") is not None and i.get("end") is not None
            and i["start"] <= at + _replace.EPS and i["end"] > at + _replace.EPS]
    if len(hits) != 1:
        raise BridgeError(f"Expected one clip on {track} at {at}s, found {len(hits)}",
                          code="NOT_FOUND" if not hits else "AMBIGUOUS", outcome="not_committed")
    cut = hits[0]["start"] if edge == "start" else hits[0]["end"]

    def at_cut():
        return [t for t in _replace.otio_transitions(rpc, idx) if abs(t["at"] - cut) <= frame]

    existing = at_cut()
    if existing:
        e = existing[0]
        raise BridgeError(f"{track} already has a transition at the {edge} of {hits[0]['name']} ({cut:.3f}s): "
                          f"{e['name'] or e['matchName']}, {e['before']:.3f}s before and {e['after']:.3f}s after the "
                          "cut. Premiere keeps a transition already at an edit point and would change nothing. "
                          "Nothing was written.",
                          code="TRANSITION_EXISTS", outcome="not_committed", data={"existing": e, "cut": cut},
                          hint=f"Delete that transition first: remove_transition --track {track} --at {at} --edge "
                               f"{edge} (or in Premiere: click it on the timeline, press Delete), then add this one "
                               "again.")
    params = {"videoTrack": idx, "atSeconds": at, "matchName": match_name, "durationSeconds": (n_frames + 0.1) / fps,
              "applyToStart": edge == "start", "forceSingleSided": not double_sided}
    if label:
        params["undoLabel"] = label
    data = rpc("add_transition", params)
    data.setdefault("durationFrames", n_frames)
    found = at_cut()
    landed = [t for t in found if t["matchName"] == match_name
              and abs(t["before"] + t["after"] - n_frames * frame) <= 0.6 * frame]
    if not landed:
        raise BridgeError(f"add_transition ran, but the OTIO export shows no {match_name} of {n_frames} frames at the "
                          f"{edge} of {hits[0]['name']} ({cut:.3f}s) on {track}"
                          + (f" (found: {found})" if found else "") + ". Check the timeline; nothing else changed.",
                          code="VERIFY_FAILED", outcome="committed", data={"reply": data, "found": found, "cut": cut})
    data.update(verified=True, transition=landed[0])
    return data


def _same_transition(a: dict, b: dict, tol: float) -> bool:
    return (a["matchName"] == b["matchName"] and abs(a["at"] - b["at"]) <= tol and abs(a["before"] - b["before"]) <= tol
            and abs(a["after"] - b["after"]) <= tol)


def remove_transition(rpc, *, track, at, edge, dry_run=False, label=None, expect_seq=None) -> dict:
    """Remove the video transition at the start or end of the clip under `at`, in one undo step.

    It is found in an OTIO export first (transition items expose no times over UXP): none within a frame of that edit
    point is NOT_FOUND, and nothing is sent. `transition` says which one goes — matchName, name, `at` (the cut),
    seconds `before` / `after` it, and `owner`: "clip" (it lies on this clip), "shared" (across the cut), or
    "previous" / "next" (the neighbour's single-sided fade at this edit point: an edit point holds one transition,
    whichever clip it lies on, so that is the one that goes — check the dry run). Afterwards a second export must show
    it gone and every other transition on the track still there (`verified`), else VERIFY_FAILED (committed). The
    panel's `via` is "neighbour" when Premiere filed it under the neighbouring clip's edge (one more undo step)."""
    _, idx = _track(track, "video", "remove_transition works on video tracks (V1, V2, …); UXP reaches no audio "
                                    "transitions")
    if edge not in ("start", "end"):
        raise _bad(f'edge must be "start" or "end", not "{edge}"')
    items.require_command(rpc, "remove_transition")
    seq = _sequence(rpc, expect_seq)
    frame = 1.0 / framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or 30)
    track_ = next((t for t in rpc("list_items", {"kinds": ["video"]}).get("video", []) if t["index"] == idx), None)
    hits = [i for i in (track_ or {}).get("items", []) if i.get("start") is not None and i.get("end") is not None
            and i["start"] <= at + _replace.EPS and i["end"] > at + _replace.EPS]
    if len(hits) != 1:
        raise BridgeError(f"Expected one clip on {track} at {at}s, found {len(hits)}",
                          code="NOT_FOUND" if not hits else "AMBIGUOUS", outcome="not_committed")
    clip = hits[0]
    cut = clip["start"] if edge == "start" else clip["end"]
    report = {"track": f"V{idx + 1}", "edge": edge, "cut": round(cut, 6),
              "clip": {"name": clip["name"], "start": clip["start"], "end": clip["end"]}}
    before = _replace.otio_transitions(rpc, idx)
    near = sorted((t for t in before if abs(t["at"] - cut) <= frame), key=lambda t: abs(t["at"] - cut))
    if not near:
        reason = (f"No transition at the {edge} of {clip['name']} ({cut:.3f}s) on V{idx + 1}: the OTIO export shows "
                  "none within a frame of that edit point. Nothing was changed.")
        if dry_run:
            return dict(report, dryRun=True, transition=None, wouldRefuse="NOT_FOUND", reason=reason)
        raise BridgeError(reason, code="NOT_FOUND", outcome="not_committed", data={"transitions": before})
    owned = _replace.cut_transitions(near[:1], clip["start"], clip["end"], frame)
    target = {"matchName": near[0]["matchName"], "name": near[0]["name"], "at": round(near[0]["at"], 6),
              "before": round(near[0]["before"], 6), "after": round(near[0]["after"], 6),
              "owner": owned[0]["owner"] if owned else None}
    params = {"videoTrack": idx, "atSeconds": at, "edge": edge, "dryRun": bool(dry_run)}
    if label:
        params["undoLabel"] = label
    data = rpc("remove_transition", params)
    data.update(report, transition=target)
    if dry_run:
        return data
    after = _replace.otio_transitions(rpc, idx)
    still = [t for t in after if abs(t["at"] - cut) <= frame]
    gone = [t for t in before if t is not near[0] and not any(_same_transition(t, a, frame / 2) for a in after)]
    if still or gone:
        what = ([f"a transition is still at {cut:.3f}s ({still[0]['name'] or still[0]['matchName']})"] if still else []) \
            + ([f"{len(gone)} other transition(s) on V{idx + 1} went too"] if gone else [])
        raise BridgeError(f"remove_transition ran, but the OTIO export disagrees: {'; '.join(what)}. Check the "
                          "timeline (Edit > Undo brings it back).", code="VERIFY_FAILED", outcome="committed",
                          data={"reply": data, "stillThere": still, "alsoGone": gone})
    data["verified"] = True
    return data


def remove_effect(rpc, *, track, at, match_name, occurrence=1, dry_run=False, label=None, expect_seq=None) -> dict:
    """Remove one added effect (filter) from the clip under `at` on a video or an audio track, in one undo step: the
    `occurrence`-th instance of `match_name` in chain order, as inspect_items lists it. Motion, Opacity, Time
    Remapping and an audio clip's own volume components are refused (UNSUPPORTED); a component the clip lacks is
    NOT_FOUND (a dry run says wouldRefuse). The panel reads the chain back: it must be the old one without exactly
    that instance (`verified`), else VERIFY_FAILED (committed)."""
    kind, idx = _track(track)
    if not match_name:
        raise _bad("match_name is required (inspect_items lists each clip's components)")
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1:
        raise _bad(f"occurrence must be a whole number >= 1, not {occurrence!r}")
    items.require_command(rpc, "remove_effect")
    items.check_expect_seq(rpc, expect_seq)
    params = {("audioTrack" if kind == "audio" else "videoTrack"): idx, "atSeconds": at, "matchName": match_name,
              "occurrence": occurrence, "dryRun": bool(dry_run)}
    if label:
        params["undoLabel"] = label
    data = rpc("remove_effect", params)
    if not dry_run and not data.get("verified"):
        raise BridgeError(f"remove_effect ran, but the chain read back as {data.get('chain')} (before: "
                          f"{data.get('chainBefore')}).", code="VERIFY_FAILED", outcome="committed", data=data)
    return data


KEYFRAME_TIME_BASES = ("media", "clip", "sequence")


def remove_keyframes(rpc, *, track, at, match_name, param, occurrence=1, all_keys=False, from_s=None, to_s=None,
                     time_base=None, keep_value=False, dry_run=False, label=None, expect_seq=None) -> dict:
    """Remove keyframes of one param (`match_name` / `occurrence` / `param` index, as inspect_items lists them) of the
    clip under `at`, in one undo step: all of them (`all_keys`), or those from `from_s` to `to_s` (inclusive) in
    `time_base` — media (what inspect_items reports), clip or sequence seconds, converted like set_params' keyframes;
    the range may reach past the clip, where a trim left keyframes. No keyframes there is NOT_FOUND (a dry run says
    wouldRefuse).

    When every keyframe goes, animation is switched off (`animationOff`); `valueAfter` is the value Premiere kept
    (unmeasured which: the playhead's, probably). `keep_value` sets the value the param had at the middle of the clip
    instead, in the same step (`keptValue`) — the clip's body value, where fades at either end do not reach. Read
    back (`remaining`, the kept value): `verified`, else VERIFY_FAILED (committed)."""
    kind, idx = _track(track)
    if not match_name:
        raise _bad("match_name is required (inspect_items lists each clip's components)")
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1:
        raise _bad(f"occurrence must be a whole number >= 1, not {occurrence!r}")
    if not isinstance(param, int) or isinstance(param, bool) or param < 0:
        raise _bad(f"param must be the param's index (0, 1, …; inspect_items lists them), not {param!r}")
    ranged = from_s is not None or to_s is not None
    if bool(all_keys) == ranged:
        raise _bad("Give --all, or --from and --to with --time-base (not both)")
    if ranged:
        if from_s is None or to_s is None:
            raise _bad("--from and --to come as a pair")
        if time_base not in KEYFRAME_TIME_BASES:
            raise _bad(f"--time-base must be media, clip or sequence (inspect_items reports keyframes in media time), "
                       f"not {time_base!r}")
        if to_s < from_s:
            raise _bad(f"--to ({to_s}) is before --from ({from_s})")
    elif time_base is not None:
        raise _bad("--time-base goes with --from/--to")
    items.require_command(rpc, "remove_keyframes")
    items.check_expect_seq(rpc, expect_seq)
    params = {("audioTrack" if kind == "audio" else "videoTrack"): idx, "atSeconds": at, "matchName": match_name,
              "occurrence": occurrence, "paramIndex": param, "keepValue": bool(keep_value), "dryRun": bool(dry_run)}
    if all_keys:
        params["all"] = True
    else:
        params.update(fromSeconds=from_s, toSeconds=to_s, timeBase=time_base)
    if label:
        params["undoLabel"] = label
    data = rpc("remove_keyframes", params)
    if not dry_run and not data.get("verified"):
        raise BridgeError(f"remove_keyframes ran, but the param read back differently: keyframes left "
                          f"{data.get('remaining')}" + (f", value {data.get('valueAfter')!r} (wanted "
                                                         f"{data.get('keptValue')!r})" if keep_value else "") + ".",
                          code="VERIFY_FAILED", outcome="committed", data=data)
    if data.get("animationOff") and not keep_value and not data.get("wouldRefuse"):
        data["note"] = ("Animation is off: the param holds one value (valueAfter, after a real run). --keep-value keeps "
                        "the value at the middle of the clip; set_params sets any other.")
    return data


# ---------------------------------------------------------------- razor-free editing (clone + trim; keeps clip attributes)

def _render_checker(render_check, render_dir, preset, dry_run):
    """rendercheck.check with its options bound, or None. ffmpeg and the WAV preset are checked here, before the edit
    writes anything (a real run only)."""
    if not render_check:
        return None
    if render_dir:
        paths.check(render_dir, what="--render-dir")
    if not dry_run:
        _review.check_preset_path(preset)
        _review._require_ffmpeg()
        preset = _review.resolve_preset(preset)

    def check(rpc, pieces):
        return _rendercheck.check(rpc, pieces, out_dir=render_dir, preset=preset)
    return check


def split_clip(rpc, *, track, at, label=None, dry_run=False, expect_seq=None, render_check=False, render_dir=None,
               preset=None, probe_media=None) -> dict:
    """Split the clip under `at` on one track without a razor: clone past the end, trim, head-trim the copy, clone
    it to the cut, remove the parked copy (5 undo steps, no move: a move across clips renders silent / blank on
    27.1) - the two halves keep every clip attribute (effects, volume, scaling). At a clip edge it is a no-op.
    render_check: render the sequence afterwards and check both pieces play (rendercheck; render_dir keeps the files,
    preset is the WAV .epr). probe_media (ffprobe): media lengths, when a moved transition is put back next to a
    neighbour (B2)."""
    items.require_command(rpc, "split_clip")
    _sequence(rpc, expect_seq)
    return _editing.split_clip(rpc, track=track, at=at, label=label or f"Agent split {track} at {at}s", dry_run=dry_run,
                               render_check=_render_checker(render_check, render_dir, preset, dry_run),
                               probe_media=probe_media or media.probe_media)


def cut_range(rpc, *, from_s, to_s, track=None, no_ripple=False, captions=None, srt=None, srt_offset=0.0,
              label=None, dry_run=False, expect_seq=None, render_check=False, render_dir=None, preset=None,
              probe_media=None) -> dict:
    """Remove [from, to) from every track (or the --track ones) and, unless no_ripple, move everything after it left
    on all those tracks by the same amount (one undo step), keeping them in sync; clips crossing an edge are split or
    trimmed, so they keep their attributes. With captions (a caption track) + srt, conform that track afterwards.
    render_check: render afterwards and check every piece it split, trimmed or moved plays (see split_clip)."""
    if captions and not srt:
        raise _bad("--captions needs --srt (the caption lines' SRT)")
    items.require_command(rpc, "cut_range")
    _sequence(rpc, expect_seq)
    cap = {"track": captions, "srt_path": srt, "offset": srt_offset or 0.0} if captions else None
    return _editing.cut_range(rpc, start=from_s, end=to_s, tracks=track or None, ripple=not no_ripple, captions=cap,
                              label=label or f"Agent cut {from_s}-{to_s}s", dry_run=dry_run,
                              render_check=_render_checker(render_check, render_dir, preset, dry_run),
                              probe_media=probe_media or media.probe_media)


def extend_cut(rpc, *, at, out_by=0.0, in_by=0.0, track=None, extend=None, captions=None, srt=None, srt_offset=0.0,
               label=None, dry_run=False, expect_seq=None, probe_media=None, allow_silent=False, render_check=False,
               render_dir=None, preset=None) -> dict:
    """Open up the cut at `at` (breathing room): out_by more of the outgoing clip's source, in_by more of the
    incoming one's, with everything after the cut moving right on every track (or the --track ones) in one step.
    --extend limits which tracks get the longer clips (others keep a gap); media lengths come from ffprobe.
    An extension held silent or invisible by a keyframed fade is refused unless allow_silent. render_check: render
    afterwards and check every clip it extended or moved plays (see split_clip)."""
    if captions and not srt:
        raise _bad("--captions needs --srt (the caption lines' SRT)")
    items.require_command(rpc, "extend_cut")
    _sequence(rpc, expect_seq)
    probe, lengths = probe_media or media.probe_media, {}

    def media_seconds(clip):
        path = clip.get("mediaPath")
        if not path:
            return None
        if path not in lengths:
            d = (probe(path) or {}).get("duration")
            lengths[path] = float(d) if isinstance(d, (int, float)) and d > 0 else None
        return lengths[path]

    cap = {"track": captions, "srt_path": srt, "offset": srt_offset or 0.0} if captions else None
    return _editing.extend_cut(rpc, at=at, out_by=out_by or 0.0, in_by=in_by or 0.0, tracks=track or None,
                               extend=extend or None, captions=cap, media_seconds=media_seconds, dry_run=dry_run,
                               label=label or f"Agent open cut at {at}s", allow_silent=allow_silent,
                               render_check=_render_checker(render_check, render_dir, preset, dry_run),
                               probe_media=probe)


def shift_items(rpc, *, from_s, by, track=None, label=None, dry_run=False, expect_seq=None, probe_media=None) -> dict:
    """Move every clip that starts at/after `from_s` on every track (or the --track ones) by `by` seconds (whole
    frames; negative = left) in one undo step, keeping them in sync: room before a clip that has no edit point for
    extend_cut, or a gap closed. Clips crossing `from_s` stay put."""
    info = items.require_command(rpc, "shift_items")
    _sequence(rpc, expect_seq)
    return _editing.shift_items(rpc, at=from_s, by=by, tracks=track or None, dry_run=dry_run,
                                label=label or f"Agent shift from {from_s}s by {by}s",
                                precise=items.has_feature(info, "shiftItemsPrecise"),
                                probe_media=probe_media or media.probe_media)


def snap_edges(rpc, *, track=None, label=None, dry_run=False, expect_seq=None) -> dict:
    """Put clip edges that drifted microseconds off the frame grid back on it (lint OFF_GRID), so the picture cuts
    on the same frame as the sound. Every track, or the --track ones; verified to the microsecond."""
    items.require_command(rpc, "snap_edges")
    _sequence(rpc, expect_seq)
    return _editing.snap_edges(rpc, tracks=track or None, dry_run=dry_run, label=label or "Agent snap edges to frames")


def conform_captions(rpc, *, track, srt, offset=0.0, label=None, dry_run=False, expect_seq=None) -> dict:
    """Trim the pieces of a pre-rendered caption track so none starts inside the previous line (no flash of the
    wrong line) or ends a few frames into the next one; SRT times are the caption movie's source time + offset."""
    items.require_command(rpc, "conform_captions")
    _sequence(rpc, expect_seq)
    return _editing.conform_captions(rpc, track=track, srt_path=srt, offset=offset or 0.0, dry_run=dry_run,
                                     label=label or "Agent conform captions")


# ---------------------------------------------------------------- listening report and timeline lint

def _audio_tracks(track) -> tuple:
    labels = []
    for t in _as_list(track) or ["A1"]:
        kind, idx = _track(t)
        if kind != "audio":
            raise _bad(f"{t} is not an audio track (A1, A2, …)")
        labels.append(f"A{idx + 1}")
    return tuple(labels)


def review_cuts(rpc, *, track=None, pre_roll=0.25, post_roll=0.35, words=None, out_dir=None, render=False,
                preset=None, sliver=0.5, output=None) -> dict:
    """Listening report: every cut on the dialogue track(s) measured for breathing room (silence before/after),
    clipped words, room-tone jumps and clicks, plus the timeline lint (slivers, cuts too close, gaps). Default: a
    shadow mix of the source audio (no Premiere render, clip volume not applied); render=True analyses Premiere's
    own mix (export_sequence + loudness). words: a transcript of the NEW mix (e.g. transcribe.py on the exported
    WAV) for word-level checks. `clean` is false when anything is flagged."""
    if output:
        _json_out(output)
    if out_dir:
        paths.check(out_dir, what="--out-dir")
    words_doc = transcript.read_transcript(words)[1] if words else None
    work = paths.resolved(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="ppro_review_"))
    work.mkdir(parents=True, exist_ok=True)
    report = _review.review_cuts(rpc, tracks=_audio_tracks(track), pre_roll=pre_roll, post_roll=post_roll,
                                 words=words_doc, out_dir=str(work), render=render, preset=preset, sliver=sliver)
    if output:
        out = _out_path(output)
        out.write_bytes(json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"))
        report = dict(report, savedTo=str(out))
    return report


def lint_timeline(rpc, *, sliver=0.5, min_gap_frames=1) -> dict:
    """Structural problems on the active sequence: slivers, cuts too close together, gaps and overlaps, picture and
    dialogue cuts that miss by a few frames, clips past the end."""
    return _review.lint_sequence(rpc, sliver=sliver, min_gap_frames=min_gap_frames)


def export_audio(rpc, *, output, track=None, render=False, preset=None) -> dict:
    """The sequence's audio as a mono WAV for analysis: a shadow mix of the source files of `track` (default A1;
    no render), or with render=True Premiere's own mix rendered with its WAV preset (export_sequence)."""
    return _review.export_audio(rpc, output=output, tracks=_audio_tracks(track), render=render, preset=preset)


# ---------------------------------------------------------------- export: presets and video renders, effect lists

def list_presets(rpc, *, format=None) -> dict:
    """Encoder presets (.epr) on disk; no panel needed (`rpc` is unused, as every command's first argument): Premiere's
    system presets (MediaIO/systempresets next to each running Premiere, or PPRO_PRESET_DIR) and the user's own (Adobe
    Media Encoder's Presets folders, which Premiere's Export dialog saves to), each with the format it writes (decoded
    from its folder's FourCCs or its XML). `format` filters ("h264", "hevc", "quicktime", "wav" …)."""
    found = _presets.discover(format)
    out = {"count": len(found["presets"]), "presets": found["presets"], "systemDirs": found["systemDirs"],
           "userDirs": found["userDirs"]}
    if format:
        out["format"] = format
    if found["notes"]:
        out["notes"] = found["notes"]
    return out


EXPORT_LOST = ("PANEL_TIMEOUT", "TIMEOUT", "PANEL_STUCK", "CONNECTION_CLOSED", "PANEL_DISCONNECTED")
# An export_status poll that fails with one of these is no news (the panel or broker is busy or reconnecting).
EXPORT_POLL_MISSES = ("PANEL_TIMEOUT", "TIMEOUT", "BRIDGE_BUSY", "NO_PANEL", "PANEL_DISCONNECTED", "CONNECTION_CLOSED")
EXPORT_ENDED = ("done", "failed", "cancelled")
# The temp files a render writes first, beside the target: "<stem><uuid>.m4v" and ".aac" for H.264 (live 27.1)
EXPORT_TEMP = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\.[A-Za-z0-9]+"


def _rendered_file(out: Path, reply: dict, started: float) -> Path | None:
    """The file this render wrote: the output path, the path Premiere reports, or the name with its extension doubled
    (export_frame's quirk) — whichever was written since `started` (an older file there is not the render)."""
    reported = str(reply.get("outputPath") or "")
    reported = reported[4:] if reported.startswith("\\\\?\\") else reported
    for cand in (out, Path(reported) if reported else None, out.with_name(out.name + out.suffix)):
        try:
            if cand is not None and cand.is_file() and cand.stat().st_mtime >= started - 2:
                return cand
        except OSError:
            continue
    return None


def _output_size(out: Path) -> int | None:
    """Bytes the render has written so far: the output path or its doubled-extension twin (the larger), plus the temp
    files Premiere renders into first. Live on 27.1, an H.264 render writes "<stem><uuid>.m4v" and "<stem><uuid>.aac"
    beside the target and creates the .mp4 only at the end, and no encoder event fires: those files are the only sign
    of life. None while none of them exists. Sizes come from os.stat: on Windows a directory listing's size (scandir's
    DirEntry.stat) lags for a file another process holds open — it read 0 for a temp file through a 20 s window live."""
    sizes = []
    for cand in (out, out.with_name(out.name + out.suffix)):
        try:
            sizes.append(cand.stat().st_size)
        except OSError:
            continue
    total = max(sizes) if sizes else None
    temp = re.compile(re.escape(out.stem) + EXPORT_TEMP, re.IGNORECASE)
    try:
        with os.scandir(out.parent) as entries:
            found = [e.path for e in entries if temp.fullmatch(e.name) and e.is_file()]
    except OSError:
        found = []
    for path in found:
        try:
            total = (total or 0) + os.stat(path).st_size
        except OSError:  # renamed or removed since the listing (the render ended)
            continue
    return total


def _job_signs(job: dict) -> tuple:
    """What counts as a sign of life in a job's status: its state, its progress and how many events fired."""
    events = sum(v for v in (job.get("events") or {}).values() if isinstance(v, (int, float)))
    return job.get("state"), job.get("progress"), events


def _await_export(rpc, job: dict, out: Path, *, stall_seconds, poll, sleep) -> tuple[dict, dict]:
    """Poll export_status every `poll` s until the job ends, with no time limit. A stall ends the wait instead: no new
    state, progress or event from the panel and no growth of the output file for `stall_seconds` (stats
    "stalledSeconds"). A poll the broker or panel cannot answer (busy, timed out, reconnecting) is no news. After the
    end, the file must stop growing (equal sizes one poll apart): IMMEDIATELY fills a file it created early."""
    job_id = job["jobId"]
    stats, progress = {"polls": 0, "missedPolls": 0}, set()
    prev = size = _output_size(out)
    signs = _job_signs(job)
    quiet_since, quiet_slept = time.monotonic(), 0.0
    while job.get("state") not in EXPORT_ENDED:
        sleep(poll)
        quiet_slept += poll
        current = None
        try:
            current = (rpc("export_status", {"jobId": job_id}).get("jobs") or [None])[0]
            stats["polls"] += 1
        except BridgeError as e:
            if e.code == "NOT_FOUND":
                raise BridgeError(f"Export job {job_id} is gone from the panel's job table (was the panel reloaded?): "
                                  f"Premiere may still be rendering to {out}. Check the file before rendering again.",
                                  code="EXPORT_LOST", outcome="unknown",
                                  data={"jobId": job_id, "output": str(out), "bytes": _output_size(out)}) from e
            if e.code not in EXPORT_POLL_MISSES:
                raise
            stats["missedPolls"] += 1
        prev, size = size, _output_size(out)
        alive = size != prev
        if current:
            job = current
            if isinstance(job.get("progress"), (int, float)):
                progress.add(job["progress"])
            alive = alive or _job_signs(job) != signs
            signs = _job_signs(job)
        if alive:
            quiet_since, quiet_slept = time.monotonic(), 0.0
        elif max(quiet_slept, time.monotonic() - quiet_since) >= stall_seconds:
            stats["stalledSeconds"] = round(max(quiet_slept, time.monotonic() - quiet_since), 1)
            break
    stats["progressSamples"] = len(progress)
    waited = 0.0
    while job.get("state") == "done" and size != prev and waited < stall_seconds:
        sleep(poll)
        waited += poll
        prev, size = size, _output_size(out)
    return job, stats


def _render_result(plan: dict, out: Path, reply: dict, started: float, seq: dict, info: dict, warnings: list,
                   seconds, probe_media, probed=None) -> dict:
    """The rendered file checked against the sequence (ffprobe); no new file -> EXPORT_FAILED."""
    got = _rendered_file(out, reply, started)
    if got is None:
        raise BridgeError(f"Premiere reported the render done, but no new file is at {out}.", code="EXPORT_FAILED",
                          outcome="not_committed", data=dict(plan, reply=reply))
    checks, problems = probed or _presets.check_render((probe_media or media.probe_media)(str(got)), seq, info)
    return dict(plan, exported=True, output=str(got), renderSeconds=seconds, bytes=got.stat().st_size, checks=checks,
                verified=bool(checks) and all(c["ok"] for c in checks.values()), warnings=warnings + problems)


def _finish_job(rpc, plan: dict, job: dict, out: Path, started: float, seq: dict, info: dict, warnings: list, *,
                stall_seconds, poll, sleep, probe_media) -> dict:
    """Wait for an export job (_await_export), then check its file. A failed or cancelled job is EXPORT_FAILED. A
    stalled one whose file holds the whole sequence (ffprobe) counts as done, with a warning; otherwise
    EXPORT_STALLED (outcome unknown: the render may still finish; --job resumes the wait)."""
    job, stats = _await_export(rpc, job, out, stall_seconds=stall_seconds, poll=poll, sleep=sleep)
    report = dict(plan, jobId=job["jobId"], polls=stats["polls"], progressSamples=stats["progressSamples"])
    if stats["missedPolls"]:
        report["missedPolls"] = stats["missedPolls"]
    state = job.get("state")
    if state in ("failed", "cancelled"):
        what = "was cancelled in Premiere" if state == "cancelled" else "failed"
        raise BridgeError(f"The render {what}: {job.get('error')}", code="EXPORT_FAILED", outcome="not_committed",
                          data=dict(report, job=job))
    elapsed = job.get("elapsedSeconds")
    seconds = elapsed if state == "done" and isinstance(elapsed, (int, float)) else round(time.time() - started, 1)
    report["job"] = job
    probed = None
    if state != "done":
        got = _rendered_file(out, job, started)
        probed = _presets.check_render((probe_media or media.probe_media)(str(got)), seq, info) if got else ({}, [])
        if not (probed[0].get("duration") or {}).get("ok"):
            raise BridgeError(f"Export job {job['jobId']}: no progress, no panel event and no new bytes in {out} for "
                              f"{stats['stalledSeconds']:.0f} s, and the file does not hold the whole sequence. The "
                              f"render may still finish: export_video --job {job['jobId']} waits for it again.",
                              code="EXPORT_STALLED", outcome="unknown",
                              data=dict(report, output=str(out), bytes=_output_size(out),
                                        idleSeconds=stats["stalledSeconds"]))
        report["doneBy"] = "file"
        warnings = warnings + [f"The panel never reported the end of the render (state {state}); the file stopped "
                               f"growing {stats['stalledSeconds']:.0f} s ago and holds the whole sequence."]
    return _render_result(report, out, job, started, seq, info, warnings, seconds, probe_media, probed)


def _resume_export(rpc, job_id, *, preset, output, format, detach, stall_seconds, poll, sleep, probe_media) -> dict:
    """export_video --job: the render export_sequence started as `job_id` (its output and preset come from the job);
    wait for it and check its file, or with detach only look at it once."""
    if not items.has_feature(rpc("ping"), "exportJobs"):
        raise BridgeError("The loaded panel has no export jobs (feature exportJobs): Reload the current panel in UXP "
                          "Developer Tool.", code="PANEL_OUTDATED", outcome="not_committed",
                          data={"missingFeature": "exportJobs"})
    try:
        job = rpc("export_status", {"jobId": str(job_id)})["jobs"][0]
    except BridgeError as e:
        if e.code == "NOT_FOUND":
            e.data = dict(e.data or {}, note="A panel reload forgets its jobs: look for the rendered file itself.")
        raise
    if job.get("kind") == "aaf":
        raise _bad(f"Job {job_id} is an AAF export: export_aaf --job {job_id} waits for it. Nothing was sent.")
    out = paths.resolved(job.get("outputPath") or "")  # QA: as --output is resolved (a mapped drive keeps its letter)
    if output and paths.resolved(paths.check(output, what="--output")) != out:
        raise _bad(f"--output {output} is not the file job {job_id} renders ({out}). Nothing was sent.")
    try:
        info = _presets.resolve(job.get("preset"))
    except BridgeError:  # the preset file is gone: check the length only
        info = {"name": Path(str(job.get("preset"))).stem, "title": None, "format": None, "kind": None,
                "source": "path", "path": job.get("preset"), "extensions": []}
    def same(p):  # the file, however it is written (an 8.3 short name, a junction, ..)
        return os.path.normcase(str(paths.resolved(p)))
    if preset and same(_presets.resolve(preset, format)["path"]) != same(info["path"]):
        raise _bad(f"--preset {preset!r} is not the preset job {job_id} renders with ({info['path']}). "
                   "Nothing was sent.")
    seq = rpc("get_sequence")
    plan = {"dryRun": False, "resumed": True, "mode": "job",
            "preset": {k: info.get(k) for k in ("name", "title", "format", "kind", "source", "path")},
            "output": str(out),
            "sequence": {k: seq.get(k) for k in ("name", "width", "height", "fps", "durationSeconds")}}
    warnings = []
    if job.get("sequence") and job["sequence"] != seq.get("name"):
        warnings.append(f'Job {job_id} renders "{job["sequence"]}" but the active sequence is now "{seq.get("name")}": '
                        "the file is checked against the active one.")
    if detach and job.get("state") not in EXPORT_ENDED:
        return dict(plan, detached=True, jobId=job["jobId"], state=job.get("state"), progress=job.get("progress"),
                    job=job, warnings=warnings)
    started = (job.get("startedAt") or 0) / 1000
    return _finish_job(rpc, plan, job, out, started, seq, info, warnings, stall_seconds=stall_seconds, poll=poll,
                       sleep=sleep, probe_media=probe_media)


def export_video(rpc, *, preset=None, output=None, format=None, overwrite=False, dry_run=False, job=None, detach=False,
                 stall_seconds=180.0, poll=2.0, sleep=time.sleep, probe_media=None) -> dict:
    """Render the active sequence with an encoder preset inside Premiere (export_sequence; the project does not change)
    and check the file with ffprobe: frame size, frame rate and length (one frame of slack) against the sequence. A
    mismatch is a warning, not a failure (a 16:9 preset letterboxes a vertical sequence: "High Quality 1080 HD" made
    1920x1080 of a 1080x1920 one). `preset`: a name from list_presets (`format` narrows it) or an .epr path. The output
    must end in an extension the preset's format writes (.mp4 for H.264 / HEVC, .mov for QuickTime …); an existing
    file is refused unless `overwrite` (Premiere 27.1 then replaces it in place, without a dialog), a read-only one
    always. No new file afterwards -> EXPORT_FAILED.
    A panel with the exportJobs feature renders as a job: export_status is polled every `poll` s with no time limit,
    until the job ends or stalls (no progress, no panel event and no file growth for `stall_seconds`: EXPORT_STALLED,
    unless the file already holds the whole sequence). `detach` returns the job at once; `job` resumes a job (its
    output and preset come from the job; with `detach`, one look). An older panel renders in one request (600 s)."""
    stall_seconds, poll = float(stall_seconds), float(poll)
    if stall_seconds <= 0 or poll <= 0:
        raise _bad("--stall-seconds and the poll interval must be > 0")
    if job:
        if dry_run:
            raise _bad("--dry-run does not go with --job (the render is already running). Nothing was sent.")
        return _resume_export(rpc, job, preset=preset, output=output, format=format, detach=detach,
                              stall_seconds=stall_seconds, poll=poll, sleep=sleep, probe_media=probe_media)
    if not preset or not output:
        raise _bad("export_video needs --preset and --output (or --job <id> to wait for a render already started).")
    info = _presets.resolve(preset, format)
    if info["kind"] == "image":
        raise BridgeError(f'"{info["name"]}" writes a {info["format"]} (one file per frame): export_frames makes '
                          "stills. Nothing was rendered.", code="UNSUPPORTED", outcome="not_committed")
    # A format with no extension of its own (MPEG-2, an unknown FourCC) still writes a media file: the panel's own
    # list (actions.json export_sequence outputPath extensions), never, say, a .bat.
    exts = tuple(info["extensions"] or ()) or tuple(spec.action("export_sequence")["params"]["outputPath"]["extensions"])
    if exts and not str(output).strip().lower().endswith(exts):
        raise _bad(f"--output {str(output)!r}: a {info['format']} preset writes {' or '.join(exts)} files. "
                   "Nothing was sent.", extensions=list(exts))
    paths.check(output, what="--output")
    out = paths.resolved(output)
    if out.is_dir():
        raise _bad(f"--output {out} is a folder: give a file name. Nothing was rendered.")
    if out.exists() and not overwrite:
        raise BridgeError(f"{out} already exists: pass --overwrite to replace it. Nothing was rendered.",
                          code="CONFIRM_REQUIRED", outcome="not_committed", data={"output": str(out)})
    if out.exists() and not os.access(out, os.W_OK):
        # 27.1 replaces an existing file in place without asking; a read-only one fails at once with "Unable to
        # initialize export!", the message a broken preset gives too (measured 3 Nov 2026)
        raise BridgeError(f"{out} is read-only, so Premiere cannot replace it. Nothing was rendered.",
                          code="BAD_PARAMS", outcome="not_committed", data={"output": str(out)})
    seq = rpc("get_sequence")
    as_job = items.has_feature(rpc("ping"), "exportJobs")
    plan = {"dryRun": bool(dry_run),
            "preset": {k: info[k] for k in ("name", "title", "format", "kind", "source", "path")},
            "output": str(out),
            "sequence": {k: seq.get(k) for k in ("name", "width", "height", "fps", "durationSeconds")},
            "mode": "job" if as_job else "sync"}
    warnings = [] if exts else [f"{info['format']} presets: the output extension is not checked."]
    slow = None if as_job else _presets.render_warning(seq.get("durationSeconds"), info,
                                                       spec.timeout("export_sequence"))
    if slow:
        warnings.append(slow)
    if detach and not as_job:
        raise BridgeError("--detach needs export jobs (feature exportJobs), which the loaded panel lacks: Reload the "
                          "current panel in UXP Developer Tool. Nothing was rendered.", code="PANEL_OUTDATED",
                          outcome="not_committed", data={"missingFeature": "exportJobs"})
    if dry_run:
        return dict(plan, warnings=warnings)
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    if as_job:
        start = rpc("export_sequence", {"outputPath": str(out), "presetPath": info["path"], "wait": False})
        job = {"jobId": start["jobId"], "state": start.get("state"), "outputPath": start.get("outputPath"),
               "progress": None, "events": {}}
        if detach:
            return dict(plan, detached=True, jobId=job["jobId"], state=job["state"], warnings=warnings,
                        hint=f"export_video --job {job['jobId']} waits for the render and checks the file "
                             "(add --detach for one look).")
        return _finish_job(rpc, plan, job, out, started, seq, info, warnings, stall_seconds=stall_seconds, poll=poll,
                           sleep=sleep, probe_media=probe_media)
    try:
        reply = rpc("export_sequence", {"outputPath": str(out), "presetPath": info["path"]})
    except BridgeError as e:
        if e.code in EXPORT_LOST:
            e.data = dict(e.data or {}, output=str(out), note="Premiere may still be rendering: look for the file "
                          "before rendering again (and Reload the panel if ping reports it stuck).")
        raise
    return _render_result(plan, out, reply, started, seq, info, warnings, round(time.time() - started, 1), probe_media)


# ---------------------------------------------------------------- AAF and FCP XML (export_aaf, export_xml)

AAF_HINT = ('Premiere may be showing its "AAF export log" window (an effect or transition that cannot go into the AAF): '
            "exportAAF does not return until someone clicks OK there. Ask the user to click OK in Premiere, then wait "
            "again with export_aaf --job {job}.")
AAF_LOG_NOTE = ("The export waited with no new bytes before it ended: Premiere probably showed its modal \"AAF export "
                "log\", which lists the effects and transitions left out of the AAF. Ask the user what it listed.")
AAF_HINT_AFTER = 10.0  # seconds a running AAF job may go without new bytes before the reply suspects the modal log


def _file_size(path: Path) -> int | None:
    """The file's size from os.stat (a directory listing's size lags for a file another process holds open)."""
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _fresh_file(out: Path, started: float) -> Path | None:
    """`out` when it was written since `started` (two seconds of slack) and is not empty; else None (an older file of
    that name, as with --overwrite, is not this export)."""
    try:
        st = os.stat(out)
    except OSError:
        return None
    return out if st.st_size > 0 and st.st_mtime >= started - 2 else None


def _check_new_output(out: Path, what: str, overwrite: bool) -> None:
    """Refusals before anything is sent: a folder, an existing file without `overwrite`, a read-only one."""
    if out.is_dir():
        raise _bad(f"--output {out} is a folder: give a file name. Nothing was exported.")
    if out.exists() and not overwrite:
        raise BridgeError(f"{out} already exists: pass --overwrite to replace it. Nothing was exported.",
                          code="CONFIRM_REQUIRED", outcome="not_committed", data={"output": str(out)})
    if out.exists() and not os.access(out, os.W_OK):
        raise BridgeError(f"{out} is read-only, so Premiere cannot replace it with the {what}. Nothing was exported.",
                          code="BAD_PARAMS", outcome="not_committed", data={"output": str(out)})


def _await_aaf(rpc, job: dict, out: Path, *, stall_seconds, poll, sleep) -> tuple[dict, dict]:
    """Poll export_status every `poll` s until the AAF job ends, with no time limit, or stalls: no state change and no
    growth of the .aaf for `stall_seconds` (stats "stalledSeconds"). stats "dialogSuspected": the job ran on for
    AAF_HINT_AFTER s at some point without the file growing (or appearing) - Premiere's modal AAF export log, most
    likely. A poll the broker or panel cannot answer is no news."""
    job_id = job["jobId"]
    stats = {"polls": 0, "missedPolls": 0, "dialogSuspected": False}
    size = _file_size(out)
    quiet_since, quiet_slept = time.monotonic(), 0.0
    while job.get("state") not in EXPORT_ENDED:
        sleep(poll)
        quiet_slept += poll
        try:
            current = (rpc("export_status", {"jobId": job_id}).get("jobs") or [None])[0]
            stats["polls"] += 1
        except BridgeError as e:
            if e.code == "NOT_FOUND":
                raise BridgeError(f"AAF export job {job_id} is gone from the panel's job table (was the panel "
                                  f"reloaded?): Premiere may still be writing {out}. Check the file before exporting "
                                  "again.", code="EXPORT_LOST", outcome="unknown",
                                  data={"jobId": job_id, "output": str(out), "bytes": _file_size(out),
                                        "hint": 'If Premiere shows its "AAF export log" window, ask the user to click '
                                                "OK there; then look for the .aaf before exporting again."}) from e
            if e.code not in EXPORT_POLL_MISSES:
                raise
            stats["missedPolls"] += 1
            current = None
        prev, size = size, _file_size(out)
        alive = size != prev
        if current:
            alive = alive or current.get("state") != job.get("state")
            job = current
        quiet = max(quiet_slept, time.monotonic() - quiet_since)
        if alive:
            quiet_since, quiet_slept = time.monotonic(), 0.0
            continue
        if job.get("state") == "running" and quiet >= AAF_HINT_AFTER:
            stats["dialogSuspected"] = True
        if quiet >= stall_seconds:
            stats["stalledSeconds"] = round(quiet, 1)
            break
    return job, stats


def _finish_aaf(rpc, plan: dict, job: dict, out: Path, started: float, *, stall_seconds, poll, sleep) -> dict:
    """Wait for an AAF job (_await_aaf), then check the file: written since the job started and not empty (ffprobe
    cannot read AAF). A failed job is EXPORT_FAILED, a stalled one EXPORT_STALLED (outcome unknown; --job waits again);
    both, and a done reply whose job waited without new bytes, carry the hint about Premiere's modal AAF export log."""
    job, stats = _await_aaf(rpc, job, out, stall_seconds=stall_seconds, poll=poll, sleep=sleep)
    job_id = job["jobId"]
    hint = AAF_HINT.format(job=job_id)
    report = dict(plan, jobId=job_id, polls=stats["polls"], job=job)
    if stats["missedPolls"]:
        report["missedPolls"] = stats["missedPolls"]
    state = job.get("state")
    if state in ("failed", "cancelled"):
        data = dict(report, output=str(out))
        if stats["dialogSuspected"]:
            data["hint"] = AAF_LOG_NOTE
        raise BridgeError(f"The AAF export failed: {job.get('error')}", code="EXPORT_FAILED", outcome="not_committed",
                          data=data)
    if state != "done":
        raise BridgeError(f"AAF export job {job_id}: no state change and no new bytes in {out} for "
                          f"{stats['stalledSeconds']:.0f} s. {hint}", code="EXPORT_STALLED", outcome="unknown",
                          data=dict(report, output=str(out), bytes=_file_size(out), hint=hint,
                                    idleSeconds=stats["stalledSeconds"]))
    got = _fresh_file(out, started)
    if got is None:
        size = _file_size(out)
        raise BridgeError(f"Premiere reported the AAF export done, but {out} " +
                          ("is empty." if size == 0 else "was not written." if size is None else
                           "is older than the export (not written by it)."),
                          code="EXPORT_FAILED", outcome="not_committed", data=dict(report, output=str(out), bytes=size))
    elapsed = job.get("elapsedSeconds")
    result = dict(report, exported=True, output=str(got), bytes=got.stat().st_size,
                  exportSeconds=elapsed if isinstance(elapsed, (int, float)) else round(time.time() - started, 1),
                  verified=True, warnings=list(plan.get("warnings") or []))
    if stats["dialogSuspected"]:
        result.update(dialogSuspected=True, hint=AAF_LOG_NOTE)
    return result


def _resume_aaf(rpc, job_id, *, output, detach, stall_seconds, poll, sleep) -> dict:
    """export_aaf --job: the AAF job export_aaf started as `job_id` (its output comes from the job); wait for it and
    check its file, or with detach only look at it once."""
    items.require_command(rpc, "export_aaf")
    try:
        job = rpc("export_status", {"jobId": str(job_id)})["jobs"][0]
    except BridgeError as e:
        if e.code == "NOT_FOUND":
            e.data = dict(e.data or {}, note="A panel reload forgets its jobs: look for the .aaf itself.")
        raise
    if job.get("kind") != "aaf":
        raise _bad(f"Job {job_id} is a render, not an AAF export: export_video --job {job_id} waits for it. "
                   "Nothing was sent.")
    out = paths.resolved(job.get("outputPath") or "")  # QA: as --output is resolved (a mapped drive keeps its letter)
    if output and paths.resolved(paths.check(output, what="--output")) != out:
        raise _bad(f"--output {output} is not the file job {job_id} writes ({out}). Nothing was sent.")
    plan = {"dryRun": False, "resumed": True, "output": str(out), "sequence": {"name": job.get("sequence")},
            "options": job.get("options")}
    if detach and job.get("state") not in EXPORT_ENDED:
        out_ = dict(plan, detached=True, jobId=job["jobId"], state=job.get("state"), job=job)
        if job.get("hint"):
            out_["hint"] = AAF_HINT.format(job=job["jobId"])
        return out_
    started = (job.get("startedAt") or 0) / 1000
    return _finish_aaf(rpc, plan, job, out, started, stall_seconds=stall_seconds, poll=poll, sleep=sleep)


def export_aaf(rpc, *, output=None, embed_audio=None, audio_format=None, sample_rate=None, bits_per_sample=None,
               handle_frames=None, mixdown_video=None, render_audio_effects=None, trim_sources=None, overwrite=False,
               dry_run=False, job=None, detach=False, stall_seconds=180.0, poll=2.0, sleep=time.sleep) -> dict:
    """Export the active sequence as an AAF inside Premiere (export_aaf: ProjectConverter.exportAAF; the project does
    not change), as a job polled with export_status every `poll` s and no time limit. Options left at None are not
    sent (Premiere's defaults); the panel applies the others with AAFExportOptions' setters and reports them
    (optionsSet, optionValues). The output must end in .aaf; an existing file is refused unless `overwrite` (whether
    Premiere then asks before replacing it is not measured), a read-only one always. Afterwards the file must have
    been written since the export started and not be empty (ffprobe cannot read AAF).
    Premiere 27.1 b21 opens a modal "AAF export log" when an effect or transition cannot go into the AAF, and exportAAF
    waits until someone clicks OK: a job that runs AAF_HINT_AFTER s without new bytes makes the reply carry that hint;
    one with no state change and no new bytes for `stall_seconds` is EXPORT_STALLED (outcome unknown, with the hint;
    `job` resumes the wait). `detach` returns the job at once; `job` resumes one (with `detach`: one look)."""
    stall_seconds, poll = float(stall_seconds), float(poll)
    if stall_seconds <= 0 or poll <= 0:
        raise _bad("--stall-seconds and the poll interval must be > 0")
    given = {"embedAudio": embed_audio, "audioFormat": audio_format, "sampleRate": sample_rate,
             "bitsPerSample": bits_per_sample, "handleFrames": handle_frames, "mixdownVideo": mixdown_video,
             "renderAudioEffects": render_audio_effects, "trimSources": trim_sources}
    options = {k: v for k, v in given.items() if v is not None}  # the panel's param names, None = not sent
    if job:
        if dry_run or options:
            raise _bad("--job waits for an export already running: --dry-run and the AAF options do not go with it. "
                       "Nothing was sent.")
        return _resume_aaf(rpc, job, output=output, detach=detach, stall_seconds=stall_seconds, poll=poll,
                           sleep=sleep)
    if not output:
        raise _bad("export_aaf needs --output (or --job <id> to wait for an AAF export already started).")
    if options.get("audioFormat") is not None and options["audioFormat"] not in ("wav", "aiff"):
        raise _bad(f"--audio-format must be wav or aiff, not {options['audioFormat']!r}")
    paths.check(output, what="--output", ext=".aaf")
    out = paths.resolved(output)
    _check_new_output(out, "AAF", overwrite)
    items.require_command(rpc, "export_aaf")
    seq = rpc("get_sequence")
    plan = {"dryRun": bool(dry_run), "output": str(out), "options": options,
            "sequence": {k: seq.get(k) for k in ("name", "fps", "durationSeconds")}, "mode": "job"}
    if dry_run:
        return dict(plan, warnings=[])
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        start = rpc("export_aaf", dict(options, outputPath=str(out)))
    except BridgeError as e:
        if e.code in EXPORT_LOST:
            e.data = dict(e.data or {}, output=str(out),
                          hint='Premiere may be showing its "AAF export log" window, holding exportAAF until '
                               "someone clicks OK: ask the user to click OK, then look for the file (and Reload the "
                               "panel if ping reports it stuck) before exporting again.")
        raise
    plan.update(optionsSet=start.get("optionsSet"), optionValues=start.get("optionValues"))
    job = {"jobId": start["jobId"], "kind": "aaf", "state": start.get("state"), "outputPath": start.get("outputPath")}
    if detach:
        return dict(plan, detached=True, jobId=job["jobId"], state=job["state"], warnings=[],
                    hint=f"export_aaf --job {job['jobId']} waits for the export and checks the file (add --detach for "
                         "one look). If it stays running, Premiere is probably showing its modal \"AAF export log\": "
                         "ask the user to click OK there.")
    return _finish_aaf(rpc, plan, job, out, started, stall_seconds=stall_seconds, poll=poll, sleep=sleep)


XML_SNIFF_BYTES = 1 << 16  # how much of an FCP XML file is read to find <xmeml>


def export_xml(rpc, *, output, overwrite=False) -> dict:
    """Export the active sequence as Final Cut Pro 7 XML inside Premiere (export_xml: exportAsFinalCutProXML with
    Premiere's UI suppressed, so no dialog; the project does not change), then check the file: written since the
    request, starting with <?xml (after a BOM or blanks) and holding an <xmeml> element in its first 64 KB. The output
    must end in .xml; an existing file is refused unless `overwrite`, a read-only one always."""
    paths.check(output, what="--output", ext=".xml")
    out = paths.resolved(output)
    _check_new_output(out, "XML", overwrite)
    items.require_command(rpc, "export_xml")
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    reply = rpc("export_xml", {"outputPath": str(out)})
    got = _fresh_file(out, started)
    if got is None:
        raise BridgeError(f"Premiere reported the XML export done, but no new, non-empty file is at {out}.",
                          code="EXPORT_FAILED", outcome="not_committed",
                          data={"output": str(out), "bytes": _file_size(out), "reply": reply})
    with open(got, "rb") as f:
        head = f.read(XML_SNIFF_BYTES)
    text = head.decode("utf-8", errors="replace").lstrip("﻿ \t\r\n")
    version = re.search(r"<xmeml(?:\s[^>]*?\bversion\s*=\s*[\"']([^\"']*)[\"'])?[\s>/]", text)
    checks = {"xmlDeclaration": text.startswith("<?xml"), "xmeml": bool(version)}
    if not all(checks.values()):
        raise BridgeError(f"{got} is not Final Cut Pro XML: " +
                          ("it does not start with <?xml" if not checks["xmlDeclaration"] else
                           f"no <xmeml> element in its first {XML_SNIFF_BYTES // 1024} KB") + ".",
                          code="EXPORT_FAILED", outcome="not_committed",
                          data={"output": str(got), "bytes": got.stat().st_size, "checks": checks})
    return {"exported": True, "output": str(got), "bytes": got.stat().st_size, "checks": checks,
            "xmemlVersion": version.group(1), "verified": True}


def list_effects(rpc, *, kind=None) -> dict:
    """The effects Premiere can add: video filters (match name and display name) and audio filters (display name only,
    in Premiere's UI language); `kind` "audio" or "video" narrows it."""
    items.require_command(rpc, "list_effects")
    return rpc("list_effects", {"kind": kind} if kind else {})


def add_audio_effect(rpc, *, track, name, at=None, from_s=None, to_s=None, mode="inside", dry_run=False, label=None,
                     expect_seq=None) -> dict:
    """Append an audio effect to the audio clip under `at`, by its display name as list_effects shows it (Premiere's UI
    language: "降噪" on a Chinese UI) or by a match name: one undo step, verified by reading the chain back. An
    unknown name is refused with the closest names (data.closeNames). The reply's `component` lists the new effect's
    params by index, for set_params --track A1; a param Premiere animates from the start (`keyframed`) refuses a
    static value there (KEYFRAMED), and `note` says which. from_s / to_s: every clip entirely inside that range
    instead (half a frame of slack; mode="overlap": every clip touching it), one undo step each; a failure after the
    first is `partial` with data.completed."""
    _, idx = _track(track, "audio", "add_audio_effect works on audio tracks (A1, A2, …)")
    if not str(name or "").strip():
        raise _bad("--name is empty")
    if (at is None) == (from_s is None) or (from_s is None) != (to_s is None):
        raise _bad("Give --at <sec>, or --from <sec> and --to <sec>")
    if mode not in RANGE_MODES:
        raise _bad(f'mode must be inside or overlap, not "{mode}"')
    items.require_command(rpc, "add_audio_effect")
    seq = items.check_expect_seq(rpc, expect_seq)
    if at is None:
        return _add_audio_effect_range(rpc, seq, idx, name, from_s, to_s, mode, dry_run, label)
    return _add_audio_effect(rpc, idx, at, name, dry_run, label)


def _add_audio_effect_range(rpc, seq, idx, name, frm, to, mode, dry_run, label) -> dict:
    doc = rpc("inspect_items", {"audioTracks": [idx], "fromSeconds": frm, "toSeconds": to})
    clips, skipped = [c for t in doc.get("tracks") or [] for c in t.get("clips") or []], []
    if mode == "inside":
        clips, skipped = _split_inside(clips, frm, to, _half_frame(seq or rpc("get_sequence")))
    if not clips:
        raise _none_inside(f"A{idx + 1}", frm, to, skipped)
    rows, last = [], {}
    for clip in clips:
        row = {"name": clip.get("name"), "start": clip.get("start"), "end": clip.get("end")}
        try:
            last = _add_audio_effect(rpc, idx, (clip["start"] + clip["end"]) / 2, name, dry_run, label)
        except BridgeError as e:
            if not rows:
                raise
            raise BridgeError(f"{e} ({len(rows)} of {len(clips)} clips were done.)", code=e.code, outcome="partial",
                              data={"completed": rows, "failedOn": row}) from e
        row["verified"] = last.get("verified")
        rows.append(row)
    return {"dryRun": bool(dry_run), "count": len(rows), "clips": rows, "skipped": skipped,
            "component": last.get("component"), "note": last.get("note"),
            "verified": not dry_run and all(r["verified"] for r in rows), "undoSteps": 0 if dry_run else len(rows)}


def _add_audio_effect(rpc, idx, at, name, dry_run, label) -> dict:
    params = {"audioTrack": idx, "atSeconds": at, "name": str(name).strip(), "dryRun": bool(dry_run)}
    if label:
        params["undoLabel"] = label
    result = rpc("add_audio_effect", params)
    if dry_run:
        return result
    armed = [p.get("index") for p in (result.get("component") or {}).get("params") or []
             if p.get("keyframed") and not p.get("keyframes")]
    if armed:
        result["note"] = (f"Premiere turns animation on for param(s) {armed} of the new effect, with no keyframes: "
                          "set_params --track A1 by index sets a static value there (it switches the animation off "
                          "in the same undo step) or adds keyframes.")
    if not result.get("verified"):
        raise BridgeError("The effect was appended but the chain reads back differently (see data).",
                          code="VERIFY_FAILED", outcome="committed", data=result)
    return result


def list_effect_presets(rpc, *, file=None, search=None, locale=None) -> dict:
    """Effect presets (.prfpset) on disk: Premiere's own next to the running Premiere (LocalizedPresets/<UI locale>:
    Factory and Lumetri Presets; PPRO_EFFECT_PRESET_DIR adds folders) and the user's (Documents/Adobe/Premiere Pro
    [(Beta)]/<version>/Profile-*), or only `file`. Each preset: name, bin path, file, effects (match names, in Effect
    Controls order), animated / anchor, and `replay` (full, partial with reasons, none) for apply_preset. `search`
    filters names and bins; `locale` picks the localised files (default: Premiere's UI locale from ping, else en_US).
    A read: when the bridge does not answer, the files are listed anyway (`premiere.checked` false)."""
    return _prfpset.list_presets(rpc, file=file, search_text=search, locale=locale)


def apply_preset(rpc, *, track, preset, at=None, from_s=None, to_s=None, mode="inside", file=None, locale=None,
                 dry_run=False, label=None, expect_seq=None) -> dict:
    """Apply an effect preset to the video clip under `at`, or to the clips in [from, to] (mode inside: entirely
    inside it; overlap: every clip reaching into it), as one set_params request per clip: Motion / Opacity params on
    the clip's own components, every other effect added as a new instance, keyframes in clip time per the preset's
    anchoring (scale / in point / out point), interpolation linear, hold, or bezier as Premiere's eased mode 3.
    Effects or params UXP cannot write (masks, arbitrary data, colours, audio, effects this Premiere lacks or whose
    params no longer line up) are skipped and reported. Every request is dry-run first; dry_run returns them."""
    _, idx = _track(track, "video", "apply_preset works on video tracks (V1, V2, …): audio presets are not replayed")
    if at is not None and (from_s is not None or to_s is not None):
        raise _bad("Give --at or --from/--to, not both")
    if at is None and (from_s is None or to_s is None):
        raise _bad("Give --at, or --from with --to")
    if at is None and to_s <= from_s:
        raise _bad(f"--to {to_s} must be after --from {from_s}")
    if mode not in RANGE_MODES:
        raise _bad(f'mode must be inside or overlap, not "{mode}"')
    locale = _prfpset.norm_locale(locale)
    if file:
        _prfpset.check_file(file)
    info = items.require_command(rpc, "apply_preset")
    seq = items.check_expect_seq(rpc, expect_seq)
    return _prfpset.apply(rpc, idx, preset, at=at, frm=from_s, to=to_s, mode=mode, file=file, locale=locale,
                          ping=info, seq=seq, dry_run=dry_run, label=label)


# ---------------------------------------------------------------- generic operations (apply_ops, query)

# apply_ops' ops and query's names, from actions.json (the panel's whitelist; each op carries its own param spec).
OP_NAMES = tuple(spec.action("apply_ops")["params"]["ops"]["items"]["variants"])
REMOVING_OPS = frozenset(n for n, v in spec.action("apply_ops")["params"]["ops"]["items"]["variants"].items()
                         if v.get("removes"))
QUERY_NAMES = tuple(spec.action("query")["params"]["what"]["enum"])
_QUERY_OPTIONS = {"clips": {"track", "at", "from_s", "to_s"}, "clip": {"track", "at", "max_keyframes"},
                  "markers": set(), "bins": {"bin"}, "sequence": set()}
_QUERY_FLAGS = {"track": "--track", "at": "--at", "from_s": "--from", "to_s": "--to", "bin": "--bin",
                "max_keyframes": "--max-keyframes"}


def read_ops(source) -> list:
    """apply_ops' op list from a list, a JSON file, or JSON text ("[...]" or {"ops": [...]}); anything else BAD_PARAMS."""
    doc = source
    if isinstance(source, (str, Path)):
        text = str(source).strip()
        if text.startswith(("[", "{")):
            where = "the inline JSON"
        else:
            p = _in_file(text, "--json").expanduser()
            if not p.is_file():
                raise BridgeError(f"File not found: {p.resolve()}", code="FILE_NOT_FOUND", outcome="not_committed")
            where, text = p.name, p.read_text(encoding="utf-8-sig")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise _bad(f"{where} is not valid JSON ({e})") from e
    if isinstance(doc, dict) and set(doc) == {"ops"}:
        doc = doc["ops"]
    if not isinstance(doc, list) or not doc:
        raise _bad('apply_ops needs a non-empty list of ops ([{"op": "marker.add", ...}] or {"ops": [...]}); ops: '
                   + ", ".join(OP_NAMES))
    return doc


def apply_ops(rpc, *, ops, label=None, dry_run=False, expect_seq=None) -> dict:
    """Several whitelisted small writes in ONE transaction with one undo step (the panel's apply_ops): `ops` is a list
    of {"op": name, ...its params} (or a JSON file / JSON text holding one). Every op is checked against its spec here
    and again in the panel, its target resolved, every Action built before anything commits; afterwards each op is
    read back (results[i].readBack / verified). A dry run resolves and checks everything and changes nothing.
    sequence.set_player_position runs after the transaction and is not an undo step (notUndoable)."""
    listed = read_ops(ops)
    params = {"ops": listed, "dryRun": bool(dry_run)}
    if label:
        params["undoLabel"] = label
    problems = spec.check_params("apply_ops", params)
    if problems:
        raise BridgeError("apply_ops: " + "; ".join(problems), code="BAD_PARAMS", outcome="not_committed",
                          data={"problems": problems, "ops": list(OP_NAMES)})
    items.require_command(rpc, "apply_ops")
    items.check_expect_seq(rpc, expect_seq)
    result = rpc("apply_ops", params)
    if not dry_run:
        results = result.get("results") or []
        if result.get("verified") is not True or len(results) != len(listed) or \
                not all(r.get("verified") is True for r in results):
            raise BridgeError("apply_ops committed, but not every op reads back as asked (see data.results).",
                              code="VERIFY_FAILED", outcome="committed", data=result)
    return result


def query(rpc, *, what, track=None, at=None, from_s=None, to_s=None, bin=None, max_keyframes=None) -> dict:
    """A whitelisted read by name (the panel's query): clips (on `track`s, default all; under `at` or touching
    [from_s, to_s]), clip (one track and `at`: its components and params), markers, bins (under `bin`, a bin path),
    sequence (frame size, fps, duration, player position, in / out point, tracks). Each takes only its own options."""
    if what not in QUERY_NAMES:
        raise _bad(f'Unknown query "{what}"; one of {", ".join(QUERY_NAMES)}')
    given = {k: v for k, v in (("track", track), ("at", at), ("from_s", from_s), ("to_s", to_s), ("bin", bin),
                               ("max_keyframes", max_keyframes)) if v is not None}
    extra = sorted(set(given) - _QUERY_OPTIONS[what])
    if extra:
        takes = ", ".join(_QUERY_FLAGS[k] for k in sorted(_QUERY_OPTIONS[what])) or "no options"
        raise _bad(f"query --what {what} takes {takes}; not {', '.join(_QUERY_FLAGS[k] for k in extra)}")
    tracks = [str(t).strip().upper() for t in _as_list(track) or []]
    params = {"what": what}
    if what == "clip":
        if len(tracks) != 1 or at is None:
            raise _bad("query --what clip needs one --track and --at")
        params.update(track=tracks[0], atSeconds=at)
        if max_keyframes is not None:
            params["maxKeyframes"] = max_keyframes
    elif what == "clips":
        if tracks:
            params["tracks"] = tracks
        if at is not None:
            params["atSeconds"] = at
        if from_s is not None or to_s is not None:
            if from_s is None or to_s is None:
                raise _bad("Give --from with --to")
            params.update(fromSeconds=from_s, toSeconds=to_s)
    elif what == "bins" and bin is not None:
        params["bin"] = bin
    problems = spec.check_params("query", params)
    if problems:
        raise BridgeError("query: " + "; ".join(problems), code="BAD_PARAMS", outcome="not_committed",
                          data={"problems": problems})
    items.require_command(rpc, "query")
    data = rpc("query", params)
    if what == "markers":
        _name_colours(data.get("markers"))
    return data
