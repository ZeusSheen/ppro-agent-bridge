#!/usr/bin/env python3
"""
ppro-agent-bridge broker.

A tiny WebSocket hub. Exists because UXP plugins can only connect OUT as
clients — something has to sit in the middle and hold both ends:

    CLI / agent  --->  broker (this)  --->  UXP panel in Premiere

Roles are declared by the first message a client sends, and both prove the pairing token without sending it
(ppro_bridge/auth.py, "Pairing"):
    {"role": "panel", "plugin": id, "version": v, "auth": "hmac-sha256", "nonce": n}   the Premiere plugin (one)
    {"role": "controller", "auth": "hmac-sha256", "nonce": n}                        a CLI call, a script, the MCP server
The broker answers {"event": "challenge", "nonce": m}, the client {"proof": ...}; once that proof matches, the panel
gets {"event": "registered", ..., "proof": ...} and a controller {"event": "authenticated", "proof": ...}: the
broker's own proof, which the client checks before it sends a request (controller) or runs one (panel). A wrong or
missing client proof is refused (panel: close 4401; controller: AUTH_FAILED, close 1008) and nothing else is said.
Older clients: a controller hello {"role": "controller", "token": ...} (CLIs 0.5 / 0.6) is still accepted when the
token matches (constant time; the broker's side is no weaker, the old client risks its own token). A panel without
"auth" (0.6 and older) is refused (close 4401) unless PANEL_AUTH is "legacy" (PPRO_PANEL_AUTH=legacy, or settings.json
{"panelAuth": "legacy"}): then it is registered with a warning, as before 0.7, which leaves open issue 2 open.

Controllers then send  {"id": ..., "action": ..., "params": {...}, "timeoutSeconds": n}
and receive the panel's {"id": ..., "ok": ..., "data"/"error": ...}, plus "broker": version.

One request reaches the panel at a time: Premiere crashes when API calls from
several requests interleave. A request that waits longer than QUEUE_TIMEOUT for
its turn is answered BRIDGE_BUSY without being forwarded, and one whose client
disconnected while it waited is dropped. A request that times
out while the panel works on it is answered outcome "unknown" (it may still
commit), and the queue stays closed until the panel's late reply arrives or the
panel disconnects.

Write guard (a runaway script, not normal editing): writes (actions.json kind "write", dry runs not counted)
beyond WRITE_LIMIT in any WRITE_WINDOW seconds are answered WRITE_LIMIT without being forwarded; from two thirds
of the limit replies carry a warning. Real jobs peaked at 81 writes in 10 s and 118 in a minute (audit logs,
26 Oct - 2 Nov 2026), so the default 300 per 60 s never touches them. WRITE_GAP_MS (default 0 = off) spaces writes
out; 27.1 took 20 ms gaps on those jobs without trouble. PPRO_WRITE_LIMIT=0 turns the limit off.

Panels: a panel hello naming another plugin id than PLUGIN_ID is refused (close 4403); one without an id (a 0.5
panel) is registered with a warning. A new panel takes the place of the registered one only when that one's socket
is closed (a Reload, a crash: the OS closes a dead process's sockets): the broker pings the registered panel (a
WebSocket ping, PANEL_PING_TIMEOUT) and refuses the newcomer (close 4409) when it answers or stays silent. Silent is
not dead on localhost: UXP answers pings even while the panel's JS is blocked (a 4 s busy loop still ponged in 15 ms,
27.1, 2 Nov 2026), so a silent socket is a Premiere that hangs, and a takeover would send the next commands to another
Premiere. The keepalive (20 s ping, 20 s timeout) closes a socket that is really gone; the newcomer's next retry then
gets in. A refused panel gets {"event": "refused", ...} before the close, an accepted one {"event": "registered"}. A
UDT Reload closes the old socket before the new panel says hello (measured twice: disconnected, then registered
0.3-0.45 s later), so it never meets the rule.

Security:
  - Binds 127.0.0.1 and ::1 ONLY (UXP's ws://localhost may resolve to either). ::1 is skipped with a warning only
    when the machine has no IPv6 loopback. When another program holds 127.0.0.1:port or [::1]:port the broker does
    not start (exit 1), and says which program (PID and name, from netstat and tasklist): a panel dialing
    ws://localhost reaches ::1 first and would talk to that program instead. Do not change this to 0.0.0.0 or ::.
  - Accepts only handshakes without an Origin header (the CLI, the fake panel)
    or with "file://" (the UXP panel on Premiere 27.1). Browsers let any page
    open a WebSocket to localhost; they always send an Origin — the page's
    http(s) origin, or "null" from sandboxed frames — and are refused with 403.
  - Controllers and the panel must prove the pairing token (ppro_bridge/auth.py: read from, or created in,
    %USERPROFILE%\\.ppro-agent-bridge\\token), and the broker proves it back. A connection that has not proved it
    within HELLO_TIMEOUT (hello, then the proof) is closed; at most MAX_UNPAIRED connections may be in the hello at
    once (more are closed at once: a flood of idle sockets cannot exhaust the broker).
  - The broker forwards blindly; the whitelist lives in the panel.

Audit log (ppro_bridge/audit.py): one JSON line per request, refusal and panel
(dis)connection in %USERPROFILE%\\.ppro-agent-bridge\\audit\\audit-YYYY-MM-DD.jsonl
(PPRO_AUDIT_DIR moves it, "off" turns it off); 30 days are kept.

Update check (ppro_bridge/update.py; SECURITY_REVIEW.md "Update check"): the broker's only outbound request. A
background thread asks GitHub's releases/latest API for this repository at most once in 24 h (HTTPS, User-Agent
ppro-agent-bridge/<version>, no cookies, no identifier) and keeps the version, date and release URL in update.json.
Failures are quiet: an audit record {"event": "update_check", "outcome": ...}, nothing in the console. ping replies
and the panel's "registered" notice carry "update" ({latest, url, published, newer, skipped, checkedAt} or null), and
the registered panel gets {"event": "update", ...} when a check finds something. The panel's one message of its own,
{"event": "skip_version", "version": "x.y.z"}, is taken only from the registered panel and only for the release the
broker announced; it lands in settings.json ("skipVersion"). PPRO_UPDATE_CHECK=0 or settings.json
{"updateCheck": false} turns the check off.

--load-panel (or PPRO_LOAD_PANEL=1): also load the panel into every Premiere that starts (ppro_bridge/devloader.py) -
no clicking Load, and the panel is back after every Premiere restart. UXP Developer Tool runs only while it loads
(RA-5: its service takes any WebSocket and loads any folder): for a Premiere process the bridge has not handled it
starts UDT minimized, loads, waits until the panel answers and closes UDT again; a UDT the user opened is used and
left open. --plugin DIR loads another plugin folder. A load
replaces a loaded panel, so none is loaded over a connected panel that is busy, already runs that folder's code or
is an installed package (panel_state: one ping per panel connection) - e.g. the panel whose Start bridge button
started this broker.

Requires: Python 3.10+, `pip install "websockets>=14"`
"""

import os
import sys

# Run as a script, Python puts this file's folder first on sys.path: a socket.py or json.py planted there would shadow
# the standard library before any code of ours runs (RA-3). Nothing is imported from this folder, so it is dropped
# (os and sys are loaded before any script runs; -P / -I leave the folder off already).
if __name__ == "__main__" and not getattr(sys.flags, "safe_path", False) and sys.path and \
        os.path.normcase(os.path.abspath(sys.path[0] or os.curdir)) == \
        os.path.normcase(os.path.dirname(os.path.abspath(__file__))):
    del sys.path[0]

import asyncio  # noqa: E402
import collections  # noqa: E402
import contextlib
import csv
import errno
import http
import io
import json
import logging
import signal
import os
import socket
import subprocess
import sys
import threading
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
from ppro_bridge import audit, auth, devloader, spec  # noqa: E402  (stdlib + websockets)
from ppro_bridge import update as updates  # noqa: E402  (stdlib)

VERSION = "0.7.0"
HOST = "127.0.0.1"   # localhost only — this is a security boundary
HOST6 = "::1"        # the same on IPv6: UXP's ws://localhost may try ::1 first
PLUGIN_ID = "org.pproagentbridge.panel"  # plugin/manifest.json "id"; the panel's hello names it
PORT = int(os.environ.get("PPRO_BRIDGE_PORT", "8017"))  # override only for headless tests
PANEL_TIMEOUT = float(os.environ.get("PPRO_PANEL_TIMEOUT", "30"))   # default per-request execution limit
QUEUE_TIMEOUT = float(os.environ.get("PPRO_QUEUE_TIMEOUT", "120"))  # longest wait for the panel to be free
WRITE_LIMIT = int(os.environ.get("PPRO_WRITE_LIMIT", "300"))        # writes per WRITE_WINDOW; 0 = no limit
WRITE_WINDOW = float(os.environ.get("PPRO_WRITE_WINDOW", "60"))     # seconds
WRITE_GAP = float(os.environ.get("PPRO_WRITE_GAP_MS", "0")) / 1000  # least time between two writes; 0 = off
PANEL_PING_TIMEOUT = float(os.environ.get("PPRO_PANEL_PING_TIMEOUT", "2"))  # wait for the registered one's pong
HELLO_TIMEOUT = float(os.environ.get("PPRO_HELLO_TIMEOUT", "10"))  # s from the connection to a proved hello
MAX_UNPAIRED = int(os.environ.get("PPRO_MAX_UNPAIRED", "64"))      # connections in the hello at once; more: 1013
ALLOWED_ORIGINS = {None, "file://"}
PANEL_AUTH_ENV = "PPRO_PANEL_AUTH"
PANEL_AUTH = "required"  # or "legacy": a panel that cannot prove the token is registered anyway (set in main)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("broker")

panel_ws = None                 # the single connected Premiere panel
pending: dict[str, tuple] = {}  # msg id -> (panel socket it was sent on, future awaiting the reply)
panel_lock = asyncio.Lock()     # held from forwarding a request until its reply (or the panel's death)
register_lock = asyncio.Lock()  # one panel registration at a time (the live-panel check pings the old one)
TOKEN = None                    # the pairing token (set in main)
AUDIT = None                    # the audit log, or None when it is off
UPDATES = None                  # the update check (updates.Checker, set in main)
recent_writes = collections.deque()  # monotonic times of the writes forwarded within the last WRITE_WINDOW
last_write_done = None               # monotonic time the last write's reply arrived


def is_write(msg: dict) -> bool:
    """A request that may change the project: an actions.json write that is not a dry run."""
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    return spec.kind(msg.get("action")) == "write" and not params.get("dryRun")


def write_guard(msg_id) -> tuple[dict | None, str | None]:
    """(a WRITE_LIMIT refusal, or None; a warning, or None) for a write about to be forwarded. Call with the panel
    lock held; counts the write when it goes ahead."""
    now = time.monotonic()
    while recent_writes and now - recent_writes[0] > WRITE_WINDOW:
        recent_writes.popleft()
    if WRITE_LIMIT and len(recent_writes) >= WRITE_LIMIT:
        wait = WRITE_WINDOW - (now - recent_writes[0])
        return failure(msg_id, "WRITE_LIMIT", "not_committed",
                       f"{len(recent_writes)} writes in the last {WRITE_WINDOW:.0f}s reached the broker's limit "
                       f"({WRITE_LIMIT}; a guard against a runaway script). The request was not sent. Retry in "
                       f"{wait:.0f}s, or raise PPRO_WRITE_LIMIT when restarting the broker.", retryable=True), None
    recent_writes.append(now)
    if WRITE_LIMIT and len(recent_writes) >= WRITE_LIMIT * 2 // 3:
        return None, (f"{len(recent_writes)} writes in the last {WRITE_WINDOW:.0f}s; the broker refuses more than "
                      f"{WRITE_LIMIT} (runaway guard).")
    return None, None


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


PANEL_LIVE = "A live Agent Bridge panel is already connected"   # close reasons stay under 123 bytes
NOT_OUR_PLUGIN = "Only the Agent Bridge panel may register here"
PANEL_UNPAIRED = "This broker needs a panel that proves the pairing token (0.7 or later): reload or reinstall it"
PANEL_BAD_PROOF = "The panel's pairing proof does not match this broker's token file"
MAX_MESSAGE = 16 * 1024 * 1024   # a message from a proved client (a transcript, a long timeline)
PREAUTH_MAX = 1024 * 1024        # before the proof (a hello is ~200 bytes): 64 idle sockets cannot hold 1 GiB
unpaired = 0                     # connections still in the hello


def set_max_size(ws, size: int):
    """The largest message this connection may send from now on (websockets 15+: max_message_size; 14: max_size)."""
    protocol = getattr(ws, "protocol", None)
    for name in ("max_message_size", "max_size"):
        if hasattr(protocol, name):
            setattr(protocol, name, size)
            return


def update_info() -> dict | None:
    """What ping and the panel get as "update": the latest release, newer / skipped, or None (check off, nothing
    known yet)."""
    return UPDATES.snapshot() if UPDATES is not None else None


async def send_update(ws, error: str | None = None):
    """Tell a panel the update state ({"event": "update", ...}): after a check, or in answer to its skip_version."""
    notice = {"event": "update", "update": update_info(), "broker": VERSION}
    if error:
        notice["error"] = error
    try:
        await ws.send(json.dumps(notice))
    except Exception:  # noqa: BLE001 - a closed panel: it gets the state with its next "registered"
        pass


async def panel_event(ws, msg: dict):
    """A message of the panel's own (no id): only {"event": "skip_version", "version": "x.y.z"}, and only from the
    registered panel. Anything else is logged and dropped."""
    if msg.get("event") != "skip_version":
        log.warning("Panel sent an unknown message: %.80s", json.dumps(msg)[:80])
        return
    if panel_ws is not ws:
        log.warning("Ignored skip_version from a panel that is not the registered one.")
        return
    version = msg.get("version")
    ok, why = (False, "the update check is not running") if UPDATES is None else UPDATES.skip(version)
    rec = {"event": "skip_version", "version": audit.shorten(version if isinstance(version, str) else repr(version)),
           "outcome": "ok" if ok else "refused"}
    if not ok:
        rec["reason"] = why
    record(rec)
    if ok:
        log.info("Skip this version: %s is not announced again (settings.json skipVersion).", version)
    else:
        log.warning("Refused skip_version from the panel: %s.", why)
    await send_update(ws, None if ok else why)


def fail_pending(sock):
    """Fail what was sent on `sock` (it disconnected or was replaced); a newer panel's requests are untouched."""
    for mid, (sent_on, fut) in list(pending.items()):
        if sent_on is sock:
            pending.pop(mid, None)
            if not fut.done():
                fut.set_result(failure(mid, "PANEL_DISCONNECTED", "unknown",
                                       "Panel disconnected mid-command; the change may or may not have been made."))


async def ping_within(sock, timeout: float) -> str:
    """"pong" when `sock` answers a WebSocket ping within `timeout` seconds, "closed" when it is (or gets) closed,
    "silent" otherwise. Transport liveness only: UXP answers pings without the panel's JS."""
    async def roundtrip():
        await (await sock.ping())

    try:
        await asyncio.wait_for(roundtrip(), timeout)
        return "pong"
    except websockets.ConnectionClosed:
        return "closed"
    except Exception:  # TimeoutError, or a socket in a state that cannot ping
        return "silent"


async def close_quietly(sock, code: int, reason: str):
    try:
        await sock.close(code=code, reason=reason)
    except Exception:
        pass


async def refuse_panel(ws, reason: str, code: int, text: str, **extra):
    """Turn a panel away: an {"event": "refused"} notice (the panel shows `text`), then a close with `code`."""
    log.warning("Refused a panel (%s%s): %s", reason, "".join(f", {k}: {v}" for k, v in extra.items()), text)
    record(dict({"event": "refused", "reason": reason, "role": "panel"}, **extra))
    try:
        await ws.send(json.dumps({"event": "refused", "reason": reason, "closeCode": code, "error": text,
                                  "broker": VERSION}))
    except websockets.ConnectionClosed:
        return
    await close_quietly(ws, code, text)


async def handle_panel(ws, token_state: str, hello: dict, proof: str | None = None):
    """Register the panel unless a live one holds the place; then route its replies to waiting controllers.
    `token_state`: "proved" (it proved the pairing token; `proof` is the broker's, sent in "registered") or "legacy"
    (PANEL_AUTH legacy: a panel that cannot)."""
    global panel_ws
    async with register_lock:
        old = panel_ws
        answer = None if old is None else await ping_within(old, PANEL_PING_TIMEOUT)
        # only a closed socket gives way: silent is a busy Premiere, not a dead one (see the module docstring)
        live = answer in ("pong", "silent")
        if not live:
            panel_ws = ws
    if live:
        await refuse_panel(ws, "live_panel", 4409, PANEL_LIVE, old=answer)
        return
    if old is not None:  # its socket closed (a Reload, a crash) while still registered: the newcomer takes its place
        log.warning("The registered panel's socket is closed; the new one takes its place.")
        fail_pending(old)
        asyncio.get_running_loop().create_task(close_quietly(old, 4410, "Replaced by a newer Agent Bridge panel"))
    plugin, version = hello.get("plugin"), hello.get("version")
    origin = ws.request.headers.get("Origin") or "none"
    address = (ws.remote_address or ("?",))[0]  # ::1 or 127.0.0.1: which one the panel's localhost resolved to
    log.info("Panel registered (origin: %s, address: %s, token: %s, plugin: %s, version: %.40s).", origin, address,
             token_state, plugin or "none", version or "?")
    if token_state == "legacy":
        log.warning("The panel did not prove the pairing token (a panel older than 0.7), and %s=legacy let it in: "
                    "any local program can register as the panel this way. Reload or reinstall the panel, then "
                    "turn legacy off.", PANEL_AUTH_ENV)
    if plugin is None:
        log.warning("The panel sent no plugin id: a 0.5 panel. Reload it in UXP Developer Tool to load this "
                    "repository's panel.")
    rec = {"event": "panel", "state": "registered", "origin": origin, "address": address, "token": token_state,
           "plugin": "missing" if plugin is None else plugin, "version": audit.shorten(version)}
    if old is not None:
        rec["replaced"] = answer  # "closed"
    record(rec)
    try:
        notice = {"event": "registered", "broker": VERSION, "update": update_info()}
        if proof is not None:
            notice["proof"] = proof
        try:
            await ws.send(json.dumps(notice))
        except websockets.ConnectionClosed:
            pass  # the loop below ends at once
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Panel sent non-JSON: %.200s", raw)
                continue
            if not isinstance(msg, dict):
                log.warning("Panel sent a message that is not a JSON object: %.80s", raw)
                continue
            if "id" not in msg and "event" in msg:
                await panel_event(ws, msg)
                continue
            sent_on, fut = pending.get(msg.get("id", ""), (None, None))
            if fut is None or sent_on is not ws:
                log.warning("Panel reply with unknown id: %s", msg.get("id"))
                continue
            pending.pop(msg["id"], None)
            if not fut.done():
                fut.set_result(msg)
    finally:
        current = panel_ws is ws
        if current:
            panel_ws = None
        log.info("Panel disconnected." if current else "Replaced panel disconnected.")
        record({"event": "panel", "state": "disconnected"} if current else
               {"event": "panel", "state": "disconnected", "replaced": True})
        fail_pending(ws)


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
    global last_write_done
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
        write, warning = is_write(msg), None
        if write:
            refused, warning = write_guard(msg_id)
            if refused:
                log.warning("Refused %s (id=%s): %s", action, msg_id, refused["error"])
                return refused
            if WRITE_GAP and last_write_done is not None:
                pause = WRITE_GAP - (time.monotonic() - last_write_done)
                if pause > 0:
                    await asyncio.sleep(pause)
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
        if write:
            last_write_done = time.monotonic()
            if warning:
                reply.setdefault("warnings", []).append(warning)
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
        if msg.get("action") == "ping":
            reply["update"] = update_info()
        try:
            await ws.send(json.dumps(reply))
        except websockets.ConnectionClosed:
            return


class Unpaired(Exception):
    """The client did not prove the pairing token in time (it was refused, went away, or sent something else)."""


async def recv_json(ws, deadline: float):
    """The next message as JSON, by the monotonic `deadline` (Unpaired after it, or once the client is gone)."""
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.0, deadline - time.monotonic()))
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        raise Unpaired from None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None


async def pair(ws, role: str, hello: dict, deadline: float) -> str | None:
    """The challenge-response of the hello (ppro_bridge/auth.py, "Pairing"): the broker's proof, to send in the
    client's welcome, once the client proved the token; None after refusing it (refusal sent, connection closed).
    Nothing but the challenge is said before the client's proof matches."""
    client_nonce = hello.get("nonce")
    if hello.get("auth") != auth.SCHEME or not auth.valid_nonce(client_nonce):
        await refuse_pairing(ws, role, "malformed")
        return None
    broker_nonce = auth.new_nonce()
    try:
        await ws.send(json.dumps({"event": "challenge", "auth": auth.SCHEME, "nonce": broker_nonce,
                                  "broker": VERSION}))
    except websockets.ConnectionClosed:
        return None
    try:
        answer = await recv_json(ws, deadline)
    except Unpaired:
        record({"event": "refused", "reason": "token", "role": role, "token": "no_proof"})
        await close_quietly(ws, 1008, "no pairing proof in time")
        return None
    given = answer.get("proof") if isinstance(answer, dict) else None
    if not auth.proof_matches(given, TOKEN, "client", role, client_nonce, broker_nonce):
        await refuse_pairing(ws, role, "missing" if given is None else "wrong")
        return None
    return auth.proof(TOKEN, "broker", role, client_nonce, broker_nonce)


async def refuse_pairing(ws, role: str, state: str):
    """A client that did not prove the token: a panel gets {"event": "refused", "reason": "token"} and close 4401,
    a controller AUTH_FAILED and close 1008. `state`: missing / wrong / malformed (audited, never the proof)."""
    if role == "panel":
        await refuse_panel(ws, "token", 4401, PANEL_BAD_PROOF, token=state)
    else:
        await refuse(ws, role, missing=state == "missing", state=state)


async def route(ws):
    """First message declares the role; the client proves the pairing token; then hand off."""
    global unpaired
    if unpaired >= MAX_UNPAIRED:  # a flood of connections that never say hello: shed them before reading anything
        record({"event": "refused", "reason": "unpaired_limit", "limit": MAX_UNPAIRED})
        await close_quietly(ws, 1013, "too many connections in the hello")
        return
    unpaired += 1
    deadline = time.monotonic() + HELLO_TIMEOUT
    try:
        try:
            hello = await recv_json(ws, deadline)
        except Unpaired:
            return
        if hello is None:
            await close_quietly(ws, 1002, "First message must be JSON role declaration.")
            return
        if not isinstance(hello, dict):
            hello = {}
        role = hello.get("role")
        if role == "panel":
            plugin = hello.get("plugin")
            if plugin is not None and plugin != PLUGIN_ID:  # None: a 0.5 panel (handle_panel warns)
                await refuse_panel(ws, "plugin", 4403, NOT_OUR_PLUGIN, plugin=audit.shorten(plugin))
                return
            if "auth" in hello or "nonce" in hello:
                proof = await pair(ws, role, hello, deadline)
                if proof is None:
                    return
                state = "proved"
            elif "token" in hello:  # no panel sends the token itself; a script may (it gets no broker proof)
                if not auth.matches(hello.get("token"), TOKEN):
                    await refuse(ws, role, missing=False)
                    return
                proof, state = None, "token"
            elif PANEL_AUTH == "legacy":
                proof, state = None, "legacy"
            else:
                await refuse_panel(ws, "pairing", 4401, PANEL_UNPAIRED, token="unpaired",
                                   version=audit.shorten(hello.get("version")))
                return
        elif role == "controller":
            if "auth" in hello or "nonce" in hello:
                proof = await pair(ws, role, hello, deadline)
                if proof is None:
                    return
            elif auth.matches(hello.get("token"), TOKEN):  # a 0.5 / 0.6 CLI: the token itself
                proof = None
            else:
                await refuse(ws, role, missing=hello.get("token") is None)
                return
        else:
            await close_quietly(ws, 1002, 'First message must declare {"role": "panel"|"controller"}.')
            return
    finally:
        unpaired -= 1
    set_max_size(ws, MAX_MESSAGE)
    if role == "panel":
        await handle_panel(ws, state, hello, proof)
        return
    if proof is not None:
        try:
            await ws.send(json.dumps({"event": "authenticated", "broker": VERSION, "proof": proof}))
        except websockets.ConnectionClosed:
            return
    await handle_controller(ws)


async def refuse(ws, role: str, missing: bool = False, state: str | None = None):
    """A missing or wrong pairing token (or proof): say so in a reply (a 0.4 CLI prints it), then close with 1008."""
    state = state or ("missing" if missing else "wrong")
    why = {"missing": "no pairing token", "malformed": "a malformed pairing hello"}.get(state, "a wrong pairing token")
    log.warning("Refused a %s with %s.", role, why)
    record({"event": "refused", "reason": "token", "role": role, "token": state})
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


class PortHeld(OSError):
    """Another program holds [::1]:port, where a panel dialing ws://localhost arrives first: the broker must not run."""


def ipv6_missing(e: OSError) -> bool:
    """True when a failed bind on ::1 means this machine has no IPv6 loopback (127.0.0.1 alone is then right); False
    when the port is held or blocked there (EADDRINUSE, or EACCES from a program that bound it exclusively)."""
    return (not socket.has_ipv6 or isinstance(e, socket.gaierror)
            or e.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, getattr(errno, "EPROTONOSUPPORT", None)))


async def listen(stack: contextlib.AsyncExitStack, port: int) -> list[str]:
    """Serve on 127.0.0.1 and, where the machine has IPv6, on ::1 (the panel connects to ws://localhost, which may
    resolve to either); the servers close with `stack`. The hosts listened on; OSError when 127.0.0.1 is taken,
    PortHeld when another program holds [::1]:port (measured: it then receives the panel, which arrives over ::1)."""
    options = dict(max_size=PREAUTH_MAX, process_request=check_origin)  # raised to MAX_MESSAGE once paired
    await stack.enter_async_context(websockets.serve(route, HOST, port, **options))
    try:
        await stack.enter_async_context(websockets.serve(route, HOST6, port, **options))
    except OSError as e:
        if not ipv6_missing(e):
            raise PortHeld(e.errno, f"Cannot listen on [{HOST6}]:{port} ({e}): another program holds that port, and "
                                    f"a panel dialing ws://localhost:{port} would reach it instead of this broker.") from e
        log.warning("Not listening on [%s]:%d (%s): this machine has no IPv6 loopback. A panel whose localhost "
                    "resolves to ::1 first may not connect.", HOST6, port, e)
        return [HOST]
    return [HOST, HOST6]


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def listening_pid(netstat_text: str, host: str, port: int) -> int | None:
    """The PID listening on host:port in `netstat -ano` output (Proto, Local address, Foreign address, State, PID).
    The header and the state are in the system's language, so only the addresses are read: a listening socket's
    foreign address is 0.0.0.0:0 or [::]:0. A wildcard listener (0.0.0.0 / [::]) on the port counts when nothing
    listens on `host` itself."""
    local = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    wildcard = f"[::]:{port}" if ":" in host else f"0.0.0.0:{port}"
    found = {}
    for line in netstat_text.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0].upper() == "TCP" and parts[2] in ("0.0.0.0:0", "[::]:0")
                and parts[-1].isdigit()):
            found.setdefault(parts[1], int(parts[-1]))
    return found.get(local, found.get(wildcard))


def port_holder(host: str, port: int, timeout: float = 4.0) -> str | None:
    """Who listens on host:port, as "PID 1234 (python.exe)", for the message of a broker that cannot start: netstat
    for the PID, tasklist for its name (neither needs admin rights). Windows only. Best effort and bounded (two
    commands, `timeout` seconds each): None when it cannot tell, never an exception."""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCPv6" if ":" in host else "TCP"], capture_output=True,
                             text=True, errors="replace", timeout=timeout, creationflags=NO_WINDOW).stdout or ""
        pid = listening_pid(out, host, port)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if pid is None:
        return None
    name = None
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, errors="replace", timeout=timeout, creationflags=NO_WINDOW).stdout or ""
        for row in csv.reader(io.StringIO(out)):
            if len(row) > 1 and row[1].strip() == str(pid):
                name = row[0].strip()
                break
    except (OSError, subprocess.SubprocessError, ValueError, csv.Error):
        pass
    return f"PID {pid} ({name})" if name else f"PID {pid}"


def bind_failure(e: OSError, port: int) -> str:
    """The error a broker that cannot listen prints: which address, what holds it (when netstat can tell), what to do."""
    held6 = isinstance(e, PortHeld)
    holder = port_holder(HOST6 if held6 else HOST, port)
    text = e.strerror if held6 else f"Cannot listen on {HOST}:{port} ({e})."
    who = (f" It is held by {holder}." if holder else
           f" `netstat -ano | findstr :{port}` shows the PID of the program that holds it.")
    return f"{text}{who} Is another broker running? Close that program, then start the broker again."


def on_loader_event(event: dict):
    text = devloader.describe(event)
    if text:
        (log.warning if event.get("ok") is False or event.get("state") in ("missing", "still-running")
         else log.info)("%s", text)
    if event.get("event") in ("load", "udt") and event.get("state") != "unreachable":
        record(dict(event, event=f"devloader.{event['event']}"))


async def in_thread(fn):
    """fn() in a daemon thread, awaited without blocking the loop (a shutdown does not wait for it)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def deliver(result):
        if not fut.done():
            fut.set_result(result)

    def work():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 - Checker.run never raises; this keeps the loop alive if it did
            result = {"event": "update_check", "outcome": "error", "error": type(e).__name__}
        try:
            loop.call_soon_threadsafe(deliver, result)
        except RuntimeError:  # the loop closed meanwhile (the broker stopped)
            pass

    threading.Thread(target=work, name="update-check", daemon=True).start()
    return await fut


async def update_loop():
    """The update check: when one is due (at most once in 24 h; update.json remembers the last attempt across
    restarts), run it in a background thread, audit the outcome and tell the registered panel. Quiet on failure."""
    if UPDATES is None:
        return
    if not UPDATES.enabled:
        log.info("Update check: off (%s).", UPDATES.off_reason)
        record({"event": "update_check", "outcome": "off", "reason": UPDATES.off_reason})
        return
    while True:
        wait = UPDATES.wait_seconds()
        if wait > 0:
            await asyncio.sleep(min(wait, 3600))  # look again at least hourly: the wall clock may jump
            continue
        rec = await in_thread(UPDATES.run)
        record(rec)
        info = update_info() or {}
        if rec.get("outcome") == "ok" and info.get("newer") and not info.get("skipped"):
            log.info("Update check: %s is available (this broker is %s): %s", info.get("latest"), VERSION,
                     info.get("url") or updates.RELEASES_URL)
        if panel_ws is not None:
            await send_update(panel_ws)


_panel_seen = {}  # the panel socket last asked -> what its ping said (it does not change while it stays connected)


async def panel_state() -> dict | None:
    """For the devloader, before it loads a panel (which replaces the loaded one): None without a panel; busy while a
    request runs; else the codeHash and build.packaged its ping reports (asked once per panel connection)."""
    panel = panel_ws
    if panel is None:
        return None
    if panel_lock.locked():
        return {"busy": True}
    if panel not in _panel_seen:
        reply = await forward({"id": f"broker-state-{time.monotonic_ns()}", "action": "ping", "params": {}})
        if panel_ws is not panel:
            return None if panel_ws is None else {"busy": False}
        if not reply.get("ok") or not isinstance(reply.get("data"), dict):
            return {"busy": False}  # unknown code: the devloader loads, as before
        data = reply["data"]
        build = data.get("build") if isinstance(data.get("build"), dict) else {}
        seen = {"codeHash": data.get("codeHash"), "packaged": build.get("packaged") is True}
        if not (seen["codeHash"] or seen["packaged"]):
            return dict(seen, busy=False)  # a panel still hashing its files (or a 0.4 one): ask again next time
        _panel_seen.clear()
        _panel_seen[panel] = seen
    return dict(_panel_seen[panel], busy=False)


def panel_auth_policy(environ=None, settings=None) -> tuple[str, str | None]:
    """("required" | "legacy", where "legacy" came from): PPRO_PANEL_AUTH, else settings.json "panelAuth". Anything but
    the word "legacy" is "required" (fail closed)."""
    environ = os.environ if environ is None else environ
    value = (environ.get(PANEL_AUTH_ENV) or "").strip().lower()
    if value:
        return ("legacy", f"{PANEL_AUTH_ENV}=legacy") if value == "legacy" else ("required", None)
    if str((settings or {}).get("panelAuth", "")).strip().lower() == "legacy":
        return "legacy", 'settings.json "panelAuth": "legacy"'
    return "required", None


async def main(load_panel=False, plugin_dir=None):
    global TOKEN, AUDIT, UPDATES, PANEL_AUTH
    try:
        TOKEN, source = auth.ensure_token()
    except auth.TokenError as e:
        log.error("%s", e)
        sys.exit(1)
    where = auth.TOKEN_ENV if source == "env" else auth.token_path()
    log.info("Pairing token %s %s", "created in" if source == "created" else "from", where)
    default_file = auth.data_dir() / "token"
    if source == "env":
        log.warning("%s is set: the Premiere panel reads only the token file (%s), so it pairs with this broker only "
                    "when that file holds the same token.", auth.TOKEN_ENV, default_file)
    elif os.path.normcase(os.path.abspath(auth.token_path())) != os.path.normcase(os.path.abspath(default_file)):
        # QA-7: the same for a token file elsewhere - the panel reads only the default one.
        log.warning("%s points at %s: the Premiere panel reads only %s, so it pairs with this broker only when that "
                    "file holds the same token.", auth.TOKEN_FILE_ENV, auth.token_path(), default_file)
    PANEL_AUTH, legacy_from = panel_auth_policy(settings=updates.read_settings())
    if PANEL_AUTH == "legacy":
        log.warning("Legacy panels allowed (%s): a panel that cannot prove the pairing token (older than 0.7) is "
                    "registered anyway, so any local program can register as the panel while none is connected.",
                    legacy_from)
    for error in [e for e in auth.ACL_ERRORS if e]:
        log.warning("Could not narrow the access rights of what the bridge created (%s): other users may read it if "
                    "its folder lets them.", error)
    folder = audit.audit_dir()
    if folder is not None:
        AUDIT = audit.AuditLog(folder)
        pruned = AUDIT.prune()
        log.info("Audit log: %s%s", folder, f" (pruned {len(pruned)} old file(s))" if pruned else "")
    else:
        log.info("Audit log: off (%s=off)", audit.DIR_ENV)
    async with contextlib.AsyncExitStack() as stack:
        try:
            hosts = await listen(stack, PORT)
        except OSError as e:  # 127.0.0.1 taken, or PortHeld: [::1] held
            log.error("%s", bind_failure(e, PORT))
            sys.exit(1)
        record({"event": "start", "broker": VERSION, "port": PORT, "token": source, "hosts": hosts,
                "panelAuth": PANEL_AUTH})
        log.info("ppro-agent-bridge broker %s listening on %s", VERSION,
                 ", ".join(f"ws://[{h}]:{PORT}" if ":" in h else f"ws://{h}:{PORT}" for h in hosts))
        log.info("Waiting for the Premiere panel to connect...")
        UPDATES = updates.Checker(version=VERSION)
        checker = asyncio.get_running_loop().create_task(update_loop())
        loader = None
        if load_panel:
            plugin = Path(plugin_dir).resolve() if plugin_dir else devloader.PLUGIN_DIR
            if devloader.developer_mode() is False:
                log.warning("UXP developer mode is off (%s): turn it on in UXP Developer Tool, or Premiere will not "
                            "load the panel.", devloader.developer_mode_file())
            log.info("Loading the panel from %s whenever Premiere connects to UXP Developer Tool.", plugin)
            loader = asyncio.get_running_loop().create_task(devloader.watch(plugin, on_event=on_loader_event,
                                                                            panel_state=panel_state))
        stop = asyncio.get_running_loop().create_future()
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, lambda: stop.set_result(None))
        except NotImplementedError:
            pass  # Windows: fall back to KeyboardInterrupt
        try:
            await stop
        finally:
            checker.cancel()
            if loader is not None:
                loader.cancel()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="broker", description="ppro-agent-bridge broker (127.0.0.1 and ::1 only).")
    parser.add_argument("--load-panel", action="store_true", default=os.environ.get("PPRO_LOAD_PANEL") == "1",
                        help="load the panel whenever Premiere connects to UXP Developer Tool (no clicking Load)")
    parser.add_argument("--plugin", default=None, help="plugin folder for --load-panel (default: this repo's plugin/)")
    cli = parser.parse_args()
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        asyncio.run(main(cli.load_panel, cli.plugin))
    except KeyboardInterrupt:
        log.info("Broker stopped.")
