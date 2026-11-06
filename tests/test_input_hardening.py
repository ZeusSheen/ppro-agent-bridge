"""Malicious inputs the 0.7 security review found (blue team B / red team B): planted files beside a conversion,
plans that reach outside their folder, preset XML with a document type, file names that are shell syntax, paths in a
problem report, and the separate programs (transcribe.py, the MCP gate) that open paths too."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port)
from helpers import fake_rpc

from ppro_bridge import media, prfpset, report
from ppro_bridge import plan as edit_plan
from ppro_bridge import transcribe as ppro_transcribe
from ppro_bridge.errors import BridgeError
from ppro_bridge.mcp import tools as mcp_tools

B = "\\"
UNC = B * 2 + "pab-guard.invalid" + B + "share" + B


def tmpdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="pab_inputs_"))
    return d


class WavPartFileTest(unittest.TestCase):
    """RB-1: ffmpeg -y writes only a file this conversion created itself, never a <name>.wav.part planted before."""

    INFO = {"hasAudio": True, "audioCodecs": ["flac"], "duration": 1.0}

    def convert(self, folder: Path):
        song = folder / "song.flac"
        song.write_bytes(b"fLaC")
        written = []

        def run(cmd, **kw):
            written.append(cmd[-1])
            Path(cmd[-1]).write_bytes(b"RIFF....WAVE")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def probe(path):
            return {"audioCodecs": ["pcm_s24le"], "duration": 1.0} if str(path).endswith(".part") else {}

        return media.convert_to_wav(song, self.INFO, probe=probe, run=run), written

    def test_a_planted_part_link_is_never_written_through(self):
        folder = tmpdir()
        victim = folder / "victim.docx"
        victim.write_bytes(b"PK precious")
        planted = folder / "song.wav.part"
        try:
            os.symlink(victim, planted)
        except OSError:  # no right to make links here: a planted plain file must stay untouched too
            planted.write_bytes(b"planted")
        done, written = self.convert(folder)
        self.assertNotIn("error", done, done)
        self.assertEqual(victim.read_bytes(), b"PK precious")
        self.assertNotIn(str(planted), written)
        self.assertTrue(os.path.lexists(planted))  # left where it was, unread
        self.assertEqual((folder / "song.wav").read_bytes(), b"RIFF....WAVE")
        self.assertEqual([p.name for p in folder.iterdir() if p.name.endswith(".part") and p != planted], [])

    def test_a_link_named_like_the_target_counts_as_taken(self):
        folder = tmpdir()
        try:
            os.symlink(folder / "nowhere.wav", folder / "song.wav")  # dangling: os.path.exists says False
        except OSError as e:
            self.skipTest(f"no right to create symbolic links here: {e}")
        done, _ = self.convert(folder)
        self.assertEqual(Path(done["to"]).name, "song (48k).wav")


class PlanContainmentTest(unittest.TestCase):
    """RB-2: a plan reaches only files under its media_root, and its media_root lies in the plan's own folder,
    unless the caller passes --media-root."""

    def run_plan(self, folder: Path, media_root, src, cli_root=None):
        plan = folder / "plan.json"
        plan.write_text(json.dumps({"sequence": "Sequence 01", "media_root": media_root, "commands": [
            {"command": "insert_clip", "id": "c1", "track": "V1", "timeline_at": 0, "src": src, "src_out": 1}]}),
            encoding="utf-8")
        rpc, _fake, calls = fake_rpc()
        result = edit_plan.run_plan(plan, rpc=rpc, dry_run=True, media_root=cli_root, probe_media=lambda p: {})
        return result, calls

    def test_dotdot_and_absolute_src_are_step_errors(self):
        folder = tmpdir()
        (folder / "media").mkdir()
        outside = folder.parent / (folder.name + "_secret")
        outside.mkdir()
        (outside / "payload.mp4").write_bytes(b"\0" * 16)
        for src in (os.path.join("..", "..", outside.name, "payload.mp4"), str(outside / "payload.mp4")):
            with self.subTest(src=src):
                result, _ = self.run_plan(folder, "media", src)
                step = result["steps"][0]
                self.assertEqual(step["status"], "error", step)
                self.assertIn("leaves media_root", step["why"])
                self.assertNotIn("media", step)  # never resolved

    def test_a_media_root_outside_the_plan_folder_is_refused_unless_given_by_the_caller(self):
        folder = tmpdir()
        other = tmpdir()
        (other / "a.mp4").write_bytes(b"\0" * 16)
        with self.assertRaises(BridgeError) as cm:
            self.run_plan(folder, str(other), "a.mp4")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("BAD_PARAMS", "not_committed"))
        self.assertIn("--media-root", str(cm.exception))
        result, _ = self.run_plan(folder, ".", "a.mp4", cli_root=str(other))  # explicit: the caller's decision
        self.assertNotIn("leaves media_root", result["steps"][0].get("why", ""))
        self.assertEqual(Path(result["steps"][0]["media"]), (other / "a.mp4").resolve())

    def test_a_src_inside_media_root_is_planned_as_before(self):
        folder = tmpdir()
        (folder / "media" / "sub").mkdir(parents=True)
        (folder / "media" / "sub" / "a.mp4").write_bytes(b"\0" * 16)
        result, _ = self.run_plan(folder, "media", "sub/../sub/a.mp4")
        self.assertEqual(Path(result["steps"][0]["media"]), (folder / "media" / "sub" / "a.mp4").resolve())

    @unittest.skipUnless(os.name == "nt", "junctions")
    def test_a_junction_inside_the_plan_folder_does_not_lead_out(self):
        """QA-6: the containment holds on the real path too (a junction needs no rights to create)."""
        import _winapi
        folder, elsewhere = tmpdir(), tmpdir()
        (elsewhere / "broll.mp4").write_bytes(b"\0" * 16)
        (folder / "media").mkdir()
        _winapi.CreateJunction(str(elsewhere), str(folder / "media" / "lnk"))
        _winapi.CreateJunction(str(elsewhere), str(folder / "linked_media"))
        self.addCleanup(os.rmdir, folder / "media" / "lnk")
        self.addCleanup(os.rmdir, folder / "linked_media")
        result, _ = self.run_plan(folder, "media", "lnk/broll.mp4")
        step = result["steps"][0]
        self.assertEqual(step["status"], "error", step)
        self.assertIn("through a link", step["why"])
        self.assertNotIn("media", step)
        with self.assertRaises(BridgeError) as cm:  # media_root itself a junction out of the plan folder
            self.run_plan(folder, "linked_media", "broll.mp4")
        self.assertEqual(cm.exception.code, "BAD_PARAMS")
        self.assertIn("through a link", str(cm.exception))
        result, _ = self.run_plan(folder, ".", "broll.mp4", cli_root=str(folder / "linked_media"))  # explicit
        self.assertEqual(Path(result["steps"][0]["media"]), (elsewhere / "broll.mp4").resolve())


class PresetDoctypeTest(unittest.TestCase):
    """A document type declaration (where entities are defined) is refused wherever it sits in the prolog and in
    any encoding expat reads - the old byte search looked only at the first 64 KiB, in UTF-8."""

    BODY = '<PremiereData Version="3"><Tree ObjectID="1"/></PremiereData>'

    def read(self, data: bytes):
        path = tmpdir() / "x.prfpset"
        path.write_bytes(data)
        return prfpset.read_file(path)

    def test_doctype_after_a_long_comment_or_in_utf16_is_refused(self):
        dtd = '<!DOCTYPE PremiereData [<!ENTITY a "aaaa">]>'
        cases = {"late": ('<?xml version="1.0"?><!--' + "x" * 70000 + "-->" + dtd + self.BODY).encode("utf-8"),
                 "utf16": ("\ufeff" + '<?xml version="1.0" encoding="UTF-16"?>' + dtd + self.BODY).encode("utf-16-le")}
        for name, data in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(BridgeError) as cm:
                    self.read(data)
                self.assertIn("document type declaration", str(cm.exception))

    def test_a_plain_preset_file_still_reads(self):
        self.assertFalse(prfpset._has_doctype(('<?xml version="1.0"?><!-- ok -->' + self.BODY).encode("utf-8")))


class ShellSafeCommandTest(unittest.TestCase):
    """A suggested ffmpeg command never carries a file name a shell would expand: $ and ` (bash, PowerShell), % and !
    (cmd) inside double quotes."""

    def test_names_a_shell_would_expand_are_left_out(self):
        audio = {"hasAudio": True, "audioCodecs": ["flac"]}
        for name in ("$(calc).flac", "a`b`.flac", "%PATH%.flac", "wow!.flac"):
            with self.subTest(name=name):
                cmd = media.transcode_command(Path(tempfile.gettempdir()) / name, audio)
                self.assertNotIn(name.rsplit(".", 1)[0], cmd)
                self.assertIn("<input file", cmd)
        plain = media.transcode_command(Path(tempfile.gettempdir()) / "Bob's song (1).flac", audio)
        self.assertIn("Bob's song (1).flac", plain)


class ReportPathsTest(unittest.TestCase):
    """RB-5: any path-like text, not only the home folder, is cut out of a problem report."""

    def test_paths_outside_the_home_folder_are_redacted(self):
        for text in ("stale: D:" + B + "Job" + B + "Client" + B + "SECRET cut.prproj", UNC + "x.mov",
                     "file:///E" + ":/media/x.mp4", "at /Volumes/Media/x.mov"):
            with self.subTest(text=text):
                out = report.redact(text)
                self.assertIn("[path]", out)
                self.assertNotIn("SECRET", out)
                self.assertNotIn("x.m", out)
        self.assertEqual(report.redact("protocol 2, compatible"), "protocol 2, compatible")


class OtioTempFileTest(unittest.TestCase):
    """BB-9: replace_clip's OTIO export goes to a fresh private folder, never a fixed name in %TEMP% that a file left
    or planted there could answer for."""

    def test_the_export_lands_in_a_fresh_folder_and_a_planted_file_is_never_read(self):
        from ppro_bridge import replace
        planted = Path(tempfile.gettempdir()) / f"ppro_replace_{os.getpid()}.otio"
        planted.write_text(json.dumps({"planted": True}), encoding="utf-8")
        self.addCleanup(planted.unlink, True)
        seen = []

        def rpc(action, params):
            out = Path(params["outputPath"])
            seen.append(out)
            out.write_text(json.dumps({"OTIO_SCHEMA": "Timeline.1"}), encoding="utf-8")
            return {"exported": True, "outputPath": str(out)}

        self.assertEqual(replace.export_otio_doc(rpc), {"OTIO_SCHEMA": "Timeline.1"})
        out = seen[0]
        self.assertNotEqual(out, planted)
        self.assertTrue(out.parent.name.startswith("ppro_replace_"))
        self.assertFalse(out.parent.exists())  # removed again

        def silent(action, params):  # Premiere wrote nothing: an error, never the planted file
            seen.append(Path(params["outputPath"]))
            return {"exported": True}
        with self.assertRaises(FileNotFoundError):
            replace.export_otio_doc(silent)
        self.assertFalse(seen[-1].parent.exists())


class OtherProgramsTest(unittest.TestCase):
    def test_transcribe_refuses_a_share_before_anything_runs(self):
        for argv in ([UNC + "a.mp4", "--output", str(tmpdir() / "t.json")],
                     [str(tmpdir() / "a.mp4"), "--output", UNC + "t.json"],
                     [str(tmpdir() / "a.mp4"), "--output", str(tmpdir() / "t.bat")]):
            with self.subTest(argv=argv):
                args = ppro_transcribe.build_parser().parse_args(argv)
                with self.assertRaises(BridgeError) as cm:
                    ppro_transcribe._run(args)
                self.assertEqual(cm.exception.code, "BAD_PARAMS")

    def test_the_mcp_overwrite_gate_never_stats_a_share(self):
        with mock.patch.object(mcp_tools.Path, "resolve", side_effect=AssertionError("resolved a share")):
            self.assertFalse(mcp_tools._exists(UNC + "x.mp4"))

    def test_cli_json_outputs_must_be_json(self):
        rpc, _fake, calls = fake_rpc()
        out = io.StringIO()
        from ppro_bridge import cli
        with contextlib.redirect_stdout(out):
            cli.main(["inspect_items", "--output", str(tmpdir() / "Startup" / "x.bat")], rpc=rpc)
        reply = json.loads(out.getvalue())
        self.assertEqual(reply["code"], "BAD_PARAMS")
        self.assertIn(".json", reply["error"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
