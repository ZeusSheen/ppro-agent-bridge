"""ppro_bridge.devloader: loading the panel through UXP Developer Tool's service (/socket/cli), against a fake service
speaking the protocol of Adobe's devtools-cli (on a free port, never the real 14001)."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port)

import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import websockets

from ppro_bridge import devloader, panelhash

PREMIERE = {"appId": "premierepro", "appVersion": "27.1.0", "appName": "Premiere Pro (Beta)"}
PHOTOSHOP = {"appId": "PS", "appVersion": "27.11.0"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeUdt:
    """UDT's service as the CLI sees it: lists the apps, answers proxied loads, can announce a late app."""

    def __init__(self, apps, reply=None):
        self.apps = dict(apps)  # client id -> app info
        self.reply = reply if reply is not None else {"pluginSessionId": "s-1"}
        self.loads, self.paths, self.sockets = [], [], []

    async def handler(self, ws):
        self.paths.append(ws.request.path)
        self.sockets.append(ws)
        for cid, app in self.apps.items():
            await ws.send(json.dumps({"command": "didAddRuntimeClient", "id": cid, "platform": "win", "app": app}))
        await ws.send(json.dumps({"command": "didCompleteConnection"}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("command") == "proxy":
                self.loads.append(msg)
                await ws.send(json.dumps(dict(self.reply, command="reply", requestId=msg["requestId"])))

    async def announce(self, cid, app):
        for ws in self.sockets:
            await ws.send(json.dumps({"command": "didAddRuntimeClient", "id": cid, "platform": "win", "app": app}))


class DevLoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = Path(self.tmp.name) / "plugin"
        self.plugin.mkdir()
        (self.plugin / "manifest.json").write_text(json.dumps({"id": "org.pproagentbridge.panel"}), encoding="utf-8")

    def test_load_once_proxies_a_disk_load_to_each_premiere(self):
        udt, port = FakeUdt({7: PREMIERE, 8: PHOTOSHOP}), free_port()

        async def go():
            async with websockets.serve(udt.handler, "127.0.0.1", port):
                return await devloader.load_once(self.plugin, port, wait=2)

        results = asyncio.run(go())
        self.assertEqual(udt.paths, ["/socket/cli"])
        self.assertEqual(len(udt.loads), 1)  # Photoshop is left alone
        load = udt.loads[0]
        self.assertEqual((load["command"], load["clientId"]), ("proxy", 7))
        self.assertEqual(load["message"], {"command": "Plugin", "action": "load", "breakOnStart": False,
                                           "params": {"provider": {"type": "disk", "path": str(self.plugin.resolve()),
                                                                   "id": "org.pproagentbridge.panel"}}})
        self.assertEqual(results, [{"event": "load", "app": "premierepro", "appVersion": "27.1.0", "ok": True,
                                    "session": "s-1", "path": str(self.plugin.resolve())}])
        self.assertIn("Loaded the Agent Bridge panel into premierepro 27.1.0", devloader.describe(results[0]))

    def test_errors_and_no_premiere(self):
        udt, port = FakeUdt({7: PREMIERE}, reply={"error": "Plugin validation failed"}), free_port()

        async def go(service):
            async with websockets.serve(service.handler, "127.0.0.1", port):
                return await devloader.load_once(self.plugin, port, wait=1)

        results = asyncio.run(go(udt))
        self.assertEqual((results[0]["ok"], results[0]["error"]), (False, "Plugin validation failed"))
        self.assertEqual(asyncio.run(go(FakeUdt({8: PHOTOSHOP}))), [])

    def test_watch_loads_again_when_premiere_reconnects(self):
        udt, port, events = FakeUdt({7: PREMIERE}), free_port(), []

        async def go():
            stop = asyncio.Event()
            async with websockets.serve(udt.handler, "127.0.0.1", port):
                task = asyncio.create_task(devloader.watch(self.plugin, port, start=False, on_event=events.append,
                                                           stop=stop))
                for _ in range(50):
                    if udt.loads:
                        break
                    await asyncio.sleep(0.05)
                await udt.announce(9, PREMIERE)  # Premiere restarted and connected again
                for _ in range(50):
                    if len(udt.loads) == 2:
                        break
                    await asyncio.sleep(0.05)
                stop.set()
                await asyncio.wait_for(task, 5)

        asyncio.run(go())
        self.assertEqual([m["clientId"] for m in udt.loads], [7, 9])
        self.assertEqual([e["ok"] for e in events if e["event"] == "load"], [True, True])
        self.assertIn({"event": "udt", "state": "connected"}, events)

    def test_watch_without_udt_says_so_once(self):
        events = []

        async def go():
            stop = asyncio.Event()
            with mock.patch.object(devloader, "RETRY_SECONDS", 0.05), mock.patch.object(devloader, "PREMIERE_POLL", 0.05), \
                    mock.patch.object(devloader, "find_udt", return_value=None):
                task = asyncio.create_task(devloader.watch(self.plugin, free_port(), start=True,
                                                           on_event=events.append, stop=stop,
                                                           processes=lambda: {100}))
                await asyncio.sleep(0.3)
                stop.set()
                await asyncio.wait_for(task, 5)

        asyncio.run(go())
        missing = [e for e in events if e.get("state") == "missing"]
        self.assertEqual(len(missing), 1)
        self.assertIn("UXP Developer Tool not found", devloader.describe(missing[0]))

    def test_machine_checks(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict("os.environ", {"CommonProgramFiles": d}):
                self.assertIs(devloader.developer_mode(), False)
                f = devloader.developer_mode_file()
                f.parent.mkdir(parents=True)
                f.write_text('{"developer": true}', encoding="utf-8")
                self.assertIs(devloader.developer_mode(), True)
            exe = Path(d) / "udt.exe"
            with mock.patch.dict("os.environ", {devloader.UDT_ENV: str(exe)}):
                self.assertIsNone(devloader.find_udt())
                exe.write_bytes(b"")
                self.assertEqual(devloader.find_udt(), exe)
        self.assertTrue(devloader.is_premiere({"appId": "PPROBETA"}))
        self.assertFalse(devloader.is_premiere({"appId": "PS"}))

    # ---- a load replaces a loaded panel: none over a live one that needs none (broker panel_state)

    def _hash(self):
        for name in ("index.html", "main.js"):
            (self.plugin / name).write_text(name, encoding="utf-8")
        return panelhash.code_hash(self.plugin)

    def test_no_load_reason_reads_the_brokers_panel(self):
        mine = self._hash()

        def state(value):
            async def panel_state():
                if isinstance(value, Exception):
                    raise value
                return value
            return panel_state

        async def slow():
            await asyncio.sleep(5)

        reason = lambda ps, grace=0.2: asyncio.run(devloader.no_load_reason(ps, self.plugin, grace))  # noqa: E731
        self.assertIsNone(reason(state(None)))  # no panel within the grace: load
        self.assertIn("busy", reason(state({"busy": True})))
        self.assertIn("installed", reason(state({"busy": False, "codeHash": "0", "packaged": True})))
        self.assertIn("already runs this code", reason(state({"busy": False, "codeHash": mine})))
        self.assertIsNone(reason(state({"busy": False, "codeHash": "00000000"})))  # other code: replace it
        self.assertIsNone(reason(state({"busy": False})))                          # unknown code (0.4 panel)
        self.assertIsNone(reason(state(RuntimeError("broker trouble"))))
        self.assertIn("busy", reason(slow, grace=0))  # a ping that does not come back in time

        seen = []

        async def reconnecting():  # the panel comes back on its 3 s retry, inside the grace
            seen.append(1)
            return {"busy": False, "codeHash": mine} if len(seen) > 2 else None

        self.assertIn("already runs this code", reason(reconnecting, grace=2))

        hashing = []

        async def still_hashing():  # connected, but its codeHash comes a moment later: not "other code"
            hashing.append(1)
            return {"busy": False, "codeHash": mine if len(hashing) > 2 else None, "packaged": False}

        self.assertIn("already runs this code", reason(still_hashing, grace=4))
        self.assertEqual(len(hashing), 3)  # asked once a second while it had no hash, not every 0.25 s

        hashless = []

        async def old_panel():  # a 0.5 panel never sends a codeHash: asked about once a second, then loaded
            hashless.append(1)
            return {"busy": False, "codeHash": None, "packaged": False}

        self.assertIsNone(reason(old_panel, grace=1.5))
        self.assertLessEqual(len(hashless), 3)

    def _watch(self, panel_state, udt):
        events, port = [], free_port()

        async def go():
            stop = asyncio.Event()
            async with websockets.serve(udt.handler, "127.0.0.1", port):
                task = asyncio.create_task(devloader.watch(self.plugin, port, start=False, on_event=events.append,
                                                           stop=stop, panel_state=panel_state))
                for _ in range(100):
                    if any(e["event"] == "load" for e in events):  # a skip, or a load's reply (not just its request)
                        break
                    await asyncio.sleep(0.05)
                stop.set()
                await asyncio.wait_for(task, 5)

        with mock.patch.object(devloader, "LOAD_GRACE", 0.3):
            asyncio.run(go())
        return [e for e in events if e["event"] == "load"]

    def test_watch_loads_nothing_over_a_live_panel_with_this_code(self):
        mine = self._hash()

        async def live():
            return {"busy": False, "codeHash": mine, "packaged": False}

        udt = FakeUdt({7: PREMIERE})
        loads = self._watch(live, udt)
        self.assertEqual(udt.loads, [])
        self.assertEqual(loads, [{"event": "load", "skipped": True, "app": "premierepro", "appVersion": "27.1.0",
                                  "reason": "the panel connected to the broker already runs this code"}])
        self.assertEqual(devloader.describe(loads[0]), "Did not load the panel into premierepro 27.1.0: the panel "
                                                       "connected to the broker already runs this code.")

    def test_watch_replaces_a_panel_with_other_code_and_loads_without_one(self):
        self._hash()

        async def other():
            return {"busy": False, "codeHash": "00000000", "packaged": False}

        async def none():
            return None

        for panel_state in (other, none):
            udt = FakeUdt({7: PREMIERE})
            loads = self._watch(panel_state, udt)
            self.assertEqual([m["clientId"] for m in udt.loads], [7])
            self.assertEqual([e["ok"] for e in loads], [True])

    def test_udt_starts_off_the_brokers_console(self):
        """Closing the broker window must not close UDT: it starts detached, minimized, with no inherited handles."""
        with mock.patch.object(devloader.subprocess, "Popen") as popen:
            devloader.start_udt(Path("/UDT/Adobe UXP Developer Tools.exe"))
        (args,), kw = popen.call_args
        self.assertEqual(args, [str(Path("/UDT/Adobe UXP Developer Tools.exe"))])
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (devloader.subprocess.DEVNULL,) * 3)
        if devloader.os.name == "nt":
            want = devloader.subprocess.DETACHED_PROCESS | devloader.subprocess.CREATE_NEW_PROCESS_GROUP
            self.assertEqual(kw["creationflags"] & want, want)
            self.assertEqual(kw["startupinfo"].wShowWindow, 7)

    def test_watch_does_not_start_udt_for_an_installed_panel(self):
        events, starts, stops = [], [], []
        state = {"value": {"busy": False, "codeHash": None, "packaged": True}}
        pids = {"value": {100}}

        async def panel_state():
            return state["value"]

        async def go():
            stop = asyncio.Event()
            task = asyncio.create_task(devloader.watch(self.plugin, free_port(), start=True, on_event=events.append,
                                                       stop=stop, panel_state=panel_state,
                                                       processes=lambda: pids["value"]))
            for _ in range(60):  # several polls while the installed panel is connected
                await asyncio.sleep(0.05)
            self.assertEqual(starts, [])
            state["value"] = None  # Premiere quit, and started again without its panel: UDT is needed again
            pids["value"] = {200}
            for _ in range(100):
                if stops:
                    break
                await asyncio.sleep(0.05)
            stop.set()
            await asyncio.wait_for(task, 5)

        with mock.patch.object(devloader, "PREMIERE_POLL", 0.05), mock.patch.object(devloader, "LOAD_GRACE", 0.1), \
                mock.patch.object(devloader, "STARTUP_GRACE", 0.1), mock.patch.object(devloader, "UDT_START_WAIT", 0.3), \
                mock.patch.object(devloader, "find_udt", return_value=Path("udt.exe")), \
                mock.patch.object(devloader, "start_udt", side_effect=lambda exe: starts.append(exe) or "proc"), \
                mock.patch.object(devloader, "stop_udt", side_effect=lambda proc: stops.append(proc)):
            asyncio.run(go())
        not_needed = [e for e in events if e.get("state") == "not-needed"]
        self.assertEqual(len(not_needed), 1)  # said once, not every poll
        self.assertIn("Not starting UXP Developer Tool: an installed", devloader.describe(not_needed[0]))
        self.assertEqual(starts, [Path("udt.exe")])
        self.assertEqual(stops, ["proc"])  # it never came up: closed again all the same


class UdtWindowTest(unittest.TestCase):
    """RA-5: UXP Developer Tool's service takes any WebSocket (no Origin check, no credential) and can load any folder
    into Premiere, so the bridge runs it only while it loads the panel: started for a Premiere process it has not
    handled, closed once the panel answers (or the load failed), started again for a Premiere that starts later; a
    UDT the user opened is followed as before and never closed. A stand-in UDT on a free port (never 14001) that
    "starts" when start_udt is called and goes away when stop_udt is; a fake process list."""

    def setUp(self):
        import logging
        quiet = logging.getLogger("websockets")  # service_up's bare TCP probe makes the stand-in log a failed handshake
        level, quiet.level = quiet.level, logging.CRITICAL
        self.addCleanup(setattr, quiet, "level", level)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = Path(self.tmp.name) / "plugin"
        self.plugin.mkdir()
        (self.plugin / "manifest.json").write_text(json.dumps({"id": "org.pproagentbridge.panel"}), encoding="utf-8")
        for name in ("index.html", "main.js"):
            (self.plugin / name).write_text(name, encoding="utf-8")
        self.mine = panelhash.code_hash(self.plugin)
        self.port = free_port()
        self.events, self.starts, self.stops = [], [], []
        self.pids = {100}
        self.panel = None  # what the broker sees: None until a load "registers" the panel

    def fake_udt(self, udt):
        """start_udt / stop_udt for a stand-in UDT: start serves `udt` on self.port, stop closes it."""
        test = self

        class Proc:
            pid = None  # stop_udt must use the object start_udt returned, never a name

        def start(exe):
            loop = asyncio.get_running_loop()
            proc = Proc()
            test.starts.append(exe)

            async def serve():
                proc.server = await websockets.serve(udt.handler, "127.0.0.1", test.port)
            proc.task, proc.loop = loop.create_task(serve()), loop
            return proc

        def stop(proc):  # called in a worker thread (asyncio.to_thread), like the real taskkill
            test.stops.append(proc)
            proc.loop.call_soon_threadsafe(proc.server.close)
            return None

        return start, stop

    def run_watch(self, udt, script, user_udt=False, processes=None, **patches):
        start, stop = self.fake_udt(udt)
        processes = processes or (lambda: set(self.pids))

        async def panel_state():
            return self.panel

        async def on_load_reply():  # the panel registers a moment after a load succeeds
            while True:
                if udt.loads and self.panel is None and len(udt.loads) > self.loaded:
                    self.loaded = len(udt.loads)
                    await asyncio.sleep(0.1)
                    self.panel = {"busy": False, "codeHash": self.mine, "packaged": False}
                await asyncio.sleep(0.02)

        async def go():
            self.loaded = 0
            stop_event = asyncio.Event()
            server = await websockets.serve(udt.handler, "127.0.0.1", self.port) if user_udt else None
            helper = asyncio.create_task(on_load_reply())
            task = asyncio.create_task(devloader.watch(self.plugin, self.port, start=True, on_event=self.events.append,
                                                       stop=stop_event, panel_state=panel_state,
                                                       processes=processes))
            try:
                await script()
            finally:
                stop_event.set()
                await asyncio.wait_for(task, 10)
                helper.cancel()
                if server is not None:
                    server.close()

        values = dict(PREMIERE_POLL=0.05, LOAD_GRACE=0.2, STARTUP_GRACE=0.2, RETRY_SECONDS=0.05, UDT_START_WAIT=3,
                      PREMIERE_CONNECT_WAIT=3, ANSWER_WAIT=3, CLOSE_WAIT=2)
        values.update(patches)
        with mock.patch.multiple(devloader, find_udt=mock.Mock(return_value=Path("udt.exe")), start_udt=start,
                                 stop_udt=stop, **values):
            asyncio.run(go())

    async def until(self, check, seconds=8):
        deadline = asyncio.get_running_loop().time() + seconds
        while not check():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"timed out; events: {self.events}")
            await asyncio.sleep(0.02)

    def states(self):
        return [e.get("state") for e in self.events if e.get("event") == "udt"]

    def test_started_for_the_load_then_closed_and_again_for_a_new_premiere(self):
        udt = FakeUdt({7: PREMIERE})

        async def script():
            await self.until(lambda: self.stops)  # loaded, answered, closed
            self.assertFalse(devloader.service_up(self.port))  # 14001's stand-in is gone again
            await asyncio.sleep(0.3)
            self.assertEqual(len(self.starts), 1)  # nothing new: UDT stays closed
            self.pids, self.panel = {200}, None    # Premiere quit and started again (a new process, no panel)
            await self.until(lambda: len(self.stops) == 2)

        self.run_watch(udt, script)
        self.assertEqual((len(self.starts), len(self.stops), [m["clientId"] for m in udt.loads]), (2, 2, [7, 7]))
        closed = [e for e in self.events if e.get("state") == "closed"]
        self.assertEqual([e["reason"] for e in closed], ["the panel is loaded and answering"] * 2)
        self.assertIn("Closed the UXP Developer Tool the bridge started (the panel is loaded and answering).",
                      devloader.describe(closed[0]))
        self.assertEqual(self.states().count("starting"), 2)

    def test_a_process_list_that_fails_once_never_starts_or_keeps_udt(self):
        """8 Nov 2026: one failed tasklist put the broker in the old way: UDT started, never closed, restarted."""
        udt = FakeUdt({7: PREMIERE})
        real = set
        reads = {"n": 0}

        def flaky():  # unreadable three times, then the normal list
            reads["n"] += 1
            return None if reads["n"] <= 3 else real(self.pids)

        async def script():
            await self.until(lambda: reads["n"] >= 3)
            self.assertEqual(self.starts, [])  # nothing started while the list cannot be read
            await self.until(lambda: self.stops)
            await asyncio.sleep(0.3)

        self.run_watch(udt, script, processes=flaky, PROCESS_LIST_EXPECTED=True)
        self.assertEqual((len(self.starts), len(self.stops)), (1, 1))  # then the normal window: started and closed
        self.assertEqual(self.states().count("no-process-list"), 1)    # said once
        self.assertNotIn("disconnected", self.states())                # the old way is the only path that says this
        said = [e for e in self.events if e.get("state") == "no-process-list"][0]
        self.assertIn("not starting UXP Developer Tool", devloader.describe(said))

    def test_a_process_list_that_never_reads_starts_nothing(self):
        udt = FakeUdt({7: PREMIERE})

        async def script():
            await asyncio.sleep(0.4)

        self.run_watch(udt, script, processes=lambda: None, PROCESS_LIST_EXPECTED=True)
        self.assertEqual(self.starts, [])
        self.assertEqual(self.states(), ["no-process-list"])

    def test_a_failed_load_closes_it_too(self):
        udt = FakeUdt({7: PREMIERE}, reply={"error": "Plugin validation failed"})

        async def script():
            await self.until(lambda: self.stops)
            await asyncio.sleep(0.3)  # the same Premiere is not retried: no flapping UDT

        self.run_watch(udt, script)
        self.assertEqual((len(self.starts), len(self.stops)), (1, 1))
        self.assertEqual([e for e in self.events if e.get("state") == "closed"][0]["reason"], "the load failed")
        self.assertEqual([e["ok"] for e in self.events if e.get("event") == "load"], [False])

    def test_a_premiere_that_never_connects_does_not_keep_udt_open(self):
        udt = FakeUdt({8: PHOTOSHOP})

        async def script():
            await self.until(lambda: self.stops)

        self.run_watch(udt, script, PREMIERE_CONNECT_WAIT=0.3)
        failed = [e for e in self.events if e.get("event") == "load"]
        self.assertIn("did not connect to UXP Developer Tool", failed[0]["error"])
        self.assertEqual([e["reason"] for e in self.events if e.get("state") == "closed"],
                         ["Premiere did not connect to it"])

    def test_no_udt_for_a_panel_that_needs_no_load(self):
        self.panel = {"busy": False, "codeHash": self.mine, "packaged": False}  # its Start bridge button started us

        async def script():
            await asyncio.sleep(0.6)

        self.run_watch(FakeUdt({7: PREMIERE}), script)
        self.assertEqual((self.starts, self.stops), ([], []))
        self.assertEqual(self.states(), ["not-needed"])

    def test_a_udt_the_user_opened_is_followed_and_never_closed(self):
        udt = FakeUdt({7: PREMIERE})

        async def script():
            await self.until(lambda: udt.loads and self.panel)
            self.panel = None  # Premiere restarted while the user's UDT runs: its panel is gone, loaded as before
            await udt.announce(9, PREMIERE)
            await self.until(lambda: len(udt.loads) == 2)

        self.run_watch(udt, script, user_udt=True)
        self.assertEqual((self.starts, self.stops), ([], []))
        self.assertNotIn("closed", self.states())

    def test_stopping_the_broker_closes_a_udt_it_started(self):
        udt = FakeUdt({})  # Premiere never connects: the broker stops meanwhile

        async def script():
            await self.until(lambda: self.starts)
            await asyncio.sleep(0.2)

        self.run_watch(udt, script, PREMIERE_CONNECT_WAIT=30)
        self.assertEqual(len(self.stops), 1)
        self.assertEqual([e["reason"] for e in self.events if e.get("state") == "closed"], ["the broker is stopping"])

    def test_cancelling_the_watch_closes_a_udt_it_started(self):
        """broker.py stops its loader with task.cancel(): the UDT it started is ended on the way out too."""
        start, stop = self.fake_udt(FakeUdt({}))

        async def go():
            task = asyncio.create_task(devloader.watch(self.plugin, self.port, start=True, on_event=self.events.append,
                                                       processes=lambda: {100}))
            await self.until(lambda: self.starts)
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with mock.patch.multiple(devloader, find_udt=mock.Mock(return_value=Path("udt.exe")), start_udt=start,
                                 stop_udt=stop, PREMIERE_POLL=0.05, LOAD_GRACE=0.1, STARTUP_GRACE=0.1, CLOSE_WAIT=1,
                                 PREMIERE_CONNECT_WAIT=30):
            asyncio.run(go())
        self.assertEqual(len(self.stops), 1)
        self.assertEqual([e["reason"] for e in self.events if e.get("event") == "udt" and "reason" in e],
                         ["the broker is stopping"])

    def test_process_list_and_closing(self):
        listing = ('"Adobe Premiere Pro (Beta).exe","20708","Console","17","6,076,212 K"\n'
                   '"Adobe Premiere Pro.exe","123","Console","17","1,000 K"\n'
                   '"Adobe Premiere Pro Helper.exe","124","Console","17","1 K"\n'
                   'INFO: No tasks are running which match the specified criteria.\n')
        self.assertEqual(devloader.parse_tasklist(listing), {20708, 123})
        self.assertEqual(devloader.parse_tasklist(""), set())
        calls = []

        def run(args, **kw):
            calls.append(args)
            return mock.Mock(stdout=listing)

        if devloader.os.name == "nt":
            self.assertEqual(devloader.premiere_pids(run), {20708, 123})
            self.assertEqual(calls[0][:4], ["tasklist", "/FO", "CSV", "/NH"])
            with mock.patch.object(devloader.subprocess, "run", return_value=mock.Mock(returncode=0)) as taskkill:
                self.assertIsNone(devloader.stop_udt(mock.Mock(pid=4242)))
            self.assertEqual(taskkill.call_args[0][0], ["taskkill", "/PID", "4242", "/T", "/F"])  # by PID, never name
            with mock.patch.object(devloader.subprocess, "run", return_value=mock.Mock(returncode=128)):
                self.assertIsNone(devloader.stop_udt(mock.Mock(pid=4242)))  # already gone
        self.assertIsNone(devloader.stop_udt(None))


if __name__ == "__main__":
    unittest.main()
