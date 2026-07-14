#!/usr/bin/env python3
"""
Fake Premiere panel for testing the broker + CLI without Premiere.

Registers as role=panel and answers a subset of commands with canned data.
Run it instead of (not alongside) the real Premiere panel.
"""

import asyncio
import json

import websockets

BROKER_URL = "ws://127.0.0.1:8017"

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

CANNED = {
    "ping": {"pong": True, "host": "FAKE Premiere 26.0", "plugin": "fake-panel"},
    "get_project": {"name": "Smoke Test.prproj", "path": "/tmp/fake.prproj", "activeSequence": "Sequence 01"},
    "get_sequence": {"name": "Sequence 01", "playerPositionSeconds": 4.2},
    "list_transcripts": {"clips": [
        {"name": "interview.mp4", "hasTranscript": True},
        {"name": "broll_city.mp4", "hasTranscript": False},
    ]},
    "export_transcript": {"itemName": "interview.mp4",
                          "transcriptJson": json.dumps(FAKE_TRANSCRIPT)},
    "import_media": {"imported": True, "count": 1},
    "import_transcript": {"imported": True, "itemName": "interview.mp4"},
    "insert_clip": {"placed": True, "itemName": "interview.mp4", "mode": "overwrite",
                    "atSeconds": 0, "videoTrack": 0, "audioTrack": 0,
                    "sourceRange": [2.2, 3.9], "undoLabel": "Claude: place interview.mp4 @0s"},
}


async def main():
    async with websockets.connect(BROKER_URL) as ws:
        await ws.send(json.dumps({"role": "panel"}))
        print("fake panel registered")
        async for raw in ws:
            msg = json.loads(raw)
            action = msg.get("action")
            if action in CANNED:
                reply = {"id": msg["id"], "ok": True, "data": CANNED[action]}
            else:
                reply = {"id": msg["id"], "ok": False, "error": f"fake panel: no handler for {action}"}
            await ws.send(json.dumps(reply))
            print(f"answered {action}")


if __name__ == "__main__":
    asyncio.run(main())
