"""
apply_plan — execute a JSON edit plan against Premiere's active sequence.

Plan format (what an editor or agent writes before cutting):

  {
    "sequence": "Main",                  # the plan's target; a real run refuses another active sequence
    "fps": 25,                           # informational; the sequence's fps wins
    "media_root": "./media/",            # relative to the plan file
    "commands": [
      {"command": "insert_clip", "id": "b01", "track": "V3", "timeline_at": 2.0,
       "src": "stock/b01.mp4", "src_in": 9.0, "src_out": 14.0,
       "atrack": "A4",                   # optional; default: a free audio track
       "scale": "fill"},                 # optional: "fit" | "fill" | percent
      {"command": "insert_clip", "id": "g01", "track": "V4", "timeline_at": 9.2,
       "src": null, "type": "graphic", ...},   # src null → built by hand, skipped
      {"command": "note", "id": "a02", "timeline_at": 34.1, "note": "..."}
    ]
  }

Times are seconds. `timeline_at` snaps to the sequence frame grid and the placed duration to whole frames.
Nothing is written until every command resolves; `--dry-run` stops after that and prints what would happen.

A real run refuses a plan whose "sequence" is not the active one (--any-sequence overrides), and stops before the
next write if the active sequence or project changes underneath it. A range covering the whole file is placed as
the whole clip, because an out point at the very end gets clamped and loses a frame.
"""

import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

from . import framegrid, items, media, spec
from .errors import BridgeError

BAD = {"conflict", "error", "missing-media", "failed", "unverified", "aborted"}


def _norm(p: str | None) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""


def _overlap(a0, a1, b0, b1, tol) -> bool:
    return a0 < b1 - tol and b0 < a1 - tol


def _read_plan(plan_path: Path) -> dict:
    if not plan_path.is_file():
        raise BridgeError(f"File not found: {plan_path}", code="FILE_NOT_FOUND", outcome="not_committed")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BridgeError(f"{plan_path.name} is not a valid JSON plan ({e}).", code="BAD_PARAMS",
                          outcome="not_committed") from e
    except OSError as e:
        raise BridgeError(f"Cannot read {plan_path}: {e}", code="IO_ERROR", outcome="not_committed") from e
    if not isinstance(plan, dict) or not isinstance(plan.get("commands") or [], list):
        raise BridgeError(f'{plan_path.name} is not a plan (expected an object with a "commands" list).',
                          code="BAD_PARAMS", outcome="not_committed")
    return plan


def run_plan(plan, *, rpc, dry_run=False, media_root=None, bin=None, no_bin=False, only=None, overwrite=False,
             any_sequence=False, notes_as_markers=False, report=None, probe_media=None) -> dict:
    """Execute (or with dry_run, only resolve and check) the plan file `plan`; returns the report.
    `only`: comma-separated command ids (or a list). `probe_media` defaults to ffprobe."""
    if isinstance(only, (list, tuple, set)):
        only = ",".join(str(x) for x in only)
    args = SimpleNamespace(plan=str(plan), dry_run=dry_run, media_root=media_root, bin=bin, no_bin=no_bin,
                           only=only, overwrite=overwrite, any_sequence=any_sequence,
                           notes_as_markers=notes_as_markers, report=report)
    min_protocol = (spec.command("apply_plan") or {}).get("minProtocol") or 1
    return run(args, rpc=rpc,
               require_panel=lambda actions: items.require_panel(rpc, actions, min_protocol=min_protocol),
               parse_track=items.parse_track, probe_media=probe_media or media.probe_media,
               fit_scale=media.fit_scale)


def run(args, *, rpc, require_panel, parse_track, probe_media, fit_scale) -> dict:
    """The 0.3.3 entry point: `args` carries plan, dry_run, media_root, bin, no_bin, only, overwrite,
    notes_as_markers, report (and optionally any_sequence); the collaborators are injected."""
    plan_path = Path(args.plan).expanduser().resolve()
    plan = _read_plan(plan_path)
    commands = plan.get("commands") or []
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        commands = [c for c in commands if c.get("id") in wanted]

    media_root = Path(args.media_root).expanduser() if args.media_root else Path(plan.get("media_root") or ".")
    if not media_root.is_absolute():
        media_root = (plan_path.parent / media_root).resolve()
    bin_name = None if args.no_bin else (args.bin or plan.get("sequence") or None)
    any_sequence = getattr(args, "any_sequence", False)

    require_panel(["get_project", "get_sequence", "list_items", "list_project_items", "import_media", "insert_clip"]
                  + (["set_scale"] if any(c.get("scale") is not None for c in commands) else [])
                  + (["add_marker"] if args.notes_as_markers else []))
    seq = rpc("get_sequence")
    fps = framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or float(plan.get("fps") or 30))
    frame = 1.0 / fps
    timeline = rpc("list_items", {"kinds": ["video", "audio"]})
    project_items = rpc("list_project_items").get("items", [])

    warnings = []
    if plan.get("sequence") and plan["sequence"] != seq.get("name"):
        mismatch = f'Plan targets sequence "{plan["sequence"]}" but the active one is "{seq.get("name")}".'
        if not args.dry_run and not any_sequence:
            raise BridgeError(mismatch + " Activate that sequence, or pass --any-sequence; nothing was changed.",
                              code="SEQUENCE_MISMATCH", outcome="not_committed")
        warnings.append(mismatch + ("" if any_sequence else " A real run will refuse unless --any-sequence is given."))
    if plan.get("fps") and abs(float(plan["fps"]) - fps) > 0.01:
        warnings.append(f"Plan says {plan['fps']} fps; the sequence is {fps} fps. Seconds still apply; "
                        "times snap to the sequence's frames.")

    # occupancy: existing clips per track, plus what this plan has claimed so far
    def spans(track):
        return [(i["start"], i["end"], i["name"]) for i in track["items"]
                if i.get("start") is not None and i.get("end") is not None]

    occ = {("video", t["index"]): spans(t) for t in timeline.get("video", [])}
    occ.update({("audio", t["index"]): spans(t) for t in timeline.get("audio", [])})
    n_video, n_audio = len(timeline.get("video", [])), len(timeline.get("audio", []))

    by_path = {}
    for it in project_items:
        by_path.setdefault(_norm(it.get("mediaPath")), []).append(it)

    steps = []
    for c in commands:
        kind = c.get("command")
        cid = c.get("id") or f"#{len(steps) + 1}"
        step = {"id": cid, "command": kind}
        steps.append(step)
        at = c.get("timeline_at")

        if kind == "note" or (kind == "insert_clip" and c.get("src") in (None, "")):
            step["status"] = "marker" if args.notes_as_markers else ("note" if kind == "note" else "manual")
            step["at"] = at
            step["text"] = c.get("note") or c.get("vo") or json.dumps(c.get("text"), ensure_ascii=False)
            if kind != "note":
                step["why"] = "src is null — a graphic built by hand in Premiere"
            continue
        if kind != "insert_clip":
            step.update(status="error", why=f'Unknown command "{kind}"')
            continue

        try:
            vkind, vidx = parse_track(c.get("track", "V1"))
        except ValueError as e:
            step.update(status="error", why=str(e))
            continue
        if vkind != "video" or vidx >= n_video:
            step.update(status="error", why=f'Track {c.get("track")} is not a video track on this sequence '
                                            f"({n_video} video tracks).")
            continue

        media_file = (media_root / c["src"]).resolve()
        step["media"] = str(media_file)
        if not media_file.is_file():
            step.update(status="missing-media", why=f"File not found: {media_file}")
            continue

        src_in = float(c.get("src_in") or 0.0)
        if c.get("src_out") is not None:
            dur = float(c["src_out"]) - src_in
            if c.get("duration") is not None and abs(float(c["duration"]) - dur) > frame:
                warnings.append(f"{cid}: duration {c['duration']}s ≠ src_out−src_in {dur:.3f}s; using the source range.")
        elif c.get("duration") is not None:
            dur = float(c["duration"])
        else:
            step.update(status="error", why="Needs src_out or duration")
            continue
        if at is None or dur <= 0:
            step.update(status="error", why="Needs timeline_at and a positive duration")
            continue

        # Snap both edges to the frame grid (not start + rounded duration), so clips
        # that butt up in the plan still share a boundary frame after snapping.
        start_f = round(float(at) * fps)
        end_f = max(start_f + 1, round((float(at) + dur) * fps))
        at_s, dur_s = start_f / fps, (end_f - start_f) / fps
        step.update(track=f"V{vidx + 1}", at=round(at_s, 4), duration=round(dur_s, 4),
                    srcIn=src_in, srcOut=round(src_in + dur_s, 4))
        step["_at"], step["_out"] = at_s, src_in + dur_s  # exact: a rounded frame boundary loses the frame
        if abs(at_s - float(at)) > 1e-4:
            step["snapped"] = f"{at} -> {at_s:.4f}"

        info = probe_media(str(media_file))
        has_audio = info.get("hasAudio", True)  # unknown → assume audio, route it safely

        # A range over the whole file goes in as the whole clip. Its real length depends on
        # how the item is interpreted, so make room for the longest possible one.
        span_end, durations = at_s + dur_s, [dur_s]
        if framegrid.covers_whole_media(src_in, src_in + dur, info.get("duration"), fps):
            extents = framegrid.whole_extents(info, fps)
            longest_end = at_s + math.ceil(max(extents) * fps - 1e-6) / fps if extents else None
            tail = [] if longest_end is None or longest_end <= span_end + frame / 2 else \
                [n for (s, e, n) in occ.get(("video", vidx), []) if _overlap(span_end, longest_end, s, e, frame / 2)]
            if longest_end is None:
                step["endClampRisk"] = "media length unknown (ffprobe?); placing the range, which may end a frame short"
            elif tail:
                step["endClampRisk"] = (f"the whole clip could run into {', '.join(tail)}; placing the range, "
                                        "which may end a frame short")
            else:
                span_end = max(span_end, longest_end)
                durations = extents
                step["placedWhole"] = True
                step["wholeLengths"] = [round(x, 4) for x in extents]

        clash = [n for (s, e, n) in occ.get(("video", vidx), []) if _overlap(at_s, span_end, s, e, frame / 2)]
        if clash and not args.overwrite:
            step.update(status="conflict", why=f"V{vidx + 1} is occupied there by: {', '.join(clash)}")
            continue
        if clash:
            step["overwrites"] = clash
        if c.get("atrack"):
            try:
                akind, aidx = parse_track(c["atrack"])
            except ValueError as e:
                step.update(status="error", why=str(e))
                continue
            if akind != "audio" or aidx >= n_audio:
                step.update(status="error", why=f'atrack {c["atrack"]} is not an audio track on this sequence')
                continue
        else:
            free = [i for i in reversed(range(n_audio))
                    if not any(_overlap(at_s, span_end, s, e, frame / 2) for (s, e, _) in occ.get(("audio", i), []))]
            if has_audio and not free:
                step.update(status="error", why="Clip has audio and no audio track is free over this range")
                continue
            aidx = free[0] if free else n_audio - 1
        step["atrack"] = f"A{aidx + 1}" if has_audio else f"A{aidx + 1} (no audio in file)"

        if c.get("scale") is not None:
            s = c["scale"]
            if s in ("fit", "fill"):
                step["scale"] = fit_scale(s, info, seq)
                if step["scale"] is None:
                    step.update(status="error", why=f'scale "{s}" needs the media size (install ffprobe) — or give a percent')
                    continue
                step["scaleMode"] = s
            else:
                try:
                    step["scale"] = float(s)
                except (TypeError, ValueError):
                    step.update(status="error", why=f'scale {s!r} is not "fit", "fill" or a percent')
                    continue

        hits = by_path.get(_norm(str(media_file)), [])
        if hits:  # placed by bin + name, so a same-named item with other media can't be picked instead
            step["item"], step["_bin"] = hits[0]["name"], hits[0].get("bin") or ""
        else:
            step["import"] = True

        occ.setdefault(("video", vidx), []).append((at_s, span_end, cid))
        if has_audio:
            occ.setdefault(("audio", aidx), []).append((at_s, span_end, cid))
        step["_v"], step["_a"], step["_hasAudio"], step["_durs"] = vidx, aidx, has_audio, durations
        step["_audioKnown"] = "hasAudio" in info
        step["status"] = "planned"

    report = {"plan": str(plan_path), "sequence": seq.get("name"), "fps": fps,
              "frameSize": [seq.get("width"), seq.get("height")], "mediaRoot": str(media_root),
              "bin": bin_name, "dryRun": bool(args.dry_run), "warnings": warnings}

    to_import = sorted({s["media"] for s in steps if s.get("status") == "planned" and s.get("import")})
    report["imports"] = to_import

    if not args.dry_run:
        _execute(args, plan, steps, to_import, bin_name, rpc, report)

    for s in steps:
        for k in ("_v", "_a", "_hasAudio", "_audioKnown", "_bin", "_durs", "_at", "_out"):
            s.pop(k, None)
    counts = {}
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    report["summary"] = counts
    report["steps"] = steps
    report["ok"] = not any(s["status"] in BAD for s in steps)
    if args.report:
        out = Path(args.report).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"))
        report["reportPath"] = str(out)
    return report


def _abort_rest(steps, start, why):
    for s in steps[start:]:
        if s.get("status") in ("marker", "planned"):
            s.update(status="aborted", why=f"Not run: {why}.")


def _execute(args, plan, steps, to_import, bin_name, rpc, report):
    label_root = f"plan {plan.get('sequence') or Path(args.plan).stem}"
    where = (rpc("get_project").get("name"), report["sequence"])

    def moved() -> str | None:
        """The project or active sequence changed under the plan (a click in the UI, say)."""
        now = (rpc("get_project").get("name"), rpc("get_sequence").get("name"))
        return None if now == where else f'the active project/sequence changed to "{now[0]}" / "{now[1]}"'

    if to_import:
        params = {"paths": to_import}
        if bin_name:
            params["bin"] = bin_name
        try:
            rpc("import_media", params)
        except Exception as e:  # the other steps can still run
            for s in steps:
                if s.get("status") == "planned" and s.get("import"):
                    s.update(status="failed", why=f"Import failed: {e}")
        fresh = rpc("list_project_items").get("items", [])
        by_path = {}
        for it in fresh:
            by_path.setdefault(_norm(it.get("mediaPath")), []).append(it)
        for s in steps:
            if s.get("status") == "planned" and s.get("import"):
                hits = by_path.get(_norm(s["media"]), [])
                if not hits:
                    s.update(status="failed", why="Imported, but no project item points at this file afterwards")
                else:
                    s["item"], s["_bin"] = hits[-1]["name"], hits[-1].get("bin") or ""

    for n, s in enumerate(steps):
        if s.get("status") not in ("marker", "planned"):
            continue
        change = moved()
        if change:
            _abort_rest(steps, n, change)
            report["aborted"] = change
            break
        if s["status"] == "marker":
            try:
                rpc("add_marker", {"atSeconds": float(s["at"] or 0), "name": s["id"],
                                   "comments": s.get("text") or "", "undoLabel": f"{label_root}: marker {s['id']}"})
                s["status"] = "marked"
            except Exception as e:  # keep going; report it
                s.update(status="failed", why=str(e))
            continue
        params = {"itemName": s["item"], "binPath": s.get("_bin", ""), "atSeconds": s["_at"],
                  "videoTrack": s["_v"], "audioTrack": s["_a"], "mode": "overwrite",
                  "undoLabel": f"{label_root}: {s['id']}"}
        if not s.get("placedWhole"):
            params.update(inSeconds=s["srcIn"], outSeconds=s["_out"])
        try:
            rpc("insert_clip", params)
            s["status"] = "placed"
        except Exception as e:
            s.update(status="failed", why=str(e))
            if getattr(e, "outcome", None) not in (None, "not_committed"):  # it may have landed: don't build on it
                _abort_rest(steps, n + 1, f"step {s['id']} may or may not have been placed")
                report["aborted"] = f"step {s['id']}: outcome {e.outcome}"
                break
            continue
        if s.get("scale") is not None:
            mid = s["at"] + s["duration"] / 2
            try:
                rpc("set_scale", {"videoTrack": s["_v"], "fromSeconds": mid, "toSeconds": mid, "value": s["scale"],
                                  "undoLabel": f"{label_root}: {s['id']} scale {s['scale']}%"})
            except Exception as e:
                s["scaleError"] = str(e)

    # Verify against the real timeline, both legs, to half a frame: a clip one frame
    # short is a real defect (a gap or a flash), not rounding.
    after = rpc("list_items", {"kinds": ["video", "audio"]})
    video = {t["index"]: t["items"] for t in after.get("video", [])}
    audio = {t["index"]: t["items"] for t in after.get("audio", [])}
    half = 0.5 / report["fps"]

    def lands(items_, s, check_length):
        return any(i.get("start") is not None and abs(i["start"] - s["at"]) <= half and i.get("projectItem") == s["item"]
                   and (not check_length or any(abs((i["end"] - i["start"]) - d) <= half for d in s["_durs"]))
                   for i in items_)

    for s in steps:
        if s.get("status") != "placed":
            continue
        legs = [] if lands(video.get(s["_v"], []), s, True) else ["video"]
        if s["_hasAudio"] and s["_audioKnown"] and not lands(audio.get(s["_a"], []), s, False):
            legs.append(f"audio (A{s['_a'] + 1})")
        if legs:
            s.update(status="unverified", why=f"Not found at the expected position/length after placing: "
                                              f"{' and '.join(legs)} leg")
