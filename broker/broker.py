#!/usr/bin/env python3
"""
ppro-agent-bridge broker.

A tiny WebSocket hub. Exists because UXP plugins can only connect OUT as
clients — something has to sit in the middle and hold both ends:

    CLI / agent  --->  broker (this)  --->  UXP panel in Premiere

Roles are declared by the first message a client sends:
    {"role": "panel"}                      the Premiere plugin (at most one at a time)
    {"role": "controller", "token": ...}   a CLI invocation, a script, the MCP server

Controllers then send  {"id": ..., "action": ..., "params": {...}, "timeoutSeconds": n}
and receive the panel's {"id": ..., "ok": ..., "data"/"error": ...}, plus "broker": version.

One request reaches the panel at a time: Premiere crashes when API calls from
several requests interleave. A request that waits longer than QUEUE_TIMEOUT for
its turn is answered BRIDGE_BUSY without being forwarded, and one whose client
disconnected while it waited is dropped. A request that times
out while the panel works on it is answered outcome "unknown" (it may still
commit), and the queue stays closed until the panel's late reply arrives or the
panel disconnects.

Security:
  - Binds 127.0.0.1 ONLY. Do not change this to 0.0.0.0.
  - Accepts only handshakes without an Origin header (the CLI, the fake panel)
    or with "file://" (the UXP panel on Premiere 27.1). Browsers let any page
    open a WebSocket to localhost; they always send an Origin — the page's
    http(s) origin, or "null" from sandboxed frames — and are refused with 403.
  - Controllers must send the pairing token (ppro_bridge/auth.py: read from, or
    created in, %USERPROFILE%\\.ppro-agent-bridge\\token); a missing or wrong one is
    answered AUTH_FAILED and the connection is closed (1008). The panel may still
    register without one (a wrong one is refused): it cannot read the file yet.
  - The broker forwards blindly; the whitelist lives in the panel.

Audit log (ppro_bridge/audit.py): one JSON line per request, refusal and panel
(dis)connection in %USERPROFILE%\\.ppro-agent-bridge\\audit\\audit-YYYY-MM-DD.jsonl
(PPRO_AUDIT_DIR moves it, "off" turns it off); 30 days are kept.

--load-panel (or PPRO_LOAD_PANEL=1): also load the panel into Premiere whenever Premiere connects to UXP
Developer Tool, starting UXP Developer Tool minimized when it is not running (ppro_bridge/devloader.py) - no
clicking Load, and the panel is back after every Premiere restart. --plugin DIR loads another plugin folder.

Requires: Python 3.10+, `pip install "websockets>=14"`
"""

import asyncio
import http
import json
import logging
import signal
import os
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Missing dependency. Run:  pip install websockets", file=sys.stderr)
    sys.exit(1)

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(1, str(REPO))
from ppro_bridge import audit, auth, devloader  # noqa: E402  (stdlib + websockets)

VERSION = "0.5.0"
HOST = "127.0.0.1"   # localhost only — this is a security boundary
PORT = int(os.environ.get("PPRO_BRIDGE_PORT", "8017"))  # override only for headless tests
PANEL_TIMEOUT = float(os.environ.get("PPRO_PANEL_TIMEOUT", "30"))   # default per-request execution limit
QUEUE_TIMEOUT = float(os.environ.get("PPRO_QUEUE_TIMEOUT", "120"))  # longest wait for the panel to be free
ALLOWED_ORIGINS = {None, "file://"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("broker")

panel_ws = None                 # the single connected Premiere panel
pending: dict[str, tuple] = {}  # msg id -> (panel socket it was sent on, future awaiting the reply)
panel_lock = asyncio.Lock()     # held from forwarding a request until its reply (or the panel's death)
TOKEN = None                    # the pairing token (set in main)
AUDIT = None                    # the audit log, or None when it is off


def record(rec: dict):
    """One audit line; a failed write is reported once in the console and never stops the bridge."""
    if AUDIT is not None and not AUDIT.write(rec) and AUDIT.failures == 1:
        log.warning("Audit log write failed (%s); requests are still forwarded.", AUDIT.last_error)


def check_origin(connection, request):
    """Handshake hook: refuse anything that is not the CLI or the UXP panel."""
    origin = request.headers.get("Origin")
    if origin not in ALLOWED_ORIGINS:
        log.warning("Refused a connection from origin %s", origin)
        record({"event": "refused", "reason": "origin", "origin": audit.shorten(origin)})
        return connection.respond(http.HTTPStatus.FORBIDDEN, "Only the CLI and the Premiere panel may connect.\n")
    return None


def failure(msg_id, code, outcome, error, retryable=False) -> dict:
    return {"id": msg_id, "ok": False, "code": code, "outcome": outcome, "retryable": retryable, "error": error}


async def handle_panel(ws, token_state: str):
    """Hold the panel connection; route its replies to waiting controllers."""
    global panel_ws
    if panel_ws is not None:
        log.warning("Second panel tried to register; closing the old one.")
        try:
            await panel_ws.close()
        except Exception:
            pass
    panel_ws = ws
    origin = ws.request.headers.get("Origin") or "none"
    log.info("Panel registered (origin: %s, token: %s).", origin, token_state)
    if token_state == "missing":
        log.warning("The panel sent no pairing token (expected until the panel learns to send one).")
    record({"event": "panel", "state": "registered", "origin": origin, "token": token_state})
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Panel sent non-JSON: %.200s", raw)
                continue
            sent_on, fut = pending.get(msg.get("id", ""), (None, None))
            if fut is None or sent_on is not ws:
                log.warning("Panel reply with unknown id: %s", msg.get("id"))
                continue
            pending.pop(msg["id"], None)
            if not fut.done():
                fut.set_result(msg)
    finally:
        if panel_ws is ws:
            panel_ws = None
        log.info("Panel disconnected.")
        record({"event": "panel", "state": "disconnected"})
        # fail only what was sent on this socket; a newer panel's requests are untouched
        for mid, (sent_on, fut) in list(pending.items()):
            if sent_on is ws:
                pending.pop(mid, None)
                if not fut.done():
                    fut.set_result(failure(mid, "PANEL_DISCONNECTED", "unknown",
                                           "Panel disconnected mid-command; the change may or may not have been made."))


async def release_when_done(fut, msg_id):
    """Keep the queue closed until a timed-out request's late reply (or the panel's death) arrives."""
    try:
        late = await fut
        log.info("<- panel (late): id=%s ok=%s", msg_id, late.get("ok"))
    finally:
        panel_lock.release()


def gone(ws) -> bool:
    """The controller hung up (the CLI timed out or was killed) — nobody is waiting for the reply."""
    state = getattr(ws, "state", None)
    return getattr(state, "name", "") in ("CLOSING", "CLOSED")


async def forward(msg: dict, controller=None, timing: dict | None = None) -> dict:
    """Send one request to the panel and wait for its reply, one request at a time. `timing`, if given, gets
    queueMs and, once the request was sent, execMs."""
    msg_id, action = msg["id"], msg.get("action")
    queued = time.monotonic()
    timing = {} if timing is None else timing
    try:
        await asyncio.wait_for(panel_lock.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        timing["queueMs"] = (time.monotonic() - queued) * 1000
        return failure(msg_id, "BRIDGE_BUSY", "not_committed",
                       f"The panel stayed busy for {QUEUE_TIMEOUT:.0f}s; the request was not sent.", retryable=True)
    timing["queueMs"] = (time.monotonic() - queued) * 1000
    release = True
    try:
        if controller is not None and gone(controller):
            # It queued behind a slow request and its CLI has given up: don't run a write nobody will see.
            log.warning("Dropped %s (id=%s): its client disconnected while it was queued.", action, msg_id)
            return failure(msg_id, "CLIENT_GONE", "not_committed", "The client disconnected before the request was sent.")
        panel = panel_ws
        if panel is None:
            return failure(msg_id, "NO_PANEL", "not_committed",
                           "No Premiere panel connected. Is Premiere open with the Agent Bridge panel loaded?")
        try:
            timeout = min(600.0, max(5.0, float(msg.get("timeoutSeconds") or PANEL_TIMEOUT)))
        except (TypeError, ValueError):
            timeout = PANEL_TIMEOUT
        fut = asyncio.get_running_loop().create_future()
        pending[msg_id] = (panel, fut)
        started = time.monotonic()
        try:
            await panel.send(json.dumps(msg))
        except Exception as e:
            pending.pop(msg_id, None)
            return failure(msg_id, "SEND_FAILED", "not_committed", f"Send to panel failed: {e}")
        try:
            reply = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            release = False  # the panel is still working on it
            asyncio.get_running_loop().create_task(release_when_done(fut, msg_id))
            reply = failure(msg_id, "PANEL_TIMEOUT", "unknown",
                            f"Panel did not answer within {timeout:.0f}s; the change may still be made. "
                            "Check the timeline before retrying.")
        timing["execMs"] = (time.monotonic() - started) * 1000
        log.info("<- panel: %s id=%s ok=%s queue=%dms exec=%dms", action, msg_id, reply.get("ok"),
                 timing["queueMs"], timing["execMs"])
        return reply
    finally:
        if release:
            panel_lock.release()


async def handle_controller(ws):
    """Serve one CLI client: forward its commands, await panel replies."""
    async for raw in ws:
        timing = {}
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            msg = {}
            reply = failure(None, "BAD_REQUEST", "not_committed", "Invalid JSON.")
        else:
            if not isinstance(msg, dict) or not msg.get("id") or "action" not in msg:
                msg = msg if isinstance(msg, dict) else {}
                reply = failure(msg.get("id"), "BAD_REQUEST", "not_committed", "Message needs 'id' and 'action'.")
            else:
                log.info("-> panel: %s (id=%s)", msg.get("action"), msg["id"])
                reply = await forward(msg, ws, timing)
        record(audit.request_record(msg, reply, queue_ms=timing.get("queueMs"), exec_ms=timing.get("execMs")))
        reply["broker"] = VERSION
        try:
            await ws.send(json.dumps(reply))
        except websockets.ConnectionClosed:
            return


async def route(ws):
    """First message declares the role; then hand off."""
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        return
    try:
        hello = json.loads(raw)
    except json.JSONDecodeError:
        await ws.close(code=1002, reason="First message must be JSON role declaration.")
        return

    if not isinstance(hello, dict):
        hello = {}
    role, token = hello.get("role"), hello.get("token")
    if role == "panel":
        if token is not None and not auth.matches(token, TOKEN):
            await refuse(ws, role)
            return
        await handle_panel(ws, "missing" if token is None else "ok")
    elif role == "controller":
        if not auth.matches(token, TOKEN):
            await refuse(ws, role, missing=token is None)
            return
        await handle_controller(ws)
    else:
        await ws.close(code=1002, reason='First message must declare {"role": "panel"|"controller"}.')


async def refuse(ws, role: str, missing: bool = False):
    """A missing or wrong pairing token: say so in a reply (a 0.4 CLI prints it), then close with 1008."""
    why = "no pairing token" if missing else "a wrong pairing token"
    log.warning("Refused a %s with %s.", role, why)
    record({"event": "refused", "reason": "token", "role": role, "token": "missing" if missing else "wrong"})
    reply = failure(None, "AUTH_FAILED", "not_committed",
                    f"This broker ({VERSION}) needs the pairing token and got {why}. The CLI reads it from "
                    f"{auth.token_path()} (or {auth.TOKEN_ENV}); a CLI older than 0.5 sends none: run the CLI "
                    "of the same version as the broker. Nothing was sent to Premiere.")
    reply["broker"] = VERSION
    try:
        await ws.send(json.dumps(reply))
        await ws.close(code=1008, reason="pairing token missing or wrong")
    except websockets.ConnectionClosed:
        pass


def on_loader_event(event: dict):
    text = devloader.describe(event)
    if text:
        (log.warning if event.get("ok") is False or event.get("state") == "missing" else log.info)("%s", text)
    if event.get("event") in ("load", "udt") and event.get("state") != "unreachable":
        record(dict(event, event=f"devloader.{event['event']}"))


async def main(load_panel=False, plugin_dir=None):
    global TOKEN, AUDIT
    try:
        TOKEN, source = auth.ensure_token()
    except auth.TokenError as e:
        log.error("%s", e)
        sys.exit(1)
    where = auth.TOKEN_ENV if source == "env" else auth.token_path()
    log.info("Pairing token %s %s", "created in" if source == "created" else "from", where)
    folder = audit.audit_dir()
    if folder is not None:
        AUDIT = audit.AuditLog(folder)
        pruned = AUDIT.prune()
        log.info("Audit log: %s%s", folder, f" (pruned {len(pruned)} old file(s))" if pruned else "")
    else:
        log.info("Audit log: off (%s=off)", audit.DIR_ENV)
    record({"event": "start", "broker": VERSION, "port": PORT, "token": source})
    log.info("ppro-agent-bridge broker %s listening on ws://%s:%d", VERSION, HOST, PORT)
    log.info("Waiting for the Premiere panel to connect...")
    async with websockets.serve(route, HOST, PORT, max_size=16 * 1024 * 1024, process_request=check_origin):
        loader = None
        if load_panel:
            plugin = Path(plugin_dir).resolve() if plugin_dir else devloader.PLUGIN_DIR
            if devloader.developer_mode() is False:
                log.warning("UXP developer mode is off (%s): turn it on in UXP Developer Tool, or Premiere will not "
                            "load the panel.", devloader.developer_mode_file())
            log.info("Loading the panel from %s whenever Premiere connects to UXP Developer Tool.", plugin)
            loader = asyncio.get_running_loop().create_task(devloader.watch(plugin, on_event=on_loader_event))
        stop = asyncio.get_running_loop().create_future()
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, lambda: stop.set_result(None))
        except NotImplementedError:
            pass  # Windows: fall back to KeyboardInterrupt
        try:
            await stop
        finally:
            if loader is not None:
                loader.cancel()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="broker", description="ppro-agent-bridge broker (127.0.0.1 only).")
    parser.add_argument("--load-panel", action="store_true", default=os.environ.get("PPRO_LOAD_PANEL") == "1",
                        help="load the panel whenever Premiere connects to UXP Developer Tool (no clicking Load)")
    parser.add_argument("--plugin", default=None, help="plugin folder for --load-panel (default: this repo's plugin/)")
    cli = parser.parse_args()
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        asyncio.run(main(cli.load_panel, cli.plugin))
    except KeyboardInterrupt:
        log.info("Broker stopped.")
