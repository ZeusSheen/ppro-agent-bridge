"""Panel tests: module rules and spec parity (static scans), and behaviour through the node harness
(tests/panel/harness.js loads plugin/main.js in a vm with Premiere mocks). Harness tests are skipped
when node is not installed. Nothing here talks to Premiere or a broker.

    python -m unittest discover -s tests -p "test_panel.py"
"""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ppro_bridge import spec as pspec  # noqa: E402  (stdlib-only module)

PLUGIN = REPO / "plugin"
LIB = PLUGIN / "lib"
HARNESS = REPO / "tests" / "panel" / "harness.js"
MOCKS = REPO / "tests" / "panel" / "mocks.js"
BUILD_PANEL = REPO / "scripts" / "build_panel.py"
NODE = shutil.which("node")
SPEC = json.loads((REPO / "ppro_bridge" / "actions.json").read_text(encoding="utf-8"))
ACTIONS = {a["name"]: a for a in SPEC["actions"]}

MAIN_REQUIRES = ["./lib/spec.js", "./lib/core.js", "./lib/timeline.js", "./lib/project.js", "./lib/effects.js",
                 "./lib/capabilities.js", "./lib/handlers_read.js", "./lib/handlers_edit.js",
                 "./lib/handlers_effects.js", "./lib/handlers_transcript.js", "./lib/probe.js", "./lib/smoke.js"]
REQUIRE_CALL = re.compile(r"""\brequire\(\s*["']([^"']+)["']\s*\)""")
SPEAKER = "6c1f0b7e-2d3a-4e5f-8a9b-0c1d2e3f4a5b"


def transcript_doc(words=("One", "two", "three")):
    toks = [{"text": w, "start": i * 0.5, "duration": 0.4, "eos": False, "confidence": 0.9, "tags": [], "type": "word"}
            for i, w in enumerate(words)]
    toks.append({"text": ".", "start": len(words) * 0.5, "duration": 0, "eos": True, "confidence": 1.0, "tags": [],
                 "type": "punctuation"})
    return {"language": "en-us", "speakers": [{"id": SPEAKER, "name": "Speaker 1"}],
            "segments": [{"start": 0.0, "duration": len(words) * 0.5, "language": "en-us", "speaker": SPEAKER,
                          "words": toks}]}


def code_hash(plugin_dir: Path):
    """Python mirror of the panel's codeHash (FNV-1a 32 over index.html, main.js, manifest.json, lib/*.js)."""
    files = ["index.html", "main.js", "manifest.json"]
    if not all((plugin_dir / f).is_file() for f in files):
        return None
    lib = plugin_dir / "lib"
    if lib.is_dir():
        files += [f"lib/{p.name}" for p in lib.glob("*.js") if p.is_file()]
    h = 0x811C9DC5
    for rel in sorted(files):
        for b in rel.encode("utf-8") + b"\0" + (plugin_dir / rel).read_bytes() + b"\0":
            h ^= b
            h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def panel_js_files():
    return [PLUGIN / "main.js"] + sorted(LIB.glob("*.js"))


def strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def harness(cmd, req=None, bundle=None, timeout=60):
    args = [NODE, str(HARNESS), cmd] + (["--bundle", str(bundle)] if bundle else [])
    p = subprocess.run(args, input=json.dumps(req if req is not None else {}), capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout)
    if p.returncode != 0:
        raise AssertionError(f"harness {cmd} exited {p.returncode}: {p.stdout}\n{p.stderr}")
    out = json.loads(p.stdout)
    assert out.get("ok") is True, out
    assert "violations" not in out, out["violations"]
    return out


def call(action, params=None, **extra):
    req = dict(extra, action=action)
    if params is not None:
        req["params"] = params
    return harness("call", req)


def batch(steps, **extra):
    return harness("batch", dict(extra, steps=steps))


def reply_ok(testcase, out):
    testcase.assertTrue(out["reply"]["ok"], out["reply"])
    return out["reply"]["data"]


RECORD_SCRIPT = r"""
const h = require(process.argv[1]);
const req = JSON.parse(process.argv[2]);
(async () => {
  const { bridge, mocks } = h.loadPanel(req, h.DEFAULT_MAIN);
  const replies = [];
  for (const step of req.steps) replies.push(await h.callOnce(bridge, step));
  const r = mocks.record;
  process.stdout.write(JSON.stringify({ replies, exports: r.exports, clones: r.clones, transactions: r.transactions,
                                        times: r.times, edges: mocks.edgeTicks(), violations: r.violations }));
  process.exit(0);
})();
"""


def recorded(steps, **extra):
    """Like batch, plus what the mocks recorded (exports, clone calls, the ticks of every time-taking action) and
    every track item's edges in ticks afterwards."""
    p = subprocess.run([NODE, "-e", RECORD_SCRIPT, str(HARNESS), json.dumps(dict(extra, steps=steps))],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"record script exited {p.returncode}: {p.stdout}\n{p.stderr}")
    out = json.loads(p.stdout)
    assert out["violations"] == [], out["violations"]
    return out


LEVEL_0DB = 0.17782793939113617  # Premiere's linear audio Level at 0 dB (measured on 27.1 b17)
VOLUME = "Internal Volume Mono"


def span(d):
    return [round(d[k], 4) for k in ("start", "end", "inPoint", "outPoint")]


FPS = 60000 / 1001     # the mock sequence (timebase 4237833600 ticks per frame)
FRAME_TICKS = 4237833600
TPS = 254016000000
NUDGE_TICKS = 254016   # core.TIME_NUDGE_S (1 us) in ticks


def g(s):
    """Seconds on the mock sequence's frame grid (the nearest frame): where the panel puts sequence edges."""
    return round(s * FPS) / FPS


def r4(*xs):
    return [round(x, 4) for x in xs]


class PanelStaticTest(unittest.TestCase):
    """Module rules and spec parity by scanning the sources (no node needed)."""

    def test_lib_modules_are_factories_without_requires(self):
        for path in sorted(LIB.glob("*.js")):
            text = path.read_text(encoding="utf-8")
            code = strip_comments(text)
            with self.subTest(file=path.name):
                self.assertNotIn("require(", code, "lib modules get their deps from main.js (no lib->lib requires)")
                self.assertNotIn("__dirname", code)
                self.assertIsNone(re.search(r"(?m)^\s*(import|export)\s", code), "no ES modules")
                self.assertIsNone(re.search(r"(?<!module\.)\bexports\b", code), "module.exports only")
                if path.name == "spec.js":
                    self.assertIn("module.exports = {", code)
                else:
                    self.assertRegex(code, r"module\.exports = function \(deps\) \{")

    def test_main_is_the_only_requirer_in_dependency_order(self):
        code = strip_comments((PLUGIN / "main.js").read_text(encoding="utf-8"))
        names = REQUIRE_CALL.findall(code)
        local = [n for n in names if n.startswith(".")]
        self.assertEqual(local, MAIN_REQUIRES)
        self.assertEqual(sorted(set(names) - set(local)), ["premierepro", "uxp"])
        self.assertNotIn("module.exports", code)
        self.assertNotIn("__dirname", code)
        self.assertIn("GLOBAL.__agentBridge = {", code)
        self.assertIn('typeof globalThis !== "undefined" ? globalThis', code)
        self.assertIn('"beforeunload"', code)

    def test_no_getKeyframePtr_anywhere_in_plugin(self):
        for path in PLUGIN.rglob("*"):
            if path.is_file():
                self.assertFalse(b"getKeyframePtr" in path.read_bytes(), f"getKeyframePtr in {path.name}")

    def test_error_codes_are_in_the_spec(self):
        known = set(SPEC["errors"])
        used = set()
        for path in panel_js_files():
            text = path.read_text(encoding="utf-8")
            used |= set(re.findall(r'BridgeError\(\s*"([A-Z_]+)"', text))
            used |= set(re.findall(r'fail\(\s*"([A-Z_]+)"', text))
        core = (LIB / "core.js").read_text(encoding="utf-8")
        listed = set(re.findall(r'"([A-Z][A-Z_]+)"', core.split("const ERROR_CODES = [", 1)[1].split("];", 1)[0]))
        self.assertTrue(used, "no codes found")
        self.assertLessEqual(used, listed, f"codes thrown but not in core.ERROR_CODES: {sorted(used - listed)}")
        self.assertLessEqual(listed, known, f"codes not in actions.json errors: {sorted(listed - known)}")
        self.assertLessEqual(set(SPEC["globalErrors"]), listed)

    def test_capability_table_mirrors_the_spec(self):
        text = (LIB / "capabilities.js").read_text(encoding="utf-8")
        table = text.split("const FLAGS = [", 1)[1].split("];", 1)[0]
        flags = re.findall(r'\["(\w+)", "(static|instance)"\]', table)
        self.assertEqual(flags, [(k, v["probe"]) for k, v in SPEC["capabilities"].items()])

    def test_my_files_use_lf(self):
        mine = panel_js_files() + [PLUGIN / "index.html", HARNESS, MOCKS, BUILD_PANEL, Path(__file__)]
        for path in mine:
            self.assertNotIn(b"\r", path.read_bytes(), path.name)


@unittest.skipIf(NODE is None, "node is not installed")
class PanelHarnessTest(unittest.TestCase):

    def test_node_check_every_plugin_file_and_the_harness(self):
        for path in panel_js_files() + [HARNESS, MOCKS]:
            p = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(p.returncode, 0, f"{path.name}: {p.stderr}")

    def test_describe_matches_the_spec(self):
        out = harness("describe", {})
        names = [a["name"] for a in SPEC["actions"]]
        self.assertEqual(out["handlers"], names)
        self.assertEqual(out["specActions"], names)
        self.assertEqual(out["protocol"], SPEC["protocol"])
        self.assertEqual(out["version"], (REPO / "VERSION").read_text(encoding="utf-8").strip())
        self.assertEqual(out["modules"], "commonjs")

    def test_harness_failure_exits_2_with_one_json_object(self):
        p = subprocess.run([NODE, str(HARNESS), "nonsense"], input="{}", capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 2)
        out = json.loads(p.stdout)
        self.assertIs(out["ok"], False)
        self.assertIn("unknown command", out["error"])

    def test_timebase_vectors(self):
        vectors = ACTIONS["set_params"]["vectors"]
        out = harness("timebase", {"vectors": vectors})
        self.assertEqual(len(out["results"]), len(vectors))
        for v, r in zip(vectors, out["results"]):
            with self.subTest(vector=v):
                if "code" in v:
                    self.assertEqual(r, {"code": v["code"]})
                else:
                    self.assertAlmostEqual(r["mediaSeconds"], v["mediaSeconds"], places=9)

    def test_bad_params_mirror_spec_py(self):
        cases = [
            ("get_project", {"foo": 1}),
            ("insert_clip", {}),
            ("insert_clip", {"itemName": 5}),
            ("insert_clip", {"itemName": "x", "inSeconds": 1}),
            ("insert_clip", {"itemName": "x", "mode": "replace"}),
            ("insert_clip", {"itemName": "x", "videoTrack": -1, "audioTrack": 1.5}),
            ("insert_clip", {"itemName": "x", "atSeconds": True}),
            ("remove_items", {"fromSeconds": 0, "toSeconds": 1}),
            ("remove_items", {"fromSeconds": 0, "toSeconds": 1, "videoTracks": [], "audioTracks": ["1"]}),
            ("set_params", {"videoTrack": 0, "atSeconds": 1,
                            "components": [{"matchName": "x", "bogus": 1, "occurrence": 0,
                                            "params": [{"index": "a", "timeBase": "frames",
                                                        "keyframes": [{"t": 1}, 5]}]}]}),
            ("set_fps", {"fps": 0}),
            ("import_media", {"paths": []}),
            ("add_marker", {"atSeconds": 1, "name": "n", "colorIndex": 9, "type": "Note"}),
            ("export_frame", {"dir": None, "width": 0}),
            ("list_items", {"kinds": ["video", 3, None]}),
            ("ping", {"probe": "yes"}),
            ("transcribe_clip", {"itemName": "a", "language": 7}),
        ]
        for action, params in cases:
            with self.subTest(action=action, params=params):
                reply = call(action, params)["reply"]
                expected = pspec.check_params(action, params)
                self.assertTrue(expected, "case must be invalid")
                self.assertEqual((reply["ok"], reply["code"], reply["outcome"]), (False, "BAD_PARAMS", "not_committed"))
                self.assertEqual(reply["data"]["problems"], expected)
        reply = call("get_project", [1])["reply"]
        self.assertEqual(reply["data"]["problems"], pspec.check_params("get_project", [1]))
        reply = call("get_project", {}, guard={"projectName": "x", "sequence": 3})["reply"]
        self.assertEqual(reply["code"], "BAD_PARAMS")
        self.assertEqual(reply["data"]["problems"], ["guard.projectName: unknown key", "guard.sequence: expected string, got int"])

    def test_empty_required_strings_are_refused_like_0_3_3(self):
        cases = [("export_frame", {"dir": ""}), ("export_otio", {"outputPath": ""}), ("insert_clip", {"itemName": ""}),
                 ("add_marker", {"atSeconds": 1, "name": ""}),
                 ("add_transition", {"videoTrack": 0, "atSeconds": 5, "matchName": "", "durationSeconds": 1}),
                 ("import_transcript", {"itemName": "broll_city.mp4", "transcriptJson": ""})]
        for action, params in cases:
            with self.subTest(action=action):
                out = call(action, params)
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("BAD_PARAMS", "not_committed"))
                self.assertEqual(out["transactions"], [])
        r = call("export_frame", {"dir": "/fake/no/such/folder"})["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("EXPORT_FAILED", "not_committed"))

    def test_dispatch_order(self):
        table = {"denied": [{"match": "27.1.*", "reason": "test denial"}]}
        r = call("no_such_action", {"x": 1}, stuck=True, hostTable=table)["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("UNKNOWN_ACTION", "not_committed"))
        r = call("get_project", {"bogus": 1}, stuck=True, hostTable=table)["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("PANEL_STUCK", "not_committed"))
        r = call("get_project", {"bogus": 1}, hostTable=table)["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("HOST_DENIED", "not_committed"))
        r = call("transcribe_clip", {}, scenario="26.4")["reply"]
        self.assertEqual(r["code"], "BAD_PARAMS")
        r = call("transcribe_clip", {"itemName": "broll_city.mp4"}, scenario="26.4")["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("UNSUPPORTED", "not_committed"))
        d = reply_ok(self, call("ping", {}, stuck=True, hostTable=table))
        self.assertEqual(d["stuck"]["action"], "insert_clip")
        self.assertEqual(d["denied"]["reason"], "test denial")

    def test_guards(self):
        cases = [
            ("get_project", {}, {"project": "Other"}, "PROJECT_MISMATCH"),
            ("get_project", {}, {"projectPrefix": "PAB_TEST"}, None),
            ("get_project", {}, {"projectPrefix": "LIVE_"}, "PROJECT_MISMATCH"),
            ("get_project", {}, {"projectGuid": "harness-project-guid"}, None),
            ("get_project", {}, {"projectGuid": "other-guid"}, "PROJECT_MISMATCH"),
            ("get_sequence", {}, {"sequence": "Other"}, "SEQUENCE_MISMATCH"),
            ("get_sequence", {}, {"sequence": "Sequence 01", "sequenceGuid": "harness-sequence-guid"}, None),
            ("list_project_items", {}, {"sequenceGuid": "other-guid"}, "SEQUENCE_MISMATCH"),
            ("list_transition_names", {}, {"project": "Other"}, "PROJECT_MISMATCH"),
            ("list_transcripts", {"jobsOnly": True}, {"project": "Other"}, "PROJECT_MISMATCH"),
            ("ping", {}, {"project": "Other"}, None),
        ]
        for action, params, guard, code in cases:
            with self.subTest(action=action, guard=guard):
                r = call(action, params, guard=guard)["reply"]
                if code is None:
                    self.assertTrue(r["ok"], r)
                else:
                    self.assertEqual((r["ok"], r["code"], r["outcome"]), (False, code, "not_committed"))
        out = call("import_media", {"paths": ["/fake/x.mp4"], "bin": "New"}, guard={"sequenceGuid": "other-guid"})
        self.assertEqual(out["reply"]["code"], "SEQUENCE_MISMATCH")
        self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))
        r = call("import_media", {"paths": ["/fake/x.mp4"]}, guard={"sequence": "Sequence 01"}, scenario="no-sequence")
        self.assertEqual(r["reply"]["code"], "SEQUENCE_MISMATCH")
        r = call("get_sequence", {}, scenario="no-sequence")["reply"]
        self.assertEqual(r["code"], "NO_SEQUENCE")
        r = call("get_project", {}, scenario="no-project")["reply"]
        self.assertEqual(r["code"], "NO_PROJECT")

    def test_outcome_rule(self):
        # an error after a committed transaction -> unknown
        out = call("add_marker", {"atSeconds": 3, "name": "m", "colorIndex": 2}, mock={"failTransactions": ["[color]"]})
        self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("TRANSACTION_FAILED", "unknown"))
        self.assertEqual(out["transactions"], ['Agent: marker "m" @3s'])
        self.assertEqual(out["sideEffects"], 2)
        # the place step failed but the range was cleared: timeline unchanged -> explicit not_committed
        out = call("insert_clip", {"itemName": "broll_city.mp4", "atSeconds": 20, "videoTrack": 2, "audioTrack": 2,
                                   "inSeconds": 1, "outSeconds": 3, "undoLabel": "t"},
                   mock={"failTransactions": ["[2/3 place]"]})
        self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("TRANSACTION_FAILED", "not_committed"))
        self.assertIn("[place on timeline]", out["reply"]["error"])
        self.assertEqual(out["transactions"], ["t [1/3 set range]", "t [3/3 clear range]"])
        # refused before any write
        out = call("insert_clip", {"itemName": "broll_city.mp4", "videoTrack": 9})
        self.assertEqual((out["reply"]["code"], out["reply"]["outcome"], out["transactions"]), ("NOT_FOUND", "not_committed", []))
        out = call("insert_clip", {"itemName": "broll_city.mp4", "inSeconds": 3, "outSeconds": 1})
        self.assertEqual(out["reply"]["code"], "BAD_PARAMS")
        out = call("insert_clip", {"itemName": "title.mov"})
        self.assertEqual(out["reply"]["code"], "AMBIGUOUS")
        # read actions never report unknown
        r = call("export_frame", {"dir": "/fake/out"}, mock={"exportFails": True})["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("EXPORT_FAILED", "not_committed"))
        r = call("inspect_items", {"videoTracks": [9]})["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("NOT_FOUND", "not_committed"))

    def test_set_params_validates_everything_before_the_first_transaction(self):
        blur = {"matchName": "AE.ADBE Gaussian Blur 2", "params": [{"index": 0, "value": 3}]}
        bad = [
            ([blur, {"matchName": "AE.ADBE Opacity", "params": [{"index": 7, "value": 1}]}], "NOT_FOUND"),
            ([blur, {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "timeBase": "clip",
                                                                 "keyframes": [{"t": 999, "value": 0}]}]}],
             "KEYFRAME_OUT_OF_RANGE"),
            ([blur, {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "keyframes": [{"t": 0, "value": 0}]}]}],
             "BAD_PARAMS"),
            ([blur, {"matchName": "AE.ADBE Opacity", "params": [{"index": 0}]}], "BAD_PARAMS"),
            ([blur, {"matchName": "AE.ADBE Motion", "params": [{"index": 0, "value": [1, 2, 3]}]}], "BAD_PARAMS"),
            ([blur, {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "value": 1}, {"index": 0, "value": 2}]}],
             "BAD_PARAMS"),
            ([blur, {"matchName": "AE.No Such Effect", "params": [{"index": 0, "value": 1}]}], "NOT_FOUND"),
            ([dict(blur, occurrence=40)], "BAD_PARAMS"),
        ]
        for components, code in bad:
            with self.subTest(code=code, components=components):
                out = call("set_params", {"videoTrack": 0, "atSeconds": 5, "components": components})
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), (code, "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))
        # V2 holds a clip whose Motion > Rotation (#4) is keyframed: static values and keyframes are refused
        for p in ({"index": 4, "value": 10}, {"index": 4, "timeBase": "media", "keyframes": [{"t": 3, "value": 1}]}):
            out = call("set_params", {"videoTrack": 1, "atSeconds": 12, "components": [
                blur, {"matchName": "AE.ADBE Motion", "params": [p]}]})
            self.assertEqual((out["reply"]["code"], out["transactions"]), ("KEYFRAMED", []))
        # speed != 1 is checked per time base (vectors); a clip at no clip: NOT_FOUND
        out = call("set_params", {"videoTrack": 2, "atSeconds": 5, "components": [blur]})
        self.assertEqual(out["reply"]["code"], "NOT_FOUND")

    def test_set_params_occurrence_time_bases_and_read_back(self):
        example = ACTIONS["set_params"]["example"]["params"]
        steps = [
            {"action": "set_params", "params": example},
            {"action": "set_params", "params": {"videoTrack": 1, "atSeconds": 12, "undoLabel": "seq", "components": [
                {"matchName": "AE.ADBE Opacity", "params": [
                    {"index": 0, "timeBase": "sequence", "keyframes": [{"t": 10.5, "value": 0}, {"t": 14.99, "value": 100}]}]},
                {"matchName": "AE.ADBE Motion", "params": [{"index": 0, "value": {"x": 100, "y": 200}},
                                                           {"index": 1, "value": {"value": 80}}]}]}},
            {"action": "inspect_items", "params": {"videoTracks": [0, 1]}},
        ]
        out = batch(steps)
        first, second, inspect = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        self.assertEqual(first["createdEffects"], ["AE.ADBE Gaussian Blur 2"] * 2)
        self.assertEqual(first["set"], ["AE.ADBE Opacity[1]#0 Opacity", "AE.ADBE Gaussian Blur 2[2]#0 Blurriness"])
        self.assertEqual(first["keyframes"], [{"matchName": "AE.ADBE Opacity", "occurrence": 1, "index": 0,
                                               "timeBase": "clip", "mediaSeconds": [0, 0.5]}])
        self.assertEqual(second["keyframes"][0]["timeBase"], "sequence")
        for got, want in zip(second["keyframes"][0]["mediaSeconds"], [2.5, 6.99]):
            self.assertAlmostEqual(got, want, places=9)
        self.assertEqual(out["transactions"], ["Agent: fade in [add effects]", "Agent: fade in", "seq"])
        v1, v2 = inspect["tracks"]
        comps = v1["clips"][0]["components"]
        self.assertEqual([(c["matchName"], c["occurrence"]) for c in comps],
                         [("AE.ADBE Motion", 1), ("AE.ADBE Opacity", 1),
                          ("AE.ADBE Gaussian Blur 2", 1), ("AE.ADBE Gaussian Blur 2", 2)])
        self.assertEqual([comps[2]["params"][0]["value"], comps[3]["params"][0]["value"]], [0, 12])
        opacity = comps[1]["params"][0]
        self.assertEqual((opacity["keyframed"], opacity["timeBase"]), (True, "media"))
        self.assertEqual([k["t"] for k in opacity["keyframes"]], [0, 0.5])
        motion2 = v2["clips"][0]["components"][0]["params"]
        self.assertEqual(motion2[0]["value"], {"x": 100, "y": 200})
        self.assertEqual(motion2[1]["value"], 80)
        rotation = motion2[4]
        self.assertEqual((rotation["keyframed"], rotation["timeBase"]), (True, "media"))
        self.assertEqual([k["t"] for k in rotation["keyframes"]], [3, 5])
        self.assertNotIn("timeBase", motion2[1])

    def test_set_params_dry_run_validates_and_commits_nothing(self):
        blur = {"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 2, "params": [{"index": 0, "value": 3}]}
        out = call("set_params", {"videoTrack": 0, "atSeconds": 5, "dryRun": True, "components": [blur]})
        d = reply_ok(self, out)
        self.assertEqual((d["dryRun"], d["createdEffects"], len(d["set"])), (True, ["AE.ADBE Gaussian Blur 2"] * 2, 1))
        self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))
        # what replace_clip asks before removing a clip: can one more of this component be created?
        for name, code in (("AE.ADBE CompoundComponent", "NOT_FOUND"), ("AE.ADBE Crop", None)):
            out = call("set_params", {"videoTrack": 0, "atSeconds": 5, "dryRun": True,
                                      "components": [{"matchName": name, "occurrence": 1, "params": []}]})
            self.assertEqual((out["reply"].get("code"), out["transactions"], out["sideEffects"]), (code, [], 0), name)

    def test_add_transition_says_whether_premiere_added_one(self):
        p = {"videoTrack": 0, "atSeconds": 5, "matchName": "AE.ADBE Cross Dissolve New", "durationSeconds": 1,
             "applyToStart": True}
        out = batch([{"action": "add_transition", "params": p},
                     {"action": "add_transition", "params": dict(p, matchName="AE.ADBE Dip To Black")}])
        first, second = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        self.assertEqual((first["added"], first["transitionCount"]), (True, {"before": 0, "after": 1}))
        # Premiere 27.1 keeps a transition already at the edit point (19 Oct 2026): nothing changes
        self.assertEqual((second["added"], second["transitionCount"]), (False, {"before": 1, "after": 1}))

    def test_import_transcript(self):
        tj = json.dumps(transcript_doc())
        d = reply_ok(self, call("import_transcript", {"itemName": "broll_city.mp4", "transcriptJson": tj}))
        self.assertEqual(d, {"imported": True, "itemName": "broll_city.mp4", "binPath": "", "method": "static",
                             "replaced": False, "wordsIn": 3, "wordsOut": 3})
        r = call("import_transcript", {"itemName": "interview.mp4", "transcriptJson": tj})["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("TRANSCRIPT_EXISTS", "not_committed"))
        d = reply_ok(self, call("import_transcript", {"itemName": "interview.mp4", "transcriptJson": tj, "replace": True}))
        self.assertTrue(d["replaced"])
        out = call("import_transcript", {"itemName": "broll_city.mp4", "transcriptJson": "{not json"})
        self.assertEqual((out["reply"]["code"], out["transactions"]), ("BAD_PARAMS", []))
        r = call("import_transcript", {"itemName": "title.mov", "transcriptJson": tj})["reply"]
        self.assertEqual(r["code"], "AMBIGUOUS")
        d = reply_ok(self, call("import_transcript", {"itemName": "title.mov", "binPath": "EN", "transcriptJson": tj}))
        self.assertEqual(d["binPath"], "EN")
        # Premiere's "empty shell" import: static and callback form both leave no words
        out = call("import_transcript", {"itemName": "broll_city.mp4", "transcriptJson": tj}, scenario="transcript-empty-import")
        r = out["reply"]
        self.assertEqual((r["ok"], r["code"], r["outcome"]), (False, "TRANSCRIPT_NOT_IMPORTED", "committed"))
        self.assertEqual((r["data"]["method"], r["data"]["wordsIn"], r["data"]["wordsOut"]), ("callback", 3, 0))
        self.assertEqual(len(out["transactions"]), 2)
        d = reply_ok(self, call("import_transcript", {"itemName": "broll_city.mp4", "transcriptJson": tj},
                                scenario="transcript-callback-import"))
        self.assertEqual((d["method"], d["wordsOut"]), ("callback", 3))

    def test_export_transcript(self):
        d = reply_ok(self, call("export_transcript", {}))
        self.assertEqual((d["itemName"], d["binPath"]), ("interview.mp4", ""))
        self.assertGreater(len(json.loads(d["transcriptJson"])["segments"]), 0)
        self.assertEqual(call("export_transcript", {"itemName": "broll_city.mp4"})["reply"]["code"], "NO_TRANSCRIPT")
        self.assertEqual(call("export_transcript", {"itemName": "title.mov"})["reply"]["code"], "AMBIGUOUS")
        self.assertEqual(call("export_transcript", {"itemName": "nope.mp4"})["reply"]["code"], "NOT_FOUND")
        out = batch([
            {"action": "import_transcript", "params": {"itemName": "broll_city.mp4", "transcriptJson": json.dumps(transcript_doc())}},
            {"action": "export_transcript", "params": {"itemName": "broll_city.mp4"}},
        ], scenario="transcript-empty-import")
        self.assertEqual(out["replies"][1]["code"], "NO_TRANSCRIPT")

    def test_transcribe_clip_pending_job(self):
        out = batch([
            {"action": "transcribe_clip", "params": {"itemName": "broll_city.mp4", "language": "EN-us"}},
            {"action": "list_transcripts", "params": {"jobsOnly": True}},
            {"action": "transcribe_clip", "params": {"itemName": "broll_city.mp4"}},
            {"action": "set_fps", "params": {"items": [{"name": "broll_city.mp4"}], "fps": 25}},
            {"action": "import_transcript", "params": {"itemName": "broll_city.mp4",
                                                       "transcriptJson": json.dumps(transcript_doc())}},
            {"action": "list_transcripts", "params": {}},
        ], scenario="stt-pending")
        started, jobs, again, fps, imp, full = out["replies"]
        self.assertTrue(started["ok"], started)
        self.assertEqual({k: started["data"][k] for k in ("started", "itemName", "binPath", "language")},
                         {"started": True, "itemName": "broll_city.mp4", "binPath": "", "language": "en-US"})
        self.assertNotIn("clips", jobs["data"])
        job = jobs["data"]["jobs"][0]
        self.assertEqual((job["jobId"], job["state"], job["hasTranscript"], job["language"]),
                         (started["data"]["jobId"], "running", False, "en-US"))
        for r in (again, fps, imp):
            self.assertEqual((r["code"], r["outcome"]), ("TRANSCRIBE_RUNNING", "not_committed"))
        self.assertEqual(len(full["data"]["jobs"]), 1)
        self.assertIn({"name": "broll_city.mp4", "bin": "", "hasTranscript": False}, full["data"]["clips"])
        self.assertEqual(out["transactions"], [])

    def test_transcribe_clip_done_and_refusals(self):
        out = batch([
            {"action": "transcribe_clip", "params": {"itemName": "broll_city.mp4"}},
            {"action": "list_transcripts", "params": {"jobsOnly": True}},
        ])
        started, jobs = out["replies"]
        self.assertIsNone(started["data"]["language"])
        job = jobs["data"]["jobs"][0]
        self.assertEqual((job["state"], job["hasTranscript"], job["error"]), ("done", True, None))
        cases = [
            ({"itemName": "interview.mp4"}, {}, "TRANSCRIPT_EXISTS"),
            ({"itemName": "broll_city.mp4", "language": "xx-XX"}, {}, "BAD_PARAMS"),
            ({"itemName": "broll_city.mp4", "language": "ja-jp"}, {}, "LANGUAGE_UNAVAILABLE"),
            ({"itemName": "Sequence 01"}, {}, "UNSUPPORTED"),
            ({"itemName": "title.mov"}, {}, "AMBIGUOUS"),
            ({"itemName": "broll_city.mp4"}, {"mock": {"sttReject": True}}, "TRANSCRIBE_FAILED"),
        ]
        for params, extra, code in cases:
            with self.subTest(params=params):
                r = call("transcribe_clip", params, **extra)["reply"]
                self.assertEqual((r["ok"], r["code"], r["outcome"]), (False, code, "not_committed"))

    def test_denied_host_is_ping_only(self):
        table = {"denied": [{"match": "27.1.*", "reason": "known bad build"}], "quirks": {}}
        d = reply_ok(self, call("ping", {}, hostTable=table))
        self.assertEqual(d["denied"], {"match": "27.1.*", "reason": "known bad build"})
        for action, params in (("get_project", {}), ("list_transition_names", {}), ("insert_clip", {"itemName": "x"})):
            r = call(action, params, hostTable=table)["reply"]
            self.assertEqual((r["code"], r["outcome"]), ("HOST_DENIED", "not_committed"))
        caps = harness("capabilities", {"hostTable": table})
        self.assertEqual(caps["denied"]["reason"], "known bad build")
        self.assertEqual(caps["capabilities"]["razor"], "unknown")  # no probe on a denied build
        self.assertIs(caps["capabilities"]["transcribe"], True)
        ranged = {"denied": [{"match": "27.*", "builds": "5-10", "reason": "r"}]}
        self.assertEqual(harness("capabilities", {"hostTable": ranged, "buildNumber": 7})["denied"],
                         {"match": "27.*", "builds": "5-10", "reason": "r"})
        self.assertIs(harness("capabilities", {"hostTable": ranged, "buildNumber": 11})["denied"], False)
        self.assertIs(harness("capabilities", {"hostTable": ranged})["denied"], False)  # no build number: no match
        self.assertIs(harness("capabilities", {"hostTable": {"denied": [{"match": "26.*", "reason": "r"}]}})["denied"], False)
        self.assertIs(harness("capabilities", {})["denied"], False)

    def test_capabilities_per_scenario(self):
        flags = list(SPEC["capabilities"])
        caps = harness("capabilities", {})
        self.assertEqual(list(caps["capabilities"]), flags)
        self.assertTrue(all(v is True for v in caps["capabilities"].values()), caps["capabilities"])
        self.assertEqual(caps["host"], "premierepro 27.1.0")
        self.assertEqual(caps["hostInfo"], {"name": "premierepro", "version": "27.1.0", "uiLocale": "en_US"})
        old = harness("capabilities", {"scenario": "26.4"})["capabilities"]
        missing = {"razor", "razorAllTracks", "transcribe", "mogrtText", "workArea", "timecode", "componentInstanceName"}
        self.assertEqual({k for k, v in old.items() if v is False}, missing)
        self.assertTrue(all(old[k] is True for k in flags if k not in missing))
        statics = {k for k, v in SPEC["capabilities"].items() if v["probe"] == "static"}
        none = harness("capabilities", {"scenario": "no-project"})["capabilities"]
        self.assertTrue(all(none[k] is True for k in statics))
        self.assertTrue(all(none[k] == "unknown" for k in flags if k not in statics))
        noseq = harness("capabilities", {"scenario": "no-sequence"})["capabilities"]
        self.assertEqual({k for k, v in noseq.items() if v is True} - statics, {"projectItemId", "subclip", "removeProjectItem"})
        self.assertEqual(harness("capabilities", {"buildNumber": 42})["hostInfo"]["buildNumber"], 42)

    def test_ping_fields_and_code_hash(self):
        out = call("ping", {"probe": True})
        d = reply_ok(self, out)
        spec_ping = ACTIONS["ping"]
        self.assertLessEqual(set(spec_ping["reply"]), set(d))
        self.assertLessEqual(set(d), set(spec_ping["reply"]) | set(spec_ping["replyOptional"]))
        self.assertNotIn("applicationPath", json.dumps(out))
        self.assertEqual((d["protocol"], d["modules"], d["stuck"], d["denied"]), (2, "commonjs", False, False))
        self.assertEqual(d["actions"], [a["name"] for a in SPEC["actions"]])
        self.assertIsInstance(d["loadedAt"], int)
        self.assertIsInstance(d["capabilitiesProbedAt"], int)
        self.assertEqual(d["codeHash"], code_hash(PLUGIN))
        self.assertRegex(d["codeHash"], r"^[0-9a-f]{8}$")
        try:
            from ppro_bridge import panelhash
        except ImportError:
            return
        self.assertEqual(panelhash.code_hash(PLUGIN), d["codeHash"])

    def test_reply_keys_match_the_spec(self):
        tj = json.dumps(transcript_doc())
        params = {
            "ping": {}, "get_project": {}, "get_sequence": {}, "list_items": {}, "inspect_items": {},
            "list_project_items": {}, "export_frame": {"dir": "/fake/out"}, "export_otio": {"outputPath": "/fake/t.otio"},
            "list_transcripts": {}, "export_transcript": {},
            "import_transcript": {"itemName": "broll_city.mp4", "transcriptJson": tj},
            "transcribe_clip": {"itemName": "broll_city.mp4"},
            "import_media": {"paths": ["/fake/new.mp4"]},
            "insert_clip": {"itemName": "broll_city.mp4", "videoTrack": 2, "audioTrack": 2},
            "set_scale": {"videoTrack": 0, "fromSeconds": 5, "value": 50},
            "set_params": {"videoTrack": 0, "atSeconds": 5, "components": [
                {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "value": 50}]}]},
            "list_transition_names": {},
            "add_transition": {"videoTrack": 0, "atSeconds": 5, "matchName": "AE.ADBE Cross Dissolve New", "durationSeconds": 1},
            "set_fps": {"items": [{"name": "broll_city.mp4"}], "fps": 25},
            "add_marker": {"atSeconds": 2, "name": "flag"},
            "remove_items": {"videoTracks": [1], "fromSeconds": 9, "toSeconds": 16},
            "clone_clip": {"kind": "audio", "track": 0, "atSeconds": 5, "offsetSeconds": 70},
            "trim_clip": {"kind": "video", "track": 1, "atSeconds": 12, "end": 14},
            "export_sequence": {"outputPath": "/fake/out/mix.wav", "presetPath": "/fake/presets/wav.epr"},
            "shift_items": {"fromSeconds": 5, "deltaSeconds": 3},
            "probe_api": {},
            "api_smoke": {"case": "clip_props", "videoTrack": 1, "atSeconds": 12},
        }
        self.assertEqual(sorted(params), sorted(ACTIONS))
        for action, p in params.items():
            with self.subTest(action=action):
                d = reply_ok(self, call(action, p))
                a = ACTIONS[action]
                self.assertLessEqual(set(a["reply"]), set(d))
                self.assertLessEqual(set(d), set(a["reply"]) | set(a["replyOptional"]))

    # ---- v0.4b: clone_clip / trim_clip / export_sequence, audio chains (V2 holds broll 10-15 s, source 2-7 s)

    def test_trim_clip_semantics(self):
        """Head trims (start, inPoint) move start and in point together; tail trims (end, outPoint) move end and
        out point; moveTo is an absolute start the panel turns into Premiere's relative move."""
        base = {"kind": "video", "track": 1, "atSeconds": 12}
        cases = [  # sequence times land on the nearest frame (59.94); source times move by their own delta
            ({"start": 11}, r4(g(11), 15, 2 + g(11) - 10, 7)),
            ({"inPoint": 3}, [11, 15, 3, 7]),
            ({"end": 14}, r4(10, g(14), 2, 7 + g(14) - 15)),
            ({"outPoint": 6}, [10, 14, 2, 6]),
            ({"moveTo": 20}, r4(g(20), g(20) + 5, 2, 7)),
            ({"moveTo": 4}, r4(g(4), g(4) + 5, 2, 7)),
            ({"start": 11, "end": 14}, r4(g(11), g(14), 2 + g(11) - 10, 7 + g(14) - 15)),
            ({"inPoint": 2.5, "outPoint": 6.5}, [10.5, 14.5, 2.5, 6.5]),
        ]
        for fields, want in cases:
            with self.subTest(fields=fields):
                out = call("trim_clip", dict(base, **fields))
                d = reply_ok(self, out)
                self.assertEqual(span(d["before"]), [10, 15, 2, 7])
                self.assertEqual(span(d["after"]), want)
                self.assertEqual((d["fields"], d["kind"], d["track"]), (list(fields), "video", 1))
                self.assertEqual(out["transactions"], [f"Agent: trim broll_city.mp4 on V2 ({', '.join(fields)})"])
        out = batch([{"action": "trim_clip", "params": dict(base, end=14, undoLabel="tail")},
                     {"action": "list_items", "params": {"kinds": ["video"]}}])
        self.assertEqual(out["transactions"], ["tail"])
        self.assertEqual(span(out["replies"][1]["data"]["video"][1]["items"][0]), r4(10, g(14), 2, 7 + g(14) - 15))

    def test_trim_clip_refusals(self):
        base = {"kind": "video", "track": 1, "atSeconds": 12}
        out = call("trim_clip", base)
        self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("BAD_PARAMS", "not_committed"))
        self.assertEqual(out["reply"]["data"]["problems"], pspec.check_params("trim_clip", base))
        for params, code in ((dict(base, start=11, moveTo=20), "BAD_PARAMS"),
                             (dict(base, atSeconds=30, end=31), "NOT_FOUND"),
                             (dict(base, track=9, end=14), "NOT_FOUND"),
                             (dict(base, kind="audio", track=1, end=14), "NOT_FOUND")):
            with self.subTest(params=params):
                out = call("trim_clip", params)
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), (code, "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))
        out = call("trim_clip", dict(base, end=14), mock={"failTransactions": ["Agent: trim"]})
        self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("TRANSACTION_FAILED", "unknown"))

    def test_clone_clip_copies_one_item_with_its_effects(self):
        out = recorded([
            {"action": "set_params", "params": {"videoTrack": 1, "atSeconds": 12, "components": [
                {"matchName": "AE.ADBE Motion", "params": [{"index": 1, "value": 120}]}]}},
            {"action": "clone_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12, "offsetSeconds": 20}},
            {"action": "clone_clip", "params": {"kind": "video", "track": 0, "atSeconds": 5, "offsetSeconds": 70,
                                                "undoLabel": "past the end"}},
            {"action": "inspect_items", "params": {"videoTracks": [1]}},
            {"action": "list_items", "params": {}},
        ])
        sp, clone, clone2, inspect, items = out["replies"]
        self.assertTrue(all(r["ok"] for r in out["replies"]), out["replies"])
        d = clone["data"]
        self.assertEqual((d["cloned"], d["kind"], d["track"], d["start"], d["end"]), (True, "video", 1, 30, 35))
        self.assertEqual(span(d["source"]), [10, 15, 2, 7])
        self.assertEqual(d["undoLabel"], "Agent: clone broll_city.mp4 V2 by 20s")
        self.assertEqual(out["transactions"][1:], ["Agent: clone broll_city.mp4 V2 by 20s", "past the end"])
        self.assertEqual([(c["kind"], c["from"], c["to"], c["vOff"], c["aOff"], c["alignToVideo"], c["isInsert"])
                          for c in out["clones"]], [("video", 1, 1, 0, 0, True, False), ("video", 0, 0, 0, 0, True, False)])
        self.assertAlmostEqual(out["clones"][0]["offset"], g(20), places=9)  # whole frames
        original, copy_ = inspect["data"]["tracks"][0]["clips"]
        self.assertEqual(span(copy_), r4(10 + g(20), 15 + g(20), 2, 7))
        self.assertEqual(copy_["components"], original["components"])  # Scale 120 and the Rotation keyframes
        self.assertEqual(copy_["components"][0]["params"][1]["value"], 120)
        self.assertEqual([k["t"] for k in copy_["components"][0]["params"][4]["keyframes"]], [3, 5])
        tracks = items["data"]
        self.assertEqual([span(c) for c in tracks["video"][0]["items"]], [[0, 60, 0, 60], r4(g(70), 60 + g(70), 0, 60)])
        self.assertEqual(len(tracks["audio"][0]["items"]), 1)  # the linked audio is not cloned with it

    def test_clone_clip_overwrites_and_refuses_a_missing_track(self):
        out = batch([
            {"action": "clone_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12, "offsetSeconds": 10,
                                                "trackOffset": -1}},
            {"action": "list_items", "params": {"kinds": ["video"]}},
        ])
        self.assertEqual(out["replies"][0]["data"]["track"], 0)
        v1 = out["replies"][1]["data"]["video"][0]["items"]
        at = 10 + g(10)  # whole-frame offset
        self.assertEqual([(c["name"], span(c)) for c in v1], [("interview.mp4", r4(0, at, 0, at)),
                                                             ("broll_city.mp4", r4(at, at + 5, 2, 7)),
                                                             ("interview.mp4", r4(at + 5, 60, at + 5, 60))])
        for params in ({"kind": "video", "track": 1, "atSeconds": 12, "offsetSeconds": 10, "trackOffset": 9},
                       {"kind": "audio", "track": 0, "atSeconds": 5, "offsetSeconds": 10, "trackOffset": -1},
                       {"kind": "video", "track": 1, "atSeconds": 30, "offsetSeconds": 10}):
            with self.subTest(params=params):
                out = call("clone_clip", params)
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("NOT_FOUND", "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))

    def test_export_sequence(self):
        out = recorded([
            {"action": "export_sequence", "params": {"outputPath": "/fake/out/mix.wav", "presetPath": "/fake/wav.epr"}},
            {"action": "export_sequence", "params": {"outputPath": "/fake/out/wa.wav", "presetPath": "/fake/wav.epr",
                                                     "workAreaOnly": True}},
        ])
        full, work = out["replies"]
        self.assertEqual(full["data"], {"exported": True, "outputPath": "/fake/out/mix.wav", "preset": "/fake/wav.epr"})
        self.assertTrue(work["ok"], work)
        self.assertEqual([(e["kind"], e["type"], e["outputPath"], e["preset"], e["exportFull"]) for e in out["exports"]],
                         [("sequence", "IMMEDIATELY", "/fake/out/mix.wav", "/fake/wav.epr", True),
                          ("sequence", "IMMEDIATELY", "/fake/out/wa.wav", "/fake/wav.epr", False)])
        self.assertEqual(out["transactions"], [])
        good = {"outputPath": "/fake/out/mix.wav", "presetPath": "/fake/wav.epr"}
        for params, extra, code in (({"outputPath": "/fake/out/mix.wav"}, {}, "BAD_PARAMS"),
                                    (dict(good, presetPath=""), {}, "BAD_PARAMS"),
                                    (dict(good, outputPath=""), {}, "BAD_PARAMS"),
                                    (good, {"mock": {"exportThrows": True}}, "EXPORT_FAILED"),
                                    (good, {"mock": {"exportFails": True}}, "EXPORT_FAILED"),
                                    (dict(good, presetPath="/fake/no/such/wav.epr"), {}, "EXPORT_FAILED"),
                                    (good, {"scenario": "no-sequence"}, "NO_SEQUENCE")):
            with self.subTest(params=params, extra=extra):
                out = call("export_sequence", params, **extra)
                r = out["reply"]
                self.assertEqual((r["ok"], r["code"], r["outcome"]), (False, code, "not_committed"))
                self.assertEqual(out["sideEffects"], 0)
                if extra.get("mock", {}).get("exportThrows"):
                    self.assertIn("Unable to initialize export!", r["error"])

    def test_shift_items(self):
        """Clips starting at/after fromSeconds move by deltaSeconds in one undo step (Premiere's relative move);
        a left shift needs the room."""
        clone = {"action": "clone_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12, "offsetSeconds": 10}}
        listing = {"action": "list_items", "params": {}}
        out = batch([clone, {"action": "shift_items", "params": {"fromSeconds": 10, "deltaSeconds": -4,
                                                                  "videoTracks": [1], "audioTracks": [],
                                                                  "undoLabel": "close gap"}},
                     listing])
        _, shift, items = out["replies"]
        self.assertTrue(shift["ok"], shift)
        copy_at = 10 + g(10)  # the clone: a whole-frame offset
        self.assertEqual([(s["track"], round(s["from"], 4), round(s["to"], 4)) for s in shift["data"]["shifted"]],
                         [("V2", 10, 6), ("V2", round(copy_at, 4), round(copy_at - 4, 4))])  # left shift: earliest first
        self.assertEqual((shift["data"]["deltaSeconds"], shift["data"]["undoLabel"]), (-4, "close gap"))
        self.assertEqual(out["transactions"][-1], "close gap")
        data = items["data"]
        self.assertEqual([span(c) for c in data["video"][1]["items"]],  # moved by whole frames
                         [r4(10 + g(-4), 15 + g(-4), 2, 7), r4(copy_at + g(-4), copy_at + 5 + g(-4), 2, 7)])
        self.assertEqual(span(data["video"][0]["items"][0]), [0, 60, 0, 60])  # V1 / A1 were not asked for
        self.assertEqual(span(data["audio"][0]["items"][0]), [0, 60, 0, 60])
        out = batch([clone, {"action": "shift_items", "params": {"fromSeconds": 5, "deltaSeconds": 3}}, listing])
        _, shift, items = out["replies"]
        self.assertEqual([(s["track"], s["name"], round(s["from"], 4), round(s["to"], 4)) for s in shift["data"]["shifted"]],
                         [("V2", "broll_city.mp4", round(copy_at, 4), round(copy_at + 3, 4)),
                          ("V2", "broll_city.mp4", 10, 13)])  # right: latest first
        self.assertEqual(shift["data"]["undoLabel"], "Agent: shift 2 clip(s) from 5s by 3s")
        self.assertEqual([span(c) for c in items["data"]["video"][1]["items"]],
                         [r4(10 + g(3), 15 + g(3), 2, 7), r4(copy_at + g(3), copy_at + 5 + g(3), 2, 7)])
        d = reply_ok(self, call("shift_items", {"fromSeconds": 61, "deltaSeconds": 2}))
        self.assertEqual(d, {"shifted": [], "deltaSeconds": 2, "undoLabel": None})
        for params, code in (({"fromSeconds": 30, "deltaSeconds": -5}, "PRECONDITION_FAILED"),  # V1's 0-60 s clip
                             ({"fromSeconds": 12, "deltaSeconds": -1, "videoTracks": [1], "audioTracks": []},
                              "PRECONDITION_FAILED"),  # the b-roll at 10-15 s stays and is in the way
                             ({"fromSeconds": 10, "deltaSeconds": -4, "videoTracks": [1]}, "PRECONDITION_FAILED"),
                             ({"fromSeconds": 0, "deltaSeconds": 1, "audioTracks": [9]}, "NOT_FOUND"),
                             ({"fromSeconds": 0}, "BAD_PARAMS")):
            with self.subTest(params=params):
                out = call("shift_items", params)
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), (code, "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))

    # ---- sequence edges on exact frame ticks (Premiere keeps sequence times unsnapped: a +1 us nudge on an end
    #      showed the clip for one more frame on 27.1 b17); source in/out points keep the nudge

    SEQUENCE_TIME_ACTIONS = ("createOverwriteItemAction", "createInsertProjectItemAction", "createSetStartAction",
                             "createSetEndAction", "createMoveAction", "createCloneTrackItemAction",
                             "createAddMarkerAction")

    def test_sequence_edges_land_on_exact_frame_ticks(self):
        out = recorded([
            {"action": "insert_clip", "params": {"itemName": "broll_city.mp4", "atSeconds": 20, "videoTrack": 2,
                                                 "audioTrack": 2, "inSeconds": 1.001, "outSeconds": 3.003}},
            {"action": "insert_clip", "params": {"itemName": "broll_city.mp4", "atSeconds": 41.37, "videoTrack": 3,
                                                 "audioTrack": 3, "mode": "insert"}},
            {"action": "trim_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12, "start": 11, "end": 14}},
            {"action": "trim_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12, "moveTo": 30.5}},
            {"action": "clone_clip", "params": {"kind": "video", "track": 1, "atSeconds": 31, "offsetSeconds": 7.3}},
            {"action": "shift_items", "params": {"fromSeconds": 30, "deltaSeconds": 2.5, "videoTracks": [1],
                                                 "audioTracks": []}},
            {"action": "add_marker", "params": {"atSeconds": 3.3, "name": "flag", "colorIndex": 2}},
        ])
        self.assertTrue(all(r["ok"] for r in out["replies"]), out["replies"])
        self.assertTrue(out["replies"][-1]["data"]["colorSet"])  # the colour step finds the snapped marker
        seq_times = [t for t in out["times"] if t["action"] in self.SEQUENCE_TIME_ACTIONS]
        self.assertEqual(sorted({t["action"] for t in seq_times}), sorted(self.SEQUENCE_TIME_ACTIONS))
        for t in seq_times:
            with self.subTest(time=t):
                self.assertEqual(t["ticks"] % FRAME_TICKS, 0)
        moves = [t["ticks"] for t in seq_times if t["action"] == "createMoveAction"]
        self.assertEqual(moves[1:], [round(2.5 * FPS) * FRAME_TICKS] * 2)  # the shift: whole frames, every clip
        edges = [e for e in out["edges"] if e["track"] in ("V2", "V3", "A3", "V4", "A4")]
        self.assertEqual(sorted(e["track"] for e in edges), ["A3", "A4", "V2", "V2", "V3", "V4"])
        for e in edges:  # (the whole 12 s clip on V4 / A4 ends off the grid: the mock media is not whole frames)
            with self.subTest(edge=e):
                self.assertEqual(e["start"] % FRAME_TICKS, 0)
                if e["track"] not in ("V4", "A4"):
                    self.assertEqual(e["end"] % FRAME_TICKS, 0)
        f = lambda s: round(s * FPS)  # noqa: E731  (whole frames)
        self.assertEqual([e["start"] for e in edges if e["track"] == "V2"],
                         [(f(30.5) + f(2.5)) * FRAME_TICKS, (f(30.5) + f(7.3) + f(2.5)) * FRAME_TICKS])
        # source in/out points keep the nudge (Premiere floors them to whole frames)
        rng = [t for t in out["times"] if t["action"] == "createSetInOutPointsAction"]
        self.assertEqual([(t["arg"], t["ticks"] - round(x * TPS)) for t, x in zip(rng, (1.001, 3.003))],
                         [("in", NUDGE_TICKS), ("out", NUDGE_TICKS)])

    def test_absolute_edges_take_an_off_grid_clip_onto_the_grid(self):
        """The fixture's b-roll starts at 10 s, which is not a 59.94 frame (599.4): an absolute moveTo / start / end
        lands exactly on a frame; a source inPoint / outPoint trim moves the edge by its nudged source delta."""
        for fields, edge in (({"moveTo": 20}, "start"), ({"start": 10.2}, "start"), ({"end": 13.9}, "end")):
            with self.subTest(fields=fields):
                out = recorded([{"action": "trim_clip", "params": dict({"kind": "video", "track": 1, "atSeconds": 12},
                                                                        **fields)}])
                self.assertTrue(out["replies"][0]["ok"], out["replies"])
                (v2,) = [e for e in out["edges"] if e["track"] == "V2"]
                self.assertEqual(v2[edge], round(list(fields.values())[0] * FPS) * FRAME_TICKS)
        for field, value, edge, shift in (("inPoint", 2.5, "start", 8), ("outPoint", 6.5, "end", 8)):
            with self.subTest(field=field):
                out = recorded([{"action": "trim_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12,
                                                                   field: value}}])
                (t,) = out["times"]
                self.assertEqual(t["ticks"] - round(value * TPS), NUDGE_TICKS)
                (v2,) = [e for e in out["edges"] if e["track"] == "V2"]
                self.assertEqual(v2[edge] - round((value + shift) * TPS), NUDGE_TICKS)  # sequence = source + 8 s

    def test_other_timebases_and_an_unreadable_one(self):
        tb25 = 10160640000  # 25 fps
        out = recorded([{"action": "trim_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12, "end": 14.01}},
                        {"action": "shift_items", "params": {"fromSeconds": 10, "deltaSeconds": 0.53,
                                                             "videoTracks": [1], "audioTracks": []}}],
                       mock={"timebase": str(tb25)})
        self.assertTrue(all(r["ok"] for r in out["replies"]), out["replies"])
        self.assertEqual([t["ticks"] for t in out["times"]], [350 * tb25, 13 * tb25])  # 14.01 s, 0.53 s: nearest frames
        (v2,) = [e for e in out["edges"] if e["track"] == "V2"]
        self.assertEqual((v2["start"], v2["end"]), (round(10 * TPS) + 13 * tb25, 363 * tb25))
        steps = (("insert_clip", {"itemName": "broll_city.mp4", "atSeconds": 20, "videoTrack": 2, "audioTrack": 2}),
                 ("add_marker", {"atSeconds": 3, "name": "m"}),
                 ("clone_clip", {"kind": "video", "track": 1, "atSeconds": 12, "offsetSeconds": 20}),
                 ("trim_clip", {"kind": "video", "track": 1, "atSeconds": 12, "end": 14}),
                 ("shift_items", {"fromSeconds": 5, "deltaSeconds": 3}))
        for action, params in steps:
            with self.subTest(action=action):
                out = call(action, params, mock={"noTimebase": True})
                r = out["reply"]
                self.assertEqual((r["ok"], r["code"], r["outcome"]), (False, "UNSUPPORTED", "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))

    def test_export_frame_reads_the_frame_at_a_boundary(self):
        """export_frame is a read point, not an edge: it keeps the nudge, so a time on a frame boundary that went
        through float seconds still exports that frame and not the one before."""
        out = recorded([{"action": "export_frame", "params": {"dir": "/fake/out", "atSeconds": 4.004}}])
        self.assertAlmostEqual(out["exports"][0]["seconds"] - 4.004, 1e-6, places=9)

    def test_inspect_items_audio_tracks(self):
        def labels(params):
            return [(t["kind"], t["label"]) for t in reply_ok(self, call("inspect_items", params))["tracks"]]

        self.assertEqual(labels({}), [("video", f"V{i}") for i in range(1, 5)])
        self.assertEqual(labels({"audioTracks": []}), [("video", f"V{i}") for i in range(1, 5)])
        self.assertEqual(labels({"videoTracks": [1], "audioTracks": [0]}), [("video", "V2"), ("audio", "A1")])
        self.assertEqual(labels({"videoTracks": [], "audioTracks": [3, 0]}), [("audio", "A4"), ("audio", "A1")])
        a1, = reply_ok(self, call("inspect_items", {"audioTracks": [0]}))["tracks"]
        self.assertEqual((a1["index"], a1["name"], a1["transitions"]), (0, "Audio 1", []))
        comp, = a1["clips"][0]["components"]
        self.assertEqual((comp["matchName"], comp["occurrence"]), (VOLUME, 1))
        self.assertEqual([(p["index"], p["value"], p["keyframed"]) for p in comp["params"]],
                         [(0, False, False), (1, LEVEL_0DB, False)])
        r = call("inspect_items", {"audioTracks": [9]})["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("NOT_FOUND", "not_committed"))

    def test_set_params_on_audio(self):
        minus6 = 10 ** ((-6 - 15) / 20)
        out = batch([
            {"action": "set_params", "params": {"audioTrack": 0, "atSeconds": 5, "components": [
                {"matchName": VOLUME, "params": [{"index": 0, "value": True}, {"index": 1, "value": minus6}]}]}},
            {"action": "set_params", "params": {"audioTrack": 0, "atSeconds": 5, "undoLabel": "duck", "components": [
                {"matchName": VOLUME, "params": [{"index": 1, "timeBase": "clip", "keyframes": [
                    {"t": 4, "value": LEVEL_0DB}, {"t": 4.5, "value": minus6}]}]}]}},
            {"action": "inspect_items", "params": {"audioTracks": [0]}},
        ])
        static, keyed, inspect = out["replies"]
        self.assertTrue(static["ok"], static)
        self.assertEqual((static["data"]["set"], static["data"]["createdEffects"]),
                         ([f"{VOLUME}[1]#0 Bypass", f"{VOLUME}[1]#1 Level"], []))
        self.assertTrue(keyed["ok"], keyed)
        self.assertEqual(keyed["data"]["keyframes"], [{"matchName": VOLUME, "occurrence": 1, "index": 1,
                                                       "timeBase": "clip", "mediaSeconds": [4, 4.5]}])
        self.assertEqual(out["transactions"], ["Agent: set params on A1 @5s", "duck"])
        bypass, level = inspect["data"]["tracks"][0]["clips"][0]["components"][0]["params"]
        self.assertIs(bypass["value"], True)
        self.assertEqual((level["keyframed"], level["timeBase"], [k["t"] for k in level["keyframes"]]),
                         (True, "media", [4, 4.5]))
        level_only = {"matchName": VOLUME, "params": [{"index": 1, "value": minus6}]}
        for params, code in (
                ({"audioTrack": 0, "videoTrack": 0, "components": [level_only]}, "BAD_PARAMS"),
                ({"components": [level_only]}, "BAD_PARAMS"),
                ({"audioTrack": 0, "components": [{"matchName": "AE.ADBE Gaussian Blur 2",
                                                   "params": [{"index": 0, "value": 3}]}]}, "UNSUPPORTED"),
                ({"audioTrack": 0, "components": [{"matchName": VOLUME, "occurrence": 2, "params": []}]}, "UNSUPPORTED"),
                ({"audioTrack": 0, "components": [{"matchName": VOLUME, "params": [{"index": 5, "value": 1}]}]},
                 "NOT_FOUND"),
                ({"audioTrack": 1, "components": [level_only]}, "NOT_FOUND")):
            with self.subTest(params=params, code=code):
                out = call("set_params", dict({"atSeconds": 5}, **params))
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), (code, "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))

    def test_lock_scope_is_enforced_by_the_mocks(self):
        script = r"""
const { createMocks } = require(process.argv[1]);
const m = createMocks({});
const out = {};
const tryIt = (name, fn) => { try { fn(); out[name] = "ok"; } catch (e) { out[name] = "threw"; } };
(async () => {
  const project = await m.ppro.Project.getActiveProject();
  const seq = await project.getActiveSequence();
  const markers = await m.ppro.Markers.getMarkers(seq);
  const at = m.ppro.TickTime.createWithSeconds(1);
  tryIt("createOutsideLock", () => markers.createAddMarkerAction("x", "Comment", at, at, ""));
  tryIt("executeOutsideLock", () => project.executeTransaction(() => {}, "x"));
  tryIt("asyncTransaction", () => project.lockedAccess(() => { project.executeTransaction(async () => {}, "x"); }));
  tryIt("asyncLockedAccess", () => project.lockedAccess(async () => {}));
  tryIt("asyncSelection", () => m.ppro.TrackItemSelection.createEmptySelection(async () => {}));
  tryIt("insideLock", () => project.lockedAccess(() => {
    const a = markers.createAddMarkerAction("x", "Comment", at, at, "");
    if (!project.executeTransaction((ca) => { ca.addAction(a); }, "ok")) throw new Error("false");
  }));
  out.transactions = m.record.transactions;
  process.stdout.write(JSON.stringify(out));
})();
"""
        p = subprocess.run([NODE, "-e", script, str(MOCKS)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out, {"createOutsideLock": "threw", "executeOutsideLock": "threw", "asyncTransaction": "threw",
                               "asyncLockedAccess": "threw", "asyncSelection": "threw", "insideLock": "ok",
                               "transactions": ["ok"]})

    def test_teardown_and_probe_create_no_actions(self):
        script = r"""
const h = require(process.argv[1]);
(async () => {
  const { bridge, mocks } = h.loadPanel({}, h.DEFAULT_MAIN);
  await h.callOnce(bridge, { action: "ping", params: { probe: true } });
  const created = mocks.record.actionsCreated;
  bridge.teardown();
  process.stdout.write(JSON.stringify({ created, unloading: bridge.state.unloading, violations: mocks.record.violations }));
  process.exit(0);
})();
"""
        p = subprocess.run([NODE, "-e", script, str(HARNESS)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"created": 0, "unloading": True, "violations": []})

    def test_bundle_from_build_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plugin" / "main.js"
            p = subprocess.run([sys.executable, str(BUILD_PANEL), "--out", str(out), "--assets"],
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertNotIn(b"\r", out.read_bytes())
            check = subprocess.run([NODE, "--check", str(out)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(check.returncode, 0, check.stderr)
            d = harness("describe", {}, bundle=out)
            self.assertEqual(d["modules"], "bundle")
            self.assertEqual(d["handlers"], [a["name"] for a in SPEC["actions"]])
            ping = harness("call", {"action": "ping", "params": {"probe": True}}, bundle=out)["reply"]["data"]
            self.assertEqual((ping["modules"], ping["codeHash"]), ("bundle", code_hash(out.parent)))
            seq = harness("call", {"action": "get_sequence"}, bundle=out)["reply"]
            self.assertEqual(seq["data"]["guid"], "harness-sequence-guid")
            sp = harness("call", {"action": "set_params", "params": ACTIONS["set_params"]["example"]["params"]}, bundle=out)
            self.assertTrue(sp["reply"]["ok"], sp["reply"])
            self.assertEqual(len(sp["transactions"]), 2)
            bad = harness("call", {"action": "insert_clip", "params": {"itemName": 1}}, bundle=out)["reply"]
            self.assertEqual(bad["code"], "BAD_PARAMS")


# ---------------------------------------------------------------- probe_api and api_smoke (batch 0 of the UXP port)

SMOKE_PARAMS = {"videoTrack": 1, "audioTrack": 0, "atSeconds": 12, "itemName": "broll_city.mp4",
                "copyPath": "/fake/media/broll_copy.mp4", "outputDir": "/fake/out", "displayName": "DeNoise",
                "mogrtPath": "/fake/x.mogrt", "allowAaf": True}
# what the mocks do not model (the live run decides): XMP / project metadata, Premiere adding a track
MOCK_GAPS = {("relink", "Metadata.getXMPMetadata"), ("relink", "Metadata.getProjectMetadata"),
             ("implicit_track", "SequenceEditor#createInsertProjectItemAction past the last track")}


@unittest.skipUnless(NODE, "node is not on PATH")
class ApiProbeTest(unittest.TestCase):
    def test_probe_lists_exports_and_instances_without_builtins_or_values(self):
        d = reply_ok(self, call("probe_api", {}))
        self.assertEqual(d["counts"]["exported"], len(d["premierepro"]))
        self.assertIn("getActiveProject(0)", d["premierepro"]["Project"]["members"])
        self.assertEqual(d["premierepro"]["Constants"]["nested"]["TrackItemType"], ["CLIP=1", "EMPTY=0", "TRANSITION=2"])
        for name in ("project", "sequence", "sequenceEditor", "videoClipTrackItem", "videoComponentParam", "markers"):
            self.assertIn(name, d["instances"])
        self.assertIn("createCloneTrackItemAction(6)", d["instances"]["sequenceEditor"])
        everything = json.dumps(d)
        for builtin in ("hasOwnProperty", "__defineGetter__", "toString(0)"):
            self.assertNotIn(builtin, everything)
        self.assertIn("applicationPath:string", d["uxp"]["host"]["members"])
        self.assertNotIn("/fake/Adobe Premiere Pro", everything)  # names and types, never the host's values
        self.assertEqual(d["problems"], [])

    def test_without_instances_and_without_a_project(self):
        d = reply_ok(self, call("probe_api", {"instances": False}))
        self.assertNotIn("instances", d)
        self.assertEqual(d["counts"]["instances"], 0)
        r = call("probe_api", {}, scenario="no-project")["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("NO_PROJECT", "not_committed"))
        self.assertTrue(call("probe_api", {"instances": False}, scenario="no-project")["reply"]["ok"])


@unittest.skipUnless(NODE, "node is not on PATH")
class ApiSmokeTest(unittest.TestCase):
    def test_the_cases_are_the_spec_enum(self):
        source = (LIB / "smoke.js").read_text(encoding="utf-8")
        found = re.findall(r"(?m)^    async (\w+)\(env\) \{", source)
        self.assertEqual(found, ACTIONS["api_smoke"]["params"]["case"]["enum"])

    def test_every_case_passes_on_the_mocks_and_puts_everything_back(self):
        for case in [c for c in ACTIONS["api_smoke"]["params"]["case"]["enum"] if c != "cleanup_bin"]:
            with self.subTest(case=case):
                out = call("api_smoke", dict(SMOKE_PARAMS, case=case, allowTrackAdd=True))
                d = reply_ok(self, out)
                self.assertEqual((d["case"], d["project"]), (case, "PAB_TEST_harness"))
                bad = [(s["status"], s["api"], s.get("detail")) for s in d["steps"]
                       if s["status"] != "pass" and (case, s["api"]) not in MOCK_GAPS]
                self.assertEqual(bad, [])
                self.assertEqual(d["leftovers"], [])
                self.assertEqual(sum(d["summary"].values()), len(d["steps"]))
                self.assertTrue(all(t.startswith(f"PAB_API {case}: ") for t in out["transactions"]), out["transactions"])

    def test_cases_put_back_what_they_changed(self):
        before = reply_ok(self, call("inspect_items", {"videoTracks": [1]}))
        steps = [{"action": "api_smoke", "params": dict(SMOKE_PARAMS, case=c)}
                 for c in ("components", "keyframes", "lumetri", "blend_mode", "clip_props", "transitions")]
        out = batch(steps + [{"action": "inspect_items", "params": {"videoTracks": [1]}},
                             {"action": "get_sequence", "params": {}}])
        *smokes, after, seq = out["replies"]
        for r in smokes:
            self.assertTrue(r["ok"], r)
        self.assertEqual(after["data"]["tracks"], before["tracks"])
        self.assertEqual(seq["data"]["name"], "Sequence 01")

    def test_refused_outside_a_pab_test_project_and_without_inputs(self):
        out = call("api_smoke", {"case": "markers"}, mock={"projectName": "Real Job 2026"})
        self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("PRECONDITION_FAILED", "not_committed"))
        self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))
        for case, missing in (("keyframes", "videoTrack, atSeconds"), ("relink", "itemName, copyPath"),
                              ("converter_exports", "outputDir")):
            out = call("api_smoke", {"case": case})
            self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("BAD_PARAMS", "not_committed"), case)
            self.assertIn(missing, out["reply"]["error"])
            self.assertEqual(out["transactions"], [])

    def test_a_named_project_is_allowed_and_only_that_one(self):
        knobs = {"projectName": "Real Job 2026"}
        ok = call("api_smoke", {"case": "markers", "atSeconds": 1, "allowProject": "Real Job 2026"}, mock=knobs)
        self.assertEqual(reply_ok(self, ok)["project"], "Real Job 2026")
        other = call("api_smoke", {"case": "markers", "allowProject": "Another Job"}, mock=knobs)
        self.assertEqual((other["reply"]["code"], other["transactions"]), ("PRECONDITION_FAILED", []))

    def test_cleanup_bin_removes_only_the_runners_bins(self):
        out = batch([{"action": "import_media", "params": {"paths": ["/fake/x.mp4"], "bin": "PAB_API_20261022"}},
                     {"action": "api_smoke", "params": {"case": "cleanup_bin", "binName": "PAB_API_20261022"}},
                     {"action": "list_project_items", "params": {}}])
        imported, cleaned, items = out["replies"]
        self.assertTrue(imported["ok"], imported)
        self.assertEqual([s["status"] for s in cleaned["data"]["steps"]], ["pass"])
        self.assertFalse([i for i in items["data"]["items"] if i["bin"] == "PAB_API_20261022"])
        for name, code in (("EN", "BAD_PARAMS"), ("PAB_API_none", "NOT_FOUND")):
            r = call("api_smoke", {"case": "cleanup_bin", "binName": name})
            self.assertEqual((r["reply"]["code"], r["transactions"]), (code, []), name)

    def test_absent_apis_are_reported_not_thrown(self):
        """On the 26.4 mocks (no WorkAreaUtils): the step says absent and the case still answers."""
        d = reply_ok(self, call("api_smoke", {"case": "sequence_points", "atSeconds": 1}, scenario="26.4"))
        work_area = next(s for s in d["steps"] if s["api"].startswith("WorkAreaUtils"))
        self.assertEqual(work_area["status"], "absent")
        self.assertIn("not a function", work_area["detail"])


if __name__ == "__main__":
    unittest.main()
