"""review: the listening report and the timeline lint, on synthetic audio (tone bursts as "words" over low noise,
written with the standard library). The per-cut analysis, the lint, the word checks and pad_cut_points are pure
and always run; the shadow mix, review_cuts end to end (stub panel) and the render/loudness path need ffmpeg and
are skipped without it. Nothing here talks to Premiere or a broker."""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port)

import json
import math
import random
import shutil
import subprocess
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

from helpers import BridgeError, stub_rpc

from ppro_bridge import review

RATE = 16000
FPS = 60000 / 1001
TIMEBASE = "4237833600"
TICK_FRAME = int(TIMEBASE)
TPS = 254016000000
FRAME = 1 / FPS
WORDS = [(0.5, 1.2), (1.6, 2.4), (3.0, 3.8), (4.5, 5.3), (6.0, 6.9), (7.5, 8.2), (8.8, 9.5)]  # source seconds
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = unittest.skipUnless(FFMPEG, "needs ffmpeg on PATH")


def synth(seconds=10.0, words=WORDS, *, noise=50, dc=0, amp=6000, freq=300.0, seed=1) -> array:
    """Uniform noise (+ DC) with 300 Hz tone bursts where the words are, 10 ms raised-cosine edges."""
    rnd = random.Random(seed)
    n = int(seconds * RATE)
    env = [0.0] * n
    ramp = int(0.010 * RATE)
    for s, e in words:
        i0, i1 = int(s * RATE), min(n, int(e * RATE))
        for i in range(i0, i1):
            g = 1.0
            if i - i0 < ramp:
                g = 0.5 - 0.5 * math.cos(math.pi * (i - i0) / ramp)
            if i1 - i < ramp:
                g = min(g, 0.5 - 0.5 * math.cos(math.pi * (i1 - i) / ramp))
            env[i] = g
    w = 2 * math.pi * freq / RATE
    return array("h", (max(-32768, min(32767, int(dc + rnd.randint(-noise, noise) + amp * env[i] * math.sin(w * i))))
                       for i in range(n)))


QUIET = synth()
QUIET_DC = synth(dc=5000, seed=3)          # same floor, DC offset: a cut into it steps the waveform (a click)
LOUD_DC = synth(noise=400, dc=5000, seed=2)  # 18 dB more room tone, same DC: a level jump, no click
SOURCES = {"quiet.wav": QUIET, "quiet_dc.wav": QUIET_DC, "loud_dc.wav": LOUD_DC}

# The cut-down: (source, in, out); clips laid end to end on A1 (and mirrored on V1).
SLIVER = 19 * FRAME  # 19 frames at 59.94 — the "weird cut"
PLAN = [
    ("quiet.wav", 0.2, 2.8),               # c1
    ("quiet.wav", 4.2, 5.35),              # c2  cut 2.6: 0.4 s tail, 0.3 s head -> clean
    ("quiet.wav", 5.9, 6.5),               # c3  cut 3.75: 0.05 s tail, 0.10 s head -> TIGHT_OUT + TIGHT_IN
    ("quiet.wav", 7.2, 8.6),               # c4  cut 4.35: c3 ends inside a word -> CLIPPED
    ("quiet.wav", 2.45, 2.45 + SLIVER),    # c5  cut 5.75: room-tone sliver of an old take -> lint SLIVER
    ("quiet_dc.wav", 2.7, 4.2),            # c6  cut 6.067: waveform step -> CLICK
    ("loud_dc.wav", 2.7, 4.2),             # c7  cut 7.567: room tone +18 dB -> LEVEL_JUMP
]


def timeline(plan=PLAN):
    """[(name, source, in, out, start, end)] with clips end to end from 0."""
    out, t = [], 0.0
    for k, (src, a, b) in enumerate(plan, 1):
        out.append((f"c{k}", src, a, b, t, t + (b - a)))
        t += b - a
    return out


CLIPS = timeline()
CUT = {name: start for name, _, _, _, start, _ in CLIPS}  # cut into clip -> sequence seconds


def place(clips=CLIPS) -> array:
    """The mix as the shadow mix should build it: source samples at their sequence positions."""
    total = int(math.ceil(clips[-1][5] * RATE))
    out = array("h", bytes(2 * total))
    for _, src, a, b, start, end in clips:
        s0, s1 = int(round(start * RATE)), int(round(end * RATE))
        i0 = int(round(a * RATE))
        out[s0:s1] = SOURCES[src][i0:i0 + (s1 - s0)]
    return out


def items_doc(folder: Path, clips=CLIPS, video=True, **extra) -> dict:
    def item(name, src, a, b, start, end):
        d = {"name": name, "start": start, "end": end, "inPoint": a, "outPoint": b, "speed": 1, "disabled": False,
             "projectItem": src, "mediaPath": str(folder / src)}
        d.update(extra.get(name, {}))
        return d

    rows = [item(*c) for c in clips]
    doc = {"sequence": "Cutdown", "audio": [{"index": 0, "label": "A1", "name": "Audio 1", "items": rows},
                                             {"index": 1, "label": "A2", "name": "Audio 2", "items": []}]}
    if video:
        doc["video"] = [{"index": 0, "label": "V1", "name": "Video 1", "items": [dict(r) for r in rows]}]
    return doc


def tmpdir() -> Path:
    d = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(d.cleanup)
    return Path(d.name)


def write_sources(folder: Path):
    for name, samples in SOURCES.items():
        review.write_wav(folder / name, samples, RATE)


def flags_at(report, at, tol=1e-3):
    hits = [c for c in report["cuts"] if abs(c["at"] - at) <= tol]
    assert len(hits) == 1, (at, [c["at"] for c in report["cuts"]])
    return hits[0]["flags"]


def analyse(clips, k, **kw):
    """analyse_cut at the cut into clips[k] (k >= 1) of the placed mix."""
    mix = place(clips)
    prev, cur = clips[k - 1], clips[k]
    return review.analyse_cut(mix, RATE, cur[4], outgoing=(prev[4], cur[4]), incoming=(cur[4], cur[5]), **kw)


# ---------------------------------------------------------------- pure analysis

class AnalyseCutTest(unittest.TestCase):
    def test_clean_cut_has_no_flags_and_measures_the_pauses(self):
        r = analyse(CLIPS, 1)
        self.assertEqual(r["flags"], [])
        self.assertAlmostEqual(r["silenceBefore"], 0.40, delta=0.02)
        self.assertAlmostEqual(r["silenceAfter"], 0.30, delta=0.02)
        self.assertAlmostEqual(r["floorBefore"], r["floorAfter"], delta=1.5)

    def test_tight_cut(self):
        r = analyse(CLIPS, 2)
        self.assertEqual(r["flags"], ["TIGHT_OUT", "TIGHT_IN"])
        self.assertAlmostEqual(r["silenceBefore"], 0.05, delta=0.015)
        self.assertAlmostEqual(r["silenceAfter"], 0.10, delta=0.015)
        self.assertIn("0.05 s", r["details"]["TIGHT_OUT"])

    def test_breathing_room_follows_the_requested_rolls(self):
        r = analyse(CLIPS, 1, pre_roll=0.5, post_roll=0.5)
        self.assertEqual(r["flags"], ["TIGHT_OUT", "TIGHT_IN"])
        self.assertEqual(analyse(CLIPS, 2, pre_roll=0.05, post_roll=0.02)["flags"], [])

    def test_clipped_word(self):
        r = analyse(CLIPS, 3)
        self.assertIn("CLIPPED", r["flags"])
        self.assertIn("TIGHT_OUT", r["flags"])
        self.assertNotIn("TIGHT_IN", r["flags"])
        self.assertLess(r["silenceBefore"], 0.03)
        self.assertIn("outgoing", r["details"]["CLIPPED"])

    def test_a_syllable_cut_off_after_a_stop_closure_is_clipped(self):
        """The real-job miss: "hospi-" (word), 70 ms stop closure, then "-tal" cut 17 ms in. The burst at the cut is
        shorter than SPEECH_WINDOWS, but sound touching the cut is a chopped syllable, not 0.09 s of quiet."""
        src = synth(seconds=3.0, words=[(0.5, 1.2), (1.27, 1.33), (1.40, 2.0)], seed=5)
        for cut, clipped in ((1.287, True), (1.36, False)):  # 17 ms into "-tal": two 10 ms windows
            n = int(round(cut * RATE))
            mix = src[:n] + QUIET[int(2.45 * RATE):int(3.5 * RATE)]
            r = review.analyse_cut(mix, RATE, cut, outgoing=(0.0, cut), incoming=(cut, cut + 1.05))
            self.assertEqual("CLIPPED" in r["flags"], clipped, (cut, r))
            if clipped:
                self.assertEqual(r["silenceBefore"], 0.0)
                self.assertIn("outgoing", r["details"]["CLIPPED"])
            else:
                self.assertAlmostEqual(r["silenceBefore"], 0.03, delta=0.015)

    def test_incoming_mid_word_is_clipped_too(self):
        clips = timeline([("quiet.wav", 0.2, 2.8), ("quiet.wav", 4.8, 6.0)])
        r = analyse(clips, 1)
        self.assertIn("CLIPPED", r["flags"])
        self.assertIn("mid-word", r["details"]["CLIPPED"])

    def test_level_jump(self):
        r = analyse(CLIPS, 6)
        self.assertEqual(r["flags"], ["LEVEL_JUMP"])
        self.assertGreater(r["levelJump"], 12)

    def test_click(self):
        r = analyse(CLIPS, 5)
        self.assertEqual(r["flags"], ["CLICK"])
        self.assertGreater(r["clickRatio"], 10)

    def test_dc_offset_is_not_sound(self):
        clips = timeline([("quiet_dc.wav", 0.2, 2.8), ("quiet_dc.wav", 4.2, 5.8)])
        self.assertEqual(analyse(clips, 1)["flags"], [])

    def test_quiet_is_measured_across_a_room_tone_sliver(self):
        r = analyse(CLIPS, 5)  # into c6, with the 19-frame room-tone sliver c5 before it
        self.assertAlmostEqual(r["silenceBefore"], 0.40 + SLIVER, delta=0.03)

    def test_program_edges_only_check_clipping(self):
        mix = place()
        head = review.analyse_cut(mix, RATE, 0.0, incoming=(0.0, CLIPS[0][5]), edge="head")
        self.assertEqual(head["flags"], [])  # 0.3 s before the first word: no TIGHT_IN at the head
        self.assertIsNone(head["silenceBefore"])
        clips = timeline([("quiet.wav", 0.55, 2.0)])
        tail = review.analyse_cut(place(clips), RATE, clips[0][5], outgoing=(0.0, clips[0][5]), edge="tail")
        self.assertEqual(tail["flags"], ["CLIPPED"])

    def test_no_word_nearby_means_not_tight(self):
        clips = timeline([("quiet.wav", 2.45, 2.95), ("quiet.wav", 2.45, 2.95)])
        r = analyse(clips, 1)
        self.assertEqual(r["flags"], [])

    def test_stdlib_and_numpy_paths_agree(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed")
        saved = list(review._NUMPY)
        try:
            results = []
            for forced in ([None], []):
                review._NUMPY[:] = forced
                results.append([analyse(CLIPS, k) for k in range(1, len(CLIPS))])
        finally:
            review._NUMPY[:] = saved
        for a, b in zip(*results):
            self.assertEqual(a["flags"], b["flags"])
            self.assertEqual(a["silenceBefore"], b["silenceBefore"])
            self.assertAlmostEqual(a["floorBefore"] or 0, b["floorBefore"] or 0, delta=0.11)


class CutPointsTest(unittest.TestCase):
    def test_cuts_gaps_through_edits_and_disabled_clips(self):
        clips = [
            {"name": "a", "start": 0.0, "end": 2.0, "inPoint": 1.0, "outPoint": 3.0, "mediaPath": "/fake/x.wav"},
            {"name": "b", "start": 2.0, "end": 3.0, "inPoint": 3.0, "outPoint": 4.0, "mediaPath": "/fake/x.wav"},
            {"name": "c", "start": 3.0, "end": 4.0, "inPoint": 9.0, "outPoint": 10.0, "mediaPath": "/fake/x.wav"},
            {"name": "off", "start": 4.0, "end": 4.5, "inPoint": 0.0, "outPoint": 0.5, "mediaPath": "/fake/y.wav",
             "disabled": True},
            {"name": "d", "start": 5.0, "end": 6.0, "inPoint": 0.0, "outPoint": 1.0, "mediaPath": "/fake/y.wav"},
        ]
        points, through = review.cut_points(clips, FPS)
        self.assertEqual(through, 1)  # a -> b continues the same source
        self.assertEqual([(p["at"], p["edge"]) for p in points],
                         [(0.0, "head"), (3.0, None), (4.0, "gap-out"), (5.0, "gap-in"), (6.0, "tail")])
        self.assertEqual(points[1]["outgoing"]["name"], "b")
        self.assertAlmostEqual(points[2]["gap"], 1.0)


# ---------------------------------------------------------------- lint

def clip(name, start, end, src_in=None, media="/fake/a.mov", **extra):
    src_in = start + 100.0 if src_in is None else src_in
    d = {"name": name, "start": start, "end": end, "inPoint": src_in, "outPoint": src_in + (end - start),
         "speed": 1, "disabled": False, "mediaPath": media}
    d.update(extra)
    return d


def doc(video=None, audio=None, upper=None) -> dict:
    v = [{"index": 0, "label": "V1", "items": video or []}]
    if upper is not None:
        v.append({"index": 1, "label": "V2", "items": upper})
    return {"sequence": "S", "video": v, "audio": [{"index": 0, "label": "A1", "items": audio or []}]}


def codes(result, code=None, track=None):
    return [i for i in result["issues"]
            if (code is None or i["code"] == code) and (track is None or i["track"] == track)]


class LintTest(unittest.TestCase):
    def test_the_nineteen_frame_sliver(self):
        a, s, b = 10.0, 10.0 + SLIVER, 20.0
        rows = [clip("take2", 0.0, a, 0.0), clip("old take", a, s, 50.0), clip("take3", s, b, 70.0)]
        r = review.lint_timeline(doc(rows, [dict(x) for x in rows]), fps=FPS)
        for track in ("V1", "A1"):
            (sl,) = codes(r, "SLIVER", track)
            self.assertEqual((sl["frames"], sl["severity"], sl["clips"]), (19, "error", ["old take"]))
            self.assertAlmostEqual(sl["at"], 10.0)
            (close,) = codes(r, "CUTS_TOO_CLOSE", track)
            self.assertEqual(close["between"], "SLIVER")
        self.assertEqual(set(r["counts"]), {"SLIVER", "CUTS_TOO_CLOSE"})

    def test_a_long_sliver_threshold_catches_25_fps_slivers(self):
        rows = [clip("a", 0.0, 10.0, 0.0), clip("b", 10.0, 10.76, 40.0), clip("c", 10.76, 20.0, 60.0)]
        self.assertEqual(codes(review.lint_timeline(doc(rows), fps=25.0), "SLIVER"), [])
        self.assertEqual(len(codes(review.lint_timeline(doc(rows), fps=25.0, sliver=1.0), "SLIVER")), 1)

    def test_through_edits_are_not_cuts(self):
        rows = [clip("a", 0.0, 10.0, 0.0), clip("a2", 10.0, 10.2, 10.0), clip("a3", 10.2, 20.0, 10.2)]
        r = review.lint_timeline(doc(rows), fps=FPS)
        self.assertEqual(r["issues"], [])
        # several short pieces joined by through edits are still one sliver
        rows = [clip("a", 0.0, 10.0, 0.0), clip("x1", 10.0, 10.1, 50.0), clip("x2", 10.1, 10.3, 50.1),
                clip("b", 10.3, 20.0, 80.0)]
        (sl,) = codes(review.lint_timeline(doc(rows), fps=FPS), "SLIVER")
        self.assertEqual(sl["clips"], ["x1", "x2"])

    def test_gaps(self):
        base = [clip("a", 0.0, 10.0, 0.0), clip("b", 10.0 + 2 * FRAME, 20.0, 30.0), clip("c", 25.0, 30.0, 60.0)]
        upper = [clip("g1", 1.0, 3.0), clip("g2", 3.0 + 3 * FRAME, 5.0), clip("g3", 12.0, 14.0)]
        r = review.lint_timeline(doc(base, upper=upper), fps=FPS)
        flash, hole = codes(r, "GAP", "V1")
        self.assertEqual((flash["frames"], flash["flash"], flash["severity"]), (2, True, "error"))
        self.assertEqual((hole["frames"], hole["flash"], hole["severity"]), (300, False, "warning"))
        (g,) = codes(r, "GAP", "V2")  # the 7 s hole on V2 is normal for an upper track
        self.assertEqual((g["frames"], g["flash"]), (3, True))
        self.assertEqual({i["between"] for i in codes(r, "CUTS_TOO_CLOSE")}, {"GAP"})

    def test_a_dialogue_pause_under_music_is_a_warning_not_dead_air(self):
        dialogue = [clip("a", 0.0, 10.0, 0.0), clip("b", 10.0 + 17 * FRAME, 20.0, 30.0)]  # a trimmed '-ly'
        for music, severity in (([clip("m", 0.0, 20.0, 0.0)], "warning"), ([], "error"),
                                ([clip("m", 0.0, 20.0, 0.0, disabled=True)], "error")):
            d = doc(audio=dialogue)
            d["audio"].append({"index": 1, "label": "A2", "items": music})
            (g,) = codes(review.lint_timeline(d, fps=FPS), "GAP", "A1")
            self.assertEqual((g["severity"], g["covered"]), (severity, severity == "warning"), music)
            self.assertIn("pause in the dialogue" if music and not music[0]["disabled"] else "dead air", g["detail"])

    def test_min_gap_frames_ignores_rounding_gaps(self):
        rows = [clip("a", 0.0, 10.0, 0.0), clip("b", 10.0 + FRAME, 20.0, 30.0)]
        self.assertEqual(len(codes(review.lint_timeline(doc(rows), fps=FPS), "GAP")), 1)
        self.assertEqual(review.lint_timeline(doc(rows), fps=FPS, min_gap_frames=2)["issues"], [])
        rows[1]["start"] = 10.0 + 0.3 * FRAME  # under half a frame: tick rounding, never reported
        self.assertEqual(review.lint_timeline(doc(rows), fps=FPS)["issues"], [])

    def test_overlap_late_start_early_end_and_past_end(self):
        rows = [clip("a", 0.5, 10.0, 0.0), clip("b", 9.8, 20.0, 30.0)]
        r = review.lint_timeline(doc(rows), fps=FPS, duration=19.0)
        (ov,) = codes(r, "OVERLAP")
        self.assertAlmostEqual(ov["duration"], 0.2)
        (late,) = codes(r, "GAP")
        self.assertEqual(late["at"], 0.0)
        self.assertIn("head", late["detail"])
        (past,) = codes(r, "PAST_END")
        self.assertEqual(past["clips"], ["b"])
        r = review.lint_timeline(doc([clip("a", 0.0, 10.0)]), fps=FPS, duration=12.0)
        self.assertIn("tail", codes(r, "GAP")[0]["detail"])

    def test_av_cut_mismatch_only_for_near_misses(self):
        v = [clip("a", 0.0, 10.0, 0.0), clip("b", 10.0, 20.0, 50.0)]
        a = [clip("a", 0.0, 10.0 + 3 * FRAME, 0.0), clip("b", 10.0 + 3 * FRAME, 20.0, 50.0 + 3 * FRAME)]
        (m,) = codes(review.lint_timeline(doc(v, a), fps=FPS), "AV_CUT_MISMATCH")
        self.assertEqual((m["track"], m["frames"]), ("V1/A1", 3))
        self.assertAlmostEqual(m["video"], 10.0)
        a = [clip("a", 0.0, 11.0, 0.0), clip("b", 11.0, 20.0, 51.0)]  # a 1 s J-cut: deliberate, not reported
        self.assertEqual(codes(review.lint_timeline(doc(v, a), fps=FPS), "AV_CUT_MISMATCH"), [])

    def test_edges_drifted_off_the_frame_grid(self):
        """The +1 us nudge an older panel put on every sequence time: list_items and the half-frame checks show
        nothing, but an end just past a frame boundary shows the clip one more frame."""
        k = lambda n: n * TICK_FRAME / TPS  # noqa: E731  (frame n exactly, as Premiere reports it)
        v = [clip("a", 0.0, k(600) + 3e-6, 0.0), clip("b", k(600) + 3e-6, k(1200), 50.0),
             clip("c", k(1200) - 2e-6, k(1800), 90.0)]
        a = [clip("a", 0.0, k(600), 0.0), clip("b", k(600), k(1800), 50.0)]
        r = review.lint_timeline(doc(v, a), fps=FPS)
        (off,) = codes(r, "OFF_GRID")
        self.assertEqual((off["track"], off["edges"], off["late"], off["maxOffsetUs"], off["severity"]),
                         ("V1", 3, 2, 3.0, "warning"))
        self.assertAlmostEqual(off["at"], k(600), places=3)
        self.assertEqual([(e["clip"], e["edge"], e["offsetUs"]) for e in off["examples"]],
                         [("a", "end", 3.0), ("b", "start", 3.0), ("c", "start", -2.0)])
        self.assertEqual(r["counts"], {"OFF_GRID": 1})  # a warning: nothing else, and not an error
        # exact frames, float noise and sub-frame times placed on purpose (20.0 s: 0.2 frame early) are not drift
        rows = [clip("a", 0.0, k(600) + 1e-12, 0.0), clip("b", k(600), 20.0, 50.0), clip("c", 20.0, k(1800), 90.0)]
        self.assertEqual(codes(review.lint_timeline(doc(rows), fps=FPS), "OFF_GRID"), [])

    def test_bad_arguments(self):
        with self.assertRaises(BridgeError) as cm:
            review.lint_timeline(doc(), fps=0)
        self.assertEqual(cm.exception.code, "BAD_PARAMS")
        self.assertEqual(review.lint_timeline({}, fps=FPS)["issues"], [])

    def test_lint_sequence_reads_the_active_sequence(self):
        rows = [clip("a", 0.0, 10.0, 0.0), clip("b", 10.0, 10.2, 40.0), clip("c", 10.2, 20.0, 60.0)]
        rpc = stub_rpc({"get_sequence": {"name": "Cutdown", "timebase": TIMEBASE, "fps": 59.94,
                                         "durationSeconds": 20.0},
                        "list_items": doc(rows)})
        r = review.lint_sequence(rpc)
        self.assertEqual(r["sequence"], "Cutdown")
        self.assertEqual(r["counts"], {"SLIVER": 1, "CUTS_TOO_CLOSE": 1})


# ---------------------------------------------------------------- words

ADOBE = {"language": "en-us", "speakers": [], "segments": [{"start": 0.0, "duration": 9.0, "words": [
    {"text": "One", "start": 1.0, "duration": 0.5, "type": "word", "confidence": 0.9},
    {"text": ",", "start": 1.5, "duration": 0.0, "type": "punctuation", "confidence": 1.0},
    {"text": "two", "start": 1.7, "duration": 0.5, "type": "word", "confidence": 0.9},
    {"text": "three", "start": 4.0, "duration": 0.6, "type": "word", "confidence": 0.95},
    {"text": "four", "start": 4.7, "duration": 0.4, "type": "word", "confidence": 0.9},
    {"text": "five", "start": 5.2, "duration": 0.3, "type": "word", "confidence": 0.9}]}]}


class PadCutPointsTest(unittest.TestCase):
    def test_wide_pauses_get_the_full_rolls(self):
        # "One" … "two" (punctuation is not counted): 0.25 s before "One", 0.35 s into the 1.8 s pause after "two"
        self.assertEqual(review.pad_cut_points(ADOBE, [(0, 1)]), [(0.75, 2.55)])
        # "two" alone: only 0.2 s after "One" -> its head gets 40 % of that pause
        self.assertEqual(review.pad_cut_points(ADOBE, [(1, 1)]), [(1.62, 2.55)])

    def test_short_pauses_are_shared_and_never_reach_a_word(self):
        # keep "three" alone: 0.1 s pause to "four" -> the tail gets 60 % of it
        ((a, b),) = review.pad_cut_points(ADOBE, [(2, 2)])
        self.assertEqual((a, b), (3.75, 4.66))
        # "four" and "five" kept separately meet in the middle of their 0.1 s pause, no overlap
        (_, b1), (a2, _) = review.pad_cut_points(ADOBE, [(3, 3), (4, 4)])
        self.assertEqual(b1, a2)
        self.assertEqual(b1, 5.16)
        ((a, _),) = review.pad_cut_points(ADOBE, [(3, 3)], max_gap_share=0.6)
        self.assertEqual(a, 4.66)  # head of "four": 40 % of the 0.1 s pause after "three"

    def test_media_edges(self):
        words = [(0.1, 0.5), (0.9, 1.5)]
        self.assertEqual(review.pad_cut_points(words, [(0, 1)]), [(0.0, 1.85)])
        self.assertEqual(review.pad_cut_points(words, [(0, 1)], media_end=1.6), [(0.0, 1.6)])

    def test_word_lists_in_several_shapes(self):
        dicts = [{"start": 1.0, "end": 1.5}, {"start": 3.0, "duration": 0.5}]
        self.assertEqual(review.pad_cut_points(dicts, [(1, 1)], pre_roll=0.5, post_roll=0.5), [(2.5, 4.0)])
        rows = review.word_timings(ADOBE)
        self.assertEqual([w["text"] for w in rows], ["One", "two", "three", "four", "five"])
        self.assertEqual(rows[2]["index"], 2)

    def test_bad_keep_ranges(self):
        for keep in ([(3, 1)], [(0, 9)], [("x", 1)], [(-1, 0)]):
            with self.assertRaises(BridgeError) as cm:
                review.pad_cut_points(ADOBE, keep)
            self.assertEqual(cm.exception.code, "BAD_PARAMS")
        with self.assertRaises(BridgeError):
            review.pad_cut_points(ADOBE, [(0, 0)], max_gap_share=1.5)


class WordChecksTest(unittest.TestCase):
    def test_cut_inside_a_word_and_shaky_words(self):
        words = review.word_timings([{"text": "hello", "start": 1.0, "end": 1.5, "confidence": 0.9},
                                     {"text": "wor-", "start": 2.0, "end": 2.6, "confidence": 0.3},
                                     {"text": "next", "start": 3.0, "end": 3.4, "confidence": 0.9}])
        w = review.word_checks(words, 2.3)
        self.assertEqual(w["flags"], ["CUT_IN_WORD", "LOW_CONF_WORD"])
        self.assertEqual(w["inside"]["text"], "wor-")
        w = review.word_checks(words, 1.7)
        self.assertEqual(w["flags"], [])
        self.assertEqual((w["before"]["text"], w["before"]["gap"]), ("hello", 0.2))
        self.assertEqual((w["after"]["text"], w["after"]["gap"]), ("wor-", 0.3))


# ---------------------------------------------------------------- ffmpeg: shadow mix, review_cuts, render

def seq_reply(end):
    return {"name": "Cutdown", "playerPositionSeconds": 0.0, "width": 1080, "height": 1920, "timebase": TIMEBASE,
            "fps": 59.94, "durationSeconds": end, "videoTracks": [], "audioTracks": [], "captionTrackCount": 0}


@needs_ffmpeg
class ShadowMixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tmpdir()
        write_sources(cls.dir)

    def test_places_every_clip_sample_accurately(self):
        out = review.shadow_mix(items_doc(self.dir), out_wav=self.dir / "mix.wav")
        self.assertEqual(out["notes"], [])
        self.assertTrue(all(r["mixed"] for r in out["clips"]))
        samples, rate = review.read_wav(out["wav"])
        self.assertEqual(rate, RATE)
        self.assertEqual(samples, place())

    def test_skips_what_it_cannot_rebuild_and_says_so(self):
        # a disabled clip is silent in Premiere too: it needs no media (NoMediaTest has the clips that do)
        d = items_doc(self.dir, c2={"disabled": True, "mediaPath": ""}, c3={"speed": 2.0})
        out = review.shadow_mix(d, out_wav=self.dir / "mix2.wav")
        skipped = {r["name"]: r.get("skipped") for r in out["clips"] if not r["mixed"]}
        self.assertEqual(skipped, {"c2": "disabled", "c3": "speed"})
        self.assertEqual(len(out["notes"]), 2)
        samples, _ = review.read_wav(out["wav"])
        s, e = int(round(CUT["c3"] * RATE)), int(round(CUT["c4"] * RATE))
        self.assertEqual(set(samples[s:e]), {0})
        self.assertEqual(samples[int(round(CUT["c4"] * RATE)):], place()[int(round(CUT["c4"] * RATE)):])

    def test_sums_tracks_and_refuses_unknown_ones(self):
        d = items_doc(self.dir)
        d["audio"][1]["items"] = [dict(d["audio"][0]["items"][0], name="dup")]
        out = review.shadow_mix(d, tracks=["A1", "A2"], out_wav=self.dir / "mix3.wav")
        samples, _ = review.read_wav(out["wav"])
        ref = place()
        n = int(round(CLIPS[0][5] * RATE))
        self.assertEqual(samples[1000], max(-32768, min(32767, 2 * ref[1000])))
        self.assertEqual(samples[n + 10], ref[n + 10])
        with self.assertRaises(BridgeError) as cm:
            review.shadow_mix(d, tracks=["A3"], out_wav=self.dir / "x.wav")
        self.assertEqual(cm.exception.code, "NOT_FOUND")
        with self.assertRaises(BridgeError) as cm:
            review.shadow_mix(d, tracks=["V1"], out_wav=self.dir / "x.wav")
        self.assertEqual(cm.exception.code, "BAD_PARAMS")


@needs_ffmpeg
class ReviewCutsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tmpdir()
        write_sources(cls.dir)
        cls.end = CLIPS[-1][5]
        cls.preset = cls.dir / "Waveform Audio 48kHz 16-bit.epr"
        cls.preset.write_bytes(b"<fake preset/>\n")

    def rpc(self, **handlers):
        base = {"get_sequence": seq_reply(self.end), "list_items": items_doc(self.dir)}
        base.update(handlers)
        return stub_rpc(base)

    def check_cut_flags(self, report):
        self.assertEqual(flags_at(report, CUT["c2"]), [])
        self.assertEqual(flags_at(report, CUT["c3"]), ["TIGHT_OUT", "TIGHT_IN"])
        self.assertIn("CLIPPED", flags_at(report, CUT["c4"]))
        self.assertEqual(flags_at(report, CUT["c5"]), [])
        self.assertIn("CLICK", flags_at(report, CUT["c6"]))
        self.assertIn("LEVEL_JUMP", flags_at(report, CUT["c7"]))

    def test_the_shadow_report(self):
        rpc = self.rpc()
        report = review.review_cuts(rpc, out_dir=self.dir / "shadow")
        self.assertEqual([a for a, _ in rpc.calls], ["get_sequence", "list_items"])
        self.assertEqual((report["sequence"], report["source"], report["tracks"]), ("Cutdown", "shadow", ["A1"]))
        self.check_cut_flags(report)
        self.assertEqual(flags_at(report, CUT["c6"]), ["CLICK"])
        self.assertEqual(flags_at(report, CUT["c7"]), ["LEVEL_JUMP"])
        self.assertEqual([c.get("edge") for c in report["cuts"]][::len(report["cuts"]) - 1], ["head", "tail"])
        tight = next(c for c in report["cuts"] if abs(c["at"] - CUT["c3"]) < 1e-3)
        self.assertEqual((tight["outgoing"]["name"], tight["incoming"]["name"]), ("c2", "c3"))
        self.assertAlmostEqual(tight["silenceBefore"], 0.05, delta=0.015)
        lint = {(i["code"], i["track"]) for i in report["lint"]}
        self.assertIn(("SLIVER", "A1"), lint)
        self.assertIn(("SLIVER", "V1"), lint)
        self.assertEqual(report["summary"]["flags"]["TIGHT_OUT"], 2)
        self.assertFalse(report["clean"])
        self.assertTrue(Path(report["wav"]).is_file())
        notes = " ".join(report["notes"])
        self.assertIn("volume", notes)
        self.assertIn("NOT applied", notes)
        self.assertIn("music", notes.lower())
        self.assertIn("No transcript", notes)
        self.assertNotIn("loudness", report)
        json.dumps(report, allow_nan=False)  # one strict JSON object for the CLI

    def test_words_add_word_level_checks(self):
        words = [{"text": "chopped", "start": CUT["c4"] - 0.3, "end": CUT["c4"] + 0.25, "confidence": 0.3},
                 {"text": "fine", "start": CUT["c2"] + 0.3, "end": CUT["c2"] + 0.9, "confidence": 0.95}]
        report = review.review_cuts(self.rpc(), out_dir=self.dir / "words", words=words)
        f = flags_at(report, CUT["c4"])
        self.assertIn("CUT_IN_WORD", f)
        self.assertIn("LOW_CONF_WORD", f)
        clean = next(c for c in report["cuts"] if abs(c["at"] - CUT["c2"]) < 1e-3)
        self.assertEqual(clean["words"]["after"]["text"], "fine")
        self.assertIn("sequence seconds", " ".join(report["notes"]))

    def test_the_render_path_and_loudness(self):
        mix16 = self.dir / "premiere_mix16.wav"
        review.write_wav(mix16, place(), RATE)
        seen = {}

        def export(params):  # Premiere's render: 48 kHz stereo of the same mix
            seen.update(params)
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(mix16), "-ac", "2", "-ar", "48000",
                            params["outputPath"]], check=True, capture_output=True)
            return {"exported": True, "outputPath": params["outputPath"], "preset": "Waveform Audio 48kHz 16-bit"}

        rpc = self.rpc(export_sequence=export)
        report = review.review_cuts(rpc, out_dir=self.dir / "render", render=True, preset=self.preset)
        self.assertEqual(seen["presetPath"], str(self.preset))
        self.assertEqual(report["source"], "render")
        self.assertTrue(Path(report["rendered"]).is_file())
        self.check_cut_flags(report)
        loud = report["loudness"]
        self.assertTrue(-60 < loud["integratedLufs"] < 0, loud)
        self.assertIsNotNone(loud["truePeakDb"])
        self.assertIn("Premiere's render", " ".join(report["notes"]))
        json.dumps(report, allow_nan=False)

    def test_a_render_shorter_than_the_timeline_is_reported_not_a_crash(self):
        mix16 = self.dir / "short_mix16.wav"
        review.write_wav(mix16, place(), RATE)

        def export(params):  # only the first second comes out
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(mix16), "-t", "1", "-ac", "2", "-ar", "48000",
                            params["outputPath"]], check=True, capture_output=True)
            return {"exported": True, "outputPath": params["outputPath"], "preset": "Waveform Audio 48kHz 16-bit"}

        report = review.review_cuts(self.rpc(export_sequence=export), out_dir=self.dir / "short", render=True,
                                    preset=self.preset)
        self.assertIn("were not heard", " ".join(report["notes"]))
        late = [c for c in report["cuts"] if c["at"] > 1.1]
        self.assertTrue(late and all(c.get("unheard") for c in late), late)
        self.assertFalse(any(c.get("unheard") for c in report["cuts"] if c["at"] < 0.9))
        json.dumps(report, allow_nan=False)

    def test_export_that_writes_nothing_is_export_failed(self):
        rpc = self.rpc(export_sequence={"exported": True, "outputPath": "/fake/none.wav", "preset": "x"})
        with self.assertRaises(BridgeError) as cm:
            review.review_cuts(rpc, out_dir=self.dir / "none", render=True, preset=self.preset)
        self.assertEqual(cm.exception.code, "EXPORT_FAILED")

    def test_render_needs_a_preset_before_any_panel_call(self):
        rpc = stub_rpc({})
        with mock.patch.object(review.media, "find_wav_preset", return_value=None):
            with self.assertRaises(BridgeError) as cm:
                review.review_cuts(rpc, out_dir=self.dir / "nopreset", render=True)
            self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")
            self.assertIn("PPRO_WAV_PRESET", str(cm.exception))
        with self.assertRaises(BridgeError) as cm:
            review.review_cuts(rpc, out_dir=self.dir / "nopreset", render=True, preset=self.dir / "nope.epr")
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")
        self.assertEqual(rpc.calls, [])
        with mock.patch.object(review.media, "find_wav_preset", return_value=str(self.preset)):
            self.assertEqual(review.resolve_preset(), str(self.preset))

    def test_bad_tracks(self):
        with self.assertRaises(BridgeError) as cm:
            review.review_cuts(stub_rpc({}), out_dir=self.dir / "bad", tracks=["V1"])
        self.assertEqual(cm.exception.code, "BAD_PARAMS")
        with self.assertRaises(BridgeError) as cm:
            review.review_cuts(self.rpc(), out_dir=self.dir / "bad", tracks=["A4"])
        self.assertEqual(cm.exception.code, "NOT_FOUND")


class NoMediaTest(unittest.TestCase):
    """Clips the shadow mix cannot read — Enhance Speech replaces a clip with a generated item that list_items
    reports with mediaPath "" — refuse the mix before anything is decoded or written, instead of mixing silence.
    The refusal comes before ffmpeg runs, so `which` is stubbed and these run without ffmpeg."""

    ENHANCED = "（已增强）"  # what Premiere (Chinese UI) appends to the name of an Enhance Speech item

    @classmethod
    def setUpClass(cls):
        cls.dir = tmpdir()
        write_sources(cls.dir)
        cls.end = CLIPS[-1][5]

    def setUp(self):
        self.which = mock.patch.object(review.shutil, "which", return_value="ffmpeg")
        self.which.start()
        self.addCleanup(self.which.stop)

    def enhanced(self, *names):
        return {n: {"name": n + self.ENHANCED, "mediaPath": ""} for n in names}

    def rpc(self, doc):
        return stub_rpc({"get_sequence": seq_reply(self.end), "list_items": doc})

    def assert_no_media(self, cm, expect):
        e = cm.exception
        self.assertEqual((e.code, e.outcome), ("NO_MEDIA", "not_committed"))
        self.assertIn("--render", str(e))
        self.assertIn("--render", e.hint)
        self.assertEqual([(c["name"], c["reason"]) for c in e.data["clips"]], expect)
        self.assertEqual(e.data["tracks"], ["A1"])
        json.dumps(e.to_reply(), allow_nan=False)

    def test_all_clips_without_media_refuse_and_write_nothing(self):
        names = [c[0] for c in CLIPS]
        doc = items_doc(self.dir, **self.enhanced(*names))
        out = self.dir / "all.wav"
        with mock.patch.object(review.subprocess, "run", side_effect=AssertionError("ffmpeg ran")):
            with self.assertRaises(BridgeError) as cm:
                review.shadow_mix(doc, out_wav=out)
            self.assert_no_media(cm, [(n + self.ENHANCED, "no media path") for n in names])
            self.assertEqual(cm.exception.data["enabled"], len(names))
            self.assertIn(f"{len(names)} of {len(names)}", str(cm.exception))
            self.assertIn("4 more in data.clips", str(cm.exception))  # the message names the first three
            self.assertFalse(out.exists())

            with self.assertRaises(BridgeError) as cm:  # export_audio (shadow) = the same refusal
                review.export_audio(self.rpc(doc), output=out)
            self.assertEqual(cm.exception.code, "NO_MEDIA")
            self.assertFalse(out.exists())

            rpc = self.rpc(doc)  # review_cuts refuses too: it does not switch to the render by itself
            with self.assertRaises(BridgeError) as cm:
                review.review_cuts(rpc, out_dir=self.dir / "all")
            self.assert_no_media(cm, [(n + self.ENHANCED, "no media path") for n in names])
            self.assertEqual([a for a, _ in rpc.calls], ["get_sequence", "list_items"])
            self.assertFalse((self.dir / "all" / "review_shadow.wav").exists())

    def test_some_clips_without_media_refuse_and_are_named(self):
        extra = self.enhanced("c4")
        extra["c6"] = {"mediaPath": str(self.dir / "moved.wav")}  # offline: the path is there, the file is not
        extra["c2"] = {"disabled": True, "mediaPath": ""}         # disabled: silent anyway, not listed
        doc = items_doc(self.dir, **extra)
        with self.assertRaises(BridgeError) as cm:
            review.shadow_mix(doc, out_wav=self.dir / "some.wav")
        self.assert_no_media(cm, [("c4" + self.ENHANCED, "no media path"), ("c6", "file not found")])
        e = cm.exception
        self.assertEqual(e.data["enabled"], len(CLIPS) - 1)
        self.assertEqual(e.data["clips"][0]["start"], round(CUT["c4"], 3))
        self.assertEqual(e.data["clips"][1]["mediaPath"], str(self.dir / "moved.wav"))
        self.assertIn(f'A1 "c4{self.ENHANCED}" at {CUT["c4"]:.3f} s (no media path)', str(e))
        self.assertIn('A1 "c6"', str(e))
        self.assertIn(f"2 of {len(CLIPS) - 1}", str(e))
        self.assertNotIn("more in data.clips", str(e))
        self.assertFalse((self.dir / "some.wav").exists())

    def test_only_the_requested_tracks_are_checked(self):
        doc = items_doc(self.dir)
        doc["audio"][1]["items"] = [dict(doc["audio"][0]["items"][0], name="music" + self.ENHANCED, mediaPath="")]
        self.assertEqual(review._no_media(["A1"], dict(review._track_clips(doc, "audio"))), [])
        with self.assertRaises(BridgeError) as cm:
            review.shadow_mix(doc, tracks=["A1", "A2"], out_wav=self.dir / "a2.wav")
        self.assertEqual([(c["track"], c["name"]) for c in cm.exception.data["clips"]],
                         [("A2", "music" + self.ENHANCED)])

    def test_the_cli_reply(self):
        from ppro_bridge import cli
        doc = items_doc(self.dir, **self.enhanced("c1"))
        reply = cli.run("export_audio", {"output": str(self.dir / "cli.wav")}, self.rpc(doc))
        self.assertFalse(reply["ok"])
        self.assertEqual((reply["code"], reply["outcome"]), ("NO_MEDIA", "not_committed"))
        self.assertIn("--render", reply["hint"])
        self.assertEqual(reply["data"]["clips"][0]["name"], "c1" + self.ENHANCED)
        json.dumps(reply, allow_nan=False)

    @needs_ffmpeg
    def test_every_clip_readable_mixes_as_before(self):
        self.which.stop()  # the real ffmpeg for this one
        out = review.shadow_mix(items_doc(self.dir), out_wav=self.dir / "none.wav")
        self.assertTrue(all(r["mixed"] for r in out["clips"]))
        self.assertEqual(out["notes"], [])
        self.assertEqual(review.read_wav(out["wav"])[0], place())


class MissingFfmpegTest(unittest.TestCase):
    def test_missing_dependency_before_any_panel_call(self):
        rpc = stub_rpc({})
        with mock.patch.object(review.shutil, "which", return_value=None):
            with self.assertRaises(BridgeError) as cm:
                review.review_cuts(rpc, out_dir=tmpdir())
            self.assertEqual((cm.exception.code, cm.exception.outcome), ("MISSING_DEPENDENCY", "not_committed"))
            with self.assertRaises(BridgeError) as cm:
                review.shadow_mix({"audio": []}, out_wav=tmpdir() / "x.wav")
            self.assertEqual(cm.exception.code, "MISSING_DEPENDENCY")
        self.assertEqual(rpc.calls, [])


if __name__ == "__main__":
    unittest.main()
