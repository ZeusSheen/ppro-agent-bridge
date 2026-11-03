"""Package the panel as a .ccx, which Creative Cloud's plugin installer (UPIA) installs without UXP Developer Tool.

    python scripts/build_ccx.py [--out DIR] [--dev] [--id ID --name NAME]

Writes <out>/agent-bridge-<VERSION>.ccx (default out: dist/); ppro_bridge/ccx.py says what goes in and what changes
on the way (`host` as one object, build.js `dev: false, packaged: true` - dev stays true with --dev - and
launcher.json naming this checkout's start_bridge.bat). Refuses, writing nothing, when VERSION and the manifest
version differ, the manifest's network rule is not exactly ["ws://localhost/"], its launchProcess may not start a
.bat, plugin/lib/build.js does not hold the one dev flag, or start_bridge.bat is missing; every reason is listed.
The zip is read back before it gets its name: no entry may use a data descriptor (general-purpose flag bit 3, which
Adobe's unpackager cannot read; Python's zipfile writing to a real file never sets it), every CRC must match, the
manifest must parse with one host object, build.js must say the flags asked for, launcher.json must name
start_bridge.bat, and every file main.js, index.html and the lib modules load must be in it. Prints the UPIA command
that installs it; runs nothing. --id and --name make a test package with another plugin id and name (and menu label),
so UXP does not take it for the panel UXP Developer Tool loads; the panel still introduces itself to the broker as
the Agent Bridge, whose one-live-panel rule applies to both.
"""

import argparse
import json
import posixpath
import re
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ppro_bridge import ccx, doctor  # noqa: E402  (after the path fix: runnable as a plain script)

DATA_DESCRIPTOR = 0x08  # general-purpose flag bit 3
LOCAL_HEADER = b"PK\x03\x04"
LOCAL_REQUIRE = re.compile(r"""require\(\s*["'](\.{1,2}/[^"']*)["']\s*\)""")
HTML_REF = re.compile(r"""<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _entries(files: dict, launcher: bytes) -> list:
    """(name, bytes or None for a folder) in zip order: manifest.json first, each folder before its files."""
    out, folders = [("manifest.json", files["manifest.json"])], set()
    for name in sorted(n for n in files if n != "manifest.json"):
        parts = name.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            folder = "/".join(parts[:i]) + "/"
            if folder not in folders:
                folders.add(folder)
                out.append((folder, None))
        out.append((name, files[name]))
    out.append((ccx.LAUNCHER, launcher))
    return out


def write_zip(path: Path, entries: list) -> None:
    """A plain ZIP: deflated files, folders as entries, no data descriptors (writestr on a seekable file)."""
    stamp = time.localtime()[:6]
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, stamp)
            info.create_system = 0  # MS-DOS attributes (folder 0x10, archive 0x20), the same on every OS
            info.external_attr = 0x10 if data is None else 0x20  # never 0: writestr would add Unix bits then
            info.compress_type = zipfile.ZIP_STORED if data is None else zipfile.ZIP_DEFLATED
            zf.writestr(info, b"" if data is None else data)


def verify(path, *, dev: bool, start_script) -> list[str]:
    """What is wrong with a written package (empty: nothing)."""
    problems = []
    with open(path, "rb") as raw, zipfile.ZipFile(raw) as zf:
        infos = zf.infolist()
        for info in infos:
            raw.seek(info.header_offset)
            head = raw.read(30)
            if head[:4] != LOCAL_HEADER:
                problems.append(f"{info.filename}: no local file header where the directory says")
            elif (info.flag_bits | int.from_bytes(head[6:8], "little")) & DATA_DESCRIPTOR:
                problems.append(f"{info.filename}: uses a data descriptor (flag bit 3), which UPIA cannot unpack")
        bad = zf.testzip()
        if bad:
            problems.append(f"{bad}: CRC mismatch")
        names = {i.filename for i in infos if not i.is_dir()}

        def text(name):
            return zf.read(name).decode("utf-8-sig")

        try:
            manifest = json.loads(text("manifest.json"))
        except (KeyError, ValueError) as e:
            return problems + [f"manifest.json is missing or does not parse: {e}"]
        if not isinstance(manifest.get("host"), dict):
            problems.append(f"manifest.json: host is {json.dumps(manifest.get('host'))}, not one object")
        problems += [f"manifest.json: {p}" for p in ccx.manifest_problems(manifest)]
        main_file = manifest.get("main") or "index.html"
        if main_file not in names:
            problems.append(f"manifest.json names main {main_file!r}, which the package lacks")
        if ccx.BUILD_JS not in names:
            problems.append(f"{ccx.BUILD_JS} is missing")
        elif ccx.build_flags(text(ccx.BUILD_JS)) != (dev, True):
            problems.append(f"{ccx.BUILD_JS} does not say dev: {'true' if dev else 'false'}, packaged: true")
        try:
            launcher = json.loads(text(ccx.LAUNCHER))
        except (KeyError, ValueError) as e:
            launcher = None
            problems.append(f"{ccx.LAUNCHER} is missing or does not parse: {e}")
        if launcher is not None and launcher != {"path": str(Path(start_script).resolve())}:
            problems.append(f"{ccx.LAUNCHER} is {json.dumps(launcher)}, not the path of {start_script}")
        for name in sorted(names):
            if name.endswith(".js"):
                wanted = [(t, posixpath.normpath(posixpath.join(posixpath.dirname(name), t)))
                          for t in LOCAL_REQUIRE.findall(text(name))]
            elif name == main_file:
                wanted = [(t, posixpath.normpath(t.split("?")[0].lstrip("/"))) for t in HTML_REF.findall(text(name))
                          if "://" not in t and not t.startswith(("data:", "#", "//"))]
            else:
                continue
            problems += [f"{name} loads {target!r}, which the package lacks" for target, rel in wanted
                         if rel not in names]
    return problems


def build(repo=REPO, out_dir=None, dev=False, plugin_id=None, name=None) -> dict:
    """Write and verify the package; {path, files, bytes, dev, launcher, name, id, warnings}. ccx.PackageError (every
    reason) when the checkout cannot be packaged or the written zip fails verification (nothing is left behind then)."""
    repo = Path(repo)
    plugin = repo / "plugin"
    start_script = repo / "start_bridge.bat"
    problems = []
    try:
        version = (repo / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as e:
        version = None
        problems.append(f"VERSION cannot be read: {e}")
    try:
        manifest = ccx.read_manifest(plugin)
    except (OSError, ValueError) as e:
        raise ccx.PackageError(problems + [f"plugin/manifest.json cannot be read: {e}"]) from e
    if version and manifest.get("version") != version:
        problems.append(f"plugin/manifest.json says version {manifest.get('version')!r}, VERSION says "
                        f"{version!r}: run python scripts/sync_version.py")
    if not start_script.is_file():
        problems.append(f"{start_script} is missing: launcher.json must name it")
    try:
        files = ccx.package_files(plugin, dev=dev, plugin_id=plugin_id, name=name)
        manifest = json.loads(files["manifest.json"])
    except ccx.PackageError as e:
        problems += e.problems
    if problems:
        raise ccx.PackageError(problems)
    warnings = []
    if not (repo / "start_bridge.pyw").is_file():
        warnings.append("this checkout has no start_bridge.pyw, the launcher the panel falls back to when UXP "
                        "refuses to run the .bat")

    out_dir = Path(out_dir) if out_dir else repo / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ccx.package_name(manifest, version)
    partial = path.with_name(path.name + ".partial")
    entries = _entries(files, ccx.launcher_bytes(start_script))
    try:
        write_zip(partial, entries)
        problems = verify(partial, dev=dev, start_script=start_script)
        if problems:
            raise ccx.PackageError([f"the written package is wrong: {p}" for p in problems])
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    return {"path": path, "files": sorted(n for n, data in entries if data is not None), "bytes": path.stat().st_size,
            "dev": dev, "launcher": str(start_script.resolve()), "name": manifest.get("name"), "id": manifest.get("id"),
            "warnings": warnings}


def upia_command(*args) -> str:
    """A UPIA command line to copy (nothing here runs it)."""
    exe = doctor.find_upia() or doctor.UPIA_DEFAULT
    return " ".join([f'"{exe}"', *args])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="build_ccx", description=__doc__.split("\n\n")[0])
    p.add_argument("--out", help="folder for the .ccx (default: dist/ in this checkout)")
    p.add_argument("--dev", action="store_true", help="keep the dev build flag (the panel registers api_smoke)")
    p.add_argument("--id", help="a test package's plugin id (default: the manifest's)")
    p.add_argument("--name", help="a test package's name and menu label (default: the manifest's)")
    args = p.parse_args(argv)
    if bool(args.id) != bool(args.name):
        p.error("--id and --name go together (a test package needs both, or UXP mixes it up with the panel)")
    try:
        done = build(out_dir=args.out, dev=args.dev, plugin_id=args.id, name=args.name)
    except ccx.PackageError as e:
        print("build_ccx: cannot package the panel:\n" + "\n".join(f"  - {x}" for x in e.problems), file=sys.stderr)
        return 1
    path = done["path"]
    print(f"build_ccx: wrote {path} ({len(done['files'])} files, {done['bytes'] // 1024} KB, "
          f"dev flag {'true' if done['dev'] else 'false'}; launcher.json -> {done['launcher']})")
    for w in done["warnings"]:
        print(f"  warning: {w}")
    package, name = f'"{path}"', f'"{done["name"]}"'
    print("\nInstall it with Premiere Pro closed (not run here):\n  " + upia_command("/install", package))
    print("Upgrade: remove the old one first, also with Premiere closed (installing over it does not reload it):\n  "
          + upia_command("/remove", name) + "\n  " + upia_command("/install", package))
    if args.id:
        print(f"Test package {done['id']} ({done['name']}): UXP keeps it apart from the panel UXP Developer Tool "
              "loads, but the broker still takes one live Agent Bridge panel at a time.")
    else:
        print(f"Same plugin id ({done['id']}) as the panel UXP Developer Tool loads from plugin/: unload that one "
              "first (start_bridge.bat's loader leaves a connected packaged panel alone).")
    if doctor.find_upia() is None:
        print("UPIA was not found: a .ccx install needs the Creative Cloud desktop app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
