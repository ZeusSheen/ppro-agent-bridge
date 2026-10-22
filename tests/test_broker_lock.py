"""The broker hands the panel one request at a time, refuses to queue forever, and keeps the
queue closed while a timed-out request may still be running."""

import asyncio
import json
import re
import subprocess
import sys
import time
import unittest
import uuid

import websockets

from helpers import BROKER, CLI, Bridge, controller_hello, env

BROKER_VERSION = re.search(r'^VERSION = "([^"]+)"', (BROKER / "broker.py").read_text(encoding="utf-8"), re.M).group(1)


async def request(url, action, timeout_s=None):
    async with websockets.connect(url, open_timeout=5) as ws:
        await ws.send(controller_hello())
        msg = {"id": str(uuid.uuid4()), "action": action, "params": {}}
        if timeout_s:
            msg["timeoutSeconds"] = timeout_s
        await ws.send(json.dumps(msg))
        return json.loads(await asyncio.wait_for(ws.recv(), 60))


def parallel_cli(url, n, *args):
    procs = [subprocess.Popen([sys.executable, str(CLI / "ppro.py"), *args], env=env(PPRO_BRIDGE_URL=url),
                              stdout=subprocess.PIPE, text=True, encoding="utf-8") for _ in range(n)]
    return [json.loads(p.communicate(timeout=90)[0]) for p in procs]


class SerialTest(unittest.TestCase):
    def test_overlapping_requests_never_run_together(self):
        bridge = Bridge(panel_env={"PPRO_FAKE_CONCURRENT": 1, "PPRO_FAKE_DELAY": 300})
        try:
            replies = parallel_cli(bridge.url, 6, "list_items")
            self.assertTrue(all(r["ok"] for r in replies), replies)
            code, ping, _ = bridge.cli("ping")
            self.assertEqual(ping["data"]["maxConcurrent"], 1)
            # the CLI reports the broker's version (whether it matches the CLI is the version-sync check's job)
            self.assertEqual(ping["data"]["brokerVersion"], BROKER_VERSION)
        finally:
            bridge.stop()

    def test_queue_timeout_answers_busy_without_forwarding(self):
        bridge = Bridge(broker_env={"PPRO_QUEUE_TIMEOUT": 1}, panel_env={"PPRO_FAKE_DELAY": 1500})
        try:
            replies = parallel_cli(bridge.url, 3, "get_project")
            busy = [r for r in replies if r.get("code") == "BRIDGE_BUSY"]
            done = [r for r in replies if r.get("ok")]
            self.assertTrue(busy, replies)
            self.assertTrue(all(r["outcome"] == "not_committed" and r["retryable"] for r in busy))
            _, ping, _ = bridge.cli("ping")
            # received counts this ping too; busy requests never reached the panel
            self.assertEqual(ping["data"]["received"], len(done) + 1)
        finally:
            bridge.stop()

    def test_timed_out_request_keeps_the_queue_closed(self):
        # concurrent fake: if the broker forwarded early, the two would overlap and finish sooner
        bridge = Bridge(panel_env={"PPRO_FAKE_DELAY": 6500, "PPRO_FAKE_CONCURRENT": 1})
        try:
            async def scenario():
                t0 = time.monotonic()
                first = asyncio.create_task(request(bridge.url, "get_project", timeout_s=5))
                await asyncio.sleep(0.5)
                second = asyncio.create_task(request(bridge.url, "get_sequence", timeout_s=30))
                r1 = await first
                t1 = time.monotonic() - t0
                r2 = await second
                return r1, t1, r2, time.monotonic() - t0

            r1, t1, r2, t2 = asyncio.run(scenario())
            self.assertEqual((r1["code"], r1["outcome"]), ("PANEL_TIMEOUT", "unknown"))
            self.assertLess(t1, 6.4)
            self.assertTrue(r2["ok"], r2)
            # the second request only started after the first one's late reply (~6.5 s) + its own 6.5 s
            self.assertGreater(t2, 12.5)
        finally:
            bridge.stop()


if __name__ == "__main__":
    unittest.main()
