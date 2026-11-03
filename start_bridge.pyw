"""Start the Premiere Agent Bridge for the panel's Start bridge button when UXP will not open start_bridge.bat.

The panel opens files with UXP's shell.openPath, which may refuse a .bat; Windows runs a .pyw with pythonw (no
console of its own). This script opens start_bridge.bat (next to it) in a console window titled "Premiere Agent
Bridge", exactly as the setup window's Start button does: the same broker window users keep open while they edit,
and the same audit log. When the broker already answers on its port it starts nothing. pythonw shows no output, so
a failure is shown in a message box. Standard library only (any Python 3 that .pyw files open with).
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

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
        subprocess.Popen(["cmd", "/c", "start", TITLE, str(START_SCRIPT)], cwd=str(HERE), creationflags=NO_WINDOW)
    except OSError as e:
        tell(f"Could not start {START_SCRIPT}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
