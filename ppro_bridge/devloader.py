"""Load the Agent Bridge panel into Premiere with no clicks: UXP Developer Tool runs minimized, this drives it.

Why UXP Developer Tool (UDT) still has to run: with UXP developer mode on, Premiere connects to UDT's developer
service on ws://127.0.0.1:14001 only after UDT announces that port through Adobe's own IPC (Vulcan, a closed native
library). A look-alike service of ours on the same port is never contacted (tried on 27.1 build 21, 22 Oct 2026).

What this does instead: it talks to UDT's service on its command-line endpoint (/socket/cli - what Adobe's
`uxp plugin load` uses; protocol from Adobe's Apache-2.0 devtools-cli, uxp-devtools-core/src/core/service):
  service -> us   {"command": "didAddRuntimeClient", "id": n, "app": {"appId": "premierepro", "appVersion": ...}}
                  (one per connected app, now and whenever one connects later), then {"command": "didCompleteConnection"}
  us -> service   {"command": "proxy", "clientId": n, "requestId": r, "message": {"command": "Plugin",
                   "action": "load", "params": {"provider": {"type": "disk", "id": ..., "path": <plugin folder>}},
                   "breakOnStart": false}}
  service -> us   {"command": "reply", "requestId": r, "pluginSessionId": ...}   or {..., "error": ...}
  service -> us   {"command": "didRemoveRuntimeClient", "id": n, ...}            (the app went away)
The broker runs `watch()` with --load-panel. Loads are development loads, exactly like clicking Load in UDT.

UDT runs only while the bridge loads the panel (RA-5, SECURITY_REVIEW.md): UDT's service takes a WebSocket from
anyone - any Origin, no credential (measured 6 Nov 2026: "101 Switching Protocols" for none, http://evil.example,
null and file://) - and can load any folder as a plugin, while a loaded panel keeps running once UDT is gone (three
pings over 15 s after UDT's processes were killed, same loadedAt). So `watch()`:
  - when UDT is not running and a Premiere process it has not handled appears (Adobe Premiere Pro.exe or
    Adobe Premiere Pro (Beta).exe, polled every PREMIERE_POLL seconds without UDT), starts UDT minimized, waits for
    that Premiere to connect, loads the panel, waits until the broker sees it answering (ANSWER_WAIT), and closes
    UDT again (its process tree); after a failed load too. A Premiere that quits and starts again is a new process:
    it gets a load the same way. The "did not load the panel" cases (no_load_reason) start no UDT at all.
  - when UDT is already running (the user opened it), keeps a connection and loads into every Premiere that connects,
    as before, and never closes it; also with --no-start (start=False), which never starts UDT.
  - closes a UDT it started when it is stopped (the broker quits).
  - where there is no process list at all (not Windows), keeps the old way: starts UDT when it is down and keeps it.
    On Windows a tasklist that fails or times out only means "look again": no UDT is started until the list reads
    again (one failed tasklist on 8 Nov 2026 used to leave UDT open, and restarted, for the rest of the broker's life).

A load REPLACES a panel with the same plugin id that is already loaded: the old one disconnects ~30 ms before the
load's reply and the new one registers ~0.35 s later, also when the old one came from another folder (the broker's
audit logs, 22 Oct and 2 Nov 2026). So with the broker's `panel_state` (what its connected panel reports), watch()
first waits up to LOAD_GRACE seconds for a panel and loads nothing over one that is busy, already runs this
folder's code (codeHash) or is an installed package (ping build.packaged) - e.g. the panel whose Start bridge button
just started this broker; a panel with other code is replaced as before. While such a panel is connected, UDT is
not started either.

  python -m ppro_bridge.devloader              load once into the Premiere(s) connected to UDT
  python -m ppro_bridge.devloader --watch      keep loading whenever Premiere connects (Ctrl+C to stop)
"""

import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import panelhash

DEFAULT_PORT = 14001
HOST = "127.0.0.1"
REQUEST_TIMEOUT = 15.0
PROCESS_LIST_EXPECTED = os.name == "nt"  # None from premiere_pids is then a failure to read it, never "no list here"
RETRY_SECONDS = 5.0
# Seconds to wait for a panel on the broker before loading: a disconnected panel retries every 3 s, and on Windows an
# attempt that started while the broker was down can take ~1-2 s more to fail or succeed. (23:08, 2 Nov, a fresh
# Premiere with no panel: UDT connected at 08:44.243, the load went out at 08:48.476 after the then 4 s grace.)
LOAD_GRACE = 6.0
PREMIERE_POLL = 3.0          # s between two looks at the process list while UDT is not running
UDT_START_WAIT = 60.0        # s for a UDT we started to open its service
PREMIERE_CONNECT_WAIT = 120.0  # s for Premiere (maybe still starting up) to connect to that UDT
ANSWER_WAIT = 20.0           # s after the load for the broker to see the panel answering, before UDT is closed
STARTUP_GRACE = 20.0         # s a Premiere that started after the broker gets for its own panel (an installed one) to
                             # connect before UDT is started for it (its process appears long before its UXP is up)
CLOSE_WAIT = 5.0             # s for UDT's service to go away once its processes were ended
PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"
UDT_ENV = "PPRO_UDT_EXE"
UDT_EXE = "Adobe UXP Developer Tools.exe"
PREMIERE_IMAGES = ("adobe premiere pro.exe", "adobe premiere pro (beta).exe")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

log = logging.getLogger("devloader")


# ---------------------------------------------------------------- the machine

def developer_mode_file() -> Path:
    base = os.environ.get("CommonProgramFiles") or os.path.join(
        os.environ.get("SystemDrive", "C:") + os.sep, "Program Files", "Common Files")
    return Path(base) / "Adobe" / "UXP" / "Developer" / "settings.json"


def developer_mode() -> bool | None:
    """True / False from UXP's developer settings file; None when it cannot be read."""
    try:
        return bool(json.loads(developer_mode_file().read_text(encoding="utf-8-sig")).get("developer"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, AttributeError):
        return None


def find_udt() -> Path | None:
    """UXP Developer Tool's executable: PPRO_UDT_EXE, the uninstall entry in the registry, or the default folders."""
    env = os.environ.get(UDT_ENV)
    if env:
        return Path(env) if Path(env).is_file() else None
    candidates = []
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    with winreg.OpenKey(root, sub) as key:
                        for i in range(winreg.QueryInfoKey(key)[0]):
                            try:
                                with winreg.OpenKey(key, winreg.EnumKey(key, i)) as app:
                                    name = winreg.QueryValueEx(app, "DisplayName")[0]
                                    if "UXP Developer" in str(name):
                                        candidates.append(Path(winreg.QueryValueEx(app, "InstallLocation")[0]) / UDT_EXE)
                            except OSError:
                                continue
                except OSError:
                    continue
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.append(Path(base) / "Adobe" / "Adobe UXP Developer Tools" / UDT_EXE)
    return next((c for c in candidates if c.is_file()), None)


def service_up(port=DEFAULT_PORT) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=1):
            return True
    except OSError:
        return False


def start_udt(exe: Path):
    """Start UXP Developer Tool minimized and without taking the focus, detached from the caller's console: started
    as a plain child of the broker it shared the broker window's console, so closing that window closed UDT too
    (3 Nov 2026: UDT and its helpers were on the console's process list). Returns the process (for stop_udt)."""
    info, flags = None, 0
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 7  # SW_SHOWMINNOACTIVE
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen([str(exe)], startupinfo=info, creationflags=flags, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_udt(proc) -> str | None:
    """End a UXP Developer Tool that start_udt started: its whole process tree (Electron's helpers too; taskkill /T
    /F by its PID, an argument list). Never anything by name, so a UDT the user opened is never touched. None when
    done (or already gone), else why not."""
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        return None
    if os.name != "nt":
        try:
            proc.terminate()
        except OSError as e:
            return str(e)
        return None
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return f"taskkill failed ({e})"
    return None if r.returncode in (0, 128) else f"taskkill exit {r.returncode}"  # 128: no such process


def premiere_pids(run=subprocess.run) -> set[int] | None:
    """The PIDs of the running Premieres (Adobe Premiere Pro.exe, Adobe Premiere Pro (Beta).exe), from tasklist (no
    admin rights; image names are not translated); None when the list cannot be read (not Windows, tasklist failed)."""
    if os.name != "nt":
        return None
    try:
        out = run(["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq Adobe Premiere Pro*"], capture_output=True,
                  text=True, errors="replace", timeout=10, creationflags=_NO_WINDOW).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_tasklist(out)


def parse_tasklist(text: str) -> set[int]:
    """Premiere PIDs in `tasklist /FO CSV /NH` output (image name, PID, ...); other rows and the "INFO: ..." line are
    ignored."""
    import csv
    import io
    pids = set()
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) > 1 and row[0].strip().lower() in PREMIERE_IMAGES and row[1].strip().isdigit():
            pids.add(int(row[1].strip()))
    return pids


def plugin_id(plugin_dir) -> str | None:
    try:
        return json.loads((Path(plugin_dir) / "manifest.json").read_text(encoding="utf-8-sig")).get("id")
    except (OSError, ValueError, AttributeError):
        return None


def is_premiere(app: dict) -> bool:
    return str((app or {}).get("appId") or "").upper().startswith(("PPRO", "PREMIEREPRO"))


def load_message(plugin_dir) -> dict:
    plugin_dir = Path(plugin_dir).resolve()
    provider = {"type": "disk", "path": str(plugin_dir)}
    pid = plugin_id(plugin_dir)
    if pid:
        provider["id"] = pid
    return {"command": "Plugin", "action": "load", "params": {"provider": provider}, "breakOnStart": False}


# ---------------------------------------------------------------- the service connection

class _Session:
    """One connection to UDT's /socket/cli: tracks the connected Premieres and loads the panel into them."""

    def __init__(self, ws, plugin_dir, on_event):
        self.ws, self.plugin_dir, self.on_event = ws, Path(plugin_dir).resolve(), on_event
        self.apps, self.pending, self.rid = {}, {}, 0
        self.complete = asyncio.Event()

    async def load(self, client_id, app) -> dict:
        self.rid += 1
        rid = self.rid
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.ws.send(json.dumps({"command": "proxy", "clientId": client_id, "requestId": rid,
                                       "message": load_message(self.plugin_dir)}))
        entry = {"event": "load", "app": app.get("appId"), "appVersion": app.get("appVersion")}
        try:
            reply = await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            entry.update(ok=False, error="no reply (Premiere busy or in a modal dialog?)")
        else:
            if reply.get("error"):
                entry.update(ok=False, error=str(reply["error"]))
            else:
                entry.update(ok=True, session=reply.get("pluginSessionId"), path=str(self.plugin_dir))
        finally:
            self.pending.pop(rid, None)
        self.on_event(entry)
        return entry

    async def pump(self, on_app=None):
        """Read the service's messages until the connection closes; on_app(client_id, app) for each new Premiere."""
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            command = msg.get("command") if isinstance(msg, dict) else None
            if command == "reply":
                fut = self.pending.get(msg.get("requestId"))
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            elif command == "didAddRuntimeClient":
                app = msg.get("app") or {}
                self.on_event({"event": "app", "state": "connected", "app": app.get("appId"),
                               "appVersion": app.get("appVersion")})
                if is_premiere(app):
                    self.apps[msg.get("id")] = app
                    if on_app is not None:
                        on_app(msg.get("id"), app)
            elif command == "didRemoveRuntimeClient":
                app = self.apps.pop(msg.get("id"), None) or msg.get("app") or {}
                self.on_event({"event": "app", "state": "disconnected", "app": app.get("appId")})
            elif command == "didCompleteConnection":
                self.complete.set()


async def no_load_reason(panel_state, plugin_dir, grace=None) -> str | None:
    """Why not to load the panel now, or None to load. `panel_state()` (the broker's) returns None without a connected
    panel, else {"busy": bool, "codeHash": str | None, "packaged": bool}; a panel that is reconnecting is waited for
    up to `grace` seconds (LOAD_GRACE)."""
    grace = LOAD_GRACE if grace is None else grace
    deadline = time.monotonic() + grace
    want = panelhash.code_hash(plugin_dir)
    while True:
        try:
            state = await asyncio.wait_for(panel_state(), max(grace, 1.0))
        except asyncio.TimeoutError:
            state = {"busy": True}  # it did not answer a ping in time: it is working on something
        except Exception:  # noqa: BLE001  (the broker's view is advice: a failure must not stop loading)
            state = None
        if state:
            if state.get("busy"):
                return "the panel connected to the broker is busy with a request"
            if state.get("packaged"):
                return "an installed (packaged) Agent Bridge panel is connected to the broker"
            if want and state.get("codeHash") == want:
                return "the panel connected to the broker already runs this code"
            if state.get("codeHash"):
                return None  # other code: load it over that panel, as UDT's Load does
            # no codeHash yet (a panel still hashing its files, or one older than 0.5): ask again until the grace ends,
            # once a second (each ask is a ping through the panel's queue and log)
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(1.0 if state else 0.25)


async def load_once(plugin_dir=PLUGIN_DIR, port=DEFAULT_PORT, wait=10.0, on_event=None) -> list[dict]:
    """Load the panel into every Premiere connected to UDT's service now; [] when none connects within `wait`."""
    import websockets
    events = []
    on_event = on_event or (lambda e: None)
    async with websockets.connect(f"ws://{HOST}:{port}/socket/cli", open_timeout=5) as ws:
        session = _Session(ws, plugin_dir, lambda e: (events.append(e), on_event(e)))
        reader = asyncio.get_running_loop().create_task(session.pump())
        try:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not (session.complete.is_set() and session.apps):
                await asyncio.sleep(0.1)
            results = [await session.load(cid, app) for cid, app in list(session.apps.items())]
        finally:
            reader.cancel()
    return results


async def _up(port) -> bool:
    """service_up off the event loop (a refused connect takes ~2 s on Windows: the broker must keep serving)."""
    return await asyncio.to_thread(service_up, port)


async def _wait_for(check, seconds: float, stop: asyncio.Event, every: float = 0.25) -> bool:
    """True once check() (sync or async) is true within `seconds`; False on timeout or when `stop` is set."""
    deadline = time.monotonic() + seconds
    while not stop.is_set():
        result = check()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        if time.monotonic() >= deadline:
            return False
        await _sleep_or_stop(stop, every)
    return False


async def _panel_answers(panel_state, plugin_dir, stop) -> bool:
    """The broker sees a panel running this folder's code (or, when the code cannot be hashed, any panel) within
    ANSWER_WAIT; True at once without the broker's view (devloader on its own)."""
    if panel_state is None:
        return True
    want = panelhash.code_hash(plugin_dir)

    async def answering():
        try:
            state = await asyncio.wait_for(panel_state(), 5)
        except Exception:  # noqa: BLE001  (advice only)
            return False
        return bool(state) and not state.get("busy") and (want is None or state.get("codeHash") == want)

    return await _wait_for(answering, ANSWER_WAIT, stop, every=0.5)


async def _load_with_own_udt(exe, plugin_dir, port, on_event, stop, panel_state) -> bool:
    """Start UDT, wait for Premiere to connect to it, load the panel into every Premiere there (unless no_load_reason
    says not to), wait until the panel answers, then close UDT - whatever happened. True when a load worked."""
    import websockets
    on_event({"event": "udt", "state": "starting", "exe": str(exe)})
    try:
        proc = start_udt(exe)
    except OSError as e:
        on_event({"event": "udt", "state": "missing", "error": f"cannot start {exe} ({e})"})
        return False
    loaded, why = False, "the load failed"
    try:
        if not await _wait_for(lambda: _up(port), UDT_START_WAIT, stop, every=0.5):
            why = "its service did not come up"
            if not stop.is_set():
                on_event({"event": "udt", "state": "unreachable",
                          "error": f"the UXP Developer Tool it started did not open port {port} within "
                                   f"{UDT_START_WAIT:g} s"})
            return False
        async with websockets.connect(f"ws://{HOST}:{port}/socket/cli", open_timeout=5) as ws:
            on_event({"event": "udt", "state": "connected"})
            session = _Session(ws, plugin_dir, on_event)
            pump = asyncio.get_running_loop().create_task(session.pump())
            try:
                if not await _wait_for(lambda: bool(session.apps), PREMIERE_CONNECT_WAIT, stop):
                    if not stop.is_set():
                        why = "Premiere did not connect to it"
                        on_event({"event": "load", "ok": False, "app": "premierepro", "appVersion": "",
                                  "error": f"Premiere did not connect to UXP Developer Tool within "
                                           f"{PREMIERE_CONNECT_WAIT:g} s (is UXP developer mode on?)"})
                    return False
                await _sleep_or_stop(stop, 1.0)  # a second Premiere (the Beta) announced with the first
                for client_id, app in list(session.apps.items()):
                    reason = await no_load_reason(panel_state, plugin_dir) if panel_state is not None else None
                    if reason:
                        on_event({"event": "load", "skipped": True, "reason": reason, "app": app.get("appId"),
                                  "appVersion": app.get("appVersion")})
                        why = "it had nothing to load"
                        continue
                    if (await session.load(client_id, app)).get("ok"):
                        loaded = True
                if loaded:
                    answered = await _panel_answers(panel_state, plugin_dir, stop)
                    why = ("the panel is loaded and answering" if answered else
                           f"the panel is loaded (the broker did not see it answer within {ANSWER_WAIT:g} s)")
            finally:
                pump.cancel()
    except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
        why = "its service failed"
        on_event({"event": "udt", "state": "unreachable", "error": str(e)})
    except asyncio.CancelledError:  # the broker cancels its loader when it stops: UDT is closed all the same
        why = "the broker is stopping"
        raise
    finally:
        await _close_own_udt(proc, port, on_event, "the broker is stopping" if stop.is_set() else why)
    return loaded


async def _close_own_udt(proc, port, on_event, why):
    error = await asyncio.to_thread(stop_udt, proc)

    async def gone_now():
        return not await _up(port)

    gone = await _wait_for(gone_now, CLOSE_WAIT, asyncio.Event(), every=0.25)
    event = {"event": "udt", "state": "closed" if gone and not error else "still-running", "reason": why}
    if error or not gone:
        event["error"] = error or f"its service still answers on port {port}"
    on_event(event)


async def watch(plugin_dir=PLUGIN_DIR, port=DEFAULT_PORT, start=True, on_event=None, stop: asyncio.Event = None,
                panel_state=None, processes=None):
    """Load the panel into every Premiere, with UXP Developer Tool running only while it loads (module doc). Runs until
    `stop` is set. panel_state: the broker's async view of its panel (see no_load_reason); without it every Premiere
    gets a load. processes(): the running Premieres' PIDs, or None when that cannot be told (default premiere_pids)."""
    import websockets
    on_event = on_event or (lambda e: None)
    stop = stop or asyncio.Event()
    processes = premiere_pids if processes is None else processes
    handled: set[int] = set()  # Premiere processes that got their load (or did not need one)
    said = None                # the last "not-needed" / "missing" said, so a retry does not say it again
    initial = None             # Premieres running when the bridge starts: their panel would reconnect within seconds
    unread = False             # the process list failed to read (said once, until it reads again)
    while not stop.is_set():
        pids = await asyncio.to_thread(processes) if start else None
        if start and pids is None and PROCESS_LIST_EXPECTED:  # never the old way on a passing failure (RA-5)
            if not unread:
                on_event({"event": "udt", "state": "no-process-list"})
            unread = True
            await _sleep_or_stop(stop, PREMIERE_POLL)
            continue
        unread = False
        if pids is not None and await _up(port):  # a UDT the bridge did not start (the user's): follow, never close
            await _follow(plugin_dir, port, False, on_event, stop, panel_state, until_gone=True)
            handled |= await asyncio.to_thread(processes) or set()  # what that UDT saw needs nothing more from us
            await _sleep_or_stop(stop, RETRY_SECONDS)
            continue
        if pids is not None:  # UDT is not running: the bridge starts it only to load, and closes it again
            initial = set(pids) if initial is None else initial
            handled &= pids  # a Premiere that quit: its PID may come back as another process
            new = pids - handled
            if not new:
                await _sleep_or_stop(stop, PREMIERE_POLL)
                continue
            grace = LOAD_GRACE if new <= initial else STARTUP_GRACE
            reason = await no_load_reason(panel_state, plugin_dir, grace) if panel_state is not None else None
            exe = None if reason else find_udt()
            if reason or exe is None:
                event = ({"event": "udt", "state": "not-needed", "reason": reason} if reason else
                         {"event": "udt", "state": "missing", "error": f"UXP Developer Tool not found (set {UDT_ENV})"})
                if event != said:
                    on_event(event)
                said = event
                handled |= new
                continue
            said = None
            await _load_with_own_udt(exe, plugin_dir, port, on_event, stop, panel_state)
            handled |= new
            continue
        # --no-start (never start UDT), or a system with no process list at all (the old way: start UDT when down, keep it)
        await _follow(plugin_dir, port, start, on_event, stop, panel_state, until_gone=False)


async def _follow(plugin_dir, port, start, on_event, stop, panel_state, until_gone=False):
    """One connection to a UDT that runs anyway, and a load into every Premiere that connects; the bridge never closes
    that UDT. `start`: start UDT when its service is down (only where no process list can be read). `until_gone`:
    return once that UDT's service goes away (the user closed it), else retry until `stop`."""
    import websockets
    started_udt = False
    said_not_needed = False
    while not stop.is_set():
        try:
            ws = await websockets.connect(f"ws://{HOST}:{port}/socket/cli", open_timeout=5)
        except ConnectionRefusedError:
            ws = None
            reason = None
            if start and not started_udt and panel_state is not None:
                reason = await no_load_reason(panel_state, plugin_dir)
                if reason and not said_not_needed:
                    on_event({"event": "udt", "state": "not-needed", "reason": reason})
                said_not_needed = bool(reason)
            if start and not started_udt and not reason:
                exe = find_udt()
                if exe is not None:
                    on_event({"event": "udt", "state": "starting", "exe": str(exe)})
                    start_udt(exe)
                else:
                    on_event({"event": "udt", "state": "missing",
                              "error": f"UXP Developer Tool not found (set {UDT_ENV})"})
                started_udt = True  # start it (or say it is missing) once per outage
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
            ws = None
            on_event({"event": "udt", "state": "unreachable", "error": str(e)})
        if ws is None:
            if until_gone:
                return
            await _sleep_or_stop(stop, RETRY_SECONDS)
            continue
        try:
            async with ws:
                on_event({"event": "udt", "state": "connected"})
                session = _Session(ws, plugin_dir, on_event)
                loading = set()

                async def load_unless_live(client_id, app):
                    reason = await no_load_reason(panel_state, plugin_dir) if panel_state is not None else None
                    if reason:
                        on_event({"event": "load", "skipped": True, "reason": reason, "app": app.get("appId"),
                                  "appVersion": app.get("appVersion")})
                        return
                    await session.load(client_id, app)

                def on_app(client_id, app):
                    task = asyncio.get_running_loop().create_task(load_unless_live(client_id, app))
                    loading.add(task)
                    task.add_done_callback(loading.discard)

                pump = asyncio.get_running_loop().create_task(session.pump(on_app))
                stopper = asyncio.get_running_loop().create_task(stop.wait())
                await asyncio.wait({pump, stopper}, return_when=asyncio.FIRST_COMPLETED)
                for task in (pump, stopper, *loading):
                    task.cancel()
                on_event({"event": "udt", "state": "disconnected"})
        except (OSError, websockets.WebSocketException) as e:
            on_event({"event": "udt", "state": "unreachable", "error": str(e)})
        if until_gone:
            return
        started_udt = False
        await _sleep_or_stop(stop, RETRY_SECONDS)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


def describe(event: dict) -> str:
    kind, state = event.get("event"), event.get("state")
    if kind == "load":
        if event.get("skipped"):
            return f"Did not load the panel into {event.get('app')} {event.get('appVersion')}: {event.get('reason')}."
        if event.get("ok"):
            return f"Loaded the Agent Bridge panel into {event.get('app')} {event.get('appVersion')}."
        return f"Could not load the panel into {event.get('app')} {event.get('appVersion')}: {event.get('error')}"
    if kind == "app" and state in ("connected", "disconnected"):
        return f"{event.get('app')} {event.get('appVersion') or ''} {state} (UXP Developer Tool).".replace("  ", " ")
    if kind == "udt":
        return {"starting": f"Starting UXP Developer Tool minimized ({event.get('exe')}) to load the panel.",
                "closed": f"Closed the UXP Developer Tool the bridge started ({event.get('reason')}).",
                "still-running": f"Could not close the UXP Developer Tool the bridge started ({event.get('error')}): "
                                 "close it by hand.",
                "connected": "Connected to UXP Developer Tool's service; waiting for Premiere.",
                "disconnected": "UXP Developer Tool's service went away; retrying.",
                "missing": f"Cannot load the panel: {event.get('error')}.",
                "not-needed": f"Not starting UXP Developer Tool: {event.get('reason')}.",
                "no-process-list": "Could not read the list of running programs (tasklist): not starting UXP Developer "
                                   "Tool until it can be read; retrying.",
                "unreachable": f"UXP Developer Tool's service is not answering ({event.get('error')}); retrying."}.get(state, "")
    return ""


async def _main(args) -> int:
    def say(event):
        text = describe(event)
        if text:
            (log.warning if event.get("ok") is False or event.get("state") == "missing" else log.info)("%s", text)

    if developer_mode() is False:
        log.warning("UXP developer mode is off (%s): enable it in UXP Developer Tool first.", developer_mode_file())
    if args.watch:
        try:
            await watch(args.plugin, args.port, start=not args.no_start, on_event=say)
        except asyncio.CancelledError:
            pass
        return 0
    own = None  # a UDT started here is closed again once the load is done (RA-5)
    if not service_up(args.port):
        exe = None if args.no_start else find_udt()
        if exe is None:
            log.error("UXP Developer Tool is not running%s.", "" if args.no_start else f" and was not found (set {UDT_ENV})")
            return 1
        say({"event": "udt", "state": "starting", "exe": str(exe)})
        own = start_udt(exe)
        for _ in range(60):
            if service_up(args.port):
                break
            await asyncio.sleep(0.5)
    try:
        results = await load_once(args.plugin, args.port, wait=args.wait, on_event=say)
    finally:
        if own is not None:
            await _close_own_udt(own, args.port, say, "the load is done")
    if not results:
        log.error("No Premiere connected to UXP Developer Tool within %gs (is Premiere open?).", args.wait)
    return 0 if results and all(r["ok"] for r in results) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="devloader", description=__doc__.split("\n\n")[0])
    p.add_argument("--plugin", default=str(PLUGIN_DIR), help="the plugin folder (default: this repo's plugin/)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="UXP Developer Tool's service port (default 14001)")
    p.add_argument("--watch", action="store_true", help="keep loading the panel whenever Premiere connects")
    p.add_argument("--wait", type=float, default=30.0, help="once: seconds to wait for Premiere (default 30)")
    p.add_argument("--no-start", action="store_true", help="do not start UXP Developer Tool when it is not running")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
