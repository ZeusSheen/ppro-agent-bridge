#!/usr/bin/env python3
"""
ppro — command-line client for ppro-agent-bridge.

This is the surface an AI agent (or a human) talks to. Always prints one JSON object to
stdout; exit code 0 on success, 1 on failure.

Usage:
  python ppro.py ping
  python ppro.py get_project
  python ppro.py get_sequence                      # frame size, fps, duration, tracks
  python ppro.py list_items [--track V3]           # clips on the timeline as JSON
  python ppro.py list_project_items                # project panel clips + media paths
  python ppro.py export_frame  --dir /tmp/frames --at 12.5
  python ppro.py export_frames --dir /tmp/frames --at 4,18.5,31.6 --sheet
  python ppro.py export_otio   --output /tmp/timeline.otio
  python ppro.py import_media  --path a.mp4 --path b.mov [--bin "B-roll"]
  python ppro.py apply_plan    plan.json [--dry-run]
  python ppro.py set_scale     --track V3 --at 4.0 --fill
  python ppro.py set_fps       --item g01.mov --fps 59.94
  python ppro.py add_marker    --at 9.0 --name "Fact check" --comment "..." --color red
  python ppro.py remove_items  --track V3 --from 35 --to 53 [--dry-run]

Requires: Python 3.10+, `pip install websockets`, broker running, Premiere
open with the Agent Bridge panel loaded. Optional: ffprobe on PATH (fit/fill
scaling, audio detection), Pillow (contact sheets).
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

try:
    import websockets
except ImportError:
    print(json.dumps({"ok": False, "error": "Missing dependency. Run: pip install websockets"}))
    sys.exit(1)

CLI_VERSION = "0.3.2"
BROKER_URL = os.environ.get("PPRO_BRIDGE_URL", "ws://127.0.0.1:8017")
TIMEOUT = 35

MARKER_COLORS = {"green": 0, "red": 1, "purple": 2, "magenta": 2, "orange": 3,
                 "yellow": 4, "white": 5, "blue": 6, "cyan": 7}


class BridgeError(Exception):
    """A panel or broker failure, carrying the reply's error text."""


async def call(action: str, params: dict) -> dict:
    msg_id = str(uuid.uuid4())
    try:
        async with websockets.connect(BROKER_URL, open_timeout=5, max_size=16 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"role": "controller"}))
            await ws.send(json.dumps({"id": msg_id, "action": action, "params": params}))
            raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT)
            return json.loads(raw)
    except (ConnectionRefusedError, OSError):
        return {"ok": False, "error": f"Cannot reach broker at {BROKER_URL}. Is broker.py running?"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Timed out waiting for reply."}


def rpc(action: str, params: dict | None = None) -> dict:
    """Call the panel and return `data`, raising BridgeError on failure."""
    reply = asyncio.run(call(action, params or {}))
    if not reply.get("ok"):
        raise BridgeError(f"{action}: {reply.get('error')}")
    return reply.get("data") or {}


def parse_track(spec) -> tuple[str, int]:
    """"V3" → ("video", 2), "A1" → ("audio", 0); a bare int means a video track."""
    s = str(spec).strip().upper()
    if s.isdigit():
        return "video", int(s)
    if len(s) >= 2 and s[0] in "VA" and s[1:].isdigit() and int(s[1:]) >= 1:
        return ("video" if s[0] == "V" else "audio"), int(s[1:]) - 1
    raise ValueError(f'Bad track "{spec}" — use V1, V2, A1, …')


def require_panel(actions: list[str]) -> dict:
    """Ping and make sure the loaded panel knows the given actions (version handshake)."""
    info = rpc("ping")
    missing = [a for a in actions if a not in (info.get("actions") or [])]
    if missing:
        raise BridgeError(
            f"The loaded panel ({info.get('plugin')}) lacks {', '.join(missing)}. "
            f"This CLI is {CLI_VERSION}: load/reload the Agent Bridge {CLI_VERSION} panel in UXP Developer Tool.")
    return info


def probe_media(path: str) -> dict:
    """Width/height/hasAudio via ffprobe; {} when ffprobe is unavailable or fails."""
    if not path or not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", path],
            capture_output=True, text=True, timeout=30).stdout
        streams = json.loads(out or "{}").get("streams") or []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width")), None)
    info = {"hasAudio": any(s.get("codec_type") == "audio" for s in streams)}
    if video:
        info["width"], info["height"] = int(video["width"]), int(video["height"])
    return info


def fit_scale(mode: str, media: dict, seq: dict) -> float | None:
    """Percent that fits (letterbox) or fills (crop) the media into the sequence frame."""
    if not media.get("width") or not seq.get("width"):
        return None
    sx, sy = seq["width"] / media["width"], seq["height"] / media["height"]
    return round((min(sx, sy) if mode == "fit" else max(sx, sy)) * 100, 3)


def resolve_exported_frame(reply: dict) -> dict:
    """Work around Adobe's double-extension bug and async file writes.

    Polls for candidate filenames for up to ~5s. If still missing, includes
    the directory's actual contents so the real output name is discoverable.
    """
    import time
    data = reply.get("data") or {}
    directory = data.get("dir")
    candidates = data.get("candidates") or []
    if not directory:
        return reply

    deadline = time.monotonic() + 5.0
    stem_hits = []
    while time.monotonic() < deadline:
        for name in candidates:
            p = Path(directory) / name
            if p.is_file():
                data["resolvedPath"] = str(p)
                reply["data"] = data
                return reply
        # fuzzy pass: anything sharing the stem (Premiere may rename)
        stem = candidates[0].split(".")[0] if candidates else ""
        if stem:
            stem_hits = [f.name for f in Path(directory).glob(f"{stem}*") if f.is_file()]
            if len(stem_hits) == 1:
                data["resolvedPath"] = str(Path(directory) / stem_hits[0])
                data["note"] = "Resolved by stem match; Premiere altered the filename."
                reply["data"] = data
                return reply
        time.sleep(0.5)

    data["resolvedPath"] = None
    try:
        data["dirListing"] = sorted(f.name for f in Path(directory).iterdir() if f.is_file())[:50]
    except OSError:
        data["dirListing"] = None
    data["note"] = ("No matching file after 5s of polling. Check dirListing for "
                    "the actual output name, or the sequence may be shorter than "
                    "the requested --at time.")
    reply["data"] = data
    return reply


def export_one_frame(directory: Path, at: float | None, name: str | None, width=None, height=None) -> dict:
    if name is None:
        name = f"frame_{at:.3f}s.png" if at is not None else "frame_playhead.png"
    params = {"dir": str(directory), "name": name}
    if width and height:
        params["width"], params["height"] = width, height
    if at is not None:
        params["atSeconds"] = at
    return resolve_exported_frame(asyncio.run(call("export_frame", params)))


def make_contact_sheet(frames: list[tuple[float, str]], out: Path, cols: int, thumb_w: int) -> str | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    tiles = []
    for at, path in frames:
        im = Image.open(path).convert("RGB")
        h = round(im.height * thumb_w / im.width)
        im = im.resize((thumb_w, h))
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, thumb_w, 18), fill=(0, 0, 0))
        d.text((5, 3), f"{at:.2f}s", fill=(255, 220, 0))
        tiles.append(im)
    if not tiles:
        return None
    th = max(t.height for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (thumb_w * min(cols, len(tiles)), th * rows), (20, 20, 20))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * thumb_w, (i // cols) * th))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    return str(out)


# ---------------------------------------------------------------- commands

def cmd_ping(args) -> dict:
    data = rpc("ping")
    data["cliVersion"] = CLI_VERSION
    data["compatible"] = data.get("version") == CLI_VERSION
    if not data["compatible"]:
        data["warning"] = (f"Panel is {data.get('plugin')}, CLI is {CLI_VERSION}. "
                           "Reload the Agent Bridge panel in UXP Developer Tool.")
    return data


def cmd_list_items(args) -> dict:
    kinds = ["video", "audio"] if args.kind == "all" else [args.kind]
    if args.track:
        kind, idx = parse_track(args.track)
        data = rpc("list_items", {"kinds": [kind]})
        data[kind] = [t for t in data.get(kind, []) if t["index"] == idx]
        return data
    return rpc("list_items", {"kinds": kinds})


def cmd_inspect_items(args) -> dict:
    params = {}
    if args.track:
        idxs = []
        for spec in args.track:
            kind, idx = parse_track(spec)
            if kind != "video":
                raise ValueError("inspect_items reads video tracks (V1, V2, …)")
            idxs.append(idx)
        params["videoTracks"] = idxs
    if args.from_s is not None:
        params["fromSeconds"] = args.from_s
    if args.to_s is not None:
        params["toSeconds"] = args.to_s
    data = rpc("inspect_items", params)
    if args.output:
        out = Path(args.output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        n = sum(len(t["clips"]) for t in data["tracks"])
        return {"savedTo": str(out), "clips": n, "transitions": sum(len(t["transitions"]) for t in data["tracks"])}
    return data


def cmd_export_frames(args) -> dict:
    out_dir = Path(args.dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    times = [float(t) for t in args.at.split(",") if t.strip()]
    frames, results = [], []
    for t in times:
        r = export_one_frame(out_dir, t, None, args.width, args.height)
        path = (r.get("data") or {}).get("resolvedPath") if r.get("ok") else None
        results.append({"at": t, "path": path, "error": None if r.get("ok") else r.get("error")})
        if path:
            frames.append((t, path))
    data = {"frames": results}
    if args.sheet is not None:
        sheet_path = Path(args.sheet).expanduser().resolve() if args.sheet else out_dir / "contact-sheet.jpg"
        made = make_contact_sheet(frames, sheet_path, args.cols, args.thumb_width)
        data["sheet"] = made or "skipped (pip install pillow for contact sheets)"
    if any(r["error"] or not r["path"] for r in results):
        raise BridgeError(json.dumps(data, ensure_ascii=False))
    return data


def cmd_set_scale(args) -> dict:
    kind, idx = parse_track(args.track)
    if kind != "video":
        raise ValueError("set_scale works on video tracks (V1, V2, …)")
    if args.at is None and args.from_s is None:
        raise ValueError("Give --at <sec> or --from <sec> [--to <sec>]")
    frm = args.at if args.at is not None else args.from_s
    to = args.at if args.at is not None else (args.to_s if args.to_s is not None else args.from_s)
    base = {"videoTrack": idx, "fromSeconds": frm, "toSeconds": to}

    if args.value is not None:
        params = dict(base, value=args.value, dryRun=args.dry_run)
        if args.label:
            params["undoLabel"] = args.label
        return rpc("set_scale", params)

    mode = "fit" if args.fit else "fill"
    seq = rpc("get_sequence")
    preview = rpc("set_scale", dict(base, dryRun=True))
    out = {"mode": mode, "dryRun": args.dry_run, "sequence": [seq.get("width"), seq.get("height")], "clips": []}
    for clip in preview["clips"]:
        media = probe_media(clip.get("mediaPath"))
        value = fit_scale(mode, media, seq)
        row = {"name": clip["name"], "start": clip["start"], "media": [media.get("width"), media.get("height")],
               "scaleBefore": clip.get("scaleBefore"), "value": value}
        if value is None:
            row["error"] = "Could not read media size (is ffprobe on PATH?)"
        elif not args.dry_run:
            mid = (clip["start"] + clip["end"]) / 2
            label = args.label or f"Agent: {mode} {clip['name']} ({value}%)"
            rpc("set_scale", {"videoTrack": idx, "fromSeconds": mid, "toSeconds": mid, "value": value, "undoLabel": label})
            row["applied"] = True
        out["clips"].append(row)
    if any("error" in r for r in out["clips"]):
        raise BridgeError(json.dumps(out, ensure_ascii=False))
    return out


def cmd_add_marker(args) -> dict:
    params = {"atSeconds": args.at, "name": args.name, "comments": args.comment or "",
              "durationSeconds": args.duration, "type": args.type}
    if args.color is not None:
        c = args.color.lower()
        params["colorIndex"] = int(c) if c.isdigit() else MARKER_COLORS.get(c)
        if params["colorIndex"] is None:
            raise ValueError(f"Unknown colour {args.color}; use {', '.join(MARKER_COLORS)} or 0–7")
    if args.label:
        params["undoLabel"] = args.label
    return rpc("add_marker", params)


def cmd_remove_items(args) -> dict:
    vts, ats = [], []
    for spec in args.track:
        kind, idx = parse_track(spec)
        (vts if kind == "video" else ats).append(idx)
    params = {"videoTracks": vts, "audioTracks": ats, "fromSeconds": args.from_s, "toSeconds": args.to_s,
              "mode": args.mode, "ripple": args.ripple, "dryRun": args.dry_run}
    if args.label:
        params["undoLabel"] = args.label
    return rpc("remove_items", params)


def media_duration(path: str) -> float | None:
    if not path or not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def cmd_replace_clip(args) -> dict:
    import replace  # sibling module
    kind, idx = parse_track(args.track)
    if kind != "video":
        raise ValueError("replace_clip works on video tracks (V1, V2, …)")
    atrack = None
    if args.atrack:
        akind, atrack = parse_track(args.atrack)
        if akind != "audio":
            raise ValueError("--atrack must be an audio track (A1, A2, …)")
    require_panel(["inspect_items", "set_params", "add_transition", "remove_items", "insert_clip"])
    return replace.replace_clip(rpc, idx, args.at, args.with_item, src_in=args.in_s, src_out=args.out_s,
                                atrack=atrack, keep_effects=not args.no_effects,
                                keep_transitions=not args.no_transitions, dry_run=args.dry_run,
                                label=args.label or "Agent replace", media_duration=media_duration)


def cmd_apply_plan(args) -> dict:
    import edit_plan  # sibling module
    return edit_plan.run(args, rpc=rpc, require_panel=require_panel, parse_track=parse_track,
                         probe_media=probe_media, fit_scale=fit_scale)


def main() -> int:
    parser = argparse.ArgumentParser(prog="ppro", description="Agent Bridge CLI for Premiere Pro")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("ping", help="Liveness check + version handshake")
    sub.add_parser("get_project", help="Active project name / path / active sequence")
    sub.add_parser("get_sequence", help="Active sequence: frame size, fps, duration, tracks, player position")

    pli = sub.add_parser("list_items", help="Clips on the active sequence's tracks, as JSON")
    pli.add_argument("--kind", choices=["video", "audio", "all"], default="all")
    pli.add_argument("--track", default=None, help="Only this track, e.g. V3 or A1")

    pin = sub.add_parser("inspect_items", help="Effect chains (params, keyframes) and transitions of timeline clips")
    pin.add_argument("--track", action="append", default=None, help="Video track, e.g. V5 (repeatable; default all)")
    pin.add_argument("--from", dest="from_s", type=float, default=None)
    pin.add_argument("--to", dest="to_s", type=float, default=None)
    pin.add_argument("--output", default=None, help="Write the (large) result to a JSON file and print a summary")

    sub.add_parser("list_project_items", help="Project panel clips with bin and media path")

    pf = sub.add_parser("export_frame", help="Export one frame of the active sequence")
    pf.add_argument("--dir", required=True, help="Output directory (created if missing)")
    pf.add_argument("--name", default=None, help="Output file name (default: frame_<seconds>.png)")
    pf.add_argument("--at", type=float, default=None, help="Sequence time in seconds (default: player position)")
    pf.add_argument("--width", type=int, default=None, help="Default: the sequence frame width")
    pf.add_argument("--height", type=int, default=None, help="Default: the sequence frame height")

    pfs = sub.add_parser("export_frames", help="Export several frames, optionally as a contact sheet")
    pfs.add_argument("--dir", required=True)
    pfs.add_argument("--at", required=True, help="Comma-separated sequence seconds, e.g. 4,18.5,31.6")
    pfs.add_argument("--width", type=int, default=None)
    pfs.add_argument("--height", type=int, default=None)
    pfs.add_argument("--sheet", nargs="?", const="", default=None,
                     help="Also build a contact sheet (default path: <dir>/contact-sheet.jpg)")
    pfs.add_argument("--cols", type=int, default=4)
    pfs.add_argument("--thumb-width", type=int, default=300)

    po = sub.add_parser("export_otio", help="Export active sequence as OpenTimelineIO")
    po.add_argument("--output", required=True, help="Full output path ending in .otio")

    sub.add_parser("list_transcripts", help="List project clips and whether each has a transcript")

    pt = sub.add_parser("export_transcript", help="Export a clip's transcript as Adobe Transcript JSON")
    pt.add_argument("--item", default=None, help="Clip name (default: first clip with a transcript)")
    pt.add_argument("--output", required=True, help="Where to save the transcript JSON")

    pit = sub.add_parser("import_transcript", help="Import an Adobe-format transcript JSON onto a clip")
    pit.add_argument("--item", required=True, help="Clip name, exact match")
    pit.add_argument("--file", required=True, help="Transcript JSON file (e.g. from transcribe.py)")

    pi = sub.add_parser("import_media", help="Import media files into the project root or a bin")
    pi.add_argument("--path", action="append", required=True, help="File path (repeatable)")
    pi.add_argument("--bin", default=None, help="Top-level bin name (created if missing)")

    pc = sub.add_parser("insert_clip", help="Place a clip (or a source range of it) on the timeline")
    pc.add_argument("--item", required=True, help="Project item name, exact match")
    pc.add_argument("--at", type=float, default=0.0, help="Sequence time in seconds (default 0)")
    pc.add_argument("--vtrack", type=int, default=0, help="Video track, 0-based (0 = V1)")
    pc.add_argument("--atrack", type=int, default=0, help="Audio track, 0-based (0 = A1)")
    pc.add_argument("--mode", choices=["overwrite", "insert"], default="overwrite")
    pc.add_argument("--in", dest="in_s", type=float, default=None, help="Source in point, seconds")
    pc.add_argument("--out", dest="out_s", type=float, default=None, help="Source out point, seconds")
    pc.add_argument("--label", default=None, help="Undo history label")

    pp = sub.add_parser("apply_plan", help="Execute a JSON edit plan (insert_clip / note commands)")
    pp.add_argument("plan", help="Plan JSON file")
    pp.add_argument("--dry-run", action="store_true", help="Resolve and check everything, change nothing")
    pp.add_argument("--media-root", default=None, help="Override the plan's media_root")
    pp.add_argument("--bin", default=None, help="Bin for imported media (default: the plan's sequence name)")
    pp.add_argument("--no-bin", action="store_true", help="Import into the project root")
    pp.add_argument("--only", default=None, help="Comma-separated command ids to run")
    pp.add_argument("--overwrite", action="store_true", help="Place even where the target track is occupied")
    pp.add_argument("--notes-as-markers", action="store_true",
                    help="Turn note commands and src-less graphics into sequence markers")
    pp.add_argument("--report", default=None, help="Also write the JSON report to this file")

    prc = sub.add_parser("replace_clip", help="Replace the clip under a time with another item, keeping effects + transitions")
    prc.add_argument("--track", required=True, help="Video track, e.g. V5")
    prc.add_argument("--at", type=float, required=True, help="Any sequence time inside the clip to replace")
    prc.add_argument("--with", dest="with_item", required=True, help="Project item name to put there")
    prc.add_argument("--in", dest="in_s", type=float, default=None, help="Source in (default: the old clip's)")
    prc.add_argument("--out", dest="out_s", type=float, default=None, help="Source out (default: in + old length)")
    prc.add_argument("--atrack", default=None, help="Audio track for the new item's audio (default: last)")
    prc.add_argument("--no-effects", action="store_true", help="Don't carry effect params over")
    prc.add_argument("--no-transitions", action="store_true", help="Don't carry transitions over")
    prc.add_argument("--dry-run", action="store_true")
    prc.add_argument("--label", default=None)

    sub.add_parser("list_transition_names", help="Video transition match names available in Premiere")

    ps = sub.add_parser("set_scale", help="Set Motion > Scale on clips of one video track")
    ps.add_argument("--track", required=True, help="Video track, e.g. V3")
    ps.add_argument("--at", type=float, default=None, help="Target the clip under this sequence time")
    ps.add_argument("--from", dest="from_s", type=float, default=None)
    ps.add_argument("--to", dest="to_s", type=float, default=None)
    g = ps.add_mutually_exclusive_group(required=True)
    g.add_argument("--value", type=float, help="Scale percent, e.g. 50")
    g.add_argument("--fit", action="store_true", help="Fit inside the frame (letterbox), per clip")
    g.add_argument("--fill", action="store_true", help="Fill the frame (crop), per clip")
    ps.add_argument("--dry-run", action="store_true")
    ps.add_argument("--label", default=None)

    pfp = sub.add_parser("set_fps", help="Interpret footage: override project items' frame rate")
    pfp.add_argument("--item", action="append", required=True, help="Project item name (repeatable)")
    pfp.add_argument("--fps", type=float, required=True)
    pfp.add_argument("--label", default=None)

    pm = sub.add_parser("add_marker", help="Add a sequence marker")
    pm.add_argument("--at", type=float, required=True)
    pm.add_argument("--name", required=True)
    pm.add_argument("--comment", default="")
    pm.add_argument("--duration", type=float, default=0.0)
    pm.add_argument("--type", default="Comment", choices=["Comment", "Chapter", "Segmentation", "WebLink"])
    pm.add_argument("--color", default=None, help=f"{', '.join(MARKER_COLORS)} or 0–7")
    pm.add_argument("--label", default=None)

    pr = sub.add_parser("remove_items", help="Remove clips from tracks within a time range")
    pr.add_argument("--track", action="append", required=True, help="V3, A2, … (repeatable)")
    pr.add_argument("--from", dest="from_s", type=float, required=True)
    pr.add_argument("--to", dest="to_s", type=float, required=True)
    pr.add_argument("--mode", choices=["inside", "overlap"], default="inside",
                    help="inside (default): only clips entirely in the range")
    pr.add_argument("--ripple", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--label", default=None)

    args = parser.parse_args()

    # v0.3 commands: build the reply here, keep the one-JSON-object contract.
    composite = {"ping": cmd_ping, "list_items": cmd_list_items, "inspect_items": cmd_inspect_items,
                 "export_frames": cmd_export_frames,
                 "set_scale": cmd_set_scale, "add_marker": cmd_add_marker,
                 "remove_items": cmd_remove_items, "apply_plan": cmd_apply_plan,
                 "replace_clip": cmd_replace_clip}
    if args.action in composite:
        try:
            reply = {"ok": True, "data": composite[args.action](args)}
        except (BridgeError, ValueError, OSError) as e:
            msg = str(e)
            try:
                reply = {"ok": False, "error": "see data", "data": json.loads(msg)}
            except json.JSONDecodeError:
                reply = {"ok": False, "error": msg}
        if args.action == "apply_plan" and reply.get("ok"):
            reply["ok"] = reply["data"].get("ok", True)
        print(json.dumps(reply, indent=2, ensure_ascii=False))
        return 0 if reply.get("ok") else 1

    params: dict = {}
    if args.action == "export_frame":
        out_dir = Path(args.dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        reply = export_one_frame(out_dir, args.at, args.name, args.width, args.height)
        print(json.dumps(reply, indent=2, ensure_ascii=False))
        return 0 if reply.get("ok") else 1
    elif args.action == "export_otio":
        out = Path(args.output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        params = {"outputPath": str(out)}
    elif args.action == "export_transcript":
        if args.item:
            params["itemName"] = args.item
    elif args.action == "import_transcript":
        f = Path(args.file).expanduser().resolve()
        if not f.is_file():
            print(json.dumps({"ok": False, "error": f"File not found: {f}"}))
            return 1
        params = {"itemName": args.item, "transcriptJson": f.read_text(encoding="utf-8")}
    elif args.action == "import_media":
        paths = [Path(p).expanduser().resolve() for p in args.path]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            print(json.dumps({"ok": False, "error": "File(s) not found", "missing": missing}, ensure_ascii=False, indent=2))
            return 1
        params = {"paths": [str(p) for p in paths]}
        if args.bin:
            params["bin"] = args.bin
    elif args.action == "insert_clip":
        params = {"itemName": args.item, "atSeconds": args.at,
                  "videoTrack": args.vtrack, "audioTrack": args.atrack, "mode": args.mode}
        if (args.in_s is None) != (args.out_s is None):
            print(json.dumps({"ok": False, "error": "--in and --out must be given together."}))
            return 1
        if args.in_s is not None:
            params["inSeconds"] = args.in_s
            params["outSeconds"] = args.out_s
        if args.label:
            params["undoLabel"] = args.label
    elif args.action == "set_fps":
        params = {"itemNames": args.item, "fps": args.fps}
        if args.label:
            params["undoLabel"] = args.label

    reply = asyncio.run(call(args.action, params))

    if args.action == "export_transcript" and reply.get("ok"):
        data = reply.get("data") or {}
        raw = data.pop("transcriptJson", None)
        if raw:
            out = Path(args.output).expanduser().resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(raw, encoding="utf-8")
            data["savedTo"] = str(out)
            data["bytes"] = len(raw.encode("utf-8"))
            data["hint"] = "Run: python transcript_digest.py " + str(out)
        reply["data"] = data

    print(json.dumps(reply, indent=2, ensure_ascii=False))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    # Windows consoles default to a legacy code page; keep track names and paths readable.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
