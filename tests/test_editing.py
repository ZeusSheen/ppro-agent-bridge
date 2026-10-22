"""Razor-free editing (ppro_bridge.editing): split_clip, cut_range, conform_captions — in-process against the fake
panel (helpers.fake_rpc), so every request also passes the actions.json param check.

clone_clip / trim_clip: when the fake panel has no handler for them yet, this file installs a small stub on top of
the fake's STATE that implements the semantics measured live on Premiere 27.1 b17 (clone = exact copy offset later,
in/out kept, linked items not cloned; start/inPoint = head trim, end/outPoint = tail trim, moveTo = absolute new
start, refused with other fields). As soon as broker/fake_panel.py registers its own handlers, every test here runs
against those instead (STUBBED records which ones came from this file) — except test_wrong_semantics_are_caught,
which always installs a deliberately broken trim_clip.
All data is synthetic; nothing connects anywhere.
"""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import copy
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import BridgeError

from ppro_bridge import editing

FPS = 60000 / 1001
F = 1 / FPS
LEVEL_0DB = 0.17782793939113617  # Volume > Level at 0 dB (broker/fake_panel.py)
STUBBED = set()  # clone_clip / trim_clip handlers that came from this file


def S(t):
    """Seconds snapped to the 59.94 frame grid (the fake's sequence)."""
    return round(t * FPS) / FPS


# ---------------------------------------------------------------- stub primitives (until the fake has them)

def _under(fake, kind, idx, at):
    hits = [it for it in fake.track(kind, idx) if fake.overlaps(it, at, at)]
    if not hits:
        raise fake.Coded("NOT_FOUND", f"No clip on {fake.label_of(kind, idx)} at {at}s.")
    if len(hits) > 1:
        raise fake.Coded("AMBIGUOUS", f"Expected one clip on {fake.label_of(kind, idx)} at {at}s, found {len(hits)}.")
    return hits[0]


def _stub_clone(fake):
    def clone_clip(p):
        fake.require_sequence()
        kind, idx, off = p["kind"], p["track"], p["offsetSeconds"]
        src = _under(fake, kind, idx, p["atSeconds"])
        dest = idx + (p.get("trackOffset") or 0)
        fake.track(kind, dest)
        before = fake.describe(src)
        label = p.get("undoLabel") or f"Agent: clone {src['name']}"
        fake.transact(label)
        dup = copy.deepcopy(src)  # effects, volume, scaling: the whole clip
        dup["start"], dup["end"] = src["start"] + off, src["end"] + off
        fake.overwrite(kind, dest, dup["start"], dup["end"], dup)
        return {"cloned": True, "kind": kind, "track": dest, "source": before, "start": before["start"] + off,
                "end": before["end"] + off, "undoLabel": label}
    return clone_clip


def _stub_trim(fake, broken=False):
    def trim_clip(p):
        fake.require_sequence()
        kind, idx = p["kind"], p["track"]
        fields = [k for k in ("start", "end", "inPoint", "outPoint", "moveTo") if fake.is_num(p.get(k))]
        if not fields:
            raise fake.Coded("BAD_PARAMS", "trim_clip needs start, end, inPoint, outPoint or moveTo.")
        if "moveTo" in fields and len(fields) > 1:
            raise fake.Coded("BAD_PARAMS", "moveTo cannot be combined with other fields.")
        it = _under(fake, kind, idx, p["atSeconds"])
        before = fake.describe(it)
        label = p.get("undoLabel") or f"Agent: trim {it['name']}"
        fake.transact(label)
        for k in fields:
            v = p[k]
            if k == "start":  # head trim: the content stays anchored (unless broken: the in point stays put)
                if not broken:
                    it["inPoint"] += v - it["start"]
                it["start"] = v
            elif k == "inPoint":  # measured: ALSO a head trim
                it["start"] += v - it["inPoint"]
                it["inPoint"] = v
            elif k == "end":
                it["outPoint"] += v - it["end"]
                it["end"] = v
            elif k == "outPoint":
                it["end"] += v - it["outPoint"]
                it["outPoint"] = v
            else:
                d = v - it["start"]
                it["start"], it["end"] = it["start"] + d, it["end"] + d
        fake.track(kind, idx).sort(key=lambda x: x["start"])
        return {"trimmed": True, "kind": kind, "track": idx, "fields": fields, "before": before,
                "after": fake.describe(it), "undoLabel": label}
    return trim_clip


def make_rpc():
    rpc, fake, calls = helpers.fake_rpc()
    for name, make in (("clone_clip", _stub_clone), ("trim_clip", _stub_trim)):
        if name not in fake.HANDLERS:
            fake.HANDLERS[name] = make(fake)
            STUBBED.add(name)
    return rpc, fake, calls


# ---------------------------------------------------------------- timelines

def clip(name, start, end, in_point, **extra):
    s, e = S(start), S(end)
    return dict(name=name, start=s, end=e, inPoint=in_point, outPoint=in_point + (e - s), **extra)


def gap_timeline():
    """Dialogue in three clips on V1/A1, B-roll with gaps on V2, music under everything on A2, SFX with gaps on A3."""
    video = [[clip("interview.mp4", 0, 10, 0.0, tag="A"), clip("interview.mp4", 10, 25, 20.0, tag="B"),
              clip("interview.mp4", 25, 40, 40.0, tag="C")],
             [clip("broll_city.mp4", 5, 8, 2.0, tag="b1"), clip("broll_city.mp4", 16, 18, 1.0, tag="b2"),
              clip("broll_city.mp4", 30, 33, 4.0, tag="b3")],
             [], []]
    audio = [[clip("interview.mp4", 0, 10, 0.0, tag="a"), clip("interview.mp4", 10, 25, 20.0, tag="b"),
              clip("interview.mp4", 25, 40, 40.0, tag="c")],
             [clip("music.wav", 0, 38, 0.0, tag="m")],
             [clip("sfx.wav", 13, 16, 0.0, tag="s1"), clip("sfx.wav", 19, 21, 0.5, tag="s2"),
              clip("sfx.wav", 27, 27.5, 0.0, tag="s3")],
             []]
    return video, audio


CAPTION_SRT = """1
00:00:01,000 --> 00:00:04,000
Line one

2
00:00:04,000 --> 00:00:07,500
Line two

3
00:00:07,500 --> 00:00:10,000
Line three

4
00:00:10,000 --> 00:00:13,000
Line four
"""


def piece(start_frame, n_frames, in_point):
    return {"name": "captions.mov", "start": start_frame * F, "end": (start_frame + n_frames) * F,
            "inPoint": in_point, "outPoint": in_point + n_frames * F}


def caption_pieces():
    """A caption movie cut into four pieces on V3 (sequence times on the frame grid)."""
    return [piece(0, 150, 1.0),            # P1 clean: starts at cue 1
            piece(150, 120, 7.5 - 4 * F),  # P2 starts 4 frames before cue 3: line two would flash
            piece(270, 120, 10.0),         # P3 clean: starts at cue 4
            piece(390, 214, 4.0 + F)]      # P4 starts 1 frame into cue 2 (fine), ends 5.2 frames into cue 3


class EditingCase(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        for key in [k for k in os.environ if k.startswith("PPRO_FAKE_")]:
            del os.environ[key]
        self.rpc, self.fake, self.calls = make_rpc()
        self.addCleanup(self.fake.reset)
        self.tmp = Path(tempfile.mkdtemp(prefix="ppro_editing_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, dur in (("music.wav", 120.0), ("sfx.wav", 5.0), ("captions.mov", 60.0)):
            self.fake.STATE["items"].append({"name": name, "bin": "", "mediaPath": f"/fake/{name}",
                                             "hasTranscript": False, "duration": dur})

    def set_timeline(self, video, audio):
        self.fake.STATE["video"] = [sorted(t, key=lambda c: c["start"]) for t in video]
        self.fake.STATE["audio"] = [sorted(t, key=lambda c: c["start"]) for t in audio]

    def track(self, label):
        kind = "video" if label[0] == "V" else "audio"
        return sorted(self.fake.STATE[kind][int(label[1:]) - 1], key=lambda c: c["start"])

    def spans(self, label):
        return [(c["start"], c["end"], c["inPoint"], c["outPoint"]) for c in self.track(label)]

    def assertSpans(self, label, want):
        got = self.spans(label)
        self.assertEqual(len(got), len(want), f"{label}: {got}")
        for g, w in zip(got, want):
            for gv, wv, field in zip(g, w, ("start", "end", "inPoint", "outPoint")):
                self.assertAlmostEqual(gv, wv, delta=F / 2, msg=f"{label} {field}: got {g}, want {w}")

    def writes(self):
        return [(a, p) for a, p in self.calls if a in ("clone_clip", "trim_clip", "shift_items", "insert_clip",
                                                       "set_params")
                or (a == "remove_items" and not p.get("dryRun"))]

    def components(self, v, at):
        tracks = self.rpc("inspect_items", {"videoTracks": [v], "fromSeconds": at, "toSeconds": at})["tracks"]
        return next(c for t in tracks for c in t["clips"] if c["start"] <= at < c["end"])["components"]

    def srt(self, text=CAPTION_SRT, name="captions.srt"):
        path = self.tmp / name
        path.write_bytes(text.encode("utf-8"))
        return path


# ---------------------------------------------------------------- split_clip

class SplitTest(EditingCase):
    def test_split_in_the_middle_keeps_attributes(self):
        self.rpc("set_params", {"videoTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": "AE.ADBE Motion", "params": [{"index": 1, "value": 150.0}]},
            {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "value": 70.0}]},
            {"matchName": "AE.ADBE Gaussian Blur 2", "params": [{"index": 0, "value": 12.0}]}]})
        volume = [{"matchName": "Internal Volume Mono", "displayName": "Volume",
                   "params": [{"name": "Bypass", "value": False, "keyframed": False, "keyframes": []},
                              {"name": "Level", "value": 0.0891251, "keyframed": False, "keyframes": []}]}]
        self.fake.STATE["audio"][0][0]["components"] = copy.deepcopy(volume)  # clip volume -6 dB
        chain = self.components(0, 5.0)

        out = editing.split_clip(self.rpc, kind="video", track=0, at=20.0, label="Split test")
        at = S(20.0)
        self.assertTrue(out["split"])
        self.assertTrue(out["verified"])
        self.assertEqual(out["undoSteps"], 4)
        self.assertAlmostEqual(out["at"], at, places=6)
        self.assertSpans("V1", [(0.0, at, 0.0, at), (at, 60.0, at, 60.0)])
        self.assertEqual(self.components(0, 5.0), chain)
        self.assertEqual(self.components(0, 30.0), chain)  # the second half is the same clip, attributes and all

        out = editing.split_clip(self.rpc, track="A1", at=20.0, label="Split test")
        self.assertTrue(out["split"])
        self.assertSpans("A1", [(0.0, at, 0.0, at), (at, 60.0, at, 60.0)])
        self.assertEqual([c["components"] for c in self.track("A1")], [volume, volume])

        labels = [t for t in self.fake.TRANSACTIONS if t.startswith("Split test: ")]
        self.assertEqual(len(labels), 8)
        self.assertTrue(all(f"[{n}/4]" in labels[n - 1] for n in range(1, 5)), labels)
        self.assertFalse([a for a, _ in self.writes() if a in ("insert_clip", "remove_items")])

    def test_split_at_an_edge_is_a_no_op(self):
        editing.split_clip(self.rpc, kind="video", track=0, at=20.0, label="Split")
        done, writes = list(self.fake.TRANSACTIONS), len(self.writes())
        for at in (S(20.0) + F / 4, 0.0, 60.0):  # the cut just made (within a frame), the clip's start and end
            out = editing.split_clip(self.rpc, kind="video", track=0, at=at, label="Split")
            self.assertFalse(out["split"])
            self.assertTrue(out["noop"])
            self.assertIn("already an edge", out["reason"])
        self.assertEqual(self.fake.TRANSACTIONS, done)
        self.assertEqual(len(self.writes()), writes)

    def test_split_in_a_gap_is_not_found(self):
        with self.assertRaises(BridgeError) as cm:
            editing.split_clip(self.rpc, track="V2", at=5.0, label="Split")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("NOT_FOUND", "not_committed"))

    def test_dry_run_plans_four_steps_and_writes_nothing(self):
        before = copy.deepcopy(self.fake.STATE["video"])
        out = editing.split_clip(self.rpc, kind="video", track=0, at=20.0, label="Split", dry_run=True)
        self.assertTrue(out["dryRun"])
        self.assertFalse(out["split"])
        self.assertEqual(len(out["steps"]), 4)
        self.assertAlmostEqual(out["right"]["start"], S(20.0), places=6)
        self.assertEqual(self.fake.STATE["video"], before)
        self.assertEqual(self.writes(), [])

    def test_failure_after_the_clone_is_partial_and_says_where_the_copy_is(self):
        self.fake.FAIL_TRANSACTIONS.add("[2/4]")
        with self.assertRaises(BridgeError) as cm:
            editing.split_clip(self.rpc, kind="video", track=0, at=20.0, label="Split")
        e = cm.exception
        self.assertEqual(e.outcome, "partial")
        self.assertEqual(e.code, "TRANSACTION_FAILED")
        self.assertEqual(len(e.data["completed"]), 1)
        self.assertIn("[1/4] clone", e.data["completed"][0])
        parked = e.data["parked"]
        self.assertEqual(len(parked), 1)
        park = parked[0]["start"]
        self.assertGreaterEqual(park, 60.0 + editing.CLONE_MARGIN)
        self.assertAlmostEqual(park, S(park), places=6)  # on the frame grid
        self.assertIn(f"{park:.3f}", str(e))
        self.assertSpans("V1", [(0.0, 60.0, 0.0, 60.0), (park, park + 60.0, 0.0, 60.0)])
        # ... so it can be removed
        self.rpc("remove_items", {"videoTracks": [0], "fromSeconds": park - F, "toSeconds": parked[0]["end"] + F})
        self.assertSpans("V1", [(0.0, 60.0, 0.0, 60.0)])

    def test_failure_of_the_first_write_is_not_partial(self):
        self.fake.FAIL_TRANSACTIONS.add("[1/4]")
        with self.assertRaises(BridgeError) as cm:
            editing.split_clip(self.rpc, kind="video", track=0, at=20.0, label="Split")
        self.assertEqual(cm.exception.code, "TRANSACTION_FAILED")
        self.assertNotEqual(cm.exception.outcome, "partial")
        if cm.exception.outcome == "unknown":
            self.assertIn("list_items", cm.exception.hint)
        self.assertSpans("V1", [(0.0, 60.0, 0.0, 60.0)])

    def test_wrong_semantics_are_caught(self):
        """A trim_clip whose head trim leaves the in point alone (not what Premiere does) must not go unnoticed."""
        self.fake.HANDLERS["trim_clip"] = _stub_trim(self.fake, broken=True)
        with self.assertRaises(BridgeError) as cm:
            editing.split_clip(self.rpc, kind="video", track=0, at=20.0, label="Split")
        e = cm.exception
        self.assertEqual(e.outcome, "partial")
        self.assertIn("[verify]", str(e))
        self.assertEqual(len(e.data["completed"]), 3)
        self.assertIn("[3/4]", e.data["lastStep"])
        self.assertTrue(e.data["parked"])

    def test_speed_changed_clip_is_refused(self):
        self.fake.STATE["video"][0][0]["speed"] = 2.0
        with self.assertRaises(BridgeError) as cm:
            editing.split_clip(self.rpc, kind="video", track=0, at=20.0, label="Split")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("UNSUPPORTED", "not_committed"))
        self.assertEqual(self.writes(), [])

    def test_label_is_required(self):
        with self.assertRaises(BridgeError) as cm:
            editing.split_clip(self.rpc, kind="video", track=0, at=20.0, label=" ")
        self.assertEqual(cm.exception.code, "BAD_PARAMS")


# ---------------------------------------------------------------- cut_range

class CutRangeTest(EditingCase):
    A, B = S(14), S(20)

    def expected(self):
        a, b = self.A, self.B
        d = b - a
        return {
            "V1": [(0, S(10), 0, S(10)), (S(10), a, 20, 20 + a - S(10)),
                   (a, S(25) - d, 20 + b - S(10), 20 + S(25) - S(10)), (S(25) - d, S(40) - d, 40, 40 + S(40) - S(25))],
            "V2": [(S(5), S(8), 2, 2 + S(8) - S(5)), (S(30) - d, S(33) - d, 4, 4 + S(33) - S(30))],
            "A2": [(0, a, 0, a), (a, S(38) - d, b, S(38))],
            "A3": [(S(13), a, 0, a - S(13)), (a, S(21) - d, 0.5 + b - S(19), 0.5 + S(21) - S(19)),
                   (S(27) - d, S(27.5) - d, 0, S(27.5) - S(27))],
        }

    def test_cut_across_tracks_with_gaps_keeps_sync(self):
        self.set_timeline(*gap_timeline())
        self.rpc("set_params", {"videoTrack": 0, "atSeconds": 12.0, "components": [
            {"matchName": "AE.ADBE Motion", "params": [{"index": 1, "value": 120.0}]}]})
        old = {(kind, c["tag"]): c["start"] for kind in ("video", "audio") for t in self.fake.STATE[kind] for c in t}
        out = editing.cut_range(self.rpc, start=14.0, end=20.0, label="Cut test")
        a, b = self.A, self.B
        shift = b - a
        self.assertTrue(out["verified"])
        self.assertAlmostEqual(out["removedSeconds"], shift, places=6)

        for label, want in self.expected().items():
            self.assertSpans(label, want)
        self.assertEqual(self.spans("A1"), self.spans("V1"))  # picture and dialogue still line up
        # every clip that started after the cut moved by exactly the same amount, on every track, gaps or not
        for kind, tag in (("video", "C"), ("video", "b3"), ("audio", "c"), ("audio", "s3")):
            now = next(c for t in self.fake.STATE[kind] for c in t if c["tag"] == tag)
            self.assertAlmostEqual(now["start"], old[(kind, tag)] - shift, delta=F / 2, msg=tag)
        # the pieces after the cut show the same source as before, just earlier
        for label in ("V1", "A1", "V2", "A2", "A3"):
            for c in self.track(label):
                if c["start"] >= a - F / 2:
                    src_old = next(o for o in gap_timeline()[0 if label[0] == "V" else 1][int(label[1:]) - 1]
                                   if o["tag"] == c["tag"])
                    self.assertAlmostEqual(c["start"] - c["inPoint"], src_old["start"] - src_old["inPoint"] - shift,
                                           delta=F / 2, msg=f"{label} {c['tag']}")
        # attributes survive: both pieces of the split clip keep Motion > Scale 120
        self.assertEqual(self.components(0, 12.0), self.components(0, a + 1.0))
        self.assertEqual(self.components(0, a + 1.0)[1]["params"][1]["value"], 120.0)

        self.assertEqual(len(out["splits"]), 3)
        self.assertEqual(sorted((t["track"], t["edge"]) for t in out["trims"]), [("A3", "head"), ("A3", "tail")])
        self.assertEqual([(r["track"], r["name"]) for r in out["removals"]], [("V2", "broll_city.mp4")])
        self.assertEqual(len(out["moves"]), 8)
        self.assertEqual([m["from"] for m in out["moves"]], sorted(m["from"] for m in out["moves"]))
        self.assertTrue(all(abs(m["from"] - m["to"] - shift) < 1e-6 for m in out["moves"]))
        self.assertIn("markers", out["markers"].lower())
        cut_labels = [t for t in self.fake.TRANSACTIONS if t.startswith("Cut test: ")]
        self.assertEqual(len(cut_labels), out["undoSteps"])
        self.assertEqual(out["undoSteps"], 3 * 4 + 2 + 1 + 1)  # the ripple is one shift_items step
        shifts = [p for a, p in self.calls if a == "shift_items"]
        self.assertEqual(len(shifts), 1)
        self.assertAlmostEqual(shifts[0]["deltaSeconds"], -shift, delta=1e-6)
        self.assertFalse([a for a, _ in self.writes() if a == "insert_clip"])
        removes = [p for a, p in self.calls if a == "remove_items"]
        self.assertTrue(removes[0]["dryRun"])  # checked before the real removal
        self.assertFalse(any(p.get("ripple") for p in removes))

    def test_dry_run_plans_without_writing_and_matches_the_real_run(self):
        self.set_timeline(*gap_timeline())
        before = copy.deepcopy({k: self.fake.STATE[k] for k in ("video", "audio")})
        plan = editing.cut_range(self.rpc, start=14.0, end=20.0, label="Cut", dry_run=True)
        self.assertTrue(plan["dryRun"])
        self.assertEqual({k: self.fake.STATE[k] for k in ("video", "audio")}, before)
        self.assertEqual(self.writes(), [])
        self.assertEqual({a for a, _ in self.calls}, {"get_sequence", "list_items"})
        self.assertEqual([(s["track"], s["name"]) for s in plan["splits"]],
                         [("V1", "interview.mp4"), ("A1", "interview.mp4"), ("A2", "music.wav")])
        self.assertEqual(len(plan["trims"]), 2)
        self.assertEqual(len(plan["removals"]), 1)
        self.assertEqual(len(plan["moves"]), 8)
        self.assertEqual(plan["undoSteps"], 16)
        self.assertEqual(len(plan["steps"]), 16)

        real = editing.cut_range(self.rpc, start=14.0, end=20.0, label="Cut")
        for key in ("splits", "trims", "removals", "moves", "steps"):
            self.assertEqual(real[key], plan[key], key)

    def test_a_panel_without_shift_items_gets_one_move_per_clip(self):
        self.set_timeline(*gap_timeline())
        self.fake.HANDLERS.pop("shift_items", None)
        out = editing.cut_range(self.rpc, start=14.0, end=20.0, label="Cut")
        self.assertTrue(out["verified"])
        self.assertEqual(out["undoSteps"], 3 * 4 + 2 + 1 + 8)
        moves = [p for a, p in self.calls if a == "trim_clip" and "moveTo" in p]
        self.assertEqual(len(moves), 3 + 8)  # each split ends with a move, then one per rippled clip
        for label, spans in self.expected().items():
            self.assertSpans(label, spans)

    def test_only_some_tracks_reports_what_was_left_behind(self):
        self.set_timeline(*gap_timeline())
        out = editing.cut_range(self.rpc, start=14.0, end=20.0, tracks=["V1", "A1"], label="Cut")
        self.assertEqual(out["tracks"], ["V1", "A1"])
        self.assertEqual(sorted((n["track"], n["name"]) for n in out["notMoved"]),
                         [("A3", "sfx.wav"), ("V2", "broll_city.mp4")])
        self.assertSpans("V1", self.expected()["V1"])
        self.assertSpans("A2", [(0, S(38), 0, S(38))])  # untouched

    def test_no_ripple_leaves_a_gap(self):
        self.set_timeline(*gap_timeline())
        editing.cut_range(self.rpc, start=14.0, end=20.0, ripple=False, label="Cut")
        a, b = self.A, self.B
        self.assertSpans("A2", [(0, a, 0, a), (b, S(38), b, S(38))])

    def test_refusals_write_nothing(self):
        self.set_timeline(*gap_timeline())
        for kwargs, code in (({"start": 20.0, "end": 14.0}, "BAD_PARAMS"),
                             ({"start": 14.0, "end": 14.0 + F / 4}, "BAD_PARAMS"),
                             ({"start": 14.0, "end": 20.0, "tracks": ["V9"]}, "NOT_FOUND"),
                             ({"start": 14.0, "end": 20.0, "captions": {"track": "V3", "srt_path": "/fake/no.srt"}},
                              "FILE_NOT_FOUND")):
            with self.assertRaises(BridgeError, msg=kwargs) as cm:
                editing.cut_range(self.rpc, label="Cut", **kwargs)
            self.assertEqual((cm.exception.code, cm.exception.outcome), (code, "not_committed"), kwargs)
        self.fake.STATE["audio"][1][0]["speed"] = 0.5  # the music crosses the cut
        with self.assertRaises(BridgeError) as cm:
            editing.cut_range(self.rpc, start=14.0, end=20.0, label="Cut")
        self.assertEqual(cm.exception.code, "UNSUPPORTED")
        self.assertEqual(self.writes(), [])

    def test_cut_conforms_the_caption_movie(self):
        """The real-job case: the cut ends 4 frames before the next caption line, so after the ripple the caption
        piece would open on the previous line for a few frames; the conform trims that piece's head."""
        cue = 9.0
        srt = self.srt("1\n00:00:00,000 --> 00:00:03,000\nOne\n\n2\n00:00:03,000 --> 00:00:06,500\nTwo\n\n"
                       "3\n00:00:06,500 --> 00:00:09,000\nThree\n\n4\n00:00:09,000 --> 00:00:12,000\nFour\n")
        self.set_timeline([[clip("interview.mp4", 0, 40, 0.0)], [], [clip("captions.mov", 0, 40, 0.0)], []],
                          [[clip("interview.mp4", 0, 40, 0.0)], [], [], []])
        a, b = S(4.0), S(cue) - 4 * F
        out = editing.cut_range(self.rpc, start=a, end=b, label="Cut",
                                captions={"track": "V3", "srt_path": str(srt)})
        self.assertTrue(out["verified"])
        changes = out["captions"]["changes"]
        self.assertEqual([(c["edge"], c["cue"]["index"], c["until"]["index"]) for c in changes], [("head", 3, 4)])
        right = self.track("V3")[1]
        self.assertGreaterEqual(right["inPoint"], cue - 1e-6)  # not one frame of line three
        self.assertLess(right["inPoint"] - cue, F)             # and at most one frame of line four lost
        self.assertAlmostEqual(right["start"], a + changes[0]["trimmedFrames"] * F, delta=F / 2)
        self.assertAlmostEqual(right["start"] - right["inPoint"], a - b, delta=F / 2)  # still in sync
        self.assertSpans("V1", [(0, a, 0, a), (a, S(40) - (b - a), b, S(40))])


# ---------------------------------------------------------------- extend_cut

class ExtendCutTest(EditingCase):
    T, OUT, IN = S(10), S(0.3), S(0.25)

    def test_opens_the_cut_on_every_track_in_sync(self):
        self.set_timeline(*gap_timeline())
        t, o, i = self.T, self.OUT, self.IN
        d = o + i
        out = editing.extend_cut(self.rpc, at=10.0, out_by=0.3, in_by=0.25, label="Breathe")
        self.assertTrue(out["verified"])
        self.assertAlmostEqual(out["addedSeconds"], d, delta=1e-6)
        # dialogue: A's tail runs on into its own source, B's head comes back into its own source
        for label in ("V1", "A1"):
            self.assertSpans(label, [(0, t + o, 0, t + o), (t + o, S(25) + d, 20 - i, 20 + S(25) - t),
                                     (S(25) + d, S(40) + d, 40, 40 + S(40) - S(25))])
        # B-roll and SFX after the cut move right by the same amount, music under the cut is untouched
        self.assertSpans("V2", [(S(5), S(8), 2, 2 + S(8) - S(5)), (S(16) + d, S(18) + d, 1, 1 + S(18) - S(16)),
                                (S(30) + d, S(33) + d, 4, 4 + S(33) - S(30))])
        self.assertSpans("A2", [(0, S(38), 0, S(38))])
        self.assertEqual([s["track"] for s in out["crossing"]], ["A2"])
        self.assertEqual(sorted((e["track"], e["edge"]) for e in out["extended"]),
                         [("A1", "head"), ("A1", "tail"), ("V1", "head"), ("V1", "tail")])
        self.assertEqual(out["gaps"], [])
        shifts = [p for a, p in self.calls if a == "shift_items"]
        self.assertEqual(len(shifts), 1)
        self.assertAlmostEqual(shifts[0]["deltaSeconds"], d, delta=1e-9)
        self.assertEqual(out["undoSteps"], 1 + 2 + 2)
        self.assertEqual(len([x for x in self.fake.TRANSACTIONS if x.startswith("Breathe: ")]), 5)
        self.assertIn("markers", out["markers"].lower())

    def test_dry_run_plans_the_same_steps_and_writes_nothing(self):
        self.set_timeline(*gap_timeline())
        self.rpc("inspect_items", {"videoTracks": [0, 1], "audioTracks": [0, 1, 2]})  # the fake builds chains on read
        before = copy.deepcopy({k: self.fake.STATE[k] for k in ("video", "audio")})
        plan = editing.extend_cut(self.rpc, at=10.0, in_by=0.25, label="Breathe", dry_run=True)
        self.assertEqual({k: self.fake.STATE[k] for k in ("video", "audio")}, before)
        self.assertEqual(self.writes(), [])
        real = editing.extend_cut(self.rpc, at=10.0, in_by=0.25, label="Breathe")
        self.assertEqual(real["steps"], plan["steps"])
        self.assertEqual(real["undoSteps"], 3)

    def fade(self, label, at, keyframes):
        """Keyframes on the clip under `at` (sequence time): Volume > Level on A…, Opacity on V…."""
        comp = ({"matchName": "Internal Volume Mono", "params": [{"index": 1, "timeBase": "sequence",
                                                                 "keyframes": keyframes}]}
                if label[0] == "A" else
                {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "timeBase": "sequence",
                                                             "keyframes": keyframes}]})
        key = "audioTrack" if label[0] == "A" else "videoTrack"
        self.rpc("set_params", {key: int(label[1:]) - 1, "atSeconds": at, "components": [comp]})

    def test_a_fade_to_silence_at_the_old_end_refuses_the_extension(self):
        """Real job: a 2-frame fade-out to 0 at the outgoing clip's end held the whole extension silent."""
        self.set_timeline(*gap_timeline())
        t = self.T
        self.fade("A1", 5.0, [{"t": t - 2 * F, "value": LEVEL_0DB}, {"t": t, "value": 0.0}])
        self.fade("V1", 15.0, [{"t": t, "value": 0.0}, {"t": t + 2 * F, "value": 100.0}])  # fade-in on B
        calls = len(self.calls)
        plan = editing.extend_cut(self.rpc, at=10.0, out_by=0.3, in_by=0.25, label="Breathe", dry_run=True)
        silent = sorted((h["track"], h["edge"], h["param"]) for h in plan["heldKeyframes"] if h["silent"])
        self.assertEqual(silent, [("A1", "tail", "Volume > Level"), ("V1", "head", "Opacity")])
        self.assertEqual(next(h for h in plan["heldKeyframes"] if h["track"] == "A1")["heldDb"], "-inf")
        self.assertTrue(any("--allow-silent" in n for n in plan["notes"]))
        with self.assertRaises(BridgeError) as cm:
            editing.extend_cut(self.rpc, at=10.0, out_by=0.3, in_by=0.25, label="Breathe")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("KEYFRAMED", "not_committed"))
        self.assertEqual([a for a, _ in self.calls[calls:] if a in ("shift_items", "trim_clip")], [])
        out = editing.extend_cut(self.rpc, at=10.0, out_by=0.3, in_by=0.25, label="Breathe", allow_silent=True)
        self.assertTrue(out["verified"])
        self.assertEqual(len([h for h in out["heldKeyframes"] if h["silent"]]), 2)

    def test_a_fade_that_holds_a_level_or_runs_on_is_not_refused(self):
        self.set_timeline(*gap_timeline())
        t = self.T
        self.fade("A1", 5.0, [{"t": t - 1.0, "value": LEVEL_0DB}, {"t": t - 0.5, "value": LEVEL_0DB / 2}])  # -6 dB
        out = editing.extend_cut(self.rpc, at=10.0, out_by=0.3, label="Breathe")
        self.assertTrue(out["verified"])
        held = out["heldKeyframes"]
        self.assertEqual([(h["track"], h["silent"]) for h in held], [("A1", False)])
        self.assertAlmostEqual(held[0]["heldDb"], -6.02, delta=0.01)
        self.assertFalse(any("allow-silent" in n for n in out["notes"]))

    def test_short_source_is_a_gap_by_default_and_a_refusal_when_named(self):
        t = S(10)
        video = [[clip("a.mp4", 0, 10, 0.0), clip("b.mp4", 10, 20, 0.1)], [], [], []]
        audio = [[clip("a.mp4", 0, 10, 0.0), clip("b.mp4", 10, 20, 0.1)], [], [], []]
        self.set_timeline(video, audio)
        with self.assertRaises(BridgeError) as cm:
            editing.extend_cut(self.rpc, at=10.0, out_by=0.3, in_by=0.25, extend=["V1", "A1"], label="Breathe")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("UNSUPPORTED", "not_committed"))
        self.assertEqual(self.writes(), [])
        out = editing.extend_cut(self.rpc, at=10.0, out_by=0.3, in_by=0.25, label="Breathe")
        self.assertEqual(sorted((g["track"], g["edge"]) for g in out["gaps"]), [("A1", "head"), ("V1", "head")])
        d = self.OUT + self.IN
        self.assertSpans("V1", [(0, t + self.OUT, 0, t + self.OUT), (t + d, S(20) + d, 0.1, 0.1 + S(20) - t)])

    def test_media_length_limits_the_tail(self):
        self.set_timeline(*gap_timeline())
        length = {"interview.mp4": S(10) + 0.1}  # only 0.1 s of source after A's out point
        media = lambda c: length.get(c["name"])  # noqa: E731
        with self.assertRaises(BridgeError) as cm:
            editing.extend_cut(self.rpc, at=10.0, out_by=0.3, extend=["V1"], media_seconds=media, label="Breathe")
        self.assertEqual(cm.exception.code, "UNSUPPORTED")
        out = editing.extend_cut(self.rpc, at=10.0, out_by=0.3, media_seconds=media, label="Breathe")
        self.assertEqual(sorted(g["track"] for g in out["gaps"]), ["A1", "V1"])

    def test_extend_only_some_tracks_leaves_a_gap_on_the_others(self):
        self.set_timeline(*gap_timeline())
        out = editing.extend_cut(self.rpc, at=10.0, out_by=0.3, extend=["A1"], label="Breathe")
        self.assertEqual([(g["track"], g["reason"]) for g in out["gaps"]], [("V1", "not in extend")])
        t, o = self.T, self.OUT
        self.assertSpans("V1", [(0, t, 0, t), (t + o, S(25) + o, 20, 20 + S(25) - t),
                                (S(25) + o, S(40) + o, 40, 40 + S(40) - S(25))])
        self.assertAlmostEqual(self.track("A1")[0]["end"], t + o, delta=F / 2)

    def test_refusals_write_nothing(self):
        self.set_timeline(*gap_timeline())
        for kwargs, code in (({"at": 12.0, "out_by": 0.3}, "NOT_FOUND"),
                             ({"at": 10.0, "out_by": -0.3}, "BAD_PARAMS"),
                             ({"at": 10.0, "out_by": F / 4}, "BAD_PARAMS"),
                             ({"at": 10.0, "in_by": 0.2, "extend": ["V2"]}, "NOT_FOUND"),
                             ({"at": 10.0, "in_by": 0.2, "tracks": ["V1"], "extend": ["A1"]}, "BAD_PARAMS")):
            with self.assertRaises(BridgeError, msg=kwargs) as cm:
                editing.extend_cut(self.rpc, label="Breathe", **kwargs)
            self.assertEqual((cm.exception.code, cm.exception.outcome), (code, "not_committed"), kwargs)
        self.assertEqual(self.writes(), [])


# ---------------------------------------------------------------- snap_edges

class SnapEdgesTest(EditingCase):
    US = 1e-6

    def drifted(self):
        """The real-job state: every edit wrote frame + 1 us, shifts added more, so edges sit 1-8 us late."""
        u = self.US
        video = [[clip("a.mp4", 0, 10, 0.0), clip("b.mp4", 10, 20, 5.0)], [], [], []]
        audio = [[clip("a.mp4", 0, 10, 0.0), clip("b.mp4", 10, 20, 5.0)], [], [], []]
        for track in (video[0], audio[0]):
            track[0]["end"] += 1 * u
            track[1]["start"] += 2 * u
            track[1]["end"] += 8 * u
        self.set_timeline(video, audio)

    def offsets(self):
        return [round((c[e] - S(c[e])) * 1e6, 3) for kind in ("video", "audio") for t in self.fake.STATE[kind]
                for c in t for e in ("start", "end")]

    def test_puts_every_edge_back_on_its_frame_without_changing_the_content(self):
        self.drifted()
        before = {label: [(c["start"], c["inPoint"]) for c in self.track(label)] for label in ("V1", "A1")}
        out = editing.snap_edges(self.rpc, label="Snap")
        self.assertTrue(out["verified"])
        self.assertEqual(out["residual"], [])
        self.assertEqual(set(self.offsets()), {0.0})
        for label in ("V1", "A1"):
            for (s0, i0), c in zip(before[label], self.track(label)):
                self.assertEqual(c["inPoint"], i0)                      # a move keeps the in point...
                self.assertAlmostEqual(c["start"], s0, delta=1e-5)      # ...and shifts by microseconds only
        # per clip: a1 end trimmed, b1 moved (its end lands 6 us late -> trimmed too), per track
        self.assertEqual(out["undoSteps"], 2 * 3)
        self.assertEqual([(m["track"], m["name"]) for m in out["moved"]], [("V1", "b.mp4"), ("A1", "b.mp4")])

    def test_dry_run_and_a_clean_timeline(self):
        self.drifted()
        before = copy.deepcopy(self.fake.STATE["video"])
        plan = editing.snap_edges(self.rpc, label="Snap", dry_run=True)
        self.assertEqual((plan["clips"], plan["undoSteps"], self.writes()), (4, 6, []))
        self.assertEqual(self.fake.STATE["video"], before)
        self.set_timeline([[clip("a.mp4", 0, 10, 0.0)], [], [], []], [[clip("a.mp4", 0, 10, 0.0)], [], [], []])
        out = editing.snap_edges(self.rpc, label="Snap")
        self.assertEqual((out["clips"], out["undoSteps"], out["verified"]), (0, 0, True))


# ---------------------------------------------------------------- conform_captions

class ConformCaptionsTest(EditingCase):
    def setUp(self):
        super().setUp()
        self.set_timeline([[clip("interview.mp4", 0, 12, 0.0)], [], caption_pieces(), []],
                          [[clip("interview.mp4", 0, 12, 0.0)], [], [], []])

    def test_trims_a_head_four_frames_before_a_cue_and_leaves_clean_pieces_alone(self):
        p1, p2, p3, p4 = caption_pieces()
        out = editing.conform_captions(self.rpc, track="V3", srt_path=self.srt(), label="Captions")
        self.assertTrue(out["verified"])
        self.assertEqual(out["pieces"], 4)
        self.assertEqual([(c["edge"], c["at"]) for c in out["changes"]],
                         [("head", round(p2["start"], 6)), ("tail", round(p4["start"], 6))])
        head, tail = out["changes"]
        self.assertEqual((head["cue"]["index"], head["until"]["index"], head["trimmedFrames"]), (2, 3, 4))
        self.assertEqual((tail["cue"]["index"], tail["trimmedFrames"]), (3, 5))
        self.assertEqual(out["flagged"], [])
        self.assertSpans("V3", [
            (p1["start"], p1["end"], p1["inPoint"], p1["outPoint"]),          # clean: untouched
            (154 * F, p2["end"], 7.5, p2["outPoint"]),                        # head trimmed onto cue 3
            (p3["start"], p3["end"], p3["inPoint"], p3["outPoint"]),          # clean: untouched
            (p4["start"], 599 * F, p4["inPoint"], p4["outPoint"] - 5 * F)])   # tail trimmed off cue 3
        # A frame shows the line current at its START (measured live): the last frame starts before cue 3, and
        # the first frame cut away starts at or after it, so no good frame of line two is lost either.
        last_in = self.track("V3")[3]["outPoint"] - F
        self.assertLess(last_in, 7.5)
        self.assertGreaterEqual(last_in + F, 7.5 - 1e-6)
        self.assertEqual(len([t for t in self.fake.TRANSACTIONS if t.startswith("Captions: ")]), 2)
        self.assertEqual(out["undoSteps"], 2)

    def test_dry_run_reports_the_same_changes_without_writing(self):
        before = copy.deepcopy(self.fake.STATE["video"])
        out = editing.conform_captions(self.rpc, track=2, srt_path=self.srt(), label="Captions", dry_run=True)
        self.assertTrue(out["dryRun"])
        self.assertEqual(len(out["changes"]), 2)
        self.assertEqual(self.fake.STATE["video"], before)
        self.assertEqual(self.writes(), [])

    def test_offset_maps_srt_time_onto_source_time(self):
        shifted = "".join(line.replace("00:00:", "00:01:") if "-->" in line else line
                          for line in CAPTION_SRT.splitlines(True))  # every cue 60 s later
        plain = editing.plan_caption_fixes(caption_pieces(), editing.parse_srt(CAPTION_SRT), fps=FPS)
        moved = editing.plan_caption_fixes(caption_pieces(), editing.parse_srt(shifted), fps=FPS, offset=60.0)
        self.assertEqual([(c["edge"], c["to"]) for c in moved["changes"]],
                         [(c["edge"], c["to"]) for c in plain["changes"]])
        none = editing.plan_caption_fixes(caption_pieces(), editing.parse_srt(shifted), fps=FPS)
        self.assertEqual(none["changes"], [])  # without the offset no piece meets a cue

    def test_a_line_with_time_to_run_is_joined_in_progress(self):
        """The real-job case: a cut removed a fragment, so a piece starts 7 frames into a 1.4 s line. That line is
        what is being said: showing it late is right, trimming to the next line would hide it."""
        cues = editing.parse_srt(CAPTION_SRT)
        for p in (piece(0, 60, 5.0),           # inside cue 2 (4.0-7.5) throughout
                  piece(0, 300, 6.0),          # 1.5 s of cue 2 left, then cue 3
                  piece(0, 120, 4.0 + 7 * F)):  # 7 frames late into cue 2
            out = editing.plan_caption_fixes([p], cues, fps=FPS)
            self.assertEqual((out["changes"], out["flagged"]), ([], []), p)

    def test_a_piece_showing_a_sliver_of_one_cue_only_is_flagged(self):
        cues = editing.parse_srt(CAPTION_SRT)
        out = editing.plan_caption_fixes([piece(0, 12, 7.3)], cues, fps=FPS)  # the last 0.2 s of cue 2, nothing else
        self.assertEqual(out["changes"], [])
        self.assertEqual(len(out["flagged"]), 1)
        self.assertEqual(out["flagged"][0]["cue"]["index"], 2)

    def test_missing_srt(self):
        with self.assertRaises(BridgeError) as cm:
            editing.conform_captions(self.rpc, track="V3", srt_path=self.tmp / "none.srt", label="Captions")
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")


# ---------------------------------------------------------------- pure pieces

class PureTest(unittest.TestCase):
    def test_parse_srt_tolerates_bom_crlf_dots_and_missing_indexes(self):
        text = ("\ufeff2\r\n00:00:05.5 --> 00:00:07,250 X1:10\r\nSecond\r\nline\r\n\r\n"
                "00:00:01,000 --> 00:00:02,000\r\nFirst\r\n")
        cues = editing.parse_srt(text)
        self.assertEqual([(c["start"], c["end"], c["text"]) for c in cues],
                         [(1.0, 2.0, "First"), (5.5, 7.25, "Second\nline")])
        self.assertEqual(cues[1]["index"], 2)

    def test_assumed_semantics_are_written_down(self):
        c = {"start": 10.0, "end": 20.0, "inPoint": 3.0, "outPoint": 13.0}
        self.assertEqual(editing.trim_fields(editing.HEAD, 12.0), {"start": 12.0})
        self.assertEqual(editing.trim_fields(editing.MOVE, 5.0), {"moveTo": 5.0})
        self.assertEqual(editing.predict_trim(c, editing.HEAD, 12.0),
                         {"start": 12.0, "end": 20.0, "inPoint": 5.0, "outPoint": 13.0})
        self.assertEqual(editing.predict_trim(c, editing.TAIL, 15.0),
                         {"start": 10.0, "end": 15.0, "inPoint": 3.0, "outPoint": 8.0})
        self.assertEqual(editing.predict_trim(c, editing.MOVE, 4.0),
                         {"start": 4.0, "end": 14.0, "inPoint": 3.0, "outPoint": 13.0})
        self.assertEqual(editing.predict_clone(c, 50.0),
                         {"start": 60.0, "end": 70.0, "inPoint": 3.0, "outPoint": 13.0})


if __name__ == "__main__":
    unittest.main()
