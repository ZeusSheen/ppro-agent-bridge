"""Effect presets (.prfpset): parsing (tests/fixtures/effect_presets.prfpset, and Premiere's own Factory and Lumetri
presets when they are installed), the translation to set_params (anchoring, keys outside the clip, interpolation,
name checks), list_effect_presets and apply_preset against the fake panel."""

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import BridgeError, fake_rpc

from ppro_bridge import media, ops, prfpset

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "effect_presets.prfpset"


def localized_presets() -> Path:
    """LocalizedPresets of this machine's Premiere Pro, for the tests that read its real Factory and Lumetri presets
    (they skip without them): PPRO_TEST_LOCALIZED_PRESETS, else next to a running Premiere (the Beta first), else a
    Beta under Program Files."""
    if os.environ.get("PPRO_TEST_LOCALIZED_PRESETS"):
        return Path(os.environ["PPRO_TEST_LOCALIZED_PRESETS"])
    for exe in sorted(media.premiere_executables(), key=lambda p: "(Beta)" not in p):
        if (Path(exe).parent / "LocalizedPresets").is_dir():
            return Path(exe).parent / "LocalizedPresets"
    return Path(os.environ.get("ProgramFiles") or os.sep) / "Adobe" / "Adobe Premiere Pro (Beta)" / "LocalizedPresets"


LOCALIZED = localized_presets()
FACTORY_ZH = LOCALIZED / "zh_CN" / "Effect Presets" / "Factory Presets.prfpset"
FACTORY_EN = LOCALIZED / "en_US" / "Effect Presets" / "Factory Presets.prfpset"
LUMETRI_ZH = LOCALIZED / "zh_CN" / "Effect Presets" / "Lumetri Presets.prfpset"
T = prfpset.TICKS_PER_SECOND
FPS = 60000 / 1001


def snap(t):
    return round(t * FPS) / FPS


def by_name(presets, name, bin_=None):
    hits = [p for p in presets if p["name"] == name and (bin_ is None or p["bin"] == bin_)]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def dead_rpc(action, params=None):
    raise BridgeError("No broker", code="NO_BROKER", outcome="not_committed")


class ParseFixtureTest(unittest.TestCase):
    def setUp(self):
        self.presets = prfpset.read_file(FIXTURE, "path")

    def test_bins_names_and_both_schemas(self):
        self.assertEqual([(p["bin"], p["name"]) for p in self.presets],
                         [("Presets/Blurs", "Blur In"), ("Presets/Blurs", "Blur Out"),
                          ("Presets/Blurs", "Masked Blur"), ("Presets/Blurs", "Audio Gain"),
                          ("Presets/Moves", "Slide Scale"), ("Presets/Moves", "Tinted"),
                          ("Presets/Moves", "Old Transform"), ("Presets/Moves", "Blur In")])
        blur_in = by_name(self.presets, "Blur In", "Presets/Blurs")  # <FilterPreset ObjectRef>
        self.assertEqual(blur_in["description"], "Blur 40 to 0")
        self.assertEqual([e["matchName"] for e in blur_in["effects"]], ["AE.ADBE Gaussian Blur 2"])

    def test_filter_presets_are_stored_in_reverse_effect_controls_order(self):
        self.assertEqual([e["matchName"] for e in by_name(self.presets, "Slide Scale")["effects"]],
                         ["AE.ADBE Motion", "AE.ADBE Crop"])  # stored Crop, Motion
        self.assertEqual([e["matchName"] for e in by_name(self.presets, "Tinted")["effects"]],
                         ["AE.ADBE Opacity", "AE.ADBE Tint"])  # the intrinsic Opacity is stored last

    def test_params_by_index_with_start_keyframe_values(self):
        params = by_name(self.presets, "Blur In", "Presets/Blurs")["effects"][0]["params"]
        # <Params> order, not <Node><Children> (which lists them 2, 0, 3, 1 here)
        self.assertEqual([(p["index"], p["name"], p["kind"]) for p in params],
                         [(0, "Blurriness", "number"), (1, "Blur Dimensions", "integer"),
                          (2, "Repeat Edge Pixels", "boolean"), (3, None, "group")])
        self.assertTrue(params[2]["value"])  # StartKeyframe true, CurrentValue a stale false
        self.assertEqual(params[0]["keys"], [[3600 * T, 40.0, 0], [3601 * T, 0.0, 0]])
        motion = by_name(self.presets, "Slide Scale")["effects"][0]
        self.assertEqual(motion["params"][1]["value"], 50.0)  # CurrentValue 0
        self.assertEqual(motion["params"][0]["keys"][1], [0, [0.25, 0.5], 5])  # a point, a bezier key
        self.assertEqual(motion["params"][0]["keys"][0][0], -T)  # before the anchor in point: kept

    def test_anchoring_types_masks_audio_arb_and_colours(self):
        anchors = {p["name"]: p["effects"][0]["anchor"] for p in self.presets if p["bin"] == "Presets/Blurs"}
        self.assertEqual((anchors["Blur In"], anchors["Blur Out"], anchors["Masked Blur"]), ("in", "out", "scale"))
        self.assertEqual(by_name(self.presets, "Masked Blur")["effects"][0]["masks"], 1)
        self.assertEqual(by_name(self.presets, "Audio Gain")["effects"][0]["kind"], "audio")
        self.assertEqual(by_name(self.presets, "Blur Out")["effects"][0]["params"][3]["kind"], "arb")
        tint = by_name(self.presets, "Tinted")["effects"][1]
        self.assertEqual([prfpset.colour_hex(p["value"]) for p in tint["params"][:2]], ["#000000 ff", "#ffc080 ff"])

    def test_summary(self):
        rows = {(p["bin"], p["name"]): prfpset.summary(p) for p in self.presets}
        self.assertEqual(rows[("Presets/Blurs", "Blur In")]["replay"], "full")
        self.assertEqual((rows[("Presets/Blurs", "Blur In")]["animated"], rows[("Presets/Blurs", "Blur In")]["anchor"]),
                         (True, "in"))
        self.assertEqual(rows[("Presets/Blurs", "Blur Out")]["replay"], "partial")
        self.assertEqual(rows[("Presets/Blurs", "Blur Out")]["reasons"],
                         ["AE.ADBE Gaussian Blur 2: 1 arbitrary-data (LUT, curve) param(s) skipped"])
        self.assertEqual(rows[("Presets/Blurs", "Masked Blur")]["replay"], "none")
        self.assertEqual(rows[("Presets/Blurs", "Audio Gain")]["replay"], "none")
        self.assertIn("colour", rows[("Presets/Moves", "Tinted")]["reasons"][0])
        self.assertEqual((rows[("Presets/Moves", "Tinted")]["animated"], rows[("Presets/Moves", "Tinted")]["anchor"]),
                         (False, None))
        # against this Premiere's effects: Geometry is gone, Geometry2 replaced it
        old = by_name(self.presets, "Old Transform")
        self.assertEqual(prfpset.summary(old, {"AE.ADBE Crop"})["replay"], "none")
        self.assertEqual(prfpset.summary(old, {"AE.ADBE Geometry2"})["replay"], "full")
        self.assertEqual(prfpset.effect_target(old["effects"][0], {"AE.ADBE Geometry2"}), ("AE.ADBE Geometry2", None))

    def test_resolve(self):
        with self.assertRaises(BridgeError) as cm:
            prfpset.resolve(self.presets, "Blur In")
        self.assertEqual(cm.exception.code, "AMBIGUOUS")
        self.assertEqual([m["bin"] for m in cm.exception.data["matches"]], ["Presets/Blurs", "Presets/Moves"])
        for query in ("Presets/Blurs/Blur In", "blurs/blur in"):
            self.assertEqual(prfpset.resolve(self.presets, query)[0]["bin"], "Presets/Blurs")
        self.assertEqual(prfpset.resolve(self.presets, "slide scale")[0]["name"], "Slide Scale")
        copy = [dict(p, file="other.prfpset") for p in self.presets]  # two Premiere versions: identical copies
        preset, notes = prfpset.resolve(self.presets + copy, "Tinted")
        self.assertEqual((preset["file"], len(notes)), (self.presets[0]["file"], 1))
        with self.assertRaises(BridgeError) as cm:
            prfpset.resolve(self.presets, "Slide Scal")
        self.assertEqual((cm.exception.code, cm.exception.data["closeNames"][0]), ("NOT_FOUND", "Slide Scale"))

    def test_refuses_what_is_not_a_preset_file(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        bad = d / "x.prfpset"
        for text in ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><PremiereData/>', "<Project/>", "not xml"):
            bad.write_text(text, encoding="utf-8")
            with self.assertRaises(BridgeError) as cm:
                prfpset.read_file(bad)
            self.assertEqual(cm.exception.code, "BAD_PARAMS")


class KeyframeMathTest(unittest.TestCase):
    def effect(self, typ, a_in=3600.0, a_out=3601.0):
        return {"type": typ, "anchorIn": round(a_in * T), "anchorOut": round(a_out * T)}

    def test_anchoring(self):
        keys = [[round(3600 * T), 1.0, 0], [round(3601 * T), 2.0, 0]]
        scale = prfpset.clip_times(self.effect(0), keys, 4.0)
        self.assertEqual([round(t, 6) for t, _, _ in scale], [0.0, 4.0])  # stretched to the clip
        start = prfpset.clip_times(self.effect(1), keys, 4.0)
        self.assertEqual([round(t, 6) for t, _, _ in start], [0.0, 1.0])  # offsets from the in point
        end = prfpset.clip_times(self.effect(2), keys, 4.0)
        self.assertEqual([round(t, 6) for t, _, _ in end], [3.0, 4.0])  # offsets from the out point
        early = prfpset.clip_times(self.effect(1), [[round(3599.5 * T), 0.0, 0]], 4.0)
        self.assertEqual(round(early[0][0], 6), -0.5)  # signed, never clamped

    def test_keys_outside_the_clip_become_edge_keys(self):
        keys = [(-1.0, 0.0, 0), (1.0, 20.0, 0), (3.0, 40.0, 4), (5.0, 80.0, 0)]
        inside, outside = prfpset.fit_keys("number", keys, 4.0, 0.008)
        self.assertEqual(outside, [-1.0, 5.0])
        self.assertEqual([(t, v) for t, v, _ in inside], [(0.0, 10.0), (1.0, 20.0), (3.0, 40.0), (4.0, 40.0)])  # hold
        pts = prfpset.fit_keys("point", [(-2.0, [0.0, 0.5], 0), (2.0, [1.0, 0.5], 0)], 1.0, 0.008)[0]
        self.assertEqual([v for _, v, _ in pts], [[0.5, 0.5], [0.75, 0.5]])
        near = prfpset.fit_keys("number", [(-0.004, 1.0, 0), (4.004, 2.0, 0)], 4.0, 0.008)
        self.assertEqual(([t for t, _, _ in near[0]], near[1]), ([0.0, 4.0], []))  # within half a frame: on the edge

    def test_interpolation(self):
        self.assertEqual(prfpset.interpolation_for([(0, 1, 0), (1, 2, 0)]), ("linear", None))
        # the last key's mode shapes no segment
        self.assertEqual(prfpset.interpolation_for([(0, 1, 4), (1, 2, 0)]), ("hold", None))
        self.assertEqual(prfpset.interpolation_for([(0, 1, 5), (1, 2, 5)])[0], 3)  # bezier -> the eased mode
        mode, note = prfpset.interpolation_for([(0, 1, 5), (1, 2, 5), (2, 3, 0), (3, 1, 0)])
        self.assertEqual(mode, 3)
        self.assertIn("mixed", note)

    def test_param_request(self):
        effect = dict(self.effect(1), params=[])
        param = {"index": 0, "kind": "number", "value": 0.0, "keys": [[round(3600 * T), 40.0, 0],
                                                                     [round(3601 * T), 0.0, 0]]}
        req, info = prfpset.param_request(effect, param, 0.5, 0.008)
        self.assertEqual(req, {"index": 0, "timeBase": "clip", "interpolation": "linear",
                               "keyframes": [{"t": 0.0, "value": 40.0}, {"t": 0.5, "value": 20.0}]})
        self.assertEqual(info, {"keysOutsideClip": [1.0]})
        late = dict(self.effect(1), params=[])
        param["keys"] = [[round(3599 * T), 5.0, 0], [round(3599.5 * T), 5.0, 0]]  # all before the clip
        req, info = prfpset.param_request(late, param, 2.0, 0.008)
        self.assertEqual(req, {"index": 0, "value": 5.0})
        self.assertIn("held", info)


class NameCheckTest(unittest.TestCase):
    @staticmethod
    def params(*names):
        return [{"index": i, "name": n} for i, n in enumerate(names)]

    def test_renamed_param_is_tolerated(self):
        preset = self.params("位置", "缩放", "缩放宽度", " ", "旋转", "定位点")  # Factory Presets, saved long ago
        live = dict(enumerate(["位置", "缩放", "缩放宽度", " ", "旋转", "锚点", "防闪烁滤镜", "裁剪左侧"]))
        why, notes = prfpset.compare_names(preset, live, "AE.ADBE Motion")
        self.assertIsNone(why)
        self.assertIn("renamed", notes[0])

    def test_moved_params_are_refused(self):
        preset = self.params("Blob", " ", "基本校正", " ", " ", " ", "输入 LUT", "HDR 白色", "白平衡")
        live = dict(enumerate(["Blob", " ", "基本校正", " ", " ", " ", "输入 LUT", "输入 LUT", "HDR 白色", " "]))
        why, _ = prfpset.compare_names(preset, live, "AE.ADBE Lumetri")  # 27.1 inserted a second LUT param
        self.assertIn("changed", why)

    def test_another_language(self):
        preset = self.params("Position", "Scale", "Scale Width", "", "Rotation", "Anchor Point")
        live = dict(enumerate(["位置", "缩放", "缩放宽度", " ", "旋转", "锚点", "防闪烁滤镜"]))
        self.assertIsNone(prfpset.compare_names(preset, live, "AE.ADBE Motion")[0])  # a fixed order: by index
        why, _ = prfpset.compare_names(self.params("Blurriness", "Blur Dimensions"), {0: "模糊度", 1: "模糊尺寸"},
                                       "AE.ADBE Fast Blur")
        self.assertIn("another language", why)
        self.assertEqual(prfpset.compare_names(self.params("A", "B"), {}, "X"), (None, []))  # nothing to compare

    def test_set_names(self):
        entries = ["AE.ADBE Gaussian Blur 2[2]#0 Blurriness", "AE.ADBE Gaussian Blur 2[2]#3  ",
                   "AE.ADBE Gaussian Blur 2[1]#1 Other", "AE.ADBE Crop[1]#0 Left", "AE.ADBE Gaussian Blur 2[2]#2"]
        self.assertEqual(prfpset.set_names(entries, "AE.ADBE Gaussian Blur 2", 2), {0: "Blurriness", 3: " "})


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, rel):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE, p)
        return p

    def test_user_files_take_the_newest_version_of_each_app(self):
        self.write("Adobe/Premiere Pro/25.0/Profile-x/" + prfpset.USER_FILE)
        new = self.write("Adobe/Premiere Pro/26.0/Profile-x/" + prfpset.USER_FILE)
        beta = self.write("Adobe/Premiere Pro (Beta)/27.0/Profile-x/" + prfpset.USER_FILE)
        (self.root / "Adobe/Premiere Pro/27.0").mkdir(parents=True)  # a newer folder without the file
        with mock.patch.object(prfpset, "documents_dir", return_value=self.root):
            self.assertEqual(prfpset.user_files(), [str(new), str(beta)])

    def test_bundled_presets_next_to_the_running_premiere_in_its_locale(self):
        zh = self.write("Premiere/LocalizedPresets/zh_CN/Effect Presets/Factory Presets.prfpset")
        self.write("Premiere/LocalizedPresets/en_US/Effect Presets/Factory Presets.prfpset")
        exe = str(self.root / "Premiere" / "Adobe Premiere Pro.exe")
        with mock.patch.object(prfpset.media, "premiere_executables", return_value=[exe]), \
                mock.patch.dict(os.environ, {prfpset.PRESET_DIR_ENV: ""}):
            found = prfpset.discover("zh_CN", user=[])
            self.assertEqual([f["path"] for f in found["files"]], [str(zh)])
            fallback = prfpset.discover("fr_FR", user=[])
        self.assertIn("en_US", fallback["files"][0]["path"])
        self.assertIn("fr_FR", fallback["notes"][0])

    def test_env_folder_and_no_premiere(self):
        self.write("presets/a.prfpset")
        with mock.patch.object(prfpset.media, "premiere_executables", return_value=[]), \
                mock.patch.dict(os.environ, {prfpset.PRESET_DIR_ENV: str(self.root / "presets")}):
            found = prfpset.discover("en_US", user=[])
        self.assertEqual([f["source"] for f in found["files"]], ["bundled"])
        with mock.patch.object(prfpset.media, "premiere_executables", return_value=[]), \
                mock.patch.dict(os.environ, {prfpset.PRESET_DIR_ENV: ""}):
            self.assertIn("No running Premiere", prfpset.discover("en_US", user=[])["notes"][0])


@unittest.skipUnless(FACTORY_ZH.is_file() and FACTORY_EN.is_file(), "Premiere's Factory Presets are not installed")
class RealFactoryPresetsTest(unittest.TestCase):
    """The facts this module relies on, on the files Premiere 27.0 beta ships."""

    def test_factory_presets(self):
        zh, en = prfpset.read_file(FACTORY_ZH), prfpset.read_file(FACTORY_EN)
        self.assertEqual((len(zh), len(en)), (54, 54))
        types = [p["effects"][0]["type"] for p in zh]
        self.assertEqual((types.count(0), types.count(1), types.count(2)), (28, 14, 12))
        self.assertTrue(all(len(p["effects"]) == 1 for p in zh))
        bevel = by_name(zh, "厚斜角边")["effects"][0]
        self.assertEqual([p["name"] for p in bevel["params"]], ["边缘厚度", "照明角度", "照明颜色", "照明强度"])
        self.assertEqual(bevel["params"][1]["value"], -45.0)  # StartKeyframe; CurrentValue says 0
        self.assertEqual(prfpset.colour_hex(bevel["params"][2]["value"]), "#ffffff ff")
        move = by_name(zh, "画中画 25% LL 至 LR")["effects"][0]
        self.assertEqual((move["matchName"], move["anchor"], move["anchorIn"]),
                         ("AE.ADBE Motion", "scale", 914456685542400))  # 1 h (a still's in point), less 3.6 ms
        self.assertEqual([k[1] for k in move["params"][0]["keys"]], [[0.274305, 0.752083], [0.729167, 0.752083]])
        self.assertEqual(by_name(en, "PiP 25% LL to LR")["effects"][0]["params"][0]["keys"],
                         move["params"][0]["keys"])  # localised names, same data
        self.assertEqual(by_name(zh, "快速模糊入点")["effects"][0]["anchor"], "in")
        self.assertEqual(by_name(zh, "画中画 25% UL 旋转出点右侧")["effects"][0]["anchor"], "out")
        self.assertEqual(prfpset.summary(by_name(zh, "厚斜角边"))["replay"], "partial")  # its light colour
        self.assertEqual(prfpset.summary(by_name(zh, "画中画 25% LL 缩放入点"))["replay"], "full")

    @unittest.skipUnless(LUMETRI_ZH.is_file(), "Lumetri Presets are not installed")
    def test_lumetri_presets(self):
        lumetri = prfpset.read_file(LUMETRI_ZH)
        self.assertEqual(len(lumetri), 325)
        params = lumetri[0]["effects"][0]["params"]
        self.assertEqual((len(params), sum(p["kind"] == "arb" for p in params)), (98, 14))
        self.assertTrue(all(prfpset.summary(p)["replay"] == "partial" for p in lumetri))

    def test_apply_against_the_fake(self):
        """A Chinese Motion preset on the fake's English Motion: written by index (a fixed order); Fast Blur is not
        among the fake's effects."""
        rpc, fake, _ = fake_rpc()
        out = ops.apply_preset(rpc, track="V1", at=5.0, preset="画中画 25% LL 至 LR", file=str(FACTORY_ZH))
        self.assertTrue(out["verified"])
        motion = next(c for c in fake.STATE["video"][0][0]["components"] if c["matchName"] == "AE.ADBE Motion")
        self.assertEqual([(k["t"], k["value"]) for k in motion["params"][0]["keyframes"]],
                         [(0.0, {"x": 0.274305, "y": 0.752083}), (60.0, {"x": 0.729167, "y": 0.752083})])
        self.assertEqual(motion["params"][1]["value"], 25.0)
        with self.assertRaises(BridgeError) as cm:
            ops.apply_preset(rpc, track="V1", at=5.0, preset="快速模糊入点", file=str(FACTORY_ZH))
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("UNSUPPORTED", "not_committed"))
        self.assertIn("does not offer", cm.exception.data["skipped"][0]["reason"])


class ApplyPresetTest(unittest.TestCase):
    def setUp(self):
        self.rpc, self.fake, self.calls = fake_rpc()
        self.fake.STATE["video"][1] = [{"name": n, "start": snap(a), "end": snap(b), "inPoint": 3600.0,
                                        "outPoint": 3600.0 + snap(b) - snap(a)}
                                       for n, a, b in (("A", 0, 0.5), ("B", 0.5, 4.5), ("C", 4.5, 10))]

    def apply(self, preset, **kw):
        kw.setdefault("track", "V2")
        return ops.apply_preset(self.rpc, preset=preset, file=str(FIXTURE), **kw)

    def writes(self):
        return [p for a, p in self.calls if a == "set_params" and not p.get("dryRun")]

    def chain(self, name):
        clip = next(c for c in self.fake.STATE["video"][1] if c["name"] == name)
        return {(c["matchName"], i): c for i, c in enumerate(clip.get("components") or [])}

    def test_dry_run_writes_nothing_and_shows_the_requests(self):
        out = self.apply("Presets/Blurs/Blur In", at=1.0, dry_run=True)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.fake.TRANSACTIONS, [])
        clip = out["clips"][0]
        self.assertEqual(clip["name"], "B")
        self.assertEqual(clip["request"]["components"], [{"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 1,
                                                          "params": [
            {"index": 0, "timeBase": "clip", "keyframes": [{"t": 0.0, "value": 40.0}, {"t": 1.0, "value": 0.0}],
             "interpolation": "linear"},
            {"index": 1, "value": 1}, {"index": 2, "value": True}]}])
        self.assertEqual((clip["createdEffects"], clip["undoSteps"], out["replay"]),
                         (["AE.ADBE Gaussian Blur 2"], 3, "full"))
        self.assertEqual({a for a, _ in self.calls},
                         {"ping", "get_sequence", "list_effects", "inspect_items", "set_params"})

    def test_range_writes_each_clip_and_reads_them_back(self):
        out = self.apply("Presets/Blurs/Blur In", from_s=0.0, to_s=snap(4.5), label="Blur in")
        self.assertEqual([c["name"] for c in out["clips"]], ["A", "B"])  # C only touches the range's end
        self.assertTrue(out["verified"])
        a = out["clips"][0]
        self.assertEqual(a["adjusted"], [{"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 1, "index": 0,
                                          "keysOutsideClip": [1.0]}])
        blur = self.chain("A")[("AE.ADBE Gaussian Blur 2", 2)]["params"][0]
        self.assertEqual([round(k["value"], 2) for k in blur["keyframes"]], [40.0, 19.98])  # the curve at A's end
        self.assertEqual(out["undoSteps"], 6)
        self.assertEqual(self.fake.TRANSACTIONS.count("Blur in [add effects]"), 2)
        self.assertNotIn("AE.ADBE Gaussian Blur 2", [k[0] for k in self.chain("C")])
        overlap = self.apply("Presets/Moves/Blur In", from_s=4.0, to_s=6.0, mode="overlap")
        self.assertEqual([c["name"] for c in overlap["clips"]], ["B", "C"])
        self.assertEqual(overlap["clips"][0]["createdEffects"], ["AE.ADBE Gaussian Blur 2"])
        self.assertEqual(len([k for k in self.chain("B") if k[0] == "AE.ADBE Gaussian Blur 2"]), 2)  # a new instance

    def test_intrinsics_new_instances_points_bezier_and_anchors(self):
        out = self.apply("Slide Scale", at=6.0)
        comps = self.writes()[0]["components"]
        self.assertEqual([c["matchName"] for c in comps], ["AE.ADBE Motion", "AE.ADBE Crop"])  # Effect Controls order
        pos = comps[0]["params"][0]
        dur = snap(10) - snap(4.5)
        self.assertEqual((pos["interpolation"], [k["t"] for k in pos["keyframes"]]), (3, [0.0, round(dur, 6)]))
        self.assertEqual(pos["keyframes"][0]["value"], [0.25, 0.5])
        self.assertEqual(out["clips"][0]["adjusted"][0]["keysOutsideClip"], [round(-dur / 2, 6)])
        self.assertTrue(any("eased" in n for n in out["notes"]))
        self.assertTrue(out["verified"])
        out_type = self.apply("Blur Out", at=6.0)
        keys = self.writes()[-1]["components"][0]["params"][0]["keyframes"]
        self.assertEqual([k["t"] for k in keys], [round(dur - 0.5, 6), round(dur, 6)])  # from the out point
        self.assertEqual(out_type["replay"], "partial")
        self.assertEqual(out_type["effects"][0]["skippedParams"][0]["index"], 3)  # the arbitrary-data param

    def test_partial_effects_and_refusals(self):
        out = self.apply("Tinted", at=6.0, dry_run=True)
        comps = out["clips"][0]["request"]["components"]
        self.assertEqual(comps, [{"matchName": "AE.ADBE Opacity", "occurrence": 1,
                                  "params": [{"index": 0, "value": 80.0}, {"index": 1, "value": 0}]},
                                 {"matchName": "AE.ADBE Tint", "occurrence": 1,
                                  "params": [{"index": 2, "value": 50.0}]}])
        self.assertEqual(out["replay"], "partial")
        for name, why in (("Masked Blur", "mask"), ("Audio Gain", "audio"), ("Old Transform", "does not offer")):
            with self.assertRaises(BridgeError) as cm:
                self.apply(name, at=6.0)
            self.assertEqual((cm.exception.code, cm.exception.outcome), ("UNSUPPORTED", "not_committed"), name)
            self.assertIn(why, cm.exception.data["skipped"][0]["reason"])
        with self.assertRaises(BridgeError) as cm:
            self.apply("Blur In", at=6.0)
        self.assertEqual(cm.exception.code, "AMBIGUOUS")
        self.assertEqual(self.writes(), [])

    def test_keyframed_motion_and_speed_are_refused_before_writing(self):
        self.apply("Slide Scale", at=6.0)
        n = len(self.writes())
        with self.assertRaises(BridgeError) as cm:
            self.apply("Slide Scale", at=6.0)
        self.assertEqual(cm.exception.code, "KEYFRAMED")
        self.assertIn("remove_keyframes --track V2", cm.exception.hint)
        self.fake.STATE["video"][1][1]["speed"] = 2.0
        with self.assertRaises(BridgeError) as cm:
            self.apply("Presets/Blurs/Blur In", at=1.0)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("UNSUPPORTED", "not_committed"))
        self.assertEqual(self.apply("Tinted", at=1.0)["replay"], "partial")  # a static preset is fine at 2x
        self.assertEqual(len(self.writes()), n + 1)

    def test_live_check_skips_effects_whose_params_do_not_fit(self):
        doc = self.rpc("inspect_items", {"videoTracks": [1], "fromSeconds": 6.0, "toSeconds": 6.0})
        clip = doc["tracks"][0]["clips"][0]

        def item(pos, *params):
            ps = [{"index": i, "name": n, "kind": "number", "value": v, "keys": []} for i, (n, v) in enumerate(params)]
            effect = {"matchName": "AE.ADBE Gaussian Blur 2", "params": ps, "anchor": "scale"}
            return {"position": pos, "matchName": effect["matchName"], "effect": effect, "intrinsic": False,
                    "params": ps, "skippedParams": [], "ignoredParams": 0}

        plan = [item(0, ("Blurriness", 1.0), ("Blur Dimensions", 1), ("Repeat Edge Pixels", True), ("Extra", 1.0)),
                item(1, ("模糊度", 1.0), ("模糊尺寸", 1)), item(2, ("Blurriness", 3.0))]
        skips, _, unchecked = prfpset.check_live(self.rpc, 1, clip, plan)
        self.assertEqual([s["position"] for s in skips], [0, 1])
        self.assertIn("refused", skips[0]["reason"])  # the fake's Gaussian Blur has 3 params, not 4
        self.assertIn("another language", skips[1]["reason"])
        self.assertEqual((unchecked, self.writes()), ([], []))  # the fake names a new effect's params in a dry run

    def unnamed(self):
        """27.1: a set_params dry run names no param of an effect that is not on the clip yet (2 Nov, live)."""
        rpc = self.rpc

        def wrapped(action, params=None):
            reply = rpc(action, params)
            if action == "set_params" and (params or {}).get("dryRun"):
                reply["set"] = [re.sub(r"(#\d+) .*$", r"\1", s, flags=re.S) for s in reply.get("set") or []]
            return reply

        return wrapped

    def test_added_effects_are_checked_on_the_clip_when_a_dry_run_cannot_name_them(self):
        rpc = self.unnamed()
        dry = ops.apply_preset(rpc, track="V2", at=6.0, preset="Slide Scale", file=str(FIXTURE), dry_run=True)
        self.assertEqual([(e["matchName"], e["paramsChecked"]) for e in dry["effects"]],
                         [("AE.ADBE Motion", True), ("AE.ADBE Crop", False)])
        self.assertTrue(any("a real run adds AE.ADBE Crop" in n for n in dry["notes"]))
        self.assertEqual(self.writes(), [])
        out = ops.apply_preset(rpc, track="V2", at=6.0, preset="Slide Scale", file=str(FIXTURE))
        self.assertEqual(out["checkedOnClip"], {"added": ["AE.ADBE Crop"], "removed": [], "undoSteps": 1})
        self.assertEqual((out["clips"][0]["createdEffects"], out["verified"], out["undoSteps"]), ([], True, 3))
        self.assertEqual([k[0] for k in self.chain("C")].count("AE.ADBE Crop"), 1)  # added once, then written

    def test_a_refusal_after_the_check_changed_the_first_clip_is_partial(self):
        """QA F-5a: the second round of dry runs runs after effects were added to the first clip."""
        rpc, state = self.unnamed(), {"added": False}

        def switched(action, params=None):
            params = params or {}
            if action == "set_params" and params.get("dryRun") and state["added"]:
                raise BridgeError("set_params: another sequence is active", code="SEQUENCE_MISMATCH",
                                  outcome="not_committed")
            reply = rpc(action, params)
            state["added"] = state["added"] or (action == "set_params" and not params.get("dryRun"))
            return reply

        with self.assertRaises(BridgeError) as cm:
            ops.apply_preset(switched, track="V2", at=6.0, preset="Slide Scale", file=str(FIXTURE))
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("SEQUENCE_MISMATCH", "partial"))
        self.assertEqual(cm.exception.data["completed"], ["added AE.ADBE Crop to the first clip to check them"])

    def test_a_misfit_found_after_adding_is_taken_off_again(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        text = FIXTURE.read_text(encoding="utf-8")  # Slide Scale's Crop with its first two param names swapped
        text = text.replace("<Name>Left</Name>", "<Name>@</Name>", 1)
        text = text.replace("<Name>Top</Name>", "<Name>Left</Name>", 1)
        moved = d / "moved.prfpset"
        moved.write_text(text.replace("<Name>@</Name>", "<Name>Top</Name>", 1), encoding="utf-8")
        out = ops.apply_preset(self.unnamed(), track="V2", at=6.0, preset="Slide Scale", file=str(moved))
        self.assertIn("params changed", out["skipped"][0]["reason"])  # "Top" is this Crop's #1
        self.assertEqual(out["checkedOnClip"], {"added": ["AE.ADBE Crop"], "removed": ["AE.ADBE Crop[1]"],
                                                "undoSteps": 2})
        self.assertEqual((out["replay"], out["verified"], [e["matchName"] for e in out["effects"]]),
                         ("partial", True, ["AE.ADBE Motion"]))
        self.assertNotIn("AE.ADBE Crop", [k[0] for k in self.chain("C")])
        self.assertIn("Agent: preset Slide Scale on V2 [take off AE.ADBE Crop]", self.fake.TRANSACTIONS)
        # nothing left to write: added, checked, taken off again, and refused as committed
        with self.assertRaises(BridgeError) as cm:
            ops.apply_preset(self.unnamed(), track="V2", at=1.0, preset="Presets/Blurs/Blur In", file=str(FIXTURE))
        err = cm.exception
        self.assertEqual((err.code, err.outcome), ("UNSUPPORTED", "committed"))
        self.assertIn("4 params", err.data["skipped"][0]["reason"])  # its group header #3: the fake's has 3
        self.assertEqual(err.data["checkedOnClip"]["removed"], ["AE.ADBE Gaussian Blur 2[1]"])
        self.assertNotIn("AE.ADBE Gaussian Blur 2", [k[0] for k in self.chain("B")])  # the clip is as it was

    def test_a_failure_on_the_second_clip_is_partial(self):
        rpc = self.rpc
        writes = []

        def flaky(action, params=None):
            if action == "set_params" and not (params or {}).get("dryRun"):
                writes.append(params)
                if len(writes) == 2:
                    raise BridgeError("set_params: boom", code="TRANSACTION_FAILED", outcome="not_committed")
            return rpc(action, params)

        with self.assertRaises(BridgeError) as cm:
            ops.apply_preset(flaky, track="V2", from_s=0.0, to_s=snap(4.5), preset="Tinted", file=str(FIXTURE))
        self.assertEqual((cm.exception.outcome, cm.exception.data["completed"]), ("partial", ["A at 0.0s"]))

    def test_read_back_mismatch_is_verify_failed(self):
        rpc = self.rpc
        state = {"written": False}

        def tampering(action, params=None):
            reply = rpc(action, params)
            if action == "set_params" and not (params or {}).get("dryRun"):
                state["written"] = True
            elif action == "inspect_items" and state["written"]:
                for t in reply["tracks"]:
                    for c in t["clips"]:
                        c["components"][0]["params"][0]["value"] = 12.0  # Opacity
            return reply

        with self.assertRaises(BridgeError) as cm:
            ops.apply_preset(tampering, track="V2", at=6.0, preset="Tinted", file=str(FIXTURE))
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))
        self.assertIn("AE.ADBE Opacity[1]#0", cm.exception.data["clips"][0]["mismatches"][0])

    def test_bad_params_before_anything_is_sent(self):
        calls = []

        def rpc(action, params=None):
            calls.append(action)
            raise AssertionError("nothing may be sent")

        for kw in ({"track": "A1", "at": 1.0}, {"track": "V2", "at": 1.0, "from_s": 0.0, "to_s": 2.0},
                   {"track": "V2"}, {"track": "V2", "from_s": 2.0, "to_s": 1.0},
                   {"track": "V2", "at": 1.0, "mode": "near"}, {"track": "V2", "at": 1.0, "locale": "chinese"},
                   {"track": "V2", "at": 1.0, "file": "missing.prfpset"},
                   {"track": "V2", "at": 1.0, "file": r"\\host\share\x.prfpset"}):
            with self.subTest(kw=kw):
                with self.assertRaises(BridgeError) as cm:
                    ops.apply_preset(rpc, preset="Tinted", **kw)
                self.assertIn(cm.exception.code, ("BAD_PARAMS", "FILE_NOT_FOUND"))
        self.assertEqual(calls, [])


class ListEffectPresetsTest(unittest.TestCase):
    def test_with_the_fake_panel(self):
        rpc, _, calls = fake_rpc()
        out = ops.list_effect_presets(rpc, file=str(FIXTURE))
        self.assertEqual((out["count"], out["premiere"]["checked"], out["locale"]), (8, True, "en_US"))
        rows = {(r["bin"], r["name"]): r for r in out["presets"]}
        self.assertEqual(rows[("Presets/Moves", "Old Transform")]["replay"], "none")  # the fake has no Transform
        self.assertEqual({r["fileIndex"] for r in out["presets"]}, {0})  # an index into `files`, not the path
        self.assertEqual(out["replay"], {"full": 3, "partial": 2, "none": 3})
        self.assertEqual([a for a, _ in calls], ["ping", "list_effects"])
        found = ops.list_effect_presets(rpc, file=str(FIXTURE), search="blur", locale="zh-cn")
        self.assertEqual(([r["name"] for r in found["presets"]], found["locale"]),
                         (["Blur In", "Blur Out", "Masked Blur", "Audio Gain", "Blur In"], "zh_CN"))

    def test_without_the_bridge(self):
        out = ops.list_effect_presets(dead_rpc, file=str(FIXTURE))
        self.assertEqual((out["count"], out["premiere"]["checked"]), (8, False))
        self.assertIn("NO_BROKER", out["premiere"]["why"])
        self.assertTrue(any("not checked" in n for n in out["notes"]))
        self.assertEqual(json.loads(json.dumps(out)), out)


if __name__ == "__main__":
    unittest.main()
