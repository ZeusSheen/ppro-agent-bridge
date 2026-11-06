"""Check (and help set up) everything the bridge needs on this machine; the setup window (setup_ui) shows the same.

  python -m ppro_bridge.doctor                     report only (exit 1 when something required is missing)
  python -m ppro_bridge.doctor --interactive       report, then offer each fix (y/n)
  python -m ppro_bridge.doctor --install-deps --shortcut --autostart     fixes without asking

Required: Python 3.10+, websockets 14+, UXP Developer Tool (Premiere only loads development plugins through it),
UXP developer mode, a Premiere Pro the panel runs in (the minVersion in plugin/manifest.json: 26.3+). Optional:
ffmpeg/ffprobe (scaling, listening report), Pillow (contact sheets), faster-whisper (local transcription). Fixes: pip
install the requirements; shortcuts to start_bridge.bat with the bridge's icon on the desktop and in the Start menu;
start it at login (a shortcut in the Startup folder). Developer mode and UXP Developer Tool are turned on / installed by the user (Creative Cloud, admin rights):
the report says how. Installed packages are read from disk on every check, so a check after a pip install in the
same process (the setup window) sees the new versions. The report ends with one line about the broker's update check
(update.status(): what update.json and settings.json hold; nothing is sent from here).

The command line also asks Creative Cloud's plugin installer (UPIA, read-only `/list all`) whether the panel is
installed as a .ccx (scripts/build_ccx.py) for a Premiere Pro it knows - optional, for .ccx installs only; --no-upia
skips it. When the panel's folder is on disk but UPIA no longer lists it (a Premiere update can unregister a .ccx
plugin), the report prints the repair command; nothing here installs or removes anything. UPIA runs at most once per
process (30-s timeout; the result is reused for UPIA_REUSE_SECONDS), because rapid UPIA calls can wedge it until
Creative Cloud restarts. The setup window leaves this check out: it is the UXP Developer Tool route, and UPIA can
block for minutes while Premiere starts.
"""

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import auth, ccx, console, devloader
from . import update as updates

REPO = Path(__file__).resolve().parent.parent
START_SCRIPT = REPO / "start_bridge.bat"
MANIFEST = REPO / "plugin" / "manifest.json"
ICON = REPO / "ppro_bridge" / "bridge.ico"  # built by scripts/make_icon.py
SHORTCUT_NAME = "Premiere Agent Bridge.lnk"
BROKER_PORT = 8017
PREMIERE_MIN_FALLBACK = (26, 3)
COMMON_FILES = Path(os.environ.get("CommonProgramFiles")
                    or os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Program Files", "Common Files"))
UPIA_ENV = "PPRO_UPIA_EXE"
UPIA_DEFAULT = (COMMON_FILES / "Adobe" / "Adobe Desktop Common" / "RemoteComponents" / "UPI" /
                "UnifiedPluginInstallerAgent" / "UnifiedPluginInstallerAgent.exe")
UPIA_TIMEOUT = 30
UPIA_REUSE_SECONDS = 300.0
CCX_CHECK = "Panel as .ccx"

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
    "UPIA": "a .ccx install needs the Creative Cloud desktop app (UXP Developer Tool loads the panel without it)",
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


# ---------------------------------------------------------------- the panel as a .ccx (UPIA)

_UPIA_HOST = re.compile(r"^\s*(\d+) extensions? installed for (.+?)(?: \(ver ([^)]*)\))?\s*$")
_UPIA_ROW = re.compile(r"^\s*(\S+)\s+(.+?)\s+(\S+)\s*$")
_upia_lock = threading.Lock()
_upia_last = None  # (time.monotonic() when it ran, the upia_list() result)


def find_upia() -> Path | None:
    """Creative Cloud's plugin installer, UnifiedPluginInstallerAgent.exe: PPRO_UPIA_EXE, else its place under Common
    Files (Creative Cloud puts it there). None when it is not installed."""
    env = os.environ.get(UPIA_ENV)
    path = Path(env) if env else UPIA_DEFAULT
    return path if path.is_file() else None


def parse_upia_list(text) -> list[dict]:
    """UPIA `/list all` output -> [{host, version, extensions: [{status, name, version}]}], one entry per host it
    knows ("Premiere Pro", "26.5.2"; "Others" has no version). Each host block is a line "<n> extension(s) installed
    for <host> (ver <version>)", a header, a ==== rule, then one row per extension: status, name, version."""
    hosts, current = [], None
    for line in str(text).splitlines():
        m = _UPIA_HOST.match(line)
        if m:
            current = {"host": m.group(2).strip(), "version": m.group(3), "extensions": []}
            hosts.append(current)
            continue
        stripped = line.strip()
        if (current is None or not stripped or set(stripped) <= {"=", " "}
                or (stripped.startswith("Status") and "Extension Name" in stripped)):
            continue
        row = _UPIA_ROW.match(line)
        if row:
            current["extensions"].append({"status": row.group(1), "name": row.group(2), "version": row.group(3)})
    return hosts


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("mbcs" if os.name == "nt" else "latin-1", errors="replace")


def _run_upia(exe) -> tuple[int, str]:
    """(exit code, output) of `UPIA /list all`; TimeoutExpired after UPIA_TIMEOUT, the process killed. The output
    goes to a temporary file, not a pipe: after a timeout subprocess.run waits without limit for the pipes to close,
    which a helper process UPIA started could hold open."""
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen([str(exe), "/list", "all"], stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            code = proc.wait(timeout=UPIA_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)  # the process itself, not the output a helper may still hold
            except subprocess.TimeoutExpired:
                pass
            raise
        out.seek(0)
        return code, _decode(out.read())


def upia_list(refresh=False) -> dict:
    """`UPIA /list all` (read-only): {"exe", "hosts"}, or {"exe", "error"} (with "timeout": true when it did not
    answer within UPIA_TIMEOUT); {"exe": None} when UPIA is not installed. UPIA runs at most once per
    UPIA_REUSE_SECONDS in this process - a second call reuses the first answer unless `refresh` - and never twice at
    once."""
    global _upia_last
    with _upia_lock:
        if not refresh and _upia_last and time.monotonic() - _upia_last[0] < UPIA_REUSE_SECONDS:
            return _upia_last[1]
        exe = find_upia()
        if exe is None:
            result = {"exe": None}
        else:
            try:
                code, text = _run_upia(exe)
                hosts = parse_upia_list(text)
                first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "no output")
                result = ({"exe": str(exe), "hosts": hosts} if hosts else
                          {"exe": str(exe), "error": f"exit {code}: {first[:200]}"})
            except subprocess.TimeoutExpired:
                result = {"exe": str(exe), "error": f"no answer within {UPIA_TIMEOUT} s", "timeout": True}
            except OSError as e:
                result = {"exe": str(exe), "error": str(e)}
        _upia_last = (time.monotonic(), result)
        return result


def roaming_folder() -> Path | None:
    """The user's roaming application-data folder, where a per-user .ccx install lives, as Windows' known-folder
    API gives it (SHGetKnownFolderPath: it honours folder redirection). None off Windows or when Windows cannot say."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID("3EB685DB-65F9-4CF6-A03A-E3EF65729F3D")  # the known folder for roaming application data
        guid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        found = ctypes.c_wchar_p()
        status = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(found))
        try:
            return Path(found.value) if status == 0 and found.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(found)  # also after a failure, as the API asks
    except (AttributeError, OSError, ValueError):
        return None


def ccx_install_dirs(plugin_id) -> list[Path]:
    """Folders where a .ccx install of `plugin_id` sits on disk (a folder whose manifest.json has that id) in the
    places UXP keeps installed plugins. Unlike UPIA's list, a Premiere update leaves these behind."""
    roots = [COMMON_FILES / "Adobe" / "UXP" / "extensions", COMMON_FILES / "Adobe" / "UXP" / "Plugins" / "External"]
    roaming = roaming_folder()
    if roaming:
        roots.insert(0, roaming / "Adobe" / "UXP" / "Plugins" / "External")
    found = []
    for root in roots:
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            try:
                manifest = json.loads((child / "manifest.json").read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(manifest, dict) and manifest.get("id") == plugin_id:
                found.append(child)
    return found


def _ccx_package(manifest) -> Path | None:
    """This checkout's .ccx to (re)install: dist/<name>-<VERSION>.ccx, else the newest .ccx in dist/."""
    exact = ccx.DIST / ccx.package_name(manifest, str(manifest.get("version")))
    if exact.is_file():
        return exact
    built = sorted(ccx.DIST.glob("*.ccx"), key=lambda p: p.stat().st_mtime) if ccx.DIST.is_dir() else []
    return built[-1] if built else None


def ccx_check(listing=None, manifest=None, installs=None, dirs=None) -> dict:
    """One optional check: is the panel installed as a .ccx for the Premiere Pro builds UPIA knows? `listing`
    (upia_list()), `manifest`, `installs` (premiere_installs()) and `dirs` (ccx_install_dirs()) are read when not
    given."""
    listing = upia_list() if listing is None else listing
    if not listing.get("exe"):
        return _bad(CCX_CHECK, "Creative Cloud's plugin installer (UPIA) not found", FIXES["UPIA"], required=False)
    if listing.get("error"):
        fix = ("UPIA blocks while Premiere starts: run the doctor again in a few minutes (not in a loop - rapid UPIA "
               "calls can wedge it until Creative Cloud restarts)" if listing.get("timeout") else
               "restart the Creative Cloud app, then run the doctor again")
        return _bad(CCX_CHECK, f"UPIA /list all failed: {listing['error']}", fix, required=False)
    if manifest is None:
        try:
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            manifest = {}
    name, plugin_id = manifest.get("name") or "Agent Bridge", manifest.get("id") or ""
    minimum = premiere_min()
    premiere = [h for h in listing["hosts"] if h["host"].lower().startswith("premiere pro")]
    eligible = [h for h in premiere if h["version"] and _version_tuple(h["version"]) >= minimum]
    registered = [(h, e) for h in premiere for e in h["extensions"] if e["name"] == name]
    lacking = [h for h in eligible if not any(e["name"] == name for e in h["extensions"])]
    dirs = (ccx_install_dirs(plugin_id) if plugin_id else []) if dirs is None else dirs

    knows = ("UPIA knows Premiere Pro " + ", ".join(h["version"] or "?" for h in premiere) if premiere else
             "UPIA knows no Premiere Pro")
    installs = premiere_installs() if installs is None else installs
    beta = [i for i in installs if "(beta)" in i.lower()]
    if beta and not any("beta" in h["host"].lower() for h in premiere):
        knows += f", not {beta[0]} (UXP Developer Tool still loads the panel there)"
    package = _ccx_package(manifest)
    target = package or ccx.DIST / ccx.package_name(manifest, str(manifest.get("version")))
    install = f'"{listing["exe"]}" /install "{target}"' + ("" if package else
                                                           " (build it first: python scripts/build_ccx.py)")
    if dirs and not registered and not eligible:
        return _bad(CCX_CHECK, f"{name} is installed in {dirs[0]}, but {knows}: nothing to register it for",
                    f"a .ccx needs a Premiere Pro {version_label(minimum)} or newer that UPIA lists; UXP Developer "
                    "Tool loads the panel into any other", required=False)
    if (registered or dirs) and lacking:
        where = (f"registered for Premiere Pro {', '.join(h['version'] or '?' for h, _ in registered)}" if registered
                 else f"installed in {dirs[0]}")
        missing = ", ".join(h["version"] for h in lacking)
        return _bad(CCX_CHECK, f"{name} is {where} but not registered for Premiere Pro {missing} (a Premiere update "
                               f"can unregister a .ccx plugin); {knows}",
                    f"repair, with Premiere closed (not run here): {install}", required=False)
    if registered:
        found = "; ".join(f"{e['name']} {e['version']} for Premiere Pro {h['version']} ({e['status']})"
                          for h, e in registered)
        if any(e["status"].lower() != "enabled" for _, e in registered):
            return _bad(CCX_CHECK, f"installed as .ccx but not enabled: {found}",
                        "enable it in the Creative Cloud app (Plugins)", required=False)
        if any(e["version"] != manifest.get("version") for _, e in registered):
            return _bad(CCX_CHECK, f"installed as .ccx: {found}; this checkout is {manifest.get('version')}",
                        f'upgrade, with Premiere closed (not run here): "{listing["exe"]}" /remove "{name}", then '
                        f"{install}", required=False)
        return _ok(CCX_CHECK, f"installed as .ccx: {found}; {knows}", required=False)
    return _ok(CCX_CHECK, f"not installed as .ccx (UXP Developer Tool loads the panel); {knows}", required=False)


def data_folder_check() -> dict:
    """The folder the pairing token lives in (auth.token_path's: it follows PPRO_BRIDGE_TOKEN_FILE), looked at, never
    created (QA-4: a check, and every test that runs one, must not make the user's real data folder): there and
    writable, or not there yet with a writable parent (the broker makes it on its first start)."""
    folder = auth.token_path().parent
    fix = "check the permissions of your user folder (%USERPROFILE%)"
    try:
        if folder.is_dir():
            return (_ok("Bridge data folder", str(folder)) if os.access(folder, os.W_OK) else
                    _bad("Bridge data folder", f"{folder} is not writable", fix))
        if folder.exists():
            return _bad("Bridge data folder", f"{folder} is not a folder", fix)
        parent = next((p for p in folder.parents if p.exists()), None)
        if parent is not None and parent.is_dir() and os.access(parent, os.W_OK):
            return _ok("Bridge data folder", f"{folder} (made when the bridge first starts)")
        return _bad("Bridge data folder", f"{folder} cannot be made: {parent} is not writable", fix)
    except OSError as e:
        return _bad("Bridge data folder", str(e), fix)


def checks(upia=False) -> list[dict]:
    """Every check: {name, ok, required, detail, fix}. `upia`: also ask UPIA about a .ccx install (ccx_check)."""
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
    out.append(data_folder_check())
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
    if upia:
        out.append(ccx_check())
    return out


def update_line() -> str:
    """One line about the broker's update check, as update.json and settings.json hold it (nothing is sent)."""
    try:
        return f"  Updates: {updates.status()['text']}"
    except Exception as e:  # noqa: BLE001 - the report goes on without it
        return f"  Updates: unknown ({type(e).__name__})"


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
    return subprocess.call([sys.executable, *console.isolated_flags(), "-m", "pip", "install", "-r",
                            str(REPO / "requirements.txt")])


def _special_folder(name: str) -> Path:
    """A Windows shell folder ("Desktop", "Startup") through PowerShell (honours redirected folders)."""
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          f"[Environment]::GetFolderPath('{name}')"], capture_output=True, text=True, timeout=30)
    path = out.stdout.strip()
    if out.returncode != 0 or not path:
        raise OSError(f"cannot find the {name} folder")
    return Path(path)


def _ps_quote(text) -> str:
    """A PowerShell single-quoted string. PowerShell ends such a string at any of ' ‘ ’ ‚ ‛ (U+0027, U+2018-U+201B),
    and a doubled one stands for itself, so every one of them is doubled - a folder named "Bob’s" must not end the
    string. make_shortcut passes its paths as environment variables instead and needs none of this."""
    return "'" + re.sub("(['‘’‚‛])", r"\1\1", str(text)) + "'"


def make_shortcut(folder: Path, minimized=False) -> Path:
    """A .lnk to start_bridge.bat in `folder` with the bridge's icon (WScript.Shell through PowerShell). The console
    window it opens takes the shortcut's icon too. The paths reach PowerShell as environment variables, never inside
    the script text, so no folder name can change what the script does."""
    link = Path(folder) / SHORTCUT_NAME
    style = 7 if minimized else 1
    icon = "$s.IconLocation = $env:PAB_SHORTCUT_ICON; " if ICON.is_file() else ""
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PAB_SHORTCUT_LINK); "
              "$s.TargetPath = $env:PAB_SHORTCUT_TARGET; $s.WorkingDirectory = $env:PAB_SHORTCUT_DIR; "
              f"$s.WindowStyle = {int(style)}; $s.Description = 'Start the Premiere Agent Bridge'; {icon}$s.Save()")
    env = dict(os.environ, PAB_SHORTCUT_LINK=str(link), PAB_SHORTCUT_TARGET=str(START_SCRIPT),
               PAB_SHORTCUT_DIR=str(START_SCRIPT.parent), PAB_SHORTCUT_ICON=f"{ICON},0")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=30, env=env)
    if out.returncode != 0 or not link.is_file():
        raise OSError(f"could not create {link}: {out.stderr.strip()}")
    return link


def desktop_shortcut() -> Path:
    """Where the desktop shortcut is (or would be) - it may not exist."""
    return _special_folder("Desktop") / SHORTCUT_NAME


def start_menu_shortcut() -> Path:
    """Where the Start menu shortcut is (or would be): the user's Programs folder, so typing "Premiere Agent Bridge"
    in Start finds it - it may not exist."""
    return _special_folder("Programs") / SHORTCUT_NAME


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
    p.add_argument("--shortcut", action="store_true",
                   help="put a 'Premiere Agent Bridge' shortcut on the desktop and in the Start menu")
    p.add_argument("--autostart", action="store_true", help="start the bridge (minimized) at Windows login")
    p.add_argument("--no-upia", action="store_true",
                   help="skip asking Creative Cloud's plugin installer whether the panel is installed as a .ccx")
    args = p.parse_args(argv)

    results = checks(upia=not args.no_upia)
    print("Premiere Agent Bridge - setup check\n" + report(results))
    print(update_line())
    missing_deps = any(r["name"] == "websockets" and not r["ok"] for r in results)
    if args.install_deps or (args.interactive and missing_deps and _ask("\nInstall the Python requirements now?")):
        install_deps()
    if args.shortcut or (args.interactive and
                         _ask("\nPut a 'Premiere Agent Bridge' shortcut on the desktop and in the Start menu?")):
        for where in ("Desktop", "Programs"):
            try:
                print(f"Created {make_shortcut(_special_folder(where))}")
            except (OSError, subprocess.SubprocessError) as e:
                print(f"Shortcut failed ({'Start menu' if where == 'Programs' else where}): {e}")
    if args.autostart or (args.interactive and _ask("Start the bridge automatically when you log in to Windows?")):
        try:
            print(f"Created {make_shortcut(_special_folder('Startup'), minimized=True)}")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"Autostart failed: {e}")
    if args.install_deps or args.interactive:
        results = checks(upia=not args.no_upia)  # UPIA's answer is reused, not asked again
        print("\nAfter the fixes:\n" + report(results))
    missing = [r["name"] for r in results if r["required"] and not r["ok"]]
    print("\nNext: double-click start_bridge.bat, open Premiere - the panel loads by itself - then "
          "`python cli\\ppro.py ping`." if not missing else f"\nStill missing: {', '.join(missing)}.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
