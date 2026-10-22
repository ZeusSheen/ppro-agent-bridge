"""A request whose client hangs up while it waits for the panel is never forwarded: nobody would see its result."""

import asyncio
import json
import unittest

import websockets

from helpers import Bridge, controller_hello


async def send(url, msg_id, action, hang_up_after=None):
    async with websockets.connect(url) as ws:
        await ws.send(controller_hello())
        await ws.send(json.dumps({"id": msg_id, "action": action, "params": {}, "timeoutSeconds": 30}))
        if hang_up_after is not None:
            await asyncio.sleep(hang_up_after)
            return None
        return json.loads(await ws.recv())


class QueueDropTest(unittest.TestCase):
    def test_request_from_a_closed_client_is_dropped(self):
        bridge = Bridge(panel_env={"PPRO_FAKE_DELAY": "1500"})
        try:
            async def scenario():
                slow = asyncio.create_task(send(bridge.url, "slow", "get_project"))
                await asyncio.sleep(0.3)  # the slow request now holds the panel
                await send(bridge.url, "quitter", "get_sequence", hang_up_after=0.2)
                first = await slow
                await asyncio.sleep(0.3)
                ping = await send(bridge.url, "ping", "ping")
                return first, ping

            first, ping = asyncio.run(scenario())
            self.assertTrue(first["ok"], first)
            self.assertTrue(ping["ok"], ping)
            # the fake counts what reached it: the slow request and this ping, not the quitter's
            self.assertEqual(ping["data"]["received"], 2, ping["data"])
        finally:
            bridge.stop()


if __name__ == "__main__":
    unittest.main()
