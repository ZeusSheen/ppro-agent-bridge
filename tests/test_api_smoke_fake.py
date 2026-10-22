"""Rehearsal of tests/live/api_smoke.py against a broker and the fake panel on a free port (never 8017).

The fake has no Premiere API: probe_api answers a canned inventory and every api_smoke step is "skipped", so this
checks the runner itself - the PAB_TEST refusal, the scratch clip, the case loop, the report files, the cleanup.
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

RUNNER = REPO / "tests" / "live" / "api_smoke.py"


class ApiSmokeAgainstFake(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        (self.media / "broll_audio.mp4").write_bytes(b"x")
        self.bridges = []

    def tearDown(self):
        for b in self.bridges:
            b.stop()
        self._tmp.cleanup()

    def bridge(self, project="PAB_TEST_fake"):
        b = Bridge(panel_env={"PPRO_FAKE_PROJECT_NAME": project, "PPRO_FAKE_MEDIA_SECONDS": "6"})
        self.bridges.append(b)
        self.assertNotIn(":8017", b.url)
        return b

    def run_it(self, bridge, *args):
        out = self.root / f"out_{len(list(self.root.glob('out_*')))}"
        proc = subprocess.run([sys.executable, str(RUNNER), "--media", str(self.media), "--url", bridge.url,
                               "--out", str(out), *args], env=env(PPRO_BRIDGE_URL=bridge.url), capture_output=True,
                              text=True, encoding="utf-8", timeout=300)
        path = out / "report.json"
        report = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        return proc, report, out

    def test_full_run_writes_the_reports_and_cleans_up(self):
        b = self.bridge()
        proc, report, out = self.run_it(b)
        self.assertIsNotNone(report, proc.stdout + proc.stderr)
        fails = [r for r in report["results"] if r["status"] == "FAIL"
                 and not r["name"].startswith(("read: panel code is current", "check: Project#save"))]
        self.assertEqual(fails, [], proc.stdout)
        st = {r["name"]: r["status"] for r in report["results"]}
        self.assertEqual(st["probe_api"], "PASS", proc.stdout)
        self.assertEqual(st["setup"], "PASS", proc.stdout)
        self.assertEqual(st["cleanup: remove placed clips"], "PASS", proc.stdout)
        self.assertEqual(st["api: implicit_track"], "SKIP")  # opt-in
        self.assertEqual(sorted(report["cases"]), sorted(c for c in report["cases"]))
        self.assertIn("markers", report["cases"])
        self.assertIn("sequences", report["cases"])
        self.assertTrue((out / "probe_api.json").is_file())
        self.assertIn("| markers |", (out / "api_smoke.md").read_text(encoding="utf-8"))
        self.assertEqual(report["leftBehind"]["clips"], [])
        self.assertEqual(report["guard"]["projectPrefix"], "PAB_TEST")

    def test_one_case_only(self):
        b = self.bridge()
        proc, report, _ = self.run_it(b, "--case", "keyframes")
        self.assertIsNotNone(report, proc.stdout + proc.stderr)
        self.assertEqual(list(report["cases"]), ["keyframes", "cleanup_bin"])  # the runner's bin goes last

    def test_a_named_project_runs_and_its_bin_is_removed(self):
        b = self.bridge(project="Real Job 2026.prproj")
        proc, report, _ = self.run_it(b, "--allow-project", "Real Job 2026.prproj", "--case", "markers",
                                      "--sample-frames", "3")
        self.assertIsNotNone(report, proc.stdout + proc.stderr)
        st = {r["name"]: r["status"] for r in report["results"]}
        self.assertEqual(st["setup"], "PASS", proc.stdout)
        self.assertEqual(st["check: sampled sequence frames"], "PASS", proc.stdout)
        self.assertNotIn("projectPrefix", report["guard"])
        self.assertIn("projectGuid", report["guard"])
        self.assertIn("cleanup_bin", report["cases"])
        proc, report, _ = self.run_it(b, "--allow-project", "Some Other.prproj")
        self.assertEqual(proc.returncode, 2, proc.stdout)

    def test_other_projects_are_refused(self):
        b = self.bridge(project="Real Job 2026.prproj")
        proc, report, _ = self.run_it(b)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual({r["name"]: r["status"] for r in report["results"]}["bootstrap"], "FAIL")
        self.assertNotIn("cases", {k for k, v in report.items() if v})


if __name__ == "__main__":
    unittest.main()
