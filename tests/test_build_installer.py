"""scripts/build_installer.py and installer/: the Windows installer's build, offline with dummy files (no download, no
pip, no Inno Setup: the commands are faked and checked), the locks, the bundled Python's ._pth, and the start scripts'
choice of Python."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ppro_bridge import doctor

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("_test_build_installer", REPO / "scripts" / "build_installer.py")
bi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bi)

ISS = (REPO / "installer" / "PremiereAgentBridge.iss").read_text(encoding="utf-8")
EMBED_PTH = "python312.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n"
PIN = {"version": "3.12.10", "url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embeddable-amd64.zip",
       "sha256": "", "pth": "python312._pth", "file": "python-3.12.10-embeddable-amd64.zip",
       "wheels": {"python_version": "3.12", "implementation": "cp", "abi": "cp312", "platform": "win_amd64"}}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, data="x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def make_export(root: Path, version="0.7.0", dev=False) -> Path:
    """A release export as export_release writes it: <root>/ppro-agent-bridge-v<version>/ with the two front .bat files
    and the bridge folder (only the files the installer relies on, plus a few for the file list)."""
    parent = root / f"ppro-agent-bridge-v{version}"
    app = parent / "ppro-agent-bridge"
    write(app / "VERSION", f"{version}\n")
    for rel in ("LICENSE", "start_bridge.bat", "setup_bridge.bat", "ppro_bridge/setup_ui.py", "ppro_bridge/doctor.py",
                "ppro_bridge/__init__.py", "ppro_bridge/mcp/server.py", "scripts/build_ccx.py", "broker/broker.py",
                "cli/ppro.py", "plugin/index.html", "plugin/lib/core.js", ".claude/skills/ppro-bridge/SKILL.md"):
        write(app / rel)
    write(app / "ppro_bridge" / "bridge.ico", b"\0\0\1\0")
    write(app / "plugin" / "manifest.json", json.dumps({"version": version}))
    write(app / "plugin" / "lib" / "build.js", f"module.exports = {{ dev: {'true' if dev else 'false'} }};\n")
    write(parent / "1 Install Premiere Agent Bridge.bat", "@echo off\r\n")
    write(parent / "2 Start Premiere Agent Bridge.bat", "@echo off\r\n")
    return parent


def make_embed_zip(path: Path, pth_name="python312._pth", extra=None) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name in ("python.exe", "pythonw.exe", "python312.dll", "python312.zip", "LICENSE.txt"):
            z.writestr(name, f"dummy {name}")
        z.writestr(pth_name, EMBED_PTH)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path.read_bytes()


def make_wheel(folder: Path, filename: str, payload=b"wheel") -> bytes:
    data = b"PK-dummy-" + filename.encode() + payload
    write(folder / filename, data)
    return data


class FakeTools:
    """Stands in for subprocess.run: pip install (fills --target with each requirement's dist-info and a bin folder),
    the export's build_ccx.py (writes a .ccx into --out), ISCC (writes the setup exe into /O). Records every command;
    anything else fails the test."""

    def __init__(self, test, iscc_writes=True):
        self.test, self.calls, self.iscc_writes = test, [], iscc_writes

    def __call__(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[1:4] == ["-m", "pip", "install"]:
            target = Path(cmd[cmd.index("--target") + 1])
            for line in Path(cmd[cmd.index("-r") + 1]).read_text(encoding="utf-8").splitlines():
                name, version = line.split()[0].split("==")
                (target / f"{name.replace('-', '_')}-{version}.dist-info").mkdir(parents=True)
            (target / "bin").mkdir(exist_ok=True)
            write(target / "bin" / "pip.exe", "launcher for the build machine's Python")
        elif cmd[1:4] == ["-m", "pip", "download"]:
            pass
        elif cmd[-3].endswith("build_ccx.py") or (len(cmd) > 3 and cmd[3].endswith("build_ccx.py")):
            out = Path(cmd[cmd.index("--out") + 1])
            write(out / "agent-bridge-0.7.0.ccx", b"PK")
        elif Path(cmd[0]).name.lower() == "iscc.exe":
            out = Path(next(c for c in cmd if c.startswith("/O"))[2:])
            if self.iscc_writes:
                write(out / "PremiereAgentBridge-Setup-0.7.0.exe", b"MZ fake setup")
        else:
            self.test.fail(f"unexpected command: {cmd}")
        return SimpleNamespace(returncode=0, stdout="ok\n")

    def ran(self, word):
        return [c for c in self.calls if word in " ".join(c)]


def no_network(*args, **kwargs):
    raise AssertionError("the build touched the network without --download")


class Workspace(unittest.TestCase):
    """A temporary root with an export, a cache filled with a dummy embeddable zip and the core wheels, and a lock pair
    pointing at them."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ppro_installer_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.export = make_export(self.root)
        self.cache = self.root / "cache"
        self.zip_bytes = make_embed_zip(self.cache / "python" / PIN["file"])
        self.pin = dict(PIN, sha256=sha(self.zip_bytes))
        wheels = self.cache / "wheels"
        ws = make_wheel(wheels, "websockets-16.1-cp312-cp312-win_amd64.whl")
        pip = make_wheel(wheels, "pip-25.3-py3-none-any.whl")
        self.lock = (f"# group: core\nwebsockets==16.1 \\\n    --hash=sha256:{sha(ws)}\n"
                     f"pip==25.3 \\\n    --hash=sha256:{sha(pip)}\n")
        self.python_lock = {"python": {"version": PIN["version"], "url": PIN["url"], "sha256": self.pin["sha256"],
                                       "pth": PIN["pth"]}, "wheels": PIN["wheels"]}
        write(self.root / "locks" / "python.lock.json", json.dumps(self.python_lock))
        write(self.root / "locks" / "requirements.lock", self.lock)
        for name, value in (("PYTHON_LOCK", self.root / "locks" / "python.lock.json"),
                            ("REQUIREMENTS_LOCK", self.root / "locks" / "requirements.lock")):
            patcher = mock.patch.object(bi, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, tools=None, **kw):
        """bi.build with the network and subprocess.run blocked; without an explicit iscc, as on a machine without
        Inno Setup (GitHub's Windows runners have one)."""
        kw.setdefault("cache", self.cache)
        kw.setdefault("out", self.root / "dist")
        kw.setdefault("work", self.root / "stage")
        find = mock.patch.object(bi, "find_iscc", return_value=None) if kw.get("iscc") is None else mock.MagicMock()
        with mock.patch.object(bi.urllib.request, "urlopen", no_network), \
                mock.patch.object(bi.subprocess, "run", no_network), find:
            return bi.build(self.export, run=tools or FakeTools(self), log=lambda *a: None, **kw)


# ---------------------------------------------------------------- the locks

class PythonLockTest(unittest.TestCase):
    def test_the_committed_lock_pins_a_real_hash_from_python_org(self):
        pin = bi.parse_python_lock(bi.PYTHON_LOCK)
        # 3.14: the line python.org still ships Windows binaries (and their security fixes) for (DOC-3)
        self.assertEqual(pin["version"], "3.14.10")
        self.assertEqual(pin["sha256"], "b553e7dac884469b40e0740bb527271c3378f700b427e0485835f1bb6424361c")
        self.assertTrue(pin["url"].startswith("https://www.python.org/ftp/python/3.14.10/"))
        self.assertEqual(pin["file"], pin["url"].rsplit("/", 1)[1])
        self.assertEqual(pin["pth"], "python314._pth")
        self.assertEqual(pin["wheels"], {"python_version": "3.14", "implementation": "cp", "abi": "cp314",
                                         "platform": "win_amd64"})

    def lock(self, **python):
        data = {"python": dict({"version": "3.12.10", "url": PIN["url"], "sha256": "ab" * 32, "pth": "python312._pth"},
                               **python), "wheels": dict(PIN["wheels"])}
        return data

    def problems(self, data):
        with self.assertRaises(bi.BuildError) as cm:
            bi.parse_python_lock(data)
        return " | ".join(cm.exception.problems)

    def test_placeholders_and_foreign_urls_are_refused(self):
        self.assertIn("python.sha256", self.problems(self.lock(sha256="<fill in>")))
        self.assertIn("python.sha256", self.problems(self.lock(sha256="0" * 64)))
        self.assertIn("https download from www.python.org",
                      self.problems(self.lock(url=PIN["url"].replace("https://", "http://"))))
        self.assertIn("https download from www.python.org",
                      self.problems(self.lock(url="https://mirror.example/python-3.12.10-embed-amd64.zip")))
        self.assertIn("embeddable amd64 zip", self.problems(self.lock(
            url="https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe")))
        self.assertIn("python312._pth", self.problems(self.lock(pth="python313._pth")))

    def test_the_wheel_settings_must_match_the_python(self):
        data = self.lock()
        data["wheels"].update(abi="cp313", python_version="3.13")
        text = self.problems(data)
        self.assertIn("wheels.abi", text)
        self.assertIn("wheels.python_version", text)


class RequirementsLockTest(unittest.TestCase):
    def test_the_committed_lock_has_core_and_mcp(self):
        reqs = bi.parse_requirements_lock(bi.REQUIREMENTS_LOCK.read_text(encoding="utf-8"))
        core = {r["name"]: r for r in bi.select_groups(reqs, ["core"])}
        self.assertEqual(set(core), {"websockets", "pip"})
        self.assertEqual(core["websockets"]["version"], "16.1")
        mcp = {r["name"] for r in bi.select_groups(reqs, ["mcp"])}
        self.assertTrue({"mcp", "pydantic", "pydantic-core", "anyio", "starlette", "pywin32"} <= mcp, mcp)
        self.assertFalse(mcp & set(core))
        for r in reqs:
            self.assertTrue(r["hashes"], r)
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", h) for h in r["hashes"]), r)
        # each requirement keeps its wheel's file name in the comment under it
        text = bi.REQUIREMENTS_LOCK.read_text(encoding="utf-8")
        self.assertIn("# websockets-16.1-cp314-cp314-win_amd64.whl", text)
        self.assertNotIn("cp312-win_amd64", text)  # every binary wheel is the bundled Python's (cp314)
        self.assertEqual({r["name"]: r["version"] for r in reqs}["pip"], "26.3")  # DOC-4: 25.3 has advisories

    def test_only_exact_pins_with_real_hashes(self):
        h = "ab" * 32
        bad = "\n".join([
            "websockets==1.0 --hash=sha256:" + h,  # before any group
            "# group: core",
            "requests>=2 --hash=sha256:" + h,
            "nohash==1.0",
            "placeholder==1.0 --hash=sha256:" + "0" * 64,
            "-r other.txt",
            "--index-url https://evil.example/simple",
            "pkg @ https://example.com/pkg.whl",
            "extras[x]==1.0 --hash=sha256:" + h,
            "dup==1.0 --hash=sha256:" + h,
            "dup==1.0 --hash=sha256:" + h,
            "md5==1.0 --hash=md5:" + "a" * 32,
        ])
        with self.assertRaises(bi.BuildError) as cm:
            bi.parse_requirements_lock(bad)
        text = " | ".join(cm.exception.problems)
        for word in ("before the first '# group: NAME'", "'requests>=2'", "nohash==1.0 has no --hash",
                     "placeholder has", "'-r'", "'--index-url'", "'pkg'", "'extras[x]==1.0'", "listed again",
                     "md5 has"):
            self.assertIn(word, text)

    def test_continuations_groups_and_render(self):
        h1, h2, h3 = "a1" * 32, "b2" * 32, "c3" * 32
        text = (f"# header\n# group: core\nalpha==1.0 \\\n    --hash=sha256:{h1} \\\n    --hash=sha256:{h2}\n"
                f"    # alpha-1.0-py3-none-any.whl\n\n# group: mcp\nBeta_Pkg==2.0 \\\n    --hash=sha256:{h3}\n")
        reqs = bi.parse_requirements_lock(text)
        self.assertEqual([(r["name"], r["version"], r["hashes"], r["group"]) for r in reqs],
                         [("alpha", "1.0", (h1, h2), "core"), ("Beta_Pkg", "2.0", (h3,), "mcp")])
        self.assertEqual([r["name"] for r in bi.select_groups(reqs, ["core"])], ["alpha"])
        with self.assertRaises(bi.BuildError):
            bi.select_groups(reqs, ["core", "gpu"])
        again = bi.parse_requirements_lock("# group: core\n" + bi.render_requirements(reqs))
        self.assertEqual([(r["name"], r["hashes"]) for r in again], [(r["name"], r["hashes"]) for r in reqs])


# ---------------------------------------------------------------- hashes and the network

class FetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ppro_fetch_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.url = "https://www.python.org/ftp/python/x.zip"

    def opener(self, data):
        calls = []

        def open_url(url, timeout=None):
            calls.append(url)
            return io.BytesIO(data)
        return open_url, calls

    def test_a_cached_file_is_used_only_when_it_matches(self):
        dest = write(self.tmp / "x.zip", b"good")
        opener, calls = self.opener(b"never")
        self.assertEqual(bi.fetch(self.url, dest, sha(b"good"), download=True, opener=opener), dest)
        self.assertEqual(calls, [])
        with self.assertRaises(bi.BuildError) as cm:
            bi.fetch(self.url, dest, sha(b"other"), download=True, opener=opener)
        self.assertIn("does not match the lock", cm.exception.problems[0])
        self.assertEqual(dest.read_bytes(), b"good")  # left there to look at, not replaced
        self.assertEqual(calls, [])

    def test_nothing_is_downloaded_without_the_flag(self):
        opener, calls = self.opener(b"data")
        with self.assertRaises(bi.BuildError) as cm:
            bi.fetch(self.url, self.tmp / "x.zip", sha(b"data"), download=False, opener=opener)
        self.assertIn("--download", cm.exception.problems[0])
        self.assertEqual(calls, [])
        self.assertFalse((self.tmp / "x.zip").exists())

    def test_a_download_is_kept_only_when_its_hash_matches(self):
        opener, calls = self.opener(b"tampered")
        with self.assertRaises(bi.BuildError) as cm:
            bi.fetch(self.url, self.tmp / "x.zip", sha(b"data"), download=True, opener=opener)
        self.assertIn("not used", cm.exception.problems[0])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [])  # no file, no .partial
        opener, calls = self.opener(b"data")
        dest = bi.fetch(self.url, self.tmp / "x.zip", sha(b"data"), download=True, opener=opener)
        self.assertEqual((dest.read_bytes(), calls), (b"data", [self.url]))

    def test_only_https(self):
        opener, calls = self.opener(b"data")
        with self.assertRaises(bi.BuildError):
            bi.fetch("http://www.python.org/x.zip", self.tmp / "y.zip", sha(b"data"), download=True, opener=opener)
        self.assertEqual(calls, [])


class WheelCacheTest(unittest.TestCase):
    def test_every_cached_wheel_of_a_locked_project_must_match(self):
        tmp = Path(tempfile.mkdtemp(prefix="ppro_wheels_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        good = make_wheel(tmp, "websockets-16.1-cp312-cp312-win_amd64.whl")
        make_wheel(tmp, "websockets-15.0-cp312-cp312-win_amd64.whl", b"other version: ignored")
        make_wheel(tmp, "unrelated-1.0-py3-none-any.whl")
        reqs = [{"name": "websockets", "version": "16.1", "hashes": (sha(good),), "group": "core"}]
        self.assertEqual(bi.check_wheels(reqs, tmp), [])
        make_wheel(tmp, "websockets-16.1-py3-none-any.whl", b"tampered")
        problems = bi.check_wheels(reqs, tmp)
        self.assertEqual(len(problems), 1)
        self.assertIn("websockets-16.1-py3-none-any.whl does not match requirements.lock", problems[0])
        missing = bi.check_wheels([{"name": "pip", "version": "25.3", "hashes": ("ab" * 32,), "group": "core"}], tmp)
        self.assertIn("run with --download", missing[0])


# ---------------------------------------------------------------- the bundled Python

class PthTest(unittest.TestCase):
    def test_site_packages_and_the_bridge_folder_are_added(self):
        text = bi.pth_text(EMBED_PTH)
        entries = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(entries, ["python312.zip", ".", "Lib\\site-packages", "..", "import site"])
        self.assertEqual(bi.pth_text(text), text)  # applying it twice changes nothing

    def test_a_pth_without_the_stdlib_zip_is_refused(self):
        with self.assertRaises(bi.BuildError):
            bi.pth_text(".\n#import site\n")

    def test_extract_checks_the_package_and_stays_inside(self):
        tmp = Path(tempfile.mkdtemp(prefix="ppro_embed_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        make_embed_zip(tmp / "ok.zip")
        pth = bi.extract_python(tmp / "ok.zip", tmp / "py", "python312._pth")
        self.assertIn("..", pth.read_text(encoding="utf-8").splitlines())
        self.assertTrue((tmp / "py" / "Lib" / "site-packages").is_dir())
        make_embed_zip(tmp / "other.zip", pth_name="python313._pth")
        with self.assertRaises(bi.BuildError):
            bi.extract_python(tmp / "other.zip", tmp / "py2", "python312._pth")
        make_embed_zip(tmp / "slip.zip", extra={"../evil.txt": "x"})
        with self.assertRaises(bi.BuildError):
            bi.extract_python(tmp / "slip.zip", tmp / "py3", "python312._pth")
        self.assertFalse((tmp / "evil.txt").exists())


# ---------------------------------------------------------------- the export and the staging

class ExportTest(Workspace):
    def problems(self, **kw):
        with self.assertRaises(bi.BuildError) as cm:
            bi.check_export(bi.find_export(self.export), **kw)
        return " | ".join(cm.exception.problems)

    def test_a_clean_release_export_passes(self):
        app = bi.find_export(self.export)
        self.assertEqual(app.name, "ppro-agent-bridge")
        self.assertEqual(bi.find_export(app), app)
        self.assertEqual(bi.check_export(app), "0.7.0")

    def test_dev_builds_caches_and_missing_files_are_refused(self):
        app = bi.find_export(self.export)
        write(app / "plugin" / "lib" / "build.js", "module.exports = { dev: true };\n")
        self.assertIn("dev: true", self.problems())
        self.assertEqual(bi.check_export(app, allow_dev=True), "0.7.0")
        write(app / "ppro_bridge" / "__pycache__" / "x.pyc")
        write(app / "python" / "python.exe")
        (app / "LICENSE").unlink()
        write(app / "plugin" / "manifest.json", json.dumps({"version": "0.6.0"}))
        text = self.problems(allow_dev=True)
        for word in ("ppro_bridge/__pycache__/", "python/", "LICENSE is missing", "says version '0.6.0'"):
            self.assertIn(word, text)

    def test_not_an_export(self):
        with self.assertRaises(bi.BuildError):
            bi.find_export(self.root / "nowhere")

    def test_a_folder_it_did_not_make_is_never_emptied(self):
        foreign = write(self.root / "mine" / "keep.txt").parent
        with self.assertRaises(bi.BuildError):
            bi.prepare_work(foreign)
        self.assertTrue((foreign / "keep.txt").is_file())
        work = bi.prepare_work(self.root / "w")
        write(work / "old.txt")
        bi.prepare_work(work)
        self.assertFalse((work / "old.txt").exists())


class StagingTest(Workspace):
    def test_the_staging_layout(self):
        tools = FakeTools(self)
        summary = self.build(tools, iscc=None, project_url="https://example.org/bridge")
        stage = self.root / "stage"
        app = stage / "app"
        self.assertEqual(summary["stage"], str(stage))
        self.assertFalse(summary["compiled"])
        # the export, without the front .bat files the shortcuts replace
        self.assertTrue((app / "start_bridge.bat").is_file())
        self.assertFalse(any(p.name.startswith(("1 Install", "2 Start")) for p in app.rglob("*")))
        # the bundled Python, its ._pth, the wheels (without the build machine's launchers)
        py = app / "python"
        self.assertTrue((py / "python.exe").is_file() and (py / "pythonw.exe").is_file())
        pth = [ln for ln in (py / "python312._pth").read_text(encoding="utf-8").splitlines()
               if ln and not ln.startswith("#")]
        self.assertEqual(pth, ["python312.zip", ".", "Lib\\site-packages", "..", "import site"])
        site = py / "Lib" / "site-packages"
        self.assertTrue((site / "websockets-16.1.dist-info").is_dir() and (site / "pip-25.3.dist-info").is_dir())
        self.assertFalse((site / "bin").exists())
        # the file list: everything outside python\, Windows separators, itself included
        listed = (app / bi.FILE_LIST).read_bytes().decode("ascii").split("\r\n")
        self.assertIn("start_bridge.bat", listed)
        self.assertIn("plugin\\lib\\build.js", listed)
        self.assertIn(".claude\\skills\\ppro-bridge\\SKILL.md", listed)
        self.assertIn(bi.FILE_LIST, listed)
        self.assertFalse([x for x in listed if x.lower().startswith("python\\")])
        # the script and its generated includes
        self.assertEqual((stage / bi.ISS_NAME).read_text(encoding="utf-8"), ISS)
        generated = (stage / "generated.iss").read_text(encoding="utf-8")
        self.assertIn('#define AppVersion "0.7.0"', generated)
        self.assertIn('#define PythonVersion "3.12.10"', generated)
        self.assertIn('#define ProjectUrl "https://example.org/bridge"', generated)
        uninstall = (stage / "generated_uninstall.iss").read_text(encoding="utf-8")
        self.assertIn('Type: filesandordirs; Name: "{app}\\ppro_bridge\\__pycache__"', uninstall)
        self.assertIn('Name: "{app}\\ppro_bridge\\mcp\\__pycache__"', uninstall)
        self.assertNotIn("python\\", uninstall)
        # pip ran once, offline, hash-checked, into the bundled Python; the panel was packaged as a check
        pip = tools.ran("pip install")
        self.assertEqual(len(pip), 1)
        for flag in ("--no-index", "--require-hashes", "--isolated", "--no-compile"):
            self.assertIn(flag, pip[0])
        self.assertEqual(pip[0][pip[0].index("--target") + 1], str(site))
        self.assertEqual(pip[0][pip[0].index("--platform") + 1], "win_amd64")
        self.assertEqual(tools.ran("pip download"), [])
        ccx = tools.ran("build_ccx.py")
        self.assertEqual(len(ccx), 1)
        self.assertIn("-I", ccx[0])
        self.assertTrue(summary["ccx_check"].endswith(".ccx"))
        # without ISCC the command is printed
        self.assertEqual(summary["command"][-1], str(stage / bi.ISS_NAME))
        self.assertIn(f"/O{self.root / 'dist'}", summary["command"])

    def test_iscc_compiles_and_the_sha256_is_written(self):
        iscc = write(self.root / "inno" / "ISCC.exe", b"MZ")
        write(self.root / "inno" / "Languages" / "ChineseSimplified.isl", "; zh")
        summary = self.build(iscc=str(iscc))
        setup = self.root / "dist" / "PremiereAgentBridge-Setup-0.7.0.exe"
        self.assertTrue(summary["compiled"])
        self.assertEqual(summary["setup"], str(setup))
        self.assertEqual((self.root / "dist" / (setup.name + ".sha256")).read_text(encoding="ascii"),
                         f"{sha(b'MZ fake setup')} *{setup.name}\n")

    def test_an_inno_setup_that_writes_no_output_is_refused(self):
        # no ChineseSimplified.isl is fine: Inno Setup 6.7.1 ships none (live, 6 Nov 2026) and the script falls back
        iscc = write(self.root / "old-inno" / "ISCC.exe", b"MZ")
        with mock.patch.object(bi, "INSTALLER_DIR", self.root / "no-installer-dir"):
            self.assertIsNotNone(bi.zh_note(iscc))  # nothing bundled either: the build only notes it
        self.assertIsNone(bi.zh_note(iscc))  # the repo bundles installer/ChineseSimplified.isl
        with self.assertRaises(bi.BuildError) as cm:
            self.build(FakeTools(self, iscc_writes=False), iscc=str(iscc))
        self.assertIn("is not there", cm.exception.problems[0])

    def test_require_iscc(self):
        with self.assertRaises(bi.BuildError) as cm:
            self.build(require_iscc=True)
        self.assertIn("--require-iscc", cm.exception.problems[0])

    def test_a_project_url_with_quotes_or_braces_is_refused(self):
        for url in ('https://example.org/"x', "https://example.org/{app}", "http://example.org", "javascript:x"):
            with self.assertRaises(bi.BuildError, msg=url):
                self.build(project_url=url, iscc=None)

    def test_a_group_the_lock_lacks_is_refused(self):
        with self.assertRaises(bi.BuildError) as cm:
            self.build(groups=("core", "mcp"))
        self.assertIn("no group mcp", cm.exception.problems[0])


class NoNetworkTest(Workspace):
    def test_an_empty_cache_without_the_flag_stops_before_any_download(self):
        shutil.rmtree(self.cache)
        tools = FakeTools(self)
        with self.assertRaises(bi.BuildError) as cm:
            self.build(tools)
        self.assertIn("--download", cm.exception.problems[0])
        self.assertEqual(tools.calls, [])
        write(self.cache / "python" / PIN["file"], self.zip_bytes)  # the Python is there, the wheels are not
        with self.assertRaises(bi.BuildError) as cm:
            self.build(tools)
        self.assertEqual(len(cm.exception.problems), 2)
        self.assertTrue(all("run with --download" in p for p in cm.exception.problems), cm.exception.problems)
        self.assertEqual(tools.calls, [])  # no pip download, no pip install

    def test_with_the_flag_pip_downloads_from_pypi_with_hashes(self):
        tools = FakeTools(self)
        self.build(tools, download=True)
        download = tools.ran("pip download")
        self.assertEqual(len(download), 1)
        for flag in ("--require-hashes", "--isolated", "--only-binary"):
            self.assertIn(flag, download[0])
        self.assertEqual(download[0][download[0].index("--index-url") + 1], "https://pypi.org/simple")

    def test_main_reports_every_problem(self):
        shutil.rmtree(self.cache)
        err = io.StringIO()
        with mock.patch.object(bi.urllib.request, "urlopen", no_network), \
                mock.patch.object(bi.subprocess, "run", no_network), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch.object(bi, "DIST", self.root / "dist"):
            self.assertEqual(bi.main([str(self.export)]), 1)
        self.assertIn("--download", err.getvalue())
        self.assertFalse((self.root / "dist" / "installer-cache").exists())


class RealCcxCheckTest(unittest.TestCase):
    """check_ccx runs the export's own scripts/build_ccx.py (isolated, no bytecode written into the staging)."""

    def test_the_repo_panel_packages_from_a_staged_copy(self):
        tmp = Path(tempfile.mkdtemp(prefix="ppro_ccxcheck_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        app = tmp / "app"
        shutil.copytree(REPO / "plugin", app / "plugin", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO / "ppro_bridge", app / "ppro_bridge", ignore=shutil.ignore_patterns("__pycache__"))
        for rel in ("VERSION", "start_bridge.bat", "start_bridge.pyw", "scripts/build_ccx.py"):
            write(app / rel, (REPO / rel).read_bytes())
        build_js = app / "plugin" / "lib" / "build.js"
        build_js.write_text(re.sub(r"\bdev\s*:\s*true\b", "dev: false", build_js.read_text(encoding="utf-8")),
                            encoding="utf-8")
        ccx = bi.check_ccx(app, tmp / "ccx-check")
        self.assertTrue(ccx.is_file())
        self.assertEqual(list(app.rglob("__pycache__")), [])
        with zipfile.ZipFile(ccx) as z:
            self.assertEqual(json.loads(z.read("launcher.json")), {"path": str((app / "start_bridge.bat").resolve())})


# ---------------------------------------------------------------- the installer script

class IssTest(unittest.TestCase):
    def section(self, name):
        m = re.search(r"^\[" + re.escape(name) + r"\]\n(.*?)(?=^\[|\Z)", ISS, re.M | re.S)
        self.assertIsNotNone(m, name)
        return m.group(1)

    def test_per_user_without_admin_in_local_programs(self):
        setup = self.section("Setup")
        self.assertIn("PrivilegesRequired=lowest", setup)
        self.assertIn("DefaultDirName={userpf}\\{#AppName}", setup)
        self.assertNotIn("PrivilegesRequiredOverridesAllowed", setup)
        self.assertRegex(setup, r"AppId=\{\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}")
        self.assertIn("SetupIconFile=app\\ppro_bridge\\bridge.ico", setup)
        self.assertIn("AppVersion={#AppVersion}", setup)

    def test_english_and_simplified_chinese(self):
        self.assertIn('Name: "zh"; MessagesFile: "{#ZhMessages}"', ISS)
        for line in ('#define ZhMessages "compiler:Languages\\ChineseSimplified.isl"',
                     '#define ZhMessages "ChineseSimplified.isl"', '#define ZhMessages "compiler:Default.isl"',
                     "zh.LanguageName=简体中文"):
            self.assertIn(line, ISS)
        self.assertIn("#if Ver < EncodeVer(6, 5, 0, 0)", ISS)
        builtin = {"CreateDesktopIcon", "AdditionalIcons"}
        used = set(re.findall(r"\{cm:(\w+)\}", ISS)) | set(re.findall(r"CustomMessage\('(\w+)'\)", ISS))
        messages = self.section("CustomMessages")
        for name in used - builtin:
            self.assertIn(f"\nen.{name}=", "\n" + messages, name)
            self.assertIn(f"\nzh.{name}=", "\n" + messages, name)

    def test_it_includes_what_the_build_generates(self):
        self.assertIn('#include "generated.iss"', ISS)
        self.assertIn('#include "generated_uninstall.iss"', self.section("UninstallDelete"))
        self.assertIn(f'#define FileList "{bi.FILE_LIST}"', ISS)
        generated = bi.generated_iss("0.7.0", "3.12.10", "", ["core"])
        for name in re.findall(r"\{#(\w+)\}", ISS):
            self.assertTrue(f"#define {name} " in generated or f'#define {name} "' in ISS, name)

    def test_shortcuts_match_the_setup_window(self):
        icons = self.section("Icons")
        name = doctor.SHORTCUT_NAME[:-len(".lnk")]
        self.assertIn("#define AppName " + json.dumps(name), ISS)
        for folder in ("{userprograms}", "{userdesktop}", "{userstartup}"):
            line = next(ln for ln in icons.splitlines() if ln.startswith(f'Name: "{folder}\\{{#AppName}}";'))
            self.assertIn('Filename: "{app}\\start_bridge.bat"; WorkingDir: "{app}"', line)
            self.assertIn('IconFilename: "{app}\\ppro_bridge\\bridge.ico"', line)
        startup = next(ln for ln in icons.splitlines() if "{userstartup}" in ln)
        self.assertIn("runminimized", startup)  # as doctor's autostart (WindowStyle 7)
        self.assertRegex(self.section("Tasks"), r'Name: "autostart";.*Flags: unchecked')
        self.assertIn('Parameters: "-I -m ppro_bridge.setup_ui"', self.section("Run"))
        self.assertIn("postinstall", self.section("Run"))

    def test_uninstall_keeps_the_bridge_data_unless_asked(self):
        code = self.section("Code")
        self.assertIn("'.ppro-agent-bridge'", code)
        self.assertIn("MB_DEFBUTTON2", code)
        self.assertIn("not UninstallSilent()", code)
        self.assertRegex(code, r"\(CurUninstallStep = usPostUninstall\) and DeleteBridgeData")
        self.assertEqual(code.count("DelTree("), 1)
        self.assertNotIn(".ppro-agent-bridge", self.section("UninstallDelete"))

    def test_the_update_check_is_named_and_can_be_turned_off(self):
        """DOC-5: a task, ticked by default, names the daily request; unticked, settings.json says updateCheck false."""
        tasks = self.section("Tasks")
        line = next(ln for ln in tasks.splitlines() if ln.startswith('Name: "updatecheck"'))
        self.assertNotIn("unchecked", line)
        run = next(ln for ln in self.section("Run").splitlines() if "Tasks: not updatecheck" in ln)
        code = re.search(r'-c ""(.+?)"""', run).group(1)
        self.assertIn("update.write_setting('updateCheck', False)", code)
        with tempfile.TemporaryDirectory() as tmp:  # the same code against a settings.json of a temp folder
            (Path(tmp) / "settings.json").write_text('{"skipVersion": "0.7.1"}', encoding="utf-8")
            subprocess.run([sys.executable, "-c", code], cwd=str(REPO), check=True, timeout=60,
                           env=dict(os.environ, PPRO_SETTINGS_DIR=tmp, PYTHONPATH=str(REPO)))
            self.assertEqual(json.loads((Path(tmp) / "settings.json").read_text(encoding="utf-8")),
                             {"skipVersion": "0.7.1", "updateCheck": False})
        for lang in ("en", "zh"):
            self.assertIn(f"{lang}.TaskUpdateCheck=", self.section("CustomMessages"))

    def test_uninstall_removes_python_and_dist_whole_only_from_a_folder_of_ours(self):
        lines = self.section("UninstallDelete").splitlines()
        for name in ("python", "dist"):
            entry = next(ln for ln in lines if f'Name: "{{app}}\\{name}"' in ln)
            self.assertIn("Check: AppFolderWasOurs", entry)
        code = self.section("Code")
        self.assertIn("AppFolderOurs := AppFolderEmpty() or OurInstallIsHere();", code)
        self.assertLess(code.index("AppFolderOurs := "), code.index("RemoveDroppedFiles();\n  end;"))

    def test_upgrades_replace_python_and_drop_old_files(self):
        self.assertIn('Type: filesandordirs; Name: "{app}\\python"; Check: OurInstallIsHere',
                      self.section("InstallDelete"))
        self.assertIn("RemoveDroppedFiles", self.section("Code"))
        self.assertIn('Source: "app\\{#FileList}"; Flags: dontcopy', self.section("Files"))


# ---------------------------------------------------------------- the release workflow

class ReleaseWorkflowTest(unittest.TestCase):
    TEXT = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    def test_every_action_is_pinned_to_a_commit(self):
        """DOC-1: a tag can be moved; a full commit SHA cannot."""
        uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", self.TEXT, re.M)
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", ref)

    def test_the_suite_runs_under_the_bundled_python_before_anything_is_uploaded(self):
        """QA-2: the interpreter every installed user runs is tested in the release build itself."""
        build = self.TEXT.split("\n  sign:\n", 1)[0]
        step = build.index("name: Run the test suite under the bundled Python")
        self.assertLess(build.index("name: Build the installer"), step)
        self.assertLess(step, build.index("name: Upload the unsigned installer"))
        body = build[step:build.index("name: Upload the unsigned installer")]
        self.assertIn('py="dist/installer-stage-$VERSION/app/python/python.exe"', body)
        self.assertIn('"$py" -I -m unittest discover -s tests', body)
        self.assertIn("(3, 14)", body)
        lock = bi.REQUIREMENTS_LOCK.read_text(encoding="utf-8")
        self.assertIn("Run the test suite", lock)  # the lock's comment says where 3.14 is tested
        self.assertNotIn("Not yet installed and tested", lock)

    def test_only_a_version_tag_is_signed_and_only_the_sign_job_has_the_token(self):
        """DOC-2: a manual run on a branch builds, never signs; the token lives in the release environment."""
        build, sign = self.TEXT.split("\n  sign:\n", 1)
        self.assertNotIn("SIGNPATH_API_TOKEN }}", build)
        self.assertIn("if: github.ref_type == 'tag' && vars.SIGNPATH_PROJECT_SLUG != ''", build)
        self.assertIn("environment: release", sign)
        self.assertRegex(sign, r"if: github.ref_type == 'tag' &&")
        self.assertIn("permissions: {}", self.TEXT)


# ---------------------------------------------------------------- the start scripts

class StartScriptsTest(unittest.TestCase):
    CHOICE = ('set "PAB_PYTHON=python"', 'if exist "%~dp0python\\python.exe" set "PAB_PYTHON=%~dp0python\\python.exe"')

    def test_both_scripts_prefer_the_bundled_python(self):
        for name in ("start_bridge.bat", "setup_bridge.bat"):
            text = (REPO / name).read_text(encoding="utf-8")
            for line in self.CHOICE:
                self.assertIn(line + "\n", text, name)
            bare = [ln for ln in text.splitlines() if re.match(r"\s*python\s", ln)]
            self.assertEqual(bare, [], f"{name} runs python from PATH directly: {bare}")
        self.assertIn('"%PAB_PYTHON%" %PAB_FLAGS% broker.py --load-panel',
                      (REPO / "start_bridge.bat").read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "cmd.exe")
    def test_cmd_picks_python_next_to_the_script_else_the_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="ppro bat (x) "))
        self.addCleanup(shutil.rmtree, tmp, True)
        bat = tmp / "choose.bat"
        bat.write_text("@echo off\n" + "\n".join(self.CHOICE) + "\necho PAB=%PAB_PYTHON%\n", encoding="ascii")

        def chosen():
            out = subprocess.run(["cmd", "/d", "/c", str(bat)], capture_output=True, text=True, timeout=30,
                                 cwd=str(tmp))
            return out.stdout.strip()
        self.assertEqual(chosen(), "PAB=python")
        write(tmp / "python" / "python.exe", b"MZ")
        self.assertEqual(chosen(), f"PAB={tmp}\\python\\python.exe")



class BundledPythonIsolatedTest(unittest.TestCase):
    """QA-8: the installer's Python turns site on through its ._pth, which adds the per-user
    site-packages folder: every start of it must be isolated (-I)."""

    def test_every_bundled_python_command_line_is_isolated(self):
        bad = []
        for rel in ("installer/PremiereAgentBridge.iss", "start_bridge.bat", "setup_bridge.bat"):
            for n, line in enumerate((REPO / rel).read_text(encoding="utf-8").splitlines(), 1):
                code = line.split(";", 1)[0] if rel.endswith(".iss") and line.lstrip().startswith(";") else line
                if code.lstrip().lower().startswith(("rem", ";", "if exist", "set ")):
                    continue
                if re.search(r'python\\pythonw?\.exe"|%PAB_PYTHON%"|%PYW%"', code):
                    if rel.endswith(".iss"):
                        ok = re.search(r'Parameters: "-I[ "]', code) is not None
                    else:
                        ok = "%PAB_FLAGS%" in code
                    if not ok:
                        bad.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(bad, [])
        for rel in ("start_bridge.bat", "setup_bridge.bat"):
            text = (REPO / rel).read_text(encoding="utf-8")
            self.assertIn('if exist "%~dp0python\\python.exe" set "PAB_FLAGS=-I"', text)

    def test_python_started_again_from_the_bridge_gets_the_flags(self):
        bad = []
        for path in sorted((REPO / "ppro_bridge").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"\[sys\.executable,([^\]]*)", text):
                if "isolated_flags" not in m.group(1):
                    bad.append(f"{path.relative_to(REPO)}: {m.group(0)[:80]}")
        self.assertEqual(bad, [])

    def test_isolated_flags_only_for_the_bundled_python(self):
        from ppro_bridge import console
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "python.exe"
            exe.write_bytes(b"MZ")
            self.assertEqual(console.isolated_flags(exe), [])  # a Python of the user's own: its user site may matter
            (Path(tmp) / "python314._pth").write_text("python314.zip\n.\nimport site\n", encoding="utf-8")
            self.assertEqual(console.isolated_flags(exe), ["-I"])

if __name__ == "__main__":
    unittest.main()
