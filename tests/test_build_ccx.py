"""scripts/build_ccx.py and ppro_bridge/ccx.py: the .ccx package of the panel, built from a temporary copy of this
checkout (nothing is written into the repo, nothing is installed), and ping's stale check for a packaged panel."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from helpers import ping_reply, stub_rpc
from ppro_bridge import ccx, ops, panelhash

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("_test_build_ccx", REPO / "scripts" / "build_ccx.py")
build_ccx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_ccx)

# plugin/lib/build.js as the build-flag slice writes it (one `dev: true`, no packaged key)
BUILD_JS = ("// The build flavour, read by main.js. This repository is a dev build: the panel registers the handlers\n"
            "// that test Premiere by changing it (api_smoke). Release exports and .ccx packages set the flag to "
            "false.\nmodule.exports = { dev: true };\n")
LAUNCH = {"schemes": [], "extensions": [".bat", ".pyw"]}


def _merged(manifest: dict) -> bool:
    perms = manifest.get("requiredPermissions", {})
    return (perms.get("network", {}).get("domains") == ccx.NETWORK_DOMAINS
            and ".bat" in (perms.get("launchProcess") or {}).get("extensions", []))


def checkout(test, **manifest_over) -> Path:
    """A temporary checkout for `test`: VERSION, start_bridge.bat and a copy of plugin/ with the agreed network rule,
    launchProcess and build.js (no-ops once those are in the repo), plus `manifest_over`."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name) / "checkout"
    shutil.copytree(REPO / "plugin", root / "plugin")
    for name in ("VERSION", "start_bridge.bat"):
        shutil.copyfile(REPO / name, root / name)
    path = root / "plugin" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["requiredPermissions"]["network"]["domains"] = list(ccx.NETWORK_DOMAINS)
    manifest["requiredPermissions"].setdefault("launchProcess", dict(LAUNCH))
    manifest.update(manifest_over)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    build_js = root / "plugin" / "lib" / "build.js"
    if not build_js.is_file():
        build_js.write_text(BUILD_JS, encoding="utf-8", newline="\n")
    return root


class PackageTest(unittest.TestCase):
    def checkout(self, **manifest_over) -> Path:
        return checkout(self, **manifest_over)

    def build(self, root, **kw) -> dict:
        return build_ccx.build(repo=root, out_dir=root / "dist", **kw)

    def refused(self, root, *words) -> list:
        with self.assertRaises(ccx.PackageError) as e:
            self.build(root)
        text = "\n".join(e.exception.problems)
        for w in words:
            self.assertIn(w, text)
        self.assertEqual(list((root / "dist").glob("*")) if (root / "dist").exists() else [], [])  # nothing left
        return e.exception.problems

    def test_a_package_without_data_descriptors(self):
        root = self.checkout()
        done = self.build(root)
        self.assertEqual(done["path"], root / "dist" / f"agent-bridge-{(REPO / 'VERSION').read_text().strip()}.ccx")
        with open(done["path"], "rb") as raw, zipfile.ZipFile(raw) as zf:
            self.assertIsNone(zf.testzip())
            self.assertEqual(zf.infolist()[0].filename, "manifest.json")
            for info in zf.infolist():
                raw.seek(info.header_offset)
                head = raw.read(30)
                self.assertEqual(head[:4], b"PK\x03\x04", info.filename)
                self.assertEqual(info.flag_bits & 0x08, 0, info.filename)  # central directory
                self.assertEqual(int.from_bytes(head[6:8], "little") & 0x08, 0, info.filename)  # local header
        self.assertEqual(build_ccx.verify(done["path"], dev=False, start_script=root / "start_bridge.bat"), [])

    def test_the_manifest_names_one_host(self):
        root = self.checkout()
        with zipfile.ZipFile(self.build(root)["path"]) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        source = json.loads((root / "plugin" / "manifest.json").read_text(encoding="utf-8"))
        self.assertIsInstance(source["host"], list)  # the repo keeps its list
        self.assertEqual(manifest["host"], source["host"][0])
        self.assertEqual({k: v for k, v in manifest.items() if k != "host"},
                         {k: v for k, v in source.items() if k != "host"})
        self.assertEqual(self.build(self.checkout(host={"app": "premierepro", "minVersion": "26.3.0"}))["dev"], False)
        self.refused(self.checkout(host=[{"app": "premierepro", "minVersion": "26.3.0"},
                                         {"app": "PS", "minVersion": "24.0.0"}]), "one host")
        self.refused(self.checkout(host={"app": "PS", "minVersion": "24.0.0"}), "one host")

    def test_the_network_rule_is_enforced(self):
        for domains in ("all", ["ws://localhost/", "ws://127.0.0.1/"], ["ws://localhost:8017/"],
                        ["ws://127.0.0.1/"], ["ws://localhost"], None):
            root = self.checkout()
            path = root / "plugin" / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if domains is None:
                del manifest["requiredPermissions"]["network"]
            else:
                manifest["requiredPermissions"]["network"]["domains"] = domains
            path.write_text(json.dumps(manifest), encoding="utf-8")
            problems = self.refused(root, '["ws://localhost/"]')
            self.assertEqual(len(problems), 1, problems)
        self.assertEqual(ccx.manifest_problems({"host": {"app": "premierepro"}, "requiredPermissions": {
            "network": {"domains": ["ws://localhost/"]}, "launchProcess": LAUNCH}}), [])

    def test_launch_process_must_allow_the_bat(self):
        for launch in (None, {"schemes": [], "extensions": [".pyw"]}, {"extensions": ".bat"}):
            root = self.checkout()
            path = root / "plugin" / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if launch is None:
                del manifest["requiredPermissions"]["launchProcess"]
            else:
                manifest["requiredPermissions"]["launchProcess"] = launch
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.refused(root, "launchProcess", ".bat")

    def test_every_problem_is_listed_at_once(self):
        root = self.checkout(version="9.9.9")
        path = root / "plugin" / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["requiredPermissions"]["network"]["domains"] = "all"
        del manifest["requiredPermissions"]["launchProcess"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        (root / "plugin" / "lib" / "build.js").unlink()
        (root / "start_bridge.bat").unlink()
        problems = self.refused(root, "9.9.9", "sync_version", "network", "launchProcess", "build.js",
                                "start_bridge.bat")
        self.assertEqual(len(problems), 5, problems)

    def test_launcher_json_names_this_checkouts_start_script(self):
        root = self.checkout()
        with zipfile.ZipFile(self.build(root)["path"]) as zf:
            raw = zf.read("launcher.json")
        self.assertEqual(json.loads(raw), {"path": str((root / "start_bridge.bat").resolve())})
        self.assertTrue(Path(json.loads(raw)["path"]).is_absolute())
        self.assertNotIn(b"\r", raw)
        raw.decode("ascii")  # ASCII JSON: a non-ASCII folder name is escaped

    def test_the_build_flag(self):
        root = self.checkout()
        # the repository's flavour, whatever this checkout says (a release export's build.js has dev: false)
        (root / "plugin" / "lib" / "build.js").write_text(BUILD_JS, encoding="utf-8", newline="\n")
        with zipfile.ZipFile(self.build(root)["path"]) as zf:
            packaged = zf.read("lib/build.js").decode("utf-8")
        self.assertEqual(packaged, BUILD_JS.replace("dev: true", "dev: false, packaged: true"))
        self.assertEqual((root / "plugin" / "lib" / "build.js").read_text(encoding="utf-8"), BUILD_JS)  # untouched
        with zipfile.ZipFile(self.build(root, dev=True)["path"]) as zf:
            self.assertEqual(ccx.build_flags(zf.read("lib/build.js").decode("utf-8")), (True, True))
        for text, why in (("module.exports = {};\n", "found 0"),
                          ("// dev: true\nmodule.exports = { dev: true };\n", "found 2"),
                          ("module.exports = { dev: true, packaged: false };\n", "packaged")):
            root = self.checkout()
            (root / "plugin" / "lib" / "build.js").write_text(text, encoding="utf-8")
            self.refused(root, "build.js", why)
        self.assertEqual(ccx.build_flags("module.exports = { dev: true };"), (True, False))

    def test_only_what_the_panel_loads(self):
        root = self.checkout()
        plugin = root / "plugin"
        for rel in ("tests/test_x.js", "__pycache__/x.pyc", "lib/__pycache__/y.js", ".DS_Store", ".vscode/s.json",
                    "README.md", "package.json", "node_modules/m/index.js", "notes.txt", "launcher.json",
                    "lib/core.js.map"):
            (plugin / rel).parent.mkdir(parents=True, exist_ok=True)
            (plugin / rel).write_text("{}", encoding="utf-8")
        (plugin / "icons").mkdir()
        (plugin / "icons" / "icon.png").write_bytes(b"\x89PNG")
        done = self.build(root)
        expected = {"manifest.json", "index.html", "main.js", "icon.png", "launcher.json", "icons/icon.png"} | {
            f"lib/{p.name}" for p in (REPO / "plugin" / "lib").glob("*.js")} | {"lib/build.js"}
        self.assertEqual(set(done["files"]), expected)
        with zipfile.ZipFile(done["path"]) as zf:
            self.assertEqual({i.filename for i in zf.infolist() if i.is_dir()}, {"lib/", "icons/"})
            self.assertNotEqual(json.loads(zf.read("launcher.json")), {})  # the package's own, not the stray one

    def test_a_missing_module_or_a_data_descriptor_fails_verification(self):
        root = self.checkout()
        good = self.build(root)["path"]
        start = root / "start_bridge.bat"
        broken = good.with_name("broken.ccx")
        with zipfile.ZipFile(good) as src, zipfile.ZipFile(broken, "w") as out:
            for info in src.infolist():
                if info.filename != "lib/core.js":
                    out.writestr(info, src.read(info))
        self.assertIn("main.js loads './lib/core.js', which the package lacks",
                      build_ccx.verify(broken, dev=False, start_script=start))

        class Unseekable(io.RawIOBase):  # zipfile writes data descriptors to a stream it cannot seek
            def __init__(self, sink):
                self.sink = sink

            def writable(self):
                return True

            def write(self, b):
                return self.sink.write(b)

        streamed = good.with_name("streamed.ccx")
        with zipfile.ZipFile(good) as src, open(streamed, "wb") as sink:
            with zipfile.ZipFile(Unseekable(sink), "w", zipfile.ZIP_DEFLATED) as out:
                for info in src.infolist():
                    if not info.is_dir():
                        with out.open(info.filename, "w") as f:
                            f.write(src.read(info))
        problems = build_ccx.verify(streamed, dev=False, start_script=start)
        self.assertIn("manifest.json: uses a data descriptor (flag bit 3), which UPIA cannot unpack", problems)
        self.assertEqual(build_ccx.verify(good, dev=True, start_script=start),
                         ["lib/build.js does not say dev: true, packaged: true"])

    def test_main_prints_the_install_command_and_runs_nothing(self):
        root = self.checkout()
        out = io.StringIO()
        real = build_ccx.build
        with mock.patch.object(build_ccx, "REPO", root), \
                mock.patch.object(build_ccx, "build",
                                  side_effect=lambda out_dir, dev, **kw: real(repo=root, dev=dev, **kw)), \
                mock.patch.object(build_ccx.doctor, "find_upia", return_value=Path("UPIA") / "upia.exe"), \
                mock.patch("subprocess.run", side_effect=AssertionError("ran a process")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("ran a process")), \
                contextlib.redirect_stdout(out):
            code = build_ccx.main([])
        self.assertEqual(code, 0)
        text = out.getvalue()
        package = root / "dist" / ccx.package_name({"name": "Agent Bridge"}, (REPO / "VERSION").read_text().strip())
        self.assertIn(f'"{Path("UPIA") / "upia.exe"}" /install "{package}"', text)
        self.assertIn('/remove "Agent Bridge"', text)
        self.assertIn("Premiere Pro closed", text)
        self.assertIn("Same plugin id (org.pproagentbridge.panel)", text)
        err = io.StringIO()
        with mock.patch.object(build_ccx, "build", side_effect=ccx.PackageError(["one", "two"])), \
                contextlib.redirect_stderr(err):
            self.assertEqual(build_ccx.main([]), 1)
        self.assertIn("  - one\n  - two", err.getvalue())

    def test_a_test_package_with_its_own_id_and_name(self):
        root = self.checkout()
        done = self.build(root, plugin_id="org.pproagentbridge.ccxtest", name="Agent Bridge CCX Test")
        self.assertEqual(done["path"].name, f"agent-bridge-ccx-test-{(REPO / 'VERSION').read_text().strip()}.ccx")
        self.assertEqual((done["id"], done["name"]), ("org.pproagentbridge.ccxtest", "Agent Bridge CCX Test"))
        with zipfile.ZipFile(done["path"]) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            main_js = zf.read("main.js")
        self.assertEqual((manifest["id"], manifest["name"]), ("org.pproagentbridge.ccxtest", "Agent Bridge CCX Test"))
        self.assertEqual([e["label"]["default"] for e in manifest["entrypoints"]], ["Agent Bridge CCX Test"])
        self.assertEqual(main_js, (REPO / "plugin" / "main.js").read_bytes())  # hello still says the real plugin id
        source = json.loads((root / "plugin" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(source["entrypoints"][0]["label"]["default"], "Agent Bridge")  # the repo is untouched
        with mock.patch.object(build_ccx, "build") as build, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            build_ccx.main(["--id", "org.x"])  # --id without --name
        build.assert_not_called()

    def test_package_name(self):
        self.assertEqual(ccx.package_name({"name": "Agent Bridge"}, "0.6.0"), "agent-bridge-0.6.0.ccx")
        self.assertEqual(ccx.package_name({}, "1.0.0"), "plugin-1.0.0.ccx")

    @unittest.skipUnless(_merged(json.loads((REPO / "plugin" / "manifest.json").read_text(encoding="utf-8")))
                         and (REPO / "plugin" / "lib" / "build.js").is_file(),
                         "this checkout's manifest does not have the .ccx network rule and launchProcess yet")
    def test_this_checkout_packages_as_it_is(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        done = build_ccx.build(out_dir=tmp.name)
        self.assertEqual(build_ccx.verify(done["path"], dev=False, start_script=REPO / "start_bridge.bat"), [])


class PackagedPanelPingTest(unittest.TestCase):
    """A panel installed from this checkout's .ccx is not stale; another .ccx build is told to reinstall."""

    def setUp(self):
        self.root = checkout(self)
        self.plugin = self.root / "plugin"

    def extracted_hash(self, dev=False) -> str:
        done = build_ccx.build(repo=self.root, out_dir=self.root / "dist", dev=dev)
        folder = self.root / f"installed-{dev}"
        with zipfile.ZipFile(done["path"]) as zf:
            zf.extractall(folder)
        return panelhash.code_hash(folder)

    def test_the_installed_package_hashes_like_the_checkout_packaged(self):
        installed = self.extracted_hash()
        self.assertNotEqual(installed, panelhash.code_hash(self.plugin))
        self.assertEqual(installed, panelhash.hash_files(ccx.package_files(self.plugin)))
        self.assertEqual(ccx.packaged_code_hashes(self.plugin), {installed, self.extracted_hash(dev=True)})
        self.assertEqual(ccx.packaged_code_hashes(self.root / "nothing"), set())

    def test_ping(self):
        installed = self.extracted_hash()
        build = {"dev": False, "packaged": True}
        with mock.patch.object(ops, "PLUGIN_DIR", self.plugin):
            fresh = ops.ping(stub_rpc({"ping": ping_reply(codeHash=installed, build=build)}))
            other = ops.ping(stub_rpc({"ping": ping_reply(codeHash="00000000", build=build)}))
            dev = ops.ping(stub_rpc({"ping": ping_reply(codeHash="00000000", build={"dev": True,
                                                                                   "packaged": False})}))
        self.assertFalse(fresh["stale"])
        self.assertNotIn("warning", fresh)
        self.assertTrue(other["stale"])
        self.assertIn("build_ccx.py", other["warning"])
        self.assertNotIn("UXP Developer Tool", other["warning"])
        self.assertIn("Reload it in UXP Developer Tool", dev["warning"])


if __name__ == "__main__":
    unittest.main()
