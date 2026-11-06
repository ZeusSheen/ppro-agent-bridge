"""The update check (ppro_bridge/update.py, the broker's update loop and skip_version, ping's `update`): what is
sent, what is kept, when it asks, how it is turned off. Every request goes to a local HTTP server on 127.0.0.1:
nothing here talks to GitHub (helpers turns the check off for every other broker, and the brokers started here get a
dead HTTPS proxy as well, so a request that slipped through would fail at once instead of leaving the machine)."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import asyncio
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import websockets

from helpers import Bridge, controller_hello, free_port, panel_pair, run_cli
from ppro_bridge import report, update
from ppro_bridge._version import VERSION

REL = update.RELEASES_URL
GOOD = {"tag_name": "v9.9.9", "name": "v9.9.9 - something", "published_at": "2026-11-20T03:08:36Z",
        "html_url": REL + "tag/v9.9.9", "body": "SECRET RELEASE TEXT <script>alert(1)</script>", "draft": False,
        "prerelease": False, "assets": [{"name": "x.zip", "browser_download_url": "https://example.com/x.zip"}],
        "author": {"login": "someone"}}
DEAD_PROXY = f"http://127.0.0.1:{free_port()}"


class FakeGitHub:
    """A local stand-in for the releases API: answers with `routes[path]` = (status, headers, body bytes, delay s);
    records every request's path and headers."""

    def __init__(self):
        self.routes, self.requests = {}, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                status, headers, body, delay = fake.routes.get(self.path, (404, {}, b"{}", 0))
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def json(self, path, doc, status=200, delay=0, ctype="application/json; charset=utf-8"):
        self.routes[path] = (status, {"Content-Type": ctype}, json.dumps(doc).encode("utf-8"), delay)
        return self.base + path

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ppro_update_"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, data):
        (self.dir / name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def read(self, name):
        return json.loads((self.dir / name).read_text(encoding="utf-8"))


class ValuesTest(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(update.parse_version("v0.7.1"), (0, 7, 1))
        self.assertEqual(update.parse_version("10.0.12"), (10, 0, 12))
        for bad in ("0.7", "v0.7.1-beta", "0.7.1 ", "1.2.3.4", "", None, 7, "v12345.0.0", "0.7.1\n"):
            self.assertIsNone(update.parse_version(bad), bad)
        self.assertEqual(update.strict_version("0.7.1"), "0.7.1")
        for bad in ("v0.7.1", "0.07.1", "0.7.1;", "0.7.1\n", " 0.7.1", "0.7", 71, None, "１.２.３"):
            self.assertIsNone(update.strict_version(bad), repr(bad))

    def test_only_release_pages_of_this_repository(self):
        for good in (REL + "tag/v0.7.1", REL + "latest", REL + "tag/v0.7.1-rc.1"):
            self.assertEqual(update.release_url(good), good)
        bad = [REL, REL + "tag/", REL + "../../../evil/repo", REL + "tag/../../x", REL + "tag/%2e%2e",
               REL + "tag/v1?x=1", REL + "tag/v1#x", REL + "tag/v1@evil.example", REL + "tag\\v1", REL + "tag/v 1",
               update.REPO_URL + "-evil/releases/tag/v1", update.REPO_URL.replace("https:", "http:") + "/releases/tag/v1",
               "https://github.com.evil.example/x/releases/tag/v1", "https://evil.example/" + REL, None, 5,
               REL + "tag/" + "x" * 300]
        for url in bad:
            self.assertIsNone(update.release_url(url), url)

    def test_a_release_keeps_three_fields_and_never_the_text(self):
        self.assertEqual(update.parse_release(GOOD), {"latest": "9.9.9", "url": REL + "tag/v9.9.9",
                                                      "published": "2026-11-20T03:08:36Z"})
        forged = dict(GOOD, html_url="https://evil.example/download", published_at="<b>soon</b>")
        self.assertEqual(update.parse_release(forged), {"latest": "9.9.9", "url": None, "published": None})
        for doc in ({"tag_name": "latest"}, {"tag_name": None}, [], "v1.0.0", {}):
            with self.assertRaises(update.UpdateError) as cm:
                update.parse_release(doc)
            self.assertEqual(cm.exception.outcome, "bad_response")

    def test_the_urls_are_derived_from_one_repository_url(self):
        self.assertTrue(update.API_URL.startswith("https://api.github.com/repos/"))
        self.assertTrue(update.API_URL.endswith("/releases/latest"))
        self.assertEqual(update.ISSUES_URL, update.REPO_URL + "/issues/new")
        self.assertTrue(update.REPO_URL.startswith("https://github.com/"))


class FetchTest(unittest.TestCase):
    """update.fetch against the local server: the request it sends and every way it fails."""

    @classmethod
    def setUpClass(cls):
        cls.gh = FakeGitHub()

    @classmethod
    def tearDownClass(cls):
        cls.gh.stop()

    def outcome(self, url, **kw):
        with self.assertRaises(update.UpdateError) as cm:
            update.fetch(url, **kw)
        return cm.exception.outcome

    def test_the_request_carries_no_cookie_and_no_identifier(self):
        url = self.gh.json("/ok", GOOD)
        self.assertEqual(update.fetch(url, version="0.7.0")["tag_name"], "v9.9.9")
        sent = self.gh.requests[-1]["headers"]
        self.assertEqual(sent["user-agent"], "ppro-agent-bridge/0.7.0")
        self.assertEqual(sent["accept"], "application/vnd.github+json")
        self.assertLessEqual(set(sent), {"host", "user-agent", "accept", "accept-encoding", "connection"}, sent)
        self.assertNotIn("cookie", sent)

    def test_failures_have_short_outcomes(self):
        self.assertEqual(self.outcome(self.gh.json("/limited", {"message": "rate limit"}, status=403)), "http_403")
        self.assertEqual(self.outcome(self.gh.json("/html", GOOD, ctype="text/html")), "bad_response")
        self.gh.routes["/notjson"] = (200, {"Content-Type": "application/json"}, b"{nope", 0)
        self.assertEqual(self.outcome(self.gh.base + "/notjson"), "bad_response")
        self.assertEqual(self.outcome(self.gh.json("/slow", GOOD, delay=1.5), timeout=0.3), "timeout")
        self.assertEqual(self.outcome(f"http://127.0.0.1:{free_port()}/x"), "network")
        with mock.patch.object(update, "MAX_BYTES", 50):
            self.assertEqual(self.outcome(self.gh.json("/big", GOOD)), "too_large")

    def test_a_trickling_answer_ends_at_the_whole_request_deadline(self):
        """BA-2: the socket timeout is per read; a body sent a byte at a time, each well inside it, still ends at the
        request's own deadline."""
        stop = threading.Event()

        class Trickle(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100000")
                self.end_headers()
                try:
                    while not stop.is_set():
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.05)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(stop.set)
        began = time.monotonic()
        self.assertEqual(self.outcome(f"http://127.0.0.1:{httpd.server_address[1]}/trickle", timeout=0.6), "timeout")
        self.assertLess(time.monotonic() - began, 3.0)  # the server would go on for ever

    def test_redirects_stay_on_the_same_host(self):
        self.gh.json("/moved-here", GOOD)
        self.gh.routes["/same"] = (301, {"Location": self.gh.base + "/moved-here"}, b"", 0)
        self.assertEqual(update.fetch(self.gh.base + "/same")["tag_name"], "v9.9.9")
        other = self.gh.base.replace("127.0.0.1", "localhost") + "/moved-here"
        self.gh.routes["/away"] = (302, {"Location": other}, b"", 0)
        self.assertEqual(self.outcome(self.gh.base + "/away"), "redirect")


class CheckerTest(Folder):
    """Checker: one attempt, the cache, at most once a day, the snapshot, the skip."""

    @classmethod
    def setUpClass(cls):
        cls.gh = FakeGitHub()

    @classmethod
    def tearDownClass(cls):
        cls.gh.stop()

    def checker(self, url="/latest", doc=GOOD, version="0.7.0", environ=None, clock=None, **kw):
        full = self.gh.json(url, doc) if doc is not None else self.gh.base + url
        return update.Checker(folder=self.dir, version=version, url=full, environ=environ or {},
                              clock=clock or time.time, **kw)

    def test_one_attempt_keeps_only_version_date_and_url(self):
        c = self.checker()
        self.assertIsNone(c.snapshot())  # nothing known yet
        self.assertEqual(c.wait_seconds(), 0.0)
        rec = c.run()
        self.assertEqual((rec["event"], rec["outcome"], rec["latest"], rec["newer"]),
                         ("update_check", "ok", "9.9.9", True))
        cache = self.read("update.json")
        self.assertEqual(set(cache), {"latest", "url", "published", "checkedAt", "attemptedAt", "outcome"})
        text = json.dumps(cache)
        for leak in ("SECRET", "script", "x.zip", "someone", "something"):
            self.assertNotIn(leak, text)
        snap = c.snapshot()
        self.assertEqual(set(snap), {"latest", "url", "published", "newer", "skipped", "checkedAt"})
        self.assertEqual((snap["latest"], snap["url"], snap["newer"], snap["skipped"]),
                         ("9.9.9", REL + "tag/v9.9.9", True, False))
        self.assertGreater(c.wait_seconds(), update.INTERVAL - 60)
        again = update.Checker(folder=self.dir, version="0.7.0", url=self.gh.base + "/latest", environ={})
        self.assertEqual(again.snapshot(), snap)  # the next broker start shows it without asking
        self.assertGreater(again.wait_seconds(), update.INTERVAL - 60)

    def test_at_most_once_a_day_whatever_the_outcome(self):
        now = [1_800_000_000.0]
        c = self.checker("/down", doc=None, clock=lambda: now[0])
        self.assertEqual(c.run()["outcome"], "http_404")
        self.assertEqual(c.snapshot(), None)
        self.assertEqual(c.wait_seconds(), update.INTERVAL)  # a failure counts as the day's attempt
        now[0] += update.INTERVAL - 1
        self.assertEqual(c.wait_seconds(), 1)
        now[0] += 1
        self.assertEqual(c.wait_seconds(), 0)
        now[0] -= 5 * 3600 + update.INTERVAL  # the clock was set back past the last attempt
        self.assertEqual(c.wait_seconds(), 0)

    def test_a_failure_keeps_what_was_known(self):
        c = self.checker()
        c.run()
        c.url = self.gh.base + "/gone"
        rec = c.run()
        self.assertEqual(rec["outcome"], "http_404")
        self.assertNotIn("latest", rec)
        self.assertEqual(c.snapshot()["latest"], "9.9.9")
        self.assertEqual(self.read("update.json")["outcome"], "http_404")

    def test_newer_and_skipped(self):
        self.assertFalse(self.checker(version="9.9.9").run().get("newer"))
        self.assertFalse(update.snapshot({"latest": "9.9.9"}, {}, "10.0.0")["newer"])
        snap = update.snapshot({"latest": "9.9.9"}, {"skipVersion": "9.9.9"}, "0.7.0")
        self.assertEqual((snap["newer"], snap["skipped"]), (True, True))
        snap = update.snapshot({"latest": "9.10.0"}, {"skipVersion": "9.9.9"}, "0.7.0")
        self.assertEqual((snap["newer"], snap["skipped"]), (True, False))  # a newer one than the skipped shows
        self.assertFalse(update.snapshot({"latest": "9.9.9"}, {"skipVersion": "v9.9.9"}, "0.7.0")["skipped"])

    def test_skip_takes_only_the_announced_newer_release(self):
        self.write("settings.json", {"updateCheck": True, "other": [1, 2]})
        c = self.checker()
        self.assertEqual(c.skip("9.9.9"), (False, "only the newer release the broker announced can be skipped"))
        c.run()
        for bad in ("9.9.9; del", "v9.9.9", "9.9.10", "0.6.0", None, 999, "9.9.9\n", {"v": 1}):
            ok, why = c.skip(bad)
            self.assertFalse(ok, bad)
        self.assertNotIn("skipVersion", self.read("settings.json"))
        self.assertEqual(c.skip("9.9.9"), (True, None))
        self.assertEqual(self.read("settings.json"), {"updateCheck": True, "other": [1, 2], "skipVersion": "9.9.9"})
        self.assertTrue(c.snapshot()["skipped"])
        self.assertFalse(update.Checker(folder=self.dir, version="9.9.9", environ={}).skip("9.9.9")[0])  # not newer

    def test_a_broken_settings_file_is_never_overwritten(self):
        c = self.checker()
        c.run()
        self.write("settings.json", "{ my notes")
        ok, why = c.skip("9.9.9")
        self.assertFalse(ok)
        self.assertIn("settings.json is not JSON", why)
        self.assertEqual((self.dir / "settings.json").read_text(encoding="utf-8"), "{ my notes")

    def test_a_tampered_cache_is_checked_again(self):
        self.write("update.json", {"latest": "v9.9.9", "url": "https://evil.example/x", "published": "<i>",
                                   "checkedAt": "yesterday", "attemptedAt": "2026-11-06T01:02:03Z",
                                   "outcome": "ok; rm -rf", "body": "release text"})
        self.assertEqual(update.load_cache(self.dir), {"latest": "9.9.9", "url": None, "published": None,
                                                       "attemptedAt": "2026-11-06T01:02:03Z"})
        self.write("update.json", "[1, 2]")
        self.assertEqual(update.load_cache(self.dir), {})

    def test_off_by_environment_or_settings(self):
        for value in ("0", "false", "OFF", " no "):
            self.assertEqual(update.enabled({"PPRO_UPDATE_CHECK": value}, {}), (False, f"PPRO_UPDATE_CHECK={value}"))
        for value in ("1", "", "yes"):
            self.assertEqual(update.enabled({"PPRO_UPDATE_CHECK": value}, {}), (True, None))
        for value in (False, "false", 0, "off"):
            self.assertFalse(update.enabled({}, {"updateCheck": value})[0], value)
        self.assertTrue(update.enabled({}, {"updateCheck": True})[0])
        self.write("settings.json", {"updateCheck": False})
        c = self.checker()
        self.assertEqual((c.enabled, c.off_reason), (False, "settings.json updateCheck: false"))
        self.write("update.json", {"latest": "9.9.9", "attemptedAt": "2026-11-06T01:02:03Z"})
        off = update.Checker(folder=self.dir, version="0.7.0", environ={})
        self.assertIsNone(off.snapshot())  # off: nothing is announced
        self.assertEqual(off.skip("9.9.9"), (False, "the update check is off"))

    def test_status_says_it_in_one_line(self):
        self.assertEqual(update.status(self.dir, {})["text"], "not checked yet (the broker asks GitHub once a day)")
        self.assertEqual(update.status(self.dir, {"PPRO_UPDATE_CHECK": "0"})["text"], "check off (PPRO_UPDATE_CHECK=0)")
        self.write("update.json", {"latest": "9.9.9", "url": REL + "tag/v9.9.9", "checkedAt": "2026-11-06T01:02:03Z",
                                   "attemptedAt": "2026-11-06T01:02:03Z"})
        st = update.status(self.dir, {}, current="0.7.0")
        self.assertEqual((st["latest"], st["newer"], st["skipped"]), ("9.9.9", True, False))
        self.assertEqual(st["text"], f"9.9.9 is available (this is 0.7.0): {REL}tag/v9.9.9")
        self.assertEqual(update.status(self.dir, {}, current="9.9.9")["text"],
                         "up to date (latest release 9.9.9, checked 2026-11-06)")
        self.write("settings.json", {"skipVersion": "9.9.9"})
        self.assertEqual(update.status(self.dir, {}, current="0.7.0")["text"],
                         "9.9.9 is available, skipped (this is 0.7.0)")


class ShownElsewhereTest(Folder):
    """doctor and the setup window: one line each, read from update.json (nothing is sent)."""

    def setUp(self):
        super().setUp()
        self.write("update.json", {"latest": "9.9.9", "url": REL + "tag/v9.9.9", "checkedAt": "2026-11-06T01:02:03Z",
                                   "attemptedAt": "2026-11-06T01:02:03Z"})

    def test_doctor_prints_one_line(self):
        with mock.patch.dict(os.environ, {"PPRO_SETTINGS_DIR": str(self.dir)}), \
                mock.patch.dict(os.environ, {"PPRO_UPDATE_CHECK": ""}):
            from ppro_bridge import doctor
            self.assertEqual(doctor.update_line(), f"  Updates: 9.9.9 is available (this is {VERSION}): {REL}tag/v9.9.9")
            with mock.patch.dict(os.environ, {"PPRO_UPDATE_CHECK": "0"}):
                self.assertEqual(doctor.update_line(), "  Updates: check off (PPRO_UPDATE_CHECK=0)")

    def test_the_setup_window_shows_it(self):
        from ppro_bridge import setup_ui
        with mock.patch.dict(os.environ, {"PPRO_SETTINGS_DIR": str(self.dir), "PPRO_UPDATE_CHECK": ""}):
            real = setup_ui.RealMachine().update()
        self.assertEqual((real["latest"], real["newer"], real["enabled"]), ("9.9.9", True, True))
        demo = setup_ui.DemoMachine(step_seconds=0).update()
        self.assertEqual((demo["latest"], demo["newer"]), (VERSION, False))
        page = setup_ui.PAGE.read_text(encoding="utf-8")
        self.assertIn('id="updateLine"', page)
        self.assertIn("function updateText(u)", page)
        for key in ("update.off", "update.none", "update.newer", "update.skipped", "update.current"):
            self.assertEqual(page.count(f"'{key}':"), 2, key)  # English and Chinese


def load_broker():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    import broker  # its logging.basicConfig would make every later test's websockets server chatty
    root.handlers[:] = handlers
    root.setLevel(level)
    return broker


class BrokerLoopTest(Folder):
    """broker.update_loop in process: the check runs off the event loop, is audited, tells the panel, stays quiet."""

    @classmethod
    def setUpClass(cls):
        cls.broker = load_broker()
        cls.gh = FakeGitHub()

    @classmethod
    def tearDownClass(cls):
        cls.gh.stop()

    def run_loop(self, checker, panel=None, until=lambda recs: recs, seconds=10):
        b, records, ticks = self.broker, [], []

        async def go():
            task = asyncio.create_task(b.update_loop())
            deadline = time.monotonic() + seconds
            while not until(records) and time.monotonic() < deadline:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        with mock.patch.object(b, "UPDATES", checker), mock.patch.object(b, "panel_ws", panel), \
                mock.patch.object(b, "record", records.append):
            asyncio.run(go())
        return records, ticks

    def test_the_check_runs_in_a_thread_and_tells_the_panel(self):
        url = self.gh.json("/slow-latest", GOOD, delay=0.6)
        checker = update.Checker(folder=self.dir, version="0.7.0", url=url, environ={})

        class Panel:
            sent = []

            async def send(self, raw):
                self.sent.append(json.loads(raw))

        panel = Panel()
        records, ticks = self.run_loop(checker, panel, until=lambda r: r and panel.sent)
        self.assertEqual([(r["event"], r["outcome"], r["latest"]) for r in records], [("update_check", "ok", "9.9.9")])
        self.assertGreater(len(ticks), 10)  # the loop kept running while the request waited 0.6 s
        self.assertEqual(panel.sent[0]["event"], "update")
        self.assertEqual(panel.sent[0]["update"]["latest"], "9.9.9")
        self.assertEqual(len([r for r in self.gh.requests if r["path"] == "/slow-latest"]), 1)

    def test_failures_are_quiet(self):
        checker = update.Checker(folder=self.dir, version="0.7.0", url=f"http://127.0.0.1:{free_port()}/x",
                                 environ={})
        with self.assertNoLogs("broker", "INFO"):
            records, _ = self.run_loop(checker)
        self.assertEqual(records[0]["outcome"], "network")
        self.assertEqual(set(records[0]), {"event", "outcome", "ms"})

    def test_off_sends_nothing(self):
        checker = update.Checker(folder=self.dir, url=self.gh.base + "/never", environ={"PPRO_UPDATE_CHECK": "0"})
        records, _ = self.run_loop(checker)
        self.assertEqual(records, [{"event": "update_check", "outcome": "off", "reason": "PPRO_UPDATE_CHECK=0"}])
        self.assertFalse([r for r in self.gh.requests if r["path"] == "/never"])


class BrokerTest(Folder):
    """A broker subprocess with a fresh update.json (nothing due, so it asks nobody): ping and the panel's
    "registered" notice carry `update`; the registered panel, and only it, can skip the announced version."""

    def setUp(self):
        super().setUp()
        self.write("update.json", {"latest": "9.9.9", "url": REL + "tag/v9.9.9", "published": "2026-11-20T03:08:36Z",
                                   "checkedAt": update.stamp(time.time()), "attemptedAt": update.stamp(time.time()),
                                   "outcome": "ok"})
        self.assertGreater(update.Checker(folder=self.dir, environ={}).wait_seconds(), 3600)  # not due: no request
        self.audit = self.dir / "audit"
        self.bridge = Bridge(panel=False, broker_env={
            "PPRO_UPDATE_CHECK": "1", "PPRO_SETTINGS_DIR": self.dir, "PPRO_AUDIT_DIR": self.audit,
            "HTTPS_PROXY": DEAD_PROXY, "https_proxy": DEAD_PROXY})
        self.addCleanup(self.bridge.stop)

    def audit_lines(self):
        out = []
        for p in sorted(self.audit.glob("audit-*.jsonl")):
            out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return out

    def test_registered_ping_and_skip(self):
        async def go():
            async with websockets.connect(self.bridge.url, open_timeout=5) as panel:
                registered = await panel_pair(panel)

                async with websockets.connect(self.bridge.url, open_timeout=5) as ctl:  # a controller cannot skip
                    await ctl.send(controller_hello())
                    await ctl.send(json.dumps({"event": "skip_version", "version": "9.9.9"}))
                    bad = json.loads(await asyncio.wait_for(ctl.recv(), 10))
                    await ctl.send(json.dumps({"id": "p1", "action": "ping", "params": {}, "timeoutSeconds": 10}))
                    request = json.loads(await asyncio.wait_for(panel.recv(), 10))
                    await panel.send(json.dumps({"id": request["id"], "ok": True, "data": {"pong": True}}))
                    ping = json.loads(await asyncio.wait_for(ctl.recv(), 10))

                await panel.send(json.dumps({"event": "skip_version", "version": "9.9.9 && calc"}))
                refused = json.loads(await asyncio.wait_for(panel.recv(), 10))
                await panel.send(json.dumps({"event": "skip_version", "version": "9.9.9"}))
                skipped = json.loads(await asyncio.wait_for(panel.recv(), 10))
                return registered, bad, ping, refused, skipped

        registered, bad, ping, refused, skipped = asyncio.run(go())
        self.assertEqual(registered["event"], "registered")
        want = {"latest": "9.9.9", "url": REL + "tag/v9.9.9", "published": "2026-11-20T03:08:36Z", "newer": True,
                "skipped": False}
        self.assertEqual({k: registered["update"][k] for k in want}, want)
        self.assertEqual(bad["code"], "BAD_REQUEST")
        self.assertEqual(ping["update"], registered["update"])
        self.assertEqual((refused["event"], refused["update"]["skipped"], refused["error"]),
                         ("update", False, "not a version x.y.z"))
        self.assertEqual((skipped["event"], skipped["update"]["skipped"]), ("update", True))
        self.assertNotIn("error", skipped)
        self.assertEqual(self.read("settings.json"), {"skipVersion": "9.9.9"})
        rows = [(r["outcome"], r["version"]) for r in self.audit_lines() if r.get("event") == "skip_version"]
        self.assertEqual(rows, [("refused", "9.9.9 && calc"), ("ok", "9.9.9")])
        self.assertEqual([r for r in self.audit_lines() if r.get("event") == "update_check"], [])  # nothing asked

    def test_the_cli_ping_passes_update_through(self):
        fake = Bridge(panel=True, broker_env={"PPRO_UPDATE_CHECK": "1", "PPRO_SETTINGS_DIR": self.dir,
                                              "HTTPS_PROXY": DEAD_PROXY, "https_proxy": DEAD_PROXY})
        self.addCleanup(fake.stop)
        code, out, _ = run_cli("ping", url=fake.url)
        self.assertEqual(code, 0, out)
        data = out["data"]
        self.assertEqual((data["update"]["latest"], data["update"]["newer"]), ("9.9.9", True))
        rep = report.build(data, None, [])
        self.assertEqual(rep["update"], {"latest": "9.9.9", "newer": True, "skipped": False})
        self.assertIn("- Update: 9.9.9 is available", report.as_text(rep))

    def test_check_off_means_null(self):
        off = Bridge(panel=True, broker_env={"PPRO_SETTINGS_DIR": self.dir})  # helpers: PPRO_UPDATE_CHECK=0
        self.addCleanup(off.stop)
        code, out, _ = run_cli("ping", url=off.url)
        self.assertEqual(code, 0, out)
        self.assertIsNone(out["data"]["update"])


if __name__ == "__main__":
    unittest.main()
