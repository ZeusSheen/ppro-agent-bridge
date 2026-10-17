#!/usr/bin/env python3
"""
Fake Premiere panel for testing the broker + CLI without Premiere.

Registers as role=panel and answers the v0.3 whitelist from an in-memory
project: a 1080×1920 / 59.94 fps sequence with 4 video and 4 audio tracks.
Imports, placements, markers and removals change that state, so multi-step
flows (apply_plan → list_items → remove_items) can be tested end to end.
Run it instead of (not alongside) the real Premiere panel; for an isolated
test, point broker, panel and CLI at another port:

  set PPRO_BRIDGE_PORT=8018            (broker)
  set PPRO_BRIDGE_URL=ws://127.0.0.1:8018   (this script and ppro.py)
"""

import asyncio
import json
import os
from pathlib import Path

import websockets

BROKER_URL = os.environ.get("PPRO_BRIDGE_URL", "ws://127.0.0.1:8017")
VERSION = "0.3.2"
FPS = 59.94
EPS = 1e-3

def solid_png(w: int, h: int, rgb=(40, 70, 120)) -> bytes:
    """A valid solid-colour PNG (no Pillow needed) for export_frame to write."""
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

FAKE_TRANSCRIPT = {
    "language": "en-us",
    "speakers": [{"id": "spk1", "name": "Jane Doe"}],
    "segments": [
        {"start": 0.5, "duration": 4.0, "language": "en-us", "speaker": "spk1", "words": [
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

STATE = {
    "items": [  # project panel
        {"name": "interview.mp4", "bin": "", "mediaPath": "/fake/interview.mp4", "hasTranscript": True},
        {"name": "broll_city.mp4", "bin": "", "mediaPath": "/fake/broll_city.mp4", "hasTranscript": False},
    ],
    # timeline: the interview runs the full length on V1/A1, like a talking-head A-roll
    "video": [[{"name": "interview.mp4", "start": 0.0, "end": 60.0, "inPoint": 0.0, "outPoint": 60.0, "scale": 100.0}],
              [], [], []],
    "audio": [[{"name": "interview.mp4", "start": 0.0, "end": 60.0, "inPoint": 0.0, "outPoint": 60.0}], [], [], []],
    "markers": [],
}


def snap(t):
    return round(t * FPS) / FPS


def describe(it):
    media = next((p["mediaPath"] for p in STATE["items"] if p["name"] == it["name"]), None)
    return {"name": it["name"], "start": it["start"], "end": it["end"], "inPoint": it["inPoint"],
            "outPoint": it["outPoint"], "speed": 1, "disabled": False, "projectItem": it["name"], "mediaPath": media}


def overlaps(it, frm, to):
    return it["start"] <= to + EPS and it["end"] > frm + EPS


def track(kind, idx):
    tracks = STATE[kind]
    if idx < 0 or idx >= len(tracks):
        raise ValueError(f"{'V' if kind == 'video' else 'A'}{idx + 1} does not exist (sequence has {len(tracks)} {kind} tracks).")
    return tracks[idx]


def overwrite(kind, idx, start, end, item):
    """Overwrite edit: trim/split whatever is under [start, end), then add the item."""
    out = []
    for it in track(kind, idx):
        if it["end"] <= start + EPS or it["start"] >= end - EPS:
            out.append(it)
            continue
        if it["start"] < start:
            out.append(dict(it, end=start, outPoint=it["inPoint"] + (start - it["start"])))
        if it["end"] > end:
            out.append(dict(it, start=end, inPoint=it["inPoint"] + (end - it["start"])))
    out.append(item)
    STATE[kind][idx] = sorted(out, key=lambda x: x["start"])


def h_ping(p):
    return {"pong": True, "host": "FAKE Premiere 27.1", "plugin": f"fake-panel {VERSION}",
            "version": VERSION, "actions": sorted(HANDLERS)}


def h_get_sequence(p):
    end = max([it["end"] for t in STATE["video"] + STATE["audio"] for it in t] or [0])
    tr = lambda kind: [{"index": i, "label": ("V" if kind == "video" else "A") + str(i + 1),
                        "name": f"{'Video' if kind == 'video' else 'Audio'} {i + 1}", "clipCount": len(t)}
                       for i, t in enumerate(STATE[kind])]
    return {"name": "Sequence 01", "playerPositionSeconds": 4.2, "width": 1080, "height": 1920,
            "timebase": "4237833600", "fps": FPS, "durationSeconds": end,
            "videoTracks": tr("video"), "audioTracks": tr("audio"), "captionTrackCount": 1}


def h_list_items(p):
    out = {"sequence": "Sequence 01"}
    for kind in p.get("kinds") or ["video", "audio"]:
        out[kind] = [{"index": i, "label": ("V" if kind == "video" else "A") + str(i + 1),
                      "name": f"{'Video' if kind == 'video' else 'Audio'} {i + 1}",
                      "items": [describe(it) for it in t]} for i, t in enumerate(STATE[kind])]
    return out


def h_list_project_items(p):
    return {"items": [{"name": i["name"], "bin": i["bin"], "mediaPath": i["mediaPath"]} for i in STATE["items"]]}


def h_import_media(p):
    new = []
    for path in p["paths"]:
        name = Path(path).name
        STATE["items"].append({"name": name, "bin": p.get("bin") or "", "mediaPath": path, "hasTranscript": False})
        new.append(name)
    return {"imported": True, "count": len(p["paths"]), "bin": p.get("bin"), "newItems": new}


def h_insert_clip(p):
    item = next((i for i in STATE["items"] if i["name"] == p["itemName"]), None)
    if not item:
        raise ValueError(f'No clip named "{p["itemName"]}".')
    at = snap(p.get("atSeconds") or 0)
    src_in = p.get("inSeconds", 0.0)
    src_out = p.get("outSeconds", src_in + 10.0)
    end = snap(at + (src_out - src_in))
    clip = {"name": item["name"], "start": at, "end": end, "inPoint": src_in, "outPoint": src_out, "scale": 100.0}
    overwrite("video", p.get("videoTrack") or 0, at, end, clip)
    if not item["name"].endswith((".mov", ".png")):  # pretend stills/graphics are silent
        overwrite("audio", p.get("audioTrack") or 0, at, end, dict(clip))
    return {"placed": True, "itemName": item["name"], "mode": p.get("mode", "overwrite"), "atSeconds": at,
            "videoTrack": p.get("videoTrack") or 0, "audioTrack": p.get("audioTrack") or 0,
            "sourceRange": [src_in, src_out], "undoLabel": p.get("undoLabel")}


def h_set_scale(p):
    frm = p["fromSeconds"]
    to = p.get("toSeconds", frm)
    hits = [it for it in track("video", p["videoTrack"]) if overlaps(it, frm, to)]
    if not hits:
        raise ValueError(f"No clip on V{p['videoTrack'] + 1} overlaps {frm}–{to}s.")
    clips = []
    for it in hits:
        d = describe(it)
        d.update(scaleParam="Scale", scaleBefore=it["scale"], scaleKeyframed=False)
        clips.append(d)
        if not p.get("dryRun"):
            it["scale"] = p["value"]
    return {"dryRun": bool(p.get("dryRun")), "value": p.get("value"), "clips": clips}


def h_set_fps(p):
    missing = [n for n in p["itemNames"] if not any(i["name"] == n for i in STATE["items"])]
    if missing:
        raise ValueError(f"No clip named: {', '.join(missing)}")
    return {"fps": p["fps"], "items": p["itemNames"]}


def h_add_marker(p):
    STATE["markers"].append(p)
    return {"added": True, "name": p["name"], "atSeconds": p["atSeconds"], "durationSeconds": p.get("durationSeconds", 0),
            "type": p.get("type", "Comment"), "colorSet": "colorIndex" in p}


def h_remove_items(p):
    removed, keep = [], {}
    for kind, idxs in (("video", p.get("videoTracks") or []), ("audio", p.get("audioTracks") or [])):
        for idx in idxs:
            kept = []
            for it in track(kind, idx):
                inside = it["start"] >= p["fromSeconds"] - EPS and it["end"] <= p["toSeconds"] + EPS
                hit = inside if p.get("mode", "inside") == "inside" else overlaps(it, p["fromSeconds"], p["toSeconds"])
                (removed.append(dict(describe(it), track=("V" if kind == "video" else "A") + str(idx + 1)))
                 if hit else kept.append(it))
            keep[(kind, idx)] = kept
    if not p.get("dryRun"):
        for (kind, idx), kept in keep.items():
            STATE[kind][idx] = kept
    return {"dryRun": bool(p.get("dryRun")), "mode": p.get("mode", "inside"), "removed": removed}


def h_export_frame(p):
    name = p.get("name") or "frame.png"
    Path(p["dir"]).mkdir(parents=True, exist_ok=True)
    (Path(p["dir"]) / name).write_bytes(solid_png(108, 192))
    return {"exported": True, "dir": p["dir"], "candidates": [name, name + ".png"],
            "width": p.get("width", 1080), "height": p.get("height", 1920), "atSeconds": p.get("atSeconds")}


HANDLERS = {
    "ping": h_ping,
    "get_project": lambda p: {"name": "Smoke Test.prproj", "path": "/tmp/fake.prproj", "activeSequence": "Sequence 01"},
    "get_sequence": h_get_sequence,
    "list_items": h_list_items,
    "list_project_items": h_list_project_items,
    "list_transcripts": lambda p: {"clips": [{"name": i["name"], "hasTranscript": i["hasTranscript"]} for i in STATE["items"]]},
    "export_transcript": lambda p: {"itemName": "interview.mp4", "transcriptJson": json.dumps(FAKE_TRANSCRIPT)},
    "import_transcript": lambda p: {"imported": True, "itemName": p.get("itemName")},
    "export_frame": h_export_frame,
    "import_media": h_import_media,
    "insert_clip": h_insert_clip,
    "set_scale": h_set_scale,
    "set_fps": h_set_fps,
    "add_marker": h_add_marker,
    "remove_items": h_remove_items,
}


async def main():
    async with websockets.connect(BROKER_URL) as ws:
        await ws.send(json.dumps({"role": "panel"}))
        print(f"fake panel {VERSION} registered at {BROKER_URL}", flush=True)
        async for raw in ws:
            msg = json.loads(raw)
            action = msg.get("action")
            handler = HANDLERS.get(action)
            if not handler:
                reply = {"id": msg["id"], "ok": False, "error": f"fake panel: no handler for {action}"}
            else:
                try:
                    reply = {"id": msg["id"], "ok": True, "data": handler(msg.get("params") or {})}
                except Exception as e:  # mirror the real panel: errors become ok:false
                    reply = {"id": msg["id"], "ok": False, "error": str(e)}
            await ws.send(json.dumps(reply))
            print(f"answered {action}: {'ok' if reply['ok'] else reply['error']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
