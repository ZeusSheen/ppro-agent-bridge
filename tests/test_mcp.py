"""The MCP server's transport-independent parts: tools generated from the CLI (ppro_bridge/mcp/tools.py), the
persistent broker connection (connection.py) and token redaction (redact.py).

Calls run in-process against the fake panel (helpers.fake_rpc) or, for the connection, a broker + fake panel on a
free port and small scripted WebSocket servers. Nothing here reaches the real broker on 8017 or the user's token.
"""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, token and audit in a temp folder)

import argparse
import asyncio
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import websockets

from helpers import REPO, Bridge, broker_pair, fake_rpc, free_port
from ppro_bridge import auth, cli, spec, transport
from ppro_bridge.errors import BridgeError
from ppro_bridge.mcp import redact, tools
from ppro_bridge.mcp.connection import Connection

TYPES = {"string", "number", "integer", "boolean", "array", "object"}
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
ALWAYS_GATED = {"remove_items", "remove_project_items", "remove_transition", "remove_effect", "remove_keyframes",
                "remove_marker", "cut_range", "extend_cut", "shift_items", "relink", "replace_clip"}
CONDITIONALLY_GATED = {"clone_sequence", "apply_plan", "export_video", "insert_clip", "import_transcript",
                       "apply_ops",  # apply_ops: when an op removes something (marker.remove)
                       # DOC-7: every export that can replace an existing file
                       "export_aaf", "export_xml", "export_otio", "export_audio", "export_transcript"}


def call(name, args=None, rpc=None, **kw):
    """(structuredContent, the whole result) of one tool call; the text block must be the same object."""
    result = tools.call_tool(name, args, rpc, **kw)
    obj = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == obj, "text and structuredContent differ"
    assert result["isError"] == (obj.get("ok") is False)
    return obj, result


def raising_rpc(exc):
    def rpc(action, params=None):
        raise exc
    return rpc


# ---------------------------------------------------------------- generation

class RegistryTest(unittest.TestCase):
    def test_one_tool_per_cli_command_plus_local_and_meta(self):
        names = set(tools.registry())
        self.assertEqual(names, set(spec.command_names()) | {"transcribe", "transcript_digest"} | set(tools.META))

    def test_every_tool_definition_is_a_sane_mcp_tool(self):
        for name, tool in tools.registry().items():
            with self.subTest(tool=name):
                d = tool.definition()
                self.assertRegex(d["name"], NAME_RE)
                self.assertIn(tools.DATA_RULE, d["description"])
                schema = d["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)
                props = schema["properties"]
                self.assertLessEqual(set(schema.get("required", [])), set(props))
                for prop, ps in props.items():
                    self.assertRegex(prop, r"^[a-z][a-z0-9_]*$")
                    kinds = ps["type"] if isinstance(ps["type"], list) else [ps["type"]]
                    self.assertLessEqual(set(kinds), TYPES, prop)
                    if "enum" in ps:
                        want = {"string": str, "integer": int, "number": (int, float)}[ps["type"]]
                        self.assertTrue(all(isinstance(v, want) and not isinstance(v, bool) for v in ps["enum"]),
                                        prop)
                    if ps["type"] == "array":
                        self.assertIn(ps["items"]["type"], TYPES)
                ann = d["annotations"]
                for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                    self.assertIsInstance(ann[key], bool, key)
                self.assertFalse(ann["readOnlyHint"] and ann["destructiveHint"])
                json.dumps(d)  # serialisable

    def test_descriptions_carry_doc_guards_labels_and_the_confirm_rule(self):
        reg = tools.registry()
        for name in spec.command_names():
            tool, command = reg[name], spec.command(name)
            with self.subTest(tool=name):
                self.assertTrue(tool.description.startswith(command["doc"].strip().rstrip(".")))
                props = tool.input_schema["properties"]
                if "label" in props:
                    self.assertIn("`label`", tool.description)
                if command["kind"] == "write":
                    self.assertIn("expect_fingerprint", props)
                    self.assertIn("`expect_fingerprint`", tool.description)
                if "expect_seq" in props:
                    self.assertIn("`expect_seq`", tool.description)
                gated = name in ALWAYS_GATED | CONDITIONALLY_GATED
                self.assertEqual("confirm" in props, gated)
                self.assertEqual("`confirm: true`" in tool.description, gated)

    def test_the_gated_set_is_the_documented_one(self):
        reg = tools.registry()
        self.assertEqual({n for n, t in reg.items() if t.gate and t.gate.when is None and t.gate.mode == "dry_run"},
                         ALWAYS_GATED)
        self.assertEqual({n for n, t in reg.items() if t.gate} - ALWAYS_GATED, CONDITIONALLY_GATED)
        for name in ALWAYS_GATED | CONDITIONALLY_GATED:
            self.assertTrue(reg[name].annotations["destructiveHint"], name)

    def test_annotations_follow_actions_json(self):
        reg = tools.registry()
        for name in spec.command_names():
            command, ann = spec.command(name), reg[name].annotations
            acts = [spec.action(c) for c in command["calls"]]
            with self.subTest(tool=name):
                if ann["readOnlyHint"]:
                    self.assertEqual(command["kind"], "read")
                    self.assertTrue(all(a["readOnly"] for a in acts))
                if any(a["destructive"] for a in acts):
                    self.assertTrue(ann["destructiveHint"])
                self.assertEqual(ann["idempotentHint"], all(a["idempotent"] for a in acts))
        self.assertTrue(reg["get_sequence"].annotations["readOnlyHint"])
        self.assertFalse(reg["export_otio"].annotations["readOnlyHint"])  # writes the file you name
        self.assertFalse(reg["set_params"].annotations["destructiveHint"])
        self.assertTrue(reg["run_tool"].annotations["destructiveHint"])
        self.assertTrue(reg["find_tool"].annotations["readOnlyHint"])

    def test_core_list_and_all(self):
        self.assertTrue(set(tools.CORE) <= set(tools.registry()))
        with mock.patch.dict(os.environ, {tools.TOOLS_ENV: ""}):
            listed = [t["name"] for t in tools.list_tools()]
        self.assertEqual(set(listed), set(tools.CORE) | set(tools.META))
        self.assertEqual(len(listed), len(tools.CORE) + 3)
        with mock.patch.dict(os.environ, {tools.TOOLS_ENV: "all"}):
            self.assertEqual(len(tools.list_tools()), len(tools.registry()))
        self.assertEqual(len(tools.list_tools(all_tools=True)), len(tools.registry()))
        self.assertIn(tools.DATA_RULE, tools.instructions())
        self.assertIn("confirm: true", tools.instructions())

    def test_a_new_cli_command_becomes_a_tool_without_editing_the_server(self):
        real = cli.build_parser

        def build():
            parser = real()
            sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            p = sub.add_parser("zz_probe", help="A command added for this test")
            p.add_argument("--speed", type=float, required=True, help="How fast")
            p.add_argument("--tag", action="append")
            p.add_argument("--loud", action="store_true")
            p.add_argument("--kind", choices=["a", "b"], default="a")
            return parser

        try:
            with mock.patch.object(tools.cli, "build_parser", build):
                tools._registry = None
                tool = tools.registry()["zz_probe"]
        finally:
            tools._registry = None
        props = tool.input_schema["properties"]
        self.assertEqual(props["speed"]["type"], "number")
        self.assertEqual(props["tag"], {"type": "array", "items": {"type": "string"}})
        self.assertEqual(props["loud"]["type"], "boolean")
        self.assertEqual((props["kind"]["enum"], props["kind"]["default"]), (["a", "b"], "a"))
        self.assertEqual(tool.input_schema["required"], ["speed"])

    def test_every_option_round_trips_through_the_commands_own_parser(self):
        def sample(opt):
            t = opt.schema.get("items", opt.schema)
            if opt.kind == "bool":
                return True, True
            if opt.kind == "tristate":
                return False, False
            if opt.kind == "optional_value":
                return True, opt.action.const
            enum = t.get("enum")
            one = enum[-1] if enum else {"number": 1.5, "integer": 2}.get(t["type"], "-x=y z")
            if opt.kind == "list":
                two = one if enum else ("b" if t["type"] == "string" else one)
                return [one, two], [opt.action.type(one) if opt.action.type else one,
                                    opt.action.type(two) if opt.action.type else two]
            return one, (opt.action.type(one) if opt.action.type else one)

        for name, tool in tools.registry().items():
            if tool.program == "meta":
                continue
            groups = [g for g in tool.parser._mutually_exclusive_groups]
            member = {id(a): g for g in groups for a in g._group_actions}
            base = {n: sample(o)[0] for n, o in tool.options.items() if n in tool.input_schema.get("required", [])}
            for g in groups:
                if g.required:
                    first = next(o for o in tool.options.values() if o.action in g._group_actions)
                    base[first.name] = sample(first)[0]
            for opt_name, opt in tool.options.items():
                with self.subTest(tool=name, option=opt_name):
                    args = dict(base)
                    g = member.get(id(opt.action))
                    if g is not None:
                        for other in tool.options.values():
                            if other.action in g._group_actions:
                                args.pop(other.name, None)
                    value, expected = sample(opt)
                    args[opt_name] = value
                    ns = tool.parser.parse_args(tools._argv(tool, args))
                    self.assertEqual(getattr(ns, opt.action.dest), expected)


# ---------------------------------------------------------------- calls

class CallTest(unittest.TestCase):
    def setUp(self):
        self.rpc, self.fake, self.calls = fake_rpc()

    def test_a_read_returns_exactly_what_the_cli_prints(self):
        obj, result = call("list_items", {"track": "V1"}, self.rpc)
        rpc2, _, _ = fake_rpc()
        printed = cli.run("list_items", {"kind": "all", "track": "V1"}, rpc2)
        self.assertEqual({k: v for k, v in obj.items() if k != "status"}, printed)
        self.assertEqual(obj["status"], "read")
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")

    def test_a_write_lands_and_says_whether_it_was_read_back(self):
        obj, _ = call("add_marker", {"at": 5, "name": "Fact check", "label": "flag a claim"}, self.rpc)
        self.assertEqual((obj["ok"], obj["status"], obj["data"]["undoLabel"]), (True, "committed_unverified",
                                                                                "flag a claim"))
        markers, _ = call("list_markers", {}, self.rpc)
        self.assertIn("Fact check", [m["name"] for m in markers["data"]["markers"]])
        obj, _ = call("set_volume", {"track": "A1", "at": 5, "db": -6, "label": "duck"}, self.rpc)
        self.assertEqual((obj["data"]["verified"], obj["status"]), (True, "verified"))

    def test_panel_errors_keep_code_outcome_and_message(self):
        obj, result = call("insert_clip", {"item": "nope.mp4"}, self.rpc)
        self.assertTrue(result["isError"])
        self.assertEqual((obj["code"], obj["outcome"], obj["status"]), ("NOT_FOUND", "not_committed", "failed"))
        obj, _ = call("add_marker", {"at": 1, "name": "x", "expect_fingerprint": "0123456789abcdef"}, self.rpc)
        self.assertEqual(obj["code"], "PRECONDITION_FAILED")
        self.assertNotIn("x", [m["name"] for m in call("list_markers", {}, self.rpc)[0]["data"]["markers"]])

    def test_bridge_error_fields_and_outcomes_map_to_status(self):
        for outcome, status in (("committed", "committed_unverified"), ("partial", "partial"),
                                ("unknown", "unknown"), ("not_committed", "failed")):
            err = BridgeError("save_project: it went wrong", code="VERIFY_FAILED", outcome=outcome,
                              data={"completed": ["a"]}, hint="look in Premiere", sent=True)
            obj, result = call("save_project", {}, raising_rpc(err))
            with self.subTest(outcome=outcome):
                self.assertTrue(result["isError"])
                self.assertEqual({k: obj[k] for k in ("ok", "code", "outcome", "hint", "data", "sent", "status")},
                                 {"ok": False, "code": "VERIFY_FAILED", "outcome": outcome, "hint": "look in Premiere",
                                  "data": {"completed": ["a"]}, "sent": True, "status": status})
                self.assertEqual(obj["error"], "save_project: it went wrong")

    def test_a_bug_is_an_internal_error_not_a_crash(self):
        with self.assertLogs("ppro_bridge.mcp", "ERROR") as logs:
            obj, result = call("get_project", {}, raising_rpc(RuntimeError("boom")))
        self.assertIn("RuntimeError: boom", logs.output[0])  # the traceback goes to the log
        self.assertEqual((obj["code"], obj["outcome"], obj["status"]), ("INTERNAL", "not_committed", "failed"))
        with self.assertLogs("ppro_bridge.mcp", "ERROR"):
            obj, _ = call("add_marker", {"at": 1, "name": "x"}, raising_rpc(RuntimeError("boom")))
        self.assertEqual((obj["code"], obj["outcome"], obj["status"]), ("INTERNAL", "unknown", "unknown"))

    def test_bad_arguments_are_bad_params_and_nothing_is_sent(self):
        for args, needle in (({"bogus": 1}, "unknown argument"), ({"track": True}, "expected string"),
                             ({"track": {"a": 1}}, "expected string"), ({"kind": "pictures"}, "invalid choice")):
            obj, result = call("list_items", args, self.rpc)
            with self.subTest(args=args):
                self.assertTrue(result["isError"])
                self.assertEqual((obj["code"], obj["outcome"], obj["status"]), ("BAD_PARAMS", "not_committed",
                                                                                "failed"))
                self.assertIn(needle, obj["error"])
        obj, _ = call("set_scale", {"track": "V1", "at": 1, "value": 50, "fit": True}, self.rpc)
        self.assertIn("not allowed with", obj["error"])
        obj, _ = call("set_scale", {"track": "V1", "at": 1}, self.rpc)
        self.assertEqual(obj["code"], "BAD_PARAMS")
        obj, _ = call("list_items", ["V1"], self.rpc)
        self.assertEqual(obj["code"], "BAD_PARAMS")
        self.assertEqual(self.calls, [])

    def test_unknown_tool_is_raised_for_the_protocol_layer(self):
        with self.assertRaises(tools.UnknownTool) as cm:
            tools.call_tool("remove_markr", {}, self.rpc)
        self.assertIn("remove_marker", str(cm.exception))

    def test_status_of_apply_plan_comes_from_its_step_summary(self):
        tool = tools.registry()["apply_plan"]
        for summary, status in (({"placed": 2, "note": 1}, "verified"), ({"placed": 1, "conflict": 1}, "partial"),
                                ({"conflict": 2}, "failed"), ({"unverified": 1}, "committed_unverified")):
            self.assertEqual(tools.status_of(tool, {"ok": True, "data": {"summary": summary}}), status, summary)
        self.assertEqual(tools.status_of(tool, {"ok": True, "data": {"dryRun": True, "summary": {"planned": 1}}}),
                         "dry_run")


class ConfirmGateTest(unittest.TestCase):
    def setUp(self):
        self.rpc, self.fake, self.calls = fake_rpc()

    def _recording(self):
        seen = []

        def execute(tool, args, rpc, internal=None):
            seen.append((tool.name, dict(args), dict(internal or {})))
            return {"ok": True, "data": {"dryRun": bool(args.get("dry_run"))}}

        return seen, mock.patch.object(tools, "_execute", execute)

    def test_every_gated_command_is_a_dry_run_until_confirmed(self):
        seen, patch = self._recording()
        with patch:
            for name in sorted(ALWAYS_GATED):
                with self.subTest(tool=name):
                    obj, result = call(name, {"label": "x"} if "label" in tools.registry()[name].options else {})
                    self.assertTrue(seen[-1][1]["dry_run"])
                    self.assertFalse(result["isError"])
                    self.assertEqual((obj["status"], obj["confirmRequired"]), ("dry_run", True))
                    self.assertTrue(obj["confirmCall"]["arguments"]["confirm"])
                    self.assertEqual(obj["confirmCall"]["name"], name)
                    self.assertIn("confirmCall", obj["hint"])
                    obj, _ = call(name, obj["confirmCall"]["arguments"])
                    self.assertNotIn("dry_run", seen[-1][1])
                    self.assertNotIn("confirmRequired", obj)

    def test_conditional_gates(self):
        seen, patch = self._recording()
        with tempfile.TemporaryDirectory() as d, patch:
            existing = Path(d, "cut.mp4")
            existing.write_bytes(b"x")
            cases = [("apply_plan", {"plan": "p.json"}, False),
                     ("apply_plan", {"plan": "p.json", "overwrite": True}, True),
                     ("export_video", {"preset": "p", "output": str(existing)}, False),
                     ("export_video", {"preset": "p", "output": str(existing), "overwrite": True}, True),
                     ("export_video", {"preset": "p", "output": str(Path(d, "new.mp4")), "overwrite": True}, False),
                     ("export_video", {"job": "j1", "overwrite": True}, False)]
            for name, args, gated in cases:
                with self.subTest(tool=name, args=args):
                    obj, _ = call(name, args)
                    self.assertEqual(bool(seen[-1][1].get("dry_run")), gated)
                    self.assertEqual(obj.get("confirmRequired", False), gated)

    def test_an_explicit_dry_run_is_just_a_dry_run(self):
        seen, patch = self._recording()
        with patch:
            obj, _ = call("cut_range", {"from": 1, "to": 2, "dry_run": True})
        self.assertEqual((obj["status"], "confirmRequired" in obj), ("dry_run", False))

    def test_no_dry_run_means_refused_and_nothing_sent(self):
        obj, result = call("insert_clip", {"item": "broll_city.mp4", "at": 10, "mode": "insert"}, self.rpc)
        self.assertTrue(result["isError"])
        self.assertEqual((obj["code"], obj["outcome"], obj["sent"], obj["status"]),
                         ("CONFIRM_REQUIRED", "not_committed", False, "failed"))
        self.assertEqual(obj["confirmCall"]["arguments"]["confirm"], True)
        obj, _ = call("import_transcript", {"item": "interview.mp4", "file": "t.json", "replace": True}, self.rpc)
        self.assertEqual(obj["code"], "CONFIRM_REQUIRED")
        self.assertEqual(self.calls, [])
        obj, _ = call("insert_clip", {"mode": "insert"}, self.rpc)  # bad arguments say so first
        self.assertEqual(obj["code"], "BAD_PARAMS")
        obj, _ = call("insert_clip", {"item": "broll_city.mp4", "at": 10, "vtrack": 1, "atrack": 1, "mode": "insert",
                                      "confirm": True, "label": "push in"}, self.rpc)
        self.assertTrue(obj["ok"], obj)
        obj, _ = call("insert_clip", {"item": "broll_city.mp4", "at": 30, "vtrack": 2, "atrack": 2}, self.rpc)
        self.assertTrue(obj["ok"], obj)  # overwrite mode is not gated

    def test_remove_marker_through_the_fake_and_through_run_tool(self):
        names = lambda: [m["name"] for m in call("list_markers", {}, self.rpc)[0]["data"]["markers"]]  # noqa: E731
        self.assertIn("harness marker", names())
        obj, _ = call("run_tool", {"name": "remove_marker", "arguments": {"name": "harness marker"}}, self.rpc)
        self.assertEqual(obj["status"], "dry_run")
        self.assertIn("harness marker", names())
        self.assertEqual(obj["confirmCall"], {"name": "run_tool", "arguments": {
            "name": "remove_marker", "arguments": {"name": "harness marker", "confirm": True}}})
        obj, _ = call(obj["confirmCall"]["name"], obj["confirmCall"]["arguments"], self.rpc)
        self.assertTrue(obj["ok"], obj)
        self.assertNotIn("harness marker", names())

    def test_a_failing_dry_run_is_returned_as_the_failure(self):
        obj, result = call("remove_marker", {"name": "no such marker"}, self.rpc)
        self.assertTrue(result["isError"])
        self.assertEqual(obj["code"], "NOT_FOUND")
        self.assertNotIn("confirmRequired", obj)

    def test_ripple_confirm_also_confirms_the_ripple(self):
        obj, _ = call("remove_items", {"track": ["V1"], "from": 0, "to": 60, "ripple": True}, self.rpc)
        self.assertIn("mayShift", obj["data"])
        obj, _ = call("remove_items", dict(obj["confirmCall"]["arguments"], label="drop it"), self.rpc)
        self.assertTrue(obj["ok"], obj)
        self.assertIn("shifted", obj["data"])
        obj, _ = call("remove_items", {"track": ["V1"], "from": 0, "to": 9, "confirm_ripple": True}, self.rpc)
        self.assertEqual(obj["code"], "BAD_PARAMS")  # not the caller's to pass

    def test_clone_sequence_is_gated_only_when_it_would_prune(self):
        count = lambda: len(call("list_sequences", {}, self.rpc)[0]["data"]["sequences"])  # noqa: E731
        before = count()
        obj, _ = call("clone_sequence", {"label": "before the re-cut"}, self.rpc)
        self.assertTrue(obj["ok"] and obj["status"] == "verified" and "confirmRequired" not in obj, obj)
        self.assertEqual(count(), before + 1)
        obj, _ = call("clone_sequence", {"keep": 1}, self.rpc)
        self.assertEqual((obj["status"], obj["confirmRequired"]), ("dry_run", True))
        self.assertEqual(len(obj["data"]["prune"]), 1)
        self.assertEqual(count(), before + 1)
        obj, _ = call("clone_sequence", obj["confirmCall"]["arguments"], self.rpc)
        self.assertTrue(obj["ok"], obj)
        self.assertEqual(len(obj["data"]["pruned"]), 1)
        self.assertEqual(count(), before + 1)

    def test_confirm_must_be_boolean_and_only_where_there_is_a_gate(self):
        obj, _ = call("remove_marker", {"name": "harness marker", "confirm": "yes"}, self.rpc)
        self.assertEqual(obj["code"], "BAD_PARAMS")
        obj, _ = call("add_marker", {"at": 1, "name": "x", "confirm": True}, self.rpc)
        self.assertEqual(obj["code"], "BAD_PARAMS")
        self.assertEqual(self.calls, [])


class MetaToolsTest(unittest.TestCase):
    def test_find_tool(self):
        obj, _ = call("find_tool", {"query": "remove marker"})
        self.assertEqual(obj["data"]["tools"][0]["name"], "remove_marker")
        row = obj["data"]["tools"][0]
        self.assertEqual((row["listed"], row["confirmGated"], row["kind"]), (False, True, "write"))
        everything, _ = call("find_tool", {"query": "", "limit": 500})
        self.assertEqual(everything["data"]["total"], len(tools.registry()) - 3)
        self.assertEqual(len(call("find_tool", {"query": "", "limit": 3})[0]["data"]["tools"]), 3)
        self.assertEqual(call("find_tool", {"query": "x", "limit": 0})[0]["code"], "BAD_PARAMS")
        self.assertEqual(call("find_tool", {"query": "zzzqqq"})[0]["data"]["tools"], [])

    def test_describe_tool(self):
        obj, _ = call("describe_tool", {"name": "remove_keyframes"})
        t = tools.registry()["remove_keyframes"]
        self.assertEqual(obj["data"]["inputSchema"], t.input_schema)
        self.assertEqual((obj["data"]["listed"], obj["data"]["call"]), (False, "through run_tool"))
        obj, _ = call("describe_tool", {"name": "get_sequence"})
        self.assertEqual(obj["data"]["call"], "directly")
        obj, result = call("describe_tool", {"name": "remove_keyframe"})
        self.assertTrue(result["isError"])
        self.assertEqual(obj["code"], "NOT_FOUND")
        self.assertIn("remove_keyframes", obj["data"]["closeNames"])

    def test_run_tool(self):
        rpc, _, _ = fake_rpc()
        direct, _ = call("list_sequences", {}, rpc)
        via, _ = call("run_tool", {"name": "list_sequences"}, rpc)
        self.assertEqual(via, direct)
        self.assertEqual(call("run_tool", {"name": "run_tool", "arguments": {}}, rpc)[0]["code"], "BAD_PARAMS")
        self.assertEqual(call("run_tool", {"name": "ping", "arguments": []}, rpc)[0]["code"], "BAD_PARAMS")
        self.assertEqual(call("run_tool", {"name": "list_items", "arguments": {"nope": 1}}, rpc)[0]["code"],
                         "BAD_PARAMS")


class LocalToolsTest(unittest.TestCase):
    def test_export_transcript_then_digest(self):
        rpc, _, _ = fake_rpc()
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d, "tr.json"))
            obj, _ = call("export_transcript", {"output": path}, rpc)
            self.assertTrue(obj["ok"], obj)
            obj, _ = call("transcript_digest", {"path": path})
            self.assertEqual(obj["status"], "read")
            self.assertGreater(obj["sentenceCount"], 0)
            out = subprocess.run([sys.executable, str(REPO / "cli" / "transcript_digest.py"), path, "--json"],
                                 capture_output=True, text=True, encoding="utf-8")
            self.assertEqual({k: v for k, v in obj.items() if k != "status"}, json.loads(out.stdout))
        obj, result = call("transcript_digest", {"path": str(Path(tempfile.gettempdir(), "no_such_tr.json"))})
        self.assertTrue(result["isError"])
        self.assertEqual(obj["code"], "FILE_NOT_FOUND")

    def test_transcribe_runs_in_its_own_process(self):
        self.assertNotIn("digest", tools.registry()["transcribe"].input_schema["properties"])
        missing = str(Path(tempfile.gettempdir(), "no_such_media_for_mcp_test.mp4"))
        obj, result = call("transcribe", {"media": missing, "output": missing + ".json"})
        self.assertTrue(result["isError"])
        self.assertEqual((obj["code"], obj["status"]), ("FILE_NOT_FOUND", "failed"))


# ---------------------------------------------------------------- the token

class RedactionTest(unittest.TestCase):
    ENV_TOKEN = 'env-token-"quoted"\\and-backslashed-0123456789'
    FILE_TOKEN = "file-token-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        token_file = Path(self.dir.name, "token")
        token_file.write_text(self.FILE_TOKEN + "\n", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {auth.TOKEN_ENV: self.ENV_TOKEN,
                                                auth.TOKEN_FILE_ENV: str(token_file)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def assertClean(self, text):
        for token in (self.ENV_TOKEN, self.FILE_TOKEN):
            for form in (token, json.dumps(token)[1:-1]):
                self.assertNotIn(form, text)

    def test_no_output_carries_the_token(self):
        rpc, _, _ = fake_rpc()
        outputs = [json.dumps(tools.list_tools(all_tools=True)), tools.instructions()]
        leaky = {"ok": True, "echo": self.ENV_TOKEN, self.FILE_TOKEN: [f"x {self.FILE_TOKEN} y"]}
        with self.assertLogs("ppro_bridge.mcp", "ERROR") as logs:
            for name, args, panel in (
                    ("ping", {}, rpc), ("get_sequence", {}, rpc), ("add_marker", {"at": 2, "name": "m"}, rpc),
                    ("remove_items", {"track": ["V1"], "from": 0, "to": 60}, rpc), ("list_items", {"bogus": 1}, rpc),
                    ("find_tool", {"query": "remove"}, rpc), ("describe_tool", {"name": "relink"}, rpc),
                    ("get_project", {}, raising_rpc(BridgeError(f"refused {self.ENV_TOKEN}", code="AUTH_FAILED",
                                                                outcome="not_committed", data={"t": self.FILE_TOKEN}))),
                    ("get_project", {}, raising_rpc(RuntimeError(f"bug near {self.FILE_TOKEN}"))),
                    ("get_project", {}, lambda action, params=None: leaky),
                    ("transcript_digest", {"path": os.environ[auth.TOKEN_FILE_ENV]}, rpc)):
                result = tools.call_tool(name, args, panel)
                outputs += [result["content"][0]["text"], json.dumps(result["structuredContent"], ensure_ascii=False)]
        outputs += logs.output  # the traceback of the RuntimeError, as logged even without a redacting handler
        self.assertIn(redact.REDACTED, "".join(outputs))
        for text in outputs:
            self.assertClean(text)

    def test_log_lines_are_scrubbed(self):
        stream = io.StringIO()
        logger = logging.getLogger("ppro_bridge.mcp.test_redaction")
        handler = redact.log_handler(stream)
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.warning("token %s in a message", self.FILE_TOKEN)
            try:
                raise ValueError(self.ENV_TOKEN)
            except ValueError:
                logger.exception("failed")
        finally:
            logger.removeHandler(handler)
        self.assertIn("in a message", stream.getvalue())
        self.assertClean(stream.getvalue())

    def test_short_values_are_left_alone(self):
        with mock.patch.dict(os.environ, {auth.TOKEN_ENV: "abc"}):
            self.assertEqual(redact.scrub("abc def"), "abc def")

    def test_a_cut_off_token_is_redacted_too(self):
        """BA-3: a quote of the token file cut short (a prefix, a suffix, the middle) must not leave most of it."""
        t = self.FILE_TOKEN
        for text in (f"line 1: '{t[:-3]}…(+3 chars)'", f"…{t[5:]}", f"x{t[4:30]}y", t[:redact.FRAGMENT] + " z"):
            with self.subTest(text=text):
                out = redact.scrub(text)
                self.assertIn(redact.REDACTED, out)
                for i in range(len(t) - redact.FRAGMENT + 1):
                    self.assertNotIn(t[i:i + redact.FRAGMENT], out)
        self.assertEqual(redact.scrub(f"a {t[:redact.FRAGMENT - 1]} b"), f"a {t[:redact.FRAGMENT - 1]} b")  # too short
        self.assertEqual(redact.scrub("ordinary words, no token here"), "ordinary words, no token here")


# ---------------------------------------------------------------- the connection

class ScriptedServer:
    """A stand-in broker: `mode` "echo" answers every request on a connection, "once" answers one then closes,
    "hangup" reads the request and closes without answering. Records hellos and requests."""

    def __init__(self, mode):
        self.mode, self.hellos, self.requests = mode, [], []
        self.port = free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self.ready = threading.Event()
        threading.Thread(target=lambda: asyncio.new_event_loop().run_until_complete(self._main()), daemon=True).start()
        self.ready.wait(5)

    async def _main(self):
        async def handler(ws):
            self.hellos.append(await broker_pair(ws))
            async for raw in ws:
                msg = json.loads(raw)
                self.requests.append(msg)
                if self.mode == "hangup":
                    await ws.close()
                    return
                await ws.send(json.dumps({"id": msg["id"], "ok": True, "data": {"echo": msg["action"]},
                                          "broker": "9.9.9"}))
                if self.mode == "once":
                    await ws.close()
                    return

        async with websockets.serve(handler, "127.0.0.1", self.port):
            self.ready.set()
            await asyncio.Future()


class ConnectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = Bridge()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def test_one_connection_for_many_calls_with_the_clis_results(self):
        conn = Connection(self.bridge.url)
        try:
            for _ in range(10):
                self.assertEqual(conn("get_project"), transport.make_rpc(url=self.bridge.url)("get_project"))
            self.assertEqual((conn.stats["connects"], conn.stats["requests"]), (1, 10))
            self.assertEqual(conn("ping")["brokerVersion"], transport.make_rpc(url=self.bridge.url)("ping")
                             ["brokerVersion"])
        finally:
            conn.close()

    def test_tools_over_the_connection(self):
        conn = Connection(self.bridge.url)
        try:
            obj, _ = call("get_sequence", {}, conn)
            self.assertEqual((obj["ok"], obj["status"]), (True, "read"))
            obj, _ = call("add_marker", {"at": 3, "name": "over the wire", "label": "mcp test"}, conn)
            self.assertTrue(obj["ok"], obj)
            obj, _ = call("remove_marker", {"name": "over the wire"}, conn)
            self.assertEqual(obj["status"], "dry_run")
            obj, _ = call("remove_marker", obj["confirmCall"]["arguments"], conn)
            self.assertTrue(obj["ok"], obj)
            self.assertEqual(conn.stats["connects"], 1)
        finally:
            conn.close()

    def test_hello_is_the_clis(self):
        server = ScriptedServer("echo")
        conn = Connection(server.url)
        try:
            self.assertEqual(conn("get_project"), {"echo": "get_project"})
            hello = server.hellos[0]  # the CLI's pairing hello (DOC-8: the token is proved, never sent)
            self.assertEqual((len(server.hellos), set(hello), hello["role"], hello["auth"]),
                             (1, {"role", "auth", "nonce"}, "controller", auth.SCHEME))
            self.assertTrue(auth.valid_nonce(hello["nonce"]))
            self.assertEqual(server.requests[0]["timeoutSeconds"], spec.timeout("get_project"))
        finally:
            conn.close()

    def test_reconnects_when_the_broker_closed_the_idle_connection(self):
        server = ScriptedServer("once")
        conn = Connection(server.url)
        try:
            for _ in range(3):
                self.assertEqual(conn("list_markers"), {"echo": "list_markers"})
                deadline = time.monotonic() + 5
                while conn.connected and time.monotonic() < deadline:  # idle: the close reaches the client
                    time.sleep(0.01)
            self.assertEqual(len(server.requests), 3)
            self.assertEqual(conn.stats["connects"], 3)
        finally:
            conn.close()

    def test_a_sent_request_is_never_sent_twice(self):
        server = ScriptedServer("hangup")
        conn = Connection(server.url)
        try:
            reply = conn.call("add_marker", {"atSeconds": 1.0, "name": "x"})
            self.assertEqual((reply["code"], reply["sent"], reply["outcome"]), ("CONNECTION_CLOSED", True, "unknown"))
            self.assertEqual(len(server.requests), 1)
            with self.assertRaises(BridgeError) as cm:
                conn("get_project")
            self.assertEqual((cm.exception.code, cm.exception.outcome), ("CONNECTION_CLOSED", "not_committed"))
            self.assertEqual(len(server.requests), 2)
        finally:
            conn.close()

    def test_no_broker(self):
        conn = Connection(f"ws://127.0.0.1:{free_port()}")
        try:
            reply = conn.call("get_project")
            self.assertEqual((reply["code"], reply["sent"], reply["outcome"]), ("NO_BROKER", False, "not_committed"))
        finally:
            conn.close()

    def test_a_refused_token_drops_the_connection_and_never_shows(self):
        conn = Connection(self.bridge.url)
        wrong = "wrong-token-for-this-test-0123456789abcdef"
        try:
            with mock.patch.dict(os.environ, {auth.TOKEN_ENV: wrong}):
                reply = conn.call("get_project")
            self.assertEqual((reply["code"], reply["outcome"]), ("AUTH_FAILED", "not_committed"))
            self.assertNotIn(wrong, json.dumps(reply))
            self.assertFalse(conn.connected)
            self.assertIn("name", conn("get_project"))  # the right token again, on a new connection
            self.assertEqual(conn.stats["connects"], 2)
        finally:
            conn.close()

    def test_close_and_use_again(self):
        conn = Connection(self.bridge.url)
        conn("get_project")
        conn.close()
        self.assertIn("name", conn("get_project"))
        conn.close()
        conn.close()  # twice is fine


class OptionalSdkTest(unittest.TestCase):
    def test_nothing_outside_the_mcp_package_imports_the_sdk(self):
        pattern = re.compile(r"^\s*(import\s+mcp\b|from\s+mcp\b)", re.M)
        # shipped code only: the tests may drive the server with the SDK's own client (guarded by skipIf)
        offenders = [str(p.relative_to(REPO)) for p in REPO.rglob("*.py")
                     if "__pycache__" not in p.parts and (REPO / "ppro_bridge" / "mcp") not in p.parents
                     and (REPO / "tests") not in p.parents
                     and p.relative_to(REPO).parts[0] not in ("dist", "python", "build")  # a staged / bundled Python
                     and pattern.search(p.read_text(encoding="utf-8", errors="replace"))]
        self.assertEqual(offenders, [])

    def test_cli_and_tools_import_without_the_sdk(self):
        code = ("import sys; sys.modules['mcp'] = None; sys.path.insert(0, sys.argv[1]); "
                "import ppro_bridge.cli, ppro_bridge.mcp.tools, ppro_bridge.mcp.connection, ppro_bridge.mcp.redact; "
                "print(len(ppro_bridge.mcp.tools.registry()))")
        out = subprocess.run([sys.executable, "-c", code, str(REPO)], capture_output=True, text=True,
                             env=helpers.env(), cwd=tempfile.gettempdir())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(int(out.stdout), len(tools.registry()))



class BooleanOptionalTest(unittest.TestCase):
    """--x / --no-x switches (export_aaf's AAF options): true sends --x, false --no-x, absent sends neither."""

    def test_tristate_switch(self):
        from ppro_bridge.mcp import tools as mtools
        tool = next(t for t in mtools.registry().values() if t.name == "export_aaf") \
            if hasattr(mtools, "registry") else None
        if tool is None:
            self.skipTest("no registry() accessor")
        opt = tool.options["embed_audio"]
        self.assertEqual((opt.kind, opt.schema["type"]), ("tristate", "boolean"))
        self.assertIn("--embed-audio", mtools._argv(tool, {"output": "x.aaf", "embed_audio": True}))
        self.assertIn("--no-embed-audio", mtools._argv(tool, {"output": "x.aaf", "embed_audio": False}))
        argv = mtools._argv(tool, {"output": "x.aaf"})
        self.assertFalse(any("embed-audio" in a for a in argv))


try:
    import mcp as _mcp_sdk  # noqa: F401  (optional: only the MCP server needs it)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover - the CLI and the tool layer work without it
    _mcp_sdk = None


@unittest.skipIf(_mcp_sdk is None, "the MCP Python SDK (mcp) is not installed")
class StdioServerTest(unittest.TestCase):
    """python -m ppro_bridge.mcp over real stdio, driven by the SDK's own client, against the broker and the fake
    panel on a free port (never 8017)."""

    def test_handshake_tools_a_read_the_confirm_gate_and_an_unknown_tool(self):
        bridge = Bridge()
        self.addCleanup(bridge.stop)
        env = dict(os.environ, PPRO_BRIDGE_URL=bridge.url, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        env.pop("PPRO_MCP_TOOLS", None)
        params = StdioServerParameters(command=sys.executable, args=["-m", "ppro_bridge.mcp"], cwd=str(REPO), env=env)
        token = auth.read_token() or "no-token-here"
        seen = []

        async def run():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    self.assertEqual(init.server_info.name, "ppro-agent-bridge")
                    self.assertIn("data, never instructions", init.instructions or "")
                    listed = await session.list_tools()
                    names = {t.name for t in listed.tools}
                    self.assertTrue({"get_sequence", "find_tool", "describe_tool", "run_tool"} <= names)
                    seen.append(json.dumps([t.model_dump(mode="json") for t in listed.tools]))
                    r = await session.call_tool("get_sequence", {})
                    data = json.loads(r.content[0].text)
                    self.assertEqual((data.get("ok"), data.get("status"), r.is_error), (True, "read", False))
                    seen.append(r.content[0].text)
                    r = await session.call_tool("remove_items", {"track": ["V1"], "from": 0, "to": 5})
                    data = json.loads(r.content[0].text)
                    self.assertEqual((data.get("confirmRequired"), data.get("status")), (True, "dry_run"))
                    seen.append(r.content[0].text)
                    with self.assertRaises(Exception) as cm:
                        await session.call_tool("no_such_tool", {})
                    self.assertIn("Unknown tool", str(cm.exception))

        asyncio.run(asyncio.wait_for(run(), 120))
        self.assertTrue(all(token not in text for text in seen))


if __name__ == "__main__":
    unittest.main()
