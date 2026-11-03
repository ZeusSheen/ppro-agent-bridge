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
`watch()` keeps that connection and loads the panel into every Premiere that connects - after a Premiere restart too -
and starts UDT minimized when it is not running. The broker runs it with --load-panel. Loads are development loads,
exactly like clicking Load in UDT.

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
RETRY_SECONDS = 5.0
# Seconds to wait for a panel on the broker before loading: a disconnected panel retries every 3 s, and on Windows an
# attempt that started while the broker was down can take ~1-2 s more to fail or succeed. (23:08, 2 Nov, a fresh
# Premiere with no panel: UDT connected at 08:44.243, the load went out at 08:48.476 after the then 4 s grace.)
LOAD_GRACE = 6.0
PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"
UDT_ENV = "PPRO_UDT_EXE"
UDT_EXE = "Adobe UXP Developer Tools.exe"

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


def start_udt(exe: Path) -> None:
    """Start UXP Developer Tool minimized and without taking the focus, detached from the caller's console: started
    as a plain child of the broker it shared the broker window's console, so closing that window closed UDT too
    (3 Nov 2026: UDT and its helpers were on the console's process list)."""
    info, flags = None, 0
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 7  # SW_SHOWMINNOACTIVE
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe)], startupinfo=info, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


async def watch(plugin_dir=PLUGIN_DIR, port=DEFAULT_PORT, start=True, on_event=None, stop: asyncio.Event = None,
                panel_state=None):
    """Keep a connection to UDT's service and load the panel into every Premiere that connects. Starts UDT
    (minimized) when its service is down and `start`; retries every RETRY_SECONDS. Runs until `stop` is set.
    panel_state: the broker's async view of its panel (see no_load_reason); without it every Premiere gets a load."""
    import websockets
    on_event = on_event or (lambda e: None)
    stop = stop or asyncio.Event()
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
        return {"starting": f"Starting UXP Developer Tool minimized ({event.get('exe')}).",
                "connected": "Connected to UXP Developer Tool's service; waiting for Premiere.",
                "disconnected": "UXP Developer Tool's service went away; retrying.",
                "missing": f"Cannot load the panel: {event.get('error')}.",
                "not-needed": f"Not starting UXP Developer Tool: {event.get('reason')}.",
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
    if not service_up(args.port):
        exe = None if args.no_start else find_udt()
        if exe is None:
            log.error("UXP Developer Tool is not running%s.", "" if args.no_start else f" and was not found (set {UDT_ENV})")
            return 1
        say({"event": "udt", "state": "starting", "exe": str(exe)})
        start_udt(exe)
        for _ in range(60):
            if service_up(args.port):
                break
            await asyncio.sleep(0.5)
    results = await load_once(args.plugin, args.port, wait=args.wait, on_event=say)
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
