#!/usr/bin/env python3
"""
ppro — command-line client for ppro-agent-bridge.

This is the surface an AI agent (or a human) talks to. Always prints one JSON object to
stdout; exit code 0 on success, 1 on failure.

Usage:
  python ppro.py ping
  python ppro.py get_project
  python ppro.py get_sequence
  python ppro.py export_frame --dir /tmp/frames --at 12.5 --width 640 --height 360
  python ppro.py export_frame --dir /tmp/frames            # at player position
  python ppro.py export_otio  --output /tmp/timeline.otio

Requires: Python 3.10+, `pip install websockets`, broker running, Premiere
open with the Claude Bridge panel loaded.
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

try:
    import websockets
except ImportError:
    print(json.dumps({"ok": False, "error": "Missing dependency. Run: pip install websockets"}))
    sys.exit(1)

BROKER_URL = "ws://127.0.0.1:8017"
TIMEOUT = 35


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


def main() -> int:
    parser = argparse.ArgumentParser(prog="ppro", description="Claude Bridge CLI for Premiere Pro")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("ping", help="Liveness check against the panel")
    sub.add_parser("get_project", help="Active project name / path / active sequence")
    sub.add_parser("get_sequence", help="Active sequence name and player position")

    pf = sub.add_parser("export_frame", help="Export one frame of the active sequence")
    pf.add_argument("--dir", required=True, help="Output directory (must exist)")
    pf.add_argument("--name", default=None, help="Output file name (default: frame_<seconds>.png)")
    pf.add_argument("--at", type=float, default=None, help="Sequence time in seconds (default: player position)")
    pf.add_argument("--width", type=int, default=640)
    pf.add_argument("--height", type=int, default=360)

    po = sub.add_parser("export_otio", help="Export active sequence as OpenTimelineIO")
    po.add_argument("--output", required=True, help="Full output path ending in .otio")

    sub.add_parser("list_transcripts", help="List project clips and whether each has a transcript")

    pt = sub.add_parser("export_transcript", help="Export a clip's transcript as Adobe Transcript JSON")
    pt.add_argument("--item", default=None, help="Clip name (default: first clip with a transcript)")
    pt.add_argument("--output", required=True, help="Where to save the transcript JSON")

    pit = sub.add_parser("import_transcript", help="Import an Adobe-format transcript JSON onto a clip")
    pit.add_argument("--item", required=True, help="Clip name, exact match")
    pit.add_argument("--file", required=True, help="Transcript JSON file (e.g. from transcribe.py)")

    pi = sub.add_parser("import_media", help="Import media files into the project root")
    pi.add_argument("--path", action="append", required=True, help="Absolute file path (repeatable)")

    pc = sub.add_parser("insert_clip", help="Place a clip (or a source range of it) on the timeline")
    pc.add_argument("--item", required=True, help="Project item name, exact match")
    pc.add_argument("--at", type=float, default=0.0, help="Sequence time in seconds (default 0)")
    pc.add_argument("--vtrack", type=int, default=0, help="Video track, 0-based (0 = V1)")
    pc.add_argument("--atrack", type=int, default=0, help="Audio track, 0-based (0 = A1)")
    pc.add_argument("--mode", choices=["overwrite", "insert"], default="overwrite")
    pc.add_argument("--in", dest="in_s", type=float, default=None, help="Source in point, seconds")
    pc.add_argument("--out", dest="out_s", type=float, default=None, help="Source out point, seconds")
    pc.add_argument("--label", default=None, help="Undo history label")

    args = parser.parse_args()

    params: dict = {}
    if args.action == "export_frame":
        out_dir = Path(args.dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        name = args.name
        if name is None:
            stamp = f"{args.at:.3f}s" if args.at is not None else "playhead"
            name = f"frame_{stamp}.png"
        params = {"dir": str(out_dir), "name": name, "width": args.width, "height": args.height}
        if args.at is not None:
            params["atSeconds"] = args.at
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
        params = {"paths": [str(Path(p).expanduser().resolve()) for p in args.path]}
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

    reply = asyncio.run(call(args.action, params))

    if args.action == "export_frame" and reply.get("ok"):
        reply = resolve_exported_frame(reply)

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
    sys.exit(main())
