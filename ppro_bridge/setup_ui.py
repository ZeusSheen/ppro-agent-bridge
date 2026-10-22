"""The setup window: a local page (setup_ui.html) that walks through doctor's checks and runs each fix with a click.

  python -m ppro_bridge.setup_ui                 open the setup window (Edge in app mode, else the default browser)
  python -m ppro_bridge.setup_ui --no-browser    only print its URL
  python -m ppro_bridge.setup_ui --demo          a made-up machine: nothing is read or changed (screenshots, tests)
  python -m ppro_bridge.setup_ui --port 8123     a fixed port (default 0: any free one)

A small HTTP server on 127.0.0.1 serves the page and a JSON API under a secret path /<secret>/ (a fresh
secrets.token_urlsafe(18) every run; the printed URL carries it):
  GET  /<secret>/             the page
  GET  /<secret>/api/state    {version, demo, platform, repo, python, pythonExe, premiereMin, checks (doctor.checks()
                              verbatim: name, ok, required, detail, fix), shortcuts {desktop, autostart},
                              brokerRunning, busy (the action running now, or null)}
  POST /<secret>/api/action   {"action": name} -> {ok, action, message, output (the command's last 4000 characters),
                              state}; one action at a time (409 "busy"), an unknown one is 400. Names: ACTIONS.
  POST /<secret>/api/ping     heartbeat                  POST /<secret>/api/bye    stop the server

Guards (defence in depth: every local process and every web page the user opens can reach the port): the Host header
must be exactly 127.0.0.1:<port> (DNS rebinding), an Origin header, when sent, http://127.0.0.1:<port>, and a
Sec-Fetch-Site header, when sent, same-origin or none - else 403. A wrong or missing secret is 404. Every POST needs
the header X-Bridge-Setup: 1 (a cross-origin page cannot send it without a CORS preflight, which is never answered)
and a JSON object body - else 403. Responses are no-store and nosniff; the page gets a strict Content-Security-Policy.

The server stops on bye (the page sends it when its window closes; it waits BYE_GRACE_SECONDS first, because a page
that reloads says bye too and then asks again), after IDLE_SECONDS without a request once the page has made one (it
pings while open; never while an action runs), or on Ctrl+C. Under pythonw.exe (setup_bridge.bat starts it that way,
so no console window stays open) a failure to start is shown in a message box. Stdlib only: it has to run before the
requirements are installed.
"""

import argparse
import hmac
import json
import logging
import os
import platform
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import console, devloader, doctor
from ._version import VERSION

PAGE = Path(__file__).resolve().with_name("setup_ui.html")
HOST = "127.0.0.1"
IDLE_SECONDS = 300.0
BYE_GRACE_SECONDS = 3.0
PIP_TIMEOUT = 900
OUTPUT_LIMIT = 4000
MAX_BODY = 64 * 1024
START_WAIT_SECONDS = 8.0
START_POLL_SECONDS = 0.5
DEMO_STEP_SECONDS = 1.2
WINDOW_SIZE = "1120,780"
CC_URL = "https://creativecloud.adobe.com/apps/all/desktop"
FFMPEG_URL = "https://ffmpeg.org/download.html"
CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'")
GET_ROUTES = ("", "api/state")
POST_ROUTES = ("api/action", "api/ping", "api/bye")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# What each action does, as "Could not <...>" completes it.
ACTIONS = {
    "install-deps": "install the Python requirements",
    "install-pillow": "install Pillow",
    "install-whisper": "install faster-whisper",
    "shortcut-desktop": "create the desktop shortcut",
    "autostart-on": "start the bridge at login",
    "autostart-off": "stop starting the bridge at login",
    "open-udt": "open UXP Developer Tool",
    "open-cc": "open the Creative Cloud app",
    "open-ffmpeg": "open the FFmpeg download page",
    "open-folder": "open the bridge folder",
    "start-bridge": "start the bridge",
}

log = logging.getLogger("setup_ui")


# ---------------------------------------------------------------- windows and programs

def find_edge() -> Path | None:
    """msedge.exe: the default install folders, else the App Paths entry in the registry."""
    candidates = [Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
                  for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")) if base]
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                value = winreg.QueryValue(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe")
            except OSError:
                continue
            if value and value.strip().strip('"'):
                candidates.append(Path(value.strip().strip('"')))
    return next((c for c in candidates if c.is_file()), None)


def find_creative_cloud() -> Path | None:
    """The Creative Cloud desktop app's executable in the default install folders."""
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            exe = Path(base) / "Adobe" / "Adobe Creative Cloud" / "ACC" / "Creative Cloud.exe"
            if exe.is_file():
                return exe
    return None


def open_app_window(url: str) -> str:
    """Show the page: Edge in app mode (a window of its own, no tabs or address bar) when installed - "edge" - else the
    default browser - "browser". Never waits for Edge: it may hand the window to an Edge already running and exit."""
    edge = find_edge() if os.name == "nt" else None
    if edge is not None:
        try:
            subprocess.Popen([str(edge), f"--app={url}", f"--window-size={WINDOW_SIZE}"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "edge"
        except OSError as e:
            log.warning("Could not start Edge (%s): opening the default browser instead.", e)
    webbrowser.open(url)
    return "browser"


def _launch(target) -> None:
    """Open a program or folder the way a double-click does: in a normal, visible window."""
    if hasattr(os, "startfile"):
        os.startfile(str(target))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(target)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _text(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return data.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------- the machines

class RealMachine:
    """This machine: doctor's checks, and the fixes for real. run(action) -> (ok, message, output)."""

    demo = False

    def __init__(self):
        self._links = {}

    def info(self) -> dict:
        return {"repo": str(doctor.REPO), "pythonExe": sys.executable}

    def checks(self, advance=True) -> list[dict]:
        return doctor.checks()

    def _link(self, kind) -> Path | None:
        """Where a shortcut goes; the shell folder is asked once (through PowerShell, which takes a moment)."""
        if kind not in self._links:
            try:
                self._links[kind] = doctor.desktop_shortcut() if kind == "desktop" else doctor.autostart_shortcut()
            except (OSError, subprocess.SubprocessError):
                self._links[kind] = None
        return self._links[kind]

    def shortcuts(self) -> dict:
        out = {}
        for kind in ("desktop", "autostart"):
            link = self._link(kind)
            out[kind] = bool(link is not None and link.is_file())
        return out

    def run(self, action) -> tuple[bool, str, str]:
        handler = getattr(self, "_" + action.replace("-", "_"))
        try:
            return handler()
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Could not {ACTIONS[action]}: {e}", ""

    # -- pip

    def _pip(self, args, done) -> tuple[bool, str, str]:
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]
        try:
            out = subprocess.run(cmd, cwd=str(doctor.REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=PIP_TIMEOUT, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                 creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            return False, f"pip did not finish within {PIP_TIMEOUT // 60} minutes.", _text(e.output)
        text = _text(out.stdout)
        if out.returncode != 0:
            return False, f"pip failed (exit code {out.returncode}): see its output.", text
        return True, done, text

    def _install_deps(self):
        return self._pip(["-r", str(doctor.REPO / "requirements.txt")], "Installed the Python requirements.")

    def _install_pillow(self):
        return self._pip(["pillow"], "Installed Pillow.")

    def _install_whisper(self):
        return self._pip(["faster-whisper"], "Installed faster-whisper.")

    # -- shortcuts

    def _shortcut(self, kind, minimized):
        link = self._link(kind)
        if link is None:
            folder = "Desktop" if kind == "desktop" else "Startup"
            return False, f"Could not find your {folder} folder.", ""
        return True, "", str(doctor.make_shortcut(link.parent, minimized=minimized))

    def _shortcut_desktop(self):
        ok, message, output = self._shortcut("desktop", False)
        return ok, message or "Created the 'Premiere Agent Bridge' shortcut on the desktop.", output

    def _autostart_on(self):
        ok, message, output = self._shortcut("autostart", True)
        return ok, message or "The bridge now starts (minimized) when you log in to Windows.", output

    def _autostart_off(self):
        if doctor.remove_autostart():
            return True, "The bridge no longer starts when you log in.", ""
        return True, "The bridge was not set to start at login.", ""

    # -- programs and pages

    def _open_udt(self):
        exe = devloader.find_udt()
        if exe is None:
            return False, ("UXP Developer Tool is not installed: install 'UXP Developer Tools' from the Creative Cloud "
                           "app first."), ""
        _launch(exe)  # in a normal window, not devloader.start_udt's minimized one: the user has to click in it
        return True, ("Opened UXP Developer Tool: click 'Enable Developer Mode' (it asks for admin rights), then "
                      "come back here."), ""

    def _open_cc(self):
        exe = find_creative_cloud()
        if exe is not None:
            _launch(exe)
            return True, "Opened the Creative Cloud app: find 'UXP Developer Tools' in its apps and install it.", ""
        webbrowser.open(CC_URL)
        return True, "The Creative Cloud app was not found: opened its page in the browser.", CC_URL

    def _open_ffmpeg(self):
        webbrowser.open(FFMPEG_URL)
        return True, "Opened the FFmpeg download page.", FFMPEG_URL

    def _open_folder(self):
        _launch(doctor.REPO)
        return True, "Opened the bridge folder.", ""

    def _start_bridge(self):
        if doctor.broker_running():
            return False, "The bridge is already running.", ""
        if os.name != "nt":
            return False, "start_bridge.bat runs on Windows only: run broker/broker.py --load-panel instead.", ""
        subprocess.Popen(["cmd", "/c", "start", "Premiere Agent Bridge", str(doctor.START_SCRIPT)],
                         cwd=str(doctor.REPO), creationflags=_NO_WINDOW)
        deadline = time.monotonic() + START_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(START_POLL_SECONDS)
            if doctor.broker_running():
                return True, "The bridge is running: keep its window open while you edit.", ""
        return False, ("Started start_bridge.bat, but the bridge is not answering yet: look at its window for an "
                       "error."), ""


_DEMO_PIP = {
    "install-deps": ("Collecting websockets>=14 (from -r requirements.txt (line 2))\n"
                     "  Downloading websockets-15.0.1-py3-none-any.whl (169 kB)\n"
                     "Installing collected packages: websockets\n"
                     "Successfully installed websockets-15.0.1\n"),
    "install-pillow": ("Collecting pillow\n"
                       "  Downloading pillow-11.0.0-cp312-cp312-win_amd64.whl (2.6 MB)\n"
                       "Installing collected packages: pillow\n"
                       "Successfully installed pillow-11.0.0\n"),
    "install-whisper": ("Collecting faster-whisper\n"
                        "  Downloading faster_whisper-1.1.0-py3-none-any.whl (1.1 MB)\n"
                        "Collecting ctranslate2<5,>=4.0 (from faster-whisper)\n"
                        "  Downloading ctranslate2-4.5.0-cp312-cp312-win_amd64.whl (19.5 MB)\n"
                        "Collecting av>=11 (from faster-whisper)\n"
                        "  Downloading av-14.0.1-cp312-cp312-win_amd64.whl (30.8 MB)\n"
                        "Installing collected packages: av, ctranslate2, faster-whisper\n"
                        "Successfully installed av-14.0.1 ctranslate2-4.5.0 faster-whisper-1.1.0\n"),
}


class DemoMachine:
    """A made-up machine for screenshots and tests: nothing on this one is read or changed. It starts without the
    Python requirements, UXP Developer Tool, developer mode, a running bridge, Pillow and faster-whisper. Each action
    takes step_seconds and fixes what it would fix; open-cc stands for the user installing UXP Developer Tool from
    Creative Cloud, which shows on the next state read (not in the action's own reply)."""

    demo = True

    def __init__(self, step_seconds=DEMO_STEP_SECONDS):
        self.step_seconds = step_seconds
        self._lock = threading.Lock()
        self._pending = []  # (check name, detail) the next state read turns ok
        self._shortcuts = {"desktop": False, "autostart": False}
        v = sys.version_info
        fix = doctor.FIXES
        self._checks = [
            doctor._ok("Python", f"{v.major}.{v.minor}.{v.micro}"),
            doctor._bad("websockets", "not installed", fix["websockets"]),
            doctor._bad("UXP Developer Tool", "not found", fix["UXP Developer Tool"]),
            doctor._bad("UXP developer mode", "off", fix["UXP developer mode"]),
            doctor._ok("Premiere Pro (UXP)", "Adobe Premiere Pro (Beta) 27.1"),
            doctor._ok("Bridge data folder", "%USERPROFILE%\\.ppro-agent-bridge (demo)"),
            doctor._ok("Start script", "start_bridge.bat (demo)"),
            doctor._bad("Broker", "not running", fix["Broker"], required=False),
            doctor._ok("ffmpeg", "ffmpeg.exe (demo)", required=False),
            doctor._ok("ffprobe", "ffprobe.exe (demo)", required=False),
            doctor._bad("PIL", "not installed", fix["PIL"], required=False),
            doctor._bad("faster_whisper", "not installed", fix["faster_whisper"], required=False),
        ]

    def info(self) -> dict:
        return {"repo": "ppro-agent-bridge (demo)", "pythonExe": "python.exe (demo)"}

    def checks(self, advance=True) -> list[dict]:
        with self._lock:
            if advance:
                for name, detail in self._pending:
                    self._fix(name, detail)
                self._pending.clear()
            return [dict(c) for c in self._checks]

    def shortcuts(self) -> dict:
        with self._lock:
            return dict(self._shortcuts)

    def run(self, action) -> tuple[bool, str, str]:
        time.sleep(self.step_seconds)
        with self._lock:
            return getattr(self, "_" + action.replace("-", "_"))()

    def _fix(self, name, detail):
        for i, c in enumerate(self._checks):
            if c["name"] == name:
                self._checks[i] = doctor._ok(name, detail, required=c["required"])

    def _ok(self, name) -> bool:
        return any(c["name"] == name and c["ok"] for c in self._checks)

    def _pip(self, action, check, package, detail, done):
        if self._ok(check):
            return True, done, f"(demo) Requirement already satisfied: {package}\n"
        self._fix(check, detail)
        return True, done, "(demo: nothing was installed)\n" + _DEMO_PIP[action]

    def _install_deps(self):
        return self._pip("install-deps", "websockets", "websockets>=14", "15.0.1", "Installed the Python requirements.")

    def _install_pillow(self):
        return self._pip("install-pillow", "PIL", "pillow", "installed", "Installed Pillow.")

    def _install_whisper(self):
        return self._pip("install-whisper", "faster_whisper", "faster-whisper", "installed",
                         "Installed faster-whisper.")

    def _shortcut_desktop(self):
        self._shortcuts["desktop"] = True
        return True, "Created the 'Premiere Agent Bridge' shortcut on the desktop.", "(demo)"

    def _autostart_on(self):
        self._shortcuts["autostart"] = True
        return True, "The bridge now starts (minimized) when you log in to Windows.", "(demo)"

    def _autostart_off(self):
        was_on, self._shortcuts["autostart"] = self._shortcuts["autostart"], False
        return True, ("The bridge no longer starts when you log in." if was_on else
                      "The bridge was not set to start at login."), ""

    def _open_udt(self):
        if not self._ok("UXP Developer Tool"):
            return False, ("UXP Developer Tool is not installed: install 'UXP Developer Tools' from the Creative Cloud "
                           "app first."), ""
        self._fix("UXP developer mode", "on")
        return True, ("Opened UXP Developer Tool: click 'Enable Developer Mode' (it asks for admin rights), then "
                      "come back here."), "(demo: developer mode is now on)"

    def _open_cc(self):
        if not self._ok("UXP Developer Tool"):
            self._pending.append(("UXP Developer Tool", "Adobe UXP Developer Tools.exe (demo)"))
        return True, "Opened the Creative Cloud app: find 'UXP Developer Tools' in its apps and install it.", ""

    def _open_ffmpeg(self):
        return True, "Opened the FFmpeg download page.", FFMPEG_URL

    def _open_folder(self):
        return True, "Opened the bridge folder.", ""

    def _start_bridge(self):
        if self._ok("Broker"):
            return False, "The bridge is already running.", ""
        self._fix("Broker", f"running on 127.0.0.1:{doctor.BROKER_PORT}")
        return True, "The bridge is running: keep its window open while you edit.", "(demo)"


# ---------------------------------------------------------------- the server

class _Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let this bind a port another program listens on
    allow_reuse_port = False
    daemon_threads = True
    app = None  # the SetupServer

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # not HTTPServer's: that one looks 127.0.0.1 up in DNS
        self.server_name, self.server_port = HOST, self.server_address[1]

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if not isinstance(error, ConnectionError):  # the page went away mid-reply: nothing to say
            log.warning("setup page request failed: %r", error)


class _Handler(BaseHTTPRequestHandler):
    server_version = "BridgeSetup"
    sys_version = ""
    timeout = 60
    route = "?"

    # Quiet: requests are not logged, only 5xx replies and errors (and never the secret path).
    def log_request(self, code="-", size="-"):
        try:
            if int(code) >= 500:
                log.warning("setup page: %s /%s -> %d", self.command, self.route, int(code))
        except (TypeError, ValueError):
            pass

    def log_message(self, fmt, *args):
        if not fmt.startswith("Request timed out"):  # a connection the browser opened ahead and never used
            log.warning("setup page: " + fmt, *args)

    # -- guards

    def _admit(self) -> str | None:
        """The route below /<secret>/ ("" is the page), or None after refusing the request."""
        app = self.server.app
        origin, site = self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")
        if (self.headers.get_all("Host") or []) != [app.host] or (origin is not None and origin != app.origin) \
                or (site is not None and site not in ("same-origin", "none")):
            self._plain(403, "forbidden")
            return None
        path = urllib.parse.urlsplit(self.path).path
        secret, sep, route = path[1:].partition("/") if path.startswith("/") else ("", "", "")
        if not sep or not hmac.compare_digest(secret.encode("utf-8"), app.secret.encode("utf-8")):
            self._plain(404, "not found")
            return None
        app.touch()
        self.route = route
        return route

    def _json_body(self) -> dict | None:
        """The POST body as a JSON object; None when the header or the body is not what the page sends."""
        if self.headers.get("X-Bridge-Setup") != "1" or self.headers.get("Transfer-Encoding"):
            return None
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    # -- replies

    def _send(self, code, body: bytes, content_type, headers=()):
        self.send_response(code)
        for name, value in (("Content-Type", content_type), ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
                            ("Cross-Origin-Resource-Policy", "same-origin"), *headers):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _plain(self, code, text):
        self._send(code, (text + "\n").encode("utf-8"), "text/plain; charset=utf-8")

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _page(self):
        try:
            page = PAGE.read_bytes()
        except OSError:
            self._plain(500, "setup_ui.html is missing next to setup_ui.py: re-extract the bridge.")
            return
        self._send(200, page, "text/html; charset=utf-8", [("Content-Security-Policy", CSP)])

    # -- routes

    def do_GET(self):
        route = self._admit()
        if route is None:
            return
        if route == "":
            self._page()
        elif route == "api/state":
            self._json(200, self.server.app.state())
        elif route in POST_ROUTES:
            self._plain(405, "use POST")
        else:
            self._plain(404, "not found")

    def do_POST(self):
        route = self._admit()
        if route is None:
            return
        if route not in POST_ROUTES:
            self._plain(405 if route in GET_ROUTES else 404, "use GET" if route in GET_ROUTES else "not found")
            return
        body = self._json_body()
        if body is None:
            self._plain(403, "forbidden")
            return
        app = self.server.app
        if route == "api/ping":
            self._json(200, {"ok": True})
        elif route == "api/bye":
            self._json(200, {"ok": True})
            app.bye()
        else:
            self._json(*app.run_action(body.get("action")))


class SetupServer:
    """The setup window's server: .url, .start() (serves in a background thread), .state(), .run_action(name),
    .busy, .wait(timeout) (True once stopped), .shutdown(). Binds 127.0.0.1:port at once (OSError when taken)."""

    def __init__(self, port=0, demo=False, idle_timeout=IDLE_SECONDS, machine=None, bye_grace=BYE_GRACE_SECONDS):
        self.machine = machine or (DemoMachine() if demo else RealMachine())
        self.demo = bool(getattr(self.machine, "demo", demo))
        self.secret = secrets.token_urlsafe(18)
        self.idle_timeout = float(idle_timeout)
        self.bye_grace = float(bye_grace)
        self.httpd = _Server((HOST, port), _Handler)
        self.httpd.app = self
        self.port = self.httpd.server_address[1]
        self.host = f"{HOST}:{self.port}"
        self.origin = f"http://{self.host}"
        self.prefix = f"/{self.secret}/"
        self.url = self.origin + self.prefix
        self._lock = threading.Lock()
        self._busy = None
        self._active = False  # the page has made a request: the idle timer runs from now on
        self._last = time.monotonic()
        self._requests = 0  # admitted requests so far (bye's grace period watches it)
        self._serving = self._closing = False
        self._stopped = threading.Event()

    @property
    def busy(self) -> str | None:
        return self._busy

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def start(self) -> "SetupServer":
        with self._lock:
            if self._serving or self._closing:
                return self
            self._serving = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, name="setup-ui",
                         daemon=True).start()
        threading.Thread(target=self._watch_idle, name="setup-ui-idle", daemon=True).start()
        return self

    def wait(self, timeout=None) -> bool:
        return self._stopped.wait(timeout)

    def shutdown(self) -> None:
        with self._lock:
            closing, self._closing = self._closing, True
        if closing:
            self._stopped.wait(5)
            return
        if self._serving:
            self.httpd.shutdown()
        self.httpd.server_close()
        self._stopped.set()

    def touch(self) -> None:
        self._active, self._last = True, time.monotonic()
        self._requests += 1

    def bye(self) -> None:
        """The page said bye: its window closed, or it is reloading. Stop unless a request comes within bye_grace
        (counted, not timed: Windows' monotonic clock ticks every ~15 ms, so a reload can share bye's tick)."""
        seen = self._requests

        def later():
            if not self._stopped.wait(self.bye_grace) and self._requests == seen and self._busy is None:
                self.shutdown()

        threading.Thread(target=later, name="setup-ui-bye", daemon=True).start()

    def _watch_idle(self):
        tick = max(0.05, min(1.0, self.idle_timeout / 4))
        while not self._stopped.wait(tick):
            if self._active and self._busy is None and time.monotonic() - self._last > self.idle_timeout:
                log.info("No word from the setup page for %g s: stopping.", self.idle_timeout)
                self.shutdown()
                return

    def state(self, advance=True) -> dict:
        checks = self.machine.checks(advance=advance)
        info = self.machine.info()
        return {"version": VERSION, "demo": self.demo, "platform": sys.platform, "repo": info["repo"],
                "python": platform.python_version(), "pythonExe": info["pythonExe"],
                "premiereMin": doctor.version_label(doctor.premiere_min()), "checks": checks,
                "shortcuts": self.machine.shortcuts(),
                "brokerRunning": any(c.get("name") == "Broker" and c.get("ok") for c in checks),
                "busy": self._busy}

    def run_action(self, name) -> tuple[int, dict]:
        """(HTTP status, reply) for one action: 400 unknown, 409 while another runs, else 200 with ok true/false."""
        if not isinstance(name, str) or name not in ACTIONS:
            return 400, {"ok": False, "action": name if isinstance(name, str) else None,
                         "message": f"unknown action {name!r}; known: {', '.join(ACTIONS)}"}
        with self._lock:
            if self._busy is not None:
                return 409, {"ok": False, "message": "busy", "busy": self._busy}
            self._busy = name
        try:
            ok, message, output = self.machine.run(name)
        except Exception as e:  # noqa: BLE001 - the page shows it; the server keeps running
            log.exception("setup action %s failed", name)
            ok, message, output = False, f"Could not {ACTIONS[name]}: {e}", ""
        finally:
            with self._lock:
                self._busy = None
            self.touch()  # the idle timer starts again when the action ends
        return 200, {"ok": bool(ok), "action": name, "message": message,
                     "output": _text(output)[-OUTPUT_LIMIT:], "state": self.state(advance=False)}


def serve(port=0, open_window=True, demo=False, idle_timeout=IDLE_SECONDS, machine=None,
          bye_grace=BYE_GRACE_SECONDS) -> SetupServer:
    """Start the setup server in a background thread and, with open_window, show the window on it. Returns the running
    SetupServer (.url, .wait(), .shutdown()); OSError when the port cannot be bound."""
    server = SetupServer(port, demo=demo, idle_timeout=idle_timeout, machine=machine, bye_grace=bye_grace).start()
    if open_window:
        open_app_window(server.url)
    return server


def _alert(text: str) -> None:
    """Say it where the user can see it: the console, or - under pythonw.exe, which has none - a message box."""
    if sys.stderr is not None:
        print(text, file=sys.stderr)
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Premiere Agent Bridge setup", 0x10)  # MB_ICONERROR
    except (ImportError, AttributeError, OSError):
        pass


def main(argv=None) -> int:
    console.utf8_stdio()
    p = argparse.ArgumentParser(prog="setup_ui", description=__doc__.split("\n\n")[0])
    p.add_argument("--no-browser", action="store_true", help="do not open a window; only print the URL")
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default 0: any free port)")
    p.add_argument("--demo", action="store_true", help="a made-up machine: nothing is read or changed")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        server = serve(args.port, open_window=not args.no_browser, demo=args.demo)
    except OSError as e:
        _alert(f"Cannot start the setup window on 127.0.0.1:{args.port}: {e}\n\n"
               "Run setup_bridge.bat --console for the same checks as text.")
        return 1
    print(f"Premiere Agent Bridge setup{' (demo)' if args.demo else ''}: {server.url}", flush=True)
    print("Close the setup window (or press Ctrl+C here) to stop.", flush=True)
    try:
        while not server.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
