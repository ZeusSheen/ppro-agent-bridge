"""transport: reads never report "unknown", the URL is read at call time, make_rpc pins a guard and refuses an old
panel, and every failure carries code/outcome/sent. A scripted broker on a free port stands in for the real one."""

import asyncio
import json
import os
import threading
import unittest
from unittest import mock

import websockets

from helpers import DEAD_URL, BridgeError, free_port

from ppro_bridge import spec, transport


class ScriptedBroker:
    """Answers each request with reply(msg) (plus "broker": "9.9.9") and records what it received."""

    def __init__(self, reply):
        self.reply, self.received = reply, []
        self.port = free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _run(self):
        async def handler(ws):
            await ws.recv()  # role
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                await ws.send(json.dumps(dict(self.reply(msg), id=msg["id"], broker="9.9.9")))

        async def main():
            async with websockets.serve(handler, "127.0.0.1", self.port):
                self.ready.set()
                await asyncio.Future()

        self.loop.run_until_complete(main())


def timeout_reply(msg):
    return {"ok": False, "code": "PANEL_TIMEOUT", "outcome": "unknown", "retryable": False, "error": "slow"}


class OutcomeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.broker = ScriptedBroker(timeout_reply)
        cls.rpc = staticmethod(transport.make_rpc(url=cls.broker.url))

    def test_read_action_unknown_becomes_not_committed(self):
        with self.assertRaises(BridgeError) as cm:
            self.rpc("get_project")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("PANEL_TIMEOUT", "not_committed"))

    def test_write_action_stays_unknown(self):
        with self.assertRaises(BridgeError) as cm:
            self.rpc("add_marker", {"atSeconds": 1.0, "name": "x"})
        self.assertEqual((cm.exception.code, cm.exception.outcome, cm.exception.retryable),
                         ("PANEL_TIMEOUT", "unknown", False))

    def test_raw_call_is_settled_too(self):
        reply = asyncio.run(transport.call("list_items", {}, url=self.broker.url))
        self.assertEqual(reply["outcome"], "not_committed")

    def test_timeout_comes_from_the_spec(self):
        asyncio.run(transport.call("export_otio", {"outputPath": "/fake/x.otio"}, url=self.broker.url))
        self.assertEqual(self.broker.received[-1]["timeoutSeconds"], spec.timeout("export_otio"))
        self.assertNotIn("guard", self.broker.received[-1])


class ReplyTest(unittest.TestCase):
    def test_ping_gets_the_broker_version_and_warnings_survive(self):
        broker = ScriptedBroker(lambda m: {"ok": True, "data": {"pong": True}, "warnings": ["careful"]})
        data = transport.make_rpc(url=broker.url)("ping")
        self.assertEqual((data["brokerVersion"], data["warnings"]), ("9.9.9", ["careful"]))

    def test_error_data_is_kept(self):
        broker = ScriptedBroker(lambda m: {"ok": False, "code": "BAD_PARAMS", "outcome": "not_committed",
                                           "error": "params.x: unknown param", "data": {"problems": ["x"]}})
        with self.assertRaises(BridgeError) as cm:
            transport.make_rpc(url=broker.url)("insert_clip", {"itemName": "a", "x": 1})
        self.assertEqual(cm.exception.data, {"problems": ["x"]})
        self.assertEqual(cm.exception.to_reply()["data"], {"problems": ["x"]})

    def test_no_broker(self):
        with self.assertRaises(BridgeError) as cm:
            transport.rpc("add_marker", {"atSeconds": 1.0, "name": "x"})
        e = cm.exception
        self.assertEqual((e.code, e.outcome, e.sent), ("NO_BROKER", "not_committed", False))
        self.assertIn(DEAD_URL, str(e))


class UrlTest(unittest.TestCase):
    def test_url_is_read_at_call_time(self):
        broker = ScriptedBroker(lambda m: {"ok": True, "data": {"name": "P"}})
        with mock.patch.dict(os.environ, {"PPRO_BRIDGE_URL": broker.url}):
            self.assertEqual(transport.broker_url(), broker.url)
            self.assertEqual(transport.rpc("get_project"), {"name": "P"})
        self.assertEqual(transport.broker_url(), DEAD_URL)

    def test_default_url(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(transport.broker_url(), "ws://127.0.0.1:8017")


class GuardTest(unittest.TestCase):
    GUARD = {"projectPrefix": "PAB_TEST", "sequenceGuid": "g-1"}

    def test_old_panel_is_refused_before_anything_else(self):
        broker = ScriptedBroker(lambda m: {"ok": True, "data": {"pong": True, "plugin": "old 0.3.3"}})
        rpc = transport.make_rpc(url=broker.url, guard=self.GUARD)
        with self.assertRaises(BridgeError) as cm:
            rpc("add_marker", {"atSeconds": 1.0, "name": "x"})
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("PANEL_OUTDATED", "not_committed"))
        self.assertEqual([m["action"] for m in broker.received], ["ping"])

    def test_guard_goes_with_every_request(self):
        def reply(m):
            return {"ok": True, "data": {"protocol": 2} if m["action"] == "ping" else {"name": "P"}}
        broker = ScriptedBroker(reply)
        rpc = transport.make_rpc(url=broker.url, guard=self.GUARD)
        rpc("get_project")
        rpc("get_sequence")
        self.assertEqual([m["action"] for m in broker.received], ["ping", "get_project", "get_sequence"])
        self.assertNotIn("guard", broker.received[0])
        self.assertEqual([m.get("guard") for m in broker.received[1:]], [self.GUARD, self.GUARD])

    def test_first_call_ping_is_not_sent_twice(self):
        broker = ScriptedBroker(lambda m: {"ok": True, "data": {"protocol": 2}})
        data = transport.make_rpc(url=broker.url, guard=self.GUARD)("ping")
        self.assertEqual(data["protocol"], 2)
        self.assertEqual(len(broker.received), 1)


class LostTest(unittest.TestCase):
    def test_lost_before_and_after_send(self):
        self.assertEqual(transport.lost("x", False, "NO_BROKER")["outcome"], "not_committed")
        after = transport.lost("x", True, "TIMEOUT")
        self.assertEqual((after["outcome"], after["sent"]), ("unknown", True))
        self.assertIn("check the timeline", after["error"])


if __name__ == "__main__":
    unittest.main()
