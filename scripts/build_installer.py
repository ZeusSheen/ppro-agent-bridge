"""Build the Windows installer, dist/PremiereAgentBridge-Setup-<version>.exe, from a release export.

    python scripts/build_installer.py <release export folder> [--download] [--with-mcp] [--out DIR] [--cache DIR]
                                      [--work DIR] [--iscc PATH] [--require-iscc] [--project-url URL] [--allow-dev]

Input: the folder the release export writes (ppro-agent-bridge-v<version>, or the ppro-agent-bridge folder inside
it), so the list of shipped files lives in one place, the export's FILES. The two front .bat files beside it are not
installed: the installer's shortcuts replace them. The export must be clean (no __pycache__, .git, dist, a python
folder) and say `dev: false` in plugin/lib/build.js (--allow-dev for a test build).

Steps, each refusing with every reason it finds (BuildError) before the next one starts:
  1. installer/python.lock.json: the python.org embeddable package (url, SHA-256). The zip comes from the cache
     (default dist/installer-cache/python); it is downloaded only with --download, and a file whose SHA-256 differs
     from the lock is refused, cached or fresh.
  2. installer/requirements.lock: wheels pinned by version and SHA-256, in groups ("# group: core" always, "mcp" with
     --with-mcp). With --download, `pip download` fetches them from PyPI into the cache (dist/installer-cache/wheels);
     without it nothing touches the network. Every cached wheel of a locked project is hashed and checked against the
     lock before pip runs; pip then installs them with --no-index --require-hashes into the bundled Python's
     Lib\\site-packages (--target, --only-binary :all:, the lock's platform win_amd64, CPython 3.12 ABI).
  3. Staging (default dist/installer-stage-<version>): app\\ = a copy of the export; app\\python\\ = the embeddable
     zip, its python3XX._pth rewritten so `import site` runs and sys.path holds Lib\\site-packages and the bridge
     folder (..), which `-m ppro_bridge.setup_ui` needs: a ._pth puts Python in isolated mode, which leaves the
     current folder and the script's folder off sys.path; app\\installed-files.txt; a copy of
     installer/PremiereAgentBridge.iss with generated.iss (version, Python version, project URL) and
     generated_uninstall.iss (the __pycache__ folders to delete on uninstall).
  4. The panel is packaged once with the export's own scripts/build_ccx.py as a check (the installer packages it again
     on the user's machine, because a .ccx's launcher.json names the absolute path of start_bridge.bat).
  5. ISCC (Inno Setup 6.5+): --iscc, else the ISCC environment variable, PATH, the
     registry and the usual folders. Found: it compiles the staged script into --out (default dist/) and the build
     writes <exe>.sha256 ("<hash> *<name>"). Not found: the command is printed (exit 1 with --require-iscc).

Nothing is installed, and nothing is downloaded without --download. The build machine needs Python 3.10+ with pip
(any OS for steps 1-4: pip downloads and installs the win_amd64 wheels by tag), and Windows for ISCC.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
INSTALLER_DIR = REPO / "installer"
ISS_NAME = "PremiereAgentBridge.iss"
PYTHON_LOCK = INSTALLER_DIR / "python.lock.json"
REQUIREMENTS_LOCK = INSTALLER_DIR / "requirements.lock"
DIST = REPO / "dist"
INNER = "ppro-agent-bridge"
FILE_LIST = "installed-files.txt"
STAGE_MARKER = ".build_installer_stage"
SETUP_NAME = "PremiereAgentBridge-Setup-{version}.exe"
DEFAULT_GROUPS = ("core",)
PYPI_INDEX = "https://pypi.org/simple"
PYTHON_HOST = "www.python.org"
DOWNLOAD_TIMEOUT = 120
CHUNK = 1 << 20

VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
BUILD_FLAG = re.compile(r"\bdev\s*:\s*(true|false)\b")  # ppro_bridge/ccx.py BUILD_FLAG: one match in build.js
PACKAGED_FLAG = re.compile(r"\bpackaged\s*:")
GROUP_LINE = re.compile(r"^#\s*group:\s*([a-z0-9_-]+)\s*$", re.IGNORECASE)
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)$")
HASH_OPTION = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")
PROJECT_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*)?$")
# Files every release export has, which the installer relies on.
REQUIRED_FILES = ("VERSION", "LICENSE", "start_bridge.bat", "setup_bridge.bat", "plugin/manifest.json",
                  "plugin/lib/build.js", "ppro_bridge/setup_ui.py", "ppro_bridge/doctor.py", "ppro_bridge/bridge.ico",
                  "scripts/build_ccx.py", "broker/broker.py")
# Folders a clean export never has: caches and environments anywhere, and at its top the build outputs and the
# python folder the staging adds.
UNCLEAN_ANYWHERE = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".pytest_cache", ".mypy_cache"}
UNCLEAN_TOP = {"dist", "python", "build"}


class BuildError(Exception):
    """The installer cannot be built; `problems` lists every reason found, not only the first."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def normalize(name: str) -> str:
    """A project name as pip compares them (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _placeholder(value) -> bool:
    return not isinstance(value, str) or not SHA256_RE.fullmatch(value) or len(set(value)) == 1


# ---------------------------------------------------------------- the locks

def parse_python_lock(data) -> dict:
    """installer/python.lock.json (a dict, or a path) -> {version, url, sha256, pth, file, wheels}; BuildError with every
    problem. The url must be an https python.org download of that version's embeddable amd64 zip, the sha256 real (64
    lowercase hex digits, not a placeholder), the pth python3XX._pth for that version, and the wheel settings must
    match it (python_version X.Y, abi cpXY)."""
    if not isinstance(data, dict):
        try:
            data = json.loads(Path(data).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise BuildError(f"python.lock.json cannot be read: {e}") from e
    problems = []
    py = data.get("python") if isinstance(data.get("python"), dict) else {}
    wheels = data.get("wheels") if isinstance(data.get("wheels"), dict) else {}
    version = str(py.get("version") or "")
    url = str(py.get("url") or "")
    if not VERSION_RE.fullmatch(version):
        problems.append(f"python.version must be x.y.z, not {version!r}")
    parsed = urllib.parse.urlsplit(url)
    file = PurePosixPath(parsed.path).name
    if parsed.scheme != "https" or parsed.hostname != PYTHON_HOST:
        problems.append(f"python.url must be an https download from {PYTHON_HOST}, not {url!r}")
    elif not (file.startswith(f"python-{version}-") and file.endswith("-amd64.zip") and "embed" in file):
        problems.append(f"python.url must name the {version} embeddable amd64 zip, not {file!r}")
    if _placeholder(py.get("sha256")):
        problems.append(f"python.sha256 is not a SHA-256 ({py.get('sha256')!r}): fill in the hash python.org publishes "
                        "for this file (its Windows release index, index-windows.json)")
    major_minor = ".".join(version.split(".")[:2])
    tag = major_minor.replace(".", "")
    if py.get("pth") != f"python{tag}._pth":
        problems.append(f"python.pth must be python{tag}._pth for {version}, not {py.get('pth')!r}")
    for key in ("python_version", "implementation", "abi", "platform"):
        if not isinstance(wheels.get(key), str) or not wheels.get(key):
            problems.append(f"wheels.{key} is missing")
    if wheels.get("python_version") and wheels.get("python_version") != major_minor:
        problems.append(f"wheels.python_version is {wheels.get('python_version')!r}, the bundled Python is "
                        f"{major_minor}")
    if wheels.get("abi") and wheels.get("abi") != f"cp{tag}":
        problems.append(f"wheels.abi is {wheels.get('abi')!r}, the bundled Python's is cp{tag}")
    if wheels.get("platform") and wheels.get("platform") != "win_amd64":
        problems.append(f"wheels.platform is {wheels.get('platform')!r}: the bundled Python is win_amd64")
    if problems:
        raise BuildError(problems)
    return {"version": version, "url": url, "sha256": py["sha256"], "pth": py["pth"], "file": file,
            "wheels": {k: wheels[k] for k in ("python_version", "implementation", "abi", "platform")}}


def parse_requirements_lock(text: str) -> list[dict]:
    """installer/requirements.lock -> [{name, version, hashes, group}] in file order; BuildError with every problem.

    Each requirement is `name==version` followed by `--hash=sha256:<64 hex>` options (backslash continuations), after a
    `# group: NAME` line. Nothing else is allowed: no ranges, extras, markers, URLs or pip options (-r, -e,
    --index-url ...), which could point pip somewhere the hashes do not cover."""
    problems, out, seen = [], [], {}
    group, logical, start = None, "", 0
    lines = str(text).splitlines()
    for number, line in enumerate(lines + [""], 1):
        stripped = line.strip()
        m = GROUP_LINE.match(stripped)
        if not logical and m:
            group = m.group(1).lower()
            continue
        if stripped.startswith("#") or (not stripped and not logical):
            continue
        if not logical:
            start = number
        if stripped.endswith("\\"):
            logical += " " + stripped[:-1]
            if number <= len(lines):
                continue
        else:
            logical += " " + stripped
        tokens, logical = logical.split(), ""
        if not tokens:
            continue
        where = f"requirements.lock line {start}"
        pin = PIN.match(tokens[0])
        if not pin:
            problems.append(f"{where}: {tokens[0]!r} is not name==version (a lock pins exact versions, nothing else)")
            continue
        hashes, bad = [], []
        for token in tokens[1:]:
            h = HASH_OPTION.match(token)
            if h and not _placeholder(h.group(1)):
                hashes.append(h.group(1))
            else:
                bad.append(token)
        name, version = pin.group(1), pin.group(2)
        if bad:
            problems.append(f"{where}: {name} has {' '.join(bad)!r}: only --hash=sha256:<64 hex digits> may follow "
                            "the pin (no placeholders)")
        if not hashes and not bad:
            problems.append(f"{where}: {name}=={version} has no --hash=sha256:...")
        if group is None:
            problems.append(f"{where}: {name} comes before the first '# group: NAME' line")
        key = normalize(name)
        if key in seen:
            problems.append(f"{where}: {name} is listed again (first in group {seen[key]})")
        seen.setdefault(key, group)
        out.append({"name": name, "version": version, "hashes": tuple(hashes), "group": group})
    if not out and not problems:
        problems.append("requirements.lock lists no requirement")
    if problems:
        raise BuildError(problems)
    return out


def select_groups(requirements, groups) -> list[dict]:
    """The requirements of `groups` (core is always one); BuildError for a group the lock does not have."""
    known = {r["group"] for r in requirements}
    unknown = sorted(set(groups) - known)
    if unknown:
        raise BuildError(f"requirements.lock has no group {', '.join(unknown)} (it has {', '.join(sorted(known))})")
    return [r for r in requirements if r["group"] in set(groups)]


def render_requirements(requirements) -> str:
    """The pip requirements file for these requirements (one line each)."""
    return "".join(f"{r['name']}=={r['version']} " + " ".join(f"--hash=sha256:{h}" for h in r["hashes"]) + "\n"
                   for r in requirements)


def wheel_project(filename: str):
    """(name, version) from a wheel file name, or None when it is not one."""
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    return (parts[0], parts[1]) if len(parts) in (5, 6) else None


def check_wheels(requirements, wheel_dir) -> list[str]:
    """What is wrong with the wheel cache for these requirements (empty: nothing). Every wheel of a locked project and
    version must have one of the lock's hashes - a stray one is refused rather than left for pip to judge - and each
    requirement needs at least one."""
    problems = []
    wheels = sorted(Path(wheel_dir).glob("*.whl")) if Path(wheel_dir).is_dir() else []
    for req in requirements:
        matched = 0
        for path in wheels:
            project = wheel_project(path.name)
            if not project or normalize(project[0]) != normalize(req["name"]) or project[1] != req["version"]:
                continue
            digest = sha256_file(path)
            if digest in req["hashes"]:
                matched += 1
            else:
                problems.append(f"{path} does not match requirements.lock (sha256 {digest}, the lock allows "
                                f"{', '.join(req['hashes'])}): delete it and check where it came from")
        if not matched:
            problems.append(f"no wheel for {req['name']}=={req['version']} in {wheel_dir}: run with --download")
    return problems


# ---------------------------------------------------------------- downloads (only with --download)

def fetch(url: str, dest, sha256: str, *, download: bool, opener=None) -> Path:
    """The file `url` names, from `dest` when it is there and matches `sha256`. Downloaded only when `download`, to
    dest + ".partial", and kept only when it matches. A cached file that does not match is refused (and left there to
    look at), never replaced silently."""
    dest = Path(dest)
    if dest.is_file():
        digest = sha256_file(dest)
        if digest != sha256:
            raise BuildError(f"{dest} does not match the lock (sha256 {digest}, expected {sha256}): delete it and "
                             f"check where it came from")
        return dest
    if not download:
        raise BuildError(f"{dest.name} is not in the cache ({dest.parent}): run with --download to fetch {url}, or "
                         f"put the file there yourself")
    if urllib.parse.urlsplit(url).scheme != "https":
        raise BuildError(f"refusing to download over anything but https: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    h = hashlib.sha256()
    try:
        with (opener or urllib.request.urlopen)(url, timeout=DOWNLOAD_TIMEOUT) as response, open(partial, "wb") as out:
            for block in iter(lambda: response.read(CHUNK), b""):
                h.update(block)
                out.write(block)
        if h.hexdigest() != sha256:
            raise BuildError(f"{url} downloaded with sha256 {h.hexdigest()}, the lock says {sha256}: not used")
        partial.replace(dest)
    except OSError as e:
        raise BuildError(f"downloading {url} failed: {e}") from e
    finally:
        partial.unlink(missing_ok=True)
    return dest


def _pip_target_args(wheels: dict) -> list[str]:
    return ["--only-binary", ":all:", "--platform", wheels["platform"], "--python-version", wheels["python_version"],
            "--implementation", wheels["implementation"], "--abi", wheels["abi"]]


def pip_download_command(requirements_file, wheel_dir, wheels: dict, python=None) -> list[str]:
    """pip download of the locked wheels from PyPI (network; only with --download). --isolated: no pip.ini or PIP_*
    variable can send pip to another index."""
    return [python or sys.executable, "-m", "pip", "download", "--isolated", "--disable-pip-version-check",
            "--no-input", "--require-hashes", "--index-url", PYPI_INDEX, *_pip_target_args(wheels),
            "--dest", str(wheel_dir), "-r", str(requirements_file)]


def pip_install_command(requirements_file, wheel_dir, target, wheels: dict, python=None) -> list[str]:
    """pip install of the cached wheels into the bundled Python, offline (--no-index)."""
    return [python or sys.executable, "-m", "pip", "install", "--isolated", "--disable-pip-version-check",
            "--no-input", "--require-hashes", "--no-index", "--find-links", str(wheel_dir), *_pip_target_args(wheels),
            "--target", str(target), "--no-compile", "-r", str(requirements_file)]


def _run(cmd, run=None, what="") -> None:
    try:
        out = (run or subprocess.run)(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                      encoding="utf-8", errors="replace")
    except OSError as e:
        raise BuildError(f"{what} could not start: {e}") from e
    if out.returncode != 0:
        tail = "\n".join((out.stdout or "").strip().splitlines()[-15:])
        raise BuildError(f"{what} failed (exit {out.returncode}):\n{tail}")


# ---------------------------------------------------------------- the export

def find_export(path) -> Path:
    """The bridge folder of a release export: <path>/ppro-agent-bridge when there, else <path> itself."""
    path = Path(path)
    for candidate in (path / INNER, path):
        if (candidate / "VERSION").is_file():
            return candidate
    raise BuildError(f"{path} is not a release export: no VERSION in it or in {path / INNER}")


def check_export(app_src, allow_dev=False) -> str:
    """The export's version; BuildError listing everything that keeps it from going into an installer."""
    root = Path(app_src)
    problems = []
    try:
        version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
    except OSError as e:
        raise BuildError(f"VERSION cannot be read: {e}") from e
    if not VERSION_RE.fullmatch(version):
        problems.append(f"VERSION must be x.y.z, not {version!r}")
    problems += [f"{rel} is missing" for rel in REQUIRED_FILES if not (root / rel).is_file()]
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_dir() and (path.name in UNCLEAN_ANYWHERE or (len(rel.parts) == 1 and path.name in UNCLEAN_TOP)):
            problems.append(f"{rel.as_posix()}/ does not belong in a release export (build from a fresh export)")
        elif not rel.as_posix().isascii():
            problems.append(f"{rel.as_posix()!r}: the installer's file list takes ASCII names only")
    build_js = root / "plugin" / "lib" / "build.js"
    if build_js.is_file():
        text = build_js.read_text(encoding="utf-8")
        flags = BUILD_FLAG.findall(text)
        if len(flags) != 1 or PACKAGED_FLAG.search(text):
            problems.append("plugin/lib/build.js must hold exactly one `dev: true|false` and no `packaged:`")
        elif flags[0] != "false" and not allow_dev:
            problems.append("plugin/lib/build.js says dev: true (a dev build registers api_smoke): build from a release "
                            "export, or pass --allow-dev for a test installer")
    manifest = root / "plugin" / "manifest.json"
    if manifest.is_file():
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8-sig")).get("version")
        except (ValueError, AttributeError):
            declared = None
            problems.append("plugin/manifest.json does not parse")
        if declared is not None and declared != version:
            problems.append(f"plugin/manifest.json says version {declared!r}, VERSION says {version!r}")
    if problems:
        raise BuildError(problems)
    return version


# ---------------------------------------------------------------- staging

def pth_text(original: str) -> str:
    """The bundled Python's ._pth: the embeddable package's own entries (its stdlib zip and its folder), then
    Lib\\site-packages (the bundled wheels, and what the setup window's pip installs later), then .. (the bridge
    folder: `python -m ppro_bridge...` from anywhere), and `import site` (site-packages' .pth files, e.g. pywin32's).
    `import site` also adds the per-user site-packages folder unless Python runs with -s / -I,
    so every start of this Python - shortcuts, [Run] entries, start_bridge.bat, setup_bridge.bat, the bridge's own
    subprocesses (console.isolated_flags) - passes -I (QA-8). BuildError when the original names no stdlib zip."""
    keep = []
    for line in str(original).splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.startswith("import "):
            continue
        if entry not in keep:
            keep.append(entry)
    if not any(entry.lower().endswith(".zip") for entry in keep):
        raise BuildError(f"the embeddable package's ._pth names no standard library zip: {original!r}")
    for entry in ("Lib\\site-packages", ".."):
        if entry not in keep:
            keep.append(entry)
    return ("# Written by scripts/build_installer.py: the Python bundled with Premiere Agent Bridge.\n"
            "# Paths are relative to this folder; .. is the bridge folder. A ._pth keeps PYTHONPATH and the\n"
            "# current folder off sys.path; `import site` would still add the user site-packages, so the\n"
            "# bridge always starts this Python with -I.\n"
            + "".join(f"{entry}\n" for entry in keep) + "import site\n")


def extract_python(zip_path, dest, pth_name: str) -> Path:
    """Unpack the embeddable package into `dest` (nothing outside it) and rewrite its ._pth; the ._pth path."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if info.filename.startswith(("/", "\\")) or ":" in info.filename or (
                    target != root and root not in target.parents):
                raise BuildError(f"{zip_path}: entry {info.filename!r} would land outside {dest}")
        z.extractall(dest)
    pths = sorted(p.name for p in dest.glob("*._pth"))
    missing = [name for name in ("python.exe", "pythonw.exe") if not (dest / name).is_file()]
    if pths != [pth_name] or missing:
        raise BuildError(f"{zip_path} is not the embeddable package the lock describes: ._pth files {pths} (expected "
                         f"[{pth_name!r}])" + (f", missing {', '.join(missing)}" if missing else ""))
    pth = dest / pth_name
    pth.write_text(pth_text(pth.read_text(encoding="utf-8-sig")), encoding="utf-8", newline="\r\n")
    (dest / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    return pth


def check_installed(requirements, site_packages) -> list[str]:
    """Each requirement has its <name>-<version>.dist-info in site-packages (empty list: all there)."""
    found = set()
    for path in Path(site_packages).glob("*.dist-info"):
        name, _, version = path.name[:-len(".dist-info")].rpartition("-")
        found.add((normalize(name), version))
    return [f"{r['name']}=={r['version']} is not in {site_packages} after pip install" for r in requirements
            if (normalize(r["name"]), r["version"]) not in found]


def file_list(app_dir) -> list[str]:
    """Every file under app\\ except the bundled Python (replaced whole on every install), as relative Windows paths,
    sorted; the installer compares the old and new lists on upgrade."""
    root = Path(app_dir)
    out = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if path.is_file() and rel.parts[0].lower() != "python":
            out.append("\\".join(rel.parts))
    out.append(FILE_LIST)  # itself, written after this list is made
    return sorted(set(out), key=str.lower)


def pycache_dirs(app_dir) -> list[str]:
    """The folders (relative, Windows separators; "" = the top) holding .py files outside the bundled Python: where
    Python writes __pycache__ at run time."""
    root = Path(app_dir)
    dirs = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0].lower() != "python":
            dirs.add("\\".join(rel.parts[:-1]))
    return sorted(dirs, key=str.lower)


def _iss_string(value: str) -> str:
    """An ISPP string literal (a " inside is doubled)."""
    return '"' + str(value).replace('"', '""') + '"'


def generated_iss(version: str, python_version: str, project_url: str, groups) -> str:
    """generated.iss: the defines PremiereAgentBridge.iss reads."""
    return ("; Generated by scripts/build_installer.py. Do not edit.\n"
            f"#define AppVersion {_iss_string(version)}\n"
            f"#define PythonVersion {_iss_string(python_version)}\n"
            f"#define ProjectUrl {_iss_string(project_url)}\n"
            f"#define BundledGroups {_iss_string(', '.join(groups))}\n")


def generated_uninstall(dirs) -> str:
    """generated_uninstall.iss: one [UninstallDelete] line per __pycache__ folder Python may write."""
    lines = ["; Generated by scripts/build_installer.py. Do not edit."]
    for rel in dirs:
        where = "{app}\\" + (rel + "\\" if rel else "") + "__pycache__"
        lines.append(f'Type: filesandordirs; Name: "{where}"')
    return "\n".join(lines) + "\n"


def prepare_work(work) -> Path:
    """An empty staging folder. An existing one is emptied only when this script made it (its marker file is there):
    --work never deletes a folder it did not create."""
    work = Path(work)
    if work.exists():
        if not (work / STAGE_MARKER).is_file() and any(work.iterdir()):
            raise BuildError(f"{work} exists and was not made by build_installer.py: choose another --work folder")
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / STAGE_MARKER).write_text("staging folder of scripts/build_installer.py; deleted on the next build\n",
                                     encoding="utf-8")
    return work


def stage(export_dir, work, python_zip, pin: dict, requirements, wheel_dir, *, version: str, project_url="",
          groups=DEFAULT_GROUPS, iss=None, run=None) -> dict:
    """Lay out the staging folder (see the module docstring); {work, app, iss, files, pycache}."""
    work = prepare_work(work)
    app = work / "app"
    shutil.copytree(export_dir, app)
    extract_python(python_zip, app / "python", pin["pth"])
    site_packages = app / "python" / "Lib" / "site-packages"
    requirements_file = work / "requirements.txt"
    requirements_file.write_text(render_requirements(requirements), encoding="utf-8")
    _run(pip_install_command(requirements_file, wheel_dir, site_packages, pin["wheels"]), run, "pip install")
    for scripts in ("bin", "Scripts"):  # console-script launchers pip wrote for the build machine's Python
        shutil.rmtree(site_packages / scripts, ignore_errors=True)
    missing = check_installed(requirements, site_packages)
    if missing:
        raise BuildError(missing)
    requirements_file.unlink()
    files = file_list(app)
    (app / FILE_LIST).write_text("".join(f"{rel}\r\n" for rel in files), encoding="ascii", newline="")
    dirs = pycache_dirs(app)
    shutil.copyfile(iss or INSTALLER_DIR / ISS_NAME, work / ISS_NAME)
    zh = INSTALLER_DIR / "ChineseSimplified.isl"  # optional: Inno Setup 6.7.1 ships no Simplified Chinese texts
    if zh.is_file():
        shutil.copyfile(zh, work / zh.name)
    (work / "generated.iss").write_text(generated_iss(version, pin["version"], project_url, groups), encoding="utf-8")
    (work / "generated_uninstall.iss").write_text(generated_uninstall(dirs), encoding="utf-8")
    return {"work": work, "app": app, "iss": work / ISS_NAME, "files": len(files), "pycache": len(dirs)}


def check_ccx(app_dir, out_dir, run=None) -> Path:
    """Package the panel once with the export's own scripts/build_ccx.py (isolated Python, as the installer runs it):
    the .ccx path. BuildError when it refuses."""
    out_dir = Path(out_dir)
    _run([sys.executable, "-I", "-B", str(Path(app_dir) / "scripts" / "build_ccx.py"), "--out", str(out_dir)], run,
         "scripts/build_ccx.py")
    built = sorted(out_dir.glob("*.ccx"))
    if len(built) != 1:
        raise BuildError(f"scripts/build_ccx.py wrote {len(built)} .ccx files in {out_dir}, expected one")
    return built[0]


# ---------------------------------------------------------------- Inno Setup

def find_iscc(explicit=None) -> Path | None:
    """ISCC.exe: `explicit`, else $ISCC, PATH, the Inno Setup 6/7 uninstall entries in the registry, the usual
    folders. None when there is none."""
    candidates = [explicit, os.environ.get("ISCC"), shutil.which("iscc")]
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                for name in ("Inno Setup 6_is1", "Inno Setup 7_is1"):
                    try:
                        with winreg.OpenKey(root, f"{sub}\\{name}") as key:
                            candidates.append(str(Path(winreg.QueryValueEx(key, "InstallLocation")[0]) / "ISCC.exe"))
                    except OSError:
                        continue
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs") if os.environ.get("LOCALAPPDATA") else None):
        if base:
            candidates += [os.path.join(base, f"Inno Setup {major}", "ISCC.exe") for major in (6, 7)]
    return next((Path(c) for c in candidates if c and Path(c).is_file()), None)


def iscc_problems(iscc) -> list[str]:
    """Why this ISCC cannot compile the script (empty: it can). The Simplified Chinese wizard texts are not needed:
    Inno Setup 6.7.1 ships none (they are an unofficial translation), and the script falls back (zh_note)."""
    return [] if Path(iscc).is_file() else [f"{iscc} is not a file"]


def zh_note(iscc) -> str | None:
    """None when the Chinese wizard has its own texts (the compiler's Languages\\ChineseSimplified.isl, or
    installer/ChineseSimplified.isl), else what the Chinese installer will look like."""
    if (Path(iscc).parent / "Languages" / "ChineseSimplified.isl").is_file() or \
            (INSTALLER_DIR / "ChineseSimplified.isl").is_file():
        return None
    return ("no ChineseSimplified.isl (Inno Setup 6.7.1 ships none; it is an unofficial translation): the Chinese "
            "wizard shows Inno Setup's English pages, with our own texts in Chinese. Put the file in installer/ to "
            "bundle it.")


def iscc_command(iscc, iss, out_dir) -> list[str]:
    return [str(iscc), "/Qp", f"/O{Path(out_dir)}", str(iss)]


def write_sha256(path) -> Path:
    """<file>.sha256 next to it, as sha256sum writes it: "<hash> *<name>"."""
    path = Path(path)
    out = path.with_name(path.name + ".sha256")
    out.write_text(f"{sha256_file(path)} *{path.name}\n", encoding="ascii", newline="\n")
    return out


# ---------------------------------------------------------------- main

def build(export, *, download=False, groups=DEFAULT_GROUPS, out=None, cache=None, work=None, iscc=None,
          require_iscc=False, project_url="", allow_dev=False, run=None, opener=None, log=print) -> dict:
    """Everything main() does; a summary dict. BuildError on any refusal."""
    if project_url and not PROJECT_URL.fullmatch(project_url):
        raise BuildError(f"--project-url must be a plain https URL, not {project_url!r}")
    export_dir = find_export(export)
    version = check_export(export_dir, allow_dev=allow_dev)
    pin = parse_python_lock(PYTHON_LOCK)
    try:
        lock_text = REQUIREMENTS_LOCK.read_text(encoding="utf-8")
    except OSError as e:
        raise BuildError(f"requirements.lock cannot be read: {e}") from e
    requirements = select_groups(parse_requirements_lock(lock_text), groups)
    out = Path(out) if out else DIST
    cache = Path(cache) if cache else DIST / "installer-cache"
    work = Path(work) if work else DIST / f"installer-stage-{version}"
    wheel_dir = cache / "wheels"

    log(f"build_installer: {export_dir} (version {version}); bundling Python {pin['version']} and "
        f"{len(requirements)} wheels ({', '.join(groups)})")
    python_zip = fetch(pin["url"], cache / "python" / pin["file"], pin["sha256"], download=download, opener=opener)
    log(f"  python: {python_zip} (sha256 ok)")
    if download:
        wheel_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            req_file = Path(tmp) / "requirements.txt"
            req_file.write_text(render_requirements(requirements), encoding="utf-8")
            _run(pip_download_command(req_file, wheel_dir, pin["wheels"]), run, "pip download")
    problems = check_wheels(requirements, wheel_dir)
    if problems:
        raise BuildError(problems)
    log(f"  wheels: {len(requirements)} in {wheel_dir} (sha256 ok)")
    staged = stage(export_dir, work, python_zip, pin, requirements, wheel_dir, version=version,
                   project_url=project_url, groups=groups, run=run)
    log(f"  staged: {staged['work']} ({staged['files']} files outside python\\)")
    ccx_path = check_ccx(staged["app"], staged["work"] / "ccx-check", run=run)
    log(f"  panel: packaged as a check ({ccx_path.name}); the installer packages it again for its own folder")

    summary = {"version": version, "stage": str(staged["work"]), "ccx_check": str(ccx_path), "compiled": False}
    setup = out / SETUP_NAME.format(version=version)
    compiler = find_iscc(iscc)
    if compiler is not None:
        problems = iscc_problems(compiler)
        if problems:
            raise BuildError(problems)
        note = zh_note(compiler)
        if note:
            log("  note: " + note)
            summary["zhNote"] = note
    command = iscc_command(compiler or "ISCC.exe", staged["iss"], out)
    if compiler is None:
        log("  Inno Setup (ISCC.exe) not found: install Inno Setup 6.5+ or pass --iscc, then run:\n    "
            + subprocess.list2cmdline(command))
        summary["command"] = command
        if require_iscc:
            raise BuildError("ISCC.exe not found (--require-iscc)")
        return summary
    out.mkdir(parents=True, exist_ok=True)
    setup.unlink(missing_ok=True)
    _run(command, run, "ISCC")
    if not setup.is_file():
        raise BuildError(f"ISCC finished but {setup} is not there")
    digest_file = write_sha256(setup)
    summary.update(compiled=True, setup=str(setup), sha256=sha256_file(setup), sha256_file=str(digest_file))
    log(f"  wrote {setup} ({setup.stat().st_size // 1024} KB)\n  sha256 {summary['sha256']} ({digest_file.name})")
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="build_installer", description=__doc__.split("\n\n")[0])
    p.add_argument("export", help="the release export folder (ppro-agent-bridge-v<version> or the bridge folder in it)")
    p.add_argument("--download", action="store_true",
                   help="fetch what the cache lacks (python.org, PyPI); without it nothing touches the network")
    p.add_argument("--with-mcp", action="store_true", help="also bundle the MCP Python SDK (group mcp)")
    p.add_argument("--out", help="where the installer goes (default: dist/)")
    p.add_argument("--cache", help="download cache (default: dist/installer-cache)")
    p.add_argument("--work", help="staging folder (default: dist/installer-stage-<version>)")
    p.add_argument("--iscc", help="path of ISCC.exe")
    p.add_argument("--require-iscc", action="store_true", help="fail when ISCC.exe is not found (CI)")
    p.add_argument("--project-url", default="", help="https URL shown in Settings > Apps (the repository)")
    p.add_argument("--allow-dev", action="store_true", help="accept an export whose build.js says dev: true (tests)")
    args = p.parse_args(argv)
    groups = DEFAULT_GROUPS + (("mcp",) if args.with_mcp else ())
    try:
        build(args.export, download=args.download, groups=groups, out=args.out, cache=args.cache, work=args.work,
              iscc=args.iscc, require_iscc=args.require_iscc, project_url=args.project_url, allow_dev=args.allow_dev)
    except BuildError as e:
        print("build_installer: cannot build the installer:\n" + "\n".join(f"  - {x}" for x in e.problems),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
