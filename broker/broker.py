#!/usr/bin/env python3
"""
ppro-agent-bridge broker.

A tiny WebSocket hub. Exists because UXP plugins can only connect OUT as
clients — something has to sit in the middle and hold both ends:

    CLI / agent  --->  broker (this)  --->  UXP panel in Premiere

Roles are declared by the first message a client sends:
    {"role": "panel"}        the Premiere plugin (at most one at a time)
    {"role": "controller"}   a CLI invocation

Controllers then send  {"id": ..., "action": ..., "params": {...}}
and receive the panel's {"id": ..., "ok": ..., "data"/"error": ...}.

Security:
  - Binds 127.0.0.1 ONLY. Do not change this to 0.0.0.0.
  - The broker forwards blindly; the whitelist lives in the panel.

Requires: Python 3.10+, `pip install websockets`
"""

import asyncio
import json
import logging
import signal
import sys

try:
    import websockets
except ImportError:
    print("Missing dependency. Run:  pip install websockets", file=sys.stderr)
    sys.exit(1)

HOST = "127.0.0.1"   # localhost only — this is a security boundary
PORT = 8017
PANEL_TIMEOUT = 30   # seconds a controller waits for the panel to answer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("broker")

panel_ws = None                 # the single connected Premiere panel
pending: dict[str, asyncio.Future] = {}   # msg id -> future awaiting reply


async def handle_panel(ws):
    """Hold the panel connection; route its replies to waiting controllers."""
    global panel_ws
    if panel_ws is not None:
        log.warning("Second panel tried to register; closing the old one.")
        try:
            await panel_ws.close()
        except Exception:
            pass
    panel_ws = ws
    log.info("Panel registered.")
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Panel sent non-JSON: %.200s", raw)
                continue
            fut = pending.pop(msg.get("id", ""), None)
            if fut and not fut.done():
                fut.set_result(msg)
            else:
                log.warning("Panel reply with unknown id: %s", msg.get("id"))
    finally:
        if panel_ws is ws:
            panel_ws = None
        log.info("Panel disconnected.")
        # fail everything still waiting
        for fut in pending.values():
            if not fut.done():
                fut.set_result({"ok": False, "error": "Panel disconnected mid-command."})
        pending.clear()


async def handle_controller(ws):
    """Serve one CLI client: forward its commands, await panel replies."""
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(json.dumps({"ok": False, "error": "Invalid JSON."}))
            continue

        msg_id = msg.get("id")
        if not msg_id or "action" not in msg:
            await ws.send(json.dumps({"ok": False, "error": "Message needs 'id' and 'action'."}))
            continue

        if panel_ws is None:
            await ws.send(json.dumps({
                "id": msg_id, "ok": False,
                "error": "No Premiere panel connected. Is Premiere open with the Claude Bridge panel loaded?",
            }))
            continue

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        pending[msg_id] = fut
        log.info("-> panel: %s (id=%s)", msg.get("action"), msg_id)
        try:
            await panel_ws.send(json.dumps(msg))
        except Exception as e:
            pending.pop(msg_id, None)
            await ws.send(json.dumps({"id": msg_id, "ok": False, "error": f"Send to panel failed: {e}"}))
            continue

        try:
            reply = await asyncio.wait_for(fut, timeout=PANEL_TIMEOUT)
        except asyncio.TimeoutError:
            pending.pop(msg_id, None)
            reply = {"id": msg_id, "ok": False, "error": f"Panel did not answer within {PANEL_TIMEOUT}s."}

        log.info("<- panel: id=%s ok=%s", msg_id, reply.get("ok"))
        await ws.send(json.dumps(reply))


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

    role = hello.get("role")
    if role == "panel":
        await handle_panel(ws)
    elif role == "controller":
        await handle_controller(ws)
    else:
        await ws.close(code=1002, reason='First message must declare {"role": "panel"|"controller"}.')


async def main():
    log.info("ppro-agent-bridge broker listening on ws://%s:%d", HOST, PORT)
    log.info("Waiting for the Premiere panel to connect...")
    async with websockets.serve(route, HOST, PORT, max_size=16 * 1024 * 1024):
        stop = asyncio.get_running_loop().create_future()
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, lambda: stop.set_result(None))
        except NotImplementedError:
            pass  # Windows: fall back to KeyboardInterrupt
        await stop


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Broker stopped.")
