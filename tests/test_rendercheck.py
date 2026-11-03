"""--render-check (ppro_bridge.rendercheck and its use by split_clip / cut_range / extend_cut).

The measurements run on synthetic signals (no ffmpeg, no Premiere): a deterministic band-limited "source" per media
file, a "render" built from it the way Premiere's mix holds a clip (at a gain, 21 ms early like its AAC decode, with
music from another track over it), and the B1 symptoms: digital silence, the piece missing under music, a blank
frame, a piece past the end Premiere reports. The command tests run split_clip / cut_range against the fake panel
with the render and the source decoder replaced, so they check the wiring: which pieces are checked, and that a
failed check is RENDER_CHECK_FAILED with outcome committed after a verified edit.
All data is synthetic; nothing connects anywhere.
"""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port)

import math
import os
import shutil
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

from helpers import BridgeError

from ppro_bridge import editing, ops, rendercheck, review

RATE = 16000
FREQS = {"/fake/interview.mp4": (211.3, 487.9, 1033.7), "/fake/music.wav": (173.1, 389.3, 911.9)}


def signal(path, t0, t1, rate=RATE, amp=2500.0):
    """The media's audio from t0 to t1 (source seconds): frequency-modulated tones below 1.5 kHz, so the signal only
    matches itself (no repeat within the +-50 ms the check searches) and survives the stdlib path's 4 kHz."""
    fs = FREQS.get(path, (300.0, 700.0, 1300.0))
    n = int(round((t1 - t0) * rate))

    def at(t):
        return sum(math.sin(2 * math.pi * f * t + 25.0 * math.sin(2 * math.pi * (5.3 + k) * t) + k)
                   for k, f in enumerate(fs))
    return array("h", (int(amp * at(t0 + i / rate) / 2) for i in range(n)))


def render_of(pieces, seconds, *, gain=1.0, lead=0.0214, music=None, rate=RATE):
    """A mono "render": each piece's source at `gain`, played `lead` s early (Premiere's AAC offset), plus music."""
    out = [0.0] * int(seconds * rate)
    for p in pieces:
        src = signal(p["mediaPath"], p["inPoint"] + lead, p["inPoint"] + lead + (p["end"] - p["start"]), rate)
        a = int(round(p["start"] * rate))
        for i, v in enumerate(src):
            if a + i < len(out):
                out[a + i] += gain * v
    if music:
        m = signal("/fake/music.wav", 0.0, seconds, rate, amp=music)
        out = [x + y for x, y in zip(out, m)]
    return array("h", (max(-32768, min(32767, int(v))) for v in out))


def piece(start, end, in_point, track="A1", path="/fake/interview.mp4"):
    return {"track": track, "name": Path(path).name, "start": start, "end": end, "inPoint": in_point,
            "outPoint": in_point + end - start, "mediaPath": path}


class AudioTest(unittest.TestCase):
    P = piece(2.0, 5.0, 10.0)

    def status(self, render, p=None, **kw):
        return rendercheck.check_audio(p or self.P, render, RATE, source=kw.pop("source", signal), **kw)

    def both(self):
        """Run the subtest body with numpy (when importable) and with the stdlib path."""
        modes = [("numpy", None)] if review._np() is not None else []
        return modes + [("stdlib", [None])]

    def test_a_piece_that_plays_is_ok_with_its_gain_and_the_aac_lead(self):
        for mode, forced in self.both():
            with self.subTest(mode=mode), mock.patch.object(review, "_NUMPY", forced or review._NUMPY):
                out = self.status(render_of([self.P], 8.0, gain=0.5))
                self.assertEqual(out["status"], "ok", out)
                self.assertGreater(out["correlation"], 0.95 if mode == "numpy" else 0.8)  # stdlib: 4 kHz
                self.assertAlmostEqual(out["gainDb"], -6.0, delta=1.0 if mode == "numpy" else 2.0)
                self.assertAlmostEqual(out["lagMs"], -21.4, delta=0.3 if mode == "numpy" else 0.5)

    def test_music_over_it_does_not_hide_whether_it_plays(self):
        for mode, forced in self.both():
            with self.subTest(mode=mode), mock.patch.object(review, "_NUMPY", forced or review._NUMPY):
                self.assertEqual(self.status(render_of([self.P], 8.0, music=2500.0))["status"], "ok")
                out = self.status(render_of([], 8.0, music=2500.0))  # B1 under a music bed: the piece is gone
                self.assertEqual(out["status"], "missing", out)
                self.assertGreater(out["renderDb"], -30)
                self.assertLess(out["correlation"], rendercheck.CORR_MISSING)

    def test_b1_digital_silence(self):
        out = self.status(render_of([], 8.0))
        self.assertEqual(out["status"], "silent")
        self.assertEqual(out["renderDb"], -180.0)
        self.assertGreater(out["sourceDb"], -30)

    def test_not_judged(self):
        self.assertEqual(self.status(render_of([], 4.0))["status"], "notRendered")  # past the render's end
        self.assertEqual(self.status(render_of([], 8.0), piece(2.0, 2.1, 0.0))["status"], "tooShort")
        self.assertEqual(self.status(render_of([], 8.0), dict(self.P, mediaPath=None))["status"], "unverified")
        quiet = self.status(render_of([], 8.0), source=lambda path, t0, t1, rate: signal(path, t0, t1, rate, amp=1))
        self.assertEqual(quiet["status"], "quietSource")

        def broken(path, t0, t1, rate):
            raise OSError("moov atom not found")
        out = self.status(render_of([], 8.0), source=broken)
        self.assertEqual(out["status"], "unverified")
        self.assertIn("moov atom", out["detail"])


class VideoTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ppro_rendercheck_"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def png(self, name, mode, colour):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("needs Pillow")
        path = self.dir / name
        Image.new(mode, (40, 60), colour).save(path)
        return path

    def exporter(self, path):
        def export(rpc, directory, at, name):
            self.asked.append(at)
            return {"resolvedPath": str(path)}
        self.asked = []
        return export

    def test_blank_and_drawn_frames(self):
        p = piece(2.0, 4.0, 0.0, track="V1")
        for name, mode, colour, want in (("clear.png", "RGBA", (0, 0, 0, 0), "blank"),
                                         ("drawn.png", "RGBA", (10, 20, 30, 255), "ok"),
                                         ("black.png", "RGB", (4, 4, 4), "blank"),
                                         ("picture.png", "RGB", (40, 70, 120), "ok")):
            with self.subTest(frame=name):
                out = rendercheck.check_video(None, p, self.dir, 10.0, 59.94, [],
                                              export=self.exporter(self.png(name, mode, colour)))
                self.assertEqual(out["status"], want, out)
                self.assertEqual(self.asked, [3.0])  # the middle of the piece

    def test_a_drawn_frame_with_another_track_there_proves_nothing(self):
        """QA F-RC1: a blank V1 piece under a V2 graphic still gives a drawn frame (the graphic's pixels)."""
        out = rendercheck.check_video(None, piece(2.0, 4.0, 0.0, track="V1"), self.dir, 10.0, 59.94, ["V2"],
                                      export=self.exporter(self.png("graphic.png", "RGBA", (200, 200, 200, 255))))
        self.assertEqual((out["status"], out["covered"]), ("uncertain", ["V2"]))
        self.assertIn("not measured", out["detail"])
        out = rendercheck.check_video(None, piece(2.0, 4.0, 0.0, track="V1"), self.dir, 10.0, 59.94, ["V2"],
                                      export=self.exporter(self.png("empty.png", "RGBA", (0, 0, 0, 0))))
        self.assertEqual(out["status"], "blank")  # nothing drawn at all is still a definite failure

    def test_a_piece_past_the_end_premiere_reports_is_not_rendered_and_not_exported(self):
        out = rendercheck.check_video(None, piece(12.0, 14.0, 0.0, track="V1"), self.dir, 10.0, 59.94, ["V2"],
                                      export=self.exporter(self.dir / "never.png"))
        self.assertEqual(out["status"], "notRendered")
        self.assertEqual(out["covered"], ["V2"])
        self.assertEqual(self.asked, [])


class CheckTest(unittest.TestCase):
    """rendercheck.check end to end on a stub rpc: one render for the audio pieces, one frame per video piece."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ppro_rendercheck_"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_check(self, pieces, render, out_dir=None, seen=None):
        wav = self.dir / "mix.wav"
        review.write_wav(wav, render, RATE)
        rendered = self.dir / "mix_render.wav"
        rendered.write_bytes(b"RIFF")

        def export_audio(rpc, *, output, render, preset):
            self.assertTrue(render)
            return {"wav": str(wav), "rendered": str(rendered)}

        def rpc(action, params=None):
            if action == "get_sequence":
                return {"durationSeconds": 8.0, "timebase": 4237833600}
            if action == "list_items":
                return {"video": [{"label": "V1", "items": [{"start": 0.0, "end": 8.0}]}]}
            raise AssertionError(action)

        frame = self.dir / "frame.png"
        try:
            from PIL import Image
            Image.new("RGB", (8, 8), (40, 70, 120)).save(frame)
        except ImportError:
            pass
        def export(rpc, d, at, name):
            if seen is not None:
                seen.append(str(d))
            return {"resolvedPath": str(frame)}
        with mock.patch.object(review, "export_audio", export_audio):
            return rendercheck.check(rpc, pieces, out_dir=out_dir or self.dir / "check", source=signal,
                                     export=export), rendered

    def test_all_pieces_play(self):
        audio = [piece(0.5, 3.0, 10.0), piece(3.0, 6.0, 20.0)]
        out, rendered = self.run_check(audio + [piece(0.5, 3.0, 10.0, track="V1")], render_of(audio, 8.0))
        self.assertTrue(out["ok"], out["problems"])
        self.assertEqual([p["status"] for p in out["pieces"]][:2], ["ok", "ok"])
        self.assertEqual(out["render"]["renderedSeconds"], 8.0)
        self.assertFalse(rendered.exists())  # the full-rate render is deleted when everything plays

    def test_a_relative_render_dir_reaches_the_panel_absolute(self):
        """Live 3 Nov 2026: a relative --render-dir went to Premiere as is, and export_frame failed (-1610153462)."""
        seen = []
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            out, _ = self.run_check([piece(0.5, 3.0, 10.0, track="V1")], render_of([], 8.0), out_dir="rel",
                                    seen=seen)
        finally:
            os.chdir(cwd)
        self.assertTrue(Path(out["dir"]).is_absolute())
        self.assertTrue(seen and all(Path(d).is_absolute() for d in seen), seen)

    def test_disabled_pieces_are_listed_not_measured(self):
        audio = [piece(0.5, 3.0, 10.0), dict(piece(3.0, 6.0, 20.0), disabled=True)]
        video = [dict(piece(3.0, 6.0, 20.0, track="V1"), disabled=True)]
        out, _ = self.run_check(audio + video, render_of(audio[:1], 8.0))
        self.assertTrue(out["ok"], out["problems"])
        self.assertEqual(sorted((p["track"], p["status"]) for p in out["pieces"]),
                         [("A1", "disabled"), ("A1", "ok"), ("V1", "disabled")])

    def test_a_silent_piece_fails_and_keeps_the_render(self):
        audio = [piece(0.5, 3.0, 10.0), piece(3.0, 6.0, 20.0)]
        out, rendered = self.run_check(audio, render_of(audio[:1], 8.0))
        self.assertFalse(out["ok"])
        self.assertEqual([p["status"] for p in out["pieces"]], ["ok", "silent"])
        self.assertEqual(len(out["problems"]), 1)
        self.assertIn("A1 3.000-6.000s", out["problems"][0])
        self.assertTrue(rendered.exists())


# ---------------------------------------------------------------- the commands

class CommandTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        for key in [k for k in os.environ if k.startswith("PPRO_FAKE_")]:
            del os.environ[key]
        self.rpc, self.fake, self.calls = helpers.fake_rpc()
        self.addCleanup(self.fake.reset)
        self.dir = Path(tempfile.mkdtemp(prefix="ppro_rendercheck_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.checked = []

    def checker(self, broken=()):
        """A render check that records the pieces and plays all of them except the ones starting at `broken`."""
        def check(rpc, pieces):
            self.checked.append(pieces)
            bad = [p for p in pieces if any(abs(p["start"] - b) < 0.01 for b in broken)]
            return {"ok": not bad, "pieces": [dict(p, status="silent" if p in bad else "ok") for p in pieces],
                    "problems": [f"{p['track']} {p['start']:.3f}: silent" for p in bad], "warnings": []}
        return check

    def test_split_checks_both_pieces(self):
        out = editing.split_clip(self.rpc, track="A1", at=20.0, label="Split", render_check=self.checker())
        self.assertTrue(out["renderCheck"]["ok"])
        (pieces,) = self.checked
        self.assertEqual([(p["track"], round(p["start"], 2), round(p["end"], 2)) for p in pieces],
                         [("A1", 0.0, 20.0), ("A1", 20.0, 60.0)])  # the left (trimmed) and the right (cloned) piece
        self.assertEqual(pieces[1]["mediaPath"], pieces[0]["mediaPath"])

    def test_a_failed_check_is_committed_after_a_verified_edit(self):
        with self.assertRaises(BridgeError) as cm:
            editing.split_clip(self.rpc, track="V1", at=20.0, label="Split", render_check=self.checker(broken=[20.0]))
        e = cm.exception
        self.assertEqual((e.code, e.outcome), ("RENDER_CHECK_FAILED", "committed"))
        self.assertTrue(e.data["verified"])
        self.assertEqual(e.data["undoSteps"], 5)
        self.assertFalse(e.data["renderCheck"]["ok"])
        self.assertIn("insert_clip", e.hint)  # how to repair a silent piece
        self.assertEqual(len(self.track("V1")), 2)  # the edit stays

    def test_a_dry_run_renders_nothing(self):
        out = editing.split_clip(self.rpc, track="V1", at=20.0, label="Split", dry_run=True,
                                 render_check=self.checker())
        self.assertIn("skipped", out["renderCheck"])
        self.assertEqual(self.checked, [])

    def test_a_render_that_cannot_run_is_reported_after_the_edit(self):
        def check(rpc, pieces):
            raise BridgeError("exportSequence failed", code="EXPORT_RUNNING", outcome="not_committed")
        with self.assertRaises(BridgeError) as cm:
            editing.split_clip(self.rpc, track="V1", at=20.0, label="Split", render_check=check)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("RENDER_CHECK_FAILED", "committed"))
        self.assertEqual(cm.exception.data["renderCheck"]["error"]["code"], "EXPORT_RUNNING")

    def test_cut_range_checks_what_it_split_trimmed_and_moved(self):
        # V1/A1 interview 0-60 s: cut 14-20 s, everything after moves left
        out = editing.cut_range(self.rpc, start=14.0, end=20.0, tracks=["V1", "A1"], label="Cut",
                                render_check=self.checker())
        self.assertTrue(out["verified"])
        (pieces,) = self.checked
        self.assertEqual(sorted((p["track"], round(p["start"], 2)) for p in pieces),
                         [("A1", 0.0), ("A1", 14.0), ("V1", 0.0), ("V1", 14.0)])

    def test_ops_refuses_without_ffmpeg_before_writing(self):
        with mock.patch.object(review.shutil, "which", return_value=None):
            with self.assertRaises(BridgeError) as cm:
                ops.split_clip(self.rpc, track="V1", at=20.0, label="Split", render_check=True)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("MISSING_DEPENDENCY", "not_committed"))
        self.assertFalse([a for a, p in self.calls if a in ("clone_clip", "trim_clip", "remove_items")])

    def track(self, label):
        kind = "video" if label[0] == "V" else "audio"
        return self.fake.STATE[kind][int(label[1:]) - 1]


if __name__ == "__main__":
    unittest.main()
