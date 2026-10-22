"""The 0.3.3 entry points keep working on top of the ppro_bridge package: `python ppro.py …` from cli/,
`python cli/ppro.py …` and `python -m ppro_bridge …` from the repo root, `import ppro` / `import replace` from cli/,
and transcribe.py / transcript_digest.py as scripts (transcribe --help even without faster-whisper)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import CLI, REPO, env

from ppro_bridge import cli, spec
from ppro_bridge._version import VERSION


def py(*args, cwd, timeout=60):
    return subprocess.run([sys.executable, *args], cwd=str(cwd), env=env(), capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)


def py_code(code, cwd=CLI):
    out = py("-c", code, cwd=cwd)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout.strip()


class ImportFromCliTest(unittest.TestCase):
    def test_import_ppro_keeps_the_033_names(self):
        names = ["CLI_VERSION", "BridgeError", "BROKER_URL", "call", "lost", "rpc", "item_ref", "parse_track",
                 "probe_media", "fit_scale", "resolve_exported_frame", "make_contact_sheet", "MARKER_COLORS", "main",
                 "resolve_item", "check_expect_seq", "require_panel", "export_one_frame"]
        got = json.loads(py_code(f"import json, ppro; print(json.dumps([ppro.CLI_VERSION, "
                                 f"[n for n in {names!r} if not hasattr(ppro, n)]]))"))
        self.assertEqual(got, [VERSION, []])

    def test_rpc_stub_replacement_reaches_the_wrappers(self):
        code = (
            "import json, ppro\n"
            "calls = []\n"
            "def stub(action, params=None):\n"
            "    calls.append(action)\n"
            "    if action == 'list_project_items':\n"
            "        return {'items': [{'name': 'a.mp4', 'bin': 'B', 'mediaPath': '/fake/a.mp4'}]}\n"
            "    if action == 'get_sequence':\n"
            "        return {'name': 'Seq'}\n"
            "    if action == 'ping':\n"
            "        return {'actions': ['ping'], 'plugin': 'stub'}\n"
            "ppro.rpc = stub\n"
            "item = ppro.resolve_item('a.mp4')\n"
            "ppro.check_expect_seq('Seq')\n"
            "try:\n"
            "    ppro.require_panel(['set_params'])\n"
            "    code = None\n"
            "except ppro.BridgeError as err:\n"
            "    code = err.code\n"
            "print(json.dumps([ppro.item_ref(item), calls, code]))\n")
        ref, calls, code = json.loads(py_code(code))
        self.assertEqual(ref, "B/a.mp4")
        self.assertEqual(calls, ["list_project_items", "get_sequence", "ping"])
        self.assertEqual(code, "PANEL_OUTDATED")

    def test_ppro_main_uses_the_replaced_rpc(self):
        code = ("import ppro\n"
                "ppro.rpc = lambda action, params=None: {'name': 'Stub Project', 'action': action}\n"
                "raise SystemExit(ppro.main(['get_project']))\n")
        out = py("-c", code, cwd=CLI)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["data"], {"name": "Stub Project", "action": "get_project"})

    def test_module_shims_are_the_package_modules(self):
        got = json.loads(py_code(
            "import json, sys, errors, edit_plan, replace, framegrid, transcribe, transcript_digest, ppro\n"
            "import ppro_bridge.errors, ppro_bridge.plan, ppro_bridge.replace, ppro_bridge.framegrid\n"
            "import ppro_bridge.transcribe, ppro_bridge.digest\n"
            "print(json.dumps([edit_plan is ppro_bridge.plan, replace is ppro_bridge.replace,\n"
            "                  framegrid is ppro_bridge.framegrid, errors is ppro_bridge.errors,\n"
            "                  transcribe is ppro_bridge.transcribe, transcript_digest is ppro_bridge.digest,\n"
            "                  errors.BridgeError is ppro.BridgeError is ppro_bridge.errors.BridgeError,\n"
            "                  'faster_whisper' in sys.modules]))"))
        self.assertEqual(got, [True] * 7 + [False])

    def test_one_bridge_error_class_across_shims(self):
        out = py_code("import replace, errors\n"
                      "try:\n"
                      "    replace.replace_clip(lambda a, p=None: {'tracks': [], 'name': 'S', 'fps': 25}, 0, 1.0,\n"
                      "                         {'name': 'x'}, dry_run=True)\n"
                      "except errors.BridgeError as err:\n"
                      "    print(err.code)\n")
        self.assertEqual(out, "NOT_FOUND")


class EntryPointTest(unittest.TestCase):
    def test_help_everywhere(self):
        for args, cwd in ((["ppro.py", "--help"], CLI), (["cli/ppro.py", "--help"], REPO),
                          (["-m", "ppro_bridge", "--help"], REPO), (["transcript_digest.py", "--help"], CLI)):
            out = py(*args, cwd=cwd)
            self.assertEqual(out.returncode, 0, (args, out.stderr))
            self.assertIn("usage:", out.stdout)

    def test_transcribe_help_without_faster_whisper(self):
        code = ("import runpy, sys\n"
                "sys.modules['faster_whisper'] = None  # as if it were not installed\n"
                "sys.modules['ctranslate2'] = None\n"
                "sys.argv = ['transcribe.py', '--help']\n"
                "runpy.run_path('transcribe.py', run_name='__main__')\n")
        out = py("-c", code, cwd=CLI)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--adobe-language", out.stdout)

    def test_transcribe_without_faster_whisper_reports_the_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "clip.wav"
            media.write_bytes(b"RIFF")
            code = ("import runpy, sys\n"
                    "sys.modules['faster_whisper'] = None\n"
                    f"sys.argv = ['transcribe.py', {str(media)!r}, '--output', {str(Path(tmp) / 'tr.json')!r}]\n"
                    "runpy.run_path('transcribe.py', run_name='__main__')\n")
            out = py("-c", code, cwd=CLI)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(json.loads(out.stdout)["code"], "MISSING_DEPENDENCY")

    def test_cli_failure_from_cli_folder_is_one_json_object(self):
        out = py("ppro.py", "get_project", cwd=CLI)  # PPRO_BRIDGE_URL points at a closed port
        self.assertEqual(out.returncode, 1)
        self.assertEqual(json.loads(out.stdout)["code"], "NO_BROKER")

    def test_digest_script_from_cli_folder(self):
        doc = {"language": "en-us", "speakers": [{"id": "s", "name": "A"}], "segments": [
            {"start": 0, "duration": 1, "speaker": "s", "words": [
                {"text": "Hello", "start": 0.0, "duration": 0.4, "eos": False, "confidence": 0.9, "type": "word"},
                {"text": "there", "start": 0.4, "duration": 0.4, "eos": False, "confidence": 0.9, "type": "word"},
                {"text": ".", "start": 0.8, "duration": 0.0, "eos": True, "confidence": 1.0, "type": "punctuation"}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode("utf-8"))  # with a BOM
            out = py("transcript_digest.py", str(path), "--json", cwd=CLI)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["sentences"][0]["text"], "Hello there.")


class ParserTest(unittest.TestCase):
    def test_subcommands_are_the_spec_commands(self):
        sub = next(a for a in cli.build_parser()._actions if a.dest == "action")
        self.assertEqual(sorted(sub.choices), sorted(spec.command_names()))
        self.assertEqual(len(sub.choices), 34)

    def test_dest_names_are_ops_keywords(self):
        import inspect

        from ppro_bridge import ops
        sub = next(a for a in cli.build_parser()._actions if a.dest == "action")
        for name, parser in sub.choices.items():
            sig = inspect.signature(getattr(ops, name))
            params = list(sig.parameters.values())
            self.assertEqual(params[0].name, "rpc", name)
            self.assertIs(params[0].default, inspect.Parameter.empty, f"{name}: rpc must not have a default")
            dests = {a.dest for a in parser._actions if a.dest != "help"}
            kwonly = {p.name for p in params[1:] if p.kind is inspect.Parameter.KEYWORD_ONLY}
            self.assertLessEqual(dests, kwonly, name)
            self.assertTrue(all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]), name)

    def test_no_default_transport_in_the_library(self):
        import inspect

        from ppro_bridge import frames, items, plan, replace
        for fn in (plan.run, plan.run_plan, replace.replace_clip, replace.otio_transitions,
                   replace.free_audio_track, frames.export_one_frame, items.check_expect_seq,
                   items.require_panel, items.require_command):
            rpc = inspect.signature(fn).parameters["rpc"]
            self.assertIs(rpc.default, inspect.Parameter.empty, fn.__qualname__)


if __name__ == "__main__":
    unittest.main()
