"""The pairing token (ppro_bridge/auth.py) and the broker's audit log (ppro_bridge/audit.py): unit rules, then a
real broker with the fake panel — controllers need the token, the panel may still come without one, and every
request, refusal and panel connection leaves one JSON line."""

import asyncio
import datetime as dt
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import websockets

from helpers import STATE, Bridge, controller_hello

from ppro_bridge import audit, auth, transport

GOOD = "t" * 40


def tmp() -> Path:
    d = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(d.cleanup)
    return Path(d.name)


class TokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tmp()
        patch = mock.patch.dict(os.environ, {auth.TOKEN_FILE_ENV: str(self.dir / "sub" / "token")})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop(auth.TOKEN_ENV, None)

    def test_created_once_then_reused(self):
        token, source = auth.ensure_token()
        self.assertEqual(source, "created")
        self.assertGreaterEqual(len(token), auth.MIN_LENGTH)
        self.assertEqual(auth.ensure_token(), (token, "file"))
        self.assertEqual(auth.read_token(), token)
        self.assertEqual((self.dir / "sub" / "token").read_bytes(), (token + "\n").encode())

    def test_env_token_wins_and_must_be_long(self):
        auth.ensure_token()
        with mock.patch.dict(os.environ, {auth.TOKEN_ENV: GOOD}):
            self.assertEqual(auth.ensure_token(), (GOOD, "env"))
            self.assertEqual(auth.read_token(), GOOD)
        with mock.patch.dict(os.environ, {auth.TOKEN_ENV: "short"}):
            with self.assertRaises(auth.TokenError):
                auth.ensure_token()
            self.assertIsNone(auth.read_token())

    def test_a_bad_file_is_refused_not_replaced(self):
        path = self.dir / "sub" / "token"
        path.parent.mkdir(parents=True)
        path.write_text("too short\n", encoding="utf-8")
        with self.assertRaises(auth.TokenError) as cm:
            auth.ensure_token()
        self.assertIn("delete it", str(cm.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "too short\n")
        self.assertIsNone(auth.read_token())

    def test_missing_file_reads_as_none(self):
        self.assertIsNone(auth.read_token())

    def test_matches(self):
        self.assertTrue(auth.matches(GOOD, GOOD))
        for given in (None, "", GOOD[:-1], 123, ["t" * 40]):
            self.assertFalse(auth.matches(given, GOOD), given)

    def test_default_location(self):
        os.environ.pop(auth.TOKEN_FILE_ENV, None)
        # never %LOCALAPPDATA%: packaged (MSIX) apps get a private copy of it, and a CLI run from one would
        # read another token than a broker started by double-click
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": os.path.join("data", "local")}):
            self.assertEqual(auth.token_path(), Path.home() / ".ppro-agent-bridge" / "token")
            self.assertEqual(auth.data_dir(), Path.home() / ".ppro-agent-bridge")


class AuditUnitTest(unittest.TestCase):
    def test_shorten(self):
        long = "x" * 450
        out = audit.shorten({"s": long, "l": list(range(25)), "deep": [[[[[[[1]]]]]]], "n": 1.5})
        self.assertEqual(out["s"], "x" * 200 + "…(+250 chars)")
        self.assertEqual(out["l"], list(range(20)) + ["…(+5 items)"])
        self.assertIn("…", json.dumps(out["deep"], ensure_ascii=False))
        self.assertEqual(out["n"], 1.5)

    def test_params_are_capped_near_2_kb(self):
        params = {"itemName": "a.mp4", "atSeconds": 4.0, "components": [{"name": "y" * 150} for _ in range(20)]}
        out = audit.shorten_params(params)
        self.assertEqual(out, {"itemName": "a.mp4", "atSeconds": 4.0, "omitted": ["components"]})
        self.assertEqual(audit.shorten_params("nope"), {"invalid": "'nope'"})

    def test_summary(self):
        seq = {"name": "SLO9 ENG", "guid": "g1", "durationSeconds": 36.6, "videoTracks": [{}, {}],
               "audioTracks": [{}, {}, {}], "width": 1080, "height": 1920, "fps": 59.94}
        self.assertEqual(audit.summary("get_sequence", seq),
                         {"name": "SLO9 ENG", "guid": "g1", "durationSeconds": 36.6, "videoTracks": 2,
                          "audioTracks": 3, "width": 1080, "height": 1920})
        self.assertEqual(audit.summary("list_items", {"sequence": "S", "video": [{}] * 4, "audio": [{}]}),
                         {"sequence": "S", "videoTracks": 4, "audioTracks": 1})
        self.assertEqual(audit.summary("trim_clip", {"undoLabel": "Agent: trim"}), {"undoLabel": "Agent: trim"})
        self.assertEqual(audit.summary("x", None), {})

    def test_request_record(self):
        msg = {"id": "r1", "action": "remove_items", "params": {"videoTracks": [2], "dryRun": True},
               "guard": {"sequence": "Main"}, "timeoutSeconds": 30}
        rec = audit.request_record(msg, {"ok": False, "code": "NOT_FOUND", "outcome": "not_committed",
                                         "error": "e" * 600, "warnings": ["a", "b"]}, queue_ms=1.4, exec_ms=12.6)
        self.assertEqual({k: rec[k] for k in ("event", "id", "action", "kind", "ok", "code", "outcome", "queueMs",
                                              "execMs", "warnings", "timeoutSeconds")},
                         {"event": "request", "id": "r1", "action": "remove_items", "kind": "write", "ok": False,
                          "code": "NOT_FOUND", "outcome": "not_committed", "queueMs": 1, "execMs": 13,
                          "warnings": 2, "timeoutSeconds": 30})
        self.assertEqual(rec["guard"], {"sequence": "Main"})
        self.assertEqual(len(rec["error"]), audit.MAX_ERROR + 1)
        bad = audit.request_record({}, {"ok": False, "code": "BAD_REQUEST"})
        self.assertEqual((bad["action"], bad["code"]), (None, "BAD_REQUEST"))
        self.assertNotIn("kind", bad)

    def test_write_and_prune(self):
        folder = tmp() / "audit"
        today = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=11)))
        log = audit.AuditLog(folder, now=lambda: today)
        self.assertTrue(log.write({"event": "start"}))
        self.assertTrue(log.write({"event": "request", "action": "ping"}))
        lines = (folder / "audit-2026-10-22.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x)["event"] for x in lines], ["start", "request"])
        self.assertEqual(json.loads(lines[0])["ts"], "2026-10-22T12:00:00.000+11:00")
        for name in ("audit-2026-09-21.jsonl", "audit-2026-09-22.jsonl", "audit-2026-08-01.jsonl", "notes.txt",
                     "audit-2026-02-31.jsonl"):
            (folder / name).write_text("x", encoding="utf-8")
        self.assertEqual(sorted(log.prune()), ["audit-2026-08-01.jsonl", "audit-2026-09-21.jsonl"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["audit-2026-02-31.jsonl", "audit-2026-09-22.jsonl", "audit-2026-10-22.jsonl", "notes.txt"])

    def test_a_failed_write_never_raises(self):
        blocker = tmp() / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")
        log = audit.AuditLog(blocker)
        self.assertFalse(log.write({"event": "start"}))
        self.assertFalse(log.write({"event": "request", "action": "ping"}))
        self.assertEqual(log.failures, 2)
        self.assertTrue(log.last_error)
        self.assertEqual(log.prune(), [])

    def test_off(self):
        with mock.patch.dict(os.environ, {audit.DIR_ENV: "off"}):
            self.assertIsNone(audit.audit_dir())
        with mock.patch.dict(os.environ, {audit.DIR_ENV: str(STATE / "x")}):
            self.assertEqual(audit.audit_dir(), STATE / "x")


def audit_lines(folder: Path = STATE / "audit") -> list[dict]:
    out = []
    for p in sorted(folder.glob("audit-*.jsonl")):
        out += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return out


async def raw(url, hello, action="ping", msg_id=None):
    """One request over a raw socket: (reply or None, the broker's close code when it refused, else None)."""
    async with websockets.connect(url, open_timeout=5) as ws:
        await ws.send(hello if isinstance(hello, str) else json.dumps(hello))
        await ws.send(json.dumps({"id": msg_id or str(uuid.uuid4()), "action": action, "params": {}}))
        try:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 30))
        except websockets.ConnectionClosed:
            reply = None
        if reply is not None and reply.get("code") != "AUTH_FAILED":
            return reply, None
        try:
            await asyncio.wait_for(ws.recv(), 5)
        except websockets.ConnectionClosed:
            pass
        return reply, ws.close_code


class BrokerAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = Bridge()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def received(self) -> int:
        reply, _ = asyncio.run(raw(self.bridge.url, controller_hello()))
        return reply["data"]["received"]

    def test_controllers_need_the_token(self):
        before = self.received()
        for hello, state in (({"role": "controller"}, "missing"), ({"role": "controller", "token": GOOD}, "wrong")):
            reply, close = asyncio.run(raw(self.bridge.url, hello, "get_sequence"))
            self.assertEqual((reply["code"], reply["outcome"], close), ("AUTH_FAILED", "not_committed", 1008), state)
            self.assertIn("older than 0.5", reply["error"])  # what a 0.4 CLI prints
        self.assertEqual(self.received(), before + 1)  # only the new ping reached the panel
        refused = [r for r in audit_lines() if r.get("event") == "refused" and r.get("reason") == "token"]
        self.assertTrue({"missing", "wrong"} <= {r["token"] for r in refused}, refused)

    def test_the_cli_pairs_by_itself(self):
        code, out, _ = self.bridge.cli("get_sequence")
        self.assertEqual(code, 0, out)

    def test_transport_reports_a_refused_token(self):
        with mock.patch.dict(os.environ, {auth.TOKEN_ENV: GOOD}):
            reply = asyncio.run(transport.call("get_sequence", {}, url=self.bridge.url))
        self.assertEqual((reply["ok"], reply["code"], reply["outcome"], reply["sent"]),
                         (False, "AUTH_FAILED", "not_committed", False))
        self.assertIn("PPRO_BRIDGE_TOKEN", reply["error"])
        with mock.patch.dict(os.environ, {auth.TOKEN_FILE_ENV: str(tmp() / "none")}):
            reply = asyncio.run(transport.call("ping", {}, url=self.bridge.url))
        self.assertEqual(reply["code"], "AUTH_FAILED")
        self.assertFalse(reply["data"]["tokenFound"])
        self.assertIn("none found at", reply["error"])

    def test_a_panel_with_a_wrong_token_is_refused_and_the_real_one_stays(self):
        _, close = asyncio.run(raw(self.bridge.url, {"role": "panel", "token": GOOD}))
        self.assertEqual(close, 1008)
        reply, _ = asyncio.run(raw(self.bridge.url, controller_hello()))
        self.assertTrue(reply["ok"], reply)  # the fake panel (no token) is still registered
        panels = [r for r in audit_lines() if r.get("event") == "panel" and r.get("state") == "registered"]
        self.assertIn("missing", {r["token"] for r in panels})

    def test_every_request_leaves_one_line(self):
        rid = str(uuid.uuid4())
        reply, _ = asyncio.run(raw(self.bridge.url, controller_hello(), "get_sequence", rid))
        self.assertTrue(reply["ok"], reply)
        lines = [r for r in audit_lines() if r.get("id") == rid]
        self.assertEqual(len(lines), 1, lines)
        rec = lines[0]
        self.assertEqual((rec["event"], rec["action"], rec["kind"], rec["ok"]), ("request", "get_sequence", "read", True))
        self.assertGreaterEqual(rec["execMs"], 0)
        self.assertGreaterEqual(rec["queueMs"], 0)
        self.assertEqual(rec["summary"]["videoTracks"], len(reply["data"]["videoTracks"]))
        self.assertEqual(rec["summary"]["name"], reply["data"]["name"])
        self.assertTrue(dt.datetime.fromisoformat(rec["ts"]).tzinfo)


class AuditOffOrBrokenTest(unittest.TestCase):
    def run_one(self, audit_env):
        bridge = Bridge(broker_env={"PPRO_AUDIT_DIR": audit_env})
        try:
            rid = str(uuid.uuid4())
            reply, _ = asyncio.run(raw(bridge.url, controller_hello(), "get_project", rid))
            return reply, rid
        finally:
            bridge.stop()

    def test_off_writes_nothing(self):
        reply, rid = self.run_one("off")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([r for r in audit_lines() if r.get("id") == rid], [])

    def test_an_unwritable_log_still_forwards(self):
        blocker = tmp() / "file"
        blocker.write_text("x", encoding="utf-8")
        reply, _ = self.run_one(str(blocker))
        self.assertTrue(reply["ok"], reply)


if __name__ == "__main__":
    unittest.main()
