"""wsclient, the CLI's standard-library WebSocket client, and transport.call_sync on top of it: the real broker (and
fake panel) on a free port, websockets servers that fragment, ping, close or stall, and raw sockets that answer the
opening handshake wrongly. Every failure must map to the same code / outcome / sent as transport.call (websockets).
A CLI `ping` must not import asyncio, websockets or the editing modules (start-up time). Nothing touches 8017."""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import unittest
import uuid
from unittest import mock

import websockets

from helpers import CLI, Bridge, broker_pair, env, free_port

from ppro_bridge import auth, transport, wsclient


class RealBrokerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = Bridge()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def test_ping_and_a_read_go_through(self):
        reply = transport.call_sync("ping", {}, url=self.bridge.url)
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["data"]["pong"])
        self.assertTrue(reply["broker"])
        self.assertEqual(transport.make_rpc(url=self.bridge.url)("get_project")["name"], "Smoke Test.prproj")

    def test_a_reply_of_many_kilobytes(self):
        data = transport.make_rpc(url=self.bridge.url)("list_items", {"kinds": ["video", "audio"]})
        self.assertIn("video", data)
        big = transport.call_sync("ping", {"probe": True}, url=self.bridge.url)  # a few KB of capabilities
        self.assertTrue(big["ok"], big)

    def test_a_wrong_token_is_auth_failed(self):
        with mock.patch.dict(os.environ, {"PPRO_BRIDGE_TOKEN": "x" * 40}):
            reply = transport.call_sync("get_project", {}, url=self.bridge.url)
        self.assertEqual((reply["code"], reply["outcome"], reply["sent"]), ("AUTH_FAILED", "not_committed", False))
        self.assertEqual(reply, self.async_reply("get_project", {"PPRO_BRIDGE_TOKEN": "x" * 40}))

    def test_the_same_replies_as_websockets(self):
        for action in ("get_project", "get_sequence"):
            with self.subTest(action=action):
                got = transport.call_sync(action, {}, url=self.bridge.url)
                want = self.async_reply(action)
                self.assertEqual((got["ok"], got["data"]), (want["ok"], want["data"]))

    def async_reply(self, action, environ=None):
        with mock.patch.dict(os.environ, environ or {}):
            return asyncio.run(transport.call(action, {}, url=self.bridge.url))


class NoPanelTest(unittest.TestCase):
    def test_the_brokers_own_failure_comes_through(self):
        bridge = Bridge(panel=False)
        try:
            reply = transport.call_sync("add_marker", {"atSeconds": 1.0, "name": "x"}, url=bridge.url)
        finally:
            bridge.stop()
        self.assertEqual((reply["ok"], reply["code"], reply["outcome"]), (False, "NO_PANEL", "not_committed"))


class Server:
    """A websockets server on a free port running `handler(ws)` after the pairing (a 0.7 broker's side, helpers.
    broker_pair) and the request; `seen` collects what the handler reports. pair=False: `handler` gets the
    connection right after the handshake (a program that is not a 0.7 broker)."""

    def __init__(self, handler, pair=True):
        self.handler, self.seen, self.pair = handler, {}, pair
        self.port = free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self.ready, self.done = threading.Event(), threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _run(self):
        async def serve(ws):
            try:
                if not self.pair:
                    await self.handler(ws, self.seen)
                    return
                self.seen["hello"] = await broker_pair(ws)
                self.seen["request"] = json.loads(await ws.recv())
                await self.handler(ws, self.seen)
            finally:
                self.done.set()

        async def main():
            async with websockets.serve(serve, "127.0.0.1", self.port):
                self.ready.set()
                await asyncio.Future()

        asyncio.new_event_loop().run_until_complete(main())


class FramesTest(unittest.TestCase):
    def test_a_fragmented_reply_after_a_ping_and_a_clean_close(self):
        async def handler(ws, seen):
            pong = await ws.ping(b"are you there")
            await ws.send(['{"ok": true, ', '"data": {"pong": true, ', '"x": "中"}}'])  # three frames
            await asyncio.wait_for(pong, 5)
            seen["pong"] = True
            await ws.wait_closed()
            seen["close"] = (ws.close_code, ws.close_reason)

        server = Server(handler)
        reply = transport.call_sync("ping", {}, url=server.url)
        self.assertEqual(reply["data"], {"pong": True, "x": "中"})
        self.assertTrue(server.done.wait(5))
        self.assertEqual((server.seen["pong"], server.seen["close"]), (True, (1000, "")))
        self.assertEqual(server.seen["hello"]["role"], "controller")
        self.assertNotIn("token", server.seen["hello"])  # DOC-8: proved, never sent
        self.assertEqual(server.seen["request"]["action"], "ping")

    def test_a_binary_reply(self):
        async def handler(ws, seen):
            await ws.send(json.dumps({"ok": True, "data": {"n": 1}}).encode("utf-8"))

        self.assertEqual(transport.call_sync("ping", {}, url=Server(handler).url)["data"], {"n": 1})

    def test_a_reply_over_the_size_cap_closes_with_1009(self):
        async def handler(ws, seen):
            await ws.recv()
            await ws.recv()
            await ws.send("x" * 100)
            await ws.wait_closed()
            seen["close"] = ws.close_code

        server = Server(handler, pair=False)  # the raw client below does not pair: frames only
        with wsclient.connect(server.url, max_size=50) as ws:
            ws.send_texts("{}", '{"id": "1", "action": "ping"}')
            with self.assertRaises(wsclient.Closed) as cm:
                ws.recv(timeout=5)
        self.assertEqual((cm.exception.rcvd, cm.exception.sent.code), (None, 1009))
        self.assertTrue(server.done.wait(5))
        self.assertEqual(server.seen["close"], 1009)
        mapped = transport.failure_reply(cm.exception, server.url, True, None)
        self.assertEqual((mapped["code"], mapped["outcome"]), ("CONNECTION_CLOSED", "unknown"))

    def test_no_reply_in_time_is_a_timeout(self):
        async def handler(ws, seen):
            await asyncio.sleep(30)

        server = Server(handler)
        with mock.patch.object(transport, "reply_wait", lambda msg: 0.3):
            write = transport.call_sync("add_marker", {"atSeconds": 1.0, "name": "x"}, url=server.url)
            read = transport.call_sync("get_project", {}, url=server.url)
        self.assertEqual((write["code"], write["sent"], write["outcome"]), ("TIMEOUT", True, "unknown"))
        self.assertEqual((read["code"], read["outcome"]), ("TIMEOUT", "not_committed"))

    def test_a_1008_close_after_the_proof_is_auth_failed(self):
        async def handler(ws, seen):
            hello = json.loads(await ws.recv())
            await ws.send(json.dumps({"event": "challenge", "auth": auth.SCHEME, "nonce": auth.new_nonce()}))
            seen["proof"] = json.loads(await ws.recv())
            await ws.close(code=1008, reason="pairing token missing or wrong")
            seen["hello"] = hello

        server = Server(handler, pair=False)
        reply = transport.call_sync("get_project", {}, url=server.url)
        self.assertEqual((reply["code"], reply["outcome"], reply["sent"]), ("AUTH_FAILED", "not_committed", False))
        self.assertEqual(set(server.seen["proof"]), {"proof"})


class FakeBrokerTest(unittest.TestCase):
    """DOC-8: a program on the broker's port that cannot prove the pairing token gets neither the token nor the
    request - whether it says nothing, answers like a 0.6 broker, skips the challenge or forges the proof."""

    def call(self, handler, call=transport.call_sync):
        server = Server(handler, pair=False)
        if call is transport.call_sync:
            reply = call("add_marker", {"atSeconds": 1.0, "name": "secret client"}, url=server.url)
        else:
            reply = asyncio.run(call("add_marker", {"atSeconds": 1.0, "name": "secret client"}, url=server.url))
        self.assertTrue(server.done.wait(5))
        return reply, server.seen

    def frames(self, ws, seen):
        async def collect():
            seen.setdefault("frames", [])
            try:
                async for raw in ws:
                    seen["frames"].append(raw)
            except websockets.ConnectionClosed:
                pass
        return collect()

    def check(self, reply, seen):
        self.assertEqual((reply["code"], reply["outcome"], reply["sent"]), ("BROKER_UNVERIFIED", "not_committed", False))
        token = auth.read_token()
        everything = "".join(seen.get("frames", []))
        self.assertNotIn(token, everything)
        self.assertNotIn("secret client", everything)  # the request never went out
        self.assertNotIn(token, json.dumps(reply))

    def test_the_rogues(self):
        async def old_broker(ws, seen):  # a 0.6 broker: refuses a hello without the token
            seen["frames"] = [await ws.recv()]
            await ws.send(json.dumps({"id": None, "ok": False, "code": "AUTH_FAILED", "outcome": "not_committed",
                                      "error": "needs the token", "broker": "0.6.0"}))
            await self.frames(ws, seen)

        async def registers_at_once(ws, seen):  # no challenge: straight to "authenticated"
            seen["frames"] = [await ws.recv()]
            await ws.send(json.dumps({"event": "authenticated", "broker": "0.7.0", "proof": "0" * 64}))
            await self.frames(ws, seen)

        async def forged(ws, seen):  # a challenge, then a proof it cannot have made
            seen["frames"] = [await ws.recv()]
            await ws.send(json.dumps({"event": "challenge", "auth": auth.SCHEME, "nonce": auth.new_nonce()}))
            seen["frames"].append(await ws.recv())
            await ws.send(json.dumps({"event": "authenticated", "broker": "0.7.0", "proof": "a" * 64}))
            await self.frames(ws, seen)

        async def hangs_up(ws, seen):
            seen["frames"] = [await ws.recv()]
            await ws.close(code=1008)

        for handler in (old_broker, registers_at_once, forged, hangs_up):
            for call in (transport.call_sync, transport.call):
                with self.subTest(rogue=handler.__name__, client=call.__name__):
                    reply, seen = self.call(handler, call)
                    self.check(reply, seen)
                    hello = json.loads(seen["frames"][0])
                    self.assertEqual(set(hello), {"role", "auth", "nonce"})
        self.assertIn("Broker 0.6.0", self.call(old_broker)[0]["error"])

    def test_a_normal_close_after_the_request_is_connection_closed(self):
        async def handler(ws, seen):
            await ws.close()

        reply = transport.call_sync("add_marker", {"atSeconds": 1.0, "name": "x"}, url=Server(handler).url)
        self.assertEqual((reply["code"], reply["sent"], reply["outcome"]), ("CONNECTION_CLOSED", True, "unknown"))
        self.assertIn("received 1000 (OK)", reply["error"])

    def test_a_reply_that_is_not_json(self):
        async def handler(ws, seen):
            await ws.send("not json")

        reply = transport.call_sync("get_project", {}, url=Server(handler).url)
        self.assertEqual((reply["code"], reply["sent"]), ("BAD_REPLY", True))


class RawServer:
    """A TCP server that answers the opening handshake with `answer(request_text)` (bytes), then closes."""

    def __init__(self, answer):
        self.answer, self.request = answer, None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.url = f"ws://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        conn, _ = self.sock.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.request = data.decode("iso-8859-1")
            conn.sendall(self.answer(self.request))
        self.sock.close()


def accept_for(request: str) -> str:
    key = re.search(r"Sec-WebSocket-Key: (\S+)", request).group(1)
    return wsclient.accept_key(key)


class HandshakeTest(unittest.TestCase):
    def call(self, answer):
        server = RawServer(answer)
        return transport.call_sync("get_project", {}, url=server.url), server

    def test_a_refusal_is_refused(self):
        reply, server = self.call(lambda req: b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        self.assertEqual((reply["code"], reply["sent"], reply["outcome"]), ("REFUSED", False, "not_committed"))
        self.assertIn("HTTP 403", reply["error"])
        request = server.request
        self.assertTrue(request.startswith("GET / HTTP/1.1\r\n"))
        self.assertIn(f"Host: 127.0.0.1:{server.url.rsplit(':', 1)[1]}\r\n", request)
        self.assertIn("Sec-WebSocket-Version: 13\r\n", request)
        self.assertNotIn("origin:", request.lower())  # the broker refuses any Origin (browsers)
        self.assertNotIn("sec-websocket-extensions", request.lower())

    def test_a_wrong_accept_key_is_refused(self):
        reply, _ = self.call(lambda req: b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: AAAA\r\n\r\n")
        self.assertEqual(reply["code"], "REFUSED")
        self.assertIn("Sec-WebSocket-Accept", reply["error"])

    def test_an_extension_nobody_offered_is_refused(self):
        def answer(req):
            return (f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept_for(req)}\r\n"
                    f"Sec-WebSocket-Extensions: permessage-deflate\r\n\r\n").encode("ascii")
        reply, _ = self.call(answer)
        self.assertEqual(reply["code"], "REFUSED")

    def test_no_http_answer_is_refused(self):
        reply, _ = self.call(lambda req: b"")
        self.assertEqual(reply["code"], "REFUSED")
        self.assertIn("did not receive a valid HTTP response", reply["error"])

    def test_a_good_handshake_then_a_hang_up(self):
        def answer(req):
            return (f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept_for(req)}\r\n\r\n").encode("ascii")
        reply, _ = self.call(answer)
        # gone before any pairing challenge: whoever it was never proved the token
        self.assertEqual((reply["code"], reply["outcome"], reply["sent"]), ("BROKER_UNVERIFIED", "not_committed", False))

    def test_no_broker(self):
        url = f"ws://127.0.0.1:{free_port()}"
        reply = transport.call_sync("add_marker", {"atSeconds": 1.0, "name": "x"}, url=url)
        self.assertEqual((reply["code"], reply["sent"], reply["outcome"]), ("NO_BROKER", False, "not_committed"))
        self.assertIn(url, reply["error"])


class UrlTest(unittest.TestCase):
    def test_what_wsclient_handles(self):
        for url in ("ws://127.0.0.1:8018", "ws://localhost:1/path?q=1", "ws://[::1]:8018"):
            self.assertTrue(wsclient.handles(url), url)
        for url in ("wss://127.0.0.1:8018", "http://127.0.0.1:1", "ws://u:p@127.0.0.1:1", "ws://127.0.0.1:99999",
                    "nonsense", "", None):
            self.assertFalse(wsclient.handles(url), url)

    def test_other_urls_get_websockets_replies(self):
        for url in ("nonsense", "http://127.0.0.1:1"):
            with self.subTest(url=url):
                self.assertEqual(transport.call_sync("get_project", {}, url=url),
                                 asyncio.run(transport.call("get_project", {}, url=url)))


class PiecesTest(unittest.TestCase):
    def test_frames_are_masked(self):
        for n in (0, 1, 125, 126, 65535, 65536, 70000):
            payload = os.urandom(n)
            data = wsclient.frame(wsclient.OP_TEXT, payload)
            self.assertEqual(data[0], 0x81)
            self.assertTrue(data[1] & 0x80)  # masked
            size, at = data[1] & 0x7F, 2
            if size == 126:
                size, at = int.from_bytes(data[2:4], "big"), 4
            elif size == 127:
                size, at = int.from_bytes(data[2:10], "big"), 10
            key, body = data[at:at + 4], data[at + 4:]
            self.assertEqual(size, n)
            self.assertEqual(bytes(b ^ key[i % 4] for i, b in enumerate(body)), payload)

    def test_accept_key_of_rfc_6455(self):
        self.assertEqual(wsclient.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_request_ids_are_uuid4(self):
        ids = {transport.request_message("ping", {})["id"] for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for i in ids:
            u = uuid.UUID(i)
            self.assertEqual((str(u), u.version, u.variant), (i, 4, uuid.RFC_4122))


class StartupTest(unittest.TestCase):
    def test_a_cli_ping_loads_neither_asyncio_nor_the_editing_modules(self):
        bridge = Bridge()
        try:
            out = subprocess.run([sys.executable, "-X", "importtime", str(CLI / "ppro.py"), "ping"],
                                 env=env(PPRO_BRIDGE_URL=bridge.url), capture_output=True, text=True,
                                 encoding="utf-8", timeout=60)
        finally:
            bridge.stop()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(json.loads(out.stdout)["data"]["pong"])
        loaded = {line.split("|")[-1].strip() for line in out.stderr.splitlines() if line.startswith("import time:")}
        self.assertIn("ppro_bridge.wsclient", loaded)
        heavy = {"asyncio", "websockets", "ppro_bridge.editing", "ppro_bridge.review", "ppro_bridge.prfpset",
                 "ppro_bridge.plan", "ppro_bridge.replace", "ppro_bridge.media", "ppro_bridge.items"}
        self.assertEqual(loaded & heavy, set())


if __name__ == "__main__":
    unittest.main()
