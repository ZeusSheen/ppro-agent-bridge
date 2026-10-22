"""ppro_bridge.setup_ui: the setup window's local server - its guards (secret path, Host, Origin, the POST header and
body), the state and the demo machine, one action at a time, how it stops, and the real actions with doctor, devloader
and every program launch mocked (nothing here installs, starts or creates anything)."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port)

import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ppro_bridge import doctor, setup_ui
from ppro_bridge._version import VERSION

STATE_KEYS = {"version", "demo", "platform", "repo", "python", "pythonExe", "premiereMin", "checks", "shortcuts",
              "brokerRunning", "busy"}
CHECK_NAMES = ["Python", "websockets", "UXP Developer Tool", "UXP developer mode", "Premiere Pro (UXP)",
               "Bridge data folder", "Start script", "Broker", "ffmpeg", "ffprobe", "PIL", "faster_whisper"]
POST_HEADERS = {"X-Bridge-Setup": "1", "Content-Type": "application/json"}


def request(server, method, path, body=None, headers=None, host=None):
    """(status, headers, raw body) of one request; the Host header is the right one unless `host` says otherwise."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=20)
    try:
        sent = {"Host": host or server.host}
        sent.update(headers or {})
        data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        conn.request(method, path, body=data, headers=sent)
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


def post(server, route, body, headers=POST_HEADERS):
    status, _, raw = request(server, "POST", server.prefix + route, body, headers)
    return status, (json.loads(raw) if raw.startswith(b"{") else raw)


def get_state(server):
    status, headers, raw = request(server, "GET", server.prefix + "api/state")
    assert status == 200, (status, raw)
    return json.loads(raw)


def by_name(state):
    return {c["name"]: c for c in state["checks"]}


class DemoServerTest(unittest.TestCase):
    def setUp(self):
        self.server = setup_ui.serve(port=0, open_window=False, demo=True)
        self.server.machine.step_seconds = 0.05
        self.addCleanup(self.server.shutdown)

    # -- the page and the guards

    def test_listens_on_loopback_under_a_secret_path(self):
        self.assertEqual(self.server.httpd.server_address[0], "127.0.0.1")
        self.assertTrue(self.server.url.startswith(f"http://127.0.0.1:{self.server.port}/"))
        self.assertGreaterEqual(len(self.server.secret), 24)
        other = setup_ui.SetupServer(demo=True)
        other.shutdown()
        self.assertNotEqual(self.server.secret, other.secret)  # fresh every run

    def test_page(self):
        if not setup_ui.PAGE.is_file():
            self.skipTest("ppro_bridge/setup_ui.html is not written yet")
        status, headers, raw = request(self.server, "GET", self.server.prefix)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertTrue(raw.lstrip().lower().startswith(b"<!doctype html"))
        self.assertEqual(headers["Content-Security-Policy"], setup_ui.CSP)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_a_missing_page_is_500(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(setup_ui, "PAGE", Path(tmp) / "none.html"), \
                self.assertLogs("setup_ui", "WARNING") as logged:
            status, _, raw = request(self.server, "GET", self.server.prefix)
        self.assertEqual(status, 500)
        self.assertIn(b"setup_ui.html", raw)
        self.assertNotIn(self.server.secret, "\n".join(logged.output))  # the log never shows the secret path

    def test_wrong_or_missing_secret_is_404(self):
        for path in ("/", "/api/state", "/wrong/api/state", "/" + self.server.secret, "/" + self.server.secret[:-1]
                     + "/api/state", "/" + self.server.secret.upper() + "x/"):
            self.assertEqual(request(self.server, "GET", path)[0], 404, path)
        self.assertEqual(post(self.server, "../wrong/api/bye", {})[0], 404)
        self.assertFalse(self.server.stopped)

    def test_wrong_host_is_403(self):
        port = self.server.port
        for host in (f"localhost:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", "evil.example", f"[::1]:{port}"):
            self.assertEqual(request(self.server, "GET", self.server.prefix + "api/state", host=host)[0], 403, host)
        self.assertEqual(request(self.server, "GET", self.server.prefix + "api/state")[0], 200)

    def test_foreign_origin_is_403(self):
        path = self.server.prefix + "api/state"
        for origin in ("http://evil.example", f"http://localhost:{self.server.port}", "null",
                       f"https://127.0.0.1:{self.server.port}"):
            self.assertEqual(request(self.server, "GET", path, headers={"Origin": origin})[0], 403, origin)
            self.assertEqual(post(self.server, "api/ping", {}, dict(POST_HEADERS, Origin=origin))[0], 403, origin)
        self.assertEqual(request(self.server, "GET", path, headers={"Origin": self.server.origin})[0], 200)
        self.assertEqual(post(self.server, "api/ping", {}, dict(POST_HEADERS, Origin=self.server.origin)),
                         (200, {"ok": True}))

    def test_cross_site_fetch_is_403(self):
        path = self.server.prefix + "api/state"
        self.assertEqual(request(self.server, "GET", path, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(request(self.server, "GET", path, headers={"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_post_needs_the_header_and_a_json_object(self):
        no_header = {"Content-Type": "application/json"}
        self.assertEqual(post(self.server, "api/action", {"action": "install-deps"}, no_header)[0], 403)
        self.assertEqual(post(self.server, "api/action", {"action": "install-deps"},
                              dict(no_header, **{"X-Bridge-Setup": "yes"}))[0], 403)
        self.assertEqual(post(self.server, "api/ping", b"not json")[0], 403)
        self.assertEqual(post(self.server, "api/ping", [1, 2])[0], 403)
        self.assertEqual(post(self.server, "api/ping", None)[0], 403)  # no body at all
        self.assertFalse(by_name(get_state(self.server))["websockets"]["ok"])  # nothing ran
        self.assertEqual(post(self.server, "api/ping", {}), (200, {"ok": True}))

    def test_wrong_method_for_a_route(self):
        self.assertEqual(request(self.server, "GET", self.server.prefix + "api/action")[0], 405)
        self.assertEqual(post(self.server, "api/state", {})[0], 405)
        self.assertEqual(request(self.server, "GET", self.server.prefix + "api/nothing")[0], 404)

    # -- state and actions

    def test_state_has_the_documented_keys_and_check_names(self):
        status, headers, raw = request(self.server, "GET", self.server.prefix + "api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        state = json.loads(raw)
        self.assertEqual(set(state), STATE_KEYS)
        self.assertEqual([c["name"] for c in state["checks"]], CHECK_NAMES)
        self.assertEqual((state["version"], state["demo"], state["busy"], state["platform"]),
                         (VERSION, True, None, sys.platform))
        self.assertEqual(state["premiereMin"], doctor.version_label(doctor.premiere_min()))
        self.assertEqual(state["shortcuts"], {"desktop": False, "autostart": False})
        self.assertFalse(state["brokerRunning"])
        for c in state["checks"]:
            self.assertEqual({"name", "ok", "required", "detail"} | ({"fix"} if not c["ok"] else set()), set(c), c)

    def test_demo_strings_carry_no_machine_paths(self):
        text = json.dumps(get_state(self.server))
        self.assertNotRegex(text, r"(?i)\b[a-z]:[\\/]")
        self.assertNotIn(str(doctor.REPO), text)

    def test_install_deps_fixes_websockets(self):
        status, reply = post(self.server, "api/action", {"action": "install-deps"})
        self.assertEqual(status, 200)
        self.assertEqual((reply["ok"], reply["action"]), (True, "install-deps"))
        self.assertIn("Successfully installed websockets", reply["output"])
        self.assertTrue(reply["message"])
        self.assertTrue(by_name(reply["state"])["websockets"]["ok"])
        self.assertTrue(by_name(get_state(self.server))["websockets"]["ok"])
        status, again = post(self.server, "api/action", {"action": "install-deps"})
        self.assertTrue(again["ok"])
        self.assertIn("already satisfied", again["output"])

    def test_demo_walkthrough(self):
        def act(name):
            status, reply = post(self.server, "api/action", {"action": name})
            self.assertEqual(status, 200, reply)
            return reply

        self.assertFalse(act("open-udt")["ok"])  # not installed yet
        reply = act("open-cc")
        self.assertTrue(reply["ok"])
        self.assertFalse(by_name(reply["state"])["UXP Developer Tool"]["ok"])  # the user is installing it ...
        self.assertTrue(by_name(get_state(self.server))["UXP Developer Tool"]["ok"])  # ... and comes back
        self.assertTrue(act("open-udt")["ok"])
        self.assertTrue(by_name(get_state(self.server))["UXP developer mode"]["ok"])
        for name in ("install-deps", "install-pillow", "install-whisper"):
            self.assertTrue(act(name)["ok"])
        reply = act("start-bridge")
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["state"]["brokerRunning"])
        self.assertTrue(by_name(reply["state"])["Broker"]["ok"])
        self.assertFalse(act("start-bridge")["ok"])  # already running
        self.assertEqual(act("shortcut-desktop")["state"]["shortcuts"], {"desktop": True, "autostart": False})
        self.assertTrue(act("autostart-on")["state"]["shortcuts"]["autostart"])
        self.assertFalse(act("autostart-off")["state"]["shortcuts"]["autostart"])
        for name in ("open-ffmpeg", "open-folder"):
            self.assertTrue(act(name)["ok"])
        self.assertTrue(all(c["ok"] for c in get_state(self.server)["checks"]))

    def test_unknown_action_is_400(self):
        for body in ({"action": "format-c"}, {"action": 3}, {}):
            status, reply = post(self.server, "api/action", body)
            self.assertEqual(status, 400, body)
            self.assertFalse(reply["ok"])
            self.assertIn("unknown action", reply["message"])

    def test_one_action_at_a_time(self):
        self.server.machine.step_seconds = 1.0
        first = []
        worker = threading.Thread(target=lambda: first.append(post(self.server, "api/action",
                                                                   {"action": "install-pillow"})))
        worker.start()
        deadline = time.monotonic() + 10
        while self.server.busy is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.server.busy, "install-pillow")
        self.assertEqual(get_state(self.server)["busy"], "install-pillow")
        status, reply = post(self.server, "api/action", {"action": "install-whisper"})
        self.assertEqual(status, 409)
        self.assertEqual((reply["ok"], reply["message"]), (False, "busy"))
        worker.join(10)
        self.assertEqual(first[0][0], 200)
        self.assertTrue(first[0][1]["ok"])
        self.assertIsNone(first[0][1]["state"]["busy"])
        self.assertFalse(by_name(get_state(self.server))["faster_whisper"]["ok"])  # the refused one never ran

    def test_bye_stops_the_server(self):
        self.assertEqual(post(self.server, "api/bye", {}), (200, {"ok": True}))
        self.assertTrue(self.server.wait(setup_ui.BYE_GRACE_SECONDS + 5))
        self.assertTrue(self.server.stopped)

    def test_a_reload_after_bye_keeps_the_server(self):
        server = setup_ui.serve(open_window=False, demo=True, bye_grace=0.5)
        self.addCleanup(server.shutdown)
        post(server, "api/bye", {})  # pagehide fires on a reload too ...
        get_state(server)  # ... and the reloaded page asks again at once
        self.assertFalse(server.wait(1.2))
        post(server, "api/bye", {})  # the window really closed: nothing follows
        self.assertTrue(server.wait(5))


class LifetimeTest(unittest.TestCase):
    def test_stops_after_idle_once_the_page_spoke(self):
        server = setup_ui.serve(open_window=False, demo=True, idle_timeout=0.3)
        self.addCleanup(server.shutdown)
        self.assertFalse(server.wait(0.8))  # no request yet: the window may still be opening
        get_state(server)
        self.assertTrue(server.wait(5))

    def test_never_idles_out_during_an_action(self):
        server = setup_ui.serve(open_window=False, demo=True, idle_timeout=0.2)
        self.addCleanup(server.shutdown)
        server.machine.step_seconds = 1.0
        status, reply = post(server, "api/action", {"action": "install-deps"})
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        self.assertTrue(server.wait(5))

    def test_shutdown_is_idempotent_and_works_before_start(self):
        server = setup_ui.SetupServer(demo=True)
        server.shutdown()
        server.shutdown()
        self.assertTrue(server.stopped)

    def test_serve_opens_the_window_on_the_url(self):
        with mock.patch.object(setup_ui, "open_app_window") as window:
            server = setup_ui.serve(demo=True)
        self.addCleanup(server.shutdown)
        window.assert_called_once_with(server.url)

    def test_main_prints_the_url_and_exits_0_on_bye(self):
        servers, codes, real_serve = [], [], setup_ui.serve

        def spy(*args, **kwargs):
            servers.append(real_serve(*args, **kwargs))
            return servers[-1]

        with mock.patch.object(setup_ui, "serve", side_effect=spy) as served, \
                mock.patch.object(setup_ui, "open_app_window") as window, \
                mock.patch.object(setup_ui.logging, "basicConfig"), \
                redirect_stdout(io.StringIO()) as out:
            runner = threading.Thread(target=lambda: codes.append(setup_ui.main(["--demo", "--no-browser"])))
            runner.start()
            deadline = time.monotonic() + 10
            while not servers and time.monotonic() < deadline:
                time.sleep(0.02)
            self.addCleanup(servers[0].shutdown)
            post(servers[0], "api/bye", {})
            runner.join(10)
        self.assertEqual(codes, [0])
        self.assertIn(servers[0].url, out.getvalue())
        self.assertEqual(served.call_args.kwargs, {"open_window": False, "demo": True})
        window.assert_not_called()


class StubMachine:
    demo = False

    def __init__(self, result=None, error=None):
        self.result, self.error, self.ran = result, error, []

    def info(self):
        return {"repo": "repo", "pythonExe": "python"}

    def checks(self, advance=True):
        return [doctor._ok("Broker", "running", required=False)]

    def shortcuts(self):
        return {"desktop": True, "autostart": False}

    def run(self, action):
        self.ran.append(action)
        if self.error:
            raise self.error
        return self.result


class ServerWithStubTest(unittest.TestCase):
    def test_output_is_the_last_4000_characters(self):
        server = setup_ui.SetupServer(machine=StubMachine((True, "done", "x" * 5000 + "\r\nEND")))
        status, reply = server.run_action("open-folder")
        self.assertEqual(status, 200)
        self.assertEqual(len(reply["output"]), setup_ui.OUTPUT_LIMIT)
        self.assertTrue(reply["output"].endswith("x\nEND"))
        self.assertTrue(reply["state"]["brokerRunning"])
        self.assertFalse(reply["state"]["demo"])
        server.shutdown()

    def test_an_exception_is_a_failed_action_not_a_dead_server(self):
        server = setup_ui.SetupServer(machine=StubMachine(error=RuntimeError("boom")))
        with self.assertLogs("setup_ui", "ERROR"):
            status, reply = server.run_action("open-udt")
        self.assertEqual((status, reply["ok"]), (200, False))
        self.assertIn("boom", reply["message"])
        self.assertIsNone(server.busy)
        server.shutdown()


class RealMachineTest(unittest.TestCase):
    """The real actions, with everything that would touch the machine mocked."""

    def setUp(self):
        self.machine = setup_ui.RealMachine()
        for name, folder in (("desktop_shortcut", "Desk"), ("autostart_shortcut", "Startup")):
            patch = mock.patch.object(doctor, name, return_value=Path(folder) / doctor.SHORTCUT_NAME)
            patch.start()
            self.addCleanup(patch.stop)

    def test_shortcut_desktop_uses_the_desktop_folder(self):
        with mock.patch.object(doctor, "make_shortcut", side_effect=lambda folder, minimized=False:
                               Path(folder) / doctor.SHORTCUT_NAME) as make:
            ok, message, output = self.machine.run("shortcut-desktop")
        self.assertTrue(ok, message)
        make.assert_called_once_with(Path("Desk"), minimized=False)
        self.assertEqual(output, str(Path("Desk") / doctor.SHORTCUT_NAME))

    def test_autostart_on_is_a_minimized_startup_shortcut(self):
        with mock.patch.object(doctor, "make_shortcut", return_value=Path("Startup") / doctor.SHORTCUT_NAME) as make:
            ok, _, _ = self.machine.run("autostart-on")
        self.assertTrue(ok)
        make.assert_called_once_with(Path("Startup"), minimized=True)

    def test_a_failed_shortcut_is_reported(self):
        with mock.patch.object(doctor, "make_shortcut", side_effect=OSError("access denied")):
            ok, message, _ = self.machine.run("shortcut-desktop")
        self.assertFalse(ok)
        self.assertIn("access denied", message)

    def test_autostart_off_calls_remove_autostart(self):
        for removed, words in ((True, "no longer"), (False, "was not")):
            with mock.patch.object(doctor, "remove_autostart", return_value=removed) as remove:
                ok, message, _ = self.machine.run("autostart-off")
            remove.assert_called_once_with()
            self.assertTrue(ok)
            self.assertIn(words, message)

    def test_start_bridge_refuses_when_it_is_running(self):
        with mock.patch.object(doctor, "broker_running", return_value=True), \
                mock.patch.object(setup_ui.subprocess, "Popen") as popen:
            ok, message, _ = self.machine.run("start-bridge")
        self.assertFalse(ok)
        self.assertIn("already running", message)
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "start_bridge.bat is Windows only")
    def test_start_bridge_opens_its_own_console_and_waits_for_the_broker(self):
        with mock.patch.object(doctor, "broker_running", side_effect=[False, False, True]), \
                mock.patch.object(setup_ui.subprocess, "Popen") as popen, \
                mock.patch.object(setup_ui, "START_POLL_SECONDS", 0.01):
            ok, message, _ = self.machine.run("start-bridge")
        self.assertTrue(ok, message)
        self.assertEqual(popen.call_args.args[0],
                         ["cmd", "/c", "start", "Premiere Agent Bridge", str(doctor.START_SCRIPT)])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(doctor.REPO))

    @unittest.skipUnless(os.name == "nt", "start_bridge.bat is Windows only")
    def test_start_bridge_says_when_the_broker_does_not_answer(self):
        with mock.patch.object(doctor, "broker_running", return_value=False), \
                mock.patch.object(setup_ui.subprocess, "Popen"), \
                mock.patch.object(setup_ui, "START_POLL_SECONDS", 0.01), \
                mock.patch.object(setup_ui, "START_WAIT_SECONDS", 0.05):
            ok, message, _ = self.machine.run("start-bridge")
        self.assertFalse(ok)
        self.assertIn("not answering", message)

    def test_install_deps_runs_pip_on_the_requirements(self):
        done = subprocess.CompletedProcess([], 0, stdout=b"Successfully installed websockets-15.0.1\r\n")
        with mock.patch.object(setup_ui.subprocess, "run", return_value=done) as run:
            ok, message, output = self.machine.run("install-deps")
        self.assertTrue(ok, message)
        self.assertEqual(output, "Successfully installed websockets-15.0.1\n")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(cmd[-2:], ["-r", str(doctor.REPO / "requirements.txt")])
        self.assertEqual(run.call_args.kwargs["cwd"], str(doctor.REPO))
        self.assertEqual(run.call_args.kwargs["timeout"], 900)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_optional_packages(self):
        done = subprocess.CompletedProcess([], 0, stdout=b"ok")
        for action, package in (("install-pillow", "pillow"), ("install-whisper", "faster-whisper")):
            with mock.patch.object(setup_ui.subprocess, "run", return_value=done) as run:
                ok, _, _ = self.machine.run(action)
            self.assertTrue(ok)
            self.assertEqual(run.call_args.args[0][-1], package)

    def test_pip_failure_and_timeout(self):
        failed = subprocess.CompletedProcess([], 1, stdout=b"ERROR: No matching distribution\n")
        with mock.patch.object(setup_ui.subprocess, "run", return_value=failed):
            ok, message, output = self.machine.run("install-deps")
        self.assertFalse(ok)
        self.assertIn("exit code 1", message)
        self.assertIn("No matching distribution", output)
        late = subprocess.TimeoutExpired(["pip"], 900, output=b"Downloading ...")
        with mock.patch.object(setup_ui.subprocess, "run", side_effect=late):
            ok, message, output = self.machine.run("install-whisper")
        self.assertFalse(ok)
        self.assertIn("15 minutes", message)
        self.assertEqual(output, "Downloading ...")

    def test_open_udt(self):
        with mock.patch.object(setup_ui.devloader, "find_udt", return_value=None), \
                mock.patch.object(setup_ui, "_launch") as launch:
            ok, message, _ = self.machine.run("open-udt")
        self.assertFalse(ok)
        self.assertIn("not installed", message)
        launch.assert_not_called()
        exe = Path("UDT") / "udt.exe"
        with mock.patch.object(setup_ui.devloader, "find_udt", return_value=exe), \
                mock.patch.object(setup_ui, "_launch") as launch:
            ok, message, _ = self.machine.run("open-udt")
        self.assertTrue(ok)
        self.assertIn("Enable Developer Mode", message)
        launch.assert_called_once_with(exe)

    def test_open_cc_app_or_its_web_page(self):
        exe = Path("ACC") / "Creative Cloud.exe"
        with mock.patch.object(setup_ui, "find_creative_cloud", return_value=exe), \
                mock.patch.object(setup_ui, "_launch") as launch, \
                mock.patch.object(setup_ui.webbrowser, "open") as browser:
            self.assertTrue(self.machine.run("open-cc")[0])
        launch.assert_called_once_with(exe)
        browser.assert_not_called()
        with mock.patch.object(setup_ui, "find_creative_cloud", return_value=None), \
                mock.patch.object(setup_ui, "_launch") as launch, \
                mock.patch.object(setup_ui.webbrowser, "open") as browser:
            self.assertTrue(self.machine.run("open-cc")[0])
        launch.assert_not_called()
        browser.assert_called_once_with(setup_ui.CC_URL)

    def test_open_ffmpeg_and_folder(self):
        with mock.patch.object(setup_ui.webbrowser, "open") as browser:
            self.assertTrue(self.machine.run("open-ffmpeg")[0])
        browser.assert_called_once_with("https://ffmpeg.org/download.html")
        with mock.patch.object(setup_ui, "_launch") as launch:
            self.assertTrue(self.machine.run("open-folder")[0])
        launch.assert_called_once_with(doctor.REPO)

    def test_state_through_the_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = Path(tmp) / doctor.SHORTCUT_NAME
            desk.write_text("", encoding="utf-8")
            checks = [doctor._ok("Python", "3.12"), doctor._ok("Broker", "running", required=False)]
            with mock.patch.object(doctor, "checks", return_value=checks), \
                    mock.patch.object(doctor, "desktop_shortcut", return_value=desk), \
                    mock.patch.object(doctor, "autostart_shortcut", return_value=Path(tmp) / "none.lnk"):
                server = setup_ui.serve(open_window=False)
                self.addCleanup(server.shutdown)
                state = get_state(server)
        self.assertEqual(set(state), STATE_KEYS)
        self.assertFalse(state["demo"])
        self.assertEqual(state["checks"], checks)
        self.assertTrue(state["brokerRunning"])
        self.assertEqual(state["shortcuts"], {"desktop": True, "autostart": False})
        self.assertEqual((state["repo"], state["pythonExe"]), (str(doctor.REPO), sys.executable))

    def test_shortcuts_when_the_shell_folders_cannot_be_found(self):
        with mock.patch.object(doctor, "desktop_shortcut", side_effect=OSError("no powershell")), \
                mock.patch.object(doctor, "autostart_shortcut", side_effect=OSError("no powershell")):
            machine = setup_ui.RealMachine()
            self.assertEqual(machine.shortcuts(), {"desktop": False, "autostart": False})
            ok, message, _ = machine.run("shortcut-desktop")
        self.assertFalse(ok)
        self.assertIn("Desktop folder", message)


class WindowTest(unittest.TestCase):
    URL = "http://127.0.0.1:1/secret/"

    @unittest.skipUnless(os.name == "nt", "Edge app mode is looked for on Windows")
    def test_edge_in_app_mode_without_waiting(self):
        edge = Path("Edge") / "msedge.exe"
        with mock.patch.object(setup_ui, "find_edge", return_value=edge), \
                mock.patch.object(setup_ui.subprocess, "Popen") as popen, \
                mock.patch.object(setup_ui.webbrowser, "open") as browser:
            self.assertEqual(setup_ui.open_app_window(self.URL), "edge")
        self.assertEqual(popen.call_args.args[0], [str(edge), f"--app={self.URL}", "--window-size=1120,780"])
        popen.return_value.wait.assert_not_called()
        browser.assert_not_called()

    def test_default_browser_without_edge(self):
        with mock.patch.object(setup_ui, "find_edge", return_value=None), \
                mock.patch.object(setup_ui.webbrowser, "open") as browser:
            self.assertEqual(setup_ui.open_app_window(self.URL), "browser")
        browser.assert_called_once_with(self.URL)

    @unittest.skipUnless(os.name == "nt", "Edge app mode is looked for on Windows")
    def test_default_browser_when_edge_will_not_start(self):
        with mock.patch.object(setup_ui, "find_edge", return_value=Path("msedge.exe")), \
                mock.patch.object(setup_ui.subprocess, "Popen", side_effect=OSError("bad exe")), \
                mock.patch.object(setup_ui.webbrowser, "open") as browser, \
                self.assertLogs("setup_ui", "WARNING"):
            self.assertEqual(setup_ui.open_app_window(self.URL), "browser")
        browser.assert_called_once_with(self.URL)

    def test_finding_edge_and_creative_cloud_in_program_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            edge = Path(tmp) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            acc = Path(tmp) / "Adobe" / "Adobe Creative Cloud" / "ACC" / "Creative Cloud.exe"
            for exe in (edge, acc):
                exe.parent.mkdir(parents=True)
                exe.write_bytes(b"")
            with mock.patch.dict(os.environ, {"ProgramFiles(x86)": tmp, "ProgramFiles": tmp}):
                self.assertEqual(setup_ui.find_edge(), edge)
                self.assertEqual(setup_ui.find_creative_cloud(), acc)
            with mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(Path(tmp) / "x"),
                                              "ProgramFiles": str(Path(tmp) / "y")}):
                self.assertIsNone(setup_ui.find_creative_cloud())


if __name__ == "__main__":
    unittest.main()
