"""ppro_bridge.doctor: the setup check (setup_bridge.bat) - required vs optional items, the report, the fixes."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port)

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ppro_bridge import doctor


class DoctorTest(unittest.TestCase):
    def patched(self, **over):
        values = {"find_udt": Path("UDT/udt.exe"), "developer_mode": True,
                  "installs": ["Adobe Premiere Pro (Beta) 27.1"], "broker": False}
        values.update(over)
        return [mock.patch.object(doctor.devloader, "find_udt", return_value=values["find_udt"]),
                mock.patch.object(doctor.devloader, "developer_mode", return_value=values["developer_mode"]),
                mock.patch.object(doctor, "premiere_installs", return_value=values["installs"]),
                mock.patch.object(doctor, "broker_running", return_value=values["broker"]),
                mock.patch.object(doctor, "upia_list", return_value={"exe": None}),  # never the real UPIA
                mock.patch.object(doctor, "ccx_install_dirs", return_value=[])]

    def run_checks(self, **over):
        patches = self.patched(**over)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return {r["name"]: r for r in doctor.checks()}

    def test_a_ready_machine(self):
        r = self.run_checks()
        self.assertTrue(all(x["ok"] for x in r.values() if x["required"]), r)
        self.assertFalse(r["Broker"]["required"])  # running it is the next step, not a prerequisite
        self.assertIn("27.1", r["Premiere Pro (UXP)"]["detail"])

    def test_what_is_missing_and_how_to_fix_it(self):
        r = self.run_checks(find_udt=None, developer_mode=False, installs=["Adobe Premiere Pro 2024 24.6"])
        self.assertFalse(r["UXP Developer Tool"]["ok"])
        self.assertIn("Creative Cloud", r["UXP Developer Tool"]["fix"])
        self.assertIn("Enable Developer Mode", r["UXP developer mode"]["fix"])
        self.assertFalse(r["Premiere Pro (UXP)"]["ok"])  # 24.6 has no UXP plugins
        text = doctor.report(list(r.values()))
        self.assertIn("[MISS] UXP Developer Tool: not found", text)
        self.assertIn("-> open UXP Developer Tool", text)

    def test_main_exit_code_and_fixes_without_asking(self):
        patches = self.patched(find_udt=None)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        with mock.patch.object(doctor, "install_deps", return_value=0) as deps, \
                mock.patch.object(doctor, "_special_folder", return_value=Path("Desktop")), \
                mock.patch.object(doctor, "make_shortcut", side_effect=lambda folder, minimized=False:
                                  Path(folder) / doctor.SHORTCUT_NAME) as link, \
                redirect_stdout(io.StringIO()) as out:
            code = doctor.main(["--install-deps", "--shortcut", "--autostart"])
        self.assertEqual(code, 1)  # UXP Developer Tool is still missing
        deps.assert_called_once()
        # desktop, Start menu, then the minimized Startup one
        self.assertEqual([c.kwargs.get("minimized", False) for c in link.call_args_list], [False, False, True])
        self.assertIn("Still missing: UXP Developer Tool", out.getvalue())
        self.assertIn("[opt ] Panel as .ccx: Creative Cloud's plugin installer (UPIA) not found", out.getvalue())

    def test_version_parsing(self):
        self.assertGreaterEqual(doctor._version_tuple("27.1"), (25, 6))
        self.assertLess(doctor._version_tuple("24.6.2"), (25, 6))
        self.assertGreaterEqual(doctor._version_tuple("16.1"), (14,))
        self.assertEqual([doctor.version_label(v) for v in ((26, 3), (27,), (26, 3, 1))], ["26.3", "27.0", "26.3.1"])

    def manifest(self, host) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "manifest.json"
        path.write_text(json.dumps({"id": "x", "host": host}), encoding="utf-8")
        return path

    def test_premiere_min_comes_from_the_manifest(self):
        self.assertEqual(doctor.premiere_min(self.manifest({"app": "premierepro", "minVersion": "26.3.0"})), (26, 3))
        listed = [{"app": "PS", "minVersion": "24.0.0"}, {"app": "premierepro", "minVersion": "27.0.0"}]
        self.assertEqual(doctor.premiere_min(self.manifest(listed)), (27,))
        self.assertEqual(doctor.premiere_min(self.manifest({"app": "premierepro", "minVersion": "26.3.1"})), (26, 3, 1))
        for bad in ({"app": "PS", "minVersion": "27.0"}, [], {"app": "premierepro"},
                    {"app": "premierepro", "minVersion": "beta"}, "premierepro"):
            self.assertEqual(doctor.premiere_min(self.manifest(bad)), doctor.PREMIERE_MIN_FALLBACK, bad)
        self.assertEqual(doctor.premiere_min(Path(self.manifest({}).parent) / "missing.json"),
                         doctor.PREMIERE_MIN_FALLBACK)
        real = json.loads(doctor.MANIFEST.read_text(encoding="utf-8"))["host"]
        real = real if isinstance(real, list) else [real]
        version = next(h["minVersion"] for h in real if h["app"] == "premierepro")
        self.assertEqual(doctor.version_label(doctor.premiere_min()), version.removesuffix(".0"))

    def test_premiere_check_uses_the_manifest_minimum(self):
        with mock.patch.object(doctor, "premiere_min", return_value=(27, 2)):
            r = self.run_checks(installs=["Adobe Premiere Pro (Beta) 27.1"])
        self.assertFalse(r["Premiere Pro (UXP)"]["ok"])
        self.assertEqual(r["Premiere Pro (UXP)"]["fix"], "Premiere Pro 27.2 or newer (or the Beta)")
        with mock.patch.object(doctor, "premiere_min", return_value=(26, 3)):
            r = self.run_checks(installs=["Adobe Premiere Pro 2026 26.3", "Adobe Premiere Pro 2025 25.6.1"])
        self.assertTrue(r["Premiere Pro (UXP)"]["ok"])
        self.assertEqual(r["Premiere Pro (UXP)"]["detail"], "Adobe Premiere Pro 2026 26.3")

    def test_websockets_version_is_read_from_disk(self):
        self.assertIsInstance(doctor._installed_version("websockets", "websockets"), str)  # the tests need it
        self.assertIsNone(doctor._installed_version("no-such-dist-for-doctor", "no_such_module_for_doctor"))
        for version, detail in ((None, "not installed"), ("12.0", "12.0 is too old")):
            with mock.patch.object(doctor, "_installed_version", return_value=version):
                r = self.run_checks()
            self.assertFalse(r["websockets"]["ok"])
            self.assertEqual((r["websockets"]["detail"], r["websockets"]["fix"]), (detail, "--install-deps"))

    def test_shortcut_paths_and_removing_autostart(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(doctor, "_special_folder", side_effect=lambda name: Path(tmp) / name) as folder:
            self.assertEqual(doctor.desktop_shortcut(), Path(tmp) / "Desktop" / doctor.SHORTCUT_NAME)
            self.assertEqual(doctor.start_menu_shortcut(), Path(tmp) / "Programs" / doctor.SHORTCUT_NAME)
            link = doctor.autostart_shortcut()
            self.assertEqual(link, Path(tmp) / "Startup" / doctor.SHORTCUT_NAME)
            self.assertFalse(doctor.remove_autostart())  # nothing to remove
            link.parent.mkdir()
            link.write_bytes(b"")
            self.assertTrue(doctor.remove_autostart())
            self.assertFalse(link.exists())
        self.assertIn(mock.call("Startup"), folder.call_args_list)

    def test_shortcut_carries_the_icon_and_quotes_paths(self):
        self.assertTrue(doctor.ICON.is_file(), "ppro_bridge/bridge.ico is missing: python scripts/make_icon.py")
        seen = {}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "it's here"
            folder.mkdir()

            def fake_run(cmd, **kw):
                seen["script"] = cmd[-1]
                (folder / doctor.SHORTCUT_NAME).write_bytes(b"lnk")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(doctor.subprocess, "run", side_effect=fake_run):
                doctor.make_shortcut(folder)
        self.assertIn(f"$s.IconLocation = '{doctor.ICON},0'", seen["script"])
        self.assertIn("it''s here", seen["script"])  # a quote in a folder name is doubled for PowerShell

    def test_the_icon_has_every_windows_size(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        with Image.open(doctor.ICON) as ico:
            sizes = set(ico.info["sizes"])
        self.assertLessEqual({(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)}, sizes)



FIXTURE = Path(__file__).resolve().parent / "fixtures" / "upia_list_all.txt"  # UPIA /list all, 2 Nov 2026
MANIFEST = {"id": "org.pproagentbridge.panel", "name": "Agent Bridge", "version": "0.5.1"}
BETA = ["Adobe Premiere Pro (Beta) 27.1", "Adobe Premiere Pro 2026 26.5.2"]


class UpiaTest(unittest.TestCase):
    """The read-only Creative Cloud plugin installer check: parsing `/list all`, the .ccx verdicts, running it once."""

    def setUp(self):
        doctor._upia_last = None
        self.addCleanup(setattr, doctor, "_upia_last", None)
        self.text = FIXTURE.read_text(encoding="utf-8").replace("\n", "\r\n")  # UPIA prints CRLF

    def listing(self, premiere=None):
        """The fixture's hosts, the Premiere Pro entries replaced by `premiere` [(version, [rows])] when given."""
        hosts = doctor.parse_upia_list(self.text)
        if premiere is not None:
            hosts = [h for h in hosts if h["host"] != "Premiere Pro"] + [
                {"host": "Premiere Pro", "version": v, "extensions": [
                    {"status": st, "name": n, "version": ver} for st, n, ver in rows]} for v, rows in premiere]
        return {"exe": "/CC/UPIA.exe", "hosts": hosts}

    def check(self, listing, dirs=(), installs=BETA, manifest=MANIFEST):
        with mock.patch.object(doctor, "premiere_min", return_value=(26, 3)):
            return doctor.ccx_check(listing, manifest=manifest, installs=installs, dirs=list(dirs))

    def test_parsing_what_upia_printed_on_this_machine(self):
        for text in (self.text, self.text.replace("\r\n", "\n")):
            hosts = doctor.parse_upia_list(text)
            self.assertEqual(len(hosts), 11)
            self.assertEqual([h for h in hosts if h["host"] == "Premiere Pro"],
                             [{"host": "Premiere Pro", "version": "26.5.2", "extensions": [
                                 {"status": "Enabled", "name": "Motion Array", "version": "1.7.0"}]}])
            self.assertEqual(hosts[-1], {"host": "Others", "version": None, "extensions": []})
            self.assertIn({"host": "Photoshop 2026 64", "version": "27.11.0", "extensions": []}, hosts)
            self.assertEqual(sum(len(h["extensions"]) for h in hosts), 3)
        self.assertFalse(any("beta" in h["host"].lower() for h in doctor.parse_upia_list(self.text)))  # Beta unknown
        rows = doctor.parse_upia_list(
            "2 extensions installed for Premiere Pro (ver 27.0.0)\r\n Status    Extension Name    Version\r\n"
            "=========  ==========  ==========\r\n Enabled    Agent Bridge                 0.6.0\r\n"
            " Disabled   A Name Longer Than The Fifty-Five Characters Of The Name Column Is   10.2.3\r\n")
        self.assertEqual(rows[0]["extensions"], [
            {"status": "Enabled", "name": "Agent Bridge", "version": "0.6.0"},
            {"status": "Disabled", "name": "A Name Longer Than The Fifty-Five Characters Of The Name Column Is",
             "version": "10.2.3"}])
        self.assertEqual(doctor.parse_upia_list("Error: -642 the host application is running\r\n"), [])

    def test_not_installed_as_ccx(self):
        r = self.check(self.listing())
        self.assertTrue(r["ok"])
        self.assertFalse(r["required"])
        self.assertEqual(r["detail"], "not installed as .ccx (UXP Developer Tool loads the panel); UPIA knows Premiere "
                                      "Pro 26.5.2, not Adobe Premiere Pro (Beta) 27.1 (UXP Developer Tool still loads "
                                      "the panel there)")
        self.assertNotIn("Beta", self.check(self.listing(), installs=["Adobe Premiere Pro 2026 26.5.2"])["detail"])
        self.assertIn("UPIA knows no Premiere Pro", self.check(self.listing(premiere=[]))["detail"])

    def test_registered(self):
        r = self.check(self.listing([("26.5.2", [("Enabled", "Motion Array", "1.7.0"),
                                                 ("Enabled", "Agent Bridge", "0.5.1")])]))
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["detail"].startswith("installed as .ccx: Agent Bridge 0.5.1 for Premiere Pro 26.5.2 "
                                               "(Enabled)"), r["detail"])
        older = self.check(self.listing([("26.5.2", [("Enabled", "Agent Bridge", "0.5.0")])]))
        self.assertFalse(older["ok"])
        self.assertIn('/remove "Agent Bridge", then "/CC/UPIA.exe" /install ', older["fix"])
        self.assertIn("with Premiere closed (not run here)", older["fix"])
        off = self.check(self.listing([("26.5.2", [("Disabled", "Agent Bridge", "0.5.1")])]))
        self.assertFalse(off["ok"])
        self.assertIn("not enabled", off["detail"])

    def test_unregistered_by_a_premiere_update_gets_the_repair_command(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(doctor.ccx, "DIST", Path(tmp.name)):
            r = self.check(self.listing([("26.5.2", [("Enabled", "Agent Bridge", "0.5.1")]), ("27.0.0", [])]))
            self.assertFalse(r["ok"])
            self.assertFalse(r["required"])
            self.assertIn("registered for Premiere Pro 26.5.2 but not registered for Premiere Pro 27.0.0", r["detail"])
            package = Path(tmp.name) / "agent-bridge-0.5.1.ccx"
            self.assertEqual(r["fix"], f'repair, with Premiere closed (not run here): "/CC/UPIA.exe" /install '
                                       f'"{package}" (build it first: python scripts/build_ccx.py)')
            package.write_bytes(b"PK")
            folder = Path("UXP") / "External" / "org.pproagentbridge.panel_0.5.1"
            gone = self.check(self.listing(), dirs=[folder])  # the folder stayed, UPIA forgot it
            self.assertIn(f"installed in {folder} but not registered for Premiere Pro 26.5.2", gone["detail"])
            self.assertTrue(gone["fix"].endswith(f'/install "{package}"'), gone["fix"])
            nowhere = self.check(self.listing([("25.6.6", [])]), dirs=[folder])  # only an older Premiere listed
            self.assertFalse(nowhere["ok"])
            self.assertIn("UPIA knows Premiere Pro 25.6.6, not Adobe Premiere Pro (Beta) 27.1", nowhere["detail"])
            self.assertIn("Premiere Pro 26.3 or newer that UPIA lists", nowhere["fix"])
        # a Premiere older than the panel's minimum does not count as missing
        old = self.check(self.listing([("25.6.6", []), ("26.5.2", [("Enabled", "Agent Bridge", "0.5.1")])]))
        self.assertTrue(old["ok"], old)

    def test_upia_missing_slow_or_failing(self):
        r = self.check({"exe": None})
        self.assertEqual((r["ok"], r["required"]), (False, False))
        self.assertIn("Creative Cloud", r["fix"])
        slow = self.check({"exe": "upia.exe", "error": "no answer within 30 s", "timeout": True})
        self.assertIn("blocks while Premiere starts", slow["fix"])
        self.assertIn("not in a loop", slow["fix"])
        broken = self.check({"exe": "upia.exe", "error": "exit 1: Error -642"})
        self.assertEqual(broken["detail"], "UPIA /list all failed: exit 1: Error -642")

    def test_upia_runs_once_with_a_timeout(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        exe = Path(tmp.name) / "UnifiedPluginInstallerAgent.exe"
        exe.write_bytes(b"")
        with mock.patch.object(doctor, "find_upia", return_value=exe), \
                mock.patch.object(doctor, "_run_upia", return_value=(0, self.text)) as run:
            first, again = doctor.upia_list(), doctor.upia_list()
            self.assertIs(first, again)
            run.assert_called_once_with(exe)
            self.assertEqual(len(first["hosts"]), 11)
            doctor.upia_list(refresh=True)
            self.assertEqual(run.call_count, 2)
        self.assertEqual(doctor.UPIA_TIMEOUT, 30)
        doctor._upia_last = None
        with mock.patch.object(doctor, "find_upia", return_value=exe), \
                mock.patch.object(doctor, "_run_upia", side_effect=subprocess.TimeoutExpired("upia", 30)) as run:
            self.assertEqual(doctor.upia_list(), {"exe": str(exe), "error": "no answer within 30 s", "timeout": True})
            doctor.upia_list()  # a timed-out UPIA is not asked again straight away
            run.assert_called_once()
        doctor._upia_last = None
        with mock.patch.object(doctor, "find_upia", return_value=exe), \
                mock.patch.object(doctor, "_run_upia",
                                  return_value=(4294966654, "\r\nError -642: Premiere Pro is starting\r\n")):
            self.assertEqual(doctor.upia_list()["error"], "exit 4294966654: Error -642: Premiere Pro is starting")
        doctor._upia_last = None
        with mock.patch.object(doctor, "find_upia", return_value=None), \
                mock.patch.object(doctor, "_run_upia") as run:
            self.assertEqual(doctor.upia_list(), {"exe": None})
            run.assert_not_called()

    def stand_in_upia(self, body) -> Path:
        """A stand-in UPIA: a .bat that runs the Python `body` with the arguments it was given."""
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "upia.py"
        script.write_text(body, encoding="utf-8")
        bat = Path(tmp.name) / "UnifiedPluginInstallerAgent.bat"
        bat.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return bat

    @unittest.skipUnless(os.name == "nt", "a .bat stands in for UPIA")
    def test_a_real_process_and_its_output(self):
        bat = self.stand_in_upia("import sys\nprint('args', sys.argv[1:])\n"
                                 f"sys.stdout.write(open({str(FIXTURE)!r}, encoding='utf-8').read())\nsys.exit(3)\n")
        code, text = doctor._run_upia(bat)
        self.assertEqual(code, 3)
        self.assertIn("args ['/list', 'all']", text)
        self.assertEqual(len(doctor.parse_upia_list(text)), 11)

    @unittest.skipUnless(os.name == "nt", "a .bat stands in for UPIA")
    def test_the_timeout_holds_while_a_helper_keeps_the_output_open(self):
        # subprocess.run(capture_output=True, timeout=2) took 12.3 s here: after the kill it waits for the pipe
        bat = self.stand_in_upia("import subprocess, sys, time\n"
                                 "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'], "
                                 "stdout=sys.stdout)\nprint('starting', flush=True)\ntime.sleep(8)\n")
        started = time.monotonic()
        with mock.patch.object(doctor, "UPIA_TIMEOUT", 1.5), self.assertRaises(subprocess.TimeoutExpired):
            doctor._run_upia(bat)
        self.assertLess(time.monotonic() - started, 6)

    def test_never_two_at_once(self):
        started = []

        def slow(exe):
            started.append(exe)
            threading.Event().wait(0.2)
            return 0, self.text

        with mock.patch.object(doctor, "find_upia", return_value=Path("upia.exe")), \
                mock.patch.object(doctor, "_run_upia", side_effect=slow):
            threads = [threading.Thread(target=doctor.upia_list) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(len(started), 1)

    def test_find_upia(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        exe = Path(tmp.name) / "upia.exe"
        exe.write_bytes(b"")
        with mock.patch.dict(doctor.os.environ, {doctor.UPIA_ENV: str(exe)}):
            self.assertEqual(doctor.find_upia(), exe)
        with mock.patch.dict(doctor.os.environ, {doctor.UPIA_ENV: str(exe.with_name("missing.exe"))}):
            self.assertIsNone(doctor.find_upia())
        self.assertEqual(doctor.UPIA_DEFAULT.name, "UnifiedPluginInstallerAgent.exe")
        self.assertIn("Adobe Desktop Common", str(doctor.UPIA_DEFAULT))

    def test_install_folders_on_disk(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        roaming, common = Path(tmp.name) / "Roaming", Path(tmp.name) / "Common"
        external = roaming / "Adobe" / "UXP" / "Plugins" / "External"
        for root, name, plugin_id in ((external, "org.pproagentbridge.panel_0.5.1", "org.pproagentbridge.panel"),
                                      (external, "other_1.0.0", "com.other"),
                                      (common / "Adobe" / "UXP" / "extensions", "agent", "org.pproagentbridge.panel")):
            (root / name).mkdir(parents=True)
            (root / name / "manifest.json").write_text(json.dumps({"id": plugin_id}), encoding="utf-8")
        (external / "broken").mkdir()
        with mock.patch.object(doctor, "roaming_folder", return_value=roaming), \
                mock.patch.object(doctor, "COMMON_FILES", common):
            self.assertEqual(doctor.ccx_install_dirs("org.pproagentbridge.panel"),
                             [external / "org.pproagentbridge.panel_0.5.1",
                              common / "Adobe" / "UXP" / "extensions" / "agent"])
            self.assertEqual(doctor.ccx_install_dirs("nobody"), [])
        with mock.patch.object(doctor, "roaming_folder", return_value=None), \
                mock.patch.object(doctor, "COMMON_FILES", common):  # Windows could not say: the per-user root is skipped
            self.assertEqual(doctor.ccx_install_dirs("org.pproagentbridge.panel"),
                             [common / "Adobe" / "UXP" / "extensions" / "agent"])

    def test_the_roaming_folder_comes_from_windows(self):
        found = doctor.roaming_folder()
        if doctor.os.name != "nt":
            self.assertIsNone(found)
            return
        self.assertTrue(found.is_dir(), found)
        self.assertEqual(found.name.lower(), "roaming")

    def test_only_the_command_line_asks_upia(self):
        patches = DoctorTest.patched(self)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.assertNotIn(doctor.CCX_CHECK, [r["name"] for r in doctor.checks()])  # what the setup window shows
        doctor.upia_list.assert_not_called()
        self.assertEqual(doctor.checks(upia=True)[-1]["name"], doctor.CCX_CHECK)
        doctor.upia_list.reset_mock()
        with mock.patch.object(doctor, "install_deps", return_value=0), redirect_stdout(io.StringIO()):
            doctor.main(["--no-upia"])
            doctor.upia_list.assert_not_called()
            doctor.main(["--install-deps"])  # two reports; upia_list reuses its first answer for the second
        self.assertEqual(doctor.upia_list.call_count, 2)

    def test_the_command_line_runs_upia_once(self):
        patches = DoctorTest.patched(self)[:4]  # with the real upia_list
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        with mock.patch.object(doctor, "find_upia", return_value=Path("upia.exe")), \
                mock.patch.object(doctor, "ccx_install_dirs", return_value=[]), \
                mock.patch.object(doctor, "_run_upia", return_value=(0, self.text)) as run, \
                mock.patch.object(doctor, "install_deps", return_value=0), redirect_stdout(io.StringIO()) as out:
            doctor.main(["--install-deps"])
        run.assert_called_once()
        self.assertEqual(out.getvalue().count("[OK  ] Panel as .ccx: not installed as .ccx"), 2)


if __name__ == "__main__":
    unittest.main()
