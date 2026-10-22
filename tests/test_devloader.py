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

from ppro_bridge import devloader

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


if __name__ == "__main__":
    unittest.main()
