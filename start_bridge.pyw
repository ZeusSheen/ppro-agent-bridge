"""Start the Premiere Agent Bridge for the panel's Start bridge button when UXP will not open start_bridge.bat.

The panel opens files with UXP's shell.openPath, which may refuse a .bat; Windows runs a .pyw with pythonw (no
console of its own). This script opens start_bridge.bat (next to it) in a console window titled "Premiere Agent
Bridge", exactly as the setup window's Start button does: the same broker window users keep open while they edit,
and the same audit log. When the broker already answers on its port it starts nothing. pythonw shows no output, so
a failure is shown in a message box. Standard library only (any Python 3 that .pyw files open with).

Which Python this script runs under is Windows' choice (the .pyw file association); start_bridge.bat then picks the
broker's own: the installer's bundled python/python.exe when it is there, else the one on PATH. An installed bridge on
a machine without a Python of its own has no .pyw association, so this fallback is for the zip and git copies.
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

import socket  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
START_SCRIPT = HERE / "start_bridge.bat"
TITLE = "Premiere Agent Bridge"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def broker_port() -> int:
    try:
        return int(os.environ.get("PPRO_BRIDGE_PORT", "8017"))
    except ValueError:
        return 8017


def broker_running(port=None) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port or broker_port()), timeout=1):
            return True
    except OSError:
        return False


def tell(text):
    """A message box (pythonw has no console); stderr elsewhere."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)  # MB_ICONERROR
    except (ImportError, AttributeError, OSError):
        print(text, file=sys.stderr)


def main() -> int:
    if broker_running():
        return 0
    if os.name != "nt":
        tell("start_bridge.bat runs on Windows only: run broker/broker.py --load-panel instead.")
        return 1
    if not START_SCRIPT.is_file():
        tell(f"{START_SCRIPT} is missing: the bridge folder is incomplete.")
        return 1
    try:
        # As setup_ui.start_command: the path quoted and kept quoted (/s), cmd.exe by its full path, a new console
        # (start_bridge.bat titles it): `start` would hand the .bat to cmd /K, which splits a path holding & or ^.
        exe = os.path.join(os.environ.get("SystemRoot") or os.path.join("C:" + os.sep, "Windows"), "System32",
                           "cmd.exe")
        subprocess.Popen(f'cmd /d /s /c ""{START_SCRIPT}""', executable=exe, cwd=str(HERE),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    except OSError as e:
        tell(f"Could not start {START_SCRIPT}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
