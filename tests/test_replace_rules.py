"""replace_clip: same-named items, audio routing, keyframe times, speed-changed clips, source frames, components
the new clip cannot get."""

import json
import unittest
from pathlib import Path

from helpers import BridgeError

from ppro_bridge import items, replace

OLD = {"name": "g01.mov", "start": 10.0, "end": 15.0, "inPoint": 2.0, "outPoint": 7.0, "speed": 1,
       "components": [{"matchName": "AE.ADBE Opacity", "params": [
           {"index": 0, "value": 100.0, "keyframed": True,
            "keyframes": [{"t": 2.0, "value": 0.0}, {"t": 2.5, "value": 100.0}]}]}]}


def stub(audio_tracks, old=OLD):
    """Enough of a panel for a dry run: one clip on V2, the given audio tracks (lists of [start, end]), no
    transitions in its OTIO export."""
    def rpc(action, params=None):
        if action == "get_sequence":
            return {"name": "Seq", "timebase": "4237833600", "fps": 59.94}
        if action == "inspect_items":
            return {"tracks": [{"index": 1, "clips": [old], "transitions": []}]}
        if action == "export_otio":
            doc = {"tracks": {"children": [{"kind": "Video", "children": []}, {"kind": "Video", "children": []}]}}
            Path(params["outputPath"]).write_bytes(json.dumps(doc).encode("utf-8"))
            return {"exported": True, "outputPath": params["outputPath"]}
        if action == "list_items":
            return {"audio": [{"index": i, "label": f"A{i + 1}",
                               "items": [{"name": "x", "start": s, "end": e} for s, e in spans]}
                              for i, spans in enumerate(audio_tracks)],
                    "video": [{"index": 1, "label": "V2", "items": [old]}]}
        raise AssertionError(f"dry run must not call {action}")
    return rpc


NEW = {"name": "g01_cn.mov", "bin": "CN", "mediaPath": "/fake/cn/g01_cn.mov"}


class ResolveTest(unittest.TestCase):
    ITEMS = [{"name": "title.mov", "bin": "EN", "mediaPath": "/en/title.mov"},
             {"name": "title.mov", "bin": "CN", "mediaPath": "/cn/title.mov"},
             {"name": "same.mov", "bin": "A", "mediaPath": "/x/same.mov"},
             {"name": "same.mov", "bin": "B", "mediaPath": "/x/same.mov"}]

    def test_same_name_different_media_is_ambiguous(self):
        with self.assertRaises(BridgeError) as cm:
            items.resolve_item("title.mov", self.ITEMS)
        self.assertEqual(cm.exception.code, "AMBIGUOUS")
        self.assertIn("CN/title.mov", str(cm.exception))

    def test_bin_path_picks_one(self):
        self.assertEqual(items.resolve_item("CN/title.mov", self.ITEMS)["mediaPath"], "/cn/title.mov")

    def test_same_media_is_not_ambiguous_for_placing(self):
        self.assertEqual(items.resolve_item("same.mov", self.ITEMS)["bin"], "A")

    def test_but_is_for_interpretation(self):
        with self.assertRaises(BridgeError):
            items.resolve_item("same.mov", self.ITEMS, unique=True)


class AudioRouteTest(unittest.TestCase):
    def dry(self, audio_tracks, has_audio=True, atrack=None):
        return replace.replace_clip(stub(audio_tracks), 1, 12.0, NEW, dry_run=True, keep_transitions=False,
                                    atrack=atrack, probe_media=lambda p: {"hasAudio": has_audio, "duration": 30.0})

    def test_highest_free_track(self):
        report = self.dry([[(0, 60)], [], [], [(0, 60)]])  # dialogue on A1, music on A4
        self.assertTrue(report["audioRoute"].startswith("A3"), report["audioRoute"])

    def test_refused_when_nothing_is_free(self):
        with self.assertRaises(BridgeError) as cm:
            self.dry([[(0, 60)], [(9, 16)], [(14, 20)], [(0, 60)]])
        self.assertEqual(cm.exception.code, "NO_FREE_AUDIO_TRACK")

    def test_explicit_track_wins(self):
        report = self.dry([[(0, 60)]], atrack=0)
        self.assertEqual(report["audioRoute"], "A1 (--atrack)")

    def test_silent_media_needs_no_track(self):
        report = self.dry([[(0, 60)]], has_audio=False)
        self.assertTrue(report["audioRoute"].startswith("none"))


class KeyframeTest(unittest.TestCase):
    NEW_COMPS = [{"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "value": 100.0, "keyframed": False}]}]

    def test_keyframes_move_with_the_in_point(self):
        diffs, missing = replace._param_diffs(OLD["components"], self.NEW_COMPS, delta=-2.0)
        self.assertFalse(missing)
        times = [k["t"] for k in diffs[0]["params"][0]["keyframes"]]
        self.assertEqual(times, [0.0, 0.5])

    def test_mismatch_is_reported(self):
        got = [{"matchName": "AE.ADBE Opacity", "params": [
            {"index": 0, "value": 0.0, "keyframed": True,
             "keyframes": [{"t": 2.0, "value": 0.0}, {"t": 2.5, "value": 100.0}]}]}]
        bad, _ = replace._keyframe_mismatches(OLD["components"], got, delta=-2.0, tol=0.008)
        self.assertEqual(bad, ["AE.ADBE Opacity#0"])
        ok, _ = replace._keyframe_mismatches(OLD["components"], got, delta=0.0, tol=0.008)
        self.assertEqual(ok, [])


class SpeedTest(unittest.TestCase):
    def test_speed_changed_clip_is_refused(self):
        fast = dict(OLD, speed=2.0)
        with self.assertRaises(BridgeError) as cm:
            replace.replace_clip(stub([[]], old=fast), 1, 12.0, NEW, dry_run=True, keep_transitions=False,
                                 probe_media=lambda p: {"hasAudio": False})
        self.assertEqual(cm.exception.code, "UNSUPPORTED")


class SourceSnapTest(unittest.TestCase):
    """Premiere floors a source in/out point to the item's frame: 4.554545 s (5 us under frame 273 at 59.94) would
    become frame 272. The swap snaps both to the nearest source frame first, like sequence times."""
    FRAME = 1001 / 60000

    def dry(self, info, **kw):
        return replace.replace_clip(stub([[]]), 1, 12.0, NEW, dry_run=True, keep_transitions=False,
                                    keep_effects=False, probe_media=lambda p: dict(info, hasAudio=False), **kw)

    def test_a_hair_under_a_frame_snaps_to_that_frame(self):
        report = self.dry({"duration": 30.0, "fps": 59.94}, src_in=0.0, src_out=273 * self.FRAME - 5e-6)
        snap = report["sourceSnap"]
        self.assertEqual((snap["grid"], snap["out"]), ("sequence", [round(273 * self.FRAME - 5e-6, 6), 4.55455]))
        self.assertNotIn("in", snap)  # already on a frame

    def test_the_old_clips_own_range_is_left_as_premiere_read_it(self):
        report = self.dry({"duration": 30.0, "fps": 60.0})  # OLD: source 2-7 s, on its item's (60 fps) frames
        self.assertNotIn("sourceSnap", report)
        self.assertEqual(report["source"], [2.0, 7.0])

    def test_media_at_another_rate_snaps_to_its_own_frames(self):
        report = self.dry({"duration": 30.0, "fps": 25.0}, src_in=1.013, src_out=3.0399)
        snap = report["sourceSnap"]
        self.assertEqual((snap["grid"], snap["fps"], snap["in"][1], snap["out"][1]), ("media", 25.0, 1.0, 3.04))

    def test_unknown_rate_uses_the_sequence_frames_and_says_so(self):
        report = self.dry({}, src_in=0.0, src_out=1.0)
        self.assertEqual(report["sourceSnap"]["grid"], "sequence")
        self.assertTrue(any("frame rate" in n for n in report["notes"]), report["notes"])


class UnaddableEffectTest(unittest.TestCase):
    """A component on the old clip that cannot be created on the new one is found while planning."""
    COMPOUND = {"matchName": "AE.ADBE CompoundComponent", "occurrence": 1, "displayName": "Color mode", "params": []}
    BLUR = {"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 1, "params": [{"index": 0, "value": 4.0}]}

    def old_panel(self, comps, calls):
        """A panel from before set_params took dryRun: its spec check refuses the unknown param."""
        base = stub([[]], old=dict(OLD, components=OLD["components"] + comps))

        def rpc(action, params=None):
            calls.append((action, params))
            if action == "set_params":
                raise BridgeError("set_params: params.dryRun: unknown param", code="BAD_PARAMS", outcome="not_committed")
            return base(action, params)
        return rpc

    def dry(self, comps, **kw):
        calls = []
        report = replace.replace_clip(self.old_panel(comps, calls), 1, 12.0, NEW, dry_run=True, keep_transitions=False,
                                      probe_media=lambda p: {"hasAudio": False, "duration": 30.0}, **kw)
        return report, calls

    def test_known_container_is_refused_without_asking_the_panel(self):
        with self.assertRaises(BridgeError) as cm:
            self.dry([self.COMPOUND])
        e = cm.exception
        self.assertEqual((e.code, e.outcome), ("UNSUPPORTED", "not_committed"))
        self.assertIn("colour", e.data["effects"][0]["reason"])

    def test_an_older_panel_cannot_check_the_rest_and_the_report_says_so(self):
        report, calls = self.dry([self.BLUR])
        self.assertEqual([p.get("dryRun") for a, p in calls if a == "set_params"], [True])
        self.assertTrue(any("Reload" in n for n in report["notes"]), report["notes"])
        self.assertEqual(report["readdEffects"], ["AE.ADBE Gaussian Blur 2"])

    def test_skip_effect_lists_what_is_left_behind(self):
        report, _ = self.dry([self.COMPOUND], skip_effects=["AE.ADBE CompoundComponent"])
        self.assertEqual([(x["matchName"], x["occurrence"]) for x in report["skippedEffects"]],
                         [("AE.ADBE CompoundComponent", 1)])


if __name__ == "__main__":
    unittest.main()
