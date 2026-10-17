"""
apply_plan — execute a JSON edit plan against Premiere's active sequence.

Plan format (what an editor or agent writes before cutting):

  {
    "sequence": "Main",                  # informational; warns if it differs
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

Times are seconds. `timeline_at` snaps to the sequence frame grid and the
placed duration to whole frames. Nothing is written until every command
resolves; `--dry-run` stops after that and prints what would happen.
"""

import json
import os
from pathlib import Path

BAD = {"conflict", "error", "missing-media", "failed", "unverified"}


def _norm(p: str | None) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""


def _overlap(a0, a1, b0, b1, tol) -> bool:
    return a0 < b1 - tol and b0 < a1 - tol


def run(args, *, rpc, require_panel, parse_track, probe_media, fit_scale) -> dict:
    plan_path = Path(args.plan).expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    commands = plan.get("commands") or []
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        commands = [c for c in commands if c.get("id") in wanted]

    media_root = Path(args.media_root).expanduser() if args.media_root else Path(plan.get("media_root") or ".")
    if not media_root.is_absolute():
        media_root = (plan_path.parent / media_root).resolve()
    bin_name = None if args.no_bin else (args.bin or plan.get("sequence") or None)

    require_panel(["get_sequence", "list_items", "list_project_items", "import_media", "insert_clip"]
                  + (["set_scale"] if any(c.get("scale") is not None for c in commands) else [])
                  + (["add_marker"] if args.notes_as_markers else []))
    seq = rpc("get_sequence")
    fps = seq.get("fps") or float(plan.get("fps") or 30)
    frame = 1.0 / fps
    timeline = rpc("list_items", {"kinds": ["video", "audio"]})
    project_items = rpc("list_project_items").get("items", [])

    warnings = []
    if plan.get("sequence") and plan["sequence"] != seq.get("name"):
        warnings.append(f'Plan targets sequence "{plan["sequence"]}" but the active one is "{seq.get("name")}".')
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
    names_to_paths = {}
    for it in project_items:
        names_to_paths.setdefault(it["name"], set()).add(_norm(it.get("mediaPath")))

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

        media = (media_root / c["src"]).resolve()
        step["media"] = str(media)
        if not media.is_file():
            step.update(status="missing-media", why=f"File not found: {media}")
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
        if abs(at_s - float(at)) > 1e-4:
            step["snapped"] = f"{at} -> {at_s:.4f}"

        clash = [n for (s, e, n) in occ.get(("video", vidx), []) if _overlap(at_s, at_s + dur_s, s, e, frame / 2)]
        if clash and not args.overwrite:
            step.update(status="conflict", why=f"V{vidx + 1} is occupied there by: {', '.join(clash)}")
            continue
        if clash:
            step["overwrites"] = clash

        info = probe_media(str(media))
        has_audio = info.get("hasAudio", True)  # unknown → assume audio, route it safely
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
                    if not any(_overlap(at_s, at_s + dur_s, s, e, frame / 2) for (s, e, _) in occ.get(("audio", i), []))]
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
                step["scale"] = float(s)

        hits = by_path.get(_norm(str(media)), [])
        if hits:
            step["item"] = hits[0]["name"]
            others = names_to_paths.get(step["item"], set()) - {_norm(str(media))}
            if others:
                step.update(status="error", why=f'Project item name "{step["item"]}" also belongs to other media; '
                                                "rename one so insert_clip can address it.")
                continue
        else:
            step["import"] = True

        occ.setdefault(("video", vidx), []).append((at_s, at_s + dur_s, cid))
        if has_audio:
            occ.setdefault(("audio", aidx), []).append((at_s, at_s + dur_s, cid))
        step["_v"], step["_a"], step["_hasAudio"] = vidx, aidx, has_audio
        step["status"] = "planned"

    report = {"plan": str(plan_path), "sequence": seq.get("name"), "fps": fps,
              "frameSize": [seq.get("width"), seq.get("height")], "mediaRoot": str(media_root),
              "bin": bin_name, "dryRun": bool(args.dry_run), "warnings": warnings}

    to_import = sorted({s["media"] for s in steps if s.get("status") == "planned" and s.get("import")})
    report["imports"] = to_import

    if not args.dry_run:
        _execute(args, plan, steps, to_import, bin_name, rpc, report)

    for s in steps:
        for k in ("_v", "_a", "_hasAudio"):
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
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        report["reportPath"] = str(out)
    return report


def _execute(args, plan, steps, to_import, bin_name, rpc, report):
    label_root = f"plan {plan.get('sequence') or Path(args.plan).stem}"

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
                    s["item"] = hits[-1]["name"]

    for s in steps:
        if s.get("status") == "marker":
            try:
                rpc("add_marker", {"atSeconds": float(s["at"] or 0), "name": s["id"],
                                   "comments": s.get("text") or "", "undoLabel": f"{label_root}: marker {s['id']}"})
                s["status"] = "marked"
            except Exception as e:  # keep going; report it
                s.update(status="failed", why=str(e))
            continue
        if s.get("status") != "planned":
            continue
        try:
            rpc("insert_clip", {"itemName": s["item"], "atSeconds": s["at"], "videoTrack": s["_v"],
                                "audioTrack": s["_a"], "mode": "overwrite",
                                "inSeconds": s["srcIn"], "outSeconds": s["srcOut"],
                                "undoLabel": f"{label_root}: {s['id']}"})
            s["status"] = "placed"
        except Exception as e:
            s.update(status="failed", why=str(e))
            continue
        if s.get("scale") is not None:
            mid = s["at"] + s["duration"] / 2
            try:
                rpc("set_scale", {"videoTrack": s["_v"], "fromSeconds": mid, "toSeconds": mid, "value": s["scale"],
                                  "undoLabel": f"{label_root}: {s['id']} scale {s['scale']}%"})
            except Exception as e:
                s["scaleError"] = str(e)

    # verify against the real timeline
    after = rpc("list_items", {"kinds": ["video"]})
    tracks = {t["index"]: t["items"] for t in after.get("video", [])}
    fps = report["fps"]
    for s in steps:
        if s.get("status") != "placed":
            continue
        ok = any(abs(i["start"] - s["at"]) <= 1.0 / fps and abs((i["end"] - i["start"]) - s["duration"]) <= 1.0 / fps
                 and i.get("projectItem") == s["item"] for i in tracks.get(s["_v"], []))
        s["status"] = "placed" if ok else "unverified"
        if not ok:
            s["why"] = "Not found on the timeline at the expected position after placing"
