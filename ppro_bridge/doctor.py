"""Check (and help set up) everything the bridge needs on this machine; the setup window (setup_ui) shows the same.

  python -m ppro_bridge.doctor                     report only (exit 1 when something required is missing)
  python -m ppro_bridge.doctor --interactive       report, then offer each fix (y/n)
  python -m ppro_bridge.doctor --install-deps --shortcut --autostart     fixes without asking

Required: Python 3.10+, websockets 14+, UXP Developer Tool (Premiere only loads development plugins through it),
UXP developer mode, a Premiere Pro the panel runs in (the minVersion in plugin/manifest.json: 26.3+). Optional:
ffmpeg/ffprobe (scaling, listening report), Pillow (contact sheets), faster-whisper (local transcription). Fixes: pip
install the requirements; a desktop shortcut to start_bridge.bat; start it at login (a shortcut in the Startup
folder). Developer mode and UXP Developer Tool are turned on / installed by the user (Creative Cloud, admin rights):
the report says how. Installed packages are read from disk on every check, so a check after a pip install in the
same process (the setup window) sees the new versions.
"""

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import auth, devloader

REPO = Path(__file__).resolve().parent.parent
START_SCRIPT = REPO / "start_bridge.bat"
MANIFEST = REPO / "plugin" / "manifest.json"
SHORTCUT_NAME = "Premiere Agent Bridge.lnk"
BROKER_PORT = 8017
PREMIERE_MIN_FALLBACK = (26, 3)

# How to fix each check (the setup window's demo machine shows the same texts).
FIXES = {
    "websockets": "--install-deps",
    "UXP Developer Tool": "install 'UXP Developer Tools' from the Creative Cloud app (or set PPRO_UDT_EXE to its .exe)",
    "UXP developer mode": "open UXP Developer Tool and click 'Enable Developer Mode' (asks for admin rights)",
    "Broker": "double-click start_bridge.bat",
    "ffmpeg": "install FFmpeg and add it to PATH (scaling, listening report)",
    "ffprobe": "install FFmpeg and add it to PATH (scaling, listening report)",
    "PIL": "contact sheets (pip install pillow)",
    "faster_whisper": "local transcription (pip install faster-whisper)",
}


def _ok(name, detail="", required=True):
    return {"name": name, "ok": True, "required": required, "detail": detail}


def _bad(name, detail, fix="", required=True):
    return {"name": name, "ok": False, "required": required, "detail": detail, "fix": fix}


def premiere_installs() -> list[str]:
    """Display names (with versions) of installed Premiere Pro builds, from the uninstall entries."""
    found = []
    try:
        import winreg
    except ImportError:
        return found
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(root, sub) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as app:
                                name = str(winreg.QueryValueEx(app, "DisplayName")[0])
                                if "Premiere Pro" in name:
                                    try:
                                        version = str(winreg.QueryValueEx(app, "DisplayVersion")[0])
                                    except OSError:
                                        version = "?"
                                    found.append(f"{name} {version}")
                        except OSError:
                            continue
            except OSError:
                continue
    return sorted(set(found))


def _version_tuple(text) -> tuple:
    out = []
    for part in str(text).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def premiere_min(manifest=None) -> tuple:
    """The oldest Premiere Pro the panel runs in: the premierepro host's minVersion in plugin/manifest.json (`host` is
    one object or a list of them), trailing zeros dropped so that "26.3.0" is (26, 3) and an install "26.3" passes.
    Falls back to PREMIERE_MIN_FALLBACK when the manifest is missing or names no Premiere version."""
    try:
        data = json.loads(Path(manifest or MANIFEST).read_text(encoding="utf-8-sig"))
        hosts = data.get("host")
        for host in hosts if isinstance(hosts, list) else [hosts]:
            if isinstance(host, dict) and str(host.get("app", "")).lower() == "premierepro" and host.get("minVersion"):
                version = _version_tuple(host["minVersion"])
                while len(version) > 1 and version[-1] == 0:
                    version = version[:-1]
                if version[0] > 0:
                    return version
    except (OSError, ValueError, AttributeError):
        pass
    return PREMIERE_MIN_FALLBACK


def version_label(version) -> str:
    """(26, 3) -> "26.3", (27,) -> "27.0", (26, 3, 1) -> "26.3.1"."""
    parts = tuple(version) + (0,) * max(0, 2 - len(tuple(version)))
    return ".".join(str(p) for p in parts)


def _installed_version(dist: str, module: str) -> str | None:
    """The installed version of a package as it is on disk now (a pip install since this process imported it shows);
    None when it is not installed."""
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        pass
    if importlib.util.find_spec(module) is None:
        return None
    try:
        return str(getattr(importlib.import_module(module), "__version__", "0"))
    except ImportError:
        return None


def broker_running(port=BROKER_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def checks() -> list[dict]:
    importlib.invalidate_caches()  # packages pip installed since the last check
    out = []
    v = sys.version_info
    out.append(_ok("Python", f"{v.major}.{v.minor}.{v.micro}") if v >= (3, 10) else
               _bad("Python", f"{v.major}.{v.minor} is too old", "install Python 3.10 or newer from python.org"))
    ws_version = _installed_version("websockets", "websockets")
    if ws_version is None:
        out.append(_bad("websockets", "not installed", FIXES["websockets"]))
    else:
        out.append(_ok("websockets", ws_version) if _version_tuple(ws_version) >= (14,) else
                   _bad("websockets", f"{ws_version} is too old", FIXES["websockets"]))
    udt = devloader.find_udt()
    out.append(_ok("UXP Developer Tool", str(udt)) if udt else
               _bad("UXP Developer Tool", "not found", FIXES["UXP Developer Tool"]))
    mode = devloader.developer_mode()
    out.append(_ok("UXP developer mode", "on") if mode else
               _bad("UXP developer mode", "off" if mode is False else "unreadable", FIXES["UXP developer mode"]))
    installs = premiere_installs()
    minimum = premiere_min()
    with_uxp = [i for i in installs if _version_tuple(i.rsplit(" ", 1)[-1]) >= minimum]
    out.append(_ok("Premiere Pro (UXP)", "; ".join(with_uxp)) if with_uxp else
               _bad("Premiere Pro (UXP)", "; ".join(installs) or "not found",
                    f"Premiere Pro {version_label(minimum)} or newer (or the Beta)"))
    try:
        folder = auth.data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        out.append(_ok("Bridge data folder", str(folder)))
    except OSError as e:
        out.append(_bad("Bridge data folder", str(e), "check the permissions of your user folder (%USERPROFILE%)"))
    out.append(_ok("Start script", str(START_SCRIPT)) if START_SCRIPT.is_file() else
               _bad("Start script", f"{START_SCRIPT} is missing", "re-extract the bridge"))
    out.append(_ok("Broker", f"running on 127.0.0.1:{BROKER_PORT}", required=False) if broker_running() else
               _bad("Broker", "not running", FIXES["Broker"], required=False))
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        out.append(_ok(tool, path, required=False) if path else
                   _bad(tool, "not on PATH", FIXES[tool], required=False))
    for module in ("PIL", "faster_whisper"):
        out.append(_ok(module, "installed", required=False) if importlib.util.find_spec(module) else
                   _bad(module, "not installed", FIXES[module], required=False))
    return out


def report(results) -> str:
    lines = []
    for r in results:
        mark = "OK  " if r["ok"] else ("MISS" if r["required"] else "opt ")
        line = f"  [{mark}] {r['name']}: {r['detail']}"
        if not r["ok"] and r.get("fix"):
            line += f"\n         -> {r['fix']}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------- fixes

def install_deps() -> int:
    return subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(REPO / "requirements.txt")])


def _special_folder(name: str) -> Path:
    """A Windows shell folder ("Desktop", "Startup") through PowerShell (honours redirected folders)."""
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          f"[Environment]::GetFolderPath('{name}')"], capture_output=True, text=True, timeout=30)
    path = out.stdout.strip()
    if out.returncode != 0 or not path:
        raise OSError(f"cannot find the {name} folder")
    return Path(path)


def make_shortcut(folder: Path, minimized=False) -> Path:
    """A .lnk to start_bridge.bat in `folder` (WScript.Shell through PowerShell)."""
    link = Path(folder) / SHORTCUT_NAME
    style = 7 if minimized else 1
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); "
              f"$s.TargetPath = '{START_SCRIPT}'; $s.WorkingDirectory = '{START_SCRIPT.parent}'; "
              f"$s.WindowStyle = {style}; $s.Description = 'Start the Premiere Agent Bridge'; $s.Save()")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0 or not link.is_file():
        raise OSError(f"could not create {link}: {out.stderr.strip()}")
    return link


def desktop_shortcut() -> Path:
    """Where the desktop shortcut is (or would be) - it may not exist."""
    return _special_folder("Desktop") / SHORTCUT_NAME


def autostart_shortcut() -> Path:
    """Where the start-at-login shortcut is (or would be): the Startup folder - it may not exist."""
    return _special_folder("Startup") / SHORTCUT_NAME


def remove_autostart() -> bool:
    """Stop starting the bridge at login: delete the Startup-folder shortcut. False when there was none."""
    try:
        autostart_shortcut().unlink()
    except FileNotFoundError:
        return False
    return True


def _ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes", "是", "好")
    except EOFError:
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="doctor", description=__doc__.split("\n\n")[0])
    p.add_argument("--interactive", action="store_true", help="offer each fix (y/n)")
    p.add_argument("--install-deps", action="store_true", help="pip install -r requirements.txt")
    p.add_argument("--shortcut", action="store_true", help="put a 'Premiere Agent Bridge' shortcut on the desktop")
    p.add_argument("--autostart", action="store_true", help="start the bridge (minimized) at Windows login")
    args = p.parse_args(argv)

    results = checks()
    print("Premiere Agent Bridge - setup check\n" + report(results))
    missing_deps = any(r["name"] == "websockets" and not r["ok"] for r in results)
    if args.install_deps or (args.interactive and missing_deps and _ask("\nInstall the Python requirements now?")):
        install_deps()
    if args.shortcut or (args.interactive and _ask("\nPut a 'Premiere Agent Bridge' shortcut on the desktop?")):
        try:
            print(f"Created {make_shortcut(_special_folder('Desktop'))}")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"Shortcut failed: {e}")
    if args.autostart or (args.interactive and _ask("Start the bridge automatically when you log in to Windows?")):
        try:
            print(f"Created {make_shortcut(_special_folder('Startup'), minimized=True)}")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"Autostart failed: {e}")
    if args.install_deps or args.interactive:
        results = checks()
        print("\nAfter the fixes:\n" + report(results))
    missing = [r["name"] for r in results if r["required"] and not r["ok"]]
    print("\nNext: double-click start_bridge.bat, open Premiere - the panel loads by itself - then "
          "`python cli\\ppro.py ping`." if not missing else f"\nStill missing: {', '.join(missing)}.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
