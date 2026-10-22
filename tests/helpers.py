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


DEAD_URL = f"ws://127.0.0.1:{free_port()}"
os.environ["PPRO_BRIDGE_URL"] = DEAD_URL

# The pairing token and the audit log live in a throwaway folder: no test reads or creates the user's real ones
# (brokers, fake panels and CLI subprocesses inherit these through env()).
STATE = Path(tempfile.mkdtemp(prefix="ppro_test_state_"))
atexit.register(shutil.rmtree, STATE, True)
os.environ["PPRO_BRIDGE_TOKEN_FILE"] = str(STATE / "token")
os.environ["PPRO_AUDIT_DIR"] = str(STATE / "audit")
os.environ.pop("PPRO_BRIDGE_TOKEN", None)

from ppro_bridge import auth, spec  # noqa: E402
from ppro_bridge.errors import BridgeError  # noqa: E402


def controller_hello() -> str:
    """The role message a raw-WebSocket test sends: a controller with the test brokers' token."""
    return json.dumps({"role": "controller", "token": auth.read_token()})


def env(**extra) -> dict:
    e = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    e.update({k: str(v) for k, v in extra.items()})
    return e


class Bridge:
    """A broker and (optionally) the fake panel on their own port; stop() kills both."""

    def __init__(self, panel=True, broker_env=None, panel_env=None):
        self.port = free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self.procs = []
        self.procs.append(subprocess.Popen([sys.executable, str(BROKER / "broker.py")],
                                           env=env(PPRO_BRIDGE_PORT=self.port, **(broker_env or {})),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self._wait_port()
        if panel:
            self.procs.append(subprocess.Popen([sys.executable, str(BROKER / "fake_panel.py")],
                                               env=env(PPRO_BRIDGE_URL=self.url, **(panel_env or {})),
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(0.8)

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
            "capabilities": {}, "brokerVersion": VERSION}
    data.update(extra)
    return data
