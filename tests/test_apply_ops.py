"""apply_ops and query, the whitelisted generic layer: the spec's tagged variants (Python and the panel's mirror), the
fake panel and the real panel (node harness, Premiere mocks) side by side, the library / CLI functions and the MCP
confirm gate. Nothing here connects anywhere: the fake runs in-process, the panel in node.

    python -m unittest discover -s tests -p "test_apply_ops.py"
"""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import contextlib
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import REPO, fake_rpc, stub_rpc

for _p in (REPO, REPO / "broker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ppro_bridge import cli, ops, spec  # noqa: E402
from ppro_bridge.errors import BridgeError  # noqa: E402
from ppro_bridge.mcp import tools  # noqa: E402

import fake_panel as fake  # noqa: E402

HARNESS = REPO / "tests" / "panel" / "harness.js"
NODE = shutil.which("node")
APPLY = spec.action("apply_ops")
VARIANTS = APPLY["params"]["ops"]["items"]["variants"]
QUERY = spec.action("query")
LABEL = "Agent: test ops"


def harness(cmd, req):
    p = subprocess.run([NODE, str(HARNESS), cmd], input=json.dumps(req), capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"harness {cmd} exited {p.returncode}: {p.stdout}\n{p.stderr}")
    out = json.loads(p.stdout)
    assert out.get("ok") is True, out
    assert "violations" not in out, out["violations"]
    return out


def panel_batch(steps, **extra):
    return harness("batch", dict(extra, steps=[{"action": a, "params": p} for a, p in steps]))


class FakeCase(unittest.TestCase):
    def setUp(self):
        fake.reset()
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        for key in [k for k in os.environ if k.startswith("PPRO_FAKE_")]:
            del os.environ[key]
        self.addCleanup(fake.reset)

    def call(self, action, params):
        return fake.dispatch({"id": "t", "action": action, "params": params})

    def ok(self, action, params):
        reply = self.call(action, params)
        self.assertTrue(reply["ok"], reply)
        return reply["data"]

    def fails(self, action, params, code, outcome="not_committed"):
        reply = self.call(action, params)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual((reply["code"], reply["outcome"]), (code, outcome), reply["error"])
        return reply

    def apply(self, ops_list, **extra):
        return self.ok("apply_ops", dict(extra, ops=ops_list, undoLabel=LABEL))


def state():
    """The fake project as JSON; clip component chains (created lazily on first use) are created first."""
    for kind in ("video", "audio"):
        for clips in fake.STATE[kind]:
            for clip in clips:
                fake.chain(clip, kind)
    return json.dumps({k: fake.STATE.get(k) for k in ("items", "video", "audio", "markers", "sequence", "bins",
                                                     "trackNames")}, sort_keys=True, default=str)


# ---------------------------------------------------------------- the spec

class SpecTest(unittest.TestCase):
    def test_every_op_documents_its_read_back_and_live_evidence(self):
        for name, v in VARIANTS.items():
            with self.subTest(op=name):
                self.assertRegex(name, r"^[a-z]+\.[a-z_]+$")
                self.assertEqual(set(v) - {"doc", "properties", "requireOneOf", "readBack", "evidence", "removes",
                                           "direct"}, set())
                for key in ("doc", "readBack", "evidence"):
                    self.assertGreaterEqual(len(v[key]), 12, key)
                self.assertIn("pass", v["evidence"])  # only calls api_smoke passed live are ops
                self.assertNotIn("op", v["properties"])
        self.assertEqual(ops.REMOVING_OPS, {"marker.remove"})
        self.assertEqual([n for n, v in VARIANTS.items() if v.get("direct")], ["sequence.set_player_position"])
        self.assertEqual(QUERY["params"]["what"]["enum"], list(QUERY["variants"]))

    def test_variant_check(self):
        cases = [
            ({"ops": [{"op": "eval", "code": "x"}]}, ["params.ops[0].op: must be one of " + ", ".join(VARIANTS)]),
            ({"ops": [{"code": "x"}]}, ["params.ops[0].op: required"]),
            ({"ops": [{"op": 3}]}, ["params.ops[0].op: must be one of " + ", ".join(VARIANTS)]),
            ({"ops": [{"op": "marker.set_name", "newName": "x"}]}, ["params.ops[0]: needs one of guid, name"]),
            ({"ops": [{"op": "marker.add", "name": "x", "atSeconds": 1, "bogus": 1}]}, ["params.ops[0].bogus: unknown param"]),
            ({"ops": [{"op": "marker.set_color", "name": "x", "colorIndex": 9}]}, ["params.ops[0].colorIndex: must be <= 7"]),
            ({"ops": [{"op": "clip.set_disabled", "track": "V1", "atSeconds": 1, "disabled": "yes"}]},
             ["params.ops[0].disabled: expected boolean, got str"]),
            ({"ops": ["marker.add"]}, ["params.ops[0]: expected object, got str"]),
        ]
        for params, want in cases:
            with self.subTest(params=params):
                self.assertEqual(spec.check_params("apply_ops", params), want)
        self.assertEqual(spec.check_params("query", {"what": "clip", "track": "V1"}), ["params.atSeconds: required"])
        self.assertEqual(spec.check_params("query", {"what": "sequence", "bin": "x"}), ["params.bin: unknown param"])
        self.assertEqual(spec.check_params("query", {}), ["params.what: required"])
        self.assertEqual(spec.check_params("query", {"what": "clips", "fromSeconds": 1}),
                         ["params.fromSeconds: needs toSeconds too"])

    def test_the_op_tables_are_the_spec(self):
        """The panel's OPS / QUERIES and the fake's OPS / QUERIES list exactly the spec's variants: an op exists in all
        three or nowhere."""
        js = (REPO / "plugin" / "lib" / "handlers_ops.js").read_text(encoding="utf-8")
        table = js.split("const OPS = {", 1)[1].split("\n  };", 1)[0]
        self.assertEqual(re.findall(r'(?m)^    "([a-z]+\.[a-z_]+)": ', table), list(VARIANTS))
        queries = js.split("const QUERIES = {", 1)[1].split("\n  };", 1)[0]
        self.assertEqual(re.findall(r"(?m)^    async (\w+)\(", queries), list(QUERY["variants"]))
        self.assertEqual(list(fake.OPS), list(VARIANTS))
        self.assertEqual(list(fake.QUERIES), list(QUERY["variants"]))
        for name, (_, _, direct) in fake.OPS.items():
            self.assertEqual(direct, bool(VARIANTS[name].get("direct")), name)
        self.assertNotIn("eval(", js)
        self.assertNotIn("Function(", js)

    @unittest.skipIf(NODE is None, "node is not installed")
    def test_the_panel_mirrors_the_variant_check(self):
        cases = [
            {"ops": [{"op": "eval", "code": "x"}]},
            {"ops": [{"code": "x"}]},
            {"ops": [{"op": "marker.set_name", "newName": "x"}]},
            {"ops": [{"op": "marker.add", "name": "x", "atSeconds": -1, "bogus": 1}]},
            {"ops": [{"op": "param.set", "track": 1, "atSeconds": "1", "matchName": "m", "index": 0.5}]},
            {"ops": [{"op": "sequence.set_in_out"}]},
            {"ops": [{"op": "bin.rename", "bin": "EN", "newName": None}]},
        ]
        steps = [{"action": "apply_ops", "params": c} for c in cases]
        qcases = [{"what": "clip", "track": "V1"}, {"what": "everything"}, {"what": "markers", "x": 1}, {},
                  {"what": "clips", "toSeconds": 2, "tracks": [1]}]
        steps += [{"action": "query", "params": c} for c in qcases]
        out = harness("batch", {"steps": steps})
        for (action, params), reply in zip([(s["action"], s["params"]) for s in steps], out["replies"]):
            with self.subTest(action=action, params=params):
                want = spec.check_params(action, params)
                self.assertTrue(want)
                self.assertEqual((reply["ok"], reply["code"], reply["outcome"]), (False, "BAD_PARAMS", "not_committed"))
                self.assertEqual(reply["data"]["problems"], want)
        self.assertEqual(out["transactions"], [])


# ---------------------------------------------------------------- the fake panel

class FakeApplyOpsTest(FakeCase):
    OPS = [{"op": "marker.add", "atSeconds": 2.0, "name": "Flag", "comments": "Source?", "type": "Chapter"},
           {"op": "marker.set_comment", "name": "harness marker", "comments": "checked"},
           {"op": "clip.set_name", "track": "V1", "atSeconds": 5.0, "name": "A-roll"},
           {"op": "param.set", "track": "A1", "atSeconds": 5.0, "matchName": "Internal Volume Mono", "index": 1,
            "value": 0.0891},
           {"op": "sequence.set_in_out", "inSeconds": 1.0, "outSeconds": 9.0}]

    def test_a_dry_run_writes_nothing(self):
        before = state()
        data = self.apply(self.OPS, dryRun=True)
        self.assertEqual(state(), before)
        self.assertEqual(fake.TRANSACTIONS, [])
        self.assertEqual(fake.LAST_CTX["sideEffects"], 0)
        self.assertEqual((data["dryRun"], data["count"], data["undoLabel"]), (True, 5, LABEL))
        self.assertNotIn("verified", data)
        self.assertEqual([r["op"] for r in data["results"]], [o["op"] for o in self.OPS])
        self.assertEqual(data["results"][1]["before"], {"comments": ""})
        self.assertEqual(data["results"][2]["before"], {"name": "interview.mp4"})
        self.assertNotIn("readBack", data["results"][0])

    def test_one_transaction_one_label_every_op_read_back(self):
        data = self.apply(self.OPS)
        self.assertEqual(fake.TRANSACTIONS, [LABEL])
        self.assertEqual((data["dryRun"], data["verified"]), (False, True))
        self.assertTrue(all(r["ok"] and r["verified"] for r in data["results"]))
        markers = {m["name"]: m for m in self.ok("list_markers", {})["markers"]}
        self.assertEqual(markers["Flag"]["type"], "Chapter")
        self.assertEqual(markers["harness marker"]["comments"], "checked")
        clip = self.ok("list_items", {})["video"][0]["items"][0]
        self.assertEqual((clip["name"], clip["projectItem"]), ("A-roll", "interview.mp4"))
        level = self.ok("inspect_items", {"audioTracks": [0]})["tracks"][0]["clips"][0]["components"][0]["params"][1]
        self.assertAlmostEqual(level["value"], 0.0891)
        seq = self.ok("query", {"what": "sequence"})
        self.assertEqual((seq["inPoint"], seq["outPoint"]), (fake.snap(1.0), fake.snap(9.0)))
        self.assertEqual(data["results"][0]["readBack"]["guid"], markers["Flag"]["guid"])

    def test_a_failing_op_commits_nothing(self):
        before = state()
        bad = self.OPS + [{"op": "marker.set_name", "name": "No such marker", "newName": "x"}]
        reply = self.fails("apply_ops", {"ops": bad}, "NOT_FOUND")
        self.assertTrue(reply["error"].startswith("ops[5] marker.set_name: "), reply["error"])
        self.assertEqual(state(), before)
        fake.OP_FAULTS["clip.set_name"] = "build"
        reply = self.fails("apply_ops", {"ops": self.OPS}, "INTERNAL")
        self.assertIn("nothing was committed", reply["error"])
        self.assertEqual((state(), fake.TRANSACTIONS), (before, []))
        fake.OP_FAULTS.clear()
        fake.FAIL_TRANSACTIONS.add(LABEL)
        self.fails("apply_ops", {"ops": self.OPS, "undoLabel": LABEL}, "TRANSACTION_FAILED", "unknown")
        self.assertEqual(state(), before)

    def test_a_read_back_that_disagrees_is_verify_failed_committed(self):
        fake.OP_FAULTS["marker.set_comment"] = "ignore"
        reply = self.fails("apply_ops", {"ops": self.OPS, "undoLabel": LABEL}, "VERIFY_FAILED", "committed")
        self.assertEqual(fake.TRANSACTIONS, [LABEL])
        results = reply["data"]["results"]
        self.assertEqual([r["verified"] for r in results], [True, False, True, True, True])
        self.assertEqual(results[1]["readBack"], {"comments": ""})
        self.assertIs(reply["data"]["verified"], False)

    def test_unknown_ops_are_refused_by_the_spec(self):
        seen = []
        fake.HANDLERS["apply_ops"] = lambda p: seen.append(p) or {}
        reply = self.fails("apply_ops", {"ops": [{"op": "eval", "code": "app.quit()"}]}, "BAD_PARAMS")
        self.assertIn("params.ops[0].op: must be one of", reply["error"])
        self.fails("apply_ops", {"ops": [{"op": "marker.add", "name": "x", "atSeconds": 1, "script": "y"}]}, "BAD_PARAMS")
        self.assertEqual(seen, [])

    def test_markers(self):
        guid = self.ok("list_markers", {})["markers"][0]["guid"]
        data = self.apply([{"op": "marker.set_name", "guid": guid, "newName": "renamed"},
                           {"op": "marker.set_color", "guid": guid, "colorIndex": 6},
                           {"op": "marker.set_duration", "guid": guid, "durationSeconds": 1.5},
                           {"op": "marker.set_type", "guid": guid, "type": "Segmentation"},
                           {"op": "marker.move", "guid": guid, "toSeconds": 3.0}])
        m = self.ok("query", {"what": "markers"})["markers"][0]
        self.assertEqual((m["name"], m["colorIndex"], m["duration"], m["type"], m["start"]),
                         ("renamed", 6, 1.5, "Segmentation", fake.snap(3.0)))
        self.assertTrue(data["verified"])
        self.apply([{"op": "marker.remove", "name": "renamed"}])
        self.assertEqual(self.ok("list_markers", {})["count"], 0)
        self.apply([{"op": "marker.add", "atSeconds": 4.0, "name": "twin"}])
        self.apply([{"op": "marker.add", "atSeconds": 8.0, "name": "twin"}])
        self.fails("apply_ops", {"ops": [{"op": "marker.remove", "name": "twin"}]}, "AMBIGUOUS")
        self.apply([{"op": "marker.remove", "name": "twin", "atSeconds": 8.0}])
        self.fails("apply_ops", {"ops": [{"op": "marker.set_name", "name": "twin", "newName": ""}]}, "BAD_PARAMS")

    def test_clip_ops_change_the_fingerprint_like_any_edit(self):
        before = self.ok("get_sequence", {})
        from ppro_bridge import items
        fp = items.fingerprint(fake.listing(["video", "audio"]), before)
        self.apply([{"op": "clip.set_disabled", "track": "V1", "atSeconds": 30.0, "disabled": True}])
        clip = self.ok("query", {"what": "clips", "tracks": ["v1"], "atSeconds": 30.0})["tracks"][0]["clips"][0]
        self.assertIs(clip["disabled"], True)
        self.assertNotEqual(items.fingerprint(fake.listing(["video", "audio"]), before), fp)
        self.fails("apply_ops", {"ops": [{"op": "clip.set_name", "track": "A1", "atSeconds": 5.0, "name": "x"}]},
                   "UNSUPPORTED")
        self.fails("apply_ops", {"ops": [{"op": "clip.set_name", "track": "V2", "atSeconds": 5.0, "name": "x"}]},
                   "NOT_FOUND")
        os.environ["PPRO_FAKE_CAPS"] = json.dumps({"trackItemDisable": False})
        self.fails("apply_ops", {"ops": [{"op": "clip.set_disabled", "track": "V1", "atSeconds": 5.0,
                                          "disabled": False}]}, "UNSUPPORTED")

    def test_param_set(self):
        point = {"op": "param.set", "track": "V1", "atSeconds": 5.0, "matchName": "AE.ADBE Motion", "index": 0,
                 "value": [0.25, 0.75]}
        flag = dict(point, index=3, value=False)
        scale = dict(point, index=1, value=150)
        data = self.apply([point, flag, scale])
        self.assertEqual([r["readBack"]["value"] for r in data["results"]], [{"x": 0.25, "y": 0.75}, False, 150])
        self.assertEqual(fake.STATE["video"][0][0]["scale"], 150)
        self.fails("apply_ops", {"ops": [dict(point, value=True)]}, "BAD_PARAMS")  # a point param, a boolean value
        self.fails("apply_ops", {"ops": [dict(point, value="#ff0000")]}, "BAD_PARAMS")
        self.fails("apply_ops", {"ops": [dict(point, matchName="AE.ADBE Gaussian Blur 2")]}, "NOT_FOUND")
        self.fails("apply_ops", {"ops": [dict(point, index=40)]}, "NOT_FOUND")
        self.fails("apply_ops", {"ops": [dict(point, occurrence=2)]}, "NOT_FOUND")
        rotation = fake.STATE["video"][0][0]["components"][1]["params"][4]
        rotation.update(keyframed=True, keyframes=[{"t": 1.0, "value": 0.0}, {"t": 2.0, "value": 90.0}])
        self.fails("apply_ops", {"ops": [dict(point, index=4, value=10)]}, "KEYFRAMED")
        rotation.update(keyframes=[])  # animation on, no keys: switched off and set
        data = self.apply([dict(point, index=4, value=10)])
        self.assertEqual(data["results"][0]["before"]["animated"], True)
        self.assertEqual(data["results"][0]["readBack"], {"value": 10, "animated": False})

    def test_sequence_points_and_the_playhead(self):
        data = self.apply([{"op": "sequence.set_player_position", "atSeconds": 12.5},
                           {"op": "sequence.set_in_out", "outSeconds": 20.0}])
        self.assertEqual(data["notUndoable"], ["sequence.set_player_position"])
        self.assertIs(data["results"][0]["undoable"], False)
        self.assertEqual(fake.TRANSACTIONS, [LABEL])
        self.assertAlmostEqual(self.ok("get_sequence", {})["playerPositionSeconds"], fake.snap(12.5))
        fake.TRANSACTIONS.clear()
        self.apply([{"op": "sequence.set_player_position", "atSeconds": 1.0}])
        self.assertEqual(fake.TRANSACTIONS, [])  # a direct call only: no transaction
        self.assertEqual(fake.LAST_CTX["sideEffects"], 1)
        self.fails("apply_ops", {"ops": [{"op": "sequence.set_in_out", "inSeconds": 5.0, "outSeconds": 5.0}]},
                   "BAD_PARAMS")

    def test_tracks_and_bins(self):
        self.apply([{"op": "track.rename", "track": "V3", "name": "Graphics"},
                    {"op": "bin.create", "name": "Selects"},
                    {"op": "bin.create", "name": "Day 1", "parent": "EN"},
                    {"op": "bin.rename", "bin": "CN", "newName": "ZH"}])
        self.assertEqual(self.ok("get_sequence", {})["videoTracks"][2]["name"], "Graphics")
        bins = {e["path"]: e for e in self.ok("query", {"what": "bins"})["entries"]}
        self.assertLessEqual({"Selects", "EN", "EN/Day 1", "ZH", "ZH/title.mov"}, set(bins))
        self.assertNotIn("CN", bins)
        self.assertEqual(self.ok("query", {"what": "bins", "bin": "EN"})["count"], 2)
        self.fails("apply_ops", {"ops": [{"op": "bin.create", "name": "Selects"}]}, "PRECONDITION_FAILED")
        self.fails("apply_ops", {"ops": [{"op": "bin.create", "name": "a/b"}]}, "BAD_PARAMS")
        self.fails("apply_ops", {"ops": [{"op": "bin.create", "name": "x", "parent": "Nope"}]}, "NOT_FOUND")
        self.fails("apply_ops", {"ops": [{"op": "bin.rename", "bin": "ZH", "newName": "EN"}]}, "PRECONDITION_FAILED")
        self.fails("apply_ops", {"ops": [{"op": "bin.rename", "bin": "/", "newName": "x"}]}, "BAD_PARAMS")
        self.fails("apply_ops", {"ops": [{"op": "track.rename", "track": "A1", "name": "x"}]}, "UNSUPPORTED")
        self.fails("apply_ops", {"ops": [{"op": "track.rename", "track": "V9", "name": "x"}]}, "NOT_FOUND")
        # an empty bin made here can be removed again
        self.ok("remove_project_items", {"bins": ["Selects"]})
        self.assertNotIn("Selects", {e["path"] for e in self.ok("query", {"what": "bins"})["entries"]})

    def test_conflicting_ops_are_refused(self):
        cases = [
            [{"op": "marker.set_name", "name": "harness marker", "newName": "a"},
             {"op": "marker.set_name", "name": "harness marker", "newName": "b"}],
            [{"op": "marker.remove", "name": "harness marker"},
             {"op": "marker.set_color", "name": "harness marker", "colorIndex": 2}],
            [{"op": "bin.create", "name": "X"}, {"op": "bin.rename", "bin": "EN", "newName": "X"}],
            [{"op": "marker.add", "atSeconds": 3, "name": "n"}, {"op": "marker.add", "atSeconds": 3, "name": "n"}],
        ]
        for ops_list in cases:
            with self.subTest(ops=[o["op"] for o in ops_list]):
                reply = self.fails("apply_ops", {"ops": ops_list}, "BAD_PARAMS")
                self.assertIn("ops[0]", reply["error"])
        self.apply([{"op": "marker.set_name", "name": "harness marker", "newName": "a"},
                    {"op": "marker.set_color", "name": "harness marker", "colorIndex": 2}])  # two fields: fine

    def test_bin_only_requests_need_no_sequence(self):
        fake.STATE["sequence"] = None
        self.apply([{"op": "bin.create", "name": "Selects"}])
        self.fails("apply_ops", {"ops": [{"op": "marker.add", "atSeconds": 1, "name": "x"}]}, "NO_SEQUENCE")

    def test_the_cap(self):
        many = [{"op": "bin.create", "name": f"b{i}"} for i in range(201)]
        reply = self.fails("apply_ops", {"ops": many}, "BAD_PARAMS")
        self.assertIn("at most 200", reply["error"])


class FakeQueryTest(FakeCase):
    def test_queries(self):
        clips = self.ok("query", {"what": "clips"})
        self.assertEqual([t["label"] for t in clips["tracks"]], ["V1", "V2", "V3", "V4", "A1", "A2", "A3", "A4"])
        self.assertEqual(self.ok("query", {"what": "clips", "tracks": ["A1", "A1"], "fromSeconds": 70,
                                           "toSeconds": 80})["tracks"], [
            {"label": "A1", "kind": "audio", "index": 0, "name": "Audio 1", "clips": []}])
        clip = self.ok("query", {"what": "clip", "track": "A1", "atSeconds": 3})["clip"]
        self.assertEqual(clip["components"][0]["matchName"], "Internal Volume Mono")
        self.assertEqual(self.ok("query", {"what": "markers"})["count"], 1)
        seq = self.ok("query", {"what": "sequence"})
        self.assertEqual((seq["frameSize"], seq["inPoint"], seq["outPoint"]), ({"width": 1080, "height": 1920}, None, None))
        self.fails("query", {"what": "clips", "tracks": ["V9"]}, "NOT_FOUND")
        self.fails("query", {"what": "clips", "tracks": ["X"]}, "BAD_PARAMS")
        self.fails("query", {"what": "clips", "fromSeconds": 5, "toSeconds": 1}, "BAD_PARAMS")
        fake.STATE["sequence"] = None
        self.fails("query", {"what": "markers"}, "NO_SEQUENCE")
        self.assertEqual(self.ok("query", {"what": "bins"})["bin"], "")


# ---------------------------------------------------------------- the panel (node harness) next to the fake

@unittest.skipIf(NODE is None, "node is not installed")
class PanelParityTest(FakeCase):
    """The same requests through the panel (Premiere mocks: V1 / A1 interview 0-60 s, a marker at 1 s, bins EN / CN)
    and the fake: the same ok / code / outcome, the same per-op verified, one transaction where the fake has one."""

    SCENARIOS = {
        "every op family": [("apply_ops", {"ops": [
            {"op": "marker.add", "atSeconds": 2.0, "name": "Flag", "durationSeconds": 0.5},
            {"op": "marker.set_name", "name": "harness marker", "newName": "renamed"},
            {"op": "marker.set_comment", "name": "harness marker", "comments": "c"},
            {"op": "marker.set_color", "name": "harness marker", "colorIndex": 3},
            {"op": "marker.set_duration", "name": "harness marker", "durationSeconds": 1.0},
            {"op": "marker.set_type", "name": "harness marker", "type": "Chapter"},
            {"op": "marker.move", "name": "harness marker", "toSeconds": 4.0},
            {"op": "clip.set_name", "track": "V1", "atSeconds": 5.0, "name": "A-roll"},
            {"op": "clip.set_disabled", "track": "V1", "atSeconds": 5.0, "disabled": True},
            {"op": "param.set", "track": "V1", "atSeconds": 5.0, "matchName": "AE.ADBE Opacity", "index": 0, "value": 50},
            {"op": "param.set", "track": "A1", "atSeconds": 5.0, "matchName": "Internal Volume Mono", "index": 1,
             "value": 0.1},
            {"op": "sequence.set_in_out", "inSeconds": 1.0, "outSeconds": 10.0},
            {"op": "sequence.set_player_position", "atSeconds": 7.0},
            {"op": "track.rename", "track": "V4", "name": "Captions"},
            {"op": "bin.create", "name": "Selects"},
            {"op": "bin.rename", "bin": "CN", "newName": "ZH"}], "undoLabel": LABEL}),
            ("query", {"what": "markers"}), ("query", {"what": "sequence"}), ("query", {"what": "bins"}),
            ("query", {"what": "clip", "track": "V1", "atSeconds": 5.0})],
        "dry run": [("apply_ops", {"ops": [{"op": "marker.remove", "name": "harness marker"},
                                           {"op": "sequence.set_player_position", "atSeconds": 2.0}],
                                   "dryRun": True}),
                    ("query", {"what": "markers"})],
        "remove": [("apply_ops", {"ops": [{"op": "marker.remove", "name": "harness marker"}]}),
                   ("apply_ops", {"ops": [{"op": "marker.remove", "name": "harness marker"}]})],
        "refusals": [("apply_ops", {"ops": [{"op": "bin.create", "name": "x", "parent": "EN/none"}]}),
                     ("apply_ops", {"ops": [{"op": "param.set", "track": "V1", "atSeconds": 5.0,
                                             "matchName": "AE.ADBE Opacity", "index": 0, "value": True}]}),
                     ("apply_ops", {"ops": [{"op": "marker.add", "atSeconds": 1.0, "name": ""}]}),
                     ("apply_ops", {"ops": [{"op": "sequence.set_in_out", "inSeconds": 3.0, "outSeconds": 2.0}]}),
                     ("apply_ops", {"ops": [{"op": "track.rename", "track": "A2", "name": "x"}]}),
                     ("apply_ops", {"ops": [{"op": "bin.rename", "bin": "EN", "newName": "CN"}]}),
                     ("query", {"what": "clips", "tracks": ["V1"], "atSeconds": 5, "fromSeconds": 0, "toSeconds": 1}),
                     ("query", {"what": "clips", "tracks": ["A9"]})],
    }

    def test_the_panel_answers_like_the_fake(self):
        for name, steps in self.SCENARIOS.items():
            with self.subTest(scenario=name):
                fake.reset()
                want = [self.call(a, copy.deepcopy(p)) for a, p in steps]
                fake_tx = list(fake.TRANSACTIONS)
                out = panel_batch(steps)
                for (action, params), w, g in zip(steps, want, out["replies"]):
                    self.assertEqual((g["ok"], g.get("code"), g.get("outcome")), (w["ok"], w.get("code"), w.get("outcome")),
                                     f"{action}: panel {g.get('error')} / fake {w.get('error')}")
                    if g["ok"]:
                        self.assertEqual(set(g["data"]), set(w["data"]), action)
                        if action == "apply_ops":
                            self.assertEqual([r.get("verified") for r in g["data"]["results"]],
                                             [r.get("verified") for r in w["data"]["results"]])
                            self.assertEqual([set(r) for r in g["data"]["results"]], [set(r) for r in w["data"]["results"]])
                self.assertEqual(out["transactions"], fake_tx)

    def test_every_op_lands_in_the_mock_project(self):
        steps = self.SCENARIOS["every op family"]
        out = panel_batch(steps)
        apply, markers, seq, bins, clip = out["replies"]
        self.assertTrue(apply["ok"], apply)
        self.assertEqual(out["transactions"], [LABEL])
        self.assertEqual(out["sideEffects"][0], 2)  # the transaction, then the playhead
        self.assertTrue(apply["data"]["verified"])
        m = {x["name"]: x for x in markers["data"]["markers"]}
        self.assertEqual((m["renamed"]["comments"], m["renamed"]["colorIndex"], m["renamed"]["type"]), ("c", 3, "Chapter"))
        self.assertAlmostEqual(m["renamed"]["start"], round(4.0 * 60000 / 1001) * 1001 / 60000, places=6)
        self.assertAlmostEqual(m["Flag"]["duration"], 0.5)
        s = seq["data"]
        self.assertAlmostEqual(s["playerPositionSeconds"], round(7.0 * 60000 / 1001) * 1001 / 60000, places=6)
        self.assertEqual(s["videoTracks"][3]["name"], "Captions")
        self.assertGreater(s["outPoint"], s["inPoint"])
        paths = {e["path"] for e in bins["data"]["entries"]}
        self.assertLessEqual({"Selects", "ZH", "ZH/title.mov", "EN/title.mov"}, paths)
        c = clip["data"]["clip"]
        self.assertEqual((c["name"], c["disabled"], c["projectItem"]), ("A-roll", True, "interview.mp4"))
        opacity = next(x for x in c["components"] if x["matchName"] == "AE.ADBE Opacity")
        self.assertEqual(opacity["params"][0]["value"], 50)

    def test_a_failing_op_commits_nothing_in_the_panel(self):
        ops_list = [{"op": "marker.set_comment", "name": "harness marker", "comments": "c"},
                    {"op": "clip.set_name", "track": "V1", "atSeconds": 5.0, "name": "x"}]
        out = panel_batch([("apply_ops", {"ops": ops_list}), ("query", {"what": "markers"})],
                          mock={"failCreate": ["createSetNameAction"]})
        reply = out["replies"][0]
        self.assertEqual((reply["code"], reply["outcome"]), ("INTERNAL", "not_committed"), reply)
        self.assertIn("ops[1] clip.set_name: its action could not be built", reply["error"])
        self.assertEqual(out["transactions"], [])
        self.assertEqual(out["sideEffects"][0], 0)
        self.assertEqual(out["replies"][1]["data"]["markers"][0]["comments"], "")
        # Premiere refusing the transaction: nothing applied either, outcome unknown by the side-effect rule
        out = panel_batch([("apply_ops", {"ops": ops_list, "undoLabel": LABEL}), ("query", {"what": "markers"})],
                          mock={"failTransactions": [LABEL]})
        self.assertEqual((out["replies"][0]["code"], out["replies"][0]["outcome"]), ("TRANSACTION_FAILED", "unknown"))
        self.assertEqual(out["replies"][1]["data"]["markers"][0]["comments"], "")

    def test_a_read_back_mismatch_in_the_panel(self):
        out = panel_batch([("apply_ops", {"ops": [{"op": "marker.set_comment", "name": "harness marker",
                                                   "comments": "c"}, {"op": "bin.create", "name": "B"}]})],
                          mock={"ignoreActions": ["createSetCommentsAction"]})
        reply = out["replies"][0]
        self.assertEqual((reply["code"], reply["outcome"]), ("VERIFY_FAILED", "committed"), reply)
        self.assertEqual([r["verified"] for r in reply["data"]["results"]], [False, True])

    def test_keyframed_params_and_the_capability(self):
        rotation = {"op": "param.set", "track": "V2", "atSeconds": 12.0, "matchName": "AE.ADBE Motion", "index": 4,
                    "value": 10}
        out = panel_batch([("apply_ops", {"ops": [rotation]})])
        self.assertEqual(out["replies"][0]["code"], "KEYFRAMED")
        table = {"denied": [], "quirks": {}}
        out = harness("batch", {"steps": [{"action": "ping", "params": {"probe": True}},
                                          {"action": "apply_ops", "params": {"ops": [
                                              {"op": "clip.set_disabled", "track": "V1", "atSeconds": 5.0,
                                               "disabled": True}]}}], "hostTable": table})
        self.assertTrue(out["replies"][1]["ok"], out["replies"][1])


# ---------------------------------------------------------------- the library, the CLI and the MCP gate

class OpsFunctionTest(unittest.TestCase):
    def setUp(self):
        self.rpc, self.fake, self.calls = fake_rpc()
        self.scratch = Path(tempfile.mkdtemp(prefix="ppro_ops_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def test_a_list_a_file_or_inline_json(self):
        listed = [{"op": "marker.add", "atSeconds": 3.0, "name": "a"}]
        data = ops.apply_ops(self.rpc, ops=listed, label="one")
        self.assertEqual((data["verified"], data["undoLabel"]), (True, "one"))
        path = self.scratch / "ops.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ops": [{"op": "marker.add", "atSeconds": 6.0,
                                                              "name": "b"}]}).encode("utf-8"))
        self.assertTrue(ops.apply_ops(self.rpc, ops=str(path))["verified"])
        inline = json.dumps([{"op": "marker.set_color", "name": "b", "colorIndex": 1}])
        self.assertTrue(ops.apply_ops(self.rpc, ops=inline, dry_run=True)["dryRun"])
        names = [m["name"] for m in ops.query(self.rpc, what="markers")["markers"]]
        self.assertEqual(names, ["harness marker", "a", "b"])
        self.assertEqual(ops.query(self.rpc, what="markers")["markers"][0]["color"], "green")
        self.assertEqual([c[0] for c in self.calls if c[0] == "apply_ops"], ["apply_ops"] * 3)

    def test_problems_are_found_before_anything_is_sent(self):
        for bad in ([], [{"op": "eval"}], "[1, 2", {"steps": []}, json.dumps([{"op": "marker.add"}])):
            with self.subTest(ops=bad):
                with self.assertRaises(BridgeError) as cm:
                    ops.apply_ops(self.rpc, ops=bad)
                self.assertEqual((cm.exception.code, cm.exception.outcome), ("BAD_PARAMS", "not_committed"))
        with self.assertRaises(BridgeError) as cm:
            ops.apply_ops(self.rpc, ops=str(self.scratch / "missing.json"))
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")
        self.assertEqual(self.calls, [])
        with self.assertRaises(BridgeError) as cm:
            ops.apply_ops(self.rpc, ops=[{"op": "bin.create", "name": "x"}], expect_seq="Other")
        self.assertEqual(cm.exception.code, "SEQUENCE_MISMATCH")
        self.assertNotIn("apply_ops", [c[0] for c in self.calls])

    def test_an_unverified_reply_is_verify_failed(self):
        calls = []
        rpc = stub_rpc({"ping": {"version": ops.VERSION, "protocol": spec.protocol(), "actions": spec.action_names()},
                        "apply_ops": {"dryRun": False, "count": 1, "undoLabel": "x",
                                      "results": [{"index": 0, "op": "bin.create", "ok": True, "verified": False}]}},
                       calls)
        with self.assertRaises(BridgeError) as cm:
            ops.apply_ops(rpc, ops=[{"op": "bin.create", "name": "x"}])
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))

    def test_query_options(self):
        self.assertEqual(ops.query(self.rpc, what="clip", track="v1", at=5.0)["track"], "V1")
        self.assertEqual(len(ops.query(self.rpc, what="clips", track=["V1", "A1"], from_s=0, to_s=1)["tracks"]), 2)
        for kwargs in ({"what": "markers", "at": 1.0}, {"what": "clip", "track": ["V1", "V2"], "at": 1.0},
                       {"what": "clip", "track": ["V1"]}, {"what": "clips", "from_s": 1.0}, {"what": "nope"},
                       {"what": "sequence", "bin": "EN"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(BridgeError) as cm:
                    ops.query(self.rpc, **kwargs)
                self.assertEqual(cm.exception.code, "BAD_PARAMS")

    def test_the_cli(self):
        def run(argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(argv, rpc=self.rpc)
            return code, json.loads(buf.getvalue())

        code, out = run(["apply_ops", "--json", '[{"op": "bin.create", "name": "Selects"}]', "--label", "bins"])
        self.assertEqual((code, out["data"]["undoLabel"], out["data"]["verified"]), (0, "bins", True))
        code, out = run(["query", "--what", "bins", "--bin", "Selects"])
        self.assertEqual((code, out["data"]["count"]), (0, 0))
        code, out = run(["apply_ops", "--json", '[{"op": "eval"}]'])
        self.assertEqual((code, out["code"]), (1, "BAD_PARAMS"))
        code, out = run(["query", "--what", "sequence", "--at", "3"])
        self.assertEqual((code, out["code"]), (1, "BAD_PARAMS"))


class McpGateTest(unittest.TestCase):
    def setUp(self):
        self.rpc, self.fake, self.calls = fake_rpc()

    def call(self, name, args):
        return tools.call_tool(name, args, self.rpc)["structuredContent"]

    def test_apply_ops_is_gated_only_when_an_op_removes_something(self):
        obj = self.call("apply_ops", {"json": json.dumps([{"op": "marker.set_color", "name": "harness marker",
                                                           "colorIndex": 1}])})
        self.assertEqual((obj["ok"], obj["status"]), (True, "verified"), obj)
        self.assertNotIn("confirmRequired", obj)
        removing = {"json": json.dumps([{"op": "marker.remove", "name": "harness marker"}]), "label": "drop flag"}
        obj = self.call("apply_ops", removing)
        self.assertEqual((obj["status"], obj["confirmRequired"]), ("dry_run", True))
        self.assertEqual(self.call("query", {"what": "markers"})["data"]["count"], 1)
        obj = self.call(obj["confirmCall"]["name"], obj["confirmCall"]["arguments"])
        self.assertEqual(obj["status"], "verified", obj)
        self.assertEqual(self.call("query", {"what": "markers"})["data"]["count"], 0)
        obj = self.call("apply_ops", {"json": "not json and no such file"})
        self.assertEqual((obj["ok"], obj["code"]), (False, "FILE_NOT_FOUND"))  # gated: the dry run reports it

    def test_query_is_a_read(self):
        tool = tools.registry()["query"]
        self.assertTrue(tool.annotations["readOnlyHint"])
        self.assertIsNone(tool.gate)
        self.assertEqual(self.call("query", {"what": "sequence"})["status"], "read")


if __name__ == "__main__":
    unittest.main()
