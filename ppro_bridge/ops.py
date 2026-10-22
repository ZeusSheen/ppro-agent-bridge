"""One function per CLI command — the surface the CLI (and later the MCP server) calls.

Every function takes the transport as its REQUIRED first positional argument `rpc` (`transport.rpc`,
`transport.make_rpc(...)`, or a test stub) and keyword-only arguments named like the CLI's argparse destinations.
It returns the reply data (a dict) or raises BridgeError (code, outcome, data). Nothing here prints or exits, and
nothing has a default transport: a forgotten injection fails instead of reaching a live Premiere.
"""

import json
import math
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import apiprobe, framegrid, frames, items, media, panelhash, spec, transcript
from . import editing as _editing
from . import review as _review
from . import plan as _plan
from . import replace as _replace
from ._version import VERSION
from .errors import BridgeError

MARKER_COLORS = {"green": 0, "red": 1, "purple": 2, "magenta": 2, "orange": 3,
                 "yellow": 4, "white": 5, "blue": 6, "cyan": 7}

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"  # this checkout's panel, for the stale check


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
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


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
    local = panelhash.code_hash(PLUGIN_DIR)
    if local:
        data["localCodeHash"] = local
        # A bundled panel (scripts/build_panel.py) has no lib/ folder, so its hash covers other files.
        if data.get("codeHash") and data.get("modules") != "bundle":
            data["stale"] = data["codeHash"] != local
            if data["stale"]:
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
    return rpc("get_sequence")


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
    out_dir = Path(dir).expanduser().resolve()
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
    out_dir = Path(dir).expanduser().resolve()
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
        sheet_path = Path(sheet).expanduser().resolve() if sheet else out_dir / "contact-sheet.jpg"
        made = frames.make_contact_sheet(shots, sheet_path, cols, thumb_width)
        data["sheet"] = made or "skipped (pip install pillow for contact sheets)"
    if any(r["error"] or not r["path"] for r in results):
        raise BridgeError("see data", code=next((c for c in codes if c), "EXPORT_FAILED"),
                          outcome="not_committed", data=data)
    return data


def export_otio(rpc, *, output) -> dict:
    return rpc("export_otio", {"outputPath": str(_out_path(output))})


def probe_api(rpc, *, output=None, diff=None, no_instances=False, max_members=None) -> dict:
    """This Premiere build's UXP API: a summary with the watch list (calls the UXP port depends on); --output saves
    the full dump, --diff compares it with an earlier one (added / removed members)."""
    old = None
    if diff:
        p = Path(diff).expanduser()
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
    items.require_command(rpc, "transcribe_clip")
    picked = items.resolve_item(item, rpc=rpc)
    params = {"itemName": picked["name"], "binPath": picked.get("bin") or ""}
    if language:
        params["language"] = language
    if output:  # before anything starts: a bad output folder must not fail after Speech-to-Text is running
        Path(output).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
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

def import_media(rpc, *, path, bin=None) -> dict:
    paths = [Path(p).expanduser().resolve() for p in _as_list(path) or []]
    if not paths:
        raise _bad("Give at least one --path")
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise BridgeError("File(s) not found", code="FILE_NOT_FOUND", outcome="not_committed",
                          data={"missing": missing})
    params = {"paths": [str(p) for p in paths]}
    if bin:
        params["bin"] = bin
    return rpc("import_media", params)


def insert_clip(rpc, *, item, at=0.0, vtrack=0, atrack=0, mode="overwrite", in_s=None, out_s=None, label=None,
                expect_seq=None) -> dict:
    if (in_s is None) != (out_s is None):
        raise _bad("--in and --out must be given together.")
    if mode not in ("overwrite", "insert"):
        raise _bad(f'mode must be overwrite or insert, not "{mode}"')
    items.check_expect_seq(rpc, expect_seq)
    picked = items.resolve_item(item, rpc=rpc)
    params = {"itemName": picked["name"], "binPath": picked.get("bin") or "", "atSeconds": at,
              "videoTrack": vtrack, "audioTrack": atrack, "mode": mode}
    if in_s is not None:
        params["inSeconds"], params["outSeconds"] = in_s, out_s
    if label:
        params["undoLabel"] = label
    return rpc("insert_clip", params)


def apply_plan(rpc, *, plan, dry_run=False, media_root=None, bin=None, no_bin=False, only=None, overwrite=False,
               any_sequence=False, notes_as_markers=False, report=None, probe_media=None) -> dict:
    """Run a JSON edit plan; the report's `ok` is False when any step is conflict/error/failed/unverified/aborted."""
    return _plan.run_plan(plan, rpc=rpc, dry_run=dry_run, media_root=media_root, bin=bin, no_bin=no_bin, only=only,
                          overwrite=overwrite, any_sequence=any_sequence, notes_as_markers=notes_as_markers,
                          report=report, probe_media=probe_media)


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


def set_scale(rpc, *, track, at=None, from_s=None, to_s=None, value=None, fit=False, fill=False, dry_run=False,
              label=None, expect_seq=None, probe_media=None) -> dict:
    _, idx = _track(track, "video", "set_scale works on video tracks (V1, V2, …)")
    if sum([value is not None, bool(fit), bool(fill)]) != 1:
        raise _bad("Give exactly one of --value, --fit, --fill")
    if at is None and from_s is None:
        raise _bad("Give --at <sec> or --from <sec> [--to <sec>]")
    probe = probe_media or media.probe_media
    frm = at if at is not None else from_s
    to = at if at is not None else (to_s if to_s is not None else from_s)
    base = {"videoTrack": idx, "fromSeconds": frm, "toSeconds": to}
    items.check_expect_seq(rpc, expect_seq)

    if value is not None:
        params = dict(base, value=value, dryRun=bool(dry_run))
        if label:
            params["undoLabel"] = label
        return rpc("set_scale", params)

    mode = "fit" if fit else "fill"
    seq = rpc("get_sequence")
    preview = rpc("set_scale", dict(base, dryRun=True))
    out = {"mode": mode, "dryRun": bool(dry_run), "sequence": [seq.get("width"), seq.get("height")], "clips": []}
    for clip in preview["clips"]:
        info = probe(clip.get("mediaPath"))
        scale = media.fit_scale(mode, info, seq)
        row = {"name": clip["name"], "start": clip["start"], "media": [info.get("width"), info.get("height")],
               "scaleBefore": clip.get("scaleBefore"), "value": scale}
        if scale is None:
            row["error"] = "Could not read media size (is ffprobe on PATH?)"
        elif not dry_run:
            mid = (clip["start"] + clip["end"]) / 2
            undo = label or f"Agent: {mode} {clip['name']} ({scale}%)"
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
        paths = {os.path.normcase(i.get("mediaPath") or "") for i in picked}
        timeline = rpc("list_items", {"kinds": ["video", "audio"]})
        uses = [{"track": t["label"], "name": it["name"], "start": it["start"], "end": it["end"]}
                for kind in ("video", "audio") for t in timeline.get(kind, []) for it in t["items"]
                if os.path.normcase(it.get("mediaPath") or "") in paths]
        return {"dryRun": True, "fps": fps, "items": [items.item_ref(i) for i in picked],
                "usesInActiveSequence": uses,
                "warning": "Interpretation changes these items in every sequence that uses them; "
                           "other sequences cannot be listed yet, and clip lengths there change too."}
    params = {"items": [{"name": i["name"], "binPath": i.get("bin") or ""} for i in picked], "fps": fps}
    if label:
        params["undoLabel"] = label
    return rpc("set_fps", params)


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


def _clip_keys(timeline: dict) -> dict:
    """(track, name, in, out) -> start, for spotting clips a ripple moved."""
    return {(t["label"], it["name"], round(it.get("inPoint") or 0, 4), round(it.get("outPoint") or 0, 4)): it["start"]
            for kind in ("video", "audio") for t in timeline.get(kind, []) for it in t["items"]
            if it.get("start") is not None}


def remove_items(rpc, *, track, from_s, to_s, mode="inside", ripple=False, confirm_ripple=False, dry_run=False,
                 label=None, expect_seq=None) -> dict:
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


def _read_components(json_file) -> list:
    p = Path(json_file).expanduser()
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


def set_params(rpc, *, track, at, json_file, label=None, expect_seq=None) -> dict:
    """Effect params/keyframes on the clip under `at`, from a JSON file (a components list or {"components": …}).
    Keyframes need a timeBase (media, clip or sequence); the whole request is checked before anything is sent."""
    kind, idx = _track(track)  # V… or A… (audio: components the clip already has, e.g. its Volume)
    components = _read_components(json_file)
    params = {("audioTrack" if kind == "audio" else "videoTrack"): idx, "atSeconds": at, "components": components}
    if label:
        params["undoLabel"] = label
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
    return round(20 * math.log10(level) + VOLUME_MAX_DB, 2) if level > 0 else float("-inf")


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


def set_volume(rpc, *, track, db, at=None, from_s=None, to_s=None, dry_run=False, label=None,
               expect_seq=None) -> dict:
    """Clip volume (Volume > Level, in dB as Premiere shows it: 0 = as recorded, -6 = half, at most +15) on the
    audio clip under `at`, or on every clip overlapping [from, to]. One undo step per clip; read back afterwards.
    A keyframed Level is refused before anything is written."""
    _, idx = _track(track, "audio", "set_volume works on audio tracks (A1, A2, …)")
    try:
        db = float(db)
    except (TypeError, ValueError):
        raise _bad(f"--db must be a number, not {db!r}") from None
    if not math.isfinite(db) or db > VOLUME_MAX_DB:
        raise _bad(f"--db must be a finite number up to +{VOLUME_MAX_DB:g} (Premiere's maximum), not {db}")
    if at is None and from_s is None:
        raise _bad("Give --at <sec> or --from <sec> [--to <sec>]")
    frm = at if at is not None else from_s
    to = at if at is not None else (to_s if to_s is not None else from_s)
    items.require_command(rpc, "set_volume")
    items.check_expect_seq(rpc, expect_seq)

    def read():
        doc = rpc("inspect_items", {"audioTracks": [idx], "fromSeconds": frm, "toSeconds": to})
        return [c for t in doc.get("tracks") or [] for c in t.get("clips") or []]

    level = level_from_db(db)
    rows, problems = [], []
    for clip in read():
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
    included: Premiere keeps the one there and changes nothing, and the bridge cannot delete it. Read back from an
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
                          "cut. Premiere keeps a transition already at an edit point and would change nothing, and "
                          "the bridge cannot delete transitions. Nothing was written.",
                          code="TRANSITION_EXISTS", outcome="not_committed", data={"existing": e, "cut": cut},
                          hint="Delete that transition in Premiere (click it on the timeline, press Delete), then "
                               "add this one again.")
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


# ---------------------------------------------------------------- razor-free editing (clone + trim; keeps clip attributes)

def split_clip(rpc, *, track, at, label=None, dry_run=False, expect_seq=None) -> dict:
    """Split the clip under `at` on one track without a razor: clone, trim, move — the two halves keep every clip
    attribute (effects, volume, scaling). At a clip edge it is a no-op."""
    items.require_command(rpc, "split_clip")
    _sequence(rpc, expect_seq)
    return _editing.split_clip(rpc, track=track, at=at, label=label or f"Agent split {track} at {at}s", dry_run=dry_run)


def cut_range(rpc, *, from_s, to_s, track=None, no_ripple=False, captions=None, srt=None, srt_offset=0.0,
              label=None, dry_run=False, expect_seq=None) -> dict:
    """Remove [from, to) from every track (or the --track ones) and, unless no_ripple, move everything after it left
    on all those tracks by the same amount (one undo step), keeping them in sync; clips crossing an edge are split or
    trimmed, so they keep their attributes. With captions (a caption track) + srt, conform that track afterwards."""
    if captions and not srt:
        raise _bad("--captions needs --srt (the caption lines' SRT)")
    items.require_command(rpc, "cut_range")
    _sequence(rpc, expect_seq)
    cap = {"track": captions, "srt_path": srt, "offset": srt_offset or 0.0} if captions else None
    return _editing.cut_range(rpc, start=from_s, end=to_s, tracks=track or None, ripple=not no_ripple, captions=cap,
                              label=label or f"Agent cut {from_s}-{to_s}s", dry_run=dry_run)


def extend_cut(rpc, *, at, out_by=0.0, in_by=0.0, track=None, extend=None, captions=None, srt=None, srt_offset=0.0,
               label=None, dry_run=False, expect_seq=None, probe_media=None, allow_silent=False) -> dict:
    """Open up the cut at `at` (breathing room): out_by more of the outgoing clip's source, in_by more of the
    incoming one's, with everything after the cut moving right on every track (or the --track ones) in one step.
    --extend limits which tracks get the longer clips (others keep a gap); media lengths come from ffprobe.
    An extension held silent or invisible by a keyframed fade is refused unless allow_silent."""
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
                               label=label or f"Agent open cut at {at}s", allow_silent=allow_silent)


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
    words_doc = transcript.read_transcript(words)[1] if words else None
    work = Path(out_dir).expanduser().resolve() if out_dir else Path(tempfile.mkdtemp(prefix="ppro_review_"))
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
