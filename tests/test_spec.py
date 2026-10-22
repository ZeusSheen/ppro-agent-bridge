"""The action spec (ppro_bridge/actions.json) is well formed, and the fake panel, the real panel (through the node
harness) and the CLI agree with it.

Examples and errorExamples run through a fresh fake panel; string params starting with "/tmp/" are rewritten to a
scratch folder (created) so export examples have somewhere to write. Panel checks skip without node or
tests/panel/harness.js; CLI checks skip until ppro_bridge.cli imports.
"""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import argparse
import importlib
import json
import math
import re
import shutil
import subprocess
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO, REPO / "broker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ppro_bridge import spec  # noqa: E402

import fake_panel  # noqa: E402

SPEC = spec.load()
ACTIONS = SPEC["actions"]
NAMES = [a["name"] for a in ACTIONS]
BY_NAME = {a["name"]: a for a in ACTIONS}
ERRORS = SPEC["errors"]
OUTCOMES = list(SPEC["outcomes"])
TYPES = {"string", "number", "integer", "boolean", "array", "object", "any"}
SCHEMA_KEYS = {"type", "required", "default", "enum", "minimum", "maximum", "minItems", "items", "properties",
               "additionalProperties", "pairedWith", "deprecated", "doc"}
ACTION_KEYS = {"name", "kind", "since", "timeoutSeconds", "capability", "allowedWhenStuck", "allowedWhenDenied",
               "readOnly", "destructive", "idempotent", "doc", "params", "reply", "replyOptional", "fakeOnlyReply",
               "errors", "example", "errorExamples"}
MIN_DOC = 25
HARNESS = REPO / "tests" / "panel" / "harness.js"


def same(a, b) -> bool:
    """Deep equality with a float tolerance (example replies are written by hand)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return a == b


def localize(params: dict, scratch: Path) -> dict:
    out = {}
    for k, v in params.items():
        if isinstance(v, str) and v.startswith("/tmp/"):
            p = scratch / v[len("/tmp/"):]
            (p if k == "dir" else p.parent).mkdir(parents=True, exist_ok=True)
            v = str(p)
        out[k] = v
    return out


def clean_fake_env(test: unittest.TestCase):
    """Examples are written for the fake's defaults: no PPRO_FAKE_* switch may leak in from other tests."""
    patch = mock.patch.dict(os.environ)
    patch.start()
    test.addCleanup(patch.stop)
    for key in [k for k in os.environ if k.startswith("PPRO_FAKE_")]:
        del os.environ[key]


def schema_problems(schema, path) -> list[str]:
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    probs = [f"{path}: unknown schema key {k}" for k in schema if k not in SCHEMA_KEYS]
    if schema.get("type", "any") not in TYPES:
        probs.append(f"{path}: unknown type {schema.get('type')}")
    if "items" in schema:
        probs += schema_problems(schema["items"], path + "[]")
    for name, sub in (schema.get("properties") or {}).items():
        probs += schema_problems(sub, f"{path}.{name}")
    if "default" in schema:
        probs += spec.check_value(schema, schema["default"], path + " default")
    if "doc" in schema and not isinstance(schema["doc"], str):
        probs.append(f"{path}: doc must be a string")
    return probs


# ---------------------------------------------------------------- the spec itself

class SpecShapeTest(unittest.TestCase):
    def test_top_level(self):
        self.assertEqual(SPEC["specVersion"], 1)
        self.assertIsInstance(SPEC["protocol"], int)
        self.assertGreaterEqual(SPEC["protocol"], 2)
        self.assertEqual(OUTCOMES, ["not_committed", "committed", "partial", "unknown"])
        self.assertGreater(SPEC["defaultTimeoutSeconds"], 0)
        self.assertLessEqual(set(SPEC["globalErrors"]), set(ERRORS))
        self.assertEqual(len(NAMES), len(set(NAMES)), "duplicate action names")
        cmd_names = spec.command_names()
        self.assertEqual(len(cmd_names), len(set(cmd_names)), "duplicate command names")

    def test_errors(self):
        for code, e in ERRORS.items():
            with self.subTest(code=code):
                self.assertRegex(code, r"^[A-Z][A-Z_]+$")
                self.assertIn(e["outcome"], OUTCOMES + ["varies"])
                self.assertTrue(e["source"] and set(e["source"]) <= {"panel", "broker", "cli"}, e["source"])
                self.assertGreaterEqual(len(e["doc"]), MIN_DOC, e["doc"])

    def test_capabilities(self):
        for name, c in SPEC["capabilities"].items():
            with self.subTest(capability=name):
                self.assertIsInstance(c["since"], str)
                self.assertIn(c["probe"], ("static", "instance"))
                self.assertTrue(c["doc"])

    def test_actions(self):
        for a in ACTIONS:
            with self.subTest(action=a["name"]):
                self.assertEqual(set(a) - ACTION_KEYS - {"requireOneOf", "vectors"}, set())
                self.assertLessEqual(ACTION_KEYS, set(a))
                self.assertIn(a["kind"], ("read", "write"))
                self.assertEqual(a["readOnly"], a["kind"] == "read")
                if a["destructive"]:
                    self.assertEqual(a["kind"], "write")
                self.assertIsInstance(a["timeoutSeconds"], int)
                self.assertGreater(a["timeoutSeconds"], 0)
                self.assertTrue(a["capability"] is None or a["capability"] in SPEC["capabilities"], a["capability"])
                for flag in ("allowedWhenStuck", "allowedWhenDenied", "readOnly", "destructive", "idempotent"):
                    self.assertIsInstance(a[flag], bool, flag)
                self.assertGreaterEqual(len(a["doc"]), MIN_DOC, a["doc"])
                self.assertEqual([p for name, s in a["params"].items() for p in schema_problems(s, name)], [])
                for name, s in a["params"].items():
                    partner = s.get("pairedWith")
                    if partner:
                        self.assertEqual(a["params"].get(partner, {}).get("pairedWith"), name, name)
                for group in a.get("requireOneOf", []):
                    self.assertLessEqual(set(group), set(a["params"]), group)
                lists = (a["reply"], a["replyOptional"], a["fakeOnlyReply"])
                self.assertTrue(all(isinstance(k, str) for keys in lists for k in keys))
                self.assertEqual(sum(len(k) for k in lists), len(set().union(*map(set, lists))), "reply lists overlap")
                for code in a["errors"]:
                    self.assertIn(code, ERRORS)
                    self.assertIn("panel", ERRORS[code]["source"], code)

    def test_only_ping_runs_when_stuck_or_denied(self):
        self.assertEqual([a["name"] for a in ACTIONS if a["allowedWhenStuck"]], ["ping"])
        self.assertEqual([a["name"] for a in ACTIONS if a["allowedWhenDenied"]], ["ping"])

    def test_examples(self):
        for a in ACTIONS:
            ex = a["example"]
            if ex is None:
                continue
            with self.subTest(action=a["name"]):
                self.assertEqual(set(ex), {"params", "reply"})
                self.assertEqual(spec.check_params(a["name"], ex["params"]), [])
                allowed = set(a["reply"]) | set(a["replyOptional"]) | set(a["fakeOnlyReply"])
                self.assertLessEqual(set(ex["reply"]), allowed)

    def test_error_examples(self):
        for a in ACTIONS:
            for i, e in enumerate(a["errorExamples"]):
                with self.subTest(action=a["name"], example=i):
                    self.assertLessEqual(set(e), {"params", "code", "outcome", "doc"})
                    self.assertIn(e["code"], set(a["errors"]) | set(SPEC["globalErrors"]))
                    self.assertIn(e["outcome"], OUTCOMES)
                    if ERRORS[e["code"]]["outcome"] != "varies":
                        self.assertEqual(e["outcome"], ERRORS[e["code"]]["outcome"])
                    if e["code"] != "BAD_PARAMS":  # must get past the param check to reach the handler
                        self.assertEqual(spec.check_params(a["name"], e["params"]), [])

    def test_commands(self):
        kinds = {a["name"]: a["kind"] for a in ACTIONS}
        for c in SPEC["commands"]:
            with self.subTest(command=c["name"]):
                self.assertEqual(set(c), {"name", "kind", "calls", "requirePanel", "minProtocol", "dryRun", "doc"})
                self.assertIn(c["kind"], ("read", "write"))
                self.assertTrue(c["calls"])
                self.assertLessEqual(set(c["calls"]), set(NAMES))
                self.assertIsInstance(c["requirePanel"], bool)
                self.assertIsInstance(c["dryRun"], bool)
                self.assertIn(c["minProtocol"], range(1, SPEC["protocol"] + 1))
                self.assertGreaterEqual(len(c["doc"]), MIN_DOC, c["doc"])
                if c["kind"] == "read":
                    self.assertEqual({kinds[x] for x in c["calls"]}, {"read"})
                else:
                    self.assertIn("write", {kinds[x] for x in c["calls"]})

    def test_set_params_vectors(self):
        a = BY_NAME["set_params"]
        bases = a["params"]["components"]["items"]["properties"]["params"]["items"]["properties"]["timeBase"]["enum"]
        self.assertTrue(a["vectors"])
        for i, v in enumerate(a["vectors"]):
            with self.subTest(vector=i):
                self.assertLessEqual({"clip", "frameSeconds", "timeBase", "t"}, set(v))
                self.assertEqual(set(v["clip"]), {"start", "inPoint", "outPoint", "speed"})
                self.assertTrue(all(isinstance(x, (int, float)) for x in v["clip"].values()))
                self.assertGreater(v["frameSeconds"], 0)
                self.assertIn(v["timeBase"], bases)
                self.assertIsInstance(v["t"], (int, float))
                self.assertEqual(("mediaSeconds" in v) + ("code" in v), 1, "one of mediaSeconds / code")
                if "code" in v:
                    self.assertIn(v["code"], set(a["errors"]) | set(SPEC["globalErrors"]))


# ---------------------------------------------------------------- the fake panel

class FakeVsSpecTest(unittest.TestCase):
    def setUp(self):
        clean_fake_env(self)
        fake_panel.reset()
        self.scratch = Path(tempfile.mkdtemp(prefix="ppro_spec_"))

    def tearDown(self):
        fake_panel.reset()
        shutil.rmtree(self.scratch, ignore_errors=True)

    def call(self, action, params):
        fake_panel.reset()
        return fake_panel.dispatch({"id": "t", "action": action, "params": localize(params, self.scratch)})

    def test_handlers_in_spec_order(self):
        self.assertEqual(list(fake_panel.HANDLERS), NAMES)

    def test_ping_reports_the_spec(self):
        data = self.call("ping", {})["data"]
        self.assertEqual(data["actions"], NAMES)
        self.assertEqual(data["protocol"], spec.protocol())
        self.assertEqual(data["modules"], "fake")
        self.assertEqual(set(data["capabilities"]), set(SPEC["capabilities"]))

    def test_every_action_has_an_example(self):
        self.assertEqual([a["name"] for a in ACTIONS if not a["example"]], [])

    def test_examples(self):
        for a in ACTIONS:
            if not a["example"]:
                continue
            with self.subTest(action=a["name"]):
                reply = self.call(a["name"], a["example"]["params"])
                self.assertTrue(reply["ok"], reply)
                data = reply["data"]
                self.assertLessEqual(set(a["reply"]), set(data))
                self.assertLessEqual(set(data), set(a["reply"]) | set(a["replyOptional"]) | set(a["fakeOnlyReply"]))
                for key, want in a["example"]["reply"].items():
                    self.assertTrue(same(data[key], want), f"{key}: {data[key]!r} != {want!r}")

    def test_error_examples(self):
        for a in ACTIONS:
            for i, e in enumerate(a["errorExamples"]):
                with self.subTest(action=a["name"], example=i, code=e["code"]):
                    reply = self.call(a["name"], e["params"])
                    self.assertFalse(reply["ok"], reply)
                    self.assertEqual((reply["code"], reply["outcome"]), (e["code"], e["outcome"]), reply["error"])
                    self.assertTrue(reply["error"])

    def test_vectors(self):
        for i, v in enumerate(BY_NAME["set_params"]["vectors"]):
            with self.subTest(vector=i):
                try:
                    got = fake_panel.media_seconds(v["clip"], v["frameSeconds"], v["timeBase"], v["t"])
                except fake_panel.Coded as e:
                    self.assertEqual(e.code, v.get("code"), str(e))
                else:
                    self.assertIn("mediaSeconds", v, f"expected {v.get('code')}, got {got}")
                    self.assertAlmostEqual(got, v["mediaSeconds"], places=9)

    def test_codes_in_the_fake_source_are_panel_errors(self):
        source = Path(fake_panel.__file__).read_text(encoding="utf-8")
        codes = set(re.findall(r'(?:Coded|fail)\(\s*"([A-Z_]+)"', source))
        self.assertTrue(codes)
        for code in sorted(codes):
            with self.subTest(code=code):
                self.assertIn(code, ERRORS)
                self.assertIn("panel", ERRORS[code]["source"])


# ---------------------------------------------------------------- the real panel, through the node harness

class PanelVsSpecTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not on PATH: panel-vs-spec checks need it")
        if not HARNESS.is_file():
            raise unittest.SkipTest("tests/panel/harness.js is missing")

    def harness(self, command, request=None):
        out = subprocess.run([self.node, str(HARNESS), command], input=json.dumps(request or {}),
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        reply = json.loads(out.stdout)
        self.assertTrue(reply.get("ok"), reply)
        return reply

    def test_describe(self):
        d = self.harness("describe")
        self.assertEqual(d["handlers"], NAMES)
        self.assertEqual(d["specActions"], NAMES)
        self.assertEqual(d["protocol"], spec.protocol())

    def test_timebase_vectors(self):
        vectors = BY_NAME["set_params"]["vectors"]
        results = self.harness("timebase", {"vectors": vectors})["results"]
        self.assertEqual(len(results), len(vectors))
        for i, (v, r) in enumerate(zip(vectors, results)):
            with self.subTest(vector=i):
                if "code" in v:
                    self.assertEqual(r.get("code"), v["code"], r)
                else:
                    self.assertIn("mediaSeconds", r, r)
                    self.assertAlmostEqual(r["mediaSeconds"], v["mediaSeconds"], places=9)

    def test_param_check_error_examples(self):
        """BAD_PARAMS examples the spec check itself catches (independent of the mock project)."""
        n = 0
        for a in ACTIONS:
            for i, e in enumerate(a["errorExamples"]):
                if e["code"] != "BAD_PARAMS" or not spec.check_params(a["name"], e["params"]):
                    continue
                n += 1
                with self.subTest(action=a["name"], example=i):
                    reply = self.harness("call", {"action": a["name"], "params": e["params"]})["reply"]
                    self.assertFalse(reply["ok"], reply)
                    self.assertEqual((reply.get("code"), reply.get("outcome")), ("BAD_PARAMS", "not_committed"), reply)
        self.assertGreater(n, 10)

    def test_examples_agree_with_the_fake(self):
        """Every example and errorExample through the panel (default mock scenario, the same demo project as the
        fake): the same ok / code / outcome as the fake, and reply keys within the spec."""
        clean_fake_env(self)
        scratch = Path(tempfile.mkdtemp(prefix="ppro_spec_panel_"))
        self.addCleanup(shutil.rmtree, scratch, True)
        for a in ACTIONS:
            cases = ([("example", a["example"]["params"])] if a["example"] else []) + \
                    [(f"error {i} {e['code']}", e["params"]) for i, e in enumerate(a["errorExamples"])]
            for label, params in cases:
                with self.subTest(action=a["name"], case=label):
                    params = localize(params, scratch)
                    fake_panel.reset()
                    want = fake_panel.dispatch({"id": "t", "action": a["name"], "params": params})
                    got = self.harness("call", {"action": a["name"], "params": params})["reply"]
                    self.assertEqual((got["ok"], got.get("code"), got.get("outcome")),
                                     (want["ok"], want.get("code"), want.get("outcome")),
                                     f"panel: {got.get('error')} / fake: {want.get('error')}")
                    if got["ok"]:
                        self.assertLessEqual(set(a["reply"]), set(got["data"]))
                        self.assertLessEqual(set(got["data"]), set(a["reply"]) | set(a["replyOptional"]))
        fake_panel.reset()

    def test_edits_leave_the_same_timeline_as_the_fake(self):
        """clone_clip / trim_clip / shift_items sequences on the interview clip (V1 / A1 0-60 s in both demo projects):
        every step answers alike, and the panel (mock semantics, as measured on Premiere 27.1 b17) and the fake end
        with the same clips on V1, V3, A1 and A2 - within a frame, since the fake snaps to the frame grid like
        Premiere and the mocks do not."""
        clean_fake_env(self)
        v1, a1 = {"kind": "video", "track": 0}, {"kind": "audio", "track": 0}
        split = [("clone_clip", dict(v1, atSeconds=20.0, offsetSeconds=70.0)),
                 ("trim_clip", dict(v1, atSeconds=19.0, end=20.0)),
                 ("trim_clip", dict(v1, atSeconds=75.0, start=90.0)),
                 ("trim_clip", dict(v1, atSeconds=95.0, moveTo=20.0))]
        sequences = {
            "head trims": [("trim_clip", dict(v1, atSeconds=5.0, start=2.0)),
                           ("trim_clip", dict(a1, atSeconds=5.0, inPoint=3.0))],
            "tail trims": [("trim_clip", dict(v1, atSeconds=5.0, end=30.0)),
                           ("trim_clip", dict(a1, atSeconds=5.0, outPoint=40.0))],
            "moves": [("trim_clip", dict(v1, atSeconds=5.0, moveTo=7.5)),
                      ("trim_clip", dict(a1, atSeconds=5.0, moveTo=7.5, start=8.0))],
            "clones": [("clone_clip", dict(a1, atSeconds=5.0, offsetSeconds=70.0)),
                       ("clone_clip", dict(v1, atSeconds=5.0, offsetSeconds=0.0, trackOffset=2)),
                       ("clone_clip", dict(a1, atSeconds=5.0, offsetSeconds=10.0, trackOffset=1))],
            "split, shift right and back": split + [
                ("shift_items", {"fromSeconds": 20.0, "deltaSeconds": 5.0, "videoTracks": [0], "audioTracks": []}),
                ("shift_items", {"fromSeconds": 25.0, "deltaSeconds": -5.0, "videoTracks": [0], "audioTracks": []}),
                ("shift_items", {"fromSeconds": 10.0, "deltaSeconds": -5.0, "videoTracks": [0], "audioTracks": [0]}),
                ("shift_items", {"fromSeconds": 61.0, "deltaSeconds": 3.0, "audioTracks": [0]})],
            "nothing lands before 0 s": [
                ("trim_clip", dict(v1, atSeconds=5.0, moveTo=-2.0)),
                ("trim_clip", dict(a1, atSeconds=5.0, start=-1.0)),
                ("clone_clip", dict(v1, atSeconds=5.0, offsetSeconds=-10.0)),
                ("shift_items", {"fromSeconds": 0.0, "deltaSeconds": -1.0, "videoTracks": [0], "audioTracks": [0]})],
        }
        tolerance = 1.5 / fake_panel.FPS

        def tracks(items):
            return {t["label"]: [(c["name"], c["start"], c["end"], c["inPoint"], c["outPoint"]) for c in t["items"]]
                    for kind in ("video", "audio") for t in items[kind] if t["label"] in ("V1", "V3", "A1", "A2")}

        for name, steps in sequences.items():
            with self.subTest(sequence=name):
                fake_panel.reset()
                want = [fake_panel.dispatch({"id": str(i), "action": a, "params": p}) for i, (a, p) in enumerate(steps)]
                if name == "nothing lands before 0 s":
                    self.assertEqual([w.get("code") for w in want],
                                     ["BAD_PARAMS", "BAD_PARAMS", "BAD_PARAMS", "PRECONDITION_FAILED"])
                want_items = fake_panel.dispatch({"id": "end", "action": "list_items", "params": {}})["data"]
                out = self.harness("batch", {"steps": [{"action": a, "params": p} for a, p in steps] +
                                             [{"action": "list_items", "params": {}}]})
                self.assertNotIn("violations", out)
                *got, got_items = out["replies"]
                for (action, _), w, g in zip(steps, want, got):
                    self.assertEqual((g["ok"], g.get("code"), g.get("outcome")), (w["ok"], w.get("code"), w.get("outcome")),
                                     f"{action}: panel {g.get('error')} / fake {w.get('error')}")
                fake_tracks, panel_tracks = tracks(want_items), tracks(got_items["data"])
                self.assertEqual(fake_tracks.keys(), panel_tracks.keys())
                for label in fake_tracks:
                    f, g = fake_tracks[label], panel_tracks[label]
                    self.assertEqual([c[0] for c in f], [c[0] for c in g], f"{label}: fake {f} / panel {g}")
                    for fc, gc in zip(f, g):
                        for fv, gv in zip(fc[1:], gc[1:]):
                            self.assertAlmostEqual(fv, gv, delta=tolerance, msg=f"{label}: fake {fc} / panel {gc}")
        fake_panel.reset()

    def test_unknown_action(self):
        reply = self.harness("call", {"action": "eval_script", "params": {}})["reply"]
        self.assertEqual((reply["ok"], reply.get("code"), reply.get("outcome")),
                         (False, "UNKNOWN_ACTION", "not_committed"))


# ---------------------------------------------------------------- the CLI

def _module(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


class CliVsSpecTest(unittest.TestCase):
    CALL = re.compile(r'(?:(?<![\w.])|(?<=transport\.)|(?<=self\.))(?:rpc|call)\(\s*["\']([A-Za-z_]+)["\']')

    def test_parser_commands_are_the_spec_commands(self):
        cli = _module("ppro_bridge.cli")
        if cli is None or not hasattr(cli, "build_parser"):
            self.skipTest("ppro_bridge.cli is not importable yet")
        parser = cli.build_parser()
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        self.assertEqual(len(subs), 1)
        self.assertEqual(sorted(subs[0].choices), sorted(spec.command_names()))

    def test_every_command_has_an_ops_function(self):
        ops = _module("ppro_bridge.ops")
        if ops is None:
            self.skipTest("ppro_bridge.ops is not importable yet")
        self.assertEqual([c for c in spec.command_names() if not callable(getattr(ops, c, None))], [])

    def test_literal_action_names_are_spec_actions(self):
        found = {}
        for path in sorted((REPO / "ppro_bridge").glob("*.py")):
            for name in self.CALL.findall(path.read_text(encoding="utf-8")):
                found.setdefault(name, set()).add(path.name)
        unknown = {n: sorted(f) for n, f in found.items() if n not in NAMES}
        self.assertEqual(unknown, {})
        if (REPO / "ppro_bridge" / "ops.py").is_file():
            self.assertGreater(len(found), 10, "the scan found almost no rpc(...) calls: is the pattern stale?")


if __name__ == "__main__":
    unittest.main()
