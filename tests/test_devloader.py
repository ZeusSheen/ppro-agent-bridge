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
            with mock.patch.object(devloader, "RETRY_SECONDS", 0.05), mock.patch.object(devloader, "find_udt",
                                                                                       return_value=None):
                task = asyncio.create_task(devloader.watch(self.plugin, free_port(), start=True,
                                                           on_event=events.append, stop=stop))
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
        events, starts, state = [], [], {"value": {"busy": False, "codeHash": None, "packaged": True}}

        asked = []

        async def panel_state():
            asked.append(1)
            return state["value"]

        async def go():
            stop = asyncio.Event()
            task = asyncio.create_task(devloader.watch(self.plugin, free_port(), start=True, on_event=events.append,
                                                       stop=stop, panel_state=panel_state))
            for _ in range(100):  # several retries while the installed panel is connected
                if sum(e.get("state") == "not-needed" for e in events) and len(asked) >= 3:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(starts, [])
            state["value"] = None  # Premiere quit: the panel is gone, so UDT is needed again
            for _ in range(100):
                if starts:
                    break
                await asyncio.sleep(0.05)
            stop.set()
            await asyncio.wait_for(task, 5)

        # UDT's service is down (refused at once: a closed port on Windows takes ~2 s to refuse)
        with mock.patch.object(devloader, "RETRY_SECONDS", 0.05), mock.patch.object(devloader, "LOAD_GRACE", 0.1), \
                mock.patch.object(devloader, "find_udt", return_value=Path("udt.exe")), \
                mock.patch.object(devloader, "start_udt", side_effect=starts.append), \
                mock.patch.object(websockets, "connect", side_effect=ConnectionRefusedError):
            asyncio.run(go())
        not_needed = [e for e in events if e.get("state") == "not-needed"]
        self.assertEqual(len(not_needed), 1)  # said once, not every retry
        self.assertIn("Not starting UXP Developer Tool: an installed", devloader.describe(not_needed[0]))
        self.assertEqual(starts, [Path("udt.exe")])


if __name__ == "__main__":
    unittest.main()
