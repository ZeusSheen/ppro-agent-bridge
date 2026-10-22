"""ppro_bridge.doctor: the setup check (setup_bridge.bat) - required vs optional items, the report, the fixes."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port)

import io
import json
import tempfile
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
                mock.patch.object(doctor, "broker_running", return_value=values["broker"])]

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
        self.assertEqual([c.kwargs.get("minimized", False) for c in link.call_args_list], [False, True])
        self.assertIn("Still missing: UXP Developer Tool", out.getvalue())

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
            link = doctor.autostart_shortcut()
            self.assertEqual(link, Path(tmp) / "Startup" / doctor.SHORTCUT_NAME)
            self.assertFalse(doctor.remove_autostart())  # nothing to remove
            link.parent.mkdir()
            link.write_bytes(b"")
            self.assertTrue(doctor.remove_autostart())
            self.assertFalse(link.exists())
        self.assertIn(mock.call("Startup"), folder.call_args_list)


if __name__ == "__main__":
    unittest.main()
