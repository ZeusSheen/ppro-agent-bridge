"""Every path the CLI hands Premiere or opens itself goes through ppro_bridge/paths.check first (RB-3, RB-4, RB-6):
a network share is refused before any file call touches it, and so is a local path whose links lead to one.

The registry: every string option of every CLI command is either a path (PATH_DESTS) or not (NON_PATH_DESTS), and every
path option of every command has a case in CASES that runs the command with a network path there. A new option, or a
new command with a path option, fails test_every_option_is_classified / test_every_path_option_has_a_case until it is
listed - and its case then shows it is checked: the run must refuse (BAD_PARAMS, or an apply_plan step error) while
no file call ever sees the share (open, stat, mkdir, listdir, realpath, a subprocess argument), and no write reaches
the panel. The share's name (.invalid) cannot resolve, and the recorder refuses every call that names it anyway.
"""

import argparse
import contextlib
import io
import json
import ntpath
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port)
from helpers import fake_rpc

from ppro_bridge import cli, paths, spec

B = "\\"
HOST = "pab-guard.invalid"
UNC = B * 2 + HOST + B + "share" + B

# Options that name a file or folder (checked), and the string options that do not.
PATH_DESTS = {"output", "dir", "sheet", "plan", "media_root", "report", "file", "srt", "render_dir", "preset",
              "json_file", "ops", "path", "to", "words", "out_dir", "diff"}
NON_PATH_DESTS = {"track", "name", "label", "expect_seq", "expect_fingerprint", "comment", "color", "match_name", "bin",
                  "only", "locale", "search", "captions", "format", "job", "item", "with_item", "atrack",
                  "skip_effects", "guid", "language", "at", "extend"}

SEEN = []  # every file call that named the share, while RECORDING
RECORDING = []


def _names_share(value) -> bool:
    if isinstance(value, (list, tuple)):
        return any(_names_share(v) for v in value)
    return isinstance(value, (str, bytes, os.PathLike)) and HOST in os.fsdecode(value) if not isinstance(
        value, int) else False


def _audit(event, args):
    if RECORDING and event in ("open", "os.mkdir", "os.listdir", "os.scandir", "os.rename", "os.remove", "os.rmdir",
                               "subprocess.Popen", "shutil.copyfile", "os.startfile") and _names_share(args):
        SEEN.append((event, str(args)[:200]))
        raise PermissionError(f"test: {event} reached the share")


sys.addaudithook(_audit)


def _guarded(fn, label):
    def call(*args, **kw):
        if args and _names_share(args[0]):
            SEEN.append((label, str(args[0])[:200]))
            raise PermissionError(f"test: {label} reached the share")
        return fn(*args, **kw)
    return call


@contextlib.contextmanager
def recording():
    """Record (and refuse) every file call that names the share: the audit hook covers open, mkdir, listdir,
    scandir and subprocesses; stat, lstat, realpath and os.path's checks are wrapped here."""
    patches = [mock.patch.object(os, "stat", _guarded(os.stat, "os.stat")),
               mock.patch.object(os, "lstat", _guarded(os.lstat, "os.lstat")),
               mock.patch.object(ntpath, "_getfinalpathname", _guarded(ntpath._getfinalpathname, "realpath"))]
    for name in ("isfile", "isdir", "exists", "lexists", "islink", "getsize"):
        patches.append(mock.patch.object(ntpath, name, _guarded(getattr(ntpath, name), f"os.path.{name}")))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        RECORDING.append(True)
        try:
            yield
        finally:
            RECORDING.pop()


def string_options():
    """{(command, dest): option string} for every option that takes a string (not a number, a choice or a flag)."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    out = {}
    for name, p in sub.choices.items():
        for a in p._actions:
            if isinstance(a, argparse._HelpAction) or a.nargs == 0 or a.type in (float, int) or a.choices:
                continue
            out[(name, a.dest)] = a.option_strings[0] if a.option_strings else "(positional)"
    return out


class PathGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pab_guard_"))
        cls.epr = cls.tmp / "h264.epr"
        cls.epr.write_bytes(b"<PremiereData Version=\"3\"><ExporterFileType>"
                            + str(int.from_bytes(b"H264", "big")).encode() + b"</ExporterFileType></PremiereData>")
        cls.plan = cls.tmp / "plan.json"
        cls.plan.write_text(json.dumps({"sequence": "Sequence 01", "media_root": ".", "commands": [
            {"command": "insert_clip", "id": "a", "track": "V1", "timeline_at": 0, "src": "a.mp4", "src_out": 1}]}),
            encoding="utf-8")
        t = str(cls.tmp)
        cls.CASES = {
            ("inspect_items", "output"): ["inspect_items", "--output", UNC + "i.json"],
            ("export_frame", "dir"): ["export_frame", "--dir", UNC + "frames"],
            ("export_frames", "dir"): ["export_frames", "--dir", UNC + "frames", "--at", "1"],
            ("export_frames", "sheet"): ["export_frames", "--dir", t, "--at", "1", "--sheet", UNC + "s.jpg"],
            ("export_otio", "output"): ["export_otio", "--output", UNC + "t.otio"],
            ("probe_api", "output"): ["probe_api", "--output", UNC + "d.json"],
            ("probe_api", "diff"): ["probe_api", "--diff", UNC + "old.json"],
            ("export_transcript", "output"): ["export_transcript", "--output", UNC + "t.json"],
            ("import_transcript", "file"): ["import_transcript", "--item", "x", "--file", UNC + "t.json"],
            ("transcribe_clip", "output"): ["transcribe_clip", "--item", "x", "--output", UNC + "t.json"],
            ("import_media", "path"): ["import_media", "--path", UNC + "a.mp4"],
            ("apply_plan", "plan"): ["apply_plan", UNC + "plan.json", "--dry-run"],
            ("apply_plan", "report"): ["apply_plan", str(cls.plan), "--dry-run", "--report", UNC + "r.json"],
            ("apply_plan", "media_root"): ["apply_plan", str(cls.plan), "--dry-run", "--media-root", UNC + "media"],
            ("set_params", "json_file"): ["set_params", "--track", "V1", "--at", "1", "--json", UNC + "p.json"],
            ("apply_ops", "ops"): ["apply_ops", "--json", UNC + "ops.json", "--label", "x"],
            ("relink", "to"): ["relink", "--item", "x", "--to", UNC + "a.mov"],
            ("cut_range", "srt"): ["cut_range", "--from", "1", "--to", "2", "--captions", "V3", "--srt", UNC + "c.srt"],
            ("cut_range", "render_dir"): ["cut_range", "--from", "1", "--to", "2", "--dry-run", "--render-check",
                                          "--render-dir", UNC + "r"],
            ("cut_range", "preset"): ["cut_range", "--from", "1", "--to", "2", "--render-check", "--preset",
                                      UNC + "p.epr"],
            ("extend_cut", "srt"): ["extend_cut", "--at", "1", "--out", "0.3", "--captions", "V3", "--srt", UNC + "c.srt"],
            ("extend_cut", "render_dir"): ["extend_cut", "--at", "1", "--dry-run", "--render-check", "--render-dir",
                                           UNC + "r"],
            ("extend_cut", "preset"): ["extend_cut", "--at", "1", "--render-check", "--preset", UNC + "p.epr"],
            ("split_clip", "render_dir"): ["split_clip", "--track", "V1", "--at", "1", "--dry-run", "--render-check",
                                           "--render-dir", UNC + "r"],
            ("split_clip", "preset"): ["split_clip", "--track", "V1", "--at", "1", "--render-check", "--preset",
                                       UNC + "p.epr"],
            ("conform_captions", "srt"): ["conform_captions", "--track", "V3", "--srt", UNC + "c.srt"],
            ("review_cuts", "words"): ["review_cuts", "--words", UNC + "w.json"],
            ("review_cuts", "out_dir"): ["review_cuts", "--out-dir", UNC + "wav"],
            ("review_cuts", "output"): ["review_cuts", "--output", UNC + "r.json"],
            ("review_cuts", "preset"): ["review_cuts", "--render", "--preset", UNC + "p.epr"],
            ("export_audio", "output"): ["export_audio", "--output", UNC + "a.wav"],
            ("export_audio", "preset"): ["export_audio", "--output", str(cls.tmp / "a.wav"), "--render", "--preset",
                                         UNC + "p.epr"],
            ("export_video", "output"): ["export_video", "--preset", str(cls.epr), "--output", UNC + "x.mp4"],
            ("export_video", "preset"): ["export_video", "--preset", UNC + "p.epr", "--output",
                                         str(cls.tmp / "x.mp4")],
            ("export_aaf", "output"): ["export_aaf", "--output", UNC + "c.aaf"],
            ("export_xml", "output"): ["export_xml", "--output", UNC + "c.xml"],
            ("list_effect_presets", "file"): ["list_effect_presets", "--file", UNC + "x.prfpset"],
            ("apply_preset", "file"): ["apply_preset", "--track", "V1", "--at", "1", "--preset", "n", "--file",
                                       UNC + "x.prfpset"],
            # a preset NAME (looked up in .prfpset files, never opened): no file call may see it all the same
            ("apply_preset", "preset"): ["apply_preset", "--track", "V1", "--at", "1", "--preset", UNC + "x"],
        }
        cls.NAMES = {("apply_preset", "preset")}

    def test_every_option_is_classified(self):
        unknown = sorted(f"{c} {opt} (dest {d})" for (c, d), opt in string_options().items()
                         if d not in PATH_DESTS | NON_PATH_DESTS)
        self.assertEqual(unknown, [], "a new string option: add its dest to PATH_DESTS (it names a file or folder: "
                                      "paths.check it, and add a case) or to NON_PATH_DESTS")

    def test_every_path_option_has_a_case(self):
        missing = sorted(f"{c} {opt}" for (c, d), opt in string_options().items()
                         if d in PATH_DESTS and (c, d) not in self.CASES)
        self.assertEqual(missing, [], "a path option without a case in CASES: add one (with a network path there)")

    def test_a_network_path_is_refused_before_any_file_call_sees_it(self):
        for key, argv in sorted(self.CASES.items()):
            with self.subTest(case=" ".join(key)):
                rpc, _fake, calls = fake_rpc()
                out = io.StringIO()
                SEEN.clear()
                with recording(), contextlib.redirect_stdout(out):
                    try:
                        cli.main(argv, rpc=rpc)
                    except SystemExit as e:  # argparse
                        self.fail(f"{argv}: argparse exit {e.code}")
                self.assertEqual(SEEN, [], f"{argv}: a file call reached the share")
                reply = json.loads(out.getvalue())
                self.assertFalse(reply["ok"], reply)
                writes = [a for a, _ in calls if spec.kind(a) == "write"]
                self.assertEqual(writes, [], f"{argv}: wrote to the panel")
                if key in self.NAMES:
                    continue
                if key == ("apply_plan", "media_root"):  # every step refuses its src
                    steps = reply["data"]["steps"]
                    self.assertTrue(steps and all(s["status"] == "error" and "network (UNC)" in s["why"]
                                                  for s in steps), steps)
                else:
                    self.assertEqual(reply.get("code"), "BAD_PARAMS", reply)
                    self.assertIn("network (UNC)", reply.get("error", ""), reply)


@unittest.skipUnless(os.name == "nt", "links and junctions as Windows has them")
class LinkGuardTest(unittest.TestCase):
    """A local path through a symbolic link to a share (open issue 3) is refused as a share typed out would be; the
    link itself is only read (lstat, readlink), never followed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pab_links_"))
        self.addCleanup(self._clean)
        self.share_link = self.tmp / "media"
        try:
            os.symlink(UNC.rstrip(B), self.share_link, target_is_directory=True)
        except OSError as e:
            self.skipTest(f"no right to create symbolic links here: {e}")
        (self.tmp / "real").mkdir()
        import _winapi
        _winapi.CreateJunction(str(self.tmp / "real"), str(self.tmp / "junction"))
        os.symlink(os.path.join("..", "media"), self.tmp / "real" / "back", target_is_directory=True)

    def _clean(self):
        for p in (self.share_link, self.tmp / "junction", self.tmp / "real" / "back"):
            with contextlib.suppress(OSError):
                os.rmdir(p)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_links_to_a_share_are_refused_without_following_them(self):
        SEEN.clear()
        for typed in (self.share_link / "a.mp4", self.share_link,
                      self.tmp / "junction" / "back" / "a.mp4",  # a junction, then a relative link to the share
                      B * 2 + "?" + B + str(self.share_link / "a.mp4"),  # the long form
                      str(self.tmp / "real" / ".." / "media" / "a.mp4").replace(B, "/")):
            with self.subTest(path=str(typed)), recording():
                self.assertIn("network (UNC)", paths.link_problem(typed) or "")
                with self.assertRaises(paths.BridgeError) as cm:
                    paths.check(typed, what="--path")
                self.assertEqual(cm.exception.code, "BAD_PARAMS")
        self.assertEqual(SEEN, [])

    def test_local_links_and_folders_to_come_pass(self):
        for typed in (self.tmp / "junction" / "x.otio", self.tmp / "real" / "new" / "deeper" / "x.otio"):
            with self.subTest(path=str(typed)):
                self.assertIsNone(paths.link_problem(typed))
                self.assertEqual(paths.check(typed), str(typed))
        self.assertEqual(paths.resolved(self.tmp / "junction" / "x.otio"), (self.tmp / "real" / "x.otio").resolve())

    def test_import_media_through_the_link_is_refused_and_never_touches_the_share(self):
        rpc, _fake, calls = fake_rpc()
        out = io.StringIO()
        SEEN.clear()
        with recording(), contextlib.redirect_stdout(out):
            cli.main(["import_media", "--path", str(self.share_link / "a.mp4")], rpc=rpc)
        reply = json.loads(out.getvalue())
        self.assertEqual(reply.get("code"), "BAD_PARAMS", reply)
        self.assertIn("symbolic link or junction", reply["error"])
        self.assertEqual((SEEN, [a for a, _ in calls if spec.kind(a) == "write"]), ([], []))


class ResolvedTest(unittest.TestCase):
    def test_a_mapped_drive_keeps_its_letter(self):
        """Path.resolve turns a mapped drive's path into its \\\\server\\share form: the panel would refuse that as UNC."""
        typed = "Z:" + B + "renders" + B + "cut.otio"
        with mock.patch.object(Path, "resolve", lambda self, strict=False: Path(B * 2 + "srv" + B + "share" + B
                                                                                 + "renders" + B + "cut.otio")):
            self.assertEqual(str(paths.resolved(typed)), typed)

    def test_a_local_path_is_resolved_as_before(self):
        p = Path(tempfile.gettempdir()) / "pab" / ".." / "x.otio"
        self.assertEqual(paths.resolved(p), p.resolve())


if __name__ == "__main__":
    unittest.main()
