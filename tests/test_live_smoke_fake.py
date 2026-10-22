"""Rehearsal of tests/live/live_smoke.py against a broker and the fake panel on a free port (never 8017).

The fake project is named PAB_TEST_fake, so the harness runs its full write phase; a fake project with another
name must be refused. Media files are empty placeholders (the fake panel never opens them).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from helpers import REPO, Bridge, env  # noqa: E402  (also points PPRO_BRIDGE_URL at a dead port)

SMOKE = REPO / "tests" / "live" / "live_smoke.py"
MEDIA = ("broll_audio.mp4", "EN/title.mov", "CN/title.mov", "speech_en.wav")


def _library_ready() -> bool:
    try:
        import ppro_bridge.ops  # noqa: F401
        import ppro_bridge.replace  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_library_ready(), "ppro_bridge.ops / ppro_bridge.replace are not importable yet")
class LiveSmokeAgainstFake(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.media = self.root / "media"
        for rel in MEDIA:
            path = self.media / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        self.bridges = []

    def tearDown(self):
        for b in self.bridges:
            b.stop()
        self._tmp.cleanup()

    def bridge(self, project="PAB_TEST_fake", **panel_env):
        b = Bridge(panel_env={"PPRO_FAKE_PROJECT_NAME": project, "PPRO_FAKE_MEDIA_SECONDS": "6",
                              "PPRO_FAKE_STT_POLLS": "2", **panel_env})
        self.bridges.append(b)
        self.assertNotIn(":8017", b.url)
        return b

    def smoke(self, bridge, *args):
        out = self.root / f"out_{len(list(self.root.glob('out_*')))}"
        proc = subprocess.run([sys.executable, str(SMOKE), "--media", str(self.media), "--url", bridge.url,
                               "--out", str(out), "--stt-poll", "0.05", "--stt-wait", "30", *args],
                              env=env(PPRO_BRIDGE_URL=bridge.url), capture_output=True, text=True,
                              encoding="utf-8", timeout=300)
        path = out / "report.json"
        report = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        return proc, report

    def statuses(self, report):
        return {r["name"]: r["status"] for r in report["results"]}

    def project_items(self, bridge):
        from ppro_bridge import transport
        return transport.make_rpc(url=bridge.url)("list_project_items")["items"]

    def test_full_write_run_passes_and_cleans_up(self):
        b = self.bridge()
        proc, report = self.smoke(b, "--write")
        self.assertIsNotNone(report, proc.stdout + proc.stderr)
        fails = [r for r in report["results"] if r["status"] == "FAIL"]
        self.assertEqual(fails, [], proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        st = self.statuses(report)
        for name in ("bootstrap", "read: ping protocol", "guard: other project prefix", "write: import_media",
                     "write: insert source range (frame-exact)", "write: set_params sequence time",
                     "write: set_params read-back (inspect_items)", "write: add_transition + OTIO read-back",
                     "write: replace_clip EN -> CN", "write: import_transcript (fixture)",
                     "write: transcribe_clip (Speech-to-Text)", "cleanup: remove placed clips",
                     "after: timeline restored", "after: project items"):
            self.assertEqual(st.get(name), "PASS", f"{name}: {st.get(name)}\n{proc.stdout}")
        self.assertEqual(report["guard"]["projectPrefix"], "PAB_TEST")
        self.assertIn("sequenceGuid", report["guard"])
        self.assertEqual(report["leftBehind"]["clips"], [])
        self.assertTrue(all(i.startswith(f"PAB_SMOKE_{report['runId']}") for i in report["leftBehind"]["projectItems"]))
        self.assertTrue(report["leftBehind"]["markers"][0].startswith(f"PAB_SMOKE {report['runId']}"))

    def test_read_only_run_writes_nothing(self):
        b = self.bridge()
        before = self.project_items(b)
        proc, report = self.smoke(b)
        self.assertIsNotNone(report, proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse([n for n in self.statuses(report) if n.startswith(("write:", "cleanup:"))])
        self.assertEqual(self.project_items(b), before)

    def test_keep_leaves_the_clips(self):
        b = self.bridge()
        proc, report = self.smoke(b, "--write", "--keep")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.statuses(report)["cleanup: remove placed clips"], "SKIP")
        self.assertTrue(report["leftBehind"]["clips"])

    def test_denied_host_gets_ping_only_checks(self):
        b = self.bridge(PPRO_FAKE_DENIED="rehearsal: this build is denied")
        proc, report = self.smoke(b, "--write")
        self.assertIsNotNone(report, proc.stdout + proc.stderr)
        st = self.statuses(report)
        self.assertEqual(st.get("read: ping-only mode refuses other actions"), "PASS", proc.stdout)
        self.assertEqual(st.get("write phase"), "SKIP", proc.stdout)
        self.assertFalse([n for n in st if n.startswith(("write:", "cleanup:"))], proc.stdout)
        self.assertEqual(proc.returncode, 1, "a denied host is not compatible: " + proc.stdout)

    def test_other_projects_are_refused(self):
        b = self.bridge(project="Smoke Test.prproj")
        before = self.project_items(b)
        for args in (("--write",), ("--write", "--any-project"), ()):
            proc, report = self.smoke(b, *args)
            self.assertEqual(proc.returncode, 2, f"{args}: {proc.stdout}")
            self.assertEqual(self.statuses(report)["bootstrap"], "FAIL")
        proc, report = self.smoke(b, "--any-project")  # read phase only, pinned to that project by guid
        self.assertNotEqual(proc.returncode, 2, proc.stdout)
        self.assertNotIn("projectPrefix", report["guard"])
        self.assertEqual(self.project_items(b), before)


if __name__ == "__main__":
    unittest.main()
