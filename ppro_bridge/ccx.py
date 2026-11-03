"""The panel as a .ccx package: what scripts/build_ccx.py writes, and what ping's stale check compares a panel with.

A .ccx is a ZIP of the plugin folder (manifest.json at its root) that Creative Cloud's plugin installer (UPIA,
`UnifiedPluginInstallerAgent.exe /install x.ccx`) unpacks and registers. The package differs from plugin/ in three
files:
  manifest.json   `host` is the one premierepro object (a .ccx names one host; the repo keeps a list)
  lib/build.js    `dev: false, packaged: true`: the panel registers no api_smoke (build_ccx.py --dev keeps dev
                  true), and ping's build.packaged tells the broker not to load the dev copy over it
  launcher.json   new, at the root: {"path": "<absolute start_bridge.bat of the checkout that built it>"}, which the
                  panel's Start bridge button runs (the dev layout has none and uses ../start_bridge.bat)
It holds what the panel loads - manifest.json, index.html, main.js, lib/*.js, and any stylesheet, image or font
under plugin/ - never tests, caches, dotfiles or npm files. A manifest is packaged only when its network rule is
exactly NETWORK_DOMAINS and its launchProcess may start a .bat.
"""

import json
import re
from pathlib import Path

from . import panelhash

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "plugin"
DIST = REPO / "dist"
NETWORK_DOMAINS = ["ws://localhost/"]  # UXP accepts no IP and no port here; the panel connects to ws://localhost:8017
LAUNCH_EXTENSION = ".bat"
LAUNCHER = "launcher.json"
BUILD_JS = "lib/build.js"
BUILD_FLAG = re.compile(r"\bdev\s*:\s*(true|false)\b")  # exactly one match in plugin/lib/build.js
PACKAGED_FLAG = re.compile(r"\bpackaged\s*:\s*(\w+)")  # none in the repo; `packaged: true` in a package
PACKAGED_SUFFIXES = {".html", ".js", ".json", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
                     ".woff", ".woff2", ".ttf", ".otf"}
SKIP_DIRS = {"tests", "test", "__tests__", "__pycache__", "node_modules"}
SKIP_FILES = {LAUNCHER, "package.json", "package-lock.json"}


class PackageError(ValueError):
    """The plugin folder cannot be packaged; `problems` lists every reason, not only the first."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def read_manifest(plugin_dir=PLUGIN_DIR) -> dict:
    data = json.loads((Path(plugin_dir) / "manifest.json").read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise PackageError(["plugin/manifest.json is not a JSON object"])
    return data


def manifest_problems(manifest: dict) -> list[str]:
    """Why this manifest cannot go into a .ccx (empty: it can)."""
    problems = []
    hosts = manifest.get("host")
    listed = hosts if isinstance(hosts, list) else [hosts]
    premiere = [h for h in listed if isinstance(h, dict) and str(h.get("app", "")).lower() == "premierepro"]
    if len(premiere) != 1 or len(listed) != 1:
        problems.append(f"manifest host must name Premiere Pro (app \"premierepro\") once and nothing else, not "
                        f"{json.dumps(hosts)}: a .ccx installs into one host")
    perms = manifest.get("requiredPermissions") if isinstance(manifest.get("requiredPermissions"), dict) else {}
    network = perms.get("network") if isinstance(perms.get("network"), dict) else {}
    if network.get("domains") != NETWORK_DOMAINS:
        problems.append(f"manifest requiredPermissions.network.domains is {json.dumps(network.get('domains'))}, "
                        f"a .ccx needs exactly {json.dumps(NETWORK_DOMAINS)} (the panel connects to "
                        "ws://localhost:8017; UXP accepts no IP and no port in this list)")
    launch = perms.get("launchProcess") if isinstance(perms.get("launchProcess"), dict) else None
    extensions = launch.get("extensions") if launch else None
    if not isinstance(extensions, list) or LAUNCH_EXTENSION not in extensions:
        problems.append(f"manifest requiredPermissions.launchProcess.extensions must list \"{LAUNCH_EXTENSION}\" "
                        f"(found {json.dumps(launch)}): the panel's Start bridge button runs start_bridge.bat")
    return problems


def package_manifest(manifest: dict, plugin_id=None, name=None) -> dict:
    """The manifest as packaged: `host` is the one premierepro object, everything else as it is - unless a test
    package gets another `plugin_id` and `name` (the panels' menu label too), so it can sit next to the panel UXP
    Developer Tool loads. PackageError with every problem when it cannot be packaged."""
    problems = manifest_problems(manifest)
    if problems:
        raise PackageError(problems)
    out = dict(manifest)
    hosts = manifest["host"]
    out["host"] = dict(hosts[0] if isinstance(hosts, list) else hosts)
    if plugin_id:
        out["id"] = plugin_id
    if name:
        out["name"] = name
        out["entrypoints"] = [dict(e, label=dict(e.get("label") or {}, default=name))
                              if isinstance(e, dict) and e.get("type") == "panel" else e
                              for e in manifest.get("entrypoints") or []]
    return out


def manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def set_build_flag(text: str, dev: bool) -> str:
    """build.js as packaged: its one dev flag set, followed by `packaged: true` (the broker's panel loader leaves a
    packaged panel alone). PackageError unless the flag is there exactly once and nothing says packaged yet."""
    found = len(BUILD_FLAG.findall(text))
    if found != 1:
        raise PackageError([f"plugin/{BUILD_JS} must hold exactly one `dev: true` (or false), found {found}"])
    if PACKAGED_FLAG.search(text):
        raise PackageError([f"plugin/{BUILD_JS} must not say `packaged:` (only a package does)"])
    return BUILD_FLAG.sub(f"dev: {'true' if dev else 'false'}, packaged: true", text, count=1)


def build_flags(text: str) -> tuple:
    """(dev, packaged) as build.js says them: dev None unless it is there exactly once, packaged True only for
    exactly one `packaged: true`."""
    dev = BUILD_FLAG.findall(text)
    packaged = PACKAGED_FLAG.findall(text)
    return (dev[0] == "true" if len(dev) == 1 else None), packaged == ["true"]


def plugin_files(plugin_dir=PLUGIN_DIR) -> list[str]:
    """The plugin folder's files that go into the package, as sorted relative POSIX paths."""
    root = Path(plugin_dir)
    out = []
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        if (not path.is_file() or any(p.startswith(".") for p in parts) or SKIP_DIRS & set(parts[:-1])
                or (len(parts) == 1 and parts[0] in SKIP_FILES) or path.suffix.lower() not in PACKAGED_SUFFIXES):
            continue
        out.append(rel)
    return sorted(out)


def package_files(plugin_dir=PLUGIN_DIR, dev=False, plugin_id=None, name=None) -> dict:
    """{relative POSIX path: bytes} of every package file except launcher.json, exactly as they go into the zip
    (`plugin_id`, `name`: a test package, see package_manifest). PackageError (every problem) when the folder cannot
    be packaged."""
    root = Path(plugin_dir)
    files = {rel: (root / rel).read_bytes() for rel in plugin_files(root)}
    problems = []
    try:
        files["manifest.json"] = manifest_bytes(package_manifest(read_manifest(root), plugin_id, name))
    except PackageError as e:
        problems += e.problems
    except (OSError, ValueError) as e:
        problems.append(f"plugin/manifest.json cannot be read: {e}")
    if BUILD_JS not in files:
        problems.append(f"plugin/{BUILD_JS} is missing: it carries the dev flag a package sets to false")
    else:
        try:
            files[BUILD_JS] = set_build_flag(files[BUILD_JS].decode("utf-8"), dev).encode("utf-8")
        except PackageError as e:
            problems += e.problems
    if problems:
        raise PackageError(problems)
    return files


def launcher_bytes(start_script) -> bytes:
    """launcher.json: the absolute path of start_bridge.bat (ASCII JSON, LF)."""
    return (json.dumps({"path": str(Path(start_script).resolve())}) + "\n").encode("ascii")


def package_name(manifest: dict, version: str) -> str:
    """agent-bridge-0.5.1.ccx: the manifest name in lowercase, runs of other characters as one hyphen."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(manifest.get("name") or "plugin").lower()).strip("-") or "plugin"
    return f"{slug}-{version}.ccx"


def packaged_code_hashes(plugin_dir=PLUGIN_DIR) -> set:
    """The codeHash a panel installed from this checkout's .ccx reports (built with and without --dev); empty when
    the folder cannot be packaged."""
    out = set()
    for dev in (False, True):
        try:
            value = panelhash.hash_files(package_files(plugin_dir, dev=dev))
        except (OSError, ValueError):
            continue
        if value:
            out.add(value)
    return out
