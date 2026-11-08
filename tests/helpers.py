"""Test helpers: a broker + fake panel on a free port, the CLI as a subprocess, and an in-process stand-in for the
panel (the fake panel's dispatch called directly).

Run everything with:  python -m unittest discover -s tests
All data is synthetic; nothing here touches Premiere or the real broker on 8017: importing this module points
PPRO_BRIDGE_URL at a closed local port, so a call that forgets its URL fails with NO_BROKER.
"""

import atexit
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI, BROKER = REPO / "cli", REPO / "broker"
for p in (BROKER, REPO):  # never cli/: tests import the package, and run the shims as subprocesses
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The CLI hands Premiere resolved paths. Where TEMP is written another way (GitHub's runner has an 8.3 short name; a
# junction) a test comparing them with the folder it made needs the resolved form from the start.
tempfile.tempdir = os.path.realpath(tempfile.gettempdir())
os.environ["TEMP"] = os.environ["TMP"] = tempfile.tempdir

DEAD_URL = f"ws://127.0.0.1:{free_port()}"
os.environ["PPRO_BRIDGE_URL"] = DEAD_URL

# The pairing token and the audit log live in a throwaway folder: no test reads or creates the user's real ones
# (brokers, fake panels and CLI subprocesses inherit these through env()).
STATE = Path(tempfile.mkdtemp(prefix="ppro_test_state_"))
atexit.register(shutil.rmtree, STATE, True)
os.environ["PPRO_BRIDGE_TOKEN_FILE"] = str(STATE / "token")
os.environ["PPRO_AUDIT_DIR"] = str(STATE / "audit")
os.environ.pop("PPRO_BRIDGE_TOKEN", None)
# No test asks GitHub: brokers start with the update check off, and settings.json / update.json live in the
# throwaway folder too (tests/test_update.py turns the check on against a local server).
os.environ["PPRO_UPDATE_CHECK"] = "0"
os.environ["PPRO_SETTINGS_DIR"] = str(STATE / "settings")

# QA-4: and no test in this process touches the user's real folder at all - not even a mkdir: every open, mkdir,
# listdir, scandir, remove, rename or rmtree of it or of anything inside it fails the call (an audit hook stays for
# the whole run; subprocesses get the folders above through env()).
REAL_DATA_DIR = os.path.normcase(os.path.abspath(os.path.join(os.path.expanduser("~"), ".ppro-agent-bridge")))
_GUARDED_EVENTS = {"open", "os.mkdir", "os.listdir", "os.scandir", "os.remove", "os.rename", "os.rmdir",
                   "shutil.rmtree", "shutil.copyfile", "shutil.move", "os.truncate", "os.utime", "os.chmod"}


def _under_real_data(value) -> bool:
    if isinstance(value, int) or value is None:
        return False
    try:
        p = os.path.normcase(os.path.abspath(os.fsdecode(value)))
    except (TypeError, ValueError):
        return False
    return p == REAL_DATA_DIR or p.startswith(REAL_DATA_DIR + os.sep)


def _guard_real_data(event, args):
    if event in _GUARDED_EVENTS and any(_under_real_data(a) for a in args[:2]):
        raise PermissionError(f"a test reached the real {REAL_DATA_DIR} ({event}): point it at a temp folder")


sys.addaudithook(_guard_real_data)

from ppro_bridge import auth, spec  # noqa: E402
from ppro_bridge.errors import BridgeError  # noqa: E402

auth.ensure_token()  # the test token, in STATE: stand-in brokers and the CLI pair with it from the start


def controller_hello() -> str:
    """The role message a raw-WebSocket test sends: a controller with the test brokers' token (the 0.5 / 0.6 hello,
    which the broker still takes from controllers; the CLI pairs instead, see controller_pair)."""
    return json.dumps({"role": "controller", "token": auth.read_token()})


PANEL_PLUGIN_ID = "org.pproagentbridge.panel"
PANEL_VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()


def panel_hello(**extra) -> dict:
    """A paired panel's hello (auth.py "Pairing"): role, plugin id, version, scheme and a fresh nonce."""
    return dict({"role": "panel", "plugin": PANEL_PLUGIN_ID, "version": PANEL_VERSION, "auth": auth.SCHEME,
                 "nonce": auth.new_nonce()}, **extra)


async def panel_pair(ws, hello=None, token=None, timeout=10) -> dict:
    """Play the panel's side of the hello on a websockets connection: send `hello` (default panel_hello()), answer
    the challenge with the proof of `token` (default the test token) and return the broker's next notice
    ("registered" with its proof checked: AssertionError if it does not match; or "refused"). A notice that comes
    instead of the challenge (refused at once) is returned as it is."""
    import asyncio
    hello = panel_hello() if hello is None else hello
    token = auth.read_token() if token is None else token
    await ws.send(json.dumps(hello))
    first = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    if first.get("event") != "challenge":
        return first
    await ws.send(json.dumps({"proof": auth.proof(token, "client", "panel", hello["nonce"], first["nonce"])}))
    notice = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    if notice.get("event") == "registered":
        assert auth.proof_matches(notice.get("proof"), token, "broker", "panel", hello["nonce"], first["nonce"]), \
            f"the broker's proof does not match: {notice}"
    return notice


async def broker_pair(ws, token=None, timeout=10) -> dict:
    """Play a 0.7 broker's side of a controller's hello (stand-in brokers in tests): receive the hello, send the
    challenge, check the proof (AssertionError if wrong) and send "authenticated" with the broker's proof. Returns
    the hello."""
    import asyncio
    token = auth.read_token() if token is None else token
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    nonce = auth.new_nonce()
    await ws.send(json.dumps({"event": "challenge", "auth": auth.SCHEME, "nonce": nonce, "broker": "9.9.9"}))
    answer = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    assert auth.proof_matches(answer.get("proof"), token, "client", "controller", hello.get("nonce"), nonce), answer
    await ws.send(json.dumps({"event": "authenticated", "broker": "9.9.9",
                              "proof": auth.proof(token, "broker", "controller", hello["nonce"], nonce)}))
    return hello


def env(**extra) -> dict:
    e = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    e.update({k: str(v) for k, v in extra.items()})
    return e


class Bridge:
    """A broker and (optionally) the fake panel on their own port; stop() kills both."""

    def __init__(self, panel=True, broker_env=None, panel_env=None):
        self.port = free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self.procs, self.readers = [], []
        self.procs.append(subprocess.Popen([sys.executable, str(BROKER / "broker.py")],
                                           env=env(PPRO_BRIDGE_PORT=self.port, **(broker_env or {})),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self._wait_port()
        if panel:
            fake = subprocess.Popen([sys.executable, str(BROKER / "fake_panel.py")],
                                    env=env(PPRO_BRIDGE_URL=self.url, **(panel_env or {})), stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
            self.procs.append(fake)
            self._wait_registered(fake)

    def _wait_registered(self, fake):
        """Until the broker confirms the fake panel ({"event": "registered"}, which the fake prints): a test that
        started earlier would get NO_PANEL, or register a panel of its own first and see the fake refused (4409)."""
        registered = threading.Event()

        def drain():  # read every line, or the fake blocks on a full pipe
            for line in fake.stdout:
                if line.startswith("registered by the broker"):
                    registered.set()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        self.readers.append(reader)
        if not registered.wait(15):
            self.stop()
            raise RuntimeError("the fake panel did not register with the broker within 15 s")

    def _wait_port(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.1)
        raise RuntimeError("broker did not start")

    def cli(self, *args, timeout=60):
        """Run ppro.py; returns (exit code, parsed JSON, raw stdout)."""
        return run_cli(*args, url=self.url, timeout=timeout)

    def stop(self):
        for p in reversed(self.procs):
            p.kill()
            p.wait()
        for reader in self.readers:  # the fake's stdout ends with the process
            reader.join(5)
        for p in self.procs:
            if p.stdout is not None:
                p.stdout.close()


def run_cli(*args, url, timeout=60):
    out = subprocess.run([sys.executable, str(CLI / "ppro.py"), *map(str, args)], env=env(PPRO_BRIDGE_URL=url),
                         capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return out.returncode, json.loads(out.stdout), out.stdout


def fake_rpc(*, guard=None, spec_check=True):
    """A fresh fake panel (new in-memory project) called in-process through its dispatch, plus a log of the calls.
    Every request is checked against actions.json first (a problem is a test failure, not a BAD_PARAMS reply)."""
    import fake_panel
    fake = importlib.reload(fake_panel)
    calls = []

    def rpc(action, params=None):
        params = dict(params or {})
        calls.append((action, dict(params)))
        if spec_check:
            problems = spec.check_params(action, params)
            if problems:
                raise AssertionError(f"{action}: request breaks actions.json: {problems}")
        msg = {"id": str(uuid.uuid4()), "action": action, "params": params}
        if guard is not None:
            msg["guard"] = dict(guard)
        reply = fake.dispatch(msg)
        if not reply.get("ok"):
            raise BridgeError(f"{action}: {reply.get('error')}", reply.get("code"), reply.get("outcome"),
                              data=reply.get("data"))
        data = reply.get("data") or {}
        if action == "ping":
            data["brokerVersion"] = fake.VERSION
        return data

    return rpc, fake, calls


def stub_rpc(handlers: dict, calls: list | None = None):
    """A tiny panel: {action: dict or callable(params)}; any other action fails the test. Requests are checked
    against actions.json like fake_rpc does."""
    log = [] if calls is None else calls

    def rpc(action, params=None):
        params = dict(params or {})
        log.append((action, params))
        if action not in handlers:
            raise AssertionError(f"unexpected call {action} {params}")
        problems = spec.check_params(action, params)
        if problems:
            raise AssertionError(f"{action}: request breaks actions.json: {problems}")
        h = handlers[action]
        result = h(params) if callable(h) else h
        return json.loads(json.dumps(result))  # a fresh copy, like a reply off the wire

    rpc.calls = log
    return rpc


def ping_reply(protocol=2, actions=None, **extra) -> dict:
    """The ping data of a current panel (every spec action) unless told otherwise."""
    from ppro_bridge._version import VERSION
    data = {"pong": True, "plugin": f"test-panel {VERSION}", "version": VERSION, "protocol": protocol,
            "actions": spec.action_names() if actions is None else actions, "stuck": False, "denied": False,
            "capabilities": {}, "brokerVersion": VERSION, "build": {"dev": True, "packaged": False}}
    data.update(extra)
    return data
