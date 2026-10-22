"""Whatever goes wrong on the wire, the CLI prints exactly one JSON object and says whether
the request had been sent (if it had, Premiere may have acted on it)."""

import asyncio
import json
import subprocess
import sys
import threading
import unittest

import websockets

from helpers import CLI, env, free_port, run_cli


class HangUpBroker:
    """A fake broker that accepts the request and then closes the connection."""

    def __init__(self):
        self.port = free_port()
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _run(self):
        async def handler(ws):
            await ws.recv()   # role
            await ws.recv()   # the request
            await ws.close()

        async def main():
            async with websockets.serve(handler, "127.0.0.1", self.port):
                self.ready.set()
                await asyncio.Future()

        self.loop.run_until_complete(main())


class TransportTest(unittest.TestCase):
    def test_connection_closed_after_send(self):
        broker = HangUpBroker()
        code, reply, raw = run_cli("add_marker", "--at", "1", "--name", "x", url=f"ws://127.0.0.1:{broker.port}")
        self.assertEqual(code, 1)
        self.assertEqual(raw.count('"ok"'), 1)
        self.assertEqual((reply["code"], reply["sent"], reply["outcome"]), ("CONNECTION_CLOSED", True, "unknown"))

    def test_a_lost_read_changed_nothing(self):
        broker = HangUpBroker()
        code, reply, _ = run_cli("get_project", url=f"ws://127.0.0.1:{broker.port}")
        self.assertEqual(code, 1)
        self.assertEqual((reply["code"], reply["sent"], reply["outcome"]), ("CONNECTION_CLOSED", True, "not_committed"))

    def test_no_broker(self):
        code, reply, _ = run_cli("get_project", url=f"ws://127.0.0.1:{free_port()}")
        self.assertEqual(code, 1)
        self.assertEqual((reply["code"], reply["sent"], reply["outcome"]), ("NO_BROKER", False, "not_committed"))

    def test_composite_command_reports_code(self):
        code, reply, _ = run_cli("ping", url=f"ws://127.0.0.1:{free_port()}")
        self.assertEqual(code, 1)
        self.assertEqual(reply["code"], "NO_BROKER")

    def test_ripple_needs_confirmation_before_anything_is_sent(self):
        out = subprocess.run([sys.executable, str(CLI / "ppro.py"), "remove_items", "--track", "V3",
                              "--from", "0", "--to", "5", "--ripple"],
                             env=env(PPRO_BRIDGE_URL=f"ws://127.0.0.1:{free_port()}"),
                             capture_output=True, text=True, encoding="utf-8")
        reply = json.loads(out.stdout)
        self.assertEqual((out.returncode, reply["code"]), (1, "CONFIRM_REQUIRED"))


if __name__ == "__main__":
    unittest.main()
