"""The fake panel behaves like the v0.4 panel where clients depend on it - dispatch order, guards, the outcome rule,
insert/overwrite edits, set_params (time bases, occurrence, everything validated before anything changes),
transitions and OTIO, transcripts and Speech-to-Text jobs - and the library (replace_clip, ops) runs end to end
against it, calling only the panel actions its spec command lists.

Everything runs in-process through fake_panel.dispatch (which also applies the spec's param check); nothing
connects anywhere. Library tests skip until ppro_bridge.replace / ppro_bridge.ops import.
"""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import copy
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO, REPO / "broker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ppro_bridge import spec  # noqa: E402

import fake_panel as fake  # noqa: E402


def _module(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


errors = _module("ppro_bridge.errors")
replace = _module("ppro_bridge.replace")
ops = _module("ppro_bridge.ops")
transcript = _module("ppro_bridge.transcript")

FPS = fake.FPS
CN_TITLE = {"name": "title.mov", "bin": "CN", "mediaPath": "/fake/cn/title.mov"}


def snapshot():
    return copy.deepcopy({k: fake.STATE.get(k) for k in ("items", "video", "audio", "markers", "transitions")})


class FakeCase(unittest.TestCase):
    """A fresh fake per test, no PPRO_FAKE_* switches unless the test sets them."""

    def setUp(self):
        fake.reset()
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        for key in [k for k in os.environ if k.startswith("PPRO_FAKE_")]:
            del os.environ[key]
        self.scratch = Path(tempfile.mkdtemp(prefix="ppro_parity_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.addCleanup(fake.reset)

    def call(self, action, params=None, guard=None):
        msg = {"id": "t", "action": action, "params": params or {}}
        if guard is not None:
            msg["guard"] = guard
        return fake.dispatch(msg)

    def ok(self, action, params=None, guard=None):
        reply = self.call(action, params, guard)
        self.assertTrue(reply["ok"], reply)
        return reply["data"]

    def fails(self, action, params, code, outcome="not_committed", guard=None):
        reply = self.call(action, params, guard)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual((reply["code"], reply["outcome"]), (code, outcome), reply["error"])
        return reply

    def place(self, item="broll_city.mp4", at=10.0, v=1, a=1, rng=(2.0, 7.0), **extra):
        p = {"itemName": item, "atSeconds": at, "videoTrack": v, "audioTrack": a}
        if item == "title.mov":
            p["binPath"] = "EN"
        if rng:
            p.update(inSeconds=rng[0], outSeconds=rng[1])
        p.update(extra)
        self.ok("insert_clip", p)
        return next(c for c in fake.STATE["video"][v] if abs(c["start"] - fake.snap(at)) < 1e-6)

    def comps(self, v, at):
        clip = next(c for t in self.ok("inspect_items", {"videoTracks": [v], "fromSeconds": at, "toSeconds": at})["tracks"]
                    for c in t["clips"] if c["start"] <= at < c["end"])
        return clip["components"]

    def param(self, v, at, match_name, index, occurrence=1):
        comp = next(c for c in self.comps(v, at) if c["matchName"] == match_name and c["occurrence"] == occurrence)
        return comp["params"][index]


# ---------------------------------------------------------------- the envelope

class DispatchTest(FakeCase):
    def test_unknown_action(self):
        self.fails("eval_script", {}, "UNKNOWN_ACTION")

    def test_bad_params_never_reach_the_handler(self):
        seen = []
        fake.HANDLERS["insert_clip"] = lambda p: seen.append(p) or {}
        reply = self.fails("insert_clip", {"itemName": "broll_city.mp4", "inSeconds": 1.0}, "BAD_PARAMS")
        self.assertTrue(reply["data"]["problems"])
        self.fails("insert_clip", {"itemName": "broll_city.mp4", "speed": 2}, "BAD_PARAMS")
        self.assertEqual(seen, [])

    def test_stuck_panel_answers_ping_only(self):
        fake.STUCK = {"action": "insert_clip", "id": "r1", "since": fake.now_ms() - 5000}
        before = snapshot()
        self.fails("get_project", {}, "PANEL_STUCK")
        self.fails("insert_clip", {}, "PANEL_STUCK")  # stuck is checked before the params
        stuck = self.ok("ping")["stuck"]
        self.assertEqual((stuck["action"], stuck["id"]), ("insert_clip", "r1"))
        self.assertGreaterEqual(stuck["seconds"], 4)
        self.assertEqual(snapshot(), before)

    def test_denied_host_answers_ping_only(self):
        os.environ["PPRO_FAKE_DENIED"] = "test build"
        self.assertEqual(self.ok("ping")["denied"]["reason"], "test build")
        self.fails("get_sequence", {}, "HOST_DENIED")
        self.fails("get_sequence", {"bogus": 1}, "HOST_DENIED")  # denied comes before the param check
        fake.STUCK = {"action": "x", "id": "y", "since": fake.now_ms()}
        self.fails("get_sequence", {}, "PANEL_STUCK")  # and stuck before denied

    def test_capabilities(self):
        os.environ["PPRO_FAKE_CAPS"] = json.dumps({"transcribe": False})
        caps = self.ok("ping")["capabilities"]
        self.assertIs(caps["transcribe"], False)
        self.assertIs(caps["razor"], True)
        self.fails("transcribe_clip", {"itemName": "broll_city.mp4"}, "UNSUPPORTED")
        self.fails("transcribe_clip", {}, "BAD_PARAMS")  # the param check comes first
        os.environ["PPRO_FAKE_CAPS"] = json.dumps({"transcribe": "unknown"})  # "unknown": run, the handler checks
        self.ok("transcribe_clip", {"itemName": "broll_city.mp4"})

    def test_guard(self):
        good = {"project": "Smoke Test.prproj", "projectPrefix": "Smoke", "projectGuid": "fake-project-guid",
                "sequence": "Sequence 01", "sequenceGuid": "fake-sequence-guid"}
        place = {"itemName": "broll_city.mp4", "atSeconds": 10.0, "videoTrack": 1, "audioTrack": 1}
        before = snapshot()
        for bad, code in (({"project": "Other.prproj"}, "PROJECT_MISMATCH"),
                          ({"projectPrefix": "PAB_TEST"}, "PROJECT_MISMATCH"),
                          ({"projectGuid": "another-guid"}, "PROJECT_MISMATCH"),
                          ({"sequence": "Sequence 02"}, "SEQUENCE_MISMATCH"),
                          ({"sequenceGuid": "another-guid"}, "SEQUENCE_MISMATCH")):
            with self.subTest(guard=bad):
                self.fails("insert_clip", place, code, guard=bad)
                self.assertEqual(fake.LAST_CTX["sideEffects"], 0)
        self.assertEqual(snapshot(), before)
        self.fails("import_media", {"paths": ["/fake/b.mp4"]}, "PROJECT_MISMATCH", guard={"projectGuid": "x"})
        self.fails("list_transition_names", {}, "PROJECT_MISMATCH", guard={"project": "Other.prproj"})
        self.ok("ping", guard={"project": "Other.prproj"})  # ping ignores guards
        self.ok("insert_clip", place, guard=good)
        self.fails("get_project", {}, "BAD_PARAMS", guard={"projetGuid": "typo"})
        self.fails("get_project", {}, "BAD_PARAMS", guard="Smoke Test.prproj")

    def test_project_name_switch_and_guids(self):
        os.environ["PPRO_FAKE_PROJECT_NAME"] = "PAB_TEST_fake"
        project = self.ok("get_project", guard={"projectPrefix": "PAB_TEST"})
        self.assertEqual((project["name"], project["guid"]), ("PAB_TEST_fake", "fake-project-guid"))
        seq = self.ok("get_sequence")
        self.assertEqual((seq["guid"], seq["fps"], seq["timebase"]), ("fake-sequence-guid", 59.94, "4237833600"))

    def test_no_project_and_no_sequence(self):
        fake.STATE["sequence"] = None
        self.fails("get_sequence", {}, "NO_SEQUENCE")
        self.assertIsNone(self.ok("get_project")["activeSequence"])
        fake.STATE["project"] = None
        self.fails("list_project_items", {}, "NO_PROJECT")

    def test_outcome_rule(self):
        rng = {"itemName": "broll_city.mp4", "atSeconds": 10.0, "videoTrack": 1, "audioTrack": 1,
               "inSeconds": 1.0, "outSeconds": 3.0, "undoLabel": "t"}
        before = snapshot()
        fake.FAIL_TRANSACTIONS.add("[2/3 place]")  # range set and cleared, nothing placed: explicit not_committed
        self.fails("insert_clip", rng, "TRANSACTION_FAILED", "not_committed")
        self.assertEqual(fake.TRANSACTIONS, ["t [1/3 set range]", "t [3/3 clear range]"])
        self.assertEqual(snapshot(), before)
        fake.FAIL_TRANSACTIONS.clear()
        fake.FAIL_TRANSACTIONS.add("[3/3 clear range]")  # placed, range left set: by the rule
        self.fails("insert_clip", rng, "TRANSACTION_FAILED", "unknown")
        fake.FAIL_TRANSACTIONS.clear()
        fake.FAIL_TRANSACTIONS.add("Agent: marker")  # the side effect counts before executeTransaction
        self.fails("add_marker", {"atSeconds": 1.0, "name": "x"}, "TRANSACTION_FAILED", "unknown")

    def test_unexpected_exceptions(self):
        def boom(p):
            raise RuntimeError("boom")

        def half_done(p):
            fake.side_effect()
            raise KeyError("later")

        fake.HANDLERS["get_project"] = boom
        self.assertIn("boom", self.fails("get_project", {}, "INTERNAL")["error"])
        fake.HANDLERS["list_items"] = half_done
        self.fails("list_items", {}, "INTERNAL", "not_committed")  # reads never report unknown
        fake.HANDLERS["add_marker"] = half_done
        self.fails("add_marker", {"atSeconds": 1.0, "name": "x"}, "INTERNAL", "unknown")

    def test_reset(self):
        fake.STATE["video"][1].append({"name": "x", "start": 0.0, "end": 1.0, "inPoint": 0.0, "outPoint": 1.0})
        fake.HANDLERS["ping"] = lambda p: {}
        fake.STUCK = {"action": "x"}
        fake.FAIL_TRANSACTIONS.add("x")
        fake.reset()
        self.assertEqual(fake.STATE["video"][1], [])
        self.assertIsNone(fake.STUCK)
        self.assertEqual(fake.FAIL_TRANSACTIONS, set())
        self.assertTrue(self.ok("ping")["pong"])

    def test_ping(self):
        data = self.ok("ping", {"probe": True})
        self.assertEqual(data["host"], f"{data['hostInfo']['name']} {data['hostInfo']['version']}")
        self.assertNotIn("applicationPath", data["hostInfo"])
        self.assertEqual((data["protocol"], data["modules"], data["stuck"], data["denied"]), (2, "fake", False, False))
        self.assertIsInstance(data["loadedAt"], int)
        self.assertIsInstance(data["capabilitiesProbedAt"], int)


# ---------------------------------------------------------------- placing clips

class InsertClipTest(FakeCase):
    def test_whole_clip(self):
        data = self.ok("insert_clip", {"itemName": "broll_city.mp4", "atSeconds": 10.004, "videoTrack": 1,
                                       "audioTrack": 1})
        self.assertEqual((data["sourceRange"], data["atSeconds"]), ("whole clip", 10.004))  # echoed as sent
        self.assertEqual(data["undoLabel"], "Agent: place broll_city.mp4 @10.004s")
        clip = fake.STATE["video"][1][0]
        self.assertAlmostEqual(clip["start"], fake.snap(10.004))
        self.assertAlmostEqual(clip["end"] - clip["start"], 12.0, delta=1 / FPS)
        self.assertEqual(len(fake.STATE["audio"][1]), 1)
        self.assertEqual(fake.TRANSACTIONS, ["Agent: place broll_city.mp4 @10.004s"])

    def test_range_is_three_undo_steps(self):
        data = self.ok("insert_clip", {"itemName": "broll_city.mp4", "atSeconds": 2, "videoTrack": 2,
                                       "audioTrack": 3, "inSeconds": 1.0, "outSeconds": 3.0, "undoLabel": "b01"})
        self.assertEqual(data["sourceRange"], [1.0, 3.0])
        self.assertEqual(fake.TRANSACTIONS, ["b01 [1/3 set range]", "b01 [2/3 place]", "b01 [3/3 clear range]"])

    def test_refusals_change_nothing(self):
        before = snapshot()
        self.fails("insert_clip", {"itemName": "broll_city.mp4", "inSeconds": 3.0, "outSeconds": 3.0}, "BAD_PARAMS")
        self.fails("insert_clip", {"itemName": "broll_city.mp4", "outSeconds": 3.0}, "BAD_PARAMS")
        self.fails("insert_clip", {"itemName": "broll_city.mp4", "videoTrack": 4}, "NOT_FOUND")
        self.fails("insert_clip", {"itemName": "broll_city.mp4", "audioTrack": 9}, "NOT_FOUND")
        self.fails("insert_clip", {"itemName": "title.mov"}, "AMBIGUOUS")
        self.fails("insert_clip", {"itemName": "title.mov", "binPath": ""}, "NOT_FOUND")
        self.assertEqual(snapshot(), before)
        self.assertEqual(fake.TRANSACTIONS, [])

    def test_insert_mode_pushes_content_right(self):
        self.ok("insert_clip", {"itemName": "broll_city.mp4", "atSeconds": 10.0, "mode": "insert",
                                "inSeconds": 0.0, "outSeconds": 2.0})
        for kind in ("video", "audio"):
            with self.subTest(kind=kind):
                names = [(c["name"], round(c["start"], 2), round(c["inPoint"], 2)) for c in fake.STATE[kind][0]]
                self.assertEqual([n for n, _, _ in names], ["interview.mp4", "broll_city.mp4", "interview.mp4"])
                self.assertAlmostEqual(fake.STATE[kind][0][2]["start"], fake.STATE[kind][0][1]["end"])
                self.assertAlmostEqual(fake.STATE[kind][0][2]["inPoint"], fake.STATE[kind][0][1]["start"])
                self.assertAlmostEqual(fake.STATE[kind][0][2]["end"], 60.0 + 2.0, delta=1 / FPS)

    def test_overwrite_split_pieces_do_not_share_effects(self):
        self.ok("set_params", {"videoTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": "AE.ADBE Motion", "params": [{"index": 1, "value": 120.0}]}]})
        self.place(v=0, a=2, at=20.0, rng=(0.0, 2.0))
        left, _, right = fake.STATE["video"][0]
        self.assertIsNot(left["components"], right["components"])
        self.assertEqual(self.param(0, 5.0, "AE.ADBE Motion", 1)["value"], 120.0)
        self.assertEqual(self.param(0, 30.0, "AE.ADBE Motion", 1)["value"], 120.0)

    def test_graphics_are_silent(self):
        self.place("title.mov", rng=None)
        self.assertEqual(fake.STATE["audio"][1], [])


# ---------------------------------------------------------------- effect params

class EffectsTest(FakeCase):
    def setUp(self):
        super().setUp()
        self.clip = self.place(at=10.0, rng=(2.0, 7.0))  # broll_city.mp4 2-7 s on V2
        self.start = self.clip["start"]
        self.mid = self.start + 2.0

    def test_three_time_bases_land_on_the_same_media_time(self):
        data = self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [
            {"matchName": "AE.ADBE Opacity", "params": [
                {"index": 0, "timeBase": "media", "keyframes": [{"t": 2.5, "value": 0.0}, {"t": 3.0, "value": 100.0}]}]},
            {"matchName": "AE.ADBE Motion", "params": [
                {"index": 1, "timeBase": "clip", "keyframes": [{"t": 0.5, "value": 50.0}]},
                {"index": 4, "timeBase": "sequence", "keyframes": [{"t": self.start + 0.5, "value": 90.0}]}]}]})
        self.assertEqual([k["timeBase"] for k in data["keyframes"]], ["media", "clip", "sequence"])
        for k in data["keyframes"]:
            self.assertAlmostEqual(k["mediaSeconds"][0], 2.5, places=9)
        for match_name, index in (("AE.ADBE Opacity", 0), ("AE.ADBE Motion", 1), ("AE.ADBE Motion", 4)):
            p = self.param(1, self.mid, match_name, index)
            self.assertEqual((p["keyframed"], p["timeBase"]), (True, "media"))
            self.assertAlmostEqual(p["keyframes"][0]["t"], 2.5, places=9)
        self.assertEqual(data["set"], ["AE.ADBE Opacity[1]#0 Opacity", "AE.ADBE Motion[1]#1 Scale",
                                       "AE.ADBE Motion[1]#4 Rotation"])

    def test_everything_is_checked_before_anything_changes(self):
        self.comps(1, self.mid)  # the default chain exists once read
        before, labels = snapshot(), list(fake.TRANSACTIONS)
        for comp, code in (
                ({"matchName": "AE.ADBE Opacity", "params": [
                    {"index": 0, "timeBase": "clip", "keyframes": [{"t": 5.1, "value": 0.0}]}]}, "KEYFRAME_OUT_OF_RANGE"),
                ({"matchName": "AE.ADBE Opacity", "params": [
                    {"index": 0, "timeBase": "media", "keyframes": [{"t": 1.9, "value": 0.0}]}]}, "KEYFRAME_OUT_OF_RANGE"),
                ({"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "keyframes": [{"t": 3, "value": 0.0}]}]},
                 "BAD_PARAMS"),
                ({"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "value": "high"}]}, "BAD_PARAMS"),
                ({"matchName": "AE.ADBE Opacity", "params": [{"index": 7, "value": 1.0}]}, "NOT_FOUND"),
                ({"matchName": "AE.ADBE Nope"}, "NOT_FOUND"),
                ({"matchName": "AE.ADBE Opacity", "occurrence": 2}, "NOT_FOUND")):
            with self.subTest(code=code, comp=comp):
                blur_first = {"matchName": "AE.ADBE Gaussian Blur 2", "params": [{"index": 0, "value": 10.0}]}
                self.fails("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [blur_first, comp]},
                           code)
                self.assertEqual(fake.LAST_CTX["sideEffects"], 0)
        self.assertEqual(snapshot(), before)
        self.assertEqual(fake.TRANSACTIONS, labels)
        self.assertEqual([c["matchName"] for c in self.comps(1, self.mid)], ["AE.ADBE Opacity", "AE.ADBE Motion"])

    def test_keyframed_params_refuse_values_and_more_keyframes(self):
        kf = {"index": 0, "timeBase": "clip", "keyframes": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 100.0}]}
        self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid,
                               "components": [{"matchName": "AE.ADBE Opacity", "params": [kf]}]})
        self.fails("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [
            {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "value": 50.0}]}]}, "KEYFRAMED")
        self.fails("set_params", {"videoTrack": 1, "atSeconds": self.mid,
                                  "components": [{"matchName": "AE.ADBE Opacity", "params": [kf]}]}, "KEYFRAMED")
        self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [
            {"matchName": "AE.ADBE Motion", "params": [dict(kf, index=1)]}]})
        self.fails("set_scale", {"videoTrack": 1, "fromSeconds": self.mid, "value": 80.0}, "KEYFRAMED")
        self.fails("set_scale", {"videoTrack": 1, "fromSeconds": self.mid, "dryRun": True}, "KEYFRAMED")

    def test_occurrence(self):
        data = self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [
            {"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 2, "params": [{"index": 0, "value": 7.0}]}]})
        self.assertEqual(data["createdEffects"], ["AE.ADBE Gaussian Blur 2"] * 2)
        comps = self.comps(1, self.mid)
        self.assertEqual([(c["matchName"], c["occurrence"]) for c in comps],
                         [("AE.ADBE Opacity", 1), ("AE.ADBE Motion", 1), ("AE.ADBE Gaussian Blur 2", 1),
                          ("AE.ADBE Gaussian Blur 2", 2)])
        self.assertEqual([c["params"][0]["value"] for c in comps[2:]], [0.0, 7.0])
        data = self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [
            {"matchName": "AE.ADBE Gaussian Blur 2", "params": [{"index": 0, "value": 3.0}]}]})
        self.assertEqual(data["createdEffects"], [])
        self.assertEqual([c["params"][0]["value"] for c in self.comps(1, self.mid)[2:]], [3.0, 7.0])
        self.assertEqual(fake.TRANSACTIONS[-3:], ["Agent: set params on V2 @" + fake.js_num(self.mid) + "s [add effects]",
                                                  "Agent: set params on V2 @" + fake.js_num(self.mid) + "s",
                                                  "Agent: set params on V2 @" + fake.js_num(self.mid) + "s"])

    def test_speed_changed_clip_takes_media_times_only(self):
        self.clip["speed"] = 2.0
        comp = {"matchName": "AE.ADBE Opacity", "params": [
            {"index": 0, "timeBase": "clip", "keyframes": [{"t": 0.5, "value": 0.0}]}]}
        self.fails("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [comp]}, "UNSUPPORTED")
        comp["params"][0]["timeBase"] = "media"
        comp["params"][0]["keyframes"][0]["t"] = 2.5
        self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [comp]})

    def test_points_and_truncation(self):
        self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [
            {"matchName": "AE.ADBE Motion", "params": [{"index": 0, "value": [0.25, 0.75]}]},
            {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "timeBase": "media", "keyframes": [
                {"t": 2.0 + i / 2, "value": float(i)} for i in range(5)]}]}]})
        self.assertEqual(self.param(1, self.mid, "AE.ADBE Motion", 0)["value"], {"x": 0.25, "y": 0.75})
        clip = self.ok("inspect_items", {"videoTracks": [1], "maxKeyframes": 2})["tracks"][0]["clips"][0]
        opacity = clip["components"][0]["params"][0]
        self.assertEqual((len(opacity["keyframes"]), opacity["keyframesTruncated"]), (2, 5))

    def test_set_scale(self):
        data = self.ok("set_scale", {"videoTrack": 1, "fromSeconds": self.mid, "dryRun": True})
        self.assertIsNone(data["value"])
        self.assertEqual((data["clips"][0]["scaleBefore"], data["clips"][0]["scaleParam"]), (100.0, "Scale"))
        self.ok("set_scale", {"videoTrack": 1, "fromSeconds": self.mid, "value": 80.0})
        self.assertEqual(fake.TRANSACTIONS[-1], "Agent: scale 80% on V2")
        self.assertEqual(self.param(1, self.mid, "AE.ADBE Motion", 1)["value"], 80.0)
        self.fails("set_scale", {"videoTrack": 1, "fromSeconds": 40.0, "value": 80.0}, "NOT_FOUND")


# ---------------------------------------------------------------- transitions and OTIO

def otio_cuts(doc, track_idx):
    """(matchName, at, before, after) per transition, read the way replace.otio_transitions reads it."""
    if replace is not None:
        return [(t["matchName"], t["at"], t["before"], t["after"]) for t in replace.otio_transitions(None, track_idx, doc)]
    video = [t for t in doc["tracks"]["children"] if t["kind"] == "Video"]
    out, pos = [], 0.0
    for c in video[track_idx]["children"]:
        if c["OTIO_SCHEMA"].startswith("Transition"):
            rate = c["in_offset"]["rate"]
            out.append((c["metadata"]["PremierePro_OTIO"]["MatchName"], pos, c["in_offset"]["value"] / rate,
                        c["out_offset"]["value"] / rate))
        elif c.get("source_range"):
            pos += c["source_range"]["duration"]["value"] / c["source_range"]["duration"]["rate"]
    return out


class TransitionsTest(FakeCase):
    DISSOLVE = "AE.ADBE Cross Dissolve New"

    def setUp(self):
        super().setUp()
        self.clip = self.place("title.mov", at=10.0, rng=(0.5, 4.5))
        self.mid = self.clip["start"] + 2.0

    def add(self, start=True, duration=0.5, single=True, at=None):
        return self.ok("add_transition", {"videoTrack": 1, "atSeconds": self.mid if at is None else at,
                                          "matchName": self.DISSOLVE, "durationSeconds": duration,
                                          "applyToStart": start, "forceSingleSided": single})

    def otio(self):
        out = self.scratch / "seq.otio"
        self.ok("export_otio", {"outputPath": str(out)})
        return json.loads(out.read_text(encoding="utf-8"))

    def test_edges_in_inspect_and_otio(self):
        self.add(start=True, duration=0.5)
        self.add(start=False, duration=1.0)
        trans = self.ok("inspect_items", {"videoTracks": [1]})["tracks"][0]["transitions"]
        self.assertEqual(len(trans), 2)
        cuts = otio_cuts(self.otio(), 1)
        (m1, at1, b1, a1), (m2, at2, b2, a2) = cuts
        self.assertEqual((m1, m2), (self.DISSOLVE, self.DISSOLVE))
        self.assertAlmostEqual(at1, self.clip["start"], places=6)
        self.assertAlmostEqual(at2, self.clip["end"], places=6)
        self.assertEqual((round(b1, 9), round(a2, 9)), (0, 0))
        self.assertAlmostEqual(a1, 29 / FPS, places=6)  # 0.5 s floors to 29 frames
        self.assertAlmostEqual(b2, 59 / FPS, places=6)

    def test_duration_floors_to_frames_unless_padded(self):
        self.add(duration=(round(0.5 * FPS) + 0.1) / FPS)  # what replace and ops send
        self.assertAlmostEqual(otio_cuts(self.otio(), 1)[0][3], 30 / FPS, places=6)

    def test_double_sided_needs_a_neighbour(self):
        nxt = self.place("title.mov", at=self.clip["end"], rng=(0.5, 2.5), binPath="CN")
        self.assertAlmostEqual(nxt["start"], self.clip["end"])
        self.add(start=False, duration=1.0, single=False)
        (_, at, before, after), = otio_cuts(self.otio(), 1)
        self.assertAlmostEqual(at, self.clip["end"], places=6)
        self.assertAlmostEqual(before, after, places=6)

    def test_same_edge_replaces_and_clip_removal_takes_them(self):
        self.add()
        self.add()
        self.assertEqual(len(fake.STATE["transitions"]), 1)
        removed = self.ok("remove_items", {"videoTracks": [1], "fromSeconds": 0.0, "toSeconds": 30.0})["removed"]
        self.assertEqual([r["name"] for r in removed], ["title.mov"])
        self.assertEqual(fake.STATE["transitions"], [])
        self.assertEqual(otio_cuts(self.otio(), 1), [])

    def test_ripple_moves_later_clips_and_their_transitions(self):
        later = self.place("broll_city.mp4", at=20.0, rng=(0.0, 2.0))
        self.add(start=True, at=later["start"] + 1.0)
        length = self.clip["end"] - self.clip["start"]
        old_start = later["start"]
        data = self.ok("remove_items", {"videoTracks": [1], "fromSeconds": 9.0, "toSeconds": 15.0, "ripple": True})
        self.assertEqual(len(data["removed"]), 1)
        moved = fake.STATE["video"][1][0]
        self.assertAlmostEqual(moved["start"], old_start - length, delta=1 / FPS)
        (_, at, _, _), = otio_cuts(self.otio(), 1)
        self.assertAlmostEqual(at, moved["start"], delta=1 / FPS)

    def test_unknown_transition_and_no_clip(self):
        self.fails("add_transition", {"videoTrack": 1, "atSeconds": self.mid, "matchName": "AE.ADBE Nope",
                                      "durationSeconds": 0.5}, "NOT_FOUND")
        self.fails("add_transition", {"videoTrack": 1, "atSeconds": 50.0, "matchName": self.DISSOLVE,
                                      "durationSeconds": 0.5}, "NOT_FOUND")
        self.assertIn(self.DISSOLVE, self.ok("list_transition_names")["names"])

    def test_otio_tracks_and_positions(self):
        doc = self.otio()
        kinds = [t["kind"] for t in doc["tracks"]["children"]]
        self.assertEqual(kinds, ["Video"] * 4 + ["Audio"] * 4)
        v2 = doc["tracks"]["children"][1]["children"]
        self.assertEqual([c["OTIO_SCHEMA"] for c in v2], ["Gap.1", "Clip.2"])
        gap = v2[0]["source_range"]["duration"]
        self.assertAlmostEqual(gap["value"] / gap["rate"], self.clip["start"], places=6)
        self.assertEqual(v2[1]["media_reference"]["target_url"], "file:///fake/en/title.mov")


# ---------------------------------------------------------------- transcripts and Speech-to-Text

class TranscriptTest(FakeCase):
    def test_fake_transcripts_are_well_formed(self):
        tx = json.loads(spec.action("import_transcript")["example"]["params"]["transcriptJson"])
        for doc in (fake.FAKE_TRANSCRIPT, tx):
            self.assertEqual(fake.transcript_problems(doc), [])
            self.assertTrue(all(fake.UUID4.match(s["id"]) for s in doc["speakers"]))
            if transcript is not None and hasattr(transcript, "validate"):
                self.assertEqual(transcript.validate(doc), [])  # the CLI's own check agrees

    def test_speech_to_text_job(self):
        os.environ["PPRO_FAKE_STT_POLLS"] = "2"
        start = self.ok("transcribe_clip", {"itemName": "broll_city.mp4", "language": "en-us"})
        self.assertEqual((start["started"], start["language"], start["binPath"]), (True, "en-US", ""))
        job, = self.ok("list_transcripts", {"jobsOnly": True})["jobs"]
        self.assertEqual((job["jobId"], job["state"], job["hasTranscript"]), (start["jobId"], "running", False))
        self.fails("set_fps", {"items": [{"name": "broll_city.mp4"}], "fps": 25}, "TRANSCRIBE_RUNNING")
        self.fails("transcribe_clip", {"itemName": "broll_city.mp4"}, "TRANSCRIBE_RUNNING")
        self.fails("import_transcript", {"itemName": "broll_city.mp4",
                                         "transcriptJson": json.dumps(fake.FAKE_TRANSCRIPT)}, "TRANSCRIBE_RUNNING")
        listing = self.ok("list_transcripts")
        job = listing["jobs"][0]
        self.assertEqual((job["state"], job["hasTranscript"]), ("done", True))
        self.assertIsInstance(job["finishedAt"], int)
        self.assertIn({"name": "broll_city.mp4", "bin": "", "hasTranscript": True}, listing["clips"])
        doc = json.loads(self.ok("export_transcript", {"itemName": "broll_city.mp4"})["transcriptJson"])
        self.assertEqual((doc["language"], fake.word_count(doc)), ("en-us", 8))
        self.fails("transcribe_clip", {"itemName": "broll_city.mp4"}, "TRANSCRIPT_EXISTS")

    def test_failed_and_rejected_jobs(self):
        os.environ["PPRO_FAKE_STT_RESULT"] = "fail"
        self.assertIsNone(self.ok("transcribe_clip", {"itemName": "broll_city.mp4"})["language"])
        job, = self.ok("list_transcripts", {"jobsOnly": True})["jobs"]
        self.assertEqual((job["state"], job["hasTranscript"]), ("failed", False))
        self.assertTrue(job["error"])
        fake.reset()
        os.environ["PPRO_FAKE_STT_RESULT"] = "reject"
        self.fails("transcribe_clip", {"itemName": "broll_city.mp4"}, "TRANSCRIBE_FAILED", "not_committed")
        self.assertEqual(self.ok("list_transcripts", {"jobsOnly": True})["jobs"], [])

    def test_import_verifies_by_word_count(self):
        tx = spec.action("import_transcript")["example"]["params"]["transcriptJson"]
        data = self.ok("import_transcript", {"itemName": "broll_city.mp4", "transcriptJson": tx})
        self.assertEqual((data["method"], data["wordsIn"], data["wordsOut"], data["replaced"]), ("static", 2, 2, False))
        self.assertEqual(json.loads(self.ok("export_transcript", {"itemName": "broll_city.mp4"})["transcriptJson"]),
                         json.loads(tx))
        self.fails("import_transcript", {"itemName": "broll_city.mp4", "transcriptJson": tx}, "TRANSCRIPT_EXISTS")
        self.assertTrue(self.ok("import_transcript", {"itemName": "interview.mp4", "transcriptJson": tx,
                                                      "replace": True})["replaced"])

    def test_import_bug_modes(self):
        tx = json.dumps(fake.FAKE_TRANSCRIPT)
        os.environ["PPRO_FAKE_TRANSCRIPT_IMPORT"] = "callback"
        self.assertEqual(self.ok("import_transcript", {"itemName": "broll_city.mp4", "transcriptJson": tx})["method"],
                         "callback")
        os.environ["PPRO_FAKE_TRANSCRIPT_IMPORT"] = "empty"
        self.fails("import_transcript", {"itemName": "title.mov", "binPath": "CN", "transcriptJson": tx},
                   "TRANSCRIPT_NOT_IMPORTED", "committed")
        clips = self.ok("list_transcripts")["clips"]
        self.assertIn({"name": "title.mov", "bin": "CN", "hasTranscript": True}, clips)  # an empty shell
        self.fails("export_transcript", {"itemName": "title.mov", "binPath": "CN"}, "NO_TRANSCRIPT")

    def test_export_default_and_bins(self):
        self.assertEqual(self.ok("export_transcript")["itemName"], "interview.mp4")
        self.fails("export_transcript", {"binPath": "EN"}, "NO_TRANSCRIPT")


# ---------------------------------------------------------------- trims and clones (semantics measured on 27.1 b17)

FRAME = 1 / FPS


class TrimCloneTest(FakeCase):
    def setUp(self):
        super().setUp()
        self.clip = self.place(at=10.0, rng=(2.0, 7.0))  # broll_city.mp4 (12 s of media) 2-7 s on V2 / A2
        self.before = fake.describe(self.clip)
        self.mid = self.clip["start"] + 1.0

    def trim(self, kind="video", track=1, at=None, **fields):
        return self.ok("trim_clip", dict(kind=kind, track=track, atSeconds=self.mid if at is None else at, **fields))

    def assertSpan(self, d, start, end, in_point, out_point, delta=1e-6):
        for key, want in (("start", start), ("end", end), ("inPoint", in_point), ("outPoint", out_point)):
            self.assertAlmostEqual(d[key], want, delta=delta, msg=f"{key}: {d}")

    def test_start_and_in_point_trim_the_head(self):
        b = self.before
        after = self.trim(start=b["start"] + 1.0)["after"]
        moved = after["start"] - b["start"]
        self.assertAlmostEqual(moved, 1.0, delta=FRAME)
        self.assertSpan(after, fake.snap(b["start"] + 1.0), b["end"], b["inPoint"] + moved, b["outPoint"])
        fake.reset()
        b = fake.describe(self.place(at=10.0, rng=(2.0, 7.0)))
        after = self.trim(inPoint=3.0)["after"]  # NOT "keep start": the start follows the in point
        moved = after["inPoint"] - 2.0
        self.assertAlmostEqual(moved, 1.0, delta=FRAME / 2)
        self.assertAlmostEqual(moved * FPS, round(moved * FPS), places=6)  # whole frames
        self.assertSpan(after, b["start"] + moved, b["end"], 2.0 + moved, 7.0)

    def test_end_and_out_point_trim_the_tail(self):
        b = self.before
        after = self.trim(end=b["end"] - 2.0)["after"]
        moved = after["end"] - b["end"]
        self.assertSpan(after, b["start"], fake.snap(b["end"] - 2.0), b["inPoint"], b["outPoint"] + moved)
        prev = after
        after = self.trim(outPoint=6.0)["after"]
        self.assertAlmostEqual(after["outPoint"], 6.0, delta=FRAME / 2)
        self.assertAlmostEqual(after["end"] - prev["end"], after["outPoint"] - prev["outPoint"], places=9)
        self.assertEqual((after["start"], after["inPoint"]), (b["start"], b["inPoint"]))

    def test_move_to_is_absolute_and_lands_on_the_frame_grid(self):
        b = self.before
        data = self.trim(moveTo=60.0)
        self.assertAlmostEqual(data["after"]["start"], 59.9933, places=4)  # what 27.1 b17 did with moveTo 60
        self.assertAlmostEqual(data["after"]["end"] - data["after"]["start"], b["end"] - b["start"], places=9)
        self.assertEqual((data["after"]["inPoint"], data["after"]["outPoint"]), (b["inPoint"], b["outPoint"]))
        self.assertEqual(data["fields"], ["moveTo"])
        self.assertEqual(fake.STATE["audio"][1][0]["start"], b["start"])  # the linked audio stays

    def test_fields_apply_in_order_in_one_undo_step(self):
        b = self.before
        data = self.trim(start=b["start"] + 0.5, end=b["end"] - 0.5, undoLabel="tighten")
        self.assertEqual((data["fields"], data["undoLabel"], fake.TRANSACTIONS[-1]), (["start", "end"], "tighten", "tighten"))
        self.assertEqual(data["before"], b)
        self.assertAlmostEqual(data["after"]["end"] - data["after"]["start"], b["end"] - b["start"] - 1.0, delta=2 * FRAME)
        data = self.trim(start=b["start"] + 1.0)
        self.assertEqual(data["undoLabel"], "Agent: trim broll_city.mp4 on V2 (start)")

    def test_refusals_before_the_transaction_change_nothing(self):
        before, labels = snapshot(), list(fake.TRANSACTIONS)
        for params, code in (({"start": 11.0, "moveTo": 30.0}, "BAD_PARAMS"),
                             ({}, "BAD_PARAMS"),
                             ({"atSeconds": 40.0, "end": 41.0}, "NOT_FOUND"),
                             ({"track": 9, "end": 13.0}, "NOT_FOUND")):
            with self.subTest(params=params):
                self.fails("trim_clip", dict({"kind": "video", "track": 1, "atSeconds": self.mid}, **params), code)
                self.assertEqual(fake.LAST_CTX["sideEffects"], 0)
        self.assertEqual((snapshot(), fake.TRANSACTIONS), (before, labels))

    def test_impossible_trims_fail_like_a_false_transaction(self):
        """Unprobed, the fake's choice: Premiere's executeTransaction "returns false" (outcome unknown)."""
        self.place("broll_city.mp4", at=20.0, rng=(0.0, 2.0))  # a neighbour on V2 / A2
        b = self.before
        before = snapshot()
        for fields in ({"end": b["start"]},                   # no length left
                       {"start": b["start"] - 3.0},           # in point before the media's start
                       {"outPoint": 12.5},                    # past the 12 s of media
                       {"end": 21.0},                         # into the neighbour
                       {"moveTo": 19.0}):                     # onto the neighbour
            with self.subTest(fields=fields):
                self.fails("trim_clip", dict({"kind": "video", "track": 1, "atSeconds": self.mid}, **fields),
                           "TRANSACTION_FAILED", "unknown")
        self.assertEqual(snapshot(), before)

    def test_audio_and_video_legs_trim_separately(self):
        a = fake.describe(fake.STATE["audio"][1][0])
        after = self.trim(kind="audio", end=a["end"] - 1.0)["after"]
        self.assertLess(after["end"], a["end"] - 0.9)
        self.assertEqual(fake.describe(self.clip), self.before)  # the linked video is not trimmed with it
        self.assertEqual(self.trim(kind="audio", start=a["start"] + 0.5)["undoLabel"],
                         "Agent: trim broll_city.mp4 on A2 (start)")

    def test_clone_copies_only_that_item_with_its_effects(self):
        self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [
            {"matchName": "AE.ADBE Motion", "params": [{"index": 1, "value": 120.0}]},
            {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "timeBase": "clip", "keyframes": [
                {"t": 0.0, "value": 0.0}, {"t": 0.5, "value": 100.0}]}]},
            {"matchName": "AE.ADBE Gaussian Blur 2", "params": [{"index": 0, "value": 8.0}]}]})
        data = self.ok("clone_clip", {"kind": "video", "track": 1, "atSeconds": self.mid, "offsetSeconds": 20.0})
        b = self.before
        self.assertEqual((data["cloned"], data["kind"], data["track"]), (True, "video", 1))
        self.assertEqual((data["start"], data["end"]), (b["start"] + 20.0, b["end"] + 20.0))  # computed, like the panel
        self.assertEqual(data["undoLabel"], "Agent: clone broll_city.mp4 V2 by 20s")
        self.assertEqual(data["source"], b)
        original, copy_ = fake.STATE["video"][1]
        self.assertAlmostEqual(copy_["start"], fake.snap(b["start"] + 20.0), places=9)  # the timeline: frame grid
        self.assertEqual((copy_["inPoint"], copy_["outPoint"]), (b["inPoint"], b["outPoint"]))
        self.assertEqual(self.comps(1, copy_["start"] + 1.0), self.comps(1, self.mid))
        self.assertEqual(len(fake.STATE["audio"][1]), 1)  # linked audio is not cloned with it
        self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [
            {"matchName": "AE.ADBE Motion", "params": [{"index": 1, "value": 50.0}]}]})
        self.assertEqual(self.param(1, copy_["start"] + 1.0, "AE.ADBE Motion", 1)["value"], 120.0)  # independent

    def test_clone_overwrites_the_destination(self):
        data = self.ok("clone_clip", {"kind": "video", "track": 1, "atSeconds": self.mid, "offsetSeconds": 10.0,
                                      "trackOffset": -1, "undoLabel": "onto V1"})
        self.assertEqual((data["track"], fake.TRANSACTIONS[-1]), (0, "onto V1"))
        v1 = [(c["name"], round(c["start"], 3), round(c["end"], 3), round(c["inPoint"], 3)) for c in fake.STATE["video"][0]]
        s = fake.snap(self.before["start"] + 10.0)
        e = s + self.before["end"] - self.before["start"]
        self.assertEqual(v1, [("interview.mp4", 0.0, round(s, 3), 0.0), ("broll_city.mp4", round(s, 3), round(e, 3), 2.0),
                              ("interview.mp4", round(e, 3), 60.0, round(e, 3))])

    def test_clone_refusals_and_capabilities(self):
        before = snapshot()
        self.fails("clone_clip", {"kind": "video", "track": 1, "atSeconds": self.mid, "offsetSeconds": 20.0,
                                  "trackOffset": 5}, "NOT_FOUND")
        self.fails("clone_clip", {"kind": "audio", "track": 1, "atSeconds": 40.0, "offsetSeconds": 20.0}, "NOT_FOUND")
        os.environ["PPRO_FAKE_CAPS"] = json.dumps({"cloneTrackItem": False, "trackItemTrim": False})
        self.fails("clone_clip", {"kind": "video", "track": 1, "atSeconds": self.mid, "offsetSeconds": 20.0},
                   "UNSUPPORTED")
        self.fails("trim_clip", {"kind": "video", "track": 1, "atSeconds": self.mid, "end": 12.0}, "UNSUPPORTED")
        self.assertEqual(snapshot(), before)

    def test_split_without_a_razor(self):
        """The split_clip recipe on the fake: clone past the end, trim the original's tail, trim the clone's head,
        move it into place - two pieces with continuous source and the same attributes."""
        self.ok("set_params", {"videoTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": "AE.ADBE Motion", "params": [{"index": 1, "value": 110.0}]}]})
        at = fake.snap(20.0)
        orig = fake.describe(fake.STATE["video"][0][0])
        offset = 70.0 - orig["start"]  # sequence end (60) + 10 s margin
        clone = self.ok("clone_clip", {"kind": "video", "track": 0, "atSeconds": at, "offsetSeconds": offset})
        clone_start = next(c for c in fake.STATE["video"][0] if c["start"] > 60)["start"]
        self.trim(track=0, at=at - 1.0, end=at)
        self.trim(track=0, at=clone_start + 1.0, start=clone_start + (at - orig["start"]))
        self.trim(track=0, at=clone["start"] + (at - orig["start"]) + 1.0, moveTo=at)
        left, right = (fake.describe(c) for c in fake.STATE["video"][0])
        self.assertSpan(left, 0.0, at, 0.0, at)
        self.assertSpan(right, at, orig["end"], at, orig["outPoint"], delta=FRAME / 2)
        self.assertAlmostEqual(left["outPoint"], right["inPoint"], places=9)
        for t in (5.0, 30.0):
            self.assertEqual(self.param(0, t, "AE.ADBE Motion", 1)["value"], 110.0)


# ---------------------------------------------------------------- audio chains (provisional, measured on one mono clip)

class AudioChainTest(FakeCase):
    VOL = "Internal Volume Mono"

    def audio_tracks(self, params):
        return self.ok("inspect_items", params)["tracks"]

    def volume(self, track=0, at=5.0):
        t, = self.audio_tracks({"audioTracks": [track], "fromSeconds": at, "toSeconds": at})
        return next(c for c in t["clips"] if c["start"] <= at < c["end"])["components"]

    def test_the_measured_mono_chain(self):
        t, = self.audio_tracks({"audioTracks": [0]})
        self.assertEqual((t["kind"], t["label"], t["name"], t["transitions"]), ("audio", "A1", "Audio 1", []))
        comp, = t["clips"][0]["components"]
        self.assertEqual((comp["matchName"], comp["occurrence"], comp["index"]), (self.VOL, 1, 0))
        self.assertEqual([(p["index"], p["value"], p["keyframed"]) for p in comp["params"]],
                         [(0, False, False), (1, fake.LEVEL_0DB, False)])
        self.assertAlmostEqual(fake.level_from_db(0.0), fake.LEVEL_0DB, delta=1e-8)  # float32 rounding
        self.assertAlmostEqual(fake.level_from_db(-6.0), 0.0891251, places=7)
        self.assertAlmostEqual(fake.db_from_level(fake.LEVEL_0DB), 0.0, places=5)
        fake.STATE["audio"][2].append({"name": "music.wav", "start": 0.0, "end": 5.0, "inPoint": 0.0, "outPoint": 5.0,
                                       "channels": 2})
        self.assertEqual(self.volume(track=2, at=1.0)[0]["matchName"], "Internal Volume Stereo")  # unverified

    def test_track_selection(self):
        self.assertEqual([(t["kind"], t["label"]) for t in self.audio_tracks({})],
                         [("video", f"V{i}") for i in range(1, 5)])
        self.assertEqual([t["label"] for t in self.audio_tracks({"audioTracks": []})], ["V1", "V2", "V3", "V4"])
        self.assertEqual([t["label"] for t in self.audio_tracks({"audioTracks": [1, 0]})], ["A2", "A1"])
        self.assertEqual([t["label"] for t in self.audio_tracks({"videoTracks": [0], "audioTracks": [0]})], ["V1", "A1"])
        self.assertEqual([t["label"] for t in self.audio_tracks({"videoTracks": [], "audioTracks": [3]})], ["A4"])
        self.fails("inspect_items", {"audioTracks": [9]}, "NOT_FOUND")

    def test_set_level_and_keyframes(self):
        minus6 = fake.level_from_db(-6.0)
        data = self.ok("set_params", {"audioTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": self.VOL, "params": [{"index": 1, "value": minus6}]}]})
        self.assertEqual((data["set"], data["createdEffects"], data["undoLabel"]),
                         ([f"{self.VOL}[1]#1 Level"], [], "Agent: set params on A1 @5s"))
        self.assertEqual(self.volume()[0]["params"][1]["value"], minus6)
        self.fails("set_params", {"audioTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": self.VOL, "params": [{"index": 1, "value": "loud"}]}]}, "BAD_PARAMS")
        fake.reset()
        data = self.ok("set_params", {"audioTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": self.VOL, "params": [{"index": 0, "value": True}, {"index": 1, "timeBase": "sequence",
                                                "keyframes": [{"t": 4.0, "value": fake.LEVEL_0DB},
                                                              {"t": 4.5, "value": minus6}]}]}]})
        self.assertEqual(data["keyframes"], [{"matchName": self.VOL, "occurrence": 1, "index": 1,
                                              "timeBase": "sequence", "mediaSeconds": [4.0, 4.5]}])
        bypass, level = self.volume()[0]["params"]
        self.assertIs(bypass["value"], True)
        self.assertEqual((level["keyframed"], level["timeBase"], [k["t"] for k in level["keyframes"]]),
                         (True, "media", [4.0, 4.5]))
        self.fails("set_params", {"audioTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": self.VOL, "params": [{"index": 1, "value": minus6}]}]}, "KEYFRAMED")

    def test_refusals_change_nothing(self):
        level = {"matchName": self.VOL, "params": [{"index": 1, "value": 0.1}]}
        self.volume()  # the default chain exists once read
        before, labels = snapshot(), list(fake.TRANSACTIONS)
        for params, code in (
                ({"audioTrack": 0, "components": [{"matchName": "AE.ADBE Gaussian Blur 2",
                                                   "params": [{"index": 0, "value": 1.0}]}]}, "UNSUPPORTED"),
                ({"audioTrack": 0, "components": [level, {"matchName": "Audio.Nope"}]}, "UNSUPPORTED"),
                ({"audioTrack": 0, "components": [{"matchName": self.VOL, "occurrence": 2}]}, "UNSUPPORTED"),
                ({"audioTrack": 0, "videoTrack": 0, "components": [level]}, "BAD_PARAMS"),
                ({"components": [level]}, "BAD_PARAMS"),
                ({"audioTrack": 0, "components": [{"matchName": self.VOL, "params": [{"index": 2, "value": 1}]}]},
                 "NOT_FOUND"),
                ({"audioTrack": 1, "components": [level]}, "NOT_FOUND")):
            with self.subTest(params=params, code=code):
                self.fails("set_params", dict({"atSeconds": 5.0}, **params), code)
                self.assertEqual(fake.LAST_CTX["sideEffects"], 0)
        self.assertEqual((snapshot(), fake.TRANSACTIONS), (before, labels))

    def test_set_volume_in_db(self):
        calls = []
        out = ops.set_volume(recording_rpc(calls), track="A1", at=5.0, db=-6.0, label="Duck the interview")
        self.assertTrue(out["verified"])
        row, = out["clips"]
        self.assertEqual((row["name"], row["dbBefore"], row["dbAfter"]), ("interview.mp4", 0.0, -6.0))
        self.assertAlmostEqual(self.volume()[0]["params"][1]["value"], fake.level_from_db(-6.0), places=9)
        self.assertEqual(fake.TRANSACTIONS[-1], "Duck the interview")
        dry = ops.set_volume(recording_rpc([]), track="A1", from_s=0.0, to_s=60.0, db=3.0, dry_run=True)
        self.assertEqual([(r["dbBefore"], r["db"]) for r in dry["clips"]], [(-6.0, 3.0)])
        self.assertAlmostEqual(self.volume()[0]["params"][1]["value"], fake.level_from_db(-6.0), places=9)
        for kwargs, code in (({"track": "V1", "at": 5.0, "db": 0.0}, "BAD_PARAMS"),
                             ({"track": "A1", "at": 5.0, "db": 16.0}, "BAD_PARAMS"),
                             ({"track": "A1", "db": 0.0}, "BAD_PARAMS"),
                             ({"track": "A1", "at": 70.0, "db": 0.0}, "NOT_FOUND")):
            with self.subTest(**kwargs), self.assertRaises(errors.BridgeError) as cm:
                ops.set_volume(recording_rpc([]), **kwargs)
            self.assertEqual((cm.exception.code, cm.exception.outcome), (code, "not_committed"))
        self.ok("set_params", {"audioTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": self.VOL, "params": [{"index": 1, "timeBase": "sequence",
                                                "keyframes": [{"t": 4.0, "value": 0.1}, {"t": 4.5, "value": 0.2}]}]}]})
        labels = list(fake.TRANSACTIONS)
        with self.assertRaises(errors.BridgeError) as cm:
            ops.set_volume(recording_rpc([]), track="A1", at=5.0, db=0.0)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("KEYFRAMED", "not_committed"))
        self.assertEqual(fake.TRANSACTIONS, labels)

    def test_volume_travels_with_a_clone(self):
        minus6 = fake.level_from_db(-6.0)
        self.ok("set_params", {"audioTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": self.VOL, "params": [{"index": 1, "value": minus6}]}]})
        self.ok("clone_clip", {"kind": "audio", "track": 0, "atSeconds": 5.0, "offsetSeconds": 70.0, "trackOffset": 1})
        self.assertEqual(self.volume(track=1, at=75.0)[0]["params"][1]["value"], minus6)
        self.assertEqual(len(fake.STATE["video"][0]), 1)


# ---------------------------------------------------------------- export_sequence

class ExportSequenceTest(FakeCase):
    PRESET = "/fake/presets/Waveform Audio 48kHz 16-bit.epr"

    def test_writes_a_silent_wav_as_long_as_the_sequence(self):
        import wave
        out = self.scratch / "mix.wav"
        data = self.ok("export_sequence", {"outputPath": str(out), "presetPath": self.PRESET, "workAreaOnly": True})
        self.assertEqual(data, {"exported": True, "outputPath": str(out), "preset": self.PRESET})
        with wave.open(str(out), "rb") as w:  # the format of the live render (48 kHz stereo 16-bit), 60 s
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()),
                             (48000, 2, 2, 60 * 48000))
            self.assertEqual(set(w.readframes(100)), {0})
        self.assertEqual((fake.LAST_CTX["sideEffects"], fake.TRANSACTIONS), (0, []))  # a read: nothing changes
        os.environ["PPRO_FAKE_EXPORT_SECONDS"] = "0.25"
        self.ok("export_sequence", {"outputPath": str(out), "presetPath": self.PRESET})
        with wave.open(str(out), "rb") as w:
            self.assertEqual(w.getnframes(), 12000)

    def test_failures(self):
        out = str(self.scratch / "mix.wav")
        self.fails("export_sequence", {"outputPath": out}, "BAD_PARAMS")
        self.fails("export_sequence", {"outputPath": out, "presetPath": ""}, "BAD_PARAMS")
        self.fails("export_sequence", {"outputPath": "", "presetPath": self.PRESET}, "BAD_PARAMS")
        self.fails("export_sequence", {"outputPath": str(self.scratch / "missing" / "mix.wav"),
                                       "presetPath": self.PRESET}, "EXPORT_FAILED")
        reply = self.fails("export_sequence", {"outputPath": out, "presetPath": "/fake/no/such/preset.epr"},
                           "EXPORT_FAILED")
        self.assertIn("Unable to initialize export!", reply["error"])
        fake.STATE["sequence"] = None
        self.fails("export_sequence", {"outputPath": out, "presetPath": self.PRESET}, "NO_SEQUENCE")


# ---------------------------------------------------------------- the library against the fake

def recording_rpc(calls):
    """transport.rpc's contract on top of fake.dispatch: data, or BridgeError with code/outcome/data."""
    def rpc(action, params=None):
        calls.append(action)
        reply = fake.dispatch({"id": str(len(calls)), "action": action, "params": params or {}})
        if not reply["ok"]:
            raise errors.BridgeError(f"{action}: {reply['error']}", code=reply.get("code"), outcome=reply.get("outcome"),
                                     data=reply.get("data"))
        data = reply["data"]
        if action == "ping":
            data["brokerVersion"] = fake.VERSION
        return data
    return rpc


@unittest.skipUnless(replace is not None and errors is not None, "ppro_bridge.replace is not importable yet")
class ReplaceEndToEndTest(FakeCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.rpc = recording_rpc(self.calls)
        old = self.place("title.mov", at=10.0, rng=(0.5, 4.5))
        self.mid = old["start"] + 2.0
        self.ok("set_params", {"videoTrack": 1, "atSeconds": self.mid, "components": [
            {"matchName": "AE.ADBE Motion", "params": [{"index": 1, "value": 80.0}]},
            {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "timeBase": "clip", "keyframes": [
                {"t": 0.0, "value": 0.0}, {"t": 0.5, "value": 100.0}]}]},
            {"matchName": "AE.ADBE Gaussian Blur 2", "params": [{"index": 0, "value": 12.0}]}]})
        self.ok("add_transition", {"videoTrack": 1, "atSeconds": self.mid, "matchName": "AE.ADBE Cross Dissolve New",
                                   "durationSeconds": 0.5, "applyToStart": True})
        self.calls.clear()

    def swap(self, **kw):
        return replace.replace_clip(self.rpc, 1, self.mid, CN_TITLE,
                                    probe_media=lambda p: {"hasAudio": False, "duration": 5.0}, **kw)

    def test_replace_carries_the_editors_work(self):
        report = self.swap()
        self.assertTrue(report["verified"], report["problems"])
        self.assertLessEqual({"AE.ADBE Opacity#0", "AE.ADBE Motion#1", "AE.ADBE Gaussian Blur 2#0"},
                             set(report["restoredParams"]))
        self.assertEqual([t["applyToStart"] for t in report["transitions"]], [True])
        clip = self.ok("inspect_items", {"videoTracks": [1]})["tracks"][0]
        self.assertEqual(clip["clips"][0]["mediaPath"], "/fake/cn/title.mov")
        self.assertEqual(len(clip["transitions"]), 1)
        opacity = self.param(1, self.mid, "AE.ADBE Opacity", 0)
        self.assertEqual([round(k["t"], 6) for k in opacity["keyframes"]], [0.5, 1.0])
        self.assertLessEqual(set(self.calls), set(spec.command("replace_clip")["calls"]))

    def test_dry_run_writes_nothing(self):
        before, labels = snapshot(), list(fake.TRANSACTIONS)
        report = self.swap(dry_run=True)
        self.assertTrue(report["dryRun"])
        self.assertEqual((snapshot(), fake.TRANSACTIONS), (before, labels))

    def test_failure_after_the_removal_is_partial(self):
        fake.FAIL_TRANSACTIONS.add("restore effect params")
        with self.assertRaises(errors.BridgeError) as cm:
            self.swap()
        self.assertEqual(cm.exception.outcome, "partial")
        self.assertEqual(cm.exception.data["completed"][0], "remove title.mov")


@unittest.skipUnless(replace is not None and ops is not None and errors is not None, "ppro_bridge.ops is not importable yet")
class SwapSafetyTest(FakeCase):
    """What a real language-version swap ran into (Premiere 27.1, 19 Oct 2026): a colour-tool container that cannot
    be re-added, a neighbour's outgoing-only fade at the clip's start, add_transition on an edit point that already
    has a transition, and an out point a hair under a frame boundary. V2 holds a name super A (10-12 s) and the
    banner B right after it (12-16 s, source 0.5-4.5 s)."""
    FILM = "AE.ADBE Film Dissolve"
    SILENT = staticmethod(lambda p: {"hasAudio": False, "duration": 5.0})

    def setUp(self):
        super().setUp()
        self.calls = []
        self.rpc = recording_rpc(self.calls)
        self.a = self.place("title.mov", at=10.0, rng=(0.0, 2.0))
        self.b = self.place("title.mov", at=self.a["end"], rng=(0.5, 4.5))
        self.mid = (self.b["start"] + self.b["end"]) / 2

    def fade(self, clip, start, frames=25, single=True):
        self.ok("add_transition", {"videoTrack": 1, "atSeconds": (clip["start"] + clip["end"]) / 2,
                                   "matchName": self.FILM, "durationSeconds": (frames + 0.1) / FPS,
                                   "applyToStart": start, "forceSingleSided": single})

    def cuts(self):
        """(matchName, cut frame, frames before the cut, frames after it) per transition on V2, from OTIO."""
        out = self.scratch / f"cuts{len(self.calls)}.otio"
        self.ok("export_otio", {"outputPath": str(out)})
        return [(m, round(at * FPS), round(b * FPS), round(a * FPS))
                for m, at, b, a in otio_cuts(json.loads(out.read_text(encoding="utf-8")), 1)]

    def swap(self, **kw):
        return replace.replace_clip(self.rpc, 1, self.mid, CN_TITLE, probe_media=self.SILENT, **kw)

    def refused(self, **kw):
        for clip in fake.STATE["video"][1]:
            fake.chain(clip)  # built on first read; reading is not a change
        before, labels = snapshot(), list(fake.TRANSACTIONS)
        with self.assertRaises(errors.BridgeError) as cm:
            self.swap(**kw)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("UNSUPPORTED", "not_committed"), cm.exception)
        self.assertEqual((snapshot(), fake.TRANSACTIONS), (before, labels))
        return cm.exception

    # bug 2: transitions belong to a clip and a side
    def test_the_previous_clips_outgoing_fade_is_not_the_clips_start_transition(self):
        self.fade(self.a, start=False)  # A fades out, ending exactly at the cut into B
        want = self.cuts()
        self.assertEqual(want, [(self.FILM, round(self.b["start"] * FPS), 25, 0)])
        plan = self.swap(dry_run=True)
        self.assertEqual(plan["transitions"], [])
        self.assertEqual([(t["owner"], t["matchName"], t["cut"]) for t in plan["neighbourTransitions"]],
                         [("previous", self.FILM, "start")])
        report = self.swap()
        self.assertTrue(report["verified"], report["problems"])
        self.assertEqual(self.cuts(), want)  # removing B took A's fade with it; it is back exactly
        self.assertEqual([t["status"] for t in report["neighbourTransitions"]], ["restored"])
        self.assertEqual([c["mediaPath"] for c in fake.STATE["video"][1]], ["/fake/en/title.mov", "/fake/cn/title.mov"])

    def test_the_next_clips_incoming_fade_is_left_alone(self):
        c = self.place("title.mov", at=self.b["end"], rng=(0.0, 1.0), binPath="CN")
        self.fade(c, start=True, frames=12)
        want = self.cuts()
        self.assertEqual(want, [(self.FILM, round(c["start"] * FPS), 0, 12)])
        report = self.swap()
        self.assertTrue(report["verified"], report["problems"])
        self.assertEqual((report["transitions"], [t["owner"] for t in report["neighbourTransitions"]]), ([], ["next"]))
        self.assertEqual(self.cuts(), want)

    def test_own_and_shared_transitions_keep_their_sides(self):
        self.place("title.mov", at=self.b["end"], rng=(0.0, 1.0), binPath="CN")
        self.fade(self.b, start=True, frames=10)                 # B's own fade-in
        self.fade(self.b, start=False, frames=20, single=False)  # a dissolve across the cut into C
        want = self.cuts()
        self.assertEqual([(b, a) for _, _, b, a in want], [(0, 10), (10, 10)])
        report = self.swap()
        self.assertTrue(report["verified"], report["problems"])
        self.assertEqual([t["owner"] for t in report["transitions"]], ["clip", "shared"])
        self.assertEqual(self.cuts(), want)

    def test_a_shared_transition_the_new_media_cannot_cover_is_refused(self):
        self.fade(self.a, start=False, frames=20, single=False)  # across the A|B cut: needs 10 frames of B's head
        e = self.refused(dry_run=True, src_in=0.0, src_out=4.0)
        self.assertIn("handle", str(e))
        self.assertTrue(self.swap(dry_run=True)["transitions"])  # B's own range (in 0.5 s) has the handle

    def test_an_off_centre_transition_across_the_cut_is_refused(self):
        cut = self.b["start"]
        fake.STATE["transitions"].append({"videoTrack": 1, "matchName": self.FILM, "name": "Film Dissolve",
                                          "start": cut - 5 / FPS, "end": cut + 15 / FPS, "cut": cut})
        e = self.refused(dry_run=True)
        self.assertIn("centre", str(e))

    # bug 1: components that cannot be re-created on the new clip
    def add_component(self, match_name, params=()):
        fake.chain(self.b).append({"matchName": match_name, "displayName": match_name.split(".")[-1],
                                   "params": [{"name": n, "value": v, "keyframed": False, "keyframes": []}
                                              for n, v in params]})

    def test_a_colour_tool_container_is_refused_before_anything_is_written(self):
        self.add_component("AE.ADBE CompoundComponent")
        for kw in ({"dry_run": True}, {}):
            e = self.refused(**kw)
            self.assertEqual([x["matchName"] for x in e.data["effects"]], ["AE.ADBE CompoundComponent"])
            self.assertIn("--skip-effect", e.hint)

    def test_skip_effect_swaps_without_it_and_says_so(self):
        self.add_component("AE.ADBE CompoundComponent")
        self.add_component("AE.ADBE Gaussian Blur 2", [("Blurriness", 7.0), ("Blur Dimensions", 1),
                                                      ("Repeat Edge Pixels", False)])
        report = self.swap(skip_effects=["AE.ADBE CompoundComponent"])
        self.assertTrue(report["verified"], report["problems"])
        self.assertEqual([x["matchName"] for x in report["skippedEffects"]], ["AE.ADBE CompoundComponent"])
        names = [c["matchName"] for c in self.comps(1, self.mid)]
        self.assertIn("AE.ADBE Gaussian Blur 2", names)
        self.assertNotIn("AE.ADBE CompoundComponent", names)

    def test_an_unknown_component_the_panel_cannot_create_is_found_by_its_dry_run(self):
        self.add_component("AE.ADBE Mystery Glow", [("Amount", 3.0)])
        e = self.refused(dry_run=True)
        self.assertEqual([x["matchName"] for x in e.data["effects"]], ["AE.ADBE Mystery Glow"])
        self.assertIn("set_params", self.calls)  # asked the panel (dryRun), which wrote nothing

    # bug 3: add_transition reads back
    def test_add_transition_refuses_an_edit_point_that_already_has_one(self):
        self.fade(self.a, start=False)
        before, labels = snapshot(), list(fake.TRANSACTIONS)
        with self.assertRaises(errors.BridgeError) as cm:
            ops.add_transition(self.rpc, track="V2", at=self.mid, match_name=self.FILM, duration=0.5, edge="start")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("TRANSITION_EXISTS", "not_committed"))
        self.assertEqual(cm.exception.data["existing"]["matchName"], self.FILM)
        self.assertEqual((snapshot(), fake.TRANSACTIONS), (before, labels))

    def test_add_transition_reports_what_landed(self):
        data = ops.add_transition(self.rpc, track="V2", at=self.mid, match_name=self.FILM, duration=0.5, edge="end")
        self.assertTrue(data["verified"])
        t = data["transition"]
        self.assertEqual((t["matchName"], round(t["before"] * FPS), round(t["after"] * FPS)), (self.FILM, 30, 0))
        self.assertAlmostEqual(t["at"], self.b["end"], places=6)

    def test_the_panel_keeps_a_transition_already_at_the_edit_point(self):
        self.fade(self.a, start=False)
        want = self.cuts()
        data = self.ok("add_transition", {"videoTrack": 1, "atSeconds": self.mid, "matchName": "AE.ADBE Dip To Black",
                                          "durationSeconds": 0.5, "applyToStart": True})
        self.assertFalse(data["added"])
        self.assertEqual(data["transitionCount"], {"before": 1, "after": 1})
        self.assertEqual(self.cuts(), want)

    # bug 4: source in/out on the source frame grid
    def test_an_out_point_a_hair_under_a_frame_lands_on_that_frame(self):
        os.environ["PPRO_FAKE_SOURCE_FLOOR"] = "1"  # Premiere floors source points to the item's frames
        report = self.swap(src_in=0.0, src_out=273 / FPS - 5e-6)
        self.assertTrue(report["verified"], report["problems"])
        new = fake.STATE["video"][1][1]
        self.assertEqual((round(new["inPoint"] * FPS, 6), round(new["outPoint"] * FPS, 6)), (0, 273))
        self.assertEqual(round((new["end"] - new["start"]) * FPS, 6), 273)
        self.assertEqual(report["sourceSnap"]["out"][1], round(273 / FPS, 6))


@unittest.skipUnless(ops is not None and errors is not None, "ppro_bridge.ops is not importable yet")
class OpsCallsTest(FakeCase):
    """Each CLI command, run through ops against the fake, calls only the actions its spec command lists (+ ping)."""

    def cases(self):
        s = self.scratch
        tx_file = s / "tx.json"
        tx_file.write_bytes(json.dumps(fake.FAKE_TRANSCRIPT).encode("utf-8"))
        media_file = s / "media" / "b01.mp4"
        media_file.parent.mkdir()
        media_file.write_bytes(b"synthetic")
        plan_file = s / "plan.json"
        plan_file.write_bytes(json.dumps({"sequence": "Sequence 01", "media_root": "./media/", "commands": [
            {"command": "insert_clip", "id": "b01", "track": "V3", "timeline_at": 2.0, "src": "b01.mp4",
             "src_in": 1.0, "src_out": 3.0, "scale": 50},
            {"command": "note", "id": "flag1", "timeline_at": 9.0, "note": "Check this claim"}]}).encode("utf-8"))
        params_file = s / "params.json"
        params_file.write_bytes(json.dumps([{"matchName": "AE.ADBE Opacity", "params": [
            {"index": 0, "timeBase": "clip", "keyframes": [{"t": 0, "value": 0}, {"t": 0.5, "value": 100}]}]}])
                                .encode("utf-8"))
        srt_file = s / "captions.srt"
        srt_file.write_bytes("\n".join([
            "1", "00:00:00,000 --> 00:00:01,500", "One", "",
            "2", "00:00:01,500 --> 00:00:03,000", "Two", "",
            "3", "00:00:03,000 --> 00:00:04,500", "Three", ""]).encode("utf-8"))
        preset_file = s / "wav.epr"
        preset_file.write_bytes(b"<preset/>")
        probe = lambda p: {"hasAudio": True, "duration": 10.0, "width": 1920, "height": 1080, "fps": 59.94}  # noqa: E731
        silent = lambda p: {"hasAudio": False, "duration": 5.0}  # noqa: E731

        def place_title():
            self.place("title.mov", at=10.0, rng=(0.5, 4.5))

        return {
            "ping": ({}, None),
            "get_project": ({}, None),
            "get_sequence": ({}, None),
            "list_items": ({"track": "V1"}, None),
            "inspect_items": ({"track": ["V1"], "from_s": 0.0, "to_s": 10.0}, None),
            "list_project_items": ({}, None),
            "export_frame": ({"dir": str(s / "f"), "at": 1.5}, None),
            "export_frames": ({"dir": str(s / "fs"), "at": "1,2.5"}, None),
            "export_otio": ({"output": str(s / "o" / "seq.otio")}, None),
            "list_transcripts": ({"jobs_only": True}, None),
            "list_transition_names": ({}, None),
            "export_transcript": ({"output": str(s / "tr.json"), "item": "interview.mp4"}, None),
            "import_transcript": ({"item": "broll_city.mp4", "file": str(tx_file)}, None),
            "transcribe_clip": ({"item": "broll_city.mp4", "language": "en-US", "wait": 30, "poll": 0.01,
                                 "sleep": lambda seconds: None, "output": str(s / "stt.json")}, None),
            "import_media": ({"path": [str(media_file)], "bin": "Plan A"}, None),
            "add_marker": ({"at": 3.0, "name": "Fact check", "color": "red"}, None),
            "insert_clip": ({"item": "broll_city.mp4", "at": 10.0, "vtrack": 1, "atrack": 1, "in_s": 1.0,
                             "out_s": 3.0, "label": "b-roll"}, None),
            "set_scale": ({"track": "V1", "at": 5.0, "value": 110.0}, None),
            "set_fps": ({"item": ["CN/title.mov"], "fps": 59.94}, None),
            "remove_items": ({"track": ["V1"], "from_s": 0.0, "to_s": 60.0, "dry_run": True}, None),
            "apply_plan": ({"plan": str(plan_file), "probe_media": probe, "notes_as_markers": True}, None),
            "replace_clip": ({"track": "V2", "at": fake.snap(10.0) + 2.0, "with_item": "CN/title.mov",
                              "probe_media": silent}, place_title),
            "set_params": ({"track": "V1", "at": 5.0, "json_file": str(params_file)}, None),
            "add_transition": ({"track": "V1", "at": 5.0, "match_name": "AE.ADBE Cross Dissolve New",
                                "duration": 0.5, "edge": "start"}, None),
            "set_volume": ({"track": "A1", "at": 5.0, "db": -6.0}, None),
            "split_clip": ({"track": "V1", "at": 20.0}, None),
            "cut_range": ({"from_s": 10.0, "to_s": 15.0}, None),
            "snap_edges": ({}, None),
            "extend_cut": ({"at": 20.0, "in_by": 0.25}, lambda: ops.split_clip(recording_rpc([]), track="V1", at=20.0)),
            "conform_captions": ({"track": "V2", "srt": str(srt_file)}, place_title),
            "review_cuts": ({"out_dir": str(s / "review"), "render": True, "preset": str(preset_file)}, None),
            "lint_timeline": ({}, None),
            "probe_api": ({"output": str(s / "api.json")}, None),
            "export_audio": ({"output": str(s / "mix.wav"), "render": True, "preset": str(preset_file)}, None),
        }

    def test_every_command_calls_only_its_spec_actions(self):
        cases = self.cases()
        self.assertEqual(sorted(cases), sorted(spec.command_names()))
        for name, (kwargs, setup) in cases.items():
            with self.subTest(command=name):
                fake.reset()
                if setup:
                    setup()
                calls = []
                result = getattr(ops, name)(recording_rpc(calls), **kwargs)
                self.assertIsInstance(result, dict)
                allowed = set(spec.command(name)["calls"]) | {"ping"}
                self.assertLessEqual(set(calls), allowed, f"{name} called {sorted(set(calls) - allowed)}")
                self.assertTrue(calls)

    def test_transcribe_clip_start_poll_export(self):
        os.environ["PPRO_FAKE_STT_POLLS"] = "3"
        calls, sleeps = [], []
        out = self.scratch / "stt.json"
        result = ops.transcribe_clip(recording_rpc(calls), item="broll_city.mp4", language="en-US", wait=60,
                                     poll=0.5, sleep=sleeps.append, output=str(out))
        self.assertEqual((result["state"], result["hasTranscript"]), ("done", True))
        self.assertEqual(calls.count("list_transcripts"), 3)
        self.assertEqual(sleeps, [0.5, 0.5, 0.5])
        self.assertEqual(fake.word_count(json.loads(out.read_text(encoding="utf-8"))), 8)
        self.assertLessEqual(set(calls), set(spec.command("transcribe_clip")["calls"]) | {"ping"})

    def test_transcribe_clip_timeout_and_failure(self):
        os.environ["PPRO_FAKE_STT_POLLS"] = "100"
        with self.assertRaises(errors.BridgeError) as cm:
            ops.transcribe_clip(recording_rpc([]), item="broll_city.mp4", wait=2, poll=1, sleep=lambda s: None)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("TRANSCRIBE_TIMEOUT", "unknown"))
        fake.reset()
        os.environ.update(PPRO_FAKE_STT_POLLS="1", PPRO_FAKE_STT_RESULT="fail")
        with self.assertRaises(errors.BridgeError) as cm:
            ops.transcribe_clip(recording_rpc([]), item="broll_city.mp4", wait=10, poll=1, sleep=lambda s: None)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("TRANSCRIBE_FAILED", "unknown"))


if __name__ == "__main__":
    unittest.main()
