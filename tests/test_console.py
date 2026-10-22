"""Every entry point writes UTF-8, whatever the console or pipe encoding: a Chinese clip name must reach the
caller intact without PYTHONUTF8 / PYTHONIOENCODING (Windows pipes default to the ANSI code page)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from helpers import CLI, REPO

NAME = "\u4e2d\u6587\u526a\u8f91 \U0001f3ac"  # Chinese plus a character no ANSI code page has


def child_env() -> dict:
    e = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    return e


def run(*args) -> tuple[int, str]:
    out = subprocess.run([sys.executable, *map(str, args)], cwd=str(REPO), env=child_env(), capture_output=True,
                         timeout=60)
    return out.returncode, out.stdout.decode("utf-8")  # strict: anything else is a failure


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)

    def test_cli_error_with_a_chinese_path(self):
        missing = self.tmp / f"{NAME}.mov"
        code, stdout = run("-m", "ppro_bridge", "import_media", "--path", missing)
        reply = json.loads(stdout)
        self.assertEqual((code, reply["code"]), (1, "FILE_NOT_FOUND"))
        self.assertEqual(reply["data"]["missing"], [str(missing.resolve())])

    def test_cli_shim_too(self):
        code, stdout = run(CLI / "ppro.py", "import_media", "--path", self.tmp / f"{NAME}.mp4")
        self.assertIn(NAME, json.loads(stdout)["data"]["missing"][0])

    def test_digest_prints_chinese_text(self):
        spk = str(uuid.uuid4())
        doc = {"language": "cmn-hans", "speakers": [{"id": spk, "name": NAME}], "segments": [
            {"start": 0, "duration": 1, "language": "cmn-hans", "speaker": spk, "words": [
                {"text": "\u4f60\u597d", "start": 0.0, "duration": 0.5, "eos": True, "confidence": 0.9,
                 "tags": [], "type": "word"}]}]}
        path = self.tmp / f"{NAME}.json"
        path.write_bytes(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
        code, stdout = run(CLI / "transcript_digest.py", path)
        self.assertEqual(code, 0)
        self.assertIn(f"{NAME}: \u4f60\u597d", stdout)

    def test_transcribe_error_with_a_chinese_path(self):
        code, stdout = run(CLI / "transcribe.py", self.tmp / f"{NAME}.wav", "--output", self.tmp / "t.json")
        reply = json.loads(stdout)
        self.assertEqual((code, reply["code"]), (1, "FILE_NOT_FOUND"))
        self.assertIn(NAME, reply["error"])


if __name__ == "__main__":
    unittest.main()
