"""The panel's update notice, GitHub links and problem report (plugin/lib/support.js, wired in main.js), through the
node harness: which URLs it opens (only this repository's), when the notice shows, what Skip sends, and that the
report holds no token, path, project name or error text. uxp.shell.openExternal and the clipboard are mocks: nothing
here opens a browser or touches the real clipboard."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ppro_bridge import auth, update

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "panel" / "harness.js"
NODE = shutil.which("node")
REL = update.RELEASES_URL
MANIFEST = json.loads((REPO / "plugin" / "manifest.json").read_text(encoding="utf-8"))
B = "\\"


def win(*parts):
    """A Windows path from its parts (the release scan flags literal drive and user-folder paths)."""
    return B.join(parts)


def fwd(*parts):
    """A forward-slash path from its parts (for the same reason)."""
    return "/".join(parts)


UPDATE = {"latest": "9.9.9", "url": REL + "tag/v9.9.9", "published": "2026-11-20T03:08:36Z", "newer": True,
          "skipped": False, "checkedAt": "2026-11-06T01:02:03Z"}
URLS = [REL + "tag/v0.7.1", REL + "latest", REL, REL + "tag/", REL + "../../../evil/repo", REL + "tag/../../x",
        REL + "tag/%2e%2e", REL + "tag/v1?x=1", REL + "tag/v1#x", REL + "tag/v1@evil.example", REL + "tag\\v1",
        REL + "tag/v 1", update.REPO_URL + "-evil/releases/tag/v1",
        update.REPO_URL.replace("https:", "http:") + "/releases/tag/v1", "https://github.com.evil.example/releases/x",
        "https://evil.example/" + REL, "javascript:alert(1)", "file:///" + fwd("C:", "Windows", "system32", "calc.exe"),
        update.ISSUES_URL, update.ISSUES_URL + "?title=x", update.REPO_URL, ""]
SECRETS = ["Client Secret Project", "Rough cut v3", win("C:", "Users", "bob"), "bob", fwd("", "Users", "alice"),
           "\\\\fileserver",
           "SECRET-MESSAGE", "a.mov"]


def harness(cmd, req, timeout=60):
    p = subprocess.run([NODE, str(HARNESS), cmd], input=json.dumps(req), capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout)
    if p.returncode != 0:
        raise AssertionError(f"harness {cmd} exited {p.returncode}: {p.stdout}\n{p.stderr}")
    out = json.loads(p.stdout)
    assert out.get("ok") is True and "violations" not in out, out
    return out


def support(*calls, **extra):
    return harness("support", dict(extra, calls=[{"fn": fn, "args": list(args)} for fn, *args in calls]))["results"]


def connection(steps, **extra):
    return harness("connection", dict(extra, steps=steps))


def registered(upd=UPDATE, broker="0.7.0"):
    """Open and pair (harness step `pair`: a 0.7 broker's challenge, then "registered" with its proof)."""
    return [{"open": True}, {"pair": {"update": upd, "broker": broker}}]


@unittest.skipIf(NODE is None, "node is not installed")
class UrlTest(unittest.TestCase):
    def test_only_this_repositorys_release_pages_and_the_issues_page(self):
        allowed = support(*[("allowedUrl", u) for u in URLS])
        self.assertEqual([u for u, ok in zip(URLS, allowed) if ok], [REL + "tag/v0.7.1", REL + "latest",
                                                                     update.ISSUES_URL])

    def test_the_panel_and_the_broker_agree_on_release_pages(self):
        got = support(*[("releaseUrl", u) for u in URLS])
        self.assertEqual(got, [update.release_url(u) is not None for u in URLS])

    def test_the_fixed_addresses_are_the_brokers(self):
        out = harness("support", {"calls": []})
        self.assertEqual(out["results"], [])
        main = (REPO / "plugin" / "lib" / "support.js").read_text(encoding="utf-8")
        self.assertIn(f'const REPO_URL = "{update.REPO_URL}";', main)

    def test_a_refused_url_never_reaches_uxp(self):
        out = support(("openUrl", "https://evil.example/x", "why"), ("openUrl", REL + "tag/v1", "why"))
        self.assertEqual(out, ["refused: not a page of this repository", ""])

    def test_the_manifest_lets_the_panel_open_https_and_nothing_new_else(self):
        perms = MANIFEST["requiredPermissions"]
        self.assertEqual(perms["launchProcess"], {"schemes": ["https"], "extensions": [".bat", ".pyw"]})
        self.assertEqual(perms["network"], {"domains": ["ws://localhost/"]})
        self.assertNotIn("clipboard", perms)  # UXP has no write-only level: the report is copied by hand


@unittest.skipIf(NODE is None, "node is not installed")
class NoticeTest(unittest.TestCase):
    def test_when_the_notice_shows(self):
        cases = [(UPDATE, True), (dict(UPDATE, skipped=True), False), (dict(UPDATE, newer=False), False),
                 (None, False), ({}, False), (dict(UPDATE, latest="9.9.9-beta"), False),
                 (dict(UPDATE, latest="<b>9</b>"), False), (dict(UPDATE, newer="yes"), False)]
        got = support(*[("notice", u) for u, _ in cases])
        self.assertEqual([g["show"] for g in got], [want for _, want in cases])
        self.assertEqual(got[0], {"show": True, "version": "9.9.9", "view": True})
        forged = support(("notice", dict(UPDATE, url="https://evil.example/x")))[0]
        self.assertEqual(forged, {"show": True, "version": "9.9.9", "view": False})
        self.assertEqual(support(("skipMessage", UPDATE), ("skipMessage", dict(UPDATE, skipped=True))),
                         [{"event": "skip_version", "version": "9.9.9"}, None])
        clean = support(("cleanUpdate", dict(UPDATE, url="javascript:x", published="<i>", extra="x")))[0]
        self.assertEqual(clean, dict(UPDATE, url=None, published=None))

    def test_the_line_view_and_issues_open_only_fixed_pages(self):
        out = connection(registered() + [{"click": "updateView"}, {"click": "issuesBtn"}])
        self.assertEqual(out["update"], {"display": "block", "text": "9.9.9 available", "view": "inline"})
        self.assertEqual([c["url"] for c in out["openExternal"]], [REL + "tag/v9.9.9", update.ISSUES_URL])
        self.assertEqual(out["openExternal"][0]["developerText"], "Agent Bridge opens a GitHub page in your browser.")
        self.assertEqual(out["escaped"], [])

    def test_a_forged_url_hides_view_and_opens_nothing(self):
        out = connection(registered(dict(UPDATE, url="https://evil.example/releases/x")) + [{"click": "updateView"}])
        self.assertEqual(out["update"], {"display": "block", "text": "9.9.9 available", "view": "none"})
        self.assertEqual(out["openExternal"], [])

    def test_no_notice_without_a_newer_release(self):
        for upd in (None, dict(UPDATE, newer=False), dict(UPDATE, skipped=True)):
            with self.subTest(update=upd):
                out = connection(registered(upd))
                self.assertEqual(out["update"]["display"], "none")
        out = connection([{"open": True}, {"message": {"event": "registered", "broker": "0.6.0"}}])  # a 0.6 broker
        self.assertIn(out["update"]["display"], (None, "none"))  # nothing of an unproven broker's is shown
        self.assertTrue(out["status"].startswith("Not paired: "), out["status"])

    def test_skip_sends_one_message_and_hides_the_line(self):
        out = connection(registered() + [{"click": "updateSkip"}])
        sent = out["sockets"][-1]["sent"]
        self.assertEqual(sent[-1], {"event": "skip_version", "version": "9.9.9"})
        self.assertEqual(out["update"]["display"], "none")
        refused = connection(registered() + [{"click": "updateSkip"}, {"message": {
            "event": "update", "update": UPDATE, "error": "settings.json is not JSON"}}])
        self.assertEqual(refused["update"]["display"], "block")  # the broker said no: the line is back
        self.assertIn("Skip this version: the broker refused (settings.json is not JSON).", refused["log"][-1])

    def test_a_later_check_shows_the_line(self):
        out = connection(registered(None) + [{"message": {"event": "update", "update": UPDATE}}])
        self.assertEqual(out["update"]["display"], "block")

    def test_opening_can_fail_quietly(self):
        out = connection(registered() + [{"click": "updateView"}], mock={"openExternal": "Permission denied"})
        self.assertIn(f"Could not open {REL}tag/v9.9.9: Permission denied", out["log"][-1])
        out = connection(registered() + [{"click": "issuesBtn"}], mock={"noOpenExternal": True})
        self.assertIn("this UXP build has no shell.openExternal", out["log"][-1])
        out = connection(registered() + [{"click": "issuesBtn"}], mock={"openExternal": "throw:boom"})
        self.assertIn("boom", out["log"][-1])
        self.assertEqual(out["escaped"], [])

    def test_chinese_premiere_gets_chinese_words(self):
        out = connection(registered(), mock={"uiLocale": "zh_CN"})
        self.assertEqual(out["update"]["text"], "有新版本 9.9.9")


@unittest.skipIf(NODE is None, "node is not installed")
class ReportTest(unittest.TestCase):
    def test_the_builder_takes_only_whitelisted_fields(self):
        info = {
            "version": "0.7.0", "protocol": 2, "modules": "commonjs", "build": {"dev": False, "packaged": True},
            "broker": "0.7.0", "connection": "connected", "update": UPDATE, "created": "2026-11-06T01:02:03.456Z",
            "hostInfo": {"name": "premierepro", "version": "27.1.0", "buildNumber": "31", "uiLocale": "zh_CN",
                         "applicationPath": win("C:", "Program Files", "Adobe", "Premiere")},
            "capabilities": {"razor": False, "transcribe": True, "subclip": "unknown", "<script>": True},
            "denied": {"reason": "crashes on open, see " + win("C:", "Users", "bob", "notes.txt")},
            "stuck": {"action": "insert_clip", "seconds": 42.4},
            "errors": [{"t": "10:00:01", "action": "insert_clip", "code": "NOT_FOUND", "outcome": "not_committed",
                        "error": "No item Client Secret Project/a.mov"},
                       {"t": "10:00:02", "label": "Reply to " + win("C:", "Users", "bob", "x") + " not sent"},
                       {"t": "10:00:03", "action": win("C:", "Users", "bob", "evil"), "code": "bad code", "outcome": "nope"}],
            "project": "Client Secret Project", "sequence": "Rough cut v3", "token": "SECRET-MESSAGE",
            "path": fwd("", "Users", "alice", "x"), "bridgeFolder": "\\\\fileserver\\share",
        }
        text = support(("buildReport", info))[0]
        for secret in SECRETS + ["Program Files", "notes.txt", "<script>"]:
            self.assertNotIn(secret, text)
        for line in ("- Panel 0.7.0 (protocol 2, commonjs, release build, installed as .ccx)",
                     "- Premiere 27.1.0 (build 31, zh_CN)", "- Broker 0.7.0: connected", "- Stuck on insert_clip for 42 s",
                     "- Update: 9.9.9 is available", "- Capabilities: 1 yes, 1 no (razor), 1 not probed yet",
                     "- 10:00:01 insert_clip: NOT_FOUND (not_committed)", "- 10:00:03 unknown: UNKNOWN (-)",
                     "- Created 2026-11-06T01:02:03Z", "[path]"):
            self.assertIn(line, text)

    def test_sanitize_cuts_paths(self):
        out = support(*[("sanitize", s) for s in ("open " + win("C:", "Users", "bob", "a.mov") + " now",
                                                    fwd("D:", "Job", "x.prproj"),
                                                    "\\\\host\\share\\x", "file:///" + fwd("C:", "x"), "at " + fwd("", "Users", "alice", "x"),
                                                    "(" + fwd("", "home", "bob", "y") + ")", "v0.7.1 ok")])
        self.assertEqual(out, ["open [path]", "[path]", "[path]", "[path]", "at [path]", "([path]", "v0.7.1 ok"])

    def test_note_error_keeps_codes_not_messages(self):
        out = support(("noteError", [], {"action": "insert_clip", "code": "NOT_FOUND", "outcome": "committed",
                                         "error": "SECRET-MESSAGE"}),
                      ("noteError", [], {"label": "Message failed: " + win("C:", "Users", "bob")}))
        self.assertEqual({k: v for k, v in out[0][0].items() if k != "t"},
                         {"action": "insert_clip", "code": "NOT_FOUND", "outcome": "committed"})
        self.assertNotIn("\\", out[1][0]["label"])
        lots = support(*[("noteError", [{"label": str(i)} for i in range(8)], {"label": "new"})])[0]
        self.assertEqual(len(lots), 8)

    def test_the_panel_shows_the_whole_report_before_copying(self):
        tmp = Path(tempfile.mkdtemp(prefix="ppro_report_panel_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        plugin = tmp / "Client Secret Project" / "plugin"
        plugin.mkdir(parents=True)
        (plugin.parent / "start_bridge.bat").write_text("rem\n", encoding="utf-8")
        steps = registered() + [
            {"message": {"id": "r1", "action": "insert_clip",
                         "params": {"itemName": win("C:", "Users", "bob", "Client Secret Project", "a.mov")}}},
            {"message": {"id": "r2", "action": "list_items", "params": {"bogus": "SECRET-MESSAGE"}}},
            {"wait": 50}, {"click": "reportBtn"}]
        out = connection(steps, pluginFolder=str(plugin))
        report = out["report"]
        self.assertEqual((report["display"], out["clipboard"]), ("block", []))  # shown, not copied yet
        text = report["text"]
        self.assertIn("### Premiere Agent Bridge problem report (from the panel)", text)
        self.assertIn("insert_clip: ", text)
        self.assertIn("list_items: BAD_PARAMS (not_committed)", text)
        for secret in SECRETS + [str(tmp), "Client Secret", "bogus", "FAILED", auth.read_token() or "-no-token-"]:
            self.assertNotIn(secret, text)
        self.assertIn("Client Secret Project", out["prompt"])  # the first-run prompt knows the folder; the report not

    def test_the_report_is_copied_by_hand_and_never_touches_the_clipboard(self):
        # live on 27.1 (6 Nov 2026): a click into the box selects it, Ctrl+C copies it; a script cannot (no permission,
        # and a selection made from a button does not take), so there is no Copy button
        steps = registered() + [{"click": "reportBtn"}]
        for mode in ("ok", "deny", None):
            with self.subTest(clipboard=mode):
                out = connection(steps, **({"clipboard": mode} if mode else {}))
                self.assertEqual(out["clipboard"], [])
                self.assertEqual(out["report"]["display"], "block")
        closed = connection(steps + [{"click": "reportClose"}])
        self.assertEqual(closed["report"]["display"], "none")

    def test_refusals_and_panel_errors_are_listed_by_label(self):
        out = connection([{"open": True}, {"close": {"code": 4409, "reason": "A live Agent Bridge panel is already "
                                                                               "connected"}},
                          {"safely": "sync"}, {"click": "reportBtn"}])
        text = out["report"]["text"]
        self.assertIn("refused by the broker (4409)", text)
        self.assertIn("Test callback failed", text)
        self.assertNotIn("sync boom", text)  # the error's message stays in the log
        self.assertIn("- Broker ?: disconnected, refused by the broker (close 4409)", text)


if __name__ == "__main__":
    unittest.main()
