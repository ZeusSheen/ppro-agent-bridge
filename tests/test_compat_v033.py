"""Scripts written against 0.3.3 keep working, and the review's safety fixes hold.

- cli/ppro.py: assigning ppro.BROKER_URL / ppro.call / ppro.rpc redirects the CLI as it did in 0.3.3 (a script that
  points BROKER_URL at a test broker must never end up on the live one); cmd_* and the old constants exist.
- import_transcript upgrades language codes written by transcribe.py 0.3.x instead of refusing the file.
- An empty guard is refused (an empty prefix would match every project): client, fake panel and panel.
- transcribe_clip: once the job has started, a later failure never reports "nothing happened".
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from helpers import CLI, REPO, Bridge, BridgeError, env, fake_rpc

from ppro_bridge import ops, transport

HARNESS = REPO / "tests" / "panel" / "harness.js"
NODE = shutil.which("node")


def py_in_cli(code: str, **extra_env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=CLI, env=env(**extra_env), capture_output=True,
                          text=True, encoding="utf-8", timeout=60)


class ShimRedirectTest(unittest.TestCase):
    def test_broker_url_assignment_reaches_that_broker(self):
        bridge = Bridge()
        try:
            out = py_in_cli("import json, ppro\n"
                            f"ppro.BROKER_URL = {bridge.url!r}\n"
                            "print(json.dumps(ppro.rpc('get_project')))\n")  # PPRO_BRIDGE_URL is a dead port here
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["name"], "Smoke Test.prproj")
        finally:
            bridge.stop()

    def test_patched_call_reaches_main(self):
        out = py_in_cli("import ppro\n"
                        "async def stub(action, params, **kw):\n"
                        "    return {'ok': True, 'data': {'name': 'via stub call', 'action': action}}\n"
                        "ppro.call = stub\n"
                        "raise SystemExit(ppro.main(['get_project']))\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["data"], {"name": "via stub call", "action": "get_project"})

    def test_cmd_functions_and_constants(self):
        out = py_in_cli(
            "import argparse, json, ppro, transcribe\n"
            "def stub(action, params=None):\n"
            "    if action == 'list_project_items':\n"
            "        return {'items': [{'name': 'g.mov', 'bin': '', 'mediaPath': '/fake/g.mov'}]}\n"
            "    return {'fps': params['fps'], 'items': [i['name'] for i in params['items']], 'undoLabel': 'x'}\n"
            "ppro.rpc = stub\n"
            "got = ppro.cmd_set_fps(argparse.Namespace(item=['g.mov'], fps=59.94, dry_run=False, label=None))\n"
            "print(json.dumps([got, ppro.ACTION_TIMEOUTS.get('import_media'), ppro.DEFAULT_TIMEOUT,\n"
            "                  ppro.QUEUE_TIMEOUT, transcribe.ADOBE_LANG['en'], callable(ppro.cmd_apply_plan)]))\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        got, import_timeout, default, queue, en, has_apply = json.loads(out.stdout)
        self.assertEqual(got["items"], ["g.mov"])
        self.assertEqual((import_timeout, default, queue, en, has_apply), (120, 30, 120, "en-us", True))


class LegacyTranscriptTest(unittest.TestCase):
    def test_zh_cn_is_upgraded_on_import(self):
        speaker = str(uuid.uuid4())
        doc = {"language": "zh-cn", "speakers": [{"id": speaker, "name": "Speaker 1"}], "segments": [
            {"start": 0.0, "duration": 1.0, "language": "zh-cn", "speaker": speaker, "words": [
                {"text": "你好", "start": 0.0, "duration": 0.5, "eos": True, "confidence": 0.9,
                 "tags": [], "type": "word"}]}]}
        sent = {}

        def rpc(action, params=None):
            if action == "ping":
                return {"actions": ["import_transcript", "list_project_items"], "protocol": 2, "plugin": "stub"}
            if action == "list_project_items":
                return {"items": [{"name": "a.mp4", "bin": "", "mediaPath": "/fake/a.mp4"}]}
            sent.update(params)
            return {"imported": True, "wordsIn": 1, "wordsOut": 1}

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "legacy.json"
            f.write_bytes(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
            result = ops.import_transcript(rpc, item="a.mp4", file=str(f))
        sent_doc = json.loads(sent["transcriptJson"])
        self.assertEqual((sent_doc["language"], sent_doc["segments"][0]["language"]), ("cmn-hans", "cmn-hans"))
        self.assertTrue(any("zh-cn -> cmn-hans" in w for w in result["warnings"]), result)


class EmptyGuardTest(unittest.TestCase):
    def test_client_refuses_an_empty_guard(self):
        for guard in ({"projectPrefix": ""}, {"project": None}, {}):
            with self.assertRaises(BridgeError) as cm:
                transport.make_rpc(url="ws://127.0.0.1:9", guard=guard)
            self.assertEqual(cm.exception.code, "BAD_PARAMS", guard)

    def test_fake_panel_refuses_an_empty_guard_value(self):
        _, fake, _ = fake_rpc()
        reply = fake.dispatch({"id": "1", "action": "get_sequence", "params": {}, "guard": {"projectPrefix": ""}})
        self.assertEqual((reply["ok"], reply["code"]), (False, "BAD_PARAMS"))

    @unittest.skipUnless(NODE and HARNESS.is_file(), "needs node and tests/panel/harness.js")
    def test_panel_refuses_an_empty_guard_value(self):
        p = subprocess.run([NODE, str(HARNESS), "call"], capture_output=True, text=True, encoding="utf-8", timeout=60,
                           input=json.dumps({"action": "get_sequence", "params": {}, "guard": {"projectPrefix": ""}}))
        self.assertEqual(p.returncode, 0, p.stderr)
        reply = json.loads(p.stdout)["reply"]
        self.assertEqual((reply["ok"], reply["code"], reply["outcome"]), (False, "BAD_PARAMS", "not_committed"))


class TranscribeOutcomeTest(unittest.TestCase):
    def test_failure_after_start_is_committed(self):
        def rpc(action, params=None):
            if action == "ping":
                return {"actions": ["list_project_items", "transcribe_clip", "list_transcripts", "export_transcript"],
                        "protocol": 2, "plugin": "stub"}
            if action == "list_project_items":
                return {"items": [{"name": "a.mp4", "bin": "", "mediaPath": "/fake/a.mp4"}]}
            if action == "transcribe_clip":
                return {"started": True, "jobId": "j1", "itemName": "a.mp4", "binPath": "", "language": None}
            raise BridgeError("list_transcripts: no project", code="NO_PROJECT", outcome="not_committed")

        with self.assertRaises(BridgeError) as cm:
            ops.transcribe_clip(rpc, item="a.mp4", wait=10, poll=1, sleep=lambda s: None)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("NO_PROJECT", "committed"))


if __name__ == "__main__":
    unittest.main()
