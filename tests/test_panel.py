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

MAIN_REQUIRES = ["./lib/spec.js", "./lib/build.js", "./lib/core.js", "./lib/timeline.js", "./lib/project.js",
                 "./lib/effects.js", "./lib/exportjobs.js", "./lib/capabilities.js", "./lib/support.js",
                 "./lib/pairing.js", "./lib/handlers_read.js",
                 "./lib/handlers_edit.js", "./lib/handlers_items.js", "./lib/handlers_effects.js",
                 "./lib/handlers_removals.js", "./lib/handlers_transcript.js", "./lib/handlers_markers.js",
                 "./lib/handlers_export.js", "./lib/handlers_ops.js", "./lib/probe.js", "./lib/smoke.js", "./lib/launcher.js"]
DEV_FLAG = re.compile(r"\bdev\s*:\s*(true|false)\b")  # the build flag as scripts/build_ccx.py rewrites it
DEV_ONLY = [a["name"] for a in SPEC["actions"] if a.get("devOnly")]
RELEASE_ACTIONS = [a["name"] for a in SPEC["actions"] if not a.get("devOnly")]
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
  if (req.teardown) bridge.teardown();
  const r = mocks.record;
  process.stdout.write(JSON.stringify({ replies, exports: r.exports, clones: r.clones, transactions: r.transactions,
                                        times: r.times, edges: mocks.edgeTicks(), violations: r.violations,
                                        subclips: r.subclips, relinks: r.relinks, listeners: r.listeners,
                                        componentActions: r.componentActions }));
  process.exit(0);
})();
"""


def recorded(steps, **extra):
    """Like batch, plus what the mocks recorded (exports, clone calls, subclips, relinks, the ticks of every
    time-taking action, event listeners added / removed) and every track item's edges in ticks afterwards;
    teardown=True unloads the panel after the steps."""
    p = subprocess.run([NODE, "-e", RECORD_SCRIPT, str(HARNESS), json.dumps(dict(extra, steps=steps))],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"record script exited {p.returncode}: {p.stdout}\n{p.stderr}")
    out = json.loads(p.stdout)
    assert out["violations"] == [], out["violations"]
    return out


CORE_SCRIPT = r"""
const h = require(process.argv[1]);
let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => { raw += c; });
process.stdin.on("end", () => {
  const req = JSON.parse(raw);
  const { bridge, mocks } = h.loadPanel({}, h.DEFAULT_MAIN);
  const core = bridge.lib.core;
  const out = { sha: req.texts.map((t) => core.sha256Hex(t)), fixed: req.numbers.map((v) => core.fixed6(v)) };
  const item = mocks.sequence()._video[0].items[0];
  const ctx = { sideEffects: 0, sideEffect() { this.sideEffects += 1; } };
  const transaction = (n) => {
    try {
      core.runTransaction(mocks.project, `cap ${n}`, () => Array.from({ length: n }, () =>
        item.createMoveAction(mocks.ppro.TickTime.createWithSeconds(0))), ctx);
      return { ok: true };
    } catch (e) { return { code: e.code, message: String(e.message), data: e.data }; }
  };
  out.cap = core.MAX_TRANSACTION_ACTIONS;
  out.over = transaction(core.MAX_TRANSACTION_ACTIONS + 1);
  out.overSideEffects = ctx.sideEffects;
  out.at = transaction(core.MAX_TRANSACTION_ACTIONS);
  out.transactions = mocks.record.transactions;
  out.violations = mocks.record.violations;
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
});
"""


def core_run(texts=(), numbers=()):
    """sha256Hex / fixed6 of the panel's core on the given inputs (sent on stdin, so any text survives), plus
    runTransaction at and over its action cap."""
    p = subprocess.run([NODE, "-e", CORE_SCRIPT, str(HARNESS)], input=json.dumps({"texts": list(texts),
                                                                               "numbers": list(numbers)}),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    if p.returncode != 0:
        raise AssertionError(f"core script exited {p.returncode}: {p.stdout}\n{p.stderr}")
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
                if path.name in ("spec.js", "build.js"):  # data modules
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

    def test_build_flag_is_one_literal(self):
        """plugin/lib/build.js is the build-flag contract: the repository is a dev build, and packages flip the one
        `dev: true` literal (BOARD.md; scripts/build_ccx.py, the release exporter). No second match, no expression."""
        text = (LIB / "build.js").read_text(encoding="utf-8")
        flags = DEV_FLAG.findall(text)
        self.assertEqual(len(flags), 1, flags)  # "true" in the repository, "false" in a release export
        self.assertIn(f"module.exports = {{ dev: {flags[0]} }};", text)
        self.assertNotIn("packaged", text)  # absent = false: only a .ccx package says packaged: true
        self.assertEqual(DEV_ONLY, ["api_smoke"])  # a new devOnly action is a deliberate choice: add it here
        main = strip_comments((PLUGIN / "main.js").read_text(encoding="utf-8"))
        self.assertEqual(DEV_FLAG.findall(main), [], "main.js reads build.dev; it must not look like the flag")
        self.assertIn('if (build.dev === true) handlerGroups.push(require("./lib/smoke.js")(handlerDeps));', main)

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

    def test_release_build_registers_no_dev_only_action(self):
        """dev: false (build.js as a package carries it): the devOnly actions are neither handlers nor in ping's actions,
        a call is UNKNOWN_ACTION before anything runs, and the panel sees no spec mismatch; dev: true is the default."""
        dev = harness("describe", {"dev": True})  # also the default, whatever build.js says (a release's tests)
        self.assertEqual((dev["build"], dev["specMismatch"], dev["handlers"]),
                         ({"dev": True, "packaged": False}, None, list(ACTIONS)))
        self.assertEqual(harness("describe", {})["build"], {"dev": True, "packaged": False})
        rel = harness("describe", {"dev": False})
        self.assertEqual((rel["build"], rel["specMismatch"]), ({"dev": False, "packaged": False}, None))
        self.assertEqual(rel["handlers"], RELEASE_ACTIONS)
        self.assertEqual(rel["specActions"], list(ACTIONS))  # the spec is the same file in both builds
        out = batch([{"action": "ping", "params": {"probe": True}},
                     {"action": "api_smoke", "params": dict(SMOKE_PARAMS, case="markers")},
                     {"action": "probe_api", "params": {"instances": False}}], dev=False)
        ping, smoke, probe = out["replies"]
        self.assertEqual((ping["data"]["build"], ping["data"]["actions"]),
                         ({"dev": False, "packaged": False}, RELEASE_ACTIONS))
        self.assertEqual(ping["data"]["codeHash"], code_hash(PLUGIN))  # the rewrite is in memory only
        self.assertEqual((smoke["ok"], smoke["code"], smoke["outcome"]), (False, "UNKNOWN_ACTION", "not_committed"))
        self.assertIn("release build", smoke["error"])
        self.assertTrue(probe["ok"], probe)  # read-only diagnostics stay
        self.assertEqual((out["transactions"], out["sideEffects"][1]), ([], 0))
        self.assertTrue(reply_ok(self, call("api_smoke", dict(SMOKE_PARAMS, case="markers"), dev=True))["steps"])

    def test_a_packaged_build_js_is_passed_through(self):
        """A .ccx package's build.js (scripts/build_ccx.py): ping reports both flags as booleans, whatever the text."""
        for text, want, handlers in (
                ("module.exports = { dev: false, packaged: true };\n", {"dev": False, "packaged": True}, RELEASE_ACTIONS),
                ("module.exports = { dev: true, packaged: true };\n", {"dev": True, "packaged": True}, list(ACTIONS)),
                ("module.exports = { dev: 1, packaged: \"yes\" };\n", {"dev": False, "packaged": False}, RELEASE_ACTIONS)):
            with self.subTest(text=text):
                d = reply_ok(self, call("ping", {}, buildJs=text))
                self.assertEqual((d["build"], d["actions"]), (want, handlers))

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
            ("subclip", {"itemName": "x", "name": 3, "inSeconds": -1, "outSeconds": "2", "takeAudio": "no"}),
            ("relink", {"itemName": "x", "newPath": None, "dryRun": 1}),
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

    # ---- 0.6 hardening: the fingerprint guard, failed reads, the transaction cap

    # Clips whose values exercise every corner of the fingerprint text: non-ASCII, quotes, a tab, an emoji (UTF-8 and
    # JSON escapes), -0, exact ties at the 7th decimal (odd multiples of 1/128 s), tiny negatives, no media, speed,
    # disabled. Times go through ticks in the mocks, as they do in Premiere.
    SEED = [
        {"track": "V3", "name": "采访 \"A\\B\"\t é 🎬.mp4", "start": -0.0, "end": 0.0078125, "inPoint": 0.0078125,
         "outPoint": 1.0078125, "mediaPath": "/media/采访.mp4"},
        {"track": "V3", "name": "no media", "start": 3.0234375, "end": 4.5, "inPoint": 2.5, "outPoint": 4.0,
         "speed": 2, "disabled": True},
        {"track": "A4", "name": "tiny", "start": 1e-7, "end": 2.000000500001, "inPoint": -1e-7, "outPoint": 1.9999995,
         "mediaPath": "/x/y.wav"},
    ]

    def python_fingerprint(self, **extra):
        from ppro_bridge import items
        out = batch([{"action": "list_items", "params": {}}, {"action": "get_sequence", "params": {}}], **extra)
        listing, seq = (reply_ok(self, {"reply": r}) for r in out["replies"])
        return items.fingerprint(listing, seq)

    def test_fingerprint_guard_matches_python_and_refuses_a_changed_timeline(self):
        mock = {"seedItems": self.SEED}
        fp = self.python_fingerprint(mock=mock)
        self.assertRegex(fp, r"^[0-9a-f]{16}$")
        trim = {"kind": "video", "track": 1, "atSeconds": 12, "end": 14}
        out = batch([{"action": "trim_clip", "params": trim, "guard": {"fingerprint": fp}},       # planned on fp: runs
                     {"action": "trim_clip", "params": dict(trim, end=13), "guard": {"fingerprint": fp}},  # stale now
                     {"action": "list_items", "params": {}, "guard": {"fingerprint": fp}},    # reads ignore it
                     {"action": "remove_items", "params": {"videoTracks": [1], "fromSeconds": 0, "toSeconds": 60,
                                                           "dryRun": True}, "guard": {"fingerprint": fp}},  # dry run
                     {"action": "get_sequence", "params": {}}], mock=mock)
        first, stale, listing, dry, seq = out["replies"]
        self.assertTrue(first["ok"], first)
        self.assertEqual((stale["code"], stale["outcome"]), ("PRECONDITION_FAILED", "not_committed"))
        self.assertIn("Nothing was changed", stale["error"])
        self.assertEqual(stale["data"]["expected"], fp)
        from ppro_bridge import items
        # the panel's own value of the changed timeline is the Python one, computed from its list_items
        self.assertEqual(stale["data"]["actual"], items.fingerprint(listing["data"], seq["data"]))
        self.assertTrue(listing["ok"] and dry["ok"], (listing, dry))
        self.assertEqual(len(out["transactions"]), 1)
        self.assertEqual(out["sideEffects"][1], 0)

    def test_fingerprint_guard_is_checked_once_before_the_handler(self):
        fp = self.python_fingerprint()
        # three transactions (set range, place, clear range) after one check: its own writes never trip it
        out = call("insert_clip", {"itemName": "broll_city.mp4", "atSeconds": 30, "videoTrack": 2, "audioTrack": 2,
                                   "inSeconds": 1, "outSeconds": 3, "undoLabel": "t"}, guard={"fingerprint": fp})
        self.assertTrue(out["reply"]["ok"], out["reply"])
        self.assertEqual(len(out["transactions"]), 3)
        wrong = "0123456789abcdef"
        for guard, scenario, code in (({"fingerprint": wrong}, "default", "PRECONDITION_FAILED"),
                                      ({"fingerprint": wrong, "project": "Other"}, "default", "PROJECT_MISMATCH"),
                                      ({"fingerprint": wrong}, "no-sequence", "PRECONDITION_FAILED"),
                                      ({"fingerprint": "ABCDEF0123456789"}, "default", "BAD_PARAMS"),
                                      ({"fingerprint": "abc"}, "default", "BAD_PARAMS")):
            with self.subTest(guard=guard, scenario=scenario):
                out = call("import_media", {"paths": ["/fake/x.mp4"]}, guard=guard, scenario=scenario)
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), (code, "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))
        # a fingerprint alone pins no project: an action that needs none still runs
        self.assertTrue(call("list_transition_names", {}, guard={"fingerprint": wrong}, scenario="no-project")["reply"]["ok"])

    def test_fingerprint_text_helpers_match_python(self):
        import hashlib
        import random
        from ppro_bridge import items
        vectors = {  # FIPS 180-2 / NIST test vectors
            "": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "abc": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq":
                "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
            "a" * 1000000: "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0",
        }
        texts = list(vectors) + ["x" * n for n in (55, 56, 57, 63, 64, 65, 119, 120, 128)] + [
            "采访 é 🎬 \u2028", json.dumps(self.SEED, ensure_ascii=False, separators=(",", ":"))]
        rng = random.Random(6)
        numbers = [0.0, -0.0, 1, 10, 0.5, 0.1, 1 / 3, 59.94, 1e-7, -1e-7, 5e-7, -5e-7, 4.9999995, 1.0000005,
                   2.5e-6, 3600.123456789, 0.0078125, -0.0078125, 1.0078125, 3.0234375, 123456.0078125, 32400.0]
        numbers += [rng.randrange(1, 2 ** 20, 2) / 128 for _ in range(300)]          # exact ties, both parities
        numbers += [rng.randrange(0, 254016000000 * 36000) / 254016000000 for _ in range(500)]  # tick times
        numbers += [rng.uniform(-100, 4000) for _ in range(500)]
        out = core_run(texts, numbers)
        for text, digest in zip(texts, out["sha"]):
            self.assertEqual(digest, hashlib.sha256(text.encode("utf-8")).hexdigest(), text[:40])
        for text, digest in vectors.items():
            self.assertEqual(out["sha"][texts.index(text)], digest)
        self.assertEqual(out["fixed"], [items.fixed6(v) for v in numbers])
        self.assertEqual(out["fixed"][:4], ["0.000000", "0.000000", "1.000000", "10.000000"])
        self.assertEqual(out["fixed"][numbers.index(0.0078125)], "0.007812")  # half to even, as Python prints it

    def test_transaction_cap(self):
        out = core_run()
        self.assertEqual(out["cap"], 1000)
        self.assertEqual(out["over"]["code"], "BAD_PARAMS")
        self.assertIn("Nothing was changed", out["over"]["message"])
        self.assertEqual(out["over"]["data"], {"actions": 1001, "max": 1000})
        self.assertEqual(out["overSideEffects"], 0)
        self.assertEqual(out["at"], {"ok": True})
        self.assertEqual(out["transactions"], ["cap 1000"])  # the refused one added nothing

    def test_failed_track_reads_are_errors_not_an_empty_timeline(self):
        cases = [
            ("list_items", {}, {"failTrackItems": ["V2"]}, "the clips on V2"),
            ("list_items", {"kinds": ["audio"]}, {"failTrackCount": ["audio"]}, 'audio track count of sequence "Sequence 01"'),
            ("get_sequence", {}, {"failTrackItems": ["A3"]}, "the clips on A3"),
            ("inspect_items", {"videoTracks": [1]}, {"failTrackItems": ["V2"]}, "the clips on V2"),
            ("shift_items", {"fromSeconds": 5, "deltaSeconds": 2}, {"failTrackItems": ["A2"]}, "the clips on A2"),
            ("remove_items", {"videoTracks": [1], "fromSeconds": 0, "toSeconds": 60}, {"failTrackItems": ["V2"]}, "V2"),
            ("insert_clip", {"itemName": "broll_city.mp4", "videoTrack": 2, "audioTrack": 2},
             {"failTrackCount": ["video"]}, "video track count"),
            ("trim_clip", {"kind": "video", "track": 1, "atSeconds": 12, "end": 14}, {"failTrackItems": ["V2"]}, "V2"),
            ("add_transition", {"videoTrack": 1, "atSeconds": 12, "matchName": "AE.ADBE Cross Dissolve New",
                                "durationSeconds": 1}, {"failTrackItems": ["V2"]}, "V2"),
            ("remove_project_items", {"items": [{"name": "broll_city.mp4"}]}, {"failTrackItems": ["V4"]}, "V4"),
        ]
        for action, params, mock, where in cases:
            with self.subTest(action=action, mock=mock):
                out = call(action, params, mock=mock)
                r = out["reply"]
                self.assertEqual((r["ok"], r.get("code"), r.get("outcome")), (False, "INTERNAL", "not_committed"), r)
                self.assertIn("[read]", r["error"])
                self.assertIn(where, r["error"])
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))
        # a list that comes back null is an empty track, as the 0.5 panel read it (27.1's empty-track reply unmeasured)
        audio = reply_ok(self, call("list_items", {"kinds": ["audio"]}, mock={"nullTrackItems": ["A1"]}))["audio"]
        self.assertEqual(next(t for t in audio if t["label"] == "A1")["items"], [])
        # a null slot right after a removal (27.1, live): read again after 200 ms; one that stays is not a clip
        plain = reply_ok(self, call("list_items", {"kinds": ["audio"]}))["audio"]
        for holes in (1, 5):
            with self.subTest(holes=holes):
                out = call("list_items", {"kinds": ["audio"]}, mock={"holeTrackItems": {"A1": holes}})
                self.assertEqual(reply_ok(self, out)["audio"], plain)
        out = call("remove_project_items", {"items": [{"name": "broll_city.mp4"}], "dryRun": True},
                   mock={"holeTrackItems": {"V1": 5, "A1": 5}})
        self.assertTrue(out["reply"]["ok"], out["reply"])
        # capability probing is lenient on purpose: an unreadable track leaves flags unknown, ping still answers
        d = reply_ok(self, call("ping", {"probe": True}, mock={"failTrackItems": ["V1", "V2"], "failTrackCount": ["audio"]}))
        self.assertTrue(d["pong"])

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

    def test_set_params_interpolation_is_set_and_read_back(self):
        out = batch([
            {"action": "set_params", "params": {"videoTrack": 0, "atSeconds": 5, "undoLabel": "ease", "components": [
                {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "timeBase": "clip", "interpolation": "bezier",
                                                             "keyframes": [{"t": 0, "value": 0}, {"t": 1, "value": 100}]}]}]}},
            # V2's Motion > Rotation (#4) is keyframed: an interpolation alone changes the keys it has
            {"action": "set_params", "params": {"videoTrack": 1, "atSeconds": 12, "undoLabel": "hold", "components": [
                {"matchName": "AE.ADBE Motion", "params": [{"index": 4, "interpolation": "Hold"}]}]}},
            {"action": "set_params", "params": {"videoTrack": 1, "atSeconds": 12, "undoLabel": "raw", "components": [
                {"matchName": "AE.ADBE Motion", "params": [{"index": 4, "interpolation": 2}]}]}},
        ])
        new, held, raw = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        # new keys get their mode once they exist (one more step); existing keys in the same step
        self.assertEqual(out["transactions"], ["ease", "ease [interpolation]", "hold", "raw"])
        row = new["interpolation"][0]
        self.assertEqual((row["mode"], row["name"], row["keyframeCount"]), (5, "BEZIER", 2))
        self.assertEqual([s["at"] for s in row["samples"]], [0, 0.25, 0.5, 0.75, 1])
        self.assertEqual((row["samples"][0]["value"], row["samples"][-1]["value"]), (0, 100))
        row = held["interpolation"][0]
        self.assertEqual((row["name"], row["keyframeCount"], held["keyframes"]), ("HOLD", 2, []))
        self.assertEqual(row["samples"][2]["value"], row["samples"][0]["value"])  # a held key keeps its value
        self.assertEqual((raw["interpolation"][0]["mode"], raw["interpolation"][0]["name"]), (2, "2"))
        self.assertNotEqual(raw["interpolation"][0]["samples"][2]["value"], raw["interpolation"][0]["samples"][0]["value"])
        for p in ({"index": 4, "interpolation": "auto"}, {"index": 4, "interpolation": 9},
                  {"index": 0, "interpolation": "bezier"}, {"index": 4, "value": 3, "interpolation": "bezier"}):
            with self.subTest(p=p):  # unknown names, Position (not keyframed), a static value
                out = call("set_params", {"videoTrack": 1, "atSeconds": 12, "components": [
                    {"matchName": "AE.ADBE Motion", "params": [p]}]})
                self.assertEqual((out["reply"]["code"], out["transactions"]), ("BAD_PARAMS", []))

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

    # a graphic on V3 (20-25 s): Opacity, Motion, Graphic Group, Text (mocks.js GRAPHIC_CHAIN)
    GRAPHIC = {"track": "V3", "name": "Basic Title", "start": 20, "end": 25, "inPoint": 0, "outPoint": 5, "graphic": True}
    BLUR = "AE.ADBE Gaussian Blur 2"

    def test_set_params_puts_new_effects_before_a_graphics_layers(self):
        """An effect after a graphic's layer components does nothing to it: new ones go after the clip's effects and
        before the layers (createInsertComponentAction, consecutive indexes, one undo step), so occurrence numbering
        stays as with an append. createdComponents gives the read-back index, the planned one in a dry run."""
        at = {"videoTrack": 2, "atSeconds": 22}
        crop = "AE.ADBE Crop"
        two = [{"matchName": self.BLUR, "occurrence": 2, "params": [{"index": 0, "value": 9}]},
               {"matchName": crop, "params": []}]
        out = recorded([
            {"action": "set_params", "params": dict(at, undoLabel="blur", components=[
                {"matchName": self.BLUR, "params": [{"index": 0, "value": 5}]}])},
            {"action": "set_params", "params": dict(at, dryRun=True, components=two)},
            {"action": "set_params", "params": dict(at, undoLabel="more", components=two)},
            {"action": "inspect_items", "params": {"videoTracks": [2]}},
        ], mock={"seedItems": [self.GRAPHIC]})
        blur, dry, more, inspect = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        self.assertEqual((blur["createdEffects"], blur["createdComponents"]),
                         ([self.BLUR], [{"matchName": self.BLUR, "occurrence": 1, "index": 3}]))
        planned = [{"matchName": self.BLUR, "occurrence": 2, "index": 4}, {"matchName": crop, "occurrence": 1, "index": 5}]
        self.assertEqual((dry["createdComponents"], more["createdComponents"]), (planned, planned))
        self.assertEqual(out["transactions"], ["blur [add effects]", "blur", "more [add effects]", "more"])
        self.assertEqual(out["componentActions"], [{"kind": "insert", "matchName": self.BLUR, "index": 3},
                                                   {"kind": "insert", "matchName": self.BLUR, "index": 4},
                                                   {"kind": "insert", "matchName": crop, "index": 5}])
        comps = inspect["tracks"][0]["clips"][0]["components"]
        self.assertEqual([(c["matchName"], c["occurrence"]) for c in comps],
                         [("AE.ADBE Opacity", 1), ("AE.ADBE Motion", 1), ("AE.ADBE Graphic Group", 1),
                          (self.BLUR, 1), (self.BLUR, 2), (crop, 1), ("AE.ADBE Text", 1)])
        self.assertEqual([comps[3]["params"][0]["value"], comps[4]["params"][0]["value"]], [5, 9])

    def test_set_params_appends_on_a_video_clip(self):
        """Nothing after the effects: the same createAppendComponentAction as before, in request order (A, B, A)."""
        out = recorded([
            {"action": "set_params", "params": {"videoTrack": 0, "atSeconds": 5, "components": [
                {"matchName": self.BLUR, "params": []}, {"matchName": "AE.ADBE Crop", "params": []},
                {"matchName": self.BLUR, "occurrence": 2, "params": [{"index": 0, "value": 4}]}]}},
            {"action": "inspect_items", "params": {"videoTracks": [0]}},
        ])
        added, inspect = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        self.assertEqual(added["createdEffects"], [self.BLUR, "AE.ADBE Crop", self.BLUR])
        self.assertEqual([(c["occurrence"], c["index"]) for c in added["createdComponents"]], [(1, 2), (1, 3), (2, 4)])
        self.assertEqual([a["kind"] for a in out["componentActions"]], ["append"] * 3)
        comps = inspect["tracks"][0]["clips"][0]["components"]
        self.assertEqual([c["matchName"] for c in comps],
                         ["AE.ADBE Motion", "AE.ADBE Opacity", self.BLUR, "AE.ADBE Crop", self.BLUR])
        self.assertEqual(comps[4]["params"][0]["value"], 4)

    def test_effect_insertion_index(self):
        """effects.insertionIndex: on a graphic, right before its first layer component (after Graphic Group, neither a
        top intrinsic, an addable filter nor a container); any other clip, or no layers yet: an append."""
        blur, text, comp, gg = self.BLUR, "AE.ADBE Text", "AE.ADBE CompoundComponent", "AE.ADBE Graphic Group"
        top = ["AE.ADBE Opacity", "AE.ADBE Motion"]
        cases = [
            (top, [blur], (2, [])),                                                # a video clip: append
            (top + [blur], [blur], (3, [])),
            (top + [blur, "AE.Unlisted Effect"], [blur], (4, [])),                 # unlisted last effect: still append
            (top + [comp], [blur], (3, [])),                                       # a colour-tool container: append
            (top + [gg, text], [blur], (3, [text])),                               # a graphic: before its layers
            (top + [gg, blur, text, "AE.ADBE Shape"], [blur], (4, [text, "AE.ADBE Shape"])),
            (top + [gg, text, blur], [blur], (3, [text, blur])),                   # an effect stuck after the layers
            (top + [gg], [blur], (3, [])),                                         # layers not there yet
            (top + [gg, text], None, (4, [])),                                     # no factory list: no layers known
        ]
        script = ("const h = require(process.argv[1]);"
                  "const fx = h.loadPanel({}, h.DEFAULT_MAIN).bridge.lib.effects;"
                  "const cases = JSON.parse(process.argv[2]);"
                  "process.stdout.write(JSON.stringify(cases.map((c) => fx.insertionIndex(c[0], c[1]))));")
        p = subprocess.run([NODE, "-e", script, str(HARNESS), json.dumps([[n, a] for n, a, _ in cases])],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        got = json.loads(p.stdout)
        for (names, addable, (index, tail)), g in zip(cases, got):
            self.assertEqual((g["index"], g["tail"]), (index, tail), names)

    def test_set_params_waits_for_a_graphics_layers(self):
        """Premiere adds a graphic's layer components about a second after placing it (27.1: 0.9 s): an effect added
        before then ended up after Text, where it did nothing. set_params waits up to 2 s (layerWait); a graphic still
        without layers gets the effects appended, with a warning."""
        at = {"videoTrack": 2, "atSeconds": 22}
        blur = [{"matchName": self.BLUR, "params": [{"index": 0, "value": 40}]}]
        for after_ms, index, layers in ((500, 3, 1), (-1, 3, 0)):
            with self.subTest(layersAfterMs=after_ms):
                out = recorded([{"action": "set_params", "params": dict(at, components=blur)},
                                {"action": "inspect_items", "params": {"videoTracks": [2]}}],
                               mock={"seedItems": [dict(self.GRAPHIC, layersAfterMs=after_ms)]})
                added, inspect = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
                self.assertEqual(added["createdComponents"], [{"matchName": self.BLUR, "occurrence": 1, "index": index}])
                wait = added["layerWait"]
                self.assertEqual(wait["layers"], layers)
                chain = [c["matchName"] for c in inspect["tracks"][0]["clips"][0]["components"]]
                if layers:
                    self.assertTrue(0.3 <= wait["seconds"] <= 1.5, wait)
                    self.assertNotIn("warning", added)
                    self.assertEqual(out["componentActions"], [{"kind": "insert", "matchName": self.BLUR, "index": 3}])
                    self.assertEqual(chain[2:], ["AE.ADBE Graphic Group", self.BLUR, "AE.ADBE Text"])
                else:
                    self.assertGreaterEqual(wait["seconds"], 1.9)
                    self.assertIn("appended", added["warning"])
                    self.assertEqual([a["kind"] for a in out["componentActions"]], ["append"])
                    self.assertEqual(chain[2:], ["AE.ADBE Graphic Group", self.BLUR])
        # a loaded graphic and a normal clip never wait
        out = batch([{"action": "set_params", "params": dict(at, components=blur)},
                     {"action": "set_params", "params": {"videoTrack": 0, "atSeconds": 5, "components": blur}}],
                    mock={"seedItems": [self.GRAPHIC]})
        for r in out["replies"]:
            self.assertNotIn("layerWait", r["data"] if r["ok"] else self.fail(r))

    def test_set_params_on_a_graphic_with_an_effect_after_its_layers(self):
        """An effect an older panel appended after a graphic's Text does nothing there. A new effect still goes before
        the layers; one more of that same effect is refused (it would have to follow the stuck one)."""
        stuck = dict(self.GRAPHIC, components=["AE.ADBE Opacity", "AE.ADBE Motion", "AE.ADBE Graphic Group",
                                               "AE.ADBE Text", self.BLUR])
        at = {"videoTrack": 2, "atSeconds": 22}
        out = recorded([
            {"action": "set_params", "params": dict(at, components=[{"matchName": "AE.ADBE Crop", "params": []}])},
            {"action": "set_params", "params": dict(at, components=[{"matchName": self.BLUR, "occurrence": 2, "params": []}])},
        ], mock={"seedItems": [stuck]})
        crop, refused = out["replies"]
        self.assertTrue(crop["ok"], crop)
        self.assertEqual(crop["data"]["createdComponents"], [{"matchName": "AE.ADBE Crop", "occurrence": 1, "index": 3}])
        self.assertEqual((refused["code"], refused["outcome"]), ("UNSUPPORTED", "not_committed"))
        self.assertIn("remove_effect", refused["error"])
        self.assertEqual(out["transactions"], ["Agent: set params on V3 @22s [add effects]"])

    def test_set_params_appends_after_an_unlisted_effect_on_a_normal_clip(self):
        """QA F-1a: no Graphic Group, no layers: an effect the factory does not list stays where it is, the new one is
        appended after it (as before this slice)."""
        seed = {"track": "V3", "name": "clip", "start": 20, "end": 25, "inPoint": 0, "outPoint": 5,
                "components": ["AE.ADBE Opacity", "AE.ADBE Motion", "AE.Unlisted Effect"]}
        out = recorded([{"action": "set_params", "params": {"videoTrack": 2, "atSeconds": 22, "components": [
            {"matchName": self.BLUR, "params": []}]}}], mock={"seedItems": [seed]})
        d = out["replies"][0]["data"] if out["replies"][0]["ok"] else self.fail(out["replies"][0])
        self.assertEqual(d["createdComponents"], [{"matchName": self.BLUR, "occurrence": 1, "index": 3}])
        self.assertEqual(out["componentActions"], [{"kind": "append", "matchName": self.BLUR}])

    def test_add_transition_says_whether_premiere_added_one(self):
        p = {"videoTrack": 0, "atSeconds": 5, "matchName": "AE.ADBE Cross Dissolve New", "durationSeconds": 1,
             "applyToStart": True}
        out = batch([{"action": "add_transition", "params": p},
                     {"action": "add_transition", "params": dict(p, matchName="AE.ADBE Dip To Black")}])
        first, second = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        self.assertEqual((first["added"], first["transitionCount"]), (True, {"before": 0, "after": 1}))
        # Premiere 27.1 keeps a transition already at the edit point (19 Oct 2026): nothing changes
        self.assertEqual((second["added"], second["transitionCount"]), (False, {"before": 1, "after": 1}))

    # ---- removals (V2 holds broll 10-15 s, source 2-7 s, Motion > Rotation (#4) keyed at media 3 s = 0, 5 s = 90)

    TRIM_A = {"action": "trim_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12, "end": 15}}  # on the grid
    PLACE_B = {"action": "insert_clip", "params": {"itemName": "broll_city.mp4", "atSeconds": 15, "videoTrack": 1,
                                                   "audioTrack": 1}}  # B: V2 15-27 s, touching the clip's end
    FADE_OUT_A = {"action": "add_transition", "params": {"videoTrack": 1, "atSeconds": 12, "durationSeconds": 0.5,
                                                         "matchName": "AE.ADBE Cross Dissolve New"}}  # 14.5-15 s

    def test_remove_transition_takes_the_one_at_that_edge(self):
        """One undo step, read back by the track's transition count; a track without transitions is refused before
        anything runs, and an edge without one leaves the count as it was (not_committed)."""
        at_b = {"videoTrack": 1, "atSeconds": 20}
        out = batch([
            {"action": "remove_transition", "params": {"videoTrack": 0, "atSeconds": 5, "edge": "start"}},
            self.TRIM_A, self.PLACE_B, self.FADE_OUT_A,
            {"action": "remove_transition", "params": dict(at_b, edge="end")},
            {"action": "remove_transition", "params": dict(at_b, edge="start", dryRun=True)},
            {"action": "remove_transition", "params": dict(at_b, edge="start", undoLabel="Agent: hard cut")},
            {"action": "remove_transition", "params": dict(at_b, edge="start")},
        ])
        none, _, _, _, wrong_edge, dry, done, gone = out["replies"]
        self.assertEqual((none["code"], none["outcome"], out["sideEffects"][0]), ("NOT_FOUND", "not_committed", 0))
        self.assertEqual((wrong_edge["code"], wrong_edge["outcome"]), ("NOT_FOUND", "not_committed"))
        self.assertEqual(wrong_edge["data"]["transitionCount"], {"before": 1, "after": 1})
        d = dry["data"]
        self.assertEqual((round(d["cut"], 9), d["neighbour"], d["removed"], d["transitionCount"]),
                         (round(g(15), 9), {"name": "broll_city.mp4", "start": 10, "end": d["cut"]}, False, {"before": 1}))
        self.assertEqual(out["sideEffects"][5], 0)
        d = done["data"]
        self.assertEqual((d["removed"], d["via"], d["transitionCount"], d["undoLabel"]),
                         (True, "clip", {"before": 1, "after": 0}, "Agent: hard cut"))
        self.assertEqual(out["transactions"][-1], "Agent: hard cut")
        self.assertEqual((gone["code"], gone["outcome"]), ("NOT_FOUND", "not_committed"))

    def test_remove_transition_asks_the_neighbour_when_the_clip_does_not_own_it(self):
        """Should Premiere file A's outgoing-only fade under A's END only (unmeasured), B's START removes nothing:
        the panel then asks the neighbour, in one more labelled step."""
        out = batch([self.TRIM_A, self.PLACE_B, self.FADE_OUT_A,
                     {"action": "remove_transition", "params": {"videoTrack": 1, "atSeconds": 20, "edge": "start"}}],
                    mock={"transitionSides": True})
        d = out["replies"][3]["data"]
        self.assertEqual((d["via"], d["transitionCount"]), ("neighbour", {"before": 1, "after": 0}))
        label = "Agent: remove the transition at the start of V2 @20s"
        self.assertEqual(out["transactions"][-2:], [label, label + " [neighbour]"])

    def test_remove_effect_takes_one_instance_and_reads_the_chain_back(self):
        blur = "AE.ADBE Gaussian Blur 2"
        second = {"videoTrack": 1, "atSeconds": 12, "matchName": blur, "occurrence": 2}
        out = batch([
            {"action": "set_params", "params": {"videoTrack": 1, "atSeconds": 12, "components": [
                {"matchName": blur, "occurrence": 2, "params": [{"index": 0, "value": 7}]}]}},
            {"action": "remove_effect", "params": dict(second, dryRun=True)},
            {"action": "remove_effect", "params": second},
            {"action": "remove_effect", "params": second},
            {"action": "inspect_items", "params": {"videoTracks": [1]}},
        ])
        _, dry, done, again, inspect = out["replies"]
        d = dry["data"]
        self.assertEqual((d["index"], d["displayName"], "chain" in d, out["sideEffects"][1]), (3, "Gaussian Blur", False, 0))
        d = done["data"]
        motion, opacity = "AE.ADBE Motion", "AE.ADBE Opacity"
        self.assertEqual(d["chainBefore"], [motion, opacity, blur, blur])
        self.assertEqual((d["chain"], d["verified"]), ([motion, opacity, blur], True))
        self.assertEqual((again["code"], again["outcome"], again["data"]["chain"]),
                         ("NOT_FOUND", "not_committed", [motion, opacity, blur]))
        comps = inspect["data"]["tracks"][0]["clips"][0]["components"]
        self.assertEqual(comps[2]["params"][0]["value"], 0)  # the first blur stays; the second (7) went
        for params, code in (({"videoTrack": 1, "atSeconds": 12, "matchName": opacity}, "UNSUPPORTED"),
                             ({"audioTrack": 0, "atSeconds": 5, "matchName": VOLUME}, "UNSUPPORTED"),
                             ({"audioTrack": 0, "atSeconds": 5, "matchName": "AE.ADBE DeNoise"}, "NOT_FOUND"),
                             ({"videoTrack": 1, "atSeconds": 30, "matchName": blur}, "NOT_FOUND")):
            with self.subTest(params=params):
                out = call("remove_effect", params)
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"], out["transactions"], out["sideEffects"]),
                                 (code, "not_committed", [], 0))

    def test_remove_keyframes_by_range_in_any_time_base(self):
        rot = {"videoTrack": 1, "atSeconds": 12, "matchName": "AE.ADBE Motion", "paramIndex": 4}
        out = batch([
            {"action": "remove_keyframes", "params": dict(rot, fromSeconds=0.9, toSeconds=1.1, timeBase="clip", dryRun=True)},
            {"action": "remove_keyframes", "params": dict(rot, fromSeconds=13.0, toSeconds=13.0, timeBase="sequence")},
            {"action": "remove_keyframes", "params": dict(rot, fromSeconds=0.0, toSeconds=0.5, timeBase="clip")},
            {"action": "inspect_items", "params": {"videoTracks": [1]}},
        ])
        dry, seq, empty, inspect = out["replies"]
        d = dry["data"]
        self.assertEqual((d["mode"], d["removed"], d["remaining"], d["animationOff"], out["sideEffects"][0]),
                         ("range", [3], [5], False, 0))
        self.assertAlmostEqual(d["range"]["fromMediaSeconds"], 2.9, places=9)
        d = seq["data"]
        self.assertEqual((d["removed"], d["remaining"], d["animationOff"], d["verified"]), ([5], [3], False, True))
        self.assertEqual((empty["code"], empty["outcome"], out["sideEffects"][2]), ("NOT_FOUND", "not_committed", 0))
        rotation = inspect["data"]["tracks"][0]["clips"][0]["components"][0]["params"][4]
        self.assertEqual((rotation["keyframed"], [k["t"] for k in rotation["keyframes"]]), (True, [3]))
        # a trim left the 5 s key outside the clip (out point 4 s): a media range still reaches it
        out = batch([{"action": "trim_clip", "params": {"kind": "video", "track": 1, "atSeconds": 11, "end": 12}},
                     {"action": "remove_keyframes", "params": dict(rot, atSeconds=11, fromSeconds=4.5, toSeconds=5.5,
                                                                   timeBase="media")}])
        d = out["replies"][1]["data"]
        self.assertEqual((d["removed"], d["remaining"], d["verified"]), ([5], [3], True))

    def test_remove_keyframes_all_switches_animation_off_and_can_keep_the_middle_value(self):
        rot = {"videoTrack": 1, "atSeconds": 12, "matchName": "AE.ADBE Motion", "paramIndex": 4}
        cases = [  # (params, keptValue, valueAfter): the mocks keep the value from before keyframing (0)
            (dict(rot, all=True), None, 0),
            (dict(rot, all=True, keepValue=True), 67.5, 67.5),  # the clip's middle, media 4.5 s: 3/4 of the way to 90
            (dict(rot, fromSeconds=0.0, toSeconds=5.0, timeBase="clip", keepValue=True), 67.5, 67.5),  # covers both
        ]
        for params, kept, after in cases:
            with self.subTest(params=params):
                out = batch([{"action": "remove_keyframes", "params": params},
                             {"action": "inspect_items", "params": {"videoTracks": [1]}}])
                d, inspect = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
                self.assertEqual((d["removed"], d["remaining"], d["animationOff"], d["verified"]), ([3, 5], [], True, True))
                self.assertEqual((d.get("keptValue"), d["valueAfter"], len(out["transactions"])), (kept, after, 1))
                rotation = inspect["tracks"][0]["clips"][0]["components"][0]["params"][4]
                self.assertEqual((rotation["keyframed"], rotation["value"]), (False, after))
        for params in (dict(rot, matchName="AE.ADBE Opacity", paramIndex=0, all=True),
                       dict(rot, paramIndex=9, all=True), dict(rot, matchName="AE.ADBE Crop", all=True)):
            with self.subTest(params=params):
                out = call("remove_keyframes", params)
                self.assertEqual((out["reply"]["code"], out["transactions"], out["sideEffects"]), ("NOT_FOUND", [], 0))

    def test_remove_project_items(self):
        """A bin and what is inside go in one undo step and are read back; a used item, a used item inside a bin and
        a sequence are refused before anything changes (the mock project: Sequence 01 in the root, EN / CN bins)."""
        out = batch([
            {"action": "remove_project_items", "params": {"items": [{"name": "interview.mp4"}]}},
            {"action": "insert_clip", "params": {"itemName": "title.mov", "binPath": "CN", "atSeconds": 30,
                                                 "videoTrack": 2, "audioTrack": 2}},
            {"action": "remove_project_items", "params": {"bins": ["CN"], "dryRun": True}},
            {"action": "remove_project_items", "params": {"bins": ["CN"]}},
            {"action": "remove_project_items", "params": {"items": [{"name": "Sequence 01"}], "dryRun": True}},
            {"action": "remove_project_items", "params": {"items": [{"name": "Sequence 01"}]}},
            {"action": "remove_project_items", "params": {"bins": ["EN"], "undoLabel": "Agent: tidy"}},
            {"action": "list_project_items", "params": {}},
        ])
        used, _, dry_cn, cn, dry_seq, seq, en, listing = out["replies"]
        self.assertEqual((used["code"], used["outcome"]), ("IN_USE", "not_committed"))
        self.assertEqual(sorted(u["track"] for u in used["data"]["inUse"]), ["A1", "V1"])
        self.assertEqual((dry_cn["data"]["wouldRefuse"], [u["item"] for u in dry_cn["data"]["inUse"]]),
                         ("IN_USE", ["CN/title.mov"]))
        self.assertEqual(cn["code"], "IN_USE")
        self.assertEqual(([i["kind"] for i in dry_seq["data"]["items"]], dry_seq["data"]["wouldRefuse"]),
                         (["sequence"], "UNSUPPORTED"))
        self.assertEqual(seq["code"], "UNSUPPORTED")
        d = en["data"]
        self.assertEqual((sorted(d["removed"]), d["remaining"], d["undoLabel"]), (["EN", "EN/title.mov"], [], "Agent: tidy"))
        self.assertEqual(sorted(i["kind"] for i in d["items"]), ["bin", "clip"])
        refs = [(i["bin"], i["name"]) for i in listing["data"]["items"]]
        self.assertNotIn(("EN", "title.mov"), refs)
        self.assertIn(("CN", "title.mov"), refs)

    # ---- v0.6: markers, sequences / restore points, save (the mock project: "harness marker" at 1 s, Sequence 01)

    def test_list_and_remove_markers(self):
        """Markers come in start order with guid and colour index; a name two markers share is refused until atSeconds
        (half a frame of tolerance) picks one; the removal is one undo step and is read back."""
        out = batch([
            {"action": "add_marker", "params": {"atSeconds": 5, "name": "dup", "colorIndex": 1}},
            {"action": "add_marker", "params": {"atSeconds": 3.3, "name": "dup"}},
            {"action": "list_markers", "params": {}},
            {"action": "remove_marker", "params": {"name": "dup"}},
            {"action": "remove_marker", "params": {"name": "dup", "atSeconds": 5.003, "dryRun": True}},
            {"action": "remove_marker", "params": {"name": "dup", "atSeconds": 5.003, "undoLabel": "Agent: drop flag"}},
            {"action": "remove_marker", "params": {"guid": "no-such-guid"}},
            {"action": "list_markers", "params": {}},
        ])
        _, _, listed, ambiguous, dry, removed, missing, after = out["replies"]
        markers = listed["data"]["markers"]
        self.assertEqual([m["name"] for m in markers], ["harness marker", "dup", "dup"])
        self.assertEqual([round(m["start"], 4) for m in markers], r4(1.0, g(3.3), g(5)))
        self.assertEqual([m["colorIndex"] for m in markers], [0, 0, 1])
        self.assertEqual(len({m["guid"] for m in markers}), 3)
        self.assertEqual((ambiguous["code"], ambiguous["outcome"]), ("AMBIGUOUS", "not_committed"))
        self.assertEqual([m["guid"] for m in ambiguous["data"]["matches"]], [markers[1]["guid"], markers[2]["guid"]])
        self.assertEqual((dry["data"]["dryRun"], dry["data"]["removed"][0]["guid"]), (True, markers[2]["guid"]))
        d = removed["data"]
        self.assertEqual((d["verified"], d["markerCount"], d["undoLabel"]), (True, {"before": 3, "after": 2}, "Agent: drop flag"))
        self.assertEqual((missing["code"], missing["outcome"]), ("NOT_FOUND", "not_committed"))
        self.assertEqual([m["guid"] for m in after["data"]["markers"]], [markers[0]["guid"], markers[1]["guid"]])
        self.assertEqual(out["transactions"][-1], "Agent: drop flag")
        self.assertEqual(out["sideEffects"][3:5], [0, 0])  # the refusal and the dry run wrote nothing

    def test_clone_sequence_makes_a_tagged_restore_point_and_prunes_the_oldest(self):
        """The clone is found by a guid diff, renamed and tagged in one more step, and not made active; keep counts the
        new one and prunes the oldest restore point of the same source."""
        out = batch([
            {"action": "clone_sequence", "params": {"dryRun": True}},
            {"action": "clone_sequence", "params": {"keep": 2}},
            {"action": "clone_sequence", "params": {"keep": 2}},
            {"action": "clone_sequence", "params": {"keep": 2, "dryRun": True}},
            {"action": "clone_sequence", "params": {"keep": 2, "undoLabel": "Agent: before the re-cut"}},
            {"action": "list_sequences", "params": {}},
            {"action": "get_sequence", "params": {}},
        ])
        dry, first, second, dry2, third, listing, active = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        self.assertEqual((dry["dryRun"], dry["prune"], out["sideEffects"][0]), (True, [], 0))
        self.assertRegex(dry["cloneName"], r"^Sequence 01 \[restore \d{4}-\d{2}-\d{2} \d{2}:\d{2}\]$")
        self.assertEqual((first["verified"], first["clone"]["premiereName"]), (True, "Sequence 01 Copy"))
        self.assertEqual(first["clone"]["name"], first["cloneName"])
        self.assertEqual(first["undoSteps"], ['Agent: restore point of "Sequence 01" [1/2 clone]',
                                              'Agent: restore point of "Sequence 01" [2/2 name + tag]'])
        self.assertNotEqual(second["cloneName"], first["cloneName"])  # the same minute: seconds (and #n) tell them apart
        self.assertEqual([p["guid"] for p in dry2["prune"]], [first["clone"]["guid"]])
        self.assertEqual([p["guid"] for p in third["pruned"]], [first["clone"]["guid"]])
        self.assertIn("deleteSequence", third["notes"])
        self.assertEqual(third["undoSteps"][1], "Agent: before the re-cut [2/2 name + tag]")
        seqs = listing["sequences"]
        self.assertEqual([s["guid"] for s in seqs], ["harness-sequence-guid", second["clone"]["guid"], third["clone"]["guid"]])
        self.assertEqual([s["active"] for s in seqs], [True, False, False])
        self.assertIsNone(seqs[0]["restorePoint"])
        self.assertEqual([(s["restorePoint"]["sourceGuid"], s["restorePoint"]["renamed"]) for s in seqs[1:]],
                         [("harness-sequence-guid", False)] * 2)
        self.assertEqual(active["guid"], "harness-sequence-guid")
        self.assertEqual(len(out["transactions"]), 6)

    def test_clone_sequence_as_a_plain_named_copy(self):
        """asName: the same clone, renamed only: no restore-point tag, nothing pruned, a taken name refused."""
        out = batch([
            {"action": "clone_sequence", "params": {}},
            {"action": "clone_sequence", "params": {"asName": "Second cut", "keep": 1, "dryRun": True}},
            {"action": "clone_sequence", "params": {"asName": "Second cut", "keep": 1}},
            {"action": "clone_sequence", "params": {"asName": "Second cut"}},
            {"action": "list_sequences", "params": {}},
        ])
        first, dry, copy, taken, listing = out["replies"]
        self.assertEqual((dry["data"]["cloneName"], dry["data"]["prune"]), ("Second cut", []))
        self.assertEqual((copy["data"]["clone"]["name"], copy["data"]["pruned"], copy["data"]["verified"]), ("Second cut", [], True))
        self.assertTrue(copy["data"]["undoSteps"][1].endswith("[2/2 name]"), copy)
        self.assertEqual((taken["ok"], taken["code"], taken["outcome"]), (False, "PRECONDITION_FAILED", "not_committed"))
        rows = {s["name"]: s for s in listing["data"]["sequences"]}
        self.assertIsNone(rows["Second cut"]["restorePoint"])
        self.assertIsNotNone(rows[first["data"]["clone"]["name"]]["restorePoint"])

    CLONE_SCRIPT = r"""
const h = require(process.argv[1]);
(async () => {
  const { bridge, mocks } = h.loadPanel({}, h.DEFAULT_MAIN);
  const call = async (action, params) => (await h.callOnce(bridge, { action, params })).data;
  const a = (await call("clone_sequence", {})).clone;
  const b = (await call("clone_sequence", {})).clone;
  const c = (await call("clone_sequence", {})).clone;
  const ofA = (await call("clone_sequence", { guid: a.guid })).clone;
  // the user duplicates restore point a in Premiere (the copy keeps a's custom properties here) and renames b
  const seqOf = (guid) => mocks.sequences().find((s) => s.guid === guid);
  mocks.project.lockedAccess(() => mocks.project.executeTransaction((t) => t.addAction(seqOf(a.guid).createCloneAction()), "dup"));
  seqOf(b.guid).name = "Sequence 01 keeper";
  await call("set_active_sequence", { guid: a.guid });
  const dry = await call("clone_sequence", { name: "Sequence 01", keep: 1, dryRun: true });
  const real = await call("clone_sequence", { name: "Sequence 01", keep: 1 });
  const listing = await call("list_sequences", {});
  process.stdout.write(JSON.stringify({ a, b, c, ofA, dry, real, listing, violations: mocks.record.violations }));
  process.exit(0);
})();
"""

    def test_clone_sequence_never_prunes_the_active_a_renamed_or_a_copied_restore_point(self):
        p = subprocess.run([NODE, "-e", self.CLONE_SCRIPT, str(HARNESS)], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["violations"], [])
        a, b, c, of_a = (out[k]["guid"] for k in ("a", "b", "c", "ofA"))
        self.assertEqual([x["guid"] for x in out["dry"]["prune"]], [c])  # a is active, b renamed
        self.assertEqual(([x["guid"] for x in out["real"]["pruned"]], out["real"].get("pruneFailed")), ([c], None))
        by_guid = {s["guid"]: s for s in out["listing"]["sequences"]}
        self.assertNotIn(c, by_guid)
        self.assertTrue(by_guid[a]["active"])
        self.assertEqual(by_guid[a]["restorePoint"]["sourceGuid"], "harness-sequence-guid")
        self.assertTrue(by_guid[b]["restorePoint"]["renamed"])
        self.assertEqual(by_guid[of_a]["restorePoint"]["sourceGuid"], a)  # a restore point of a restore point
        dup = [s for s in out["listing"]["sequences"] if s["name"] == out["a"]["name"] + " Copy"]
        self.assertEqual([s["restorePoint"] for s in dup], [None])  # its copied record names a's guid
        self.assertEqual(len(by_guid), 6)  # Sequence 01, a, b, ofA, the duplicate, the new one

    def test_clone_sequence_failures(self):
        lost = call("clone_sequence", {}, mock={"cloneLost": True})
        self.assertEqual((lost["reply"]["code"], lost["reply"]["outcome"]), ("VERIFY_FAILED", "committed"))
        self.assertEqual(lost["transactions"], ['Agent: restore point of "Sequence 01" [1/2 clone]'])
        half = call("clone_sequence", {}, mock={"failTransactions": ["[2/2"]})
        r = half["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("TRANSACTION_FAILED", "partial"))
        self.assertEqual((r["data"]["completed"], r["data"]["clone"]["name"]),
                         (['Agent: restore point of "Sequence 01" [1/2 clone]'], "Sequence 01 Copy"))
        out = batch([{"action": "clone_sequence", "params": {}},
                     {"action": "clone_sequence", "params": {"keep": 1}}], mock={"deleteSequenceFails": True})
        d = out["replies"][1]["data"]
        self.assertEqual((d["verified"], d["pruned"], len(d["pruneFailed"])), (True, [], 1))
        self.assertIn("deleteSequence returned false", d["pruneFailed"][0]["reason"])
        self.assertIn("warning", d)
        r = call("clone_sequence", {"name": "No Such Sequence"})
        self.assertEqual((r["reply"]["code"], r["transactions"]), ("NOT_FOUND", []))
        r = call("clone_sequence", {}, scenario="no-sequence")["reply"]
        self.assertEqual(r["code"], "NO_SEQUENCE")

    def test_save_project(self):
        out = call("save_project", {})
        self.assertEqual(reply_ok(self, out), {"saved": True, "name": "PAB_TEST_harness", "path": "/fake/PAB_TEST_harness.prproj"})
        self.assertEqual((out["transactions"], out["sideEffects"]), ([], 1))
        r = call("save_project", {}, scenario="no-project")["reply"]
        self.assertEqual(r["code"], "NO_PROJECT")
    def test_subclip_lands_in_its_bin_and_is_read_back(self):
        """createSubClipAction gets the object form ({takeVideo, takeAudio}: the positional booleans threw on 27.1) and
        nudged source points; a missing top-level bin comes in the same undo step; Premiere puts a subclip next to its
        source, so one for another bin is moved there (a second step); a name already in the bin is refused."""
        city = {"itemName": "broll_city.mp4", "name": "city 2-5", "inSeconds": 2.002, "outSeconds": 5.005,
                "bin": "Selects"}
        out = recorded([
            {"action": "subclip", "params": dict(city, takeAudio=False, undoLabel="sub")},
            {"action": "subclip", "params": dict(city, inSeconds=1, outSeconds=2)},
            {"action": "subclip", "params": dict(city, allowDuplicateName=True, dryRun=True)},
            {"action": "subclip", "params": {"itemName": "title.mov", "binPath": "EN", "name": "title intro",
                                             "inSeconds": 0, "outSeconds": 1.5}},
            {"action": "list_project_items", "params": {}},
            {"action": "insert_clip", "params": {"itemName": "city 2-5", "binPath": "Selects", "atSeconds": 30,
                                                 "videoTrack": 2, "audioTrack": 2}},
            {"action": "list_items", "params": {"kinds": ["video", "audio"]}},
        ])
        made, taken, dry, intro, listing, _, items = out["replies"]
        d = made["data"]
        self.assertEqual((d["created"], d["item"], d["binPath"], d["createsBin"], d["moved"]),
                         (True, "Selects/city 2-5", "Selects", True, True))
        self.assertEqual((d["undoSteps"], d["undoLabel"], d["hardBoundaries"], d["takeAudio"]),
                         (["sub", "sub [move to bin]"], "sub", True, False))
        self.assertEqual(r4(d["readBack"]["inSeconds"], d["readBack"]["outSeconds"]), [2.002, 5.005])
        self.assertAlmostEqual(d["frameRate"], FPS, places=9)
        self.assertEqual((d["mediaSeconds"], d["newItems"]), (12, ["Selects/city 2-5"]))
        self.assertEqual((taken["code"], taken["outcome"], taken["data"]["existing"]),
                         ("PRECONDITION_FAILED", "not_committed", ["Selects/city 2-5"]))
        self.assertEqual((dry["data"]["dryRun"], dry["data"]["created"], dry["data"]["createsBin"]), (True, False, False))
        self.assertEqual((intro["data"]["item"], intro["data"]["moved"], intro["data"]["undoSteps"]),
                         ("EN/title intro", False, ['Agent: subclip "title intro" of EN/title.mov']))
        self.assertIn({"name": "city 2-5", "bin": "Selects", "mediaPath": "/fake/media/broll_city.mp4"},
                      listing["data"]["items"])
        self.assertEqual(out["transactions"][:3], ["sub", "sub [move to bin]", intro["data"]["undoLabel"]])
        self.assertEqual([(s["name"], s["hard"], s["takeVideo"], s["takeAudio"]) for s in out["subclips"]],
                         [("city 2-5", True, True, False), ("title intro", True, True, True)])
        sub_times = [t for t in out["times"] if t["action"] == "createSubClipAction"]
        self.assertEqual([t["ticks"] - round(x * TPS) for t, x in zip(sub_times, (2.002, 5.005))],
                         [NUDGE_TICKS, NUDGE_TICKS])  # source points: nudged like insert_clip's range
        placed = items["data"]["video"][2]["items"][0]  # placed whole, a subclip is its own range (mock semantics)
        self.assertEqual(r4(placed["inPoint"], placed["outPoint"]), [2.002, 5.005])
        self.assertEqual(items["data"]["audio"][2]["items"], [])  # video only

    def test_subclip_moves_refusals_and_a_failed_move(self):
        intro = {"itemName": "title.mov", "binPath": "EN", "name": "t2", "inSeconds": 0, "outSeconds": 1}
        d = reply_ok(self, call("subclip", intro, mock={"subclipLandsInRoot": True}))
        self.assertEqual((d["item"], d["moved"], len(d["undoSteps"])), ("EN/t2", True, 2))
        out = call("subclip", intro, mock={"subclipLandsInRoot": True, "failTransactions": ["[move to bin]"]})
        r = out["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("TRANSACTION_FAILED", "partial"))
        self.assertEqual((r["data"]["completed"], r["data"]["item"]), (['Agent: subclip "t2" of EN/title.mov'], "t2"))
        self.assertIn("[move to bin]", r["error"])
        cases = [
            ({"itemName": "Sequence 01", "name": "s", "inSeconds": 0, "outSeconds": 1}, "UNSUPPORTED"),
            ({"itemName": "broll_city.mp4", "name": "b", "inSeconds": 0, "outSeconds": 1, "bin": "EN/Deeper"},
             "NOT_FOUND"),
            ({"itemName": "broll_city.mp4", "name": "b", "inSeconds": 0, "outSeconds": 12.5}, "BAD_PARAMS"),
            ({"itemName": "broll_city.mp4", "name": "broll_city.mp4", "inSeconds": 0, "outSeconds": 1},
             "PRECONDITION_FAILED"),
            ({"itemName": "title.mov", "binPath": "CN", "name": "broll_city.mp4", "inSeconds": 0, "outSeconds": 1,
              "bin": "/"}, "PRECONDITION_FAILED"),  # "/" is the root bin, where broll_city.mp4 is
            ({"itemName": "broll_city.mp4", "name": "", "inSeconds": 0, "outSeconds": 1}, "BAD_PARAMS"),
            ({"itemName": "broll_city.mp4", "name": "b", "inSeconds": 0, "outSeconds": 1, "takeVideo": False,
              "takeAudio": False}, "BAD_PARAMS"),
        ]
        for params, code in cases:
            with self.subTest(params=params):
                out = call("subclip", params)
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), (code, "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))

    def test_relink_reads_back_and_lists_every_use(self):
        new = "/fake/media/interview_v2.mp4"
        out = recorded([
            {"action": "subclip", "params": {"itemName": "interview.mp4", "name": "answer", "inSeconds": 1,
                                             "outSeconds": 2}},
            {"action": "relink", "params": {"itemName": "interview.mp4", "newPath": new, "dryRun": True}},
            {"action": "relink", "params": {"itemName": "interview.mp4", "newPath": new}},
            {"action": "list_items", "params": {"kinds": ["video"]}},
            {"action": "relink", "params": {"itemName": "interview.mp4", "newPath": "\\FAKE\\media\\INTERVIEW_V2.mp4"}},
        ])
        _, dry, done, items, again = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        uses = {"clips": 2, "sequences": [{"name": "Sequence 01", "video": 1, "audio": 1}], "complete": True}
        self.assertEqual((dry["dryRun"], dry["relinked"], dry["uses"], dry["sameMedia"]),
                         (True, False, uses, [{"item": "answer"}]))
        self.assertEqual((dry["current"]["mediaPath"], dry["current"]["offline"], dry["current"]["mediaSeconds"]),
                         ("/fake/media/interview.mp4", False, 60))
        self.assertEqual((done["relinked"], done["previousPath"], done["mediaPath"], done["returned"], done["refreshed"]),
                         (True, "/fake/media/interview.mp4", new, True, True))
        self.assertEqual(done["sameMedia"], [{"item": "answer", "mediaPathAfter": "/fake/media/interview.mp4"}])
        self.assertEqual(items["video"][0]["items"][0]["mediaPath"], new)  # clips show their item's new file
        self.assertEqual((again["alreadyLinked"], again["relinked"]), (True, False))
        self.assertEqual(out["relinks"], [{"item": "interview.mp4", "path": new, "override": True}])
        self.assertEqual(out["transactions"], ['Agent: subclip "answer" of interview.mp4'])  # relinking is no transaction

    def test_relink_refusals_and_a_relink_premiere_refuses(self):
        cases = [
            ({"itemName": "title.mov", "newPath": "/fake/x.mov"}, {}, "AMBIGUOUS"),
            ({"itemName": "Sequence 01", "newPath": "/fake/x.mov"}, {}, "UNSUPPORTED"),
            ({"itemName": "broll_city.mp4", "newPath": "/fake/x.mp4"}, {"mock": {"noRelink": True}}, "UNSUPPORTED"),
            ({"itemName": "broll_city.mp4", "newPath": ""}, {}, "BAD_PARAMS"),
        ]
        for params, extra, code in cases:
            with self.subTest(params=params, extra=extra):
                out = call("relink", params, **extra)
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), (code, "not_committed"))
                self.assertEqual(out["sideEffects"], 0)
        d = reply_ok(self, call("relink", {"itemName": "broll_city.mp4", "newPath": "/fake/x.mp4"},
                                mock={"relinkFails": True}))
        self.assertEqual((d["relinked"], d["returned"], d["mediaPath"]), (False, False, "/fake/media/broll_city.mp4"))
        out = batch([{"action": "transcribe_clip", "params": {"itemName": "broll_city.mp4"}},
                     {"action": "relink", "params": {"itemName": "broll_city.mp4", "newPath": "/fake/x.mp4"}}],
                    scenario="stt-pending")
        self.assertEqual((out["replies"][1]["code"], out["replies"][1]["outcome"]), ("TRANSCRIBE_RUNNING", "not_committed"))

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
        self.assertEqual(d["features"], list(SPEC["features"]))  # every documented feature, and only those
        self.assertEqual(d["build"], {"dev": True, "packaged": False})
        self.assertIsInstance(d["loadedAt"], int)
        self.assertIsInstance(d["capabilitiesProbedAt"], int)
        self.assertEqual(d["codeHash"], code_hash(PLUGIN))
        self.assertRegex(d["codeHash"], r"^[0-9a-f]{8}$")
        try:
            from ppro_bridge import panelhash
        except ImportError:
            return
        self.assertEqual(panelhash.code_hash(PLUGIN), d["codeHash"])

    def test_select_items_replaces_the_selection_and_reads_it_back(self):
        # harness: V1 / A1 interview 0-60 s, V2 b-roll 10-15 s
        out = batch([
            {"action": "select_items", "params": {"videoTracks": [0], "audioTracks": [0], "atSeconds": 5}},
            {"action": "select_items", "params": {"videoTracks": [0, 1], "fromSeconds": 9, "toSeconds": 16}},
            {"action": "select_items", "params": {"videoTracks": [0, 1], "fromSeconds": 9, "toSeconds": 16,
                                                  "mode": "overlap"}},
            {"action": "select_items", "params": {"videoTracks": [1], "atSeconds": 12, "dryRun": True}},
            {"action": "select_items", "params": {"clear": True}},
            {"action": "select_items", "params": {"audioTracks": [0], "fromSeconds": 10, "toSeconds": 20}},
        ])
        r = out["replies"]
        for reply in r[:5]:
            self.assertTrue(reply["ok"], reply)
        at, inside, overlap, dry, clear = (x["data"] for x in r[:5])
        self.assertEqual((at["mode"], at["before"], at["after"], at["verified"]), ("at", 0, 2, True))
        self.assertEqual([(s["track"], s["name"]) for s in at["selected"]], [("V1", "interview.mp4"), ("A1", "interview.mp4")])
        # inside: the interview crosses 9-16 s and stays out; overlap takes it
        self.assertEqual(([s["name"] for s in inside["selected"]], inside["before"], inside["after"]), (["broll_city.mp4"], 2, 1))
        self.assertEqual((sorted(s["name"] for s in overlap["selected"]), overlap["after"]),
                         (["broll_city.mp4", "interview.mp4"], 2))
        self.assertEqual((dry["dryRun"], dry["before"], [s["name"] for s in dry["selected"]]), (True, 2, ["broll_city.mp4"]))
        self.assertNotIn("after", dry)
        self.assertEqual((clear["mode"], clear["before"], clear["after"], clear["selected"]), ("clear", 2, 0, []))
        self.assertEqual((r[5]["ok"], r[5]["code"], r[5]["outcome"]), (False, "NOT_FOUND", "not_committed"))
        self.assertIn("mode overlap", r[5]["error"])
        self.assertEqual(out["transactions"], [])  # a selection is not an undo step
        # Premiere keeping the old selection is caught by the read-back
        out = call("select_items", {"audioTracks": [0], "atSeconds": 5}, mock={"selectionIgnored": True})
        self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("VERIFY_FAILED", "committed"))
        self.assertEqual(out["reply"]["data"]["after"], 0)

    def test_reply_keys_match_the_spec(self):
        tj = json.dumps(transcript_doc())
        params = {
            "ping": {}, "get_project": {}, "get_sequence": {}, "set_active_sequence": {"name": "Sequence 01"},
            "select_items": {"videoTracks": [1], "fromSeconds": 9, "toSeconds": 16},
            "list_items": {}, "inspect_items": {},
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
            "remove_transition": {"videoTrack": 1, "atSeconds": 12, "edge": "start", "dryRun": True},
            "remove_effect": {"videoTrack": 1, "atSeconds": 12, "matchName": "AE.ADBE Gaussian Blur 2", "dryRun": True},
            "remove_keyframes": {"videoTrack": 1, "atSeconds": 12, "matchName": "AE.ADBE Motion", "paramIndex": 4,
                                 "all": True, "keepValue": True},
            "set_fps": {"items": [{"name": "broll_city.mp4"}], "fps": 25},
            "subclip": {"itemName": "broll_city.mp4", "name": "broll 1-2", "inSeconds": 1, "outSeconds": 2},
            "relink": {"itemName": "broll_city.mp4", "newPath": "/fake/media/broll_city_v2.mp4"},
            "add_marker": {"atSeconds": 2, "name": "flag"},
            "list_markers": {}, "remove_marker": {"name": "harness marker"}, "list_sequences": {},
            "clone_sequence": {"keep": 3}, "save_project": {},
            "remove_items": {"videoTracks": [1], "fromSeconds": 9, "toSeconds": 16},
            "remove_project_items": {"bins": ["EN"], "dryRun": True},
            "clone_clip": {"kind": "audio", "track": 0, "atSeconds": 5, "offsetSeconds": 70},
            "trim_clip": {"kind": "video", "track": 1, "atSeconds": 12, "end": 14},
            "export_sequence": {"outputPath": "/fake/out/mix.wav", "presetPath": "/fake/presets/wav.epr"},
            "export_status": {},
            "export_aaf": {"outputPath": "/fake/out/cut.aaf", "embedAudio": True},
            "export_xml": {"outputPath": "/fake/out/cut.xml"},
            "list_effects": {}, "add_audio_effect": {"audioTrack": 0, "atSeconds": 5, "name": "DeNoise"},
            "shift_items": {"fromSeconds": 5, "deltaSeconds": 3},
            "apply_ops": {"ops": [{"op": "marker.set_color", "name": "harness marker", "colorIndex": 2},
                                  {"op": "sequence.set_player_position", "atSeconds": 3}]},
            "query": {"what": "sequence"},
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

    def test_trim_clip_refuses_a_move_past_another_clip(self):
        """B1: Premiere 27.1 renders a clip moved past another one on its track silent / blank, so moveTo refuses a
        path that crosses (or lands on) a clip of the track, before any transaction; a clear path still moves."""
        clone = {"action": "clone_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12, "offsetSeconds": 10}}
        for move_to in (21, 26, 40):  # onto the copy at 20-25, just past it, far past it
            with self.subTest(moveTo=move_to):
                out = batch([clone, {"action": "trim_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12,
                                                                       "moveTo": move_to}}])
                reply = out["replies"][1]
                self.assertEqual((reply["code"], reply["outcome"]), ("PRECONDITION_FAILED", "not_committed"), reply)
                self.assertIn("B1", reply["error"])
                self.assertEqual([round(d["start"]) for d in reply["data"]["inWay"]], [20])
                self.assertEqual(len(out["transactions"]), 1)  # the clone only
        out = batch([clone, {"action": "trim_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12,
                                                               "moveTo": 3}}])
        self.assertTrue(out["replies"][1]["ok"], out["replies"][1])  # nothing between 3 s and 10 s on V2

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

    # ---- export jobs (export_sequence wait false + export_status; mock knob exportJob: events are NOT measured)

    START = {"action": "export_sequence", "params": {"outputPath": "/fake/out/cut.mp4", "presetPath": "/fake/h264.epr",
                                                     "wait": False}}

    def test_export_job_from_events(self):
        """A render longer than the 1 s early window: running at first, progress from the events, done from the promise
        (the events only report), one render at a time while it runs."""
        status = {"action": "export_status", "params": {}}
        out = recorded([self.START, status,
                        {"action": "export_sequence", "params": {"outputPath": "/fake/out/b.wav", "presetPath": "/fake/w.epr"}},
                        dict(status, waitMs=900), dict(self.START, waitMs=100)],
                       mock={"exportJob": {"progress": [25, 50, 75], "stepMs": 400, "projectState": 1}})
        start, running, busy, done, again = out["replies"]
        self.assertEqual({k: start["data"][k] for k in ("exported", "started", "state")},
                         {"exported": False, "started": True, "state": "running"})
        job = running["data"]["jobs"][0]
        self.assertEqual((job["jobId"], job["state"], job["progress"], job["progressRaw"], job["pending"]),
                         (start["data"]["jobId"], "running", 0.5, 50, True))
        self.assertEqual((busy["code"], busy["outcome"], busy["data"]["jobId"]), ("EXPORT_RUNNING", "not_committed", job["jobId"]))
        job = done["data"]["jobs"][0]
        self.assertEqual((job["state"], job["settledBy"], job["returned"], job["pending"], job["error"], job["progress"]),
                         ("done", "promise", True, False, None, 0.75))
        self.assertEqual(job["events"], {"progress": 3, "complete": 1, "exportMediaComplete": 1})
        self.assertEqual((job["endEvent"]["type"], job["endEvent"]["state"]), ("exportMediaComplete", 1))
        self.assertEqual(job["routes"], {"EventManager(encoder)": 4, "EventManager(project)": 1})
        self.assertEqual(job["eventKeys"]["progress"], ["type", "progress"])
        self.assertEqual(job["eventSample"]["progress"], {"type": "RenderProgressEvent", "progress": 25})
        self.assertEqual(job["eventSample"]["exportMediaComplete"]["state"], 1)
        self.assertEqual(len(job["listeners"]), 12)  # 5 encoder events + ExportMediaComplete, on both routes
        self.assertEqual(job["listenerErrors"], [])
        self.assertTrue(again["ok"], again)  # the first job ended: a new one may start
        self.assertEqual([e["exportFull"] for e in out["exports"]], [True, True])
        self.assertEqual(out["transactions"], [])

    def test_export_job_ignores_stray_ending_events(self):
        """QA F-4a: RenderComplete and ExportMediaComplete(SUCCESS) from something else (a frame export, the user's own
        export) while the render runs: counted, but the job stays running until its promise settles."""
        status = {"action": "export_status", "params": {}}
        stray = [{"atMs": 300, "name": "RenderCompleteEvent"},
                 {"atMs": 500, "name": "OperationCompleteEvent.ExportMediaComplete", "target": "project",
                  "fields": {"state": 1}}]
        out = batch([self.START, status, dict(status, waitMs=900)],
                    mock={"exportJob": {"progress": [25, 50, 75], "stepMs": 400, "extraEvents": stray}})
        start, running, done = out["replies"]
        self.assertEqual((start["data"]["state"], start["data"]["exported"]), ("running", False))
        job = running["data"]["jobs"][0]
        self.assertEqual((job["state"], job["settledBy"], job["pending"], job["events"]),
                         ("running", None, True, {"progress": 2, "complete": 1, "exportMediaComplete": 1}))
        job = done["data"]["jobs"][0]
        self.assertEqual((job["state"], job["settledBy"], job["events"]["complete"]), ("done", "promise", 2))

    def test_export_job_settled_by_the_promise_or_other_routes(self):
        """No event at all: the promise settles the job. Events on the encoder's own addEventListener count too. A
        promise that returns false fails the job, with an error event's message; after a cancel event (or the project's
        ExportMediaComplete CANCELLED) it is cancelled; a promise that never settles keeps the encoder busy."""
        status = {"action": "export_status", "params": {}}
        out = batch([self.START, status], mock={"exportJob": {"progress": [10], "stepMs": 100, "deliver": "none"}})
        start, st = out["replies"]
        self.assertEqual((start["data"]["state"], start["data"]["exported"]), ("done", True))
        job = st["data"]["jobs"][0]
        self.assertEqual((job["settledBy"], job["events"], job["progress"], job["returned"], job["endEvent"]),
                         ("promise", {}, None, True, None))
        out = batch([self.START, dict(status, waitMs=600)],
                    mock={"exportJob": {"progress": [0.5], "stepMs": 600, "result": "error", "deliver": "encoder"}})
        start, st = out["replies"]
        self.assertEqual((start["ok"], start["data"]["state"]), (True, "running"))
        job = st["data"]["jobs"][0]
        self.assertEqual((job["state"], job["settledBy"], job["returned"], job["routes"], job["endEvent"]["type"]),
                         ("failed", "promise", False, {"encoder": 2}, "error"))
        self.assertEqual(job["error"], "exportSequence returned false (preset /fake/h264.epr); Premiere: Encoding failed (mock)")
        self.assertEqual((job["progress"], job["progressRaw"]), (0.5, 0.5))
        for knobs, events in (({"result": "cancel", "projectState": 2, "deliver": "both"}, {"cancel": 2, "exportMediaComplete": 2}),
                              ({"resolve": False, "projectState": 2}, {"complete": 1, "exportMediaComplete": 1})):
            with self.subTest(knobs=knobs):
                out = batch([self.START, dict(status, waitMs=500)], mock={"exportJob": dict(knobs, stepMs=1200)})
                job = out["replies"][1]["data"]["jobs"][0]
                self.assertEqual((job["state"], job["settledBy"], job["events"], job["returned"]),
                                 ("cancelled", "promise", events, False))
                self.assertIn("cancelled in Premiere (exportSequence returned false", job["error"])
        out = batch([self.START, status, {"action": "export_sequence", "params": self.START["params"]}],
                    mock={"exportJob": {"result": "never"}})
        start, st, busy = out["replies"]
        self.assertEqual((start["data"]["state"], st["data"]["jobs"][0]["pending"], busy["code"]),
                         ("running", True, "EXPORT_RUNNING"))
        # no guard check (QA F-4b): a guard naming another sequence still reads the job table
        out = batch([self.START, dict(status, guard={"sequence": "Other"})], mock={"exportJob": {"result": "never"}})
        self.assertEqual(len(out["replies"][1]["data"]["jobs"]), 1)

    def test_export_job_refused_at_once_is_no_job(self):
        """A promise that fails inside the early window (a complete event first, then false): EXPORT_FAILED as with
        wait, and no job is left behind; the listeners come off with it."""
        out = recorded([self.START, {"action": "export_status", "params": {}}],
                       mock={"exportJob": {"stepMs": 50, "resolve": False}})
        failed, st = out["replies"]
        self.assertEqual((failed["code"], failed["outcome"]), ("EXPORT_FAILED", "not_committed"))
        self.assertIn("exportSequence returned false", failed["error"])
        self.assertEqual(st["data"]["jobs"], [])
        adds = [x for x in out["listeners"] if x["op"] == "add"]
        removes = [x for x in out["listeners"] if x["op"] == "remove"]
        self.assertEqual((len(adds), len(removes), all(x["found"] for x in removes)), (12, 12, True))
        out = recorded([self.START], mock={"exportThrows": True})
        self.assertEqual((out["replies"][0]["code"], out["listeners"][-1]["op"]), ("EXPORT_FAILED", "remove"))

    def test_export_job_listeners_come_off_on_unload(self):
        out = recorded([self.START], mock={"exportJob": {"result": "never"}}, teardown=True)
        ops_ = [(x["op"], x["route"], x["name"]) for x in out["listeners"]]
        self.assertEqual(len(ops_), 24)
        self.assertEqual(sorted(o[1:] for o in ops_ if o[0] == "add"), sorted(o[1:] for o in ops_ if o[0] == "remove"))
        self.assertIn(("add", "EventManager", "RenderProgressEvent"), ops_)
        self.assertIn(("add", "own", "OperationCompleteEvent.ExportMediaComplete"), ops_)

    # ---- export_aaf / export_xml (ProjectConverter; mock knob aafJob holds exportAAF the way 27.1 b21's modal
    # "AAF export log" does until the user clicks OK - whether the panel still answers then is NOT measured)

    AAF = {"action": "export_aaf", "params": {"outputPath": "/fake/out/cut.aaf"}}

    def test_export_aaf_applies_options_with_setters(self):
        """Each option given goes through its AAFExportOptions setter (audioFormat via Constants.AAFExportAudioFormat),
        in the panel's order, and every getter is read back; the job is kind aaf, has no preset and no listeners."""
        params = {"outputPath": "/fake/out/cut.aaf", "embedAudio": True, "audioFormat": "aiff", "sampleRate": 96000,
                  "bitsPerSample": 24, "handleFrames": 12, "mixdownVideo": False, "renderAudioEffects": True,
                  "trimSources": True}
        out = recorded([{"action": "export_aaf", "params": params}, {"action": "export_status", "params": {}}])
        start, st = out["replies"]
        d = start["data"]
        self.assertEqual((d["exported"], d["state"], d["started"], d["outputPath"]),
                         (True, "done", True, "/fake/out/cut.aaf"))
        self.assertEqual(d["optionsSet"], ["embedAudio", "audioFormat", "sampleRate", "bitsPerSample", "handleFrames",
                                           "mixdownVideo", "renderAudioEffects", "trimSources"])
        values = {"audioFileFormat": 0, "bitsPerSample": 24, "embedAudio": True, "handleFrames": 12,
                  "mixdownVideo": False, "renderAudioEffects": True, "sampleRate": 96000, "trimSources": True}
        self.assertEqual({k: d["optionValues"][k] for k in values}, values)
        self.assertEqual(len(d["optionValues"]), 12)  # every getter of the 27.1 class
        [exp] = out["exports"]
        self.assertEqual((exp["kind"], exp["outputPath"], exp["argc"]), ("aaf", "/fake/out/cut.aaf", 3))
        self.assertEqual({k: exp["options"][k] for k in values}, values)
        job = st["data"]["jobs"][0]
        self.assertEqual((job["kind"], job["state"], job["settledBy"], job["returned"], job["preset"], job["events"],
                          job["listeners"], job["error"]), ("aaf", "done", "promise", True, None, {}, [], None))
        self.assertEqual(job["options"], {"set": d["optionsSet"], "values": d["optionValues"]})
        self.assertNotIn("hint", job)
        self.assertEqual((out["listeners"], out["transactions"]), ([], []))  # no encoder listeners, no undo step
        # nothing asked: a default AAFExportOptions all the same (as api_smoke's converter case passes one) ...
        out = recorded([self.AAF])
        self.assertEqual((out["replies"][0]["data"]["optionsSet"], out["exports"][0]["argc"]), ([], 3))
        # ... and on a build without the class, the two-argument call
        out = recorded([self.AAF], mock={"noAafOptions": True})
        d = out["replies"][0]["data"]
        self.assertEqual((d["optionsSet"], d["optionValues"], out["exports"][0]["argc"], out["exports"][0]["options"]),
                         ([], {}, 2, None))

    def test_export_aaf_held_by_the_modal_log(self):
        """Past the 1 s window the job is running; after 3 s its view carries the click-OK hint; the OK (the promise)
        settles it; meanwhile export_sequence, export_aaf and export_xml are EXPORT_RUNNING (one export at a time)."""
        status = {"action": "export_status", "params": {}}
        render = {"action": "export_sequence", "params": {"outputPath": "/fake/out/b.wav", "presetPath": "/fake/w.epr"}}
        xml = {"action": "export_xml", "params": {"outputPath": "/fake/out/cut.xml"}}
        out = recorded([self.AAF, status, render, self.AAF, xml, dict(status, waitMs=2500), dict(status, waitMs=2000)],
                       mock={"aafJob": {"holdMs": 5000}})
        start, early, busy_render, busy_aaf, busy_xml, held, done = out["replies"]
        self.assertEqual((start["data"]["state"], start["data"]["exported"]), ("running", False))
        job = early["data"]["jobs"][0]
        self.assertEqual((job["kind"], job["state"], job["pending"]), ("aaf", "running", True))
        self.assertNotIn("hint", job)  # not yet 3 s
        for busy in (busy_render, busy_aaf, busy_xml):
            self.assertEqual((busy["code"], busy["outcome"], busy["data"]["jobId"], busy["data"]["kind"]),
                             ("EXPORT_RUNNING", "not_committed", job["jobId"], "aaf"))
            self.assertIn("AAF export log", busy["error"])
        job = held["data"]["jobs"][0]
        self.assertEqual(job["state"], "running")
        self.assertIn("click", job["hint"])
        self.assertIn("AAF export log", job["hint"])
        job = done["data"]["jobs"][0]
        self.assertEqual((job["state"], job["settledBy"], job["returned"]), ("done", "promise", True))
        self.assertNotIn("hint", job)
        self.assertEqual([e["kind"] for e in out["exports"]], ["aaf"])  # the refused requests reached no exporter

    def test_export_aaf_failures(self):
        """Refused at once, leaving no job: false or a throw within the first second, a setter this build lacks
        (UNSUPPORTED), a setter that throws (BAD_PARAMS), options on a build without AAFExportOptions, no sequence.
        false after the log was dismissed fails the job instead."""
        status = {"action": "export_status", "params": {}}
        aaf = self.AAF["params"]
        cases = (({"outputPath": "/fake/no/such/cut.aaf"}, {}, "EXPORT_FAILED", "exportAAF returned false"),
                 (aaf, {"aafJob": {"throwSync": True}}, "EXPORT_FAILED", "exportAAF failed (exportAAF: Unable"),
                 (aaf, {"aafJob": {"result": "throw", "holdMs": 100}}, "EXPORT_FAILED",
                  "exportAAF failed (exportAAF failed (mock))"),
                 (dict(aaf, trimSources=True), {"aafMissingSetter": ["setTrimSources"]}, "UNSUPPORTED", "setTrimSources"),
                 (dict(aaf, sampleRate=44100), {"aafSetterThrows": ["setSampleRate"]}, "BAD_PARAMS",
                  "setSampleRate(44100) threw"),
                 (dict(aaf, embedAudio=True), {"noAafOptions": True}, "UNSUPPORTED", "no AAFExportOptions"))
        for params, knobs, code, text in cases:
            with self.subTest(knobs=knobs, params=params):
                out = recorded([{"action": "export_aaf", "params": params}, status], mock=knobs)
                failed, st = out["replies"]
                self.assertEqual((failed["ok"], failed["code"], failed["outcome"]), (False, code, "not_committed"))
                self.assertIn(text, failed["error"])
                self.assertEqual(st["data"]["jobs"], [])
                if code != "EXPORT_FAILED":
                    self.assertEqual(out["exports"], [])  # refused before exportAAF
        r = call("export_aaf", aaf, scenario="no-sequence")["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("NO_SEQUENCE", "not_committed"))
        out = batch([self.AAF, dict(status, waitMs=700)], mock={"aafJob": {"holdMs": 1300, "result": False}})
        start, st = out["replies"]
        self.assertEqual(start["data"]["state"], "running")
        job = st["data"]["jobs"][0]
        self.assertEqual((job["state"], job["returned"], job["error"], job["settledBy"]),
                         ("failed", False, "exportAAF returned false", "promise"))

    def test_export_xml(self):
        """exportAsFinalCutProXML(sequence, path, true): UI suppressed; false or a throw is EXPORT_FAILED. A render
        job does not block it; an AAF job does (its modal log may be up)."""
        xml = {"action": "export_xml", "params": {"outputPath": "/fake/out/cut.xml"}}
        out = recorded([xml, self.START, xml], mock={"exportJob": {"result": "never"}})
        first, render, during = out["replies"]
        self.assertEqual(first["data"], {"exported": True, "outputPath": "/fake/out/cut.xml"})
        self.assertEqual((render["data"]["state"], during["ok"]), ("running", True))
        self.assertEqual([(e["kind"], e.get("suppressUI")) for e in out["exports"]],
                         [("fcpxml", True), ("sequence", None), ("fcpxml", True)])
        self.assertEqual(out["transactions"], [])
        for params, extra, text in ((dict(xml["params"], outputPath="/fake/no/such/cut.xml"), {}, "returned false"),
                                    (xml["params"], {"mock": {"exportFails": True}}, "returned false"),
                                    (xml["params"], {"mock": {"exportThrows": True}},
                                     "exportAsFinalCutProXML failed (exportAsFinalCutProXML: Unable"),
                                    (xml["params"], {"scenario": "no-sequence"}, "")):
            with self.subTest(params=params, extra=extra):
                r = call("export_xml", params, **extra)["reply"]
                code = "NO_SEQUENCE" if extra.get("scenario") else "EXPORT_FAILED"
                self.assertEqual((r["ok"], r["code"], r["outcome"]), (False, code, "not_committed"))
                self.assertIn(text, r["error"])

    def test_list_effects(self):
        """Video filters pair match and display names (checked by creating a few); audio filters are display names
        only; lists that do not pair are listed apart; a build without the audio factory is UNSUPPORTED."""
        d = reply_ok(self, call("list_effects", {}))
        video = [(e["matchName"], e["displayName"]) for e in d["effects"] if e["kind"] == "video"]
        self.assertEqual(video, [("AE.ADBE Gaussian Blur 2", "Gaussian Blur"), ("AE.ADBE Crop", "Crop"),
                                 ("AE.ADBE Lumetri", "Lumetri Color")])
        audio = [e for e in d["effects"] if e["kind"] == "audio"]
        self.assertEqual([(e["displayName"], e["matchName"]) for e in audio], [("DeNoise", None), ("降噪", None)])
        self.assertEqual(d["counts"], {"video": 3, "audio": 2})
        self.assertNotIn("videoDisplayNames", d)
        self.assertEqual(reply_ok(self, call("list_effects", {"kind": "audio"}))["counts"], {"audio": 2})
        shuffled = ["Crop", "Gaussian Blur", "Lumetri Color"]
        d = reply_ok(self, call("list_effects", {"kind": "video"}, mock={"videoDisplayNames": shuffled}))
        self.assertEqual(({e["displayName"] for e in d["effects"]}, d["videoDisplayNames"]), ({None}, shuffled))
        self.assertIn("do not pair up", d["notes"][0])
        r = call("list_effects", {"kind": "audio"}, mock={"noAudioFilters": True})["reply"]
        self.assertEqual((r["code"], r["outcome"]), ("UNSUPPORTED", "not_committed"))
        r = call("list_effects", {}, guard={"project": "Other"})["reply"]
        self.assertEqual(r["code"], "PROJECT_MISMATCH")

    def test_add_audio_effect(self):
        """By display name, by name ignoring case, or by match name: one undo step each, read back. The measured
        "降噪" comes with all six params armed (animation on, no keyframes): set_params still sets a static value
        there, switching that param's animation off (live 27.1, 2 Nov 2026). Refusals change nothing."""
        guid = "c6762838-e451-4d22-9c90-b0eaa734605a"
        out = batch([
            {"action": "add_audio_effect", "params": {"audioTrack": 0, "atSeconds": 5, "name": "降噪", "dryRun": True}},
            {"action": "add_audio_effect", "params": {"audioTrack": 0, "atSeconds": 5, "name": "降噪",
                                                      "undoLabel": "Agent: denoise"}},
            {"action": "add_audio_effect", "params": {"audioTrack": 0, "atSeconds": 5, "name": "denoise"}},
            {"action": "add_audio_effect", "params": {"audioTrack": 0, "atSeconds": 5, "name": "ae.adbe DENOISE"}},
            {"action": "inspect_items", "params": {"audioTracks": [0]}},
            {"action": "set_params", "params": {"audioTrack": 0, "atSeconds": 5, "components": [
                {"matchName": "AE.ADBE DeNoise", "occurrence": 2, "params": [{"index": 1, "value": 25}]}]}},
            {"action": "set_params", "params": {"audioTrack": 0, "atSeconds": 5, "components": [
                {"matchName": guid, "params": [{"index": 1, "value": 0.6}]}]}},
            {"action": "inspect_items", "params": {"audioTracks": [0]}},
        ])
        dry, zh, loose, by_match, chain, ok_set, armed, after = out["replies"]
        self.assertEqual(out["sideEffects"][0], 0)
        self.assertEqual((dry["data"]["dryRun"], dry["data"]["matchName"], dry["data"]["before"]),
                         (True, guid, [VOLUME]))
        self.assertNotIn("verified", dry["data"])
        d = zh["data"]
        self.assertEqual((d["verified"], d["matchedBy"], d["after"], d["undoLabel"]),
                         (True, "displayName", [VOLUME, guid], "Agent: denoise"))
        self.assertEqual([(p["name"], p["keyframed"], p["keyframes"]) for p in d["component"]["params"]][:2],
                         [("旁路", True, []), ("数量", True, [])])
        self.assertEqual((loose["data"]["displayName"], loose["data"]["matchedBy"]),
                         ("DeNoise", "displayNameIgnoringCase"))
        self.assertEqual((by_match["data"]["matchedBy"], by_match["data"]["component"]["occurrence"]), ("matchName", 2))
        names = [c["matchName"] for c in chain["data"]["tracks"][0]["clips"][0]["components"]]
        self.assertEqual(names, [VOLUME, guid, "AE.ADBE DeNoise", "AE.ADBE DeNoise"])
        self.assertTrue(ok_set["ok"], ok_set)
        self.assertTrue(armed["ok"], armed)
        dn = next(c for c in after["data"]["tracks"][0]["clips"][0]["components"] if c["matchName"] == guid)
        amount = next(p for p in dn["params"] if p["index"] == 1)
        self.assertEqual((round(amount["value"], 4), amount["keyframed"]), (0.6, False))
        self.assertTrue(next(p for p in dn["params"] if p["index"] == 2)["keyframed"])  # the others stay armed
        self.assertEqual(out["transactions"][:3], ["Agent: denoise", "Agent: add DeNoise on A1 @5s",
                                                   "Agent: add DeNoise on A1 @5s"])
        for params, extra, code in (
                ({"name": "自适应降噪"}, {}, "NOT_FOUND"),
                ({"name": "AE.ADBE Reverb"}, {}, "NOT_FOUND"),
                ({"name": "DeNoise", "audioTrack": 1}, {}, "NOT_FOUND"),
                ({"name": "DeNoise"}, {"mock": {"noAudioFilters": True}}, "UNSUPPORTED"),
                ({"name": "DeNoise"}, {"scenario": "no-sequence"}, "NO_SEQUENCE"),
                ({"name": ""}, {}, "BAD_PARAMS")):
            with self.subTest(params=params, extra=extra):
                out = call("add_audio_effect", dict({"audioTrack": 0, "atSeconds": 5}, **params), **extra)
                r = out["reply"]
                self.assertEqual((r["ok"], r["code"], r["outcome"]), (False, code, "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))
                if params["name"] == "自适应降噪":
                    self.assertEqual(r["data"]["closeNames"], ["降噪"])
        out = call("add_audio_effect", {"audioTrack": 0, "atSeconds": 5, "name": "DeNoise"},
                   mock={"failTransactions": ["add DeNoise"]})
        self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), ("TRANSACTION_FAILED", "unknown"))

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
        # nothing starts at/after 30 s: V1's 0-60 s clip stays and nothing moves into it (0.5 refused this)
        d = reply_ok(self, call("shift_items", {"fromSeconds": 30, "deltaSeconds": -5}))
        self.assertEqual((d["shifted"], d["undoLabel"]), ([], None))
        for params, code in (({"fromSeconds": 0, "deltaSeconds": -1}, "PRECONDITION_FAILED"),  # V1 would start at -1 s
                             ({"fromSeconds": 0, "deltaSeconds": 1, "audioTracks": [9]}, "NOT_FOUND"),
                             ({"fromSeconds": 0}, "BAD_PARAMS")):
            with self.subTest(params=params):
                out = call("shift_items", params)
                self.assertEqual((out["reply"]["code"], out["reply"]["outcome"]), (code, "not_committed"))
                self.assertEqual((out["transactions"], out["sideEffects"]), ([], 0))

    def test_shift_items_checks_the_room_per_track(self):
        """The precise rule (feature shiftItemsPrecise, as ppro_bridge/editing.py plans it): a staying clip is in the
        way only when it ends after where its own track's first moving clip lands; a track with nothing to move is
        not checked at all. V2 holds the b-roll at 10-15 s and its clone at ~20-25 s."""
        clone = {"action": "clone_clip", "params": {"kind": "video", "track": 1, "atSeconds": 12, "offsetSeconds": 10}}
        copy_at = 10 + g(10)

        def shift(params):
            out = batch([clone, {"action": "shift_items", "params": params}, {"action": "list_items", "params": {}}])
            return out, out["replies"][1], out["replies"][2]["data"]

        # the b-roll ends at 15 s, after 18 - 4 = 14 s but before 16 s, where the clone lands: room enough
        out, reply, items = shift({"fromSeconds": 18, "deltaSeconds": -4, "videoTracks": [1], "audioTracks": []})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([round(s["from"], 4) for s in reply["data"]["shifted"]], [round(copy_at, 4)])
        self.assertEqual([span(c)[:2] for c in items["video"][1]["items"]], [[10, 15], r4(copy_at + g(-4), copy_at + 5 + g(-4))])
        # 6 s left would land the clone at ~14 s, inside the b-roll that stays
        out, reply, _ = shift({"fromSeconds": 18, "deltaSeconds": -6, "videoTracks": [1], "audioTracks": []})
        self.assertEqual((reply["code"], reply["outcome"]), ("PRECONDITION_FAILED", "not_committed"))
        self.assertEqual([(b["track"], b["name"], round(b["end"], 4)) for b in reply["data"]["blocking"]],
                         [("V2", "broll_city.mp4", 15)])
        self.assertIn("Nothing was moved", reply["error"])
        self.assertEqual(out["transactions"], [out["transactions"][0]])  # only the clone
        # A1 (the interview, 0-60 s) has nothing to move, so it cannot be in the way (0.5 refused this)
        out, reply, items = shift({"fromSeconds": 10, "deltaSeconds": -4, "videoTracks": [1], "audioTracks": [0]})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual({s["track"] for s in reply["data"]["shifted"]}, {"V2"})
        self.assertEqual(span(items["audio"][0]["items"][0]), [0, 60, 0, 60])
        self.assertEqual(span(items["video"][1]["items"][0])[:2], r4(10 + g(-4), 15 + g(-4)))

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
            rel = harness("describe", {"dev": False}, bundle=out)  # the bundle holds build.js's one flag
            self.assertEqual((rel["build"]["dev"], rel["handlers"], rel["specMismatch"]), (False, RELEASE_ACTIONS, None))


# ---------------------------------------------------------------- probe_api and api_smoke (batch 0 of the UXP port)

SMOKE_PARAMS = {"videoTrack": 1, "audioTrack": 0, "atSeconds": 12, "itemName": "broll_city.mp4",
                "copyPath": "/fake/media/broll_copy.mp4", "outputDir": "/fake/out", "displayName": "DeNoise",
                "mogrtPath": "/fake/x.mogrt", "allowAaf": True}
# what the mocks do not model (the live run decides): XMP / project metadata, Premiere adding a track
MOCK_GAPS = {("relink", "Metadata.getXMPMetadata"), ("relink", "Metadata.getProjectMetadata"),
             ("implicit_track", "SequenceEditor#createInsertProjectItemAction past the last track")}


B = "\\"
# Paths the panel itself must refuse (spec `localPath` / `fileName`), whoever sent the request: a program that got the
# panel to connect to it, not this CLI, could otherwise hand Premiere a network share (and the user's credentials).
REFUSED_PATHS = [
    ("import_media", {"paths": [B * 2 + "nas" + B + "media" + B + "a.mp4"]}),
    ("import_media", {"paths": ["media/ok.mp4", "//nas/media/a.mp4"]}),
    ("export_frame", {"dir": B * 2 + "localhost" + B + "x"}),
    ("export_frame", {"dir": "frames", "name": ".." + B + "x.png"}),
    ("export_otio", {"outputPath": B * 2 + "?" + B + "UNC" + B + "h" + B + "s" + B + "t.otio"}),
    ("export_sequence", {"outputPath": "out/mix.wav", "presetPath": B * 2 + "." + B + "pipe" + B + "p.epr"}),
    ("export_sequence", {"outputPath": "out/mix.wav:hidden", "presetPath": "p.epr"}),
    ("relink", {"itemName": "broll_city.mp4", "newPath": "//?/UNC/h/s/a.mov"}),
    ("export_aaf", {"outputPath": "//host/share/cut.aaf"}),
    ("export_xml", {"outputPath": "out/cut.xml:hidden"}),
    ("relink", {"itemName": "broll_city.mp4", "newPath": B + "??" + B + "UNC" + B + "h" + B + "a.mov"}),
    # exports keep their file type (spec `extensions`, DOC-9): nothing but an .otio, .xml, .aaf, media file or .png
    ("export_otio", {"outputPath": "out/run.bat"}),
    ("export_xml", {"outputPath": "out/cut.cmd"}),
    ("export_aaf", {"outputPath": "out/cut.aaf.ps1"}),
    ("export_sequence", {"outputPath": "out/mix.bat", "presetPath": "p.epr"}),
    ("export_sequence", {"outputPath": "out/mix.wav", "presetPath": "p.vbs"}),
    ("export_frame", {"dir": "frames", "name": "f.lnk"}),
]


@unittest.skipUnless(NODE, "node is not on PATH")
class PanelPathTest(unittest.TestCase):
    """core.pathProblem / nameProblem: the panel's spec check refuses network, device and stream paths before any
    handler runs, with the very problem texts ppro_bridge/spec.py gives (one rule, ppro_bridge/paths.py)."""

    def test_refused_before_any_handler_with_the_python_texts(self):
        out = batch([{"action": a, "params": p} for a, p in REFUSED_PATHS])
        for (action, params), reply, effects in zip(REFUSED_PATHS, out["replies"], out["sideEffects"]):
            with self.subTest(action=action, params=params):
                self.assertFalse(reply["ok"], reply)
                self.assertEqual((reply["code"], reply["outcome"]), ("BAD_PARAMS", "not_committed"))
                self.assertEqual(reply["data"]["problems"], pspec.check_params(action, params))
                self.assertEqual(effects, 0)
        self.assertEqual(out["transactions"], [])

    def test_local_paths_pass_the_check(self):
        long_local = B * 2 + "?" + B + "C:" + B + "Job" + B + "media" + B + "a.mp4"  # as Premiere reports project paths
        for action, params in (("import_media", {"paths": [long_local, "media/a.mp4"]}),
                               ("export_otio", {"outputPath": "out/t.otio"}),
                               ("export_otio", {"outputPath": "out/T.OTIO"}),
                               ("export_sequence", {"outputPath": "out/a.MP4", "presetPath": "p.epr"}),
                               ("export_frame", {"dir": "frames", "name": "f_1.png"})):
            with self.subTest(action=action):
                self.assertEqual(pspec.check_params(action, params), [])
                reply = call(action, params)["reply"]
                self.assertNotEqual(reply.get("code"), "BAD_PARAMS", reply)


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

    def test_keep_mogrt_leaves_the_inserted_graphic_in_place(self):
        """The live check of effect order on a graphic needs the title to stay: keepMogrt skips the removal and lists
        the clips; remove_items takes them off afterwards."""
        mogrt = {"case": "mogrt", "mogrtPath": "/fake/Basic Title.mogrt", "videoTrack": 3, "audioTrack": 3,
                 "atSeconds": 70, "keepMogrt": True}
        out = batch([{"action": "api_smoke", "params": mogrt},
                     {"action": "list_items", "params": {"kinds": ["video"]}},
                     {"action": "remove_items", "params": {"videoTracks": [3], "audioTracks": [], "fromSeconds": 69,
                                                           "toSeconds": 76}}])
        smoke, listed, removed = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        last = smoke["steps"][-1]
        self.assertEqual((last["api"], last["status"]), ("remove the inserted MOGRT clips", "skipped"))
        self.assertEqual(last["detail"]["keptInPlace"], [{"name": "Title.mogrt", "start": 70, "end": 75}])
        self.assertEqual(smoke["leftovers"], [])
        v4 = next(t for t in listed["video"] if t["index"] == 3)
        self.assertEqual([i["name"] for i in v4["items"]], ["Title.mogrt"])
        self.assertEqual([r["name"] for r in removed["removed"]], ["Title.mogrt"])

    def test_sequence_settings_works_on_an_inactive_clone_and_deletes_it(self):
        out = batch([{"action": "api_smoke", "params": {"case": "sequence_settings"}},
                     {"action": "list_sequences", "params": {}}, {"action": "get_sequence", "params": {}}])
        smoke, listing, seq = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        self.assertEqual([s["status"] for s in smoke["steps"]], ["pass"] * 9, smoke["steps"])  # 8 + the cleanup
        self.assertTrue(smoke["steps"][-1]["api"].startswith("cleanup: cloned sequence"))
        self.assertEqual(smoke["leftovers"], [])
        self.assertEqual([s["name"] for s in listing["sequences"]], ["Sequence 01"])  # the clone is gone
        self.assertEqual((seq["name"], seq["width"], seq["height"]), ("Sequence 01", 1080, 1920))
        by_api = {s["api"]: s.get("detail") for s in smoke["steps"]}
        edited = by_api["createSetSettingsAction: frame 1080x1350 via the RectF getVideoFrameRect returned"]
        self.assertEqual(edited["rectWas"]["width"], 1080)  # prototype accessors read, not {}
        self.assertEqual(edited["after1000ms"]["frameSize"], {"width": 1080, "height": 1350})
        self.assertEqual(by_api["createSetSettingsAction: control (one boolean setting flipped)"]["setting"],
                         "MaxRenderQuality")
        self.assertEqual(out["transactions"], [f"PAB_API sequence_settings: {t}" for t in (
            "clone sequence", "rename clone", "frame 1080x1350 (returned RectF)", "frame 1080x1080 (new RectF)",
            "control: MaxRenderQuality true")])

    def test_sequence_settings_reports_a_frame_size_that_does_not_apply(self):
        """The 22 Oct result (build 21): the frame steps fail, the control still passes, the clone still goes."""
        out = batch([{"action": "api_smoke", "params": {"case": "sequence_settings"}},
                     {"action": "list_sequences", "params": {}}], mock={"frameSizeIgnored": True})
        smoke, listing = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        status = {s["api"]: s["status"] for s in smoke["steps"]}
        self.assertEqual([k for k, v in status.items() if v != "pass"],
                         ["createSetSettingsAction: frame 1080x1350 via the RectF getVideoFrameRect returned",
                          "createSetSettingsAction: frame 1080x1080 via a new ppro.RectF"])
        self.assertEqual((smoke["leftovers"], [s["name"] for s in listing["sequences"]]), ([], ["Sequence 01"]))

    def test_sequence_settings_lists_a_clone_it_could_not_delete(self):
        out = call("api_smoke", {"case": "sequence_settings"}, mock={"deleteSequenceFails": True})
        d = reply_ok(self, out)
        self.assertEqual(d["steps"][-2]["status"], "fail")  # the delete step
        self.assertEqual(d["steps"][-1]["api"].split(":")[0], "cleanup")
        self.assertEqual(len(d["leftovers"]), 1)
        self.assertIn("Sequence 01 Copy", d["leftovers"][0])

    def test_sequence_settings_deletes_nothing_when_the_clone_is_not_the_only_new_sequence(self):
        """Two new sequences after one clone: neither is known to be the clone, so both stay and are listed."""
        out = batch([{"action": "api_smoke", "params": {"case": "sequence_settings"}},
                     {"action": "list_sequences", "params": {}}], mock={"cloneTwice": True})
        smoke, listing = (r["data"] if r["ok"] else self.fail(r) for r in out["replies"])
        self.assertEqual(smoke["steps"][1]["status"], "fail")
        self.assertEqual(len(smoke["leftovers"]), 2)
        self.assertTrue(all("not deleted, 2 appeared" in x for x in smoke["leftovers"]))
        self.assertEqual(len(listing["sequences"]), 3)
        self.assertEqual(out["transactions"], ["PAB_API sequence_settings: clone sequence"])
        lost = reply_ok(self, call("api_smoke", {"case": "sequence_settings"}, mock={"cloneLost": True}))
        self.assertEqual((lost["steps"][1]["status"], lost["leftovers"]), ("fail", []))

    def test_absent_apis_are_reported_not_thrown(self):
        """On the 26.4 mocks (no WorkAreaUtils): the step says absent and the case still answers."""
        d = reply_ok(self, call("api_smoke", {"case": "sequence_points", "atSeconds": 1}, scenario="26.4"))
        work_area = next(s for s in d["steps"] if s["api"].startswith("WorkAreaUtils"))
        self.assertEqual(work_area["status"], "absent")
        self.assertIn("not a function", work_area["detail"])


if __name__ == "__main__":
    unittest.main()
