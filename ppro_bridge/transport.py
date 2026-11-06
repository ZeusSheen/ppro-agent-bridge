"""The wire: one request to the broker, one reply back.

`call` returns the raw reply envelope (transport failures become ok:false replies with `sent`); `rpc` returns the
reply's `data` and raises BridgeError otherwise. `make_rpc` binds a broker URL and a guard (project/sequence pin).
`rpc` and `make_rpc` go through `call_sync`: the same exchange over ppro_bridge.wsclient, a blocking client on the
standard library, so a CLI command loads neither asyncio nor websockets (about 130 ms of start-up); `call` (async,
websockets) stays for scripts and is what call_sync falls back to for a URL wsclient does not handle (wss://).
Read actions never report outcome "unknown": nothing can have changed, so it becomes "not_committed".
Every connection pairs first (auth.py "Pairing", the Pairing class here): the client proves the pairing token
(auth.read_token) without sending it, and sends its request only once the broker has proved the token back. A broker
that refuses the proof answers AUTH_FAILED; a program that cannot prove it (a broker older than 0.7, or another
program on the port) gets neither the token nor the request: BROKER_UNVERIFIED. No token found: AUTH_FAILED, nothing
sent. request_message, Pairing, parse_reply, wire_errors and failure_reply are the pieces of one exchange; the MCP
server's persistent connection (ppro_bridge/mcp/connection.py) uses the same ones.
"""

import json
import os
import re
import sys

from . import auth, spec
from .errors import BridgeError

DEFAULT_URL = "ws://127.0.0.1:8017"
QUEUE_TIMEOUT = 120  # the broker's longest wait for the panel to be free
MAX_REPLY_BYTES = 16 * 1024 * 1024


def broker_url() -> str:
    """The broker to talk to: PPRO_BRIDGE_URL (read at call time) or the local default."""
    return os.environ.get("PPRO_BRIDGE_URL") or DEFAULT_URL


BROKER_URL = broker_url()  # 0.3.3 name; the value at import time. Calls use broker_url().


def lost(error: str, sent: bool, code: str) -> dict:
    """A reply that never came. Once the request was sent, Premiere may have acted on it."""
    return {"ok": False, "code": code, "sent": sent, "outcome": "unknown" if sent else "not_committed",
            "error": error + (" The request was sent; check the timeline before retrying." if sent else "")}


def auth_failed(reply: dict | None, token: str | None) -> dict:
    """The broker refused the pairing token: nothing reached Premiere. Says where the token was looked for."""
    where = auth.TOKEN_ENV if os.environ.get(auth.TOKEN_ENV) else str(auth.token_path())
    found = f"the token from {where}" if token else f"no token (none found at {where})"
    default = ("The broker refused the pairing token." if token or reply else
               "No pairing token to pair with the broker (the broker creates the file when it starts); nothing was "
               "sent.")
    error = (reply or {}).get("error") or default
    return {"ok": False, "code": "AUTH_FAILED", "outcome": "not_committed", "sent": False,
            "error": f"{error} This client sent {found}.", "data": {"tokenFile": str(auth.token_path()),
                                                                   "tokenFound": bool(token)},
            **({"broker": reply["broker"]} if reply and reply.get("broker") else {})}


def _settle_read(action: str, reply: dict) -> dict:
    if not reply.get("ok") and reply.get("outcome") == "unknown" and spec.kind(action) == "read":
        reply["outcome"] = "not_committed"
    return reply


# The pieces of one request/reply exchange, shared by `call` (a connection per request) and the MCP server's
# persistent connection (ppro_bridge/mcp/connection.py), so both send and map exactly the same things.

def missing_websockets() -> dict:
    return {"ok": False, "code": "MISSING_DEPENDENCY", "outcome": "not_committed", "sent": False,
            "error": "Missing dependency. Run: pip install websockets"}


def _uuid4() -> str:
    """str(uuid.uuid4()) without importing uuid (a few ms of a CLI call): 16 random bytes, version 4, RFC 4122."""
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def request_message(action: str, params: dict | None, *, timeout: float | None = None,
                    guard: dict | None = None) -> dict:
    """The request envelope: a fresh id, the action, its params and its panel timeout (the spec's unless given)."""
    timeout = spec.timeout(action) if timeout is None else timeout
    msg = {"id": _uuid4(), "action": action, "params": params or {}, "timeoutSeconds": timeout}
    if guard:
        msg["guard"] = dict(guard)
    return msg


def hello_message(token: str | None) -> dict:
    """The hello of a 0.5 / 0.6 controller: the role and the token itself. This client no longer sends it (a program
    holding the port would get the token); kept for tests and scripts that talk to an older broker on purpose."""
    hello = {"role": "controller"}
    if token:
        hello["token"] = token
    return hello


HANDSHAKE_TIMEOUT = 10.0  # s for each pairing answer (the broker gives a client 10 s for the whole hello)


def unverified(url: str, detail: str, broker=None) -> dict:
    """A program that did not prove the pairing token: nothing was sent to it, not even the token."""
    who = f"broker {broker}" if isinstance(broker, str) and len(broker) <= 40 else "the program"
    return {"ok": False, "code": "BROKER_UNVERIFIED", "outcome": "not_committed", "sent": False,
            "error": f"{who[0].upper()}{who[1:]} answering on {url} did not prove that it knows the pairing token "
                     f"({detail}). A broker older than 0.7 cannot (restart start_bridge.bat or broker.py from this "
                     "bridge's folder); anything else is another program holding the port. Nothing was sent, and "
                     "the token never leaves this machine's token file."}


class Pairing:
    """The controller side of one handshake (auth.py "Pairing"): hello(), then on_challenge(first message) -> the
    proof to send, then on_welcome(second message) -> None once the broker proved the token. Each returns a failure
    reply instead when the other side does something else; failure(exception) maps what the wire raised meanwhile."""

    def __init__(self, token: str, url: str):
        self.token, self.url = token, url
        self.client_nonce = auth.new_nonce()
        self.broker_nonce = None
        self.phase = "hello"  # hello -> proof -> done

    def hello(self) -> str:
        return json.dumps({"role": "controller", "auth": auth.SCHEME, "nonce": self.client_nonce})

    @staticmethod
    def _load(raw):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return None
        return msg if isinstance(msg, dict) else None

    def on_challenge(self, raw) -> str | dict:
        """The proof message for the broker's challenge, or a failure reply (nothing more is sent)."""
        msg = self._load(raw)
        if msg is None:
            return unverified(self.url, "its first answer is not a JSON object")
        if msg.get("code") == "AUTH_FAILED":  # a 0.5 / 0.6 broker refusing a hello without the token
            return unverified(self.url, "it asked for the token itself instead of sending a pairing challenge",
                              msg.get("broker"))
        if msg.get("event") != "challenge" or msg.get("auth") != auth.SCHEME or not auth.valid_nonce(msg.get("nonce")):
            return unverified(self.url, "it sent no pairing challenge", msg.get("broker"))
        self.broker_nonce, self.phase = msg["nonce"], "proof"
        return json.dumps({"proof": auth.proof(self.token, "client", "controller", self.client_nonce,
                                               self.broker_nonce)})

    def on_welcome(self, raw) -> dict | None:
        """None once the broker's proof matches; else a failure reply (the request is not sent)."""
        msg = self._load(raw)
        if msg is not None and msg.get("code") == "AUTH_FAILED":
            return auth_failed(msg, self.token)  # the broker refused this client's proof: another token
        if msg is None or msg.get("event") != "authenticated":
            return unverified(self.url, "it did not answer the pairing proof", (msg or {}).get("broker"))
        if not auth.proof_matches(msg.get("proof"), self.token, "broker", "controller", self.client_nonce,
                                  self.broker_nonce):
            return unverified(self.url, "its proof does not match the token file", msg.get("broker"))
        self.phase = "done"
        return None

    def failure(self, e: BaseException) -> dict:
        """The reply for an exception during the handshake (nothing was sent but the hello and the proof)."""
        from . import wsclient
        if isinstance(e, TimeoutError) or _is(e, "asyncio", "TimeoutError"):
            return lost("Timed out waiting for the broker's pairing answer.", False, "TIMEOUT")
        if isinstance(e, wsclient.Closed) or _is(e, "websockets", "ConnectionClosed"):
            code = getattr(getattr(e, "rcvd", None), "code", None)
            if code == 1013:
                return dict(lost("The broker has too many connections in the hello right now (1013).", False,
                                 "BRIDGE_BUSY"), retryable=True)
            if self.phase == "proof" and code == 1008:
                return auth_failed(None, self.token)
            return unverified(self.url, "it closed the connection " + (
                "before any pairing challenge" if self.phase == "hello" else "instead of proving the token"))
        return failure_reply(e, self.url, False, self.token)

    def run_sync(self, ws) -> dict | None:
        """The handshake over a wsclient connection: None once paired, else a failure reply. Wire errors raise."""
        ws.send_texts(self.hello())
        answer = self.on_challenge(ws.recv(timeout=HANDSHAKE_TIMEOUT))
        if isinstance(answer, dict):
            return answer
        ws.send_texts(answer)
        return self.on_welcome(ws.recv(timeout=HANDSHAKE_TIMEOUT))

    async def run_async(self, ws) -> dict | None:
        """The same over a websockets connection."""
        import asyncio
        await ws.send(self.hello())
        answer = self.on_challenge(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
        if isinstance(answer, dict):
            return answer
        await ws.send(answer)
        return self.on_welcome(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))


def reply_wait(msg: dict) -> float:
    """How long to wait for the reply to `msg`: the broker's queue wait, the panel timeout and a margin."""
    return QUEUE_TIMEOUT + msg["timeoutSeconds"] + 5


def parse_reply(raw, sent: bool, token: str | None) -> dict:
    """The reply envelope from the broker's raw message (json.JSONDecodeError propagates: see wire_errors)."""
    reply = json.loads(raw)
    if not isinstance(reply, dict):
        return lost("The broker sent a reply that is not a JSON object.", sent, "BAD_REPLY")
    if reply.get("code") == "AUTH_FAILED":
        return auth_failed(reply, token)
    return reply


def wire_errors() -> tuple:
    """The exceptions a request can end with on the wire (websockets, as `call` and the MCP connection use it);
    failure_reply turns each into a reply."""
    import asyncio

    import websockets
    return (asyncio.TimeoutError, websockets.InvalidURI, websockets.InvalidHandshake, websockets.ConnectionClosed,
            json.JSONDecodeError, OSError)


def _is(e: BaseException, module: str, name: str) -> bool:
    """isinstance(e, <module>.<name>) without importing the module: one never imported raised nothing."""
    mod = sys.modules.get(module)
    return mod is not None and isinstance(e, getattr(mod, name))


def failure_reply(e: BaseException, url: str, sent: bool, token: str | None) -> dict:
    """The reply for one of wire_errors() or of wsclient's errors (the same mapping), checked in this order
    (TimeoutError subclasses OSError)."""
    from . import wsclient
    if isinstance(e, TimeoutError) or _is(e, "asyncio", "TimeoutError"):
        return lost("Timed out waiting for the broker.", sent, "TIMEOUT")
    if isinstance(e, wsclient.BadURL) or _is(e, "websockets", "InvalidURI"):
        return lost(f"Bad broker URL {url!r} ({e}).", False, "NO_BROKER")
    if isinstance(e, wsclient.HandshakeFailed) or _is(e, "websockets", "InvalidHandshake"):
        return lost(f"The broker refused the connection ({e}).", False, "REFUSED")
    if isinstance(e, wsclient.Closed) or _is(e, "websockets", "ConnectionClosed"):
        if getattr(getattr(e, "rcvd", None), "code", None) == 1008:  # the token was refused before any reply
            return auth_failed(None, token)
        return lost(f"The broker closed the connection ({e}).", sent, "CONNECTION_CLOSED")
    if isinstance(e, json.JSONDecodeError):
        return lost("The broker sent a reply that is not JSON.", sent, "BAD_REPLY")
    return lost(f"Cannot reach broker at {url}. Is broker.py running?", sent, "NO_BROKER")


async def call(action: str, params: dict | None, *, url: str | None = None, timeout: float | None = None,
               guard: dict | None = None) -> dict:
    """Send one request through the broker and return the raw reply envelope."""
    import asyncio
    try:
        import websockets
    except ImportError:
        return missing_websockets()
    url = url or broker_url()
    msg = request_message(action, params, timeout=timeout, guard=guard)
    sent = False
    token = auth.read_token()
    try:
        async with websockets.connect(url, open_timeout=5, max_size=MAX_REPLY_BYTES) as ws:
            reply = auth_failed(None, None) if not token else None  # no token: nothing to prove, nothing sent
            if token:
                pairing = Pairing(token, url)
                try:
                    reply = await pairing.run_async(ws)
                except wire_errors() as e:
                    reply = pairing.failure(e)
            if reply is None:
                await ws.send(json.dumps(msg))
                sent = True
                raw = await asyncio.wait_for(ws.recv(), timeout=reply_wait(msg))
                reply = parse_reply(raw, sent, token)
    except wire_errors() as e:
        reply = failure_reply(e, url, sent, token)
    return _settle_read(action, reply)


def call_sync(action: str, params: dict | None, *, url: str | None = None, timeout: float | None = None,
              guard: dict | None = None) -> dict:
    """`call` without asyncio or websockets: the same request, hello, token, reply and failure mapping, over
    wsclient (one blocking connection). A URL wsclient does not handle (wss://, user info, a malformed one) goes
    through `call`, so its reply is the same too."""
    from . import wsclient
    url = url or broker_url()
    if not wsclient.handles(url):
        import asyncio
        return asyncio.run(call(action, params, url=url, timeout=timeout, guard=guard))
    msg = request_message(action, params, timeout=timeout, guard=guard)
    sent = False
    token = auth.read_token()
    try:
        with wsclient.connect(url, open_timeout=5, max_size=MAX_REPLY_BYTES) as ws:
            reply = auth_failed(None, None) if not token else None  # no token: nothing to prove, nothing sent
            if token:
                pairing = Pairing(token, url)
                try:
                    reply = pairing.run_sync(ws)
                except (wsclient.WireError, OSError) as e:
                    reply = pairing.failure(e)
            if reply is None:
                ws.send_texts(json.dumps(msg))
                sent = True
                raw = ws.recv(timeout=reply_wait(msg))
                reply = parse_reply(raw, sent, token)
    except (wsclient.WireError, json.JSONDecodeError, OSError) as e:
        reply = failure_reply(e, url, sent, token)
    return _settle_read(action, reply)


def _data(action: str, reply: dict) -> dict:
    if not reply.get("ok"):
        raise BridgeError(f"{action}: {reply.get('error')}", code=reply.get("code"), outcome=reply.get("outcome"),
                          data=reply.get("data"), sent=reply.get("sent"), retryable=reply.get("retryable"))
    data = reply.get("data") or {}
    if isinstance(data, dict):
        if reply.get("warnings"):
            data.setdefault("warnings", list(reply["warnings"]))
        if action == "ping":
            data["brokerVersion"] = reply.get("broker")
            data["update"] = reply.get("update")  # the broker's update check (0.7+), or None
    return data


def _request(action: str, params: dict | None, *, url: str | None = None, guard: dict | None = None) -> dict:
    return _data(action, call_sync(action, params or {}, url=url, guard=guard))


def rpc(action: str, params: dict | None = None) -> dict:
    """Call the panel and return `data`, raising BridgeError (code, outcome, data, sent, retryable) on failure."""
    return _request(action, params)


def make_rpc(*, url: str | None = None, guard: dict | None = None):
    """An rpc-like callable bound to a broker URL and a guard ({project?, projectPrefix?, projectGuid?, sequence?,
    sequenceGuid?, fingerprint?}) that the panel checks on every action but ping and export_status (which touches no
    project or sequence). `fingerprint` (get_sequence's, see items.fingerprint) is checked by the panel on writes that
    are not dry runs: the timeline must still be the one it was read from, else PRECONDITION_FAILED and nothing
    changes. With a guard, the first call pings once and refuses a panel older than protocol 2 (it would ignore the
    guard), or one that does not advertise the "fingerprintGuard" feature when the guard has a fingerprint, with
    PANEL_OUTDATED. A guard given but empty (no value, or "" — an
    empty prefix matches every project), or a fingerprint that is not 16 hex digits, is refused with BAD_PARAMS.
    Note: `call(..., guard=…)` sends a guard without this protocol check; use make_rpc for guarded work."""
    if guard is not None:
        set_values = {k: v for k, v in guard.items() if v is not None}
        if not set_values or any(v == "" for v in set_values.values()):
            raise BridgeError(f"Empty guard {guard!r}: give at least one non-empty project/sequence pin.",
                              code="BAD_PARAMS", outcome="not_committed")
        fp = set_values.get("fingerprint")
        if fp is not None and not (isinstance(fp, str) and re.fullmatch(r"[0-9a-f]{16}", fp)):
            raise BridgeError(f"Bad guard fingerprint {fp!r}: pass get_sequence's fingerprint (16 hex digits).",
                              code="BAD_PARAMS", outcome="not_committed")
        guard = set_values
    checked = {"done": not guard}

    def bound(action: str, params: dict | None = None) -> dict:
        if not checked["done"]:
            info = _request("ping", {}, url=url)
            if (info.get("protocol") or 1) < 2:
                raise BridgeError(f"The loaded panel ({info.get('plugin')}) speaks protocol {info.get('protocol') or 1} "
                                  "and would ignore the project/sequence guard; reload the current panel in UXP "
                                  "Developer Tool.", code="PANEL_OUTDATED", outcome="not_committed",
                                  data={"panelProtocol": info.get("protocol") or 1, "needProtocol": 2})
            if "fingerprint" in guard and "fingerprintGuard" not in (info.get("features") or []):
                raise BridgeError(f"The loaded panel ({info.get('plugin')}) cannot check a timeline fingerprint guard; "
                                  "reload the current panel in UXP Developer Tool (or use items.fingerprint_guard, "
                                  "the client-side check).", code="PANEL_OUTDATED", outcome="not_committed",
                                  data={"missingFeature": "fingerprintGuard"})
            checked["done"] = True
            if action == "ping" and not params:
                return info
        return _request(action, params, url=url, guard=guard)

    bound.url, bound.guard = url, dict(guard) if guard else None
    return bound
