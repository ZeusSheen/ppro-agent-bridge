"""Every entry point users and agents run starts under the Python that runs this suite: the system Python here, and
in the release build the installer's bundled CPython, started with -I as the bridge always starts it (QA-2, QA-8).
Each runs as a program - --help, a ping through a stand-in bridge on a free port, the .pyw that starts nothing while a
broker answers -, never on port 8017."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port)

import json
import socket
import subprocess
import sys
import unittest

from helpers import REPO, Bridge, env

from ppro_bridge import console

PY = [sys.executable, *console.isolated_flags()]  # -I under the installer's Python, as start_bridge.bat runs it


def run(args, timeout=120, **extra):
    return subprocess.run([*PY, *args], cwd=str(REPO), env=env(**extra), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


class EntryPointsTest(unittest.TestCase):
    HELP = (["cli/ppro.py", "--help"], ["cli/transcribe.py", "--help"], ["cli/transcript_digest.py", "--help"],
            ["broker/broker.py", "--help"], ["scripts/build_ccx.py", "--help"], ["-m", "ppro_bridge", "--help"],
            ["-m", "ppro_bridge.doctor", "--help"], ["-m", "ppro_bridge.setup_ui", "--help"],
            ["-m", "ppro_bridge.report", "--help"])

    def test_each_entry_point_starts(self):
        for args in self.HELP:
            with self.subTest(args=" ".join(args)):
                out = run(args)
                self.assertEqual(out.returncode, 0, out.stderr[-2000:])
                self.assertIn("usage", out.stdout.lower())
        out = run(["-c", "import ppro_bridge.mcp.server, ppro_bridge.mcp.tools, ppro_bridge.mcp.connection"])
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])

    def test_the_cli_pings_a_stand_in_bridge(self):
        bridge = Bridge()
        self.addCleanup(bridge.stop)
        out = run(["cli/ppro.py", "ping"], PPRO_BRIDGE_URL=bridge.url)
        self.assertEqual(out.returncode, 0, out.stdout[-1000:] + out.stderr[-1000:])
        reply = json.loads(out.stdout)
        self.assertTrue(reply["ok"], reply)
        out = run(["-m", "ppro_bridge", "get_sequence"], PPRO_BRIDGE_URL=bridge.url)
        self.assertTrue(json.loads(out.stdout)["ok"], out.stdout[-1000:])

    def test_the_report_runs_without_a_broker(self):
        out = run(["-m", "ppro_bridge.report", "--no-ping", "--json"])
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("bridge", json.loads(out.stdout))

    def test_start_bridge_pyw_starts_nothing_while_a_broker_answers(self):
        with socket.socket() as listener:  # a free port (never 8017) that "answers"
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            out = run(["start_bridge.pyw"], PPRO_BRIDGE_PORT=listener.getsockname()[1])
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
