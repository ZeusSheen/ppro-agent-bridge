"""The panel starts the broker: the Start bridge button (plugin/lib/launcher.js through the node harness, with
uxp.shell.openPath mocked), start_bridge.pyw (the fallback UXP may need instead of the .bat) and the broker's
panel_state (so --load-panel does not reload the panel that just started it). Nothing here opens a file, starts a
process or talks to the real broker."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import asyncio
import importlib.machinery
import importlib.util
import json
import logging
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "panel" / "harness.js"
NODE = shutil.which("node")
DECLINED = "Permission denied: this file type is not allowed"


def launcher(req, timeout=60):
    p = subprocess.run([NODE, str(HARNESS), "launcher"], input=json.dumps(req), capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout)
    if p.returncode != 0:
        raise AssertionError(f"harness launcher exited {p.returncode}: {p.stdout}\n{p.stderr}")
    out = json.loads(p.stdout)
    assert out.get("ok") is True and "violations" not in out, out
    out["log"] = [line for line in out["log"] if " commands (protocol " not in line["text"]]  # the load line
    return out


def logged(out, cls=None):
    return [line["text"].split("] ", 1)[1] for line in out["log"] if cls is None or line["cls"] == cls]


@unittest.skipIf(NODE is None, "node is not installed")
class StartButtonTest(unittest.TestCase):
    """A temporary layout per test: <tmp>/plugin stands in for the plugin folder (uxp getPluginFolder)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ppro_launcher_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = self.tmp / "plugin"
        self.plugin.mkdir()

    def touch(self, *parts):
        path = self.tmp.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("rem\n", encoding="utf-8")
        return str(path)

    def run_panel(self, **req):
        return launcher(dict(req, pluginFolder=str(self.plugin)))

    def test_it_never_launches_without_a_click(self):
        self.touch("start_bridge.bat")
        out = self.run_panel(broker="down", waitMs=3500)  # the panel's own retries (every 3 s) run meanwhile
        self.assertEqual(out["openPath"], [])
        self.assertEqual(out["before"], {"display": "inline-block", "disabled": False, "text": "Start bridge"})
        self.assertEqual(out["after"], out["before"])

    def test_it_shows_only_while_the_panel_has_no_broker(self):
        self.assertEqual(self.run_panel(broker="up")["before"]["display"], "none")
        self.assertEqual(self.run_panel(broker="refusing")["before"]["display"], "none")  # it answers, refusing
        self.assertIsNone(self.run_panel()["before"]["display"])  # still connecting: not shown yet (no flash)
        self.touch("start_bridge.bat")
        out = self.run_panel(broker="refusing", clicks=1)  # a click that reaches it anyway
        self.assertEqual((out["openPath"], out["results"]), ([], [None]))
        out = self.run_panel(broker="down", unloading=True, clicks=1)  # unloaded: it would not connect anyway
        self.assertEqual((out["before"]["display"], out["openPath"], out["results"]), ("none", [], [None]))

    def test_launcher_json_wins_over_the_repo_layout(self):
        self.touch("start_bridge.bat")
        target = self.touch("checkout", "start_bridge.bat")
        (self.plugin / "launcher.json").write_text(json.dumps({"path": target}), encoding="utf-8")
        out = self.run_panel(broker="down", clicks=1)
        self.assertEqual([c["path"] for c in out["openPath"]], [target])
        self.assertEqual(out["results"][0]["source"], "launcher.json")
        self.assertTrue(out["results"][0]["ok"])
        self.assertEqual(out["after"], {"display": "inline-block", "disabled": True, "text": "Starting ..."})
        self.assertIn(f"Start bridge: opening {target} (launcher.json) ...", logged(out, "cmd"))

    def test_the_repo_layout_opens_start_bridge_bat_next_to_the_plugin_folder(self):
        bat = self.touch("start_bridge.bat")
        out = self.run_panel(broker="after_launch", clicks=1, waitMs=2500)  # the fast poll ticks every second
        self.assertEqual(out["openPath"], [{"path": bat, "result": "",
                                            "developerText": "Agent Bridge starts its broker: a console window "
                                                             "opens. Keep it open while you edit."}])
        self.assertEqual(out["results"][0]["source"], "next to the plugin folder")
        self.assertIn("Start bridge: Windows started start_bridge.bat; waiting for the broker ...", logged(out, "ok"))
        self.assertIn("Start bridge: connected to the broker.", logged(out, "ok"))
        self.assertEqual(out["after"], {"display": "none", "disabled": False, "text": "Start bridge"})

    def test_a_missing_launcher_says_where_it_looked_and_opens_nothing(self):
        out = self.run_panel(broker="down", clicks=1)
        self.assertEqual(out["openPath"], [])
        bat, pyw = str(self.tmp / "start_bridge.bat"), str(self.tmp / "start_bridge.pyw")
        message = out["results"][0]["message"]
        self.assertFalse(out["results"][0]["ok"])
        self.assertIn(f"no launcher at {bat} or {pyw}", message)
        self.assertIn("double-click start_bridge.bat", message)
        self.assertIn(message, logged(out, "err"))
        self.assertEqual(out["after"], {"display": "inline-block", "disabled": False, "text": "Start bridge"})

        target = str(self.tmp / "moved" / "start_bridge.bat")
        (self.plugin / "launcher.json").write_text(json.dumps({"path": target}), encoding="utf-8")
        (self.tmp / "moved").mkdir()
        message = self.run_panel(broker="down", clicks=1)["results"][0]["message"]
        self.assertIn(f"no launcher at {target}", message)
        self.assertIn("rebuild and reinstall the package", message)

    def test_a_bad_launcher_json_is_reported(self):
        for text, words in (("{not json", "is not JSON"), (json.dumps({"path": "start_bridge.bat"}), "absolute"),
                            (json.dumps(["x"]), "absolute")):
            with self.subTest(text=text):
                (self.plugin / "launcher.json").write_text(text, encoding="utf-8")
                out = self.run_panel(broker="down", clicks=1)
                self.assertEqual(out["openPath"], [])
                message = out["results"][0]["message"]
                self.assertIn("launcher.json in", message)
                self.assertIn(words, message)
                self.assertIn("scripts/build_ccx.py", message)
        bom = b"\xef\xbb\xbf" + json.dumps({"path": self.touch("a.bat")}).encode()
        (self.plugin / "launcher.json").write_bytes(bom)
        self.assertTrue(self.run_panel(broker="down", clicks=1)["results"][0]["ok"])  # a BOM is fine

    def test_an_open_path_error_is_shown_and_the_pyw_is_tried(self):
        bat = self.touch("start_bridge.bat")
        out = self.run_panel(broker="down", clicks=1, mock={"openPath": {".bat": DECLINED}})
        self.assertEqual([c["path"] for c in out["openPath"]], [bat])  # no start_bridge.pyw beside it
        self.assertFalse(out["results"][0]["ok"])
        self.assertIn(f"Start bridge: UXP could not open {bat}: {DECLINED}", logged(out, "err"))
        self.assertIn("Start bridge: the broker was not started.", out["results"][0]["message"])
        self.assertFalse(out["after"]["disabled"])

        pyw = self.touch("start_bridge.pyw")
        out = self.run_panel(broker="down", clicks=1, mock={"openPath": {".bat": DECLINED}})
        self.assertEqual([(c["path"], c["result"]) for c in out["openPath"]], [(bat, DECLINED), (pyw, "")])
        self.assertTrue(out["results"][0]["ok"])
        self.assertEqual(out["results"][0]["tried"], [{"path": bat, "error": DECLINED}, {"path": pyw, "error": ""}])

        out = self.run_panel(broker="down", clicks=1, mock={"openPath": "throw:openPath exploded"})
        self.assertIn(f"Start bridge: UXP could not open {pyw}: openPath exploded", logged(out, "err"))
        out = self.run_panel(broker="down", clicks=1, mock={"noShell": True})
        self.assertIn("this UXP build has no shell.openPath", out["results"][0]["tried"][0]["error"])

    def test_without_a_folder_listing_open_path_decides(self):
        bat = str(self.tmp / "start_bridge.bat")  # not there, but this build cannot tell
        out = self.run_panel(broker="down", clicks=1, mock={"noEntryWithUrl": True,
                                                            "openPath": {".bat": "The system cannot find the file"}})
        self.assertEqual([c["path"] for c in out["openPath"]], [bat, bat[:-4] + ".pyw"])
        self.assertIn(f"Start bridge: UXP could not open {bat}: The system cannot find the file", logged(out, "err"))

    def test_one_launch_at_a_time_and_the_consent_text_follows_the_ui_language(self):
        self.touch("start_bridge.bat")
        out = self.run_panel(broker="down", clicks=2, mock={"uiLocale": "zh_CN"})
        self.assertEqual(len(out["openPath"]), 1)
        self.assertIsNone(out["results"][1])  # the second click came while the first was waiting for the broker
        self.assertIn("命令行窗口", out["openPath"][0]["developerText"])


def load_pyw():
    loader = importlib.machinery.SourceFileLoader("start_bridge_pyw", str(REPO / "start_bridge.pyw"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class StartBridgePywTest(unittest.TestCase):
    def setUp(self):
        self.pyw = load_pyw()

    def test_it_opens_start_bridge_bat_in_its_own_console(self):
        with mock.patch.object(self.pyw, "broker_running", return_value=False), \
                mock.patch.object(self.pyw.os, "name", "nt"), \
                mock.patch.object(self.pyw.subprocess, "Popen") as popen, mock.patch.object(self.pyw, "tell") as tell:
            self.assertEqual(self.pyw.main(), 0)
        tell.assert_not_called()
        self.assertEqual(popen.call_args.args[0], ["cmd", "/c", "start", "Premiere Agent Bridge",
                                                   str(REPO / "start_bridge.bat")])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(REPO))

    def test_it_starts_nothing_while_the_broker_runs(self):
        with mock.patch.object(self.pyw, "broker_running", return_value=True), \
                mock.patch.object(self.pyw.subprocess, "Popen") as popen:
            self.assertEqual(self.pyw.main(), 0)
        popen.assert_not_called()

    def test_failures_go_to_a_message_box(self):
        with mock.patch.object(self.pyw, "broker_running", return_value=False), \
                mock.patch.object(self.pyw.os, "name", "nt"), \
                mock.patch.object(self.pyw, "START_SCRIPT", REPO / "no_such_start_bridge.bat"), \
                mock.patch.object(self.pyw.subprocess, "Popen") as popen, mock.patch.object(self.pyw, "tell") as tell:
            self.assertEqual(self.pyw.main(), 1)
        popen.assert_not_called()
        self.assertIn("no_such_start_bridge.bat is missing", tell.call_args.args[0])
        with mock.patch.object(self.pyw, "broker_running", return_value=False), \
                mock.patch.object(self.pyw.os, "name", "nt"), \
                mock.patch.object(self.pyw.subprocess, "Popen", side_effect=OSError("no cmd")), \
                mock.patch.object(self.pyw, "tell") as tell:
            self.assertEqual(self.pyw.main(), 1)
        self.assertIn("no cmd", tell.call_args.args[0])

    def test_the_port_follows_the_broker(self):
        with mock.patch.dict("os.environ", {"PPRO_BRIDGE_PORT": "9123"}):
            self.assertEqual(self.pyw.broker_port(), 9123)
        with mock.patch.dict("os.environ", {"PPRO_BRIDGE_PORT": "x"}):
            self.assertEqual(self.pyw.broker_port(), 8017)
        self.assertFalse(self.pyw.broker_running(helpers.free_port()))


class FakePanelSocket:
    """The panel end of the broker: answers each forwarded request with `data` (or `reply`)."""

    def __init__(self, broker, data=None, reply=None):
        self.broker, self.data, self.reply, self.sent = broker, data, reply, []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        reply = dict(self.reply or {"ok": True, "data": self.data}, id=msg["id"])
        _, fut = self.broker.pending.pop(msg["id"])
        fut.set_result(reply)


class BrokerPanelStateTest(unittest.TestCase):
    """broker.panel_state, in process (the module is imported; nothing listens)."""

    @classmethod
    def setUpClass(cls):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        import broker  # its logging.basicConfig would make every later test's websockets server chatty
        root.handlers[:] = handlers
        root.setLevel(level)
        cls.broker = broker

    def state(self, panel, hold_lock=False):
        b = self.broker

        async def go():
            with mock.patch.object(b, "panel_ws", panel), mock.patch.object(b, "panel_lock", asyncio.Lock()):
                if hold_lock:
                    await b.panel_lock.acquire()
                return await b.panel_state()

        return asyncio.run(go())

    def setUp(self):
        self.broker._panel_seen.clear()

    def test_no_panel_and_a_busy_panel(self):
        self.assertIsNone(self.state(None))
        sock = FakePanelSocket(self.broker, {"pong": True})
        self.assertEqual(self.state(sock, hold_lock=True), {"busy": True})
        self.assertEqual(sock.sent, [])  # never queued behind a running request

    def test_code_hash_and_packaged_come_from_one_ping_per_panel(self):
        sock = FakePanelSocket(self.broker, {"pong": True, "codeHash": "1a2b3c4d",
                                             "build": {"dev": False, "packaged": True}})
        expected = {"busy": False, "codeHash": "1a2b3c4d", "packaged": True}
        self.assertEqual(self.state(sock), expected)
        self.assertEqual([(m["action"], m["params"]) for m in sock.sent], [("ping", {})])
        self.assertTrue(sock.sent[0]["id"].startswith("broker-state-"))
        self.assertEqual(self.state(sock), expected)
        self.assertEqual(len(sock.sent), 1)  # asked once while it stays connected

    def test_a_panel_without_code_hash_or_build_is_asked_again(self):
        sock = FakePanelSocket(self.broker, {"pong": True})  # 0.5 panels send no build, a fresh one no hash yet
        self.assertEqual(self.state(sock), {"busy": False, "codeHash": None, "packaged": False})
        self.state(sock)
        self.assertEqual(len(sock.sent), 2)
        failed = FakePanelSocket(self.broker, reply={"ok": False, "code": "INTERNAL", "error": "x"})
        self.assertEqual(self.state(failed), {"busy": False})


if __name__ == "__main__":
    unittest.main()
