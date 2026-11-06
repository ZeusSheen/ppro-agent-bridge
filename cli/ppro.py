#!/usr/bin/env python3
"""
ppro — the Agent Bridge CLI (0.3.3 entry point; the code lives in the ppro_bridge package).

  python ppro.py <command> …          (from cli/)
  python cli/ppro.py <command> …      (from the repo root; same as: python -m ppro_bridge <command> …)

Kept for scripts written against 0.3.3: the module-level names below, the cmd_* functions, and the wrappers
resolve_item, check_expect_seq, require_panel and export_one_frame with their 0.3.3 signatures. Everything here
looks this module's names up at call time, as 0.3.3 did: `ppro.rpc = stub`, `ppro.call = stub`,
`ppro.BROKER_URL = "ws://…"` and `ppro.probe_media = stub` all redirect the wrappers, the cmd_* functions and main.
Names that need a heavier module (item_ref, parse_track, probe_media, fit_scale, make_contact_sheet) are imported on
first use, so a plain CLI call (`ping`) loads only what it needs.
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

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# This checkout's package must win over any other ppro_bridge on sys.path: first when run as a script (its own folder
# dropped above), else right after the importer's folder.
sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.curdir)) != os.path.normcase(_REPO)]
sys.path.insert(0 if __name__ == "__main__" else 1, _REPO)

import warnings  # noqa: E402

import ppro_bridge as _pkg  # noqa: E402
from ppro_bridge import cli as _cli  # noqa: E402
from ppro_bridge import spec as _spec  # noqa: E402
from ppro_bridge import transport as _transport  # noqa: E402
from ppro_bridge._version import VERSION as CLI_VERSION  # noqa: E402
from ppro_bridge.cli import MARKER_COLORS  # noqa: E402
from ppro_bridge.errors import BridgeError  # noqa: E402
from ppro_bridge.transport import QUEUE_TIMEOUT, lost  # noqa: E402

_LAZY = {"item_ref": "items", "parse_track": "items", "probe_media": "media", "fit_scale": "media",
         "make_contact_sheet": "frames"}  # 0.3.3 names -> the ppro_bridge module that has them

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


def _module(name):
    __import__(f"ppro_bridge.{name}")
    return sys.modules[f"ppro_bridge.{name}"]


def __getattr__(name):
    """The _LAZY names, imported on first use and then kept as plain module attributes (so `ppro.probe_media = stub`
    still replaces them)."""
    if name in _LAZY:
        value = globals()[name] = getattr(_module(_LAZY[name]), name)
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _g(name):
    g = globals()
    return g[name] if name in g else __getattr__(name)


async def call(action, params, **kwargs):
    """0.3.3: one request, the raw reply envelope. Uses ppro.BROKER_URL when a script changed it."""
    url = _g("BROKER_URL")
    if "url" not in kwargs and url != _IMPORT_URL:
        kwargs["url"] = url
    return await _transport.call(action, params, **kwargs)


_CALL = call


def rpc(action, params=None):
    """0.3.3: the reply's data, or BridgeError. Goes through this module's `call` (so ppro.call / BROKER_URL apply);
    while a script has not replaced `call`, the same request goes through transport.call_sync (no asyncio)."""
    if _g("call") is _CALL:
        url = _g("BROKER_URL")
        return _transport._data(action, _transport.call_sync(action, params or {},
                                                             url=url if url != _IMPORT_URL else None))
    import asyncio
    return _transport._data(action, asyncio.run(_g("call")(action, params or {})))


def _probe_media(path):
    """ppro.probe_media, looked up when called: main passes this, so a command that probes nothing never imports
    ppro_bridge.media."""
    return _g("probe_media")(path)


def resolve_item(ref, items=None, unique=False):
    return _module("items").resolve_item(ref, items, unique, rpc=_g("rpc"))


def check_expect_seq(expected):
    _module("items").check_expect_seq(_g("rpc"), expected)


def require_panel(actions):
    return _module("items").require_panel(_g("rpc"), actions)


def resolve_exported_frame(reply):
    """0.3.3 form: takes and returns the whole reply envelope."""
    if isinstance(reply.get("data"), dict):
        reply["data"] = _module("frames").resolve_exported_frame(reply["data"])
    return reply


def export_one_frame(directory, at, name, width=None, height=None):
    """0.3.3 form: returns a reply envelope ({"ok": true, "data": …} or the failure object)."""
    frames = _module("frames")
    try:
        if at is not None:  # past the end Premiere would hand back the 0 s frame (ops.export_frame refuses it too)
            frames.check_times_inside(_g("rpc"), [at])
        return {"ok": True, "data": frames.export_one_frame(_g("rpc"), directory, at, name, width, height)}
    except BridgeError as e:
        return e.to_reply()


def _cmd(name):
    """A 0.3.3 cmd_<name>(args) function: argparse Namespace in, data out (BridgeError on failure)."""

    def cmd(args):
        import inspect
        fn = getattr(_module("ops"), name)
        accepted = set(inspect.signature(fn).parameters)
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
    probe = _g("probe_media") if "probe_media" in globals() else _probe_media  # not imported yet: when called
    return _cli.main(argv, rpc=_g("rpc"), probe_media=probe)


if __name__ == "__main__":
    sys.exit(main())
