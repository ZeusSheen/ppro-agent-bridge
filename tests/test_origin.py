"""The broker accepts the CLI (no Origin) and the UXP panel ("file://") and refuses browsers."""

import asyncio
import unittest

import websockets

from helpers import Bridge


async def connects(url, origin) -> bool:
    try:
        async with websockets.connect(url, origin=origin, open_timeout=5):
            return True
    except websockets.InvalidStatus as e:
        assert e.response.status_code == 403, e
        return False


class OriginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = Bridge(panel=False)

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def check(self, origin, allowed):
        self.assertEqual(asyncio.run(connects(self.bridge.url, origin)), allowed, origin)

    def test_cli_without_origin(self):
        self.check(None, True)

    def test_uxp_panel(self):
        self.check("file://", True)

    def test_sandboxed_frame_or_data_url(self):
        self.check("null", False)

    def test_local_web_page(self):
        self.check("http://localhost:3000", False)
        self.check("http://127.0.0.1", False)

    def test_remote_web_page(self):
        self.check("https://evil.example", False)
        self.check("http://evil.example:8017", False)


if __name__ == "__main__":
    unittest.main()
