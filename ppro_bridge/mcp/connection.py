"""One WebSocket to the broker for the whole MCP server process.

The CLI opens a connection per request (transport.call) and pays Python's start-up per command; a server that lives
for a session keeps one controller connection open and sends every request over it. The broker serves a controller
connection one request at a time, so requests here are sent one at a time too (a lock), each waiting for its reply.

Everything on the wire is transport's: the same pairing (transport.Pairing: the token from auth.read_token, read again
at each connect, is proved and never sent; no request goes out before the broker proved it back), the same request
envelope, the same reply parsing and the same failure replies (lost, auth_failed, unverified), so a tool call fails
exactly as the CLI would. Reconnecting: a connection that is not open, or that turns out closed
before the request could be sent, is replaced and the request sent on the new one; once a request was sent it is
never sent again (its outcome is then the CLI's: "unknown" for a write, "not_committed" for a read). After a timeout,
a refused token or a reply that cannot be read, the connection is dropped so no late reply can be taken for the next
request's.
"""

import asyncio
import json
import threading

from .. import auth, transport

DROP_CODES = {"AUTH_FAILED", "BAD_REPLY", "BROKER_UNVERIFIED"}


def _is_open(ws) -> bool:
    return getattr(getattr(ws, "state", None), "name", "") == "OPEN"


class Connection:
    """A callable like transport.rpc: `conn(action, params)` returns the reply's data or raises BridgeError.

    `url` pins the broker (default: PPRO_BRIDGE_URL or ws://127.0.0.1:8017, read at each request). `stats` counts
    connections opened and requests sent. close() ends the connection and its event-loop thread."""

    def __init__(self, url: str | None = None):
        self.url = url
        self.guard = None  # like make_rpc's callables: this connection sends no guard
        self.stats = {"connects": 0, "requests": 0, "reconnects": 0}
        self._lock = threading.Lock()
        self._loop = None
        self._thread = None
        self._ws = None
        self._ws_url = None
        self._token = None

    # ---------------------------------------------------------------- public

    def __call__(self, action: str, params: dict | None = None) -> dict:
        return self.rpc(action, params)

    def rpc(self, action: str, params: dict | None = None) -> dict:
        return transport._data(action, self.call(action, params))

    def call(self, action: str, params: dict | None = None, *, timeout: float | None = None,
             guard: dict | None = None) -> dict:
        """The raw reply envelope, as transport.call returns it."""
        with self._lock:
            loop = self._ensure_loop()
            return asyncio.run_coroutine_threadsafe(self._call(action, params, timeout, guard), loop).result()

    def close(self) -> None:
        with self._lock:
            loop, self._loop = self._loop, None
            if loop is None:
                return
            try:
                asyncio.run_coroutine_threadsafe(self._drop(), loop).result(10)
            except Exception:  # noqa: BLE001  (closing: nothing left to report to)
                pass
            loop.call_soon_threadsafe(loop.stop)
            self._thread.join(10)
            loop.close()
            self._thread = None

    @property
    def connected(self) -> bool:
        return _is_open(self._ws)

    # ---------------------------------------------------------------- internals

    def _ensure_loop(self):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, name="ppro-bridge-connection",
                                            daemon=True)
            self._thread.start()
        return self._loop

    async def _connect(self, url: str) -> dict | None:
        """Open and pair a connection: None once the broker proved the token (the connection is kept), else the
        failure reply (no token: AUTH_FAILED; a program that cannot prove it: BROKER_UNVERIFIED), nothing sent."""
        import websockets
        self._token = auth.read_token()
        ws = await websockets.connect(url, open_timeout=5, max_size=transport.MAX_REPLY_BYTES)
        self._ws, self._ws_url = ws, url
        self.stats["connects"] += 1
        if not self._token:
            return transport.auth_failed(None, None)
        pairing = transport.Pairing(self._token, url)
        try:
            return await pairing.run_async(ws)
        except transport.wire_errors() as e:
            return pairing.failure(e)

    async def _drop(self):
        ws, self._ws, self._ws_url = self._ws, None, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001  (already gone)
                pass

    async def _call(self, action, params, timeout, guard) -> dict:
        try:
            import websockets  # noqa: F401  (only to report a missing install the way the CLI does)
        except ImportError:
            return transport.missing_websockets()
        url = self.url or transport.broker_url()
        msg = transport.request_message(action, params, timeout=timeout, guard=guard)
        if self._ws is not None:
            await asyncio.sleep(0)  # let a close frame that already arrived be processed before reusing the socket
            if self._ws_url != url or not _is_open(self._ws):
                await self._drop()
        while True:
            reused, sent = self._ws is not None, False
            try:
                if not reused:
                    failed = await self._connect(url)
                    if failed is not None:
                        await self._drop()
                        return transport._settle_read(action, failed)
                await self._ws.send(json.dumps(msg))
                sent = True
                self.stats["requests"] += 1
                raw = await asyncio.wait_for(self._ws.recv(), timeout=transport.reply_wait(msg))
                reply = transport.parse_reply(raw, sent, self._token)
                if reply.get("id") not in (None, msg["id"]):
                    reply = transport.lost(f"The broker answered another request ({reply.get('id')}).", sent,
                                           "BAD_REPLY")
                if not reply.get("ok") and reply.get("code") in DROP_CODES:
                    await self._drop()
            except transport.wire_errors() as e:
                await self._drop()
                if reused and not sent:  # the idle connection was gone: open a new one, nothing was sent
                    self.stats["reconnects"] += 1
                    continue
                reply = transport.failure_reply(e, url, sent, self._token)
            except BaseException:
                await self._drop()
                raise
            return transport._settle_read(action, reply)
