"""The problem report (ppro_bridge/report.py): a whitelist, the home folder redacted, never the token."""
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ppro_bridge import report  # noqa: E402

HOME = str(Path.home())
TOKEN = "tok_" + "x7" * 20

PING = {
    "pong": True, "version": "0.7.0", "cliVersion": "0.7.0", "brokerVersion": "0.7.0", "protocol": 2,
    "compatible": True, "stale": False, "stuck": False, "denied": False,
    "hostInfo": {"name": "premierepro", "version": "27.1.0", "buildNumber": "37", "uiLocale": "zh_CN"},
    "capabilities": {"razor": False, "subclip": True, "trackMute": "unknown", "weird": {"nested": 1}},
    "build": {"dev": False, "packaged": True, "path": HOME + r"\secret"},
    "features": ["exportJobs"],
    # none of these may reach the report
    "project": "Client Secret Project", "sequence": "Client cut v3", "localCodeHash": "abc",
    "modulesPath": HOME + r"\plugin", "token": TOKEN, "actions": ["ping", "list_items"],
}


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ppro_report_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_only_whitelisted_ping_fields(self):
        got = report.from_ping(PING)
        text = json.dumps(got)
        for secret in ("Client Secret Project", "Client cut v3", TOKEN, "list_items", "localCodeHash", "secret"):
            self.assertNotIn(secret, text)
        self.assertEqual(got["premiere"], {"name": "premierepro", "version": "27.1.0", "buildNumber": "37",
                                           "uiLocale": "zh_CN"})
        self.assertEqual(got["capabilities"], {"razor": False, "subclip": True, "trackMute": "unknown"})
        self.assertEqual(got["build"], {"dev": False, "packaged": True})
        self.assertEqual(report.from_ping(None), {})

    def test_home_folder_becomes_tilde_in_either_slash_and_case(self):
        for form in (HOME, HOME.replace("\\", "/"), HOME.upper()):
            self.assertEqual(report.redact(form + r"\Videos\a.mov"), r"~\Videos\a.mov")
        warn = dict(PING, warning=f"Panel at {HOME}\\x is stale")
        self.assertNotIn(HOME, json.dumps(report.from_ping(warn)))

    def test_doctor_checks_keep_name_and_result_only(self):
        rows = report.from_checks([{"name": "ffmpeg", "ok": False, "required": False,
                                    "detail": HOME + r"\bin\ffmpeg.exe", "fix": "install it"}])
        self.assertEqual(rows, [{"name": "ffmpeg", "ok": False, "required": False}])

    def test_recent_failures_from_the_audit_log_without_params(self):
        day = dt.date(2026, 11, 6)
        lines = [{"ts": "2026-11-06T11:58:01", "event": "request", "action": "import_media", "ok": False,
                  "code": "PANEL_TIMEOUT", "outcome": "unknown", "params": {"paths": [HOME + r"\Music\x.flac"]}},
                 {"ts": "2026-11-06T11:59:00", "event": "request", "action": "ping", "ok": True},
                 {"ts": "2026-11-06T12:00:00", "event": "refused", "reason": "origin", "origin": "http://evil"}]
        (self.tmp / "audit-2026-11-06.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n",
                                                         encoding="utf-8")
        rows = report.recent_failures(self.tmp, today=day)
        self.assertEqual([r.get("action") or r.get("event") for r in rows], ["import_media", "refused"])
        self.assertNotIn("flac", json.dumps(rows))
        self.assertNotIn("evil", json.dumps(rows))
        self.assertEqual(report.recent_failures(self.tmp / "missing", today=day), [])

    def test_text_says_what_matters_and_never_the_token(self):
        rep = report.build(PING, [{"name": "UXP Developer Tool", "ok": False, "required": True}], [])
        text = report.as_text(rep)
        self.assertIn("Premiere 27.1.0 (build 37, zh_CN)", text)
        self.assertIn("1 yes, 1 no (razor), 1 not probed yet", text)
        self.assertIn("failing: UXP Developer Tool", text)
        self.assertNotIn(TOKEN, text)
        self.assertIn("Ping: not asked", report.as_text(report.build(None, None, [])))
        self.assertIn("no answer", report.as_text(report.build({}, None, [])))

    def test_a_token_that_slipped_in_is_cut_out(self):
        with mock.patch.object(report.auth, "read_token", return_value=TOKEN):
            self.assertEqual(report.scrub_token(f"a {TOKEN} b"), "a [removed] b")

    def test_main_prints_text_or_json_without_the_broker(self):
        with mock.patch.dict(os.environ, {"PPRO_AUDIT_DIR": str(self.tmp)}), \
                mock.patch.object(report.auth, "read_token", return_value=TOKEN):
            for argv, check in ((["--no-ping"], lambda s: self.assertIn("Ping: not asked", s)),
                                (["--no-ping", "--json"], lambda s: self.assertIsNone(json.loads(s)["ping"]))):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(report.main(argv), 0)
                check(buf.getvalue())
                self.assertNotIn(TOKEN, buf.getvalue())


class NamesStayOutTest(unittest.TestCase):
    """QA-3: no free text reaches the report - ping's warning becomes fixed labels, audit fields keep their shapes."""

    def test_the_ping_warning_is_reduced_to_kinds(self):
        ping = dict(PING, warning="The panel is stuck on insert_clip 'Confidential Cut' and refuses everything but "
                                  "ping. could not read " + "\\".join(["D:", "Job", "ClientX", "SECRET.prproj"])
                                  + " on sequence 'Confidential Cut'",
                    modules=["core", "Confidential Cut"])
        text = report.as_text(report.build(ping, [], []))
        self.assertNotIn("Confidential", text)
        self.assertNotIn("SECRET", text)
        self.assertIn("panel stuck", text)
        self.assertEqual(report.warning_kinds("something new"), "other")
        self.assertEqual(report.warning_kinds(""), None)

    def test_audit_rows_keep_only_shaped_fields_and_known_actions(self):
        rec = {"ts": "2026-11-06T10:00:00+11:00", "event": "request", "action": "Confidential Cut v3",
               "code": "Client said no", "outcome": "not_committed", "reason": "see " + "\\".join(["D:", "Job"]), "error": "x"}
        row = report._failure_row(rec)
        self.assertEqual(row, {"ts": "2026-11-06T10:00:00+11:00", "event": "request", "action": "unknown",
                               "outcome": "not_committed"})
        self.assertEqual(report._failure_row(dict(rec, action="insert_clip", code="BAD_PARAMS"))["action"],
                         "insert_clip")


if __name__ == "__main__":
    unittest.main()
