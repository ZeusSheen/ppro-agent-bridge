#!/usr/bin/env python3
"""
ppro — the Agent Bridge CLI (0.3.3 entry point; the code lives in the ppro_bridge package).

  python ppro.py <command> …          (from cli/)
  python cli/ppro.py <command> …      (from the repo root; same as: python -m ppro_bridge <command> …)

Kept for scripts written against 0.3.3: the module-level names below, the cmd_* functions, and the wrappers
resolve_item, check_expect_seq, require_panel and export_one_frame with their 0.3.3 signatures. Everything here
looks this module's names up at call time, as 0.3.3 did: `ppro.rpc = stub`, `ppro.call = stub`,
`ppro.BROKER_URL = "ws://…"` and `ppro.probe_media = stub` all redirect the wrappers, the cmd_* functions and main.
"""

import inspect
import os
import sys
import warnings

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# This checkout's package must win over any other ppro_bridge on sys.path: right after the script's folder.
sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.curdir)) != os.path.normcase(_REPO)]
sys.path.insert(1, _REPO)

import ppro_bridge as _pkg  # noqa: E402
from ppro_bridge import cli as _cli  # noqa: E402
from ppro_bridge import frames as _frames  # noqa: E402
from ppro_bridge import items as _items  # noqa: E402
from ppro_bridge import ops as _ops  # noqa: E402
from ppro_bridge import spec as _spec  # noqa: E402
from ppro_bridge import transport as _transport  # noqa: E402
from ppro_bridge._version import VERSION as CLI_VERSION  # noqa: E402
from ppro_bridge.cli import MARKER_COLORS  # noqa: E402
from ppro_bridge.errors import BridgeError  # noqa: E402
from ppro_bridge.frames import make_contact_sheet  # noqa: E402
from ppro_bridge.items import item_ref, parse_track  # noqa: E402
from ppro_bridge.media import fit_scale, probe_media  # noqa: E402
from ppro_bridge.transport import QUEUE_TIMEOUT, lost  # noqa: E402

if os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(_pkg.__file__)))) != os.path.normcase(_REPO):
    warnings.warn(f"ppro_bridge was already imported from {_pkg.__file__}, not from this checkout ({_REPO}).")

BROKER_URL = _transport.broker_url()  # 0.3.3 name: assign it to point this module at another broker
_IMPORT_URL = BROKER_URL
DEFAULT_TIMEOUT = _spec.load()["defaultTimeoutSeconds"]
ACTION_TIMEOUTS = {n: _spec.timeout(n) for n in _spec.action_names() if _spec.timeout(n) != DEFAULT_TIMEOUT}

__all__ = ["CLI_VERSION", "BridgeError", "BROKER_URL", "QUEUE_TIMEOUT", "DEFAULT_TIMEOUT", "ACTION_TIMEOUTS",
           "call", "lost", "rpc", "item_ref", "parse_track", "probe_media", "fit_scale", "resolve_exported_frame",
           "make_contact_sheet", "MARKER_COLORS", "main", "resolve_item", "check_expect_seq", "require_panel",
           "export_one_frame"]


def _g(name):
    return globals()[name]


async def call(action, params, **kwargs):
    """0.3.3: one request, the raw reply envelope. Uses ppro.BROKER_URL when a script changed it."""
    url = _g("BROKER_URL")
    if "url" not in kwargs and url != _IMPORT_URL:
        kwargs["url"] = url
    return await _transport.call(action, params, **kwargs)


def rpc(action, params=None):
    """0.3.3: the reply's data, or BridgeError. Goes through this module's `call` (so ppro.call / BROKER_URL apply)."""
    import asyncio
    return _transport._data(action, asyncio.run(_g("call")(action, params or {})))


def resolve_item(ref, items=None, unique=False):
    return _items.resolve_item(ref, items, unique, rpc=_g("rpc"))


def check_expect_seq(expected):
    _items.check_expect_seq(_g("rpc"), expected)


def require_panel(actions):
    return _items.require_panel(_g("rpc"), actions)


def resolve_exported_frame(reply):
    """0.3.3 form: takes and returns the whole reply envelope."""
    if isinstance(reply.get("data"), dict):
        reply["data"] = _frames.resolve_exported_frame(reply["data"])
    return reply


def export_one_frame(directory, at, name, width=None, height=None):
    """0.3.3 form: returns a reply envelope ({"ok": true, "data": …} or the failure object)."""
    try:
        return {"ok": True, "data": _frames.export_one_frame(_g("rpc"), directory, at, name, width, height)}
    except BridgeError as e:
        return e.to_reply()


def _cmd(name):
    """A 0.3.3 cmd_<name>(args) function: argparse Namespace in, data out (BridgeError on failure)."""
    fn = getattr(_ops, name)
    accepted = set(inspect.signature(fn).parameters)

    def cmd(args):
        kwargs = {k: v for k, v in vars(args).items() if k in accepted and k != "rpc"}
        if "probe_media" in accepted:
            kwargs["probe_media"] = _g("probe_media")
        return fn(_g("rpc"), **kwargs)

    cmd.__name__ = cmd.__qualname__ = f"cmd_{name}"
    cmd.__doc__ = f"0.3.3 entry point: ppro_bridge.ops.{name} with this module's rpc."
    return cmd


for _name in ("ping", "insert_clip", "set_fps", "list_items", "inspect_items", "export_frames", "set_scale",
              "add_marker", "remove_items", "replace_clip", "apply_plan"):
    globals()[f"cmd_{_name}"] = _cmd(_name)
del _name


def main(argv=None):
    return _cli.main(argv, rpc=_g("rpc"), probe_media=_g("probe_media"))


if __name__ == "__main__":
    sys.exit(main())
