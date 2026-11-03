"""The panel's connection: it dials ws://localhost (UXP's manifest takes no IP and no port), so the broker listens on
127.0.0.1 and ::1; its hello names the plugin id, which the broker checks; a live panel is never replaced by a new
one, a leftover socket is; and no exception in main.js's socket handlers, timers or buttons reaches UXP.
Brokers run as subprocesses with their own audit folder; the panel side runs in the node harness."""

import asyncio
import base64
import contextlib
import errno
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import websockets

from helpers import BROKER, REPO, Bridge, controller_hello, env

MANIFEST = json.loads((REPO / "plugin" / "manifest.json").read_text(encoding="utf-8"))
PLUGIN_ID = MANIFEST["id"]
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()
HARNESS = REPO / "tests" / "panel" / "harness.js"
NODE = shutil.which("node")
LIVE = "A live Agent Bridge panel is already connected"


def tmp() -> Path:
    d = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(d.cleanup)
    return Path(d.name)


def audit_lines(folder: Path) -> list[dict]:
    out = []
    for p in sorted(folder.glob("audit-*.jsonl")):
        out += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return out


def ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


def dual_free_port() -> int:
    """A port free on both 127.0.0.1 and ::1."""
    for _ in range(50):
        with socket.socket(socket.AF_INET6) as s6:
            s6.bind(("::1", 0))
            port = s6.getsockname()[1]
            with socket.socket() as s4:
                try:
                    s4.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port
    raise RuntimeError("no port free on both stacks")


def silent_panel(port: int, hello: dict) -> socket.socket:
    """A panel that says hello and never reads its socket again, so it answers no ping: what a socket whose JS context
    is gone looks like to the broker. A raw handshake and one masked text frame (no websockets client: that one
    answers pings by itself)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError(f"handshake refused: {head!r}")
        head += chunk
    assert head.startswith(b"HTTP/1.1 101"), head
    payload, mask = json.dumps(hello).encode(), os.urandom(4)
    n = len(payload)
    length = bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big")
    s.sendall(bytes([0x81]) + length + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
    return s


async def refused(url, hello) -> tuple[dict, int, str]:
    """(the broker's notice, close code, close reason) for a panel the broker turns away."""
    async with websockets.connect(url, open_timeout=5) as ws:
        await ws.send(json.dumps(hello))
        notice = json.loads(await asyncio.wait_for(ws.recv(), 10))
        try:
            await asyncio.wait_for(ws.recv(), 5)
        except websockets.ConnectionClosed:
            pass
        return notice, ws.close_code, ws.close_reason


async def panel_session(url, hello, name, registered: asyncio.Event, stop: asyncio.Event):
    """A panel that registers, then answers every request with {"who": name} until `stop`."""
    async with websockets.connect(url, open_timeout=5) as ws:
        await ws.send(json.dumps(hello))
        first = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert first.get("event") == "registered", first
        registered.set()

        async def serve():
            async for raw in ws:
                msg = json.loads(raw)
                await ws.send(json.dumps({"id": msg["id"], "ok": True, "data": {"who": name}}))

        server = asyncio.create_task(serve())
        await stop.wait()
        server.cancel()


async def request(url, action="ping", timeout_s=10) -> dict:
    async with websockets.connect(url, open_timeout=5) as ws:
        await ws.send(controller_hello())
        msg = {"id": str(uuid.uuid4()), "action": action, "params": {}, "timeoutSeconds": timeout_s}
        await ws.send(json.dumps(msg))
        return json.loads(await asyncio.wait_for(ws.recv(), timeout_s + 10))


class ConstantsTest(unittest.TestCase):
    """One plugin id and the localhost rule everywhere (UXP: no IP, no port in network.domains)."""

    def test_the_plugin_id_is_the_manifest_id_everywhere(self):
        for rel, pattern in (("plugin/main.js", r'^const PLUGIN_ID = "([^"]+)";'),
                             ("broker/broker.py", r'^PLUGIN_ID = "([^"]+)"'),
                             ("broker/fake_panel.py", r'^PLUGIN_ID = "([^"]+)"')):
            m = re.search(pattern, (REPO / rel).read_text(encoding="utf-8"), re.M)
            self.assertIsNotNone(m, rel)
            self.assertEqual(m.group(1), PLUGIN_ID, rel)

    def test_manifest_allows_localhost_websockets_only(self):
        self.assertEqual(MANIFEST["requiredPermissions"]["network"], {"domains": ["ws://localhost/"]})
        main = (REPO / "plugin" / "main.js").read_text(encoding="utf-8")
        self.assertRegex(main, r'(?m)^const BROKER_URL = "ws://localhost:8017";')


class PanelRulesTest(unittest.TestCase):
    """A broker with the fake panel registered: other plugin ids and second live panels are refused."""

    @classmethod
    def setUpClass(cls):
        cls.audit = tmp()
        cls.bridge = Bridge(broker_env={"PPRO_AUDIT_DIR": cls.audit})

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def still_served_by_the_fake(self):
        reply = asyncio.run(request(self.bridge.url))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["data"]["plugin"], f"fake-panel {VERSION}")

    def test_the_fake_panel_registers_with_id_and_version(self):
        self.still_served_by_the_fake()
        rec = [r for r in audit_lines(self.audit) if r.get("event") == "panel" and r.get("state") == "registered"]
        self.assertEqual([(r["plugin"], r["version"]) for r in rec], [(PLUGIN_ID, VERSION)])

    def test_another_plugin_id_is_refused(self):
        notice, code, reason = asyncio.run(refused(self.bridge.url, {"role": "panel", "plugin": "com.example.other"}))
        self.assertEqual((notice["event"], notice["reason"], notice["closeCode"], code),
                         ("refused", "plugin", 4403, 4403))
        self.assertIn("Agent Bridge panel", reason)
        self.still_served_by_the_fake()
        rec = [r for r in audit_lines(self.audit) if r.get("event") == "refused" and r.get("reason") == "plugin"]
        self.assertEqual([(r["role"], r["plugin"]) for r in rec], [("panel", "com.example.other")])

    def test_a_second_live_panel_is_refused(self):
        hello = {"role": "panel", "plugin": PLUGIN_ID, "version": VERSION}
        t0 = time.monotonic()
        notice, code, reason = asyncio.run(refused(self.bridge.url, hello))
        self.assertLess(time.monotonic() - t0, 2.5)  # the fake answered the ping: no waiting for a timeout
        self.assertEqual((notice["event"], notice["reason"], notice["error"], code, reason),
                         ("refused", "live_panel", LIVE, 4409, LIVE))
        self.still_served_by_the_fake()
        rec = [r for r in audit_lines(self.audit) if r.get("event") == "refused" and r.get("reason") == "live_panel"]
        self.assertEqual([r["old"] for r in rec], ["pong"])


class TakeOverTest(unittest.TestCase):
    """A panel without a live predecessor registers; a closed socket gives way, a silent or busy one does not."""

    def setUp(self):
        self.audit = tmp()
        self.bridge = Bridge(panel=False, broker_env={"PPRO_AUDIT_DIR": self.audit, "PPRO_PANEL_PING_TIMEOUT": 0.5})
        self.addCleanup(self.bridge.stop)

    def registered(self) -> list[dict]:
        return [r for r in audit_lines(self.audit) if r.get("event") == "panel" and r.get("state") == "registered"]

    def test_a_panel_without_plugin_id_is_accepted_with_a_warning(self):
        async def scenario():
            registered, stop = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(panel_session(self.bridge.url, {"role": "panel"}, "old", registered, stop))
            await asyncio.wait_for(registered.wait(), 10)
            reply = await request(self.bridge.url)
            stop.set()
            await task
            return reply

        reply = asyncio.run(scenario())
        self.assertEqual(reply["data"], {"who": "old"})
        self.assertEqual([(r["plugin"], r["version"]) for r in self.registered()], [("missing", None)])

    def test_a_closed_panel_makes_room_without_a_refusal(self):
        hello = {"role": "panel", "plugin": PLUGIN_ID, "version": VERSION}

        async def scenario():
            for name in ("first", "second"):  # what a Reload looks like: the old socket closes, then the new hello
                registered, stop = asyncio.Event(), asyncio.Event()
                task = asyncio.create_task(panel_session(self.bridge.url, hello, name, registered, stop))
                await asyncio.wait_for(registered.wait(), 10)
                reply = await request(self.bridge.url)
                stop.set()
                await task
                await asyncio.sleep(0.2)
            return reply

        self.assertEqual(asyncio.run(scenario())["data"], {"who": "second"})
        self.assertEqual(len(self.registered()), 2)
        self.assertNotIn("replaced", self.registered()[1])
        self.assertEqual([r for r in audit_lines(self.audit) if r.get("event") == "refused"], [])

    def test_a_silent_panel_keeps_its_place_until_its_socket_closes(self):
        # silent = a Premiere whose main thread is busy (QA F-2b): taking over would send commands to another one
        hello = {"role": "panel", "plugin": PLUGIN_ID, "version": VERSION}
        old = silent_panel(self.bridge.port, hello)
        self.addCleanup(old.close)
        time.sleep(0.3)
        notice, code, _ = asyncio.run(refused(self.bridge.url, hello))
        self.assertEqual((notice["reason"], code), ("live_panel", 4409))
        old.close()  # the process ends: the OS closes its socket
        time.sleep(0.3)

        async def scenario():
            registered, stop = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(panel_session(self.bridge.url, hello, "new", registered, stop))
            await asyncio.wait_for(registered.wait(), 10)
            reply = await request(self.bridge.url)
            stop.set()
            await task
            return reply

        self.assertEqual(asyncio.run(scenario())["data"], {"who": "new"})
        rec = [r for r in audit_lines(self.audit) if r.get("event") == "refused"]
        self.assertEqual([(r["reason"], r["old"]) for r in rec], [("live_panel", "silent")])
        # the broker saw the close before the new hello (None) or while pinging (closed)
        self.assertIn([r.get("replaced") for r in self.registered()], ([None, None], [None, "closed"]))

    def test_a_silent_panel_with_a_request_in_flight_is_not_replaced(self):
        hello = {"role": "panel", "plugin": PLUGIN_ID, "version": VERSION}
        old = silent_panel(self.bridge.port, hello)
        self.addCleanup(old.close)
        time.sleep(0.3)

        async def scenario():
            stuck = asyncio.create_task(request(self.bridge.url, "get_project", timeout_s=30))
            await asyncio.sleep(0.3)  # forwarded to the silent panel, which never answers
            result = await refused(self.bridge.url, hello)
            stuck.cancel()
            return result

        notice, code, _ = asyncio.run(scenario())
        self.assertEqual((notice["reason"], code), ("live_panel", 4409))
        rec = [r for r in audit_lines(self.audit) if r.get("event") == "refused"]
        self.assertEqual([(r["reason"], r["old"]) for r in rec], [("live_panel", "silent")])
        self.assertEqual(len(self.registered()), 1)


class DualStackTest(unittest.TestCase):
    """The broker listens on 127.0.0.1 and ::1 (UXP's localhost may resolve to either) and nowhere else."""

    def start(self, port: int, audit: Path) -> tuple[subprocess.Popen, Path]:
        log = audit / "broker.log"
        out = open(log, "wb")
        self.addCleanup(out.close)
        proc = subprocess.Popen([sys.executable, str(BROKER / "broker.py")],
                                env=env(PPRO_BRIDGE_PORT=port, PPRO_AUDIT_DIR=audit), stdout=out, stderr=out)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            starts = [r for r in audit_lines(audit) if r.get("event") == "start"]
            if starts:
                return proc, log
            self.assertIsNone(proc.poll(), log.read_text(encoding="utf-8", errors="replace"))
            time.sleep(0.1)
        self.fail("broker did not start")

    @unittest.skipUnless(ipv6_loopback(), "no IPv6 loopback on this machine")
    def test_both_loopback_addresses_answer(self):
        port, audit = dual_free_port(), tmp()
        self.start(port, audit)
        start = [r for r in audit_lines(audit) if r.get("event") == "start"][0]
        self.assertEqual(start["hosts"], ["127.0.0.1", "::1"])
        for url in (f"ws://127.0.0.1:{port}", f"ws://[::1]:{port}", f"ws://localhost:{port}"):
            reply = asyncio.run(request(url))
            self.assertEqual(reply["code"], "NO_PANEL", url)  # the broker itself answered there

        async def register(host):
            async with websockets.connect(f"ws://{host}:{port}", open_timeout=5) as ws:
                await ws.send(json.dumps({"role": "panel", "plugin": PLUGIN_ID, "version": VERSION}))
                return json.loads(await asyncio.wait_for(ws.recv(), 10))["event"]

        for host in ("[::1]", "127.0.0.1"):
            self.assertEqual(asyncio.run(register(host)), "registered", host)
            time.sleep(0.2)  # the broker sees the close before the next hello
        rec = [r for r in audit_lines(audit) if r.get("event") == "panel" and r.get("state") == "registered"]
        self.assertEqual([r["address"] for r in rec], ["::1", "127.0.0.1"])

    @unittest.skipUnless(ipv6_loopback(), "no IPv6 loopback on this machine")
    def test_a_held_ipv6_port_stops_the_broker(self):
        """Another program on [::1]:port would get the panel (its ws://localhost arrives over ::1 first; measured with
        a fake panel on 3 Nov 2026), so the broker refuses to start instead of serving 127.0.0.1 alone."""
        port, audit = dual_free_port(), tmp()
        with socket.socket(socket.AF_INET6) as holder:
            holder.bind(("::1", port))
            holder.listen()
            proc = subprocess.run([sys.executable, str(BROKER / "broker.py")], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30,
                                  env=env(PPRO_BRIDGE_PORT=port, PPRO_AUDIT_DIR=audit))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(f"Cannot listen on [::1]:{port}", proc.stderr)
        self.assertIn("another program holds that port", proc.stderr)
        self.assertNotIn("Not listening on [::1]", proc.stderr)
        self.assertEqual([r for r in audit_lines(audit) if r.get("event") == "start"], [])
        if os.name == "nt":  # the holder is this test process: netstat and tasklist name it
            self.assertIn(f"It is held by PID {os.getpid()} (python", proc.stderr)

    def test_a_taken_ipv4_port_stops_the_broker(self):
        audit = tmp()
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            port = holder.getsockname()[1]
            proc = subprocess.run([sys.executable, str(BROKER / "broker.py")], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30,
                                  env=env(PPRO_BRIDGE_PORT=port, PPRO_AUDIT_DIR=audit))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(f"Cannot listen on 127.0.0.1:{port}", proc.stderr)
        self.assertIn("Is another broker running?", proc.stderr)
        self.assertEqual([r for r in audit_lines(audit) if r.get("event") == "start"], [])
        if os.name == "nt":
            self.assertIn(f"It is held by PID {os.getpid()} (python", proc.stderr)


class PortHolderTest(unittest.TestCase):
    """broker.listening_pid / port_holder: who holds the port, for the message of a broker that cannot start."""

    NETSTAT = "\n".join([
        "",
        "活动连接",  # the header is in the system's language (here Chinese), the columns are not
        "",
        "  协议  本地地址          外部地址        状态           PID",
        "  TCP    0.0.0.0:8017           0.0.0.0:0              LISTENING       900",
        "  TCP    127.0.0.1:8017         0.0.0.0:0              LISTENING       61152",
        "  TCP    127.0.0.1:56704        127.0.0.1:8017         TIME_WAIT       0",
        "  TCP    [::1]:8017             [::]:0                 侦听            4242",
        "  TCP    [::1]:8017             [::1]:58586            ESTABLISHED     4242",
        "  TCP    [::]:8018              [::]:0                 LISTENING       77",
    ])

    @classmethod
    def setUpClass(cls):
        cls.broker = Ipv6BindErrorTest.load_broker()

    def test_netstat_parsing(self):
        pid = self.broker.listening_pid
        self.assertEqual(pid(self.NETSTAT, "127.0.0.1", 8017), 61152)
        self.assertEqual(pid(self.NETSTAT, "::1", 8017), 4242)  # localized state: only the addresses are read
        self.assertEqual(pid(self.NETSTAT, "::1", 8018), 77)  # a wildcard listener holds it too
        self.assertIsNone(pid(self.NETSTAT, "127.0.0.1", 56704))  # a connection, not a listener
        self.assertIsNone(pid("", "127.0.0.1", 8017))

    def run_with(self, *outputs):
        calls = []

        def fake_run(args, **kw):
            calls.append((args, kw))
            out = outputs[len(calls) - 1]
            if isinstance(out, BaseException):
                raise out
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        with mock.patch.object(self.broker.subprocess, "run", fake_run), mock.patch.object(self.broker.os, "name", "nt"):
            return self.broker.port_holder("::1", 8017), calls

    def test_holder_names_pid_and_process(self):
        holder, calls = self.run_with(self.NETSTAT, '"python.exe","4242","Console","1","12,345 K"\n')
        self.assertEqual(holder, "PID 4242 (python.exe)")
        self.assertEqual(calls[0][0], ["netstat", "-ano", "-p", "TCPv6"])
        self.assertEqual(calls[1][0], ["tasklist", "/FI", "PID eq 4242", "/FO", "CSV", "/NH"])
        self.assertTrue(all(kw["timeout"] <= 5 for _, kw in calls))  # bounded

    def test_holder_is_best_effort(self):
        self.assertEqual(self.run_with(self.NETSTAT, subprocess.TimeoutExpired("tasklist", 4))[0], "PID 4242")
        self.assertEqual(self.run_with(self.NETSTAT, "INFO: No tasks are running which match the criteria.\n")[0],
                         "PID 4242")
        self.assertIsNone(self.run_with(subprocess.TimeoutExpired("netstat", 4))[0])
        self.assertIsNone(self.run_with(OSError(2, "no netstat"))[0])
        self.assertIsNone(self.run_with("no such listener\n")[0])
        with mock.patch.object(self.broker.os, "name", "posix"):
            self.assertIsNone(self.broker.port_holder("::1", 8017))

    def test_the_message_says_what_to_do(self):
        b = self.broker
        with mock.patch.object(b, "port_holder", return_value="PID 4242 (python.exe)"):
            text = b.bind_failure(b.PortHeld(errno.EADDRINUSE, "Cannot listen on [::1]:8017 (x): another program "
                                                               "holds that port."), 8017)
        self.assertIn("It is held by PID 4242 (python.exe).", text)
        self.assertIn("Is another broker running?", text)
        with mock.patch.object(b, "port_holder", return_value=None):
            text = b.bind_failure(OSError(errno.EADDRINUSE, "in use"), 8017)
        self.assertIn("Cannot listen on 127.0.0.1:8017", text)
        self.assertIn("netstat -ano | findstr :8017", text)


class Ipv6BindErrorTest(unittest.TestCase):
    """broker.listen in process (websockets.serve faked): which failed ::1 binds are "no IPv6 here" (a warning, serve
    127.0.0.1 alone) and which mean another program holds the port (PortHeld: the broker does not start)."""

    @staticmethod
    def load_broker():
        import logging
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        import broker  # its logging.basicConfig would make every later test's websockets server chatty
        root.handlers[:] = handlers
        root.setLevel(level)
        return broker

    @classmethod
    def setUpClass(cls):
        cls.broker = cls.load_broker()

    def listen(self, error, has_ipv6=True):
        b = self.broker

        @contextlib.asynccontextmanager
        async def fake_serve(handler, host, port, **options):
            if host == b.HOST6:
                raise error
            yield None

        async def go():
            async with contextlib.AsyncExitStack() as stack:
                return await b.listen(stack, 8999)

        with mock.patch.object(b.websockets, "serve", fake_serve), mock.patch.object(b.socket, "has_ipv6", has_ipv6):
            return asyncio.run(go())

    def test_no_ipv6_is_a_warning(self):
        for error in (OSError(errno.EADDRNOTAVAIL, "the requested address is not valid in its context"),
                      OSError(errno.EAFNOSUPPORT, "address family not supported"),
                      socket.gaierror(11001, "getaddrinfo failed")):
            with self.subTest(error=error), self.assertLogs("broker", "WARNING") as logs:
                self.assertEqual(self.listen(error), ["127.0.0.1"])
            self.assertIn("no IPv6 loopback", logs.output[0])
        with self.assertLogs("broker", "WARNING"):  # an interpreter without IPv6 support: whatever the error says
            self.assertEqual(self.listen(OSError(errno.EADDRINUSE, "in use"), has_ipv6=False), ["127.0.0.1"])

    def test_a_held_or_blocked_port_is_fatal(self):
        # EACCES: a program that bound the port exclusively; Windows reports it as WSAEACCES (10013)
        codes = [errno.EADDRINUSE, errno.EACCES] + [errno.WSAEACCES] * hasattr(errno, "WSAEACCES")
        for code in codes:
            with self.subTest(errno=code), self.assertRaises(self.broker.PortHeld) as caught:
                self.listen(OSError(code, "error while attempting to bind"))
            self.assertEqual(caught.exception.errno, code)
            self.assertIn("another program holds that port", caught.exception.strerror)
            self.assertIn("ws://localhost:8999", caught.exception.strerror)


def connection(steps, **extra) -> dict:
    p = subprocess.run([NODE, str(HARNESS), "connection"], input=json.dumps(dict(extra, steps=steps)),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"harness exited {p.returncode}: {p.stdout}\n{p.stderr}")
    out = json.loads(p.stdout)
    assert out.get("ok") is True and "violations" not in out, out
    return out


REFUSED = {"event": "refused", "reason": "live_panel", "closeCode": 4409, "error": LIVE, "broker": VERSION}
LIVE_CLOSE = {"code": 4409, "reason": LIVE}


@unittest.skipUnless(NODE, "node is not on PATH")
class PanelConnectionTest(unittest.TestCase):
    """main.js against a stub socket (tests/panel/harness.js `connection`)."""

    def test_hello_names_the_plugin_and_version(self):
        out = connection([{"open": True}])
        self.assertEqual(out["sockets"][0]["url"], "ws://localhost:8017")
        self.assertEqual(out["sockets"][0]["sent"], [{"role": "panel", "plugin": PLUGIN_ID, "version": VERSION}])
        self.assertEqual((out["status"], out["dot"], out["brand"]), ("Connected to broker", "connected", "connected"))
        self.assertEqual((out["hint"], out["guide"]), ("Ready. To start editing:", "block"))  # the first-run steps
        self.assertIn("Premiere Pro Agent Bridge installed ", out["prompt"])  # wherever it is: the helper starts anywhere
        self.assertIn("read SKILL.md", out["prompt"])

    def test_a_chinese_premiere_gets_the_panel_in_chinese(self):
        out = connection([{"open": True}], mock={"uiLocale": "zh_CN"})
        self.assertEqual((out["status"], out["hint"], out["guide"]), ("已连接", "准备好了。开始剪辑只要两步：", "block"))
        self.assertIn("Premiere Pro Agent Bridge", out["prompt"])
        self.assertIn("SKILL.md", out["prompt"])
        self.assertEqual(out["escaped"], [])

    def test_a_refusal_is_shown_and_retried_with_backoff(self):
        out = connection([{"open": True}, {"message": REFUSED}, {"close": LIVE_CLOSE},
                          {"runTimer": 6000}, {"open": True}, {"message": REFUSED}, {"close": LIVE_CLOSE},
                          {"runTimer": 12000}, {"open": True}, {"close": LIVE_CLOSE},
                          {"runTimer": 24000}, {"open": True}, {"close": LIVE_CLOSE},
                          {"runTimer": 30000}, {"open": True}, {"close": LIVE_CLOSE}])
        self.assertEqual(out["delays"], [6000, 12000, 24000, 30000, 30000])
        self.assertEqual(out["status"], f"Refused: {LIVE} — retrying in 30 s")
        self.assertEqual((out["dot"], out["brand"], out["hint"], out["guide"]), ("", "", "", "none"))  # the status says why
        self.assertEqual((out["refused"]["reason"], out["refused"]["closeCode"]), (LIVE, 4409))
        self.assertEqual(len(out["sockets"]), 5)
        self.assertTrue(all(s["sent"][0]["plugin"] == PLUGIN_ID for s in out["sockets"]))
        self.assertEqual(sum("The broker refused this panel" in line for line in out["log"]), 1)  # once, not per retry

    def test_the_close_code_alone_is_a_refusal(self):
        out = connection([{"open": True}, {"close": LIVE_CLOSE}])
        self.assertEqual((out["delays"], out["status"]), ([6000], f"Refused: {LIVE} — retrying in 6 s"))

    def test_registration_or_a_request_ends_the_refusal(self):
        for accept in ({"message": {"event": "registered", "broker": VERSION}},
                       {"message": {"id": "r1", "action": "ping", "params": {}}}):
            out = connection([{"open": True}, {"close": LIVE_CLOSE}, {"runTimer": 6000}, {"open": True},
                              {"close": LIVE_CLOSE}, {"runTimer": 12000}, {"open": True}, accept, {"wait": 50},
                              {"close": {"code": 1006}}])
            self.assertIsNone(out["refused"], accept)
            self.assertEqual(out["delays"][-1], 3000, accept)  # backoff over: the next drop retries at once
            self.assertEqual(out["status"], "Disconnected — broker not running?")
            self.assertIn("The broker accepted this panel.", "\n".join(out["log"]))
        reply = [m for m in out["sockets"][2]["sent"] if isinstance(m, dict) and m.get("id") == "r1"]
        self.assertTrue(reply and reply[0]["ok"], out["sockets"][2]["sent"])

    def test_a_dropped_connection_clears_the_refusal(self):
        out = connection([{"open": True}, {"close": LIVE_CLOSE}, {"runTimer": 6000}, {"close": {"code": 1006}}])
        self.assertIsNone(out["refused"])
        self.assertEqual((out["delays"], out["status"]), ([6000, 3000], "Disconnected — broker not running?"))

    def test_reconnect_retries_now_and_backs_off_afresh(self):
        out = connection([{"open": True}, {"close": LIVE_CLOSE}, {"runTimer": 6000}, {"open": True},
                          {"close": LIVE_CLOSE}, {"click": "reconnectBtn"}, {"open": True}, {"close": LIVE_CLOSE}])
        self.assertEqual(out["delays"], [6000, 12000, 6000])
        self.assertEqual(len(out["sockets"]), 3)
        self.assertIsNotNone(out["refused"])

    def test_nothing_escapes_to_uxp(self):
        out = connection([{"fire": "error", "message": "kaboom"}, {"fire": "unhandledrejection", "message": "nope"},
                          {"open": True}, {"throwingMessage": True}, {"close": None},
                          {"safely": "sync"}, {"safely": "async"}, {"click": "reconnectBtn"}])
        self.assertEqual(out["escaped"], [])
        self.assertEqual(out["prevented"], 2)
        log = "\n".join(out["log"])
        for text in ("Uncaught error: kaboom", "Unhandled rejection: nope", "Message failed: unreadable message",
                     "Test callback failed: sync boom", "Test callback failed: async boom"):
            self.assertIn(text, log)
        self.assertEqual(len(out["sockets"]), 2)  # the close without an event object still reconnected (click)

    def test_an_unload_the_page_survives_can_be_undone_with_reconnect(self):
        out = connection([{"open": True}, {"fire": "beforeunload"}])
        self.assertEqual((out["sockets"][0]["readyState"], out["status"]),
                         (3, "Unloaded (beforeunload) — Reconnect connects again"))
        self.assertEqual(out["delays"], [])  # no retry after an unload
        out = connection([{"open": True}, {"fire": "beforeunload"}, {"click": "reconnectBtn"}, {"open": True}])
        self.assertEqual(len(out["sockets"]), 2)
        self.assertEqual(out["sockets"][1]["sent"], [{"role": "panel", "plugin": PLUGIN_ID, "version": VERSION}])
        self.assertEqual(out["status"], "Connected to broker")


if __name__ == "__main__":
    unittest.main()
