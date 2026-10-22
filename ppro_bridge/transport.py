"""The wire: one request to the broker, one reply back.

`call` returns the raw reply envelope (transport failures become ok:false replies with `sent`); `rpc` returns the
reply's `data` and raises BridgeError otherwise. `make_rpc` binds a broker URL and a guard (project/sequence pin).
Read actions never report outcome "unknown": nothing can have changed, so it becomes "not_committed".
Every connection sends the pairing token (auth.read_token); a broker that refuses it answers AUTH_FAILED.
"""

import asyncio
import json
import os
import uuid

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
    error = (reply or {}).get("error") or "The broker refused the pairing token."
    return {"ok": False, "code": "AUTH_FAILED", "outcome": "not_committed", "sent": False,
            "error": f"{error} This client sent {found}.", "data": {"tokenFile": str(auth.token_path()),
                                                                   "tokenFound": bool(token)},
            **({"broker": reply["broker"]} if reply and reply.get("broker") else {})}


def _settle_read(action: str, reply: dict) -> dict:
    if not reply.get("ok") and reply.get("outcome") == "unknown" and spec.kind(action) == "read":
        reply["outcome"] = "not_committed"
    return reply


async def call(action: str, params: dict | None, *, url: str | None = None, timeout: float | None = None,
               guard: dict | None = None) -> dict:
    """Send one request through the broker and return the raw reply envelope."""
    try:
        import websockets
    except ImportError:
        return {"ok": False, "code": "MISSING_DEPENDENCY", "outcome": "not_committed", "sent": False,
                "error": "Missing dependency. Run: pip install websockets"}
    url = url or broker_url()
    timeout = spec.timeout(action) if timeout is None else timeout
    msg = {"id": str(uuid.uuid4()), "action": action, "params": params or {}, "timeoutSeconds": timeout}
    if guard:
        msg["guard"] = dict(guard)
    sent = False
    token = auth.read_token()
    hello = {"role": "controller"}
    if token:
        hello["token"] = token
    try:
        async with websockets.connect(url, open_timeout=5, max_size=MAX_REPLY_BYTES) as ws:
            await ws.send(json.dumps(hello))
            await ws.send(json.dumps(msg))
            sent = True
            raw = await asyncio.wait_for(ws.recv(), timeout=QUEUE_TIMEOUT + timeout + 5)
            reply = json.loads(raw)
            if not isinstance(reply, dict):
                reply = lost("The broker sent a reply that is not a JSON object.", sent, "BAD_REPLY")
            elif reply.get("code") == "AUTH_FAILED":
                reply = auth_failed(reply, token)
    except asyncio.TimeoutError:  # before OSError: TimeoutError subclasses it
        reply = lost("Timed out waiting for the broker.", sent, "TIMEOUT")
    except websockets.InvalidURI as e:
        reply = lost(f"Bad broker URL {url!r} ({e}).", False, "NO_BROKER")
    except websockets.InvalidHandshake as e:
        reply = lost(f"The broker refused the connection ({e}).", False, "REFUSED")
    except websockets.ConnectionClosed as e:
        if getattr(getattr(e, "rcvd", None), "code", None) == 1008:  # the token was refused before any reply
            reply = auth_failed(None, token)
        else:
            reply = lost(f"The broker closed the connection ({e}).", sent, "CONNECTION_CLOSED")
    except json.JSONDecodeError:
        reply = lost("The broker sent a reply that is not JSON.", sent, "BAD_REPLY")
    except OSError:
        reply = lost(f"Cannot reach broker at {url}. Is broker.py running?", sent, "NO_BROKER")
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
    return data


def _request(action: str, params: dict | None, *, url: str | None = None, guard: dict | None = None) -> dict:
    return _data(action, asyncio.run(call(action, params or {}, url=url, guard=guard)))


def rpc(action: str, params: dict | None = None) -> dict:
    """Call the panel and return `data`, raising BridgeError (code, outcome, data, sent, retryable) on failure."""
    return _request(action, params)


def make_rpc(*, url: str | None = None, guard: dict | None = None):
    """An rpc-like callable bound to a broker URL and a guard ({project?, projectPrefix?, projectGuid?, sequence?,
    sequenceGuid?}) that the panel checks on every action but ping. With a guard, the first call pings once and
    refuses a panel older than protocol 2 (it would ignore the guard) with PANEL_OUTDATED. A guard given but empty
    (no value, or "" — an empty prefix matches every project) is refused with BAD_PARAMS.
    Note: `call(..., guard=…)` sends a guard without this protocol check; use make_rpc for guarded work."""
    if guard is not None:
        set_values = {k: v for k, v in guard.items() if v is not None}
        if not set_values or any(v == "" for v in set_values.values()):
            raise BridgeError(f"Empty guard {guard!r}: give at least one non-empty project/sequence pin.",
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
            checked["done"] = True
            if action == "ping" and not params:
                return info
        return _request(action, params, url=url, guard=guard)

    bound.url, bound.guard = url, dict(guard) if guard else None
    return bound
