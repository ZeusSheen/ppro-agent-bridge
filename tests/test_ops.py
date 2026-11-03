"""ops: the command functions with a stub panel — the handshake (PANEL_OUTDATED), transcribe_clip polling,
replace_clip's keyframe pre-check and partial outcome, set_params/add_transition request shaping, ping's
compatibility/stale checks, ffprobe failures, and the CLI's error mapping."""

import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from helpers import BridgeError, fake_rpc, ping_reply, stub_rpc

from ppro_bridge import apiprobe, cli, framegrid, items, media, ops, panelhash, paths, replace, spec, transcript
from ppro_bridge import presets
from ppro_bridge._version import VERSION

SEQ = {"name": "Seq", "timebase": "4237833600", "fps": 59.94, "width": 1080, "height": 1920}
FRAME = 1001 / 60000
ITEMS = {"items": [{"name": "a.mp4", "bin": "", "mediaPath": "/fake/a.mp4"},
                   {"name": "title.mov", "bin": "EN", "mediaPath": "/fake/en/title.mov"},
                   {"name": "title.mov", "bin": "CN", "mediaPath": "/fake/cn/title.mov"}]}
SPK = str(uuid.UUID(int=1, version=4))
DOC = {"language": "en-us", "speakers": [{"id": SPK, "name": "Speaker 1"}], "segments": [
    {"start": 0.0, "duration": 0.5, "language": "en-us", "speaker": SPK, "words": [
        {"text": "Hello.", "start": 0.0, "duration": 0.5, "eos": True, "confidence": 0.9, "tags": [],
         "type": "word"}]}]}


def tmpdir() -> Path:
    d = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(d.cleanup)
    return Path(d.name)


def write_json(obj, name="f.json") -> Path:
    p = tmpdir() / name
    p.write_bytes(json.dumps(obj).encode("utf-8"))
    return p


class HandshakeTest(unittest.TestCase):
    """Commands with minProtocol 2 or requirePanel refuse an older panel before they write anything."""

    def old_panel(self, **extra):
        return stub_rpc({"ping": ping_reply(protocol=1), "list_project_items": ITEMS, "get_sequence": SEQ, **extra})

    def assert_outdated(self, fn, rpc):
        with self.assertRaises(BridgeError) as cm:
            fn(rpc)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("PANEL_OUTDATED", "not_committed"))
        self.assertEqual([a for a, _ in rpc.calls], ["ping"])
        return cm.exception

    def test_protocol_1_panel_is_refused(self):
        params = write_json([{"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "value": 50}]}])
        transcript = write_json(DOC)
        for fn in (lambda r: ops.set_params(r, track="V1", at=1.0, json_file=params),
                   lambda r: ops.replace_clip(r, track="V1", at=1.0, with_item="a.mp4", dry_run=True),
                   lambda r: ops.import_transcript(r, item="a.mp4", file=transcript),
                   lambda r: ops.export_transcript(r, output=tmpdir() / "t.json", item="a.mp4"),
                   lambda r: ops.transcribe_clip(r, item="a.mp4", wait=0)):
            e = self.assert_outdated(fn, self.old_panel())
            self.assertEqual(e.data["panelProtocol"], 1)
            self.assertEqual(e.data["needProtocol"], 2)

    def test_missing_action_is_refused(self):
        actions = [a for a in spec.action_names() if a != "add_transition"]
        rpc = stub_rpc({"ping": ping_reply(actions=actions)})
        e = self.assert_outdated(lambda r: ops.add_transition(r, track="V1", at=1.0, match_name="X", duration=0.5,
                                                              edge="start"), rpc)
        self.assertEqual(e.data["missing"], ["add_transition"])

    def test_plain_commands_do_not_ping(self):
        rpc = stub_rpc({"list_project_items": ITEMS, "insert_clip": lambda p: {"placed": True, **p}})
        ops.insert_clip(rpc, item="a.mp4", at=2.0)
        self.assertEqual([a for a, _ in rpc.calls], ["list_project_items", "insert_clip"])


class PingTest(unittest.TestCase):
    def test_compatible(self):
        data = ops.ping(stub_rpc({"ping": ping_reply()}))
        self.assertTrue(data["compatible"], data)
        self.assertEqual(data["cliVersion"], VERSION)
        self.assertNotIn("warning", data)

    def test_probe_is_passed_on(self):
        rpc = stub_rpc({"ping": ping_reply()})
        ops.ping(rpc, probe=True)
        self.assertEqual(rpc.calls, [("ping", {"probe": True})])

    def test_old_panel_broker_denied_and_stuck(self):
        cases = {"panel": ping_reply(version="0.3.3", protocol=None),
                 "broker": ping_reply(brokerVersion="0.3.3"),
                 "denylist": ping_reply(denied={"match": "27.1.*", "reason": "test build"}),
                 "stuck": ping_reply(stuck={"action": "insert_clip"})}
        for what, reply in cases.items():
            data = ops.ping(stub_rpc({"ping": reply}))
            self.assertIn("warning", data, what)
            if what != "stuck":
                self.assertFalse(data["compatible"], what)
        self.assertIn("test build", ops.ping(stub_rpc({"ping": cases["denylist"]}))["warning"])

    def test_protocol_mismatch_is_incompatible(self):
        data = ops.ping(stub_rpc({"ping": ping_reply(protocol=spec.protocol() + 1)}))
        self.assertFalse(data["compatible"])

    def test_stale_panel(self):
        plugin = tmpdir() / "plugin"
        (plugin / "lib").mkdir(parents=True)
        for name, text in (("index.html", "<html>"), ("main.js", "main"), ("manifest.json", "{}"),
                           ("lib/core.js", "core"), ("lib/spec.js", "spec")):
            (plugin / name).write_bytes(text.encode("utf-8"))
        local = panelhash.code_hash(plugin)
        with mock.patch.object(ops, "PLUGIN_DIR", plugin):
            fresh = ops.ping(stub_rpc({"ping": ping_reply(codeHash=local)}))
            stale = ops.ping(stub_rpc({"ping": ping_reply(codeHash="00000000")}))
            unknown = ops.ping(stub_rpc({"ping": ping_reply()}))
        self.assertEqual((fresh["localCodeHash"], fresh["stale"]), (local, False))
        self.assertTrue(stale["stale"])
        self.assertIn("stale", stale["warning"])
        self.assertNotIn("stale", unknown)


class PanelHashTest(unittest.TestCase):
    def test_matches_the_documented_algorithm(self):
        plugin = tmpdir()
        files = {"main.js": b"a", "manifest.json": b"{}", "index.html": b"<i>", "lib/b.js": b"\xe4\xb8\xad",
                 "lib/a.js": b""}
        (plugin / "lib").mkdir()
        for name, data in files.items():
            (plugin / name).write_bytes(data)
        (plugin / "lib" / "notes.txt").write_bytes(b"ignored")
        (plugin / "README.md").write_bytes(b"ignored")
        h = 0x811C9DC5
        for name in ["index.html", "lib/a.js", "lib/b.js", "main.js", "manifest.json"]:
            for byte in name.encode() + b"\0" + files[name] + b"\0":
                h = ((h ^ byte) * 0x01000193) % 2 ** 32
        self.assertEqual(panelhash.code_hash(plugin), f"{h:08x}")

    def test_missing_folder_or_file(self):
        self.assertIsNone(panelhash.code_hash(tmpdir() / "nope"))
        d = tmpdir()
        (d / "main.js").write_bytes(b"x")
        self.assertIsNone(panelhash.code_hash(d))


class TranscribeClipTest(unittest.TestCase):
    START = {"started": True, "jobId": "j1", "itemName": "a.mp4", "binPath": "", "language": "en-US"}

    def panel(self, states, export=True):
        states = iter(states)

        def jobs(p):
            assert p == {"jobsOnly": True}
            state = next(states)
            if state is None:
                return {"jobs": []}
            return {"jobs": [{"jobId": "j1", "itemName": "a.mp4", "binPath": "", "language": "en-US",
                              "state": state, "error": "no audio" if state == "failed" else None,
                              "startedAt": 1, "finishedAt": None, "hasTranscript": state == "done"}]}

        handlers = {"ping": ping_reply(), "list_project_items": ITEMS, "transcribe_clip": self.START,
                    "list_transcripts": jobs}
        if export:
            handlers["export_transcript"] = {"itemName": "a.mp4", "binPath": "", "transcriptJson": json.dumps(DOC)}
        return stub_rpc(handlers)

    def test_waits_then_exports(self):
        rpc, sleeps = self.panel(["running", "running", "done"]), []
        out = tmpdir() / "tr.json"
        result = ops.transcribe_clip(rpc, item="a.mp4", language="en-US", wait=60, poll=5, output=out,
                                     sleep=sleeps.append)
        self.assertEqual(sleeps, [5, 5, 5])
        self.assertEqual((result["state"], result["hasTranscript"], result["jobId"]), ("done", True, "j1"))
        self.assertEqual(json.loads(out.read_bytes()), DOC)
        self.assertEqual(result["savedTo"], str(out.resolve()))
        sent = dict(rpc.calls)["transcribe_clip"]
        self.assertEqual(sent, {"itemName": "a.mp4", "binPath": "", "language": "en-US"})

    def test_wait_zero_returns_the_start_reply(self):
        rpc, sleeps = self.panel([]), []
        self.assertEqual(ops.transcribe_clip(rpc, item="a.mp4", wait=0, sleep=sleeps.append), self.START)
        self.assertEqual(sleeps, [])
        self.assertNotIn("list_transcripts", [a for a, _ in rpc.calls])

    def test_timeout(self):
        rpc, sleeps = self.panel(["running"] * 10), []
        with self.assertRaises(BridgeError) as cm:
            ops.transcribe_clip(rpc, item="a.mp4", wait=12, poll=5, sleep=sleeps.append)
        e = cm.exception
        self.assertEqual((e.code, e.outcome, e.data["jobId"]), ("TRANSCRIBE_TIMEOUT", "unknown", "j1"))
        self.assertEqual(len(sleeps), 3)

    def test_lost_and_failed(self):
        for states, code in (([None], "TRANSCRIBE_LOST"), (["running", "failed"], "TRANSCRIBE_FAILED")):
            with self.assertRaises(BridgeError) as cm:
                ops.transcribe_clip(self.panel(states, export=False), item="a.mp4", wait=60, poll=1,
                                    sleep=lambda s: None)
            self.assertEqual((cm.exception.code, cm.exception.outcome), (code, "unknown"))

    def test_ambiguous_item_is_refused_before_starting(self):
        rpc = self.panel([])
        with self.assertRaises(BridgeError) as cm:
            ops.transcribe_clip(rpc, item="title.mov", wait=0)
        self.assertEqual(cm.exception.code, "AMBIGUOUS")
        self.assertNotIn("transcribe_clip", [a for a, _ in rpc.calls])


class ReplacePanel:
    """Just enough panel for a real replace_clip run on V2 (index 1)."""

    def __init__(self, old, fail_on=None):
        self.clip, self.old_name, self.fail_on, self.sets = dict(old), old["name"], fail_on, []

    def handlers(self):
        return {"get_sequence": SEQ, "inspect_items": self.inspect, "list_items": self.list_items,
                "export_otio": self.export_otio, "remove_items": self.remove, "insert_clip": self.insert,
                "set_params": self.set_params}

    def maybe_fail(self, action):
        if self.fail_on == action:
            raise BridgeError(f"{action}: boom", code="TRANSACTION_FAILED", outcome="not_committed")

    def inspect(self, p):
        return {"tracks": [{"index": 1, "clips": [self.clip] if self.clip else [], "transitions": []}]}

    def list_items(self, p):
        return {"audio": [{"index": i, "label": f"A{i + 1}", "items": []} for i in range(2)],
                "video": [{"index": 1, "label": "V2", "items": [self.clip] if self.clip else []}]}

    def export_otio(self, p):
        doc = {"tracks": {"children": [{"kind": "Video", "children": []}, {"kind": "Video", "children": []}]}}
        Path(p["outputPath"]).write_bytes(json.dumps(doc).encode("utf-8"))
        return {"exported": True, "outputPath": p["outputPath"]}

    def remove(self, p):
        removed = [{"name": self.clip["name"], "track": "V2", "start": self.clip["start"]}] if self.clip else []
        if not p.get("dryRun"):
            self.clip = None
        return {"dryRun": bool(p.get("dryRun")), "mode": "inside", "removed": removed}

    def insert(self, p):
        self.maybe_fail("insert_clip")
        src_in, src_out = p.get("inSeconds", 0.0), p.get("outSeconds", 30.0)
        self.clip = {"name": p["itemName"], "start": p["atSeconds"], "end": p["atSeconds"] + src_out - src_in,
                     "inPoint": src_in, "outPoint": src_out, "speed": 1,
                     "components": [{"matchName": "AE.ADBE Opacity", "occurrence": 1, "params": [
                         {"index": 0, "value": 100.0, "keyframed": False}]}]}
        return {"placed": True}

    def set_params(self, p):
        if p.get("dryRun"):  # replace_clip asking whether its filters can be created: validated, nothing written
            return {"dryRun": True, "set": [], "createdEffects": [c["matchName"] for c in p["components"]],
                    "keyframes": [], "undoLabel": p.get("undoLabel") or ""}
        self.maybe_fail("set_params")
        self.sets.append(p)
        for comp in p["components"]:
            same = [c for c in self.clip["components"] if c["matchName"] == comp["matchName"]]
            while len(same) < comp.get("occurrence", 1):
                same.append({"matchName": comp["matchName"], "params": [{"index": 0, "value": 0.0}]})
                self.clip["components"].append(same[-1])
            target = same[comp.get("occurrence", 1) - 1]
            for prm in comp["params"]:
                slot = next(x for x in target["params"] if x["index"] == prm["index"])
                if prm.get("keyframes"):
                    slot.update(keyframed=True, keyframes=[dict(k) for k in prm["keyframes"]], value=prm["value"])
                else:
                    slot.update(value=prm["value"])
        return {"set": [], "createdEffects": [], "keyframes": [], "undoLabel": p.get("undoLabel")}


OLD = {"name": "g01.mov", "start": 10.0, "end": 15.0, "inPoint": 2.0, "outPoint": 7.0, "speed": 1,
       "components": [
           {"matchName": "AE.ADBE Opacity", "occurrence": 1, "params": [
               {"index": 0, "value": 100.0, "keyframed": True, "timeBase": "media",
                "keyframes": [{"t": 2.0, "value": 0.0}, {"t": 6.9, "value": 100.0}]}]},
           {"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 1, "params": [{"index": 0, "value": 5.0}]},
           {"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 2, "params": [{"index": 0, "value": 9.0}]}]}
NEW = {"name": "g01_cn.mov", "bin": "CN", "mediaPath": "/fake/cn/g01_cn.mov"}
SILENT = staticmethod(lambda path: {"hasAudio": False, "duration": 30.0, "fps": 25.0})  # OLD's 2 s / 7 s are frames


class ReplaceTest(unittest.TestCase):
    def swap(self, panel, **kw):
        kw.setdefault("keep_transitions", False)
        return replace.replace_clip(stub_rpc(panel.handlers()), 1, 12.0, NEW, probe_media=SILENT, **kw)

    def test_keyframes_outside_the_new_range_are_refused_in_a_dry_run(self):
        with self.assertRaises(BridgeError) as cm:
            self.swap(ReplacePanel(OLD), dry_run=True, src_in=2.0, src_out=5.0)
        e = cm.exception
        self.assertEqual((e.code, e.outcome), ("KEYFRAME_OUT_OF_RANGE", "not_committed"))
        self.assertEqual([k["t"] for k in e.data["keyframes"]], [6.9])

    def test_drop_outside_keyframes(self):
        report = self.swap(ReplacePanel(OLD), dry_run=True, src_in=2.0, src_out=5.0, drop_outside_keyframes=True)
        self.assertEqual([k["t"] for k in report["droppedKeyframes"]], [6.9])
        panel = ReplacePanel(OLD)
        report = self.swap(panel, src_in=2.0, src_out=5.0, drop_outside_keyframes=True)
        self.assertTrue(report["verified"], report["problems"])
        opacity = next(c for c in panel.sets[-1]["components"] if c["matchName"] == "AE.ADBE Opacity")
        self.assertEqual(opacity["params"][0]["keyframes"], [{"t": 2.0, "value": 0.0}])

    def test_occurrence_and_media_time_base_are_sent(self):
        panel = ReplacePanel(OLD)
        report = self.swap(panel, src_in=4.0)  # keyframes move with the in point (+2 s)
        self.assertTrue(report["verified"], report["problems"])
        readd, restore = panel.sets
        self.assertEqual(readd["components"], [{"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 2,
                                                "params": []}])
        by_key = {(c["matchName"], c["occurrence"]): c["params"] for c in restore["components"]}
        self.assertEqual(by_key[("AE.ADBE Opacity", 1)][0]["timeBase"], "media")
        self.assertEqual([k["t"] for k in by_key[("AE.ADBE Opacity", 1)][0]["keyframes"]], [4.0, 8.9])
        self.assertEqual(by_key[("AE.ADBE Gaussian Blur 2", 2)], [{"index": 0, "value": 9.0}])
        self.assertIn("AE.ADBE Gaussian Blur 2[2]#0", report["restoredParams"])
        self.assertEqual(report["keyframeShift"], 2.0)

    def test_failure_after_the_removal_is_partial(self):
        for fail_on, completed in (("insert_clip", ["remove g01.mov"]),
                                   ("set_params", ["remove g01.mov", "insert g01_cn.mov"])):
            with self.assertRaises(BridgeError) as cm:
                self.swap(ReplacePanel(OLD, fail_on=fail_on))
            e = cm.exception
            self.assertEqual((e.code, e.outcome), ("TRANSACTION_FAILED", "partial"), fail_on)
            self.assertEqual(e.data["completed"], completed)
            self.assertEqual(e.to_reply()["outcome"], "partial")

    def test_failure_before_the_removal_is_not_committed(self):
        panel = ReplacePanel(OLD)
        handlers = panel.handlers()
        handlers["remove_items"] = lambda p: {"dryRun": True, "mode": "inside", "removed": []}
        with self.assertRaises(BridgeError) as cm:
            replace.replace_clip(stub_rpc(handlers), 1, 12.0, NEW, probe_media=SILENT, keep_transitions=False)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("PRECONDITION_FAILED", "not_committed"))

    def test_ops_replace_clip_checks_panel_then_resolves(self):
        panel = ReplacePanel(OLD)
        items = {"items": [dict(NEW)]}
        rpc = stub_rpc(dict(panel.handlers(), ping=ping_reply(), list_project_items=items))
        report = ops.replace_clip(rpc, track="V2", at=12.0, with_item="CN/g01_cn.mov", dry_run=True,
                                  no_transitions=True, probe_media=SILENT)
        self.assertEqual(report["new"], "CN/g01_cn.mov")
        self.assertEqual([a for a, _ in rpc.calls][:2], ["ping", "list_project_items"])


class SetParamsTest(unittest.TestCase):
    def panel(self):
        return stub_rpc({"ping": ping_reply(), "get_sequence": SEQ,
                         "set_params": lambda p: {"set": [], "createdEffects": [], "keyframes": [],
                                                  "undoLabel": p.get("undoLabel")}})

    def test_list_or_object_file(self):
        comps = [{"matchName": "AE.ADBE Opacity", "params": [
            {"index": 0, "timeBase": "clip", "keyframes": [{"t": 0, "value": 0}, {"t": 0.5, "value": 100}]}]}]
        for doc in (comps, {"components": comps}):
            rpc = self.panel()
            ops.set_params(rpc, track="V3", at=4.0, json_file=write_json(doc), label="fade", expect_seq="Seq")
            sent = dict(rpc.calls)["set_params"]
            self.assertEqual(sent, {"videoTrack": 2, "atSeconds": 4.0, "components": comps, "undoLabel": "fade"})

    def test_bad_requests_are_refused_locally(self):
        cases = [[{"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "keyframes": [{"t": 0, "value": 0}]}]}],
                 [{"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "value": 1, "bogus": 1}]}],
                 {"components": [], "extra": 1},
                 []]
        for doc in cases:
            rpc = self.panel()
            with self.assertRaises(BridgeError) as cm:
                ops.set_params(rpc, track="V1", at=1.0, json_file=write_json(doc))
            self.assertEqual(cm.exception.code, "BAD_PARAMS", doc)
            self.assertEqual(rpc.calls, [], doc)

    def test_audio_track_and_missing_file(self):
        with self.assertRaises(BridgeError) as cm:
            ops.set_params(self.panel(), track="A1", at=1.0, json_file=write_json([]))
        self.assertEqual(cm.exception.code, "BAD_PARAMS")
        with self.assertRaises(BridgeError) as cm:
            ops.set_params(self.panel(), track="V1", at=1.0, json_file=tmpdir() / "none.json")
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")


class TransitionPanel:
    """V2 holds one clip, 10-15 s; export_otio shows the transitions add_transition put there (or, with
    `sticks=False`, nothing: a panel that says added while Premiere kept the timeline as it was)."""

    def __init__(self, sticks=True, existing=None):
        self.sticks, self.transitions = sticks, list(existing or [])

    def handlers(self):
        return {"ping": ping_reply(), "get_sequence": SEQ, "list_items": self.list_items,
                "export_otio": self.export_otio, "add_transition": self.add}

    def list_items(self, p):
        return {"video": [{"index": 1, "label": "V2", "items": [{"name": "a.mp4", "start": 10.0, "end": 15.0}]}]}

    def add(self, p):
        if self.sticks:
            frames = int(p["durationSeconds"] / FRAME + 1e-9)
            cut = 10.0 if p["applyToStart"] else 15.0
            single = p["forceSingleSided"]
            split = (0, frames) if p["applyToStart"] and single else (frames, 0) if single else (frames / 2, frames / 2)
            self.transitions.append((cut, split, p["matchName"]))
        return {"added": True, **p}

    def export_otio(self, p):
        rt = lambda frames: {"rate": 1 / FRAME, "value": frames}  # noqa: E731
        children = [{"OTIO_SCHEMA": "Gap.1", "source_range": {"duration": rt(10.0 / FRAME)}}]
        for cut, (before, after), match in sorted(self.transitions):
            if cut > 10.0:
                children.append({"OTIO_SCHEMA": "Clip.2", "source_range": {"duration": rt(5.0 / FRAME)}})
            children.append({"OTIO_SCHEMA": "Transition.1", "name": "Cross Dissolve", "in_offset": rt(before),
                             "out_offset": rt(after), "metadata": {"PremierePro_OTIO": {"MatchName": match}}})
        doc = {"tracks": {"children": [{"kind": "Video", "children": []}, {"kind": "Video", "children": children}]}}
        Path(p["outputPath"]).write_bytes(json.dumps(doc).encode("utf-8"))
        return {"exported": True, "outputPath": p["outputPath"]}


class AddTransitionTest(unittest.TestCase):
    def run_it(self, panel=None, **kw):
        rpc = stub_rpc((panel or TransitionPanel()).handlers())
        data = ops.add_transition(rpc, track="V2", at=12.0, match_name="AE.ADBE Cross Dissolve New", **kw)
        return dict(rpc.calls)["add_transition"], data

    def test_reads_the_transition_back(self):
        _, data = self.run_it(duration=0.5, edge="end")
        self.assertTrue(data["verified"])
        self.assertEqual((round(data["transition"]["before"] / FRAME), data["transition"]["after"]), (30, 0))

    def test_nothing_landing_is_a_committed_failure(self):
        with self.assertRaises(BridgeError) as cm:
            self.run_it(TransitionPanel(sticks=False), duration=0.5, edge="start")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))

    def test_an_occupied_edit_point_is_refused_before_writing(self):
        panel = TransitionPanel(existing=[(15.0, (12, 0), "AE.ADBE Dip To Black")])
        rpc = stub_rpc(panel.handlers())
        with self.assertRaises(BridgeError) as cm:
            ops.add_transition(rpc, track="V2", at=12.0, match_name="AE.ADBE Cross Dissolve New", duration=0.5,
                               edge="end")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("TRANSITION_EXISTS", "not_committed"))
        self.assertNotIn("add_transition", [a for a, _ in rpc.calls])
        self.assertIn("Delete", cm.exception.hint)

    def test_start_edge_single_sided_whole_frames(self):
        sent, data = self.run_it(duration=0.5, edge="start", label="dissolve in")
        self.assertEqual((sent["videoTrack"], sent["applyToStart"], sent["forceSingleSided"]), (1, True, True))
        self.assertAlmostEqual(sent["durationSeconds"], 30.1 * FRAME)
        self.assertEqual((data["durationFrames"], sent["undoLabel"]), (30, "dissolve in"))

    def test_end_edge_double_sided(self):
        sent, _ = self.run_it(duration=1.0, edge="end", double_sided=True)
        self.assertEqual((sent["applyToStart"], sent["forceSingleSided"]), (False, False))

    def test_refusals(self):
        for kw in ({"duration": 0.5, "edge": "middle"}, {"duration": 0, "edge": "start"},
                   {"duration": 0.001, "edge": "start"}):
            with self.assertRaises(BridgeError) as cm:
                self.run_it(**kw)
            self.assertEqual(cm.exception.code, "BAD_PARAMS", kw)

    def test_expect_seq(self):
        rpc = stub_rpc({"ping": ping_reply(), "get_sequence": SEQ})
        with self.assertRaises(BridgeError) as cm:
            ops.add_transition(rpc, track="V2", at=1.0, match_name="X", duration=0.5, edge="end", expect_seq="Other")
        self.assertEqual(cm.exception.code, "SEQUENCE_MISMATCH")


class RemovalPanel(TransitionPanel):
    """TransitionPanel plus remove_transition: `takes` = how many transitions the removal deletes (0: Premiere kept
    the timeline as it was; 2: it took a neighbour's too) while the panel still reports one removed."""

    def __init__(self, takes=1, existing=None):
        super().__init__(existing=existing)
        self.takes = takes

    def handlers(self):
        return dict(super().handlers(), remove_transition=self.remove)

    def remove(self, p):
        n = len(self.transitions)
        if not p["dryRun"]:
            cut = 10.0 if p["edge"] == "start" else 15.0
            mine = [t for t in self.transitions if t[0] == cut][:self.takes]
            others = [t for t in self.transitions if t[0] != cut][:max(0, self.takes - 1)]
            self.transitions = [t for t in self.transitions if t not in mine + others]
        return {"dryRun": p["dryRun"], "track": "V2", "edge": p["edge"], "cut": 10.0, "neighbour": None,
                "clip": {"name": "a.mp4", "start": 10.0, "end": 15.0}, "removed": not p["dryRun"],
                "via": None if p["dryRun"] else "clip", "transitionCount": {"before": n}}


class RemovalsTest(unittest.TestCase):
    """remove_transition (OTIO before and after), remove_effect and remove_keyframes: refused locally, verified."""
    DIP = "AE.ADBE Dip To Black"
    FILM = "AE.ADBE Film Dissolve"

    def test_remove_transition_finds_it_in_otio_and_verifies_it_is_gone(self):
        panel = RemovalPanel(existing=[(10.0, (0, 12), self.DIP), (15.0, (30, 0), self.FILM)])
        rpc = stub_rpc(panel.handlers())
        dry = ops.remove_transition(rpc, track="V2", at=12.0, edge="end", dry_run=True)
        t = dry["transition"]
        self.assertEqual((t["matchName"], t["at"], round(t["before"] / FRAME), t["after"], t["owner"]),
                         (self.FILM, 15.0, 30, 0, "clip"))
        self.assertEqual(len(panel.transitions), 2)
        data = ops.remove_transition(rpc, track="V2", at=12.0, edge="end", label="Agent: hard out")
        self.assertEqual((data["verified"], data["transition"]["matchName"]), (True, self.FILM))
        self.assertEqual(dict(rpc.calls)["remove_transition"]["undoLabel"], "Agent: hard out")
        self.assertEqual([m for _, _, m in panel.transitions], [self.DIP])

    def test_no_transition_at_the_edge_sends_nothing(self):
        rpc = stub_rpc(RemovalPanel(existing=[(10.0, (0, 12), self.DIP)]).handlers())
        with self.assertRaises(BridgeError) as cm:
            ops.remove_transition(rpc, track="V2", at=12.0, edge="end")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("NOT_FOUND", "not_committed"))
        self.assertEqual(cm.exception.data["transitions"][0]["matchName"], self.DIP)
        dry = ops.remove_transition(rpc, track="V2", at=12.0, edge="end", dry_run=True)
        self.assertEqual((dry["wouldRefuse"], dry["transition"]), ("NOT_FOUND", None))
        self.assertNotIn("remove_transition", [a for a, _ in rpc.calls])

    def test_a_removal_the_otio_export_disagrees_with_is_a_committed_failure(self):
        for takes, key in ((0, "stillThere"), (2, "alsoGone")):
            with self.subTest(takes=takes):
                panel = RemovalPanel(takes=takes, existing=[(10.0, (0, 12), self.DIP), (15.0, (30, 0), self.FILM)])
                with self.assertRaises(BridgeError) as cm:
                    ops.remove_transition(stub_rpc(panel.handlers()), track="V2", at=12.0, edge="end")
                self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))
                self.assertTrue(cm.exception.data[key])

    def test_effect_and_keyframe_read_backs_that_disagree_are_committed_failures(self):
        handlers = {"ping": ping_reply(),
                    "remove_effect": {"dryRun": False, "verified": False, "chain": ["AE.ADBE Motion"], "chainBefore": []},
                    "remove_keyframes": {"dryRun": False, "verified": False, "remaining": [1.0], "animationOff": False}}
        for fn in (lambda r: ops.remove_effect(r, track="V1", at=1.0, match_name="AE.ADBE Crop"),
                   lambda r: ops.remove_keyframes(r, track="A1", at=1.0, match_name="Internal Volume Mono", param=1,
                                                  all_keys=True)):
            with self.assertRaises(BridgeError) as cm:
                fn(stub_rpc(handlers))
            self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))

    def test_bad_requests_are_refused_locally(self):
        base = {"track": "V1", "at": 1.0, "match_name": "AE.ADBE Opacity", "param": 0}
        bad = [
            lambda r: ops.remove_transition(r, track="A1", at=1.0, edge="start"),
            lambda r: ops.remove_transition(r, track="V1", at=1.0, edge="middle"),
            lambda r: ops.remove_effect(r, track="V1", at=1.0, match_name=""),
            lambda r: ops.remove_effect(r, track="V1", at=1.0, match_name="AE.ADBE Crop", occurrence=0),
            lambda r: ops.remove_keyframes(r, **base),
            lambda r: ops.remove_keyframes(r, **base, all_keys=True, from_s=0.0, to_s=1.0, time_base="clip"),
            lambda r: ops.remove_keyframes(r, **base, from_s=0.0, time_base="clip"),
            lambda r: ops.remove_keyframes(r, **base, from_s=0.0, to_s=1.0),
            lambda r: ops.remove_keyframes(r, **base, from_s=2.0, to_s=1.0, time_base="clip"),
            lambda r: ops.remove_keyframes(r, **base, all_keys=True, time_base="clip"),
            lambda r: ops.remove_keyframes(r, **dict(base, param=-1), all_keys=True),
        ]
        rpc = stub_rpc({})
        for i, fn in enumerate(bad):
            with self.subTest(case=i):
                with self.assertRaises(BridgeError) as cm:
                    fn(rpc)
                self.assertEqual((cm.exception.code, cm.exception.outcome), ("BAD_PARAMS", "not_committed"))
        self.assertEqual(rpc.calls, [])

    def test_against_the_fake(self):
        """A neighbour's outgoing fade at the clip's start goes and is named as the previous clip's; a filter and
        keyframes come off V1 (the interview, 0-60 s) in one step each."""
        rpc, fake, _ = fake_rpc()
        title = {"itemName": "title.mov", "binPath": "EN", "videoTrack": 1, "audioTrack": 1}
        rpc("insert_clip", dict(title, atSeconds=10.0, inSeconds=0.0, outSeconds=2.0))
        a = fake.STATE["video"][1][0]
        rpc("insert_clip", dict(title, atSeconds=a["end"], inSeconds=0.5, outSeconds=4.5))
        rpc("add_transition", {"videoTrack": 1, "atSeconds": a["start"] + 1.0, "matchName": self.FILM,
                               "durationSeconds": 25.1 / 59.94, "applyToStart": False})
        b_mid = a["end"] + 2.0
        dry = ops.remove_transition(rpc, track="V2", at=b_mid, edge="start", dry_run=True)
        self.assertEqual((dry["transition"]["owner"], dry["neighbour"]["start"]), ("previous", a["start"]))
        self.assertEqual(len(fake.STATE["transitions"]), 1)
        done = ops.remove_transition(rpc, track="V2", at=b_mid, edge="start")
        self.assertEqual((done["verified"], done["via"], fake.STATE["transitions"]), (True, "clip", []))

        rpc("set_params", {"videoTrack": 0, "atSeconds": 5.0, "components": [
            {"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 2, "params": [{"index": 0, "value": 3.0}]},
            {"matchName": "AE.ADBE Opacity", "params": [{"index": 0, "timeBase": "clip", "keyframes": [
                {"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 100.0}, {"t": 59.0, "value": 100.0}]}]}]})
        gone = ops.remove_effect(rpc, track="V1", at=5.0, match_name="AE.ADBE Gaussian Blur 2", occurrence=2)
        self.assertEqual((gone["index"], gone["verified"], gone["chain"].count("AE.ADBE Gaussian Blur 2")), (3, True, 1))
        opacity = {"track": "V1", "at": 5.0, "match_name": "AE.ADBE Opacity", "param": 0}
        tail = ops.remove_keyframes(rpc, **opacity, from_s=58.5, to_s=60.0, time_base="sequence")
        self.assertEqual((tail["removed"], tail["remaining"], tail["animationOff"]), ([59.0], [0.0, 1.0], False))
        plan = ops.remove_keyframes(rpc, **opacity, all_keys=True, dry_run=True)
        self.assertEqual((plan["removed"], plan["animationOff"]), ([0.0, 1.0], True))
        self.assertIn("--keep-value", plan["note"])  # which value stays is Premiere's choice without it
        rest = ops.remove_keyframes(rpc, **opacity, all_keys=True, keep_value=True)
        self.assertEqual((rest["animationOff"], rest["keptValue"], rest["valueAfter"], rest["verified"]),
                         (True, 100.0, 100.0, True))  # the clip's middle (30 s) is past the fade-in
        self.assertNotIn("note", rest)
        with self.assertRaises(BridgeError) as cm:  # nothing left to remove
            ops.remove_keyframes(rpc, **opacity, all_keys=True)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("NOT_FOUND", "not_committed"))


class TranscriptOpsTest(unittest.TestCase):
    def test_export_sends_bin_path_and_writes_the_bytes(self):
        raw = json.dumps(DOC, ensure_ascii=False)
        rpc = stub_rpc({"ping": ping_reply(), "list_project_items": ITEMS,
                        "export_transcript": lambda p: {"itemName": p["itemName"], "binPath": p["binPath"],
                                                        "transcriptJson": raw}})
        out = tmpdir() / "sub" / "tr.json"
        data = ops.export_transcript(rpc, output=out, item="CN/title.mov")
        self.assertEqual(dict(rpc.calls)["export_transcript"], {"itemName": "title.mov", "binPath": "CN"})
        self.assertEqual((data["savedTo"], data["bytes"]), (str(out.resolve()), len(raw.encode("utf-8"))))
        self.assertIn("transcript_digest", data["hint"])
        self.assertNotIn("transcriptJson", data)
        self.assertEqual(out.read_bytes(), raw.encode("utf-8"))

    def test_import_checks_the_file_before_calling_the_panel(self):
        bad = dict(DOC, speakers=[{"id": "spk1", "name": "x"}])
        rpc = stub_rpc({})
        with self.assertRaises(BridgeError) as cm:
            ops.import_transcript(rpc, item="a.mp4", file=write_json(bad))
        self.assertEqual(cm.exception.code, "TRANSCRIPT_INVALID")
        self.assertTrue(cm.exception.data["problems"])
        self.assertEqual(rpc.calls, [])

    def test_import_sends_bin_path_and_replace(self):
        rpc = stub_rpc({"ping": ping_reply(), "list_project_items": ITEMS,
                        "import_transcript": lambda p: {"imported": True, "itemName": p["itemName"]}})
        path = tmpdir() / "bom.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(DOC).encode("utf-8"))
        ops.import_transcript(rpc, item="EN/title.mov", file=path, replace=True)
        sent = dict(rpc.calls)["import_transcript"]
        self.assertEqual((sent["itemName"], sent["binPath"], sent["replace"]), ("title.mov", "EN", True))
        self.assertEqual(json.loads(sent["transcriptJson"]), DOC)


class ProbeMediaTest(unittest.TestCase):
    def probe(self, result=None, raises=None):
        def run(*a, **k):
            if raises:
                raise raises
            return result
        with mock.patch.object(media.shutil, "which", return_value="/fake/ffprobe"), \
                mock.patch.object(media.subprocess, "run", side_effect=run):
            return media.probe_media("/fake/clip.mov")

    def test_failures_are_unknown_not_silent(self):
        done = lambda code, out: subprocess.CompletedProcess(["ffprobe"], code, stdout=out, stderr="")
        self.assertEqual(self.probe(done(1, "")), {})
        self.assertEqual(self.probe(done(0, '{"streams": []}')), {})
        self.assertEqual(self.probe(done(0, "not json")), {})
        self.assertEqual(self.probe(raises=OSError("no exe")), {})
        self.assertEqual(self.probe(raises=subprocess.TimeoutExpired("ffprobe", 30)), {})
        with mock.patch.object(media.shutil, "which", return_value=None):
            self.assertEqual(media.probe_media("/fake/clip.mov"), {})

    def test_success(self):
        out = json.dumps({"streams": [{"codec_type": "video", "width": 3840, "height": 2160,
                                       "avg_frame_rate": "60000/1001", "nb_frames": "300"}],
                          "format": {"duration": "5.005"}})
        info = self.probe(subprocess.CompletedProcess(["ffprobe"], 0, stdout=out, stderr=""))
        self.assertEqual((info["hasAudio"], info["width"], info["frames"]), (False, 3840, 300))
        self.assertAlmostEqual(info["fps"], 59.94, places=2)
        self.assertAlmostEqual(info["videoDuration"], 300 / (60000 / 1001))  # nb_frames / fps: no stream duration

    def test_picture_shorter_than_the_file(self):
        out = json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                                       "avg_frame_rate": "30/1", "duration": "9.500000"},
                                      {"codec_type": "audio", "codec_name": "aac", "duration": "10.000000"}],
                          "format": {"duration": "10.000000"}})
        info = self.probe(subprocess.CompletedProcess(["ffprobe"], 0, stdout=out, stderr=""))
        self.assertEqual((info["duration"], info["videoDuration"], info["frames"]), (10.0, 9.5, 285))
        self.assertEqual((info["videoCodec"], info["audioCodecs"]), ("h264", ["aac"]))
        self.assertEqual(framegrid.short_picture(info, 30.0), 9.5)
        self.assertIsNone(framegrid.short_picture(dict(info, videoDuration=9.99), 30.0))  # under 1.5 frames short

    def test_a_still_has_no_picture_length(self):
        out = json.dumps({"streams": [{"codec_type": "video", "codec_name": "png", "width": 1080, "height": 1920,
                                       "avg_frame_rate": "25/1", "nb_frames": "1", "duration": "0.040000"}],
                          "format": {"duration": "0.040000"}})
        info = self.probe(subprocess.CompletedProcess(["ffprobe"], 0, stdout=out, stderr=""))
        self.assertNotIn("videoDuration", info)
        self.assertIsNone(framegrid.short_picture(info, 25.0))


class OtherOpsTest(unittest.TestCase):
    def test_import_media_missing_files(self):
        rpc = stub_rpc({})
        with self.assertRaises(BridgeError) as cm:
            ops.import_media(rpc, path=[str(tmpdir() / "gone.mp4")])
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")
        self.assertEqual(len(cm.exception.data["missing"]), 1)
        self.assertEqual(rpc.calls, [])

    def test_import_media_refuses_codecs_premiere_cannot_read(self):
        # a YouTube download: VP9 + Opus in .mp4 hangs Premiere's import and leaves the panel stuck
        folder = tmpdir()
        yt, ok = folder / "yt.mp4", folder / "ok.mp4"
        yt.write_bytes(b"x")
        ok.write_bytes(b"x")
        probes = {str(yt.resolve()): {"videoCodec": "vp9", "audioCodecs": ["opus"]},
                  str(ok.resolve()): {"videoCodec": "h264", "audioCodecs": ["aac"]}}
        rpc = stub_rpc({"import_media": lambda p: {"imported": len(p["paths"])}})
        with self.assertRaises(BridgeError) as cm:
            ops.import_media(rpc, path=[str(ok), str(yt)], probe_media=probes.get)
        e = cm.exception
        self.assertEqual((e.code, e.outcome), ("UNSUPPORTED", "not_committed"))
        self.assertEqual([(f["path"], f["codecs"]) for f in e.data["files"]], [(str(yt.resolve()), ["vp9", "opus"])])
        self.assertIn("libx264", e.data["files"][0]["transcode"])
        self.assertEqual(rpc.calls, [])
        ops.import_media(rpc, path=[str(ok), str(yt)], force=True, probe_media=probes.get)
        self.assertEqual(len(dict(rpc.calls)["import_media"]["paths"]), 2)

    def test_remove_project_items_reads_back_and_never_takes_the_root(self):
        left = {"ping": ping_reply(), "remove_project_items": lambda p: {"dryRun": False, "items": [], "inUse": [],
                                                                         "removed": [], "remaining": ["PAB_TEST_06"]}}
        with self.assertRaises(BridgeError) as cm:
            ops.remove_project_items(stub_rpc(left), bin=["PAB_TEST_06"])
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))
        rpc = stub_rpc({})
        for kwargs in ({}, {"bin": ["/"]}):
            with self.assertRaises(BridgeError) as cm:
                ops.remove_project_items(rpc, **kwargs)
            self.assertEqual(cm.exception.code, "BAD_PARAMS")
        self.assertEqual(rpc.calls, [])

    def test_only_the_audio_needs_re_encoding(self):
        cmd = media.transcode_command("m/a.mp4", {"videoCodec": "h264", "audioCodecs": ["opus"]})
        self.assertIn("-c:v copy", cmd)
        self.assertEqual(media.unimportable({}), [])  # ffprobe could not tell: not refused

    def test_marker_colours(self):
        rpc = stub_rpc({"add_marker": lambda p: {"added": True, **p}})
        ops.add_marker(rpc, at=1.0, name="Check", color="Red")
        self.assertEqual(dict(rpc.calls)["add_marker"]["colorIndex"], 1)
        for bad in ("pink", "9"):
            with self.assertRaises(BridgeError) as cm:
                ops.add_marker(rpc, at=1.0, name="x", color=bad)
            self.assertEqual(cm.exception.code, "BAD_PARAMS")

    def test_fill_without_media_size_reports_see_data(self):
        rpc = stub_rpc({"get_sequence": SEQ, "set_scale": {"dryRun": True, "value": None, "clips": [
            {"name": "b.mp4", "start": 0.0, "end": 2.0, "mediaPath": "/fake/b.mp4", "scaleBefore": 100.0}]}})
        with self.assertRaises(BridgeError) as cm:
            ops.set_scale(rpc, track="V3", at=1.0, fill=True, dry_run=True, probe_media=lambda p: {})
        reply = cm.exception.to_reply()
        self.assertEqual((reply["error"], reply["outcome"]), ("see data", "not_committed"))
        self.assertIn("error", reply["data"]["clips"][0])

    def test_ripple_needs_confirmation(self):
        with self.assertRaises(BridgeError) as cm:
            ops.remove_items(stub_rpc({}), track=["V3"], from_s=0, to_s=5, ripple=True)
        self.assertEqual(cm.exception.code, "CONFIRM_REQUIRED")

    def test_dry_runs_honour_expect_seq(self):
        """A dry run against another sequence plans the wrong timeline (seen on a real job: remove_items)."""
        for fn in (lambda r: ops.remove_items(r, track=["V3"], from_s=0, to_s=5, dry_run=True, expect_seq="Other"),
                   lambda r: ops.set_scale(r, track="V3", at=1.0, value=50, dry_run=True, expect_seq="Other"),
                   lambda r: ops.set_volume(r, track="A1", at=1.0, db=-6, dry_run=True, expect_seq="Other")):
            rpc = stub_rpc({"ping": ping_reply(), "get_sequence": SEQ})
            with self.assertRaises(BridgeError) as cm:
                fn(rpc)
            self.assertEqual((cm.exception.code, cm.exception.outcome), ("SEQUENCE_MISMATCH", "not_committed"))
            self.assertEqual([a for a, _ in rpc.calls if a not in ("ping", "get_sequence")], [])


class MarkersAndRestorePointsTest(unittest.TestCase):
    """list_markers / remove_marker, clone_sequence's pruning rules and save_project's file-time check (v0.6)."""

    def test_markers_listed_with_colour_names_and_removed_one_at_a_time(self):
        rpc, _, _ = fake_rpc()
        ops.add_marker(rpc, at=5.0, name="dup", color="purple")
        ops.add_marker(rpc, at=7.0, name="dup", color="cyan")
        listed = ops.list_markers(rpc)
        self.assertEqual([(m["name"], m["color"]) for m in listed["markers"]],
                         [("harness marker", "green"), ("dup", "purple"), ("dup", "cyan")])
        with self.assertRaises(BridgeError) as cm:
            ops.remove_marker(rpc, name="dup")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("AMBIGUOUS", "not_committed"))
        self.assertEqual(len(cm.exception.data["matches"]), 2)
        gone = ops.remove_marker(rpc, name="dup", at=7.0, label="Agent: drop the second flag")
        self.assertEqual((gone["verified"], gone["removed"][0]["color"]), (True, "cyan"))
        by_guid = ops.remove_marker(rpc, guid=listed["markers"][0]["guid"], dry_run=True)
        self.assertEqual((by_guid["dryRun"], by_guid["removed"][0]["name"]), (True, "harness marker"))
        self.assertEqual([m["name"] for m in ops.list_markers(rpc)["markers"]], ["harness marker", "dup"])

    def test_remove_marker_refusals_before_the_panel(self):
        rpc = stub_rpc({})
        for kwargs in ({}, {"at": 1.0}, {"name": ""}):
            with self.assertRaises(BridgeError) as cm:
                ops.remove_marker(rpc, **kwargs)
            self.assertEqual(cm.exception.code, "BAD_PARAMS")
        self.assertEqual(rpc.calls, [])
        rpc = stub_rpc({"ping": ping_reply(), "get_sequence": SEQ})
        with self.assertRaises(BridgeError) as cm:
            ops.remove_marker(rpc, name="Fact check", expect_seq="Other")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("SEQUENCE_MISMATCH", "not_committed"))
        self.assertNotIn("remove_marker", [a for a, _ in rpc.calls])

    def test_clone_sequence_prunes_only_untouched_restore_points_of_that_source(self):
        rpc, fake, _ = fake_rpc()
        first = ops.clone_sequence(rpc, keep=3)
        second = ops.clone_sequence(rpc, keep=3)
        third = ops.clone_sequence(rpc, keep=3)
        others = fake.STATE["otherSequences"]
        record = next(s for s in others if s["guid"] == first["clone"]["guid"])
        # the user duplicates the first restore point (the copy keeps its record) and renames the second one
        others.append(dict(record, name=record["name"] + " Copy", guid="user-duplicate", props=dict(record["props"])))
        next(s for s in others if s["guid"] == second["clone"]["guid"])["name"] = "Sequence 01 keeper"
        dry = ops.clone_sequence(rpc, keep=1, dry_run=True)
        self.assertEqual([p["guid"] for p in dry["prune"]], [first["clone"]["guid"], third["clone"]["guid"]])
        self.assertEqual(len(others), 4)  # a dry run deletes nothing
        real = ops.clone_sequence(rpc, keep=1)
        self.assertEqual([p["guid"] for p in real["pruned"]], [first["clone"]["guid"], third["clone"]["guid"]])
        seqs = {s["guid"]: s for s in ops.list_sequences(rpc)["sequences"]}
        self.assertEqual(sorted(seqs), sorted(["fake-sequence-guid", "user-duplicate", second["clone"]["guid"],
                                               real["clone"]["guid"]]))
        self.assertIsNone(seqs["user-duplicate"]["restorePoint"])
        self.assertTrue(seqs[second["clone"]["guid"]]["restorePoint"]["renamed"])
        self.assertEqual(seqs[real["clone"]["guid"]]["restorePoint"]["sourceGuid"], "fake-sequence-guid")
        with self.assertRaises(BridgeError) as cm:
            ops.clone_sequence(stub_rpc({}), keep=0)
        self.assertEqual(cm.exception.code, "BAD_PARAMS")

    def test_save_project_checks_the_project_file_time(self):
        job = tmpdir() / "job.prproj"
        job.write_bytes(b"<project/>")
        os.utime(job, (1_000_000_000, 1_000_000_000))
        rpc, fake, _ = fake_rpc()
        fake.STATE["project"]["path"] = str(job)
        saved = ops.save_project(rpc, sleep=lambda s: None)
        self.assertEqual((saved["saved"], saved["fileTimeChanged"], saved["path"]), (True, True, str(job)))
        self.assertTrue(saved["fileTimeBefore"].startswith("2001-09-09"), saved["fileTimeBefore"])
        # Premiere answers "saved" but the file keeps its time: polled, then a committed failure
        os.utime(job, (1_000_000_000, 1_000_000_000))
        project = {"name": "job", "path": str(job), "activeSequence": None}
        idle = stub_rpc({"ping": ping_reply(), "get_project": project,
                         "save_project": {"saved": True, "name": "job", "path": str(job)}})
        sleeps = []
        with self.assertRaises(BridgeError) as cm:
            ops.save_project(idle, sleep=sleeps.append)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))
        self.assertIs(cm.exception.data["fileTimeChanged"], False)
        self.assertEqual(sum(sleeps), 3.0)
        # a path this machine cannot read is reported, not checked
        far = dict(project, path=str(tmpdir() / "elsewhere" / "job.prproj"))
        result = ops.save_project(stub_rpc({"ping": ping_reply(), "get_project": far,
                                            "save_project": {"saved": True, "name": "job", "path": far["path"]}}),
                                  sleep=sleeps.append)
        self.assertEqual((result["fileTimeChanged"], "not readable" in result["note"]), (None, True))
        # save returned false and the file is untouched: nothing was written
        fake.FAIL_TRANSACTIONS.add("Project#save")
        with self.assertRaises(BridgeError) as cm:
            ops.save_project(rpc, sleep=lambda s: None)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("TRANSACTION_FAILED", "not_committed"))
FPS_5994 = 60000 / 1001
BROLL = {"hasAudio": True, "duration": 12.0, "width": 1920, "height": 1080, "fps": FPS_5994}  # the fake's broll_city


class SubclipOpsTest(unittest.TestCase):
    """subclip: the panel's refusals first (dry run), source points snapped to the item's frames, read back."""

    def test_snapped_to_the_items_frames_and_verified_against_the_fake(self):
        rpc, fake, calls = fake_rpc()
        r = ops.subclip(rpc, item="broll_city.mp4", in_s=2.0045, out_s=5.0, name="city", bin="Selects",
                        probe_media=lambda p: BROLL)
        want_in, want_out = round(2.0045 * FPS_5994) / FPS_5994, round(5.0 * FPS_5994) / FPS_5994
        self.assertEqual((r["verified"], r["item"], r["binPath"], r["createsBin"]), (True, "Selects/city", "Selects", True))
        self.assertEqual(r["sourceSnap"]["grid"], "item")  # the panel's interpreted rate
        self.assertAlmostEqual(r["inSeconds"], want_in, places=9)
        self.assertAlmostEqual(r["outSeconds"], want_out, places=9)
        self.assertEqual([p.get("dryRun") for a, p in calls if a == "subclip"], [True, False])
        self.assertNotIn("get_sequence", [a for a, _ in calls])
        self.assertEqual((r["moved"], r["undoSteps"]), (True, ['Agent: subclip "city" of broll_city.mp4',
                                                                'Agent: subclip "city" of broll_city.mp4 [move to bin]']))
        fake.FAIL_TRANSACTIONS.add("[move to bin]")
        with self.assertRaises(BridgeError) as cm:  # made next to its source, the move failed: partial
            ops.subclip(rpc, item="broll_city.mp4", in_s=1, out_s=2, name="city 2", bin="Selects",
                        probe_media=lambda p: BROLL)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("TRANSACTION_FAILED", "partial"))
        self.assertEqual(cm.exception.data["item"], "city 2")
        fake.FAIL_TRANSACTIONS.clear()
        listed = ops.list_project_items(rpc)["items"]
        self.assertIn({"name": "city", "bin": "Selects", "mediaPath": "/fake/broll_city.mp4"}, listed)
        with self.assertRaises(BridgeError) as cm:  # the name is taken there now: refused by the dry run
            ops.subclip(rpc, item="broll_city.mp4", in_s=1, out_s=2, name="city", bin="Selects",
                        probe_media=lambda p: BROLL)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("PRECONDITION_FAILED", "not_committed"))
        self.assertEqual(cm.exception.data["existing"], ["Selects/city"])
        dry = ops.subclip(rpc, item="broll_city.mp4", in_s=1, out_s=2, name="city", bin="Selects",
                          allow_duplicate_name=True, dry_run=True, probe_media=lambda p: BROLL)
        self.assertEqual((dry["dryRun"], dry["created"]), (True, False))

    def panel(self, frame_rate=None, reply=None, seq=None):
        def subclip(p):
            base = {"dryRun": bool(p.get("dryRun")), "created": False, "source": "a.mp4", "name": p["name"],
                    "binPath": p.get("bin", ""), "inSeconds": p["inSeconds"], "outSeconds": p["outSeconds"],
                    "takeVideo": p.get("takeVideo", True), "takeAudio": p.get("takeAudio", True),
                    "hardBoundaries": True, "createsBin": False, "sameNameElsewhere": [], "frameRate": frame_rate,
                    "mediaSeconds": 10.0}
            if p.get("dryRun"):
                return base
            done = dict(base, created=True, item=p["name"], moved=False, undoLabel="Agent: sub", undoSteps=["Agent: sub"],
                        readBack={"inSeconds": p["inSeconds"], "outSeconds": p["outSeconds"]})
            return reply(done) if reply else done
        handlers = {"ping": ping_reply(), "list_project_items": ITEMS, "subclip": subclip}
        if seq is not None:
            handlers["get_sequence"] = seq
        return stub_rpc(handlers)

    def test_without_the_items_rate_the_sequence_and_the_file_decide(self):
        rpc = self.panel(seq=SEQ)
        r = ops.subclip(rpc, item="a.mp4", in_s=1.01, out_s=2.0, name="s", probe_media=lambda p: {"fps": 25.0})
        self.assertEqual((r["sourceSnap"]["grid"], r["sourceSnap"]["fps"], r["inSeconds"]), ("media", 25.0, 1.0))
        r = ops.subclip(rpc, item="a.mp4", in_s=1.01, out_s=2.0, name="s", probe_media=lambda p: {})
        self.assertEqual(r["sourceSnap"]["grid"], "sequence")  # no ffprobe: the sequence's frames

        def no_sequence(p):
            raise BridgeError("Project has no active sequence.", "NO_SEQUENCE", "not_committed")
        rpc = self.panel(seq=no_sequence)
        r = ops.subclip(rpc, item="a.mp4", in_s=1.01, out_s=2.0, name="s", probe_media=lambda p: {})
        self.assertEqual((r["inSeconds"], "sourceSnap" in r), (1.01, False))
        self.assertIn("frame rate is unknown", r["notes"][0])

    def test_refusals_before_the_write(self):
        rpc = stub_rpc({})
        for kwargs in ({"video_only": True, "audio_only": True}, {"in_s": 3, "out_s": 2}, {"in_s": -1},
                       {"name": " "}, {"in_s": "x"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(BridgeError) as cm:
                    ops.subclip(rpc, **dict({"item": "a.mp4", "in_s": 1, "out_s": 2, "name": "s"}, **kwargs))
                self.assertEqual(cm.exception.code, "BAD_PARAMS")
        self.assertEqual(rpc.calls, [])
        for kwargs, probe in (({"out_s": 10.5}, {}), ({"audio_only": True}, {"hasAudio": False, "width": 8}),
                              ({"video_only": True}, {"hasAudio": True}), ({"out_s": 1.005}, {})):
            with self.subTest(kwargs=kwargs):  # past the 10 s media, no such stream, under a frame after snapping
                rpc = self.panel(frame_rate=FPS_5994)
                with self.assertRaises(BridgeError) as cm:
                    ops.subclip(rpc, **dict({"item": "a.mp4", "in_s": 1, "out_s": 2, "name": "s",
                                             "probe_media": lambda p, probe=probe: probe}, **kwargs))
                self.assertEqual((cm.exception.code, cm.exception.outcome), ("BAD_PARAMS", "not_committed"))
                self.assertEqual([p.get("dryRun") for a, p in rpc.calls if a == "subclip"], [True])

    def test_read_back_problems_are_verify_failed(self):
        for change, text in ((lambda d: dict(d, created=False, newItems=["s 1"]), "no new item"),
                             (lambda d: dict(d, binPath="Other"), 'in bin "Other"'),
                             (lambda d: dict(d, readBack={"inSeconds": 0.0, "outSeconds": 1.0}), "in/out reads")):
            with self.subTest(text=text):
                with self.assertRaises(BridgeError) as cm:
                    ops.subclip(self.panel(frame_rate=25.0, reply=change), item="a.mp4", in_s=1, out_s=2, name="s",
                                probe_media=lambda p: {})
                self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))
                self.assertIn(text, str(cm.exception))
        # 27.1 answers -400000 s ("not set") for a subclip's in/out (live, 2 Nov 2026): no range to compare, not a failure
        for back in (None, {"inSeconds": -400000.0, "outSeconds": -400000.0}):
            with self.subTest(readBack=back):
                r = ops.subclip(self.panel(frame_rate=25.0, reply=lambda d, back=back: dict(d, readBack=back)),
                                item="a.mp4", in_s=1, out_s=2, name="s", probe_media=lambda p: {})
                self.assertTrue(r["verified"])
                self.assertIs(r["rangeVerified"], False)
                self.assertIn("range is not verified", r["notes"][0])


class RelinkOpsTest(unittest.TestCase):
    """relink: the new file is checked (path rules, exists, codec, same media) before anything changes; read back."""

    def new_file(self, name="new.mp4"):
        p = tmpdir() / name
        p.write_bytes(b"x")
        return p

    def test_compare_media(self):
        a = {"duration": 10.0, "fps": 25.0, "hasVideo": True, "hasAudio": True}
        self.assertEqual(ops.compare_media(a, dict(a, duration=10.039)), ([], ["duration", "fps", "hasVideo", "hasAudio"]))
        diffs, _ = ops.compare_media(a, dict(a, duration=10.05, fps=24.0, hasAudio=False))
        self.assertEqual(len(diffs), 3)
        self.assertIn("frame rate 25.000 -> 24.000", diffs[1])
        self.assertIn("has audio -> no audio", diffs[2])
        premiere = {"duration": 10.0, "fps": 25.0, "hasVideo": None, "hasAudio": None}  # an offline item
        self.assertEqual(ops.compare_media(premiere, dict(a, hasAudio=False)), ([], ["duration", "fps"]))
        self.assertEqual(ops.compare_media({}, a), ([], []))
        self.assertEqual(ops.compare_media(dict(a, fps=FPS_5994), dict(a, fps=60.0))[0],
                         ["frame rate 59.940 -> 60.000 fps"])

    def test_relink_against_the_fake(self):
        rpc, fake, calls = fake_rpc()
        new = self.new_file()
        r = ops.relink(rpc, item="broll_city.mp4", to=str(new), probe_media=lambda p: BROLL)
        self.assertEqual((r["verified"], r["relinked"], r["previousPath"], r["mediaPath"]),
                         (True, True, "/fake/broll_city.mp4", str(new.resolve())))
        self.assertEqual((r["comparison"]["oldFrom"], r["comparison"]["checked"]), ("premiere", ["duration", "fps"]))
        self.assertIn("0 clip(s) in 0 sequence(s)", r["note"])
        self.assertEqual([p.get("dryRun") for a, p in calls if a == "relink"], [True, False])
        self.assertIn(str(new.resolve()), [i["mediaPath"] for i in ops.list_project_items(rpc)["items"]])
        calls.clear()
        with self.assertRaises(BridgeError) as cm:  # the interview is 60 s: refused, only the dry run went out
            ops.relink(rpc, item="interview.mp4", to=str(new), probe_media=lambda p: BROLL)
        e = cm.exception
        self.assertEqual((e.code, e.outcome), ("PRECONDITION_FAILED", "not_committed"))
        self.assertIn("duration 60.000s -> 12.000s", e.data["comparison"]["differences"][0])
        self.assertEqual([p.get("dryRun") for a, p in calls if a == "relink"], [True])
        r = ops.relink(rpc, item="interview.mp4", to=str(new), force=True, dry_run=True, probe_media=lambda p: BROLL)
        self.assertEqual((r["dryRun"], r["comparison"]["forced"], r["uses"]["clips"]), (True, True, 2))
        self.assertIn("2 clip(s) in 1 sequence(s)", r["note"])
        r = ops.relink(rpc, item="interview.mp4", to=str(new), force=True, probe_media=lambda p: BROLL)
        self.assertTrue(r["verified"])
        timeline = ops.list_items(rpc, track="V1")["video"][0]["items"]
        self.assertEqual(timeline[0]["mediaPath"], str(new.resolve()))  # the clips follow their item
        again = ops.relink(rpc, item="interview.mp4", to=str(new), probe_media=lambda p: BROLL)
        self.assertTrue(again["alreadyLinked"])
        with self.assertRaises(BridgeError) as cm:
            ops.relink(rpc, item="title.mov", to=str(new), probe_media=lambda p: BROLL)
        self.assertEqual(cm.exception.code, "AMBIGUOUS")

    def test_path_checks_come_before_any_request(self):
        rpc = stub_rpc({})
        for to, code in ((str(tmpdir() / "gone.mp4"), "FILE_NOT_FOUND"), (r"\\evil-host\share\a.mp4", "BAD_PARAMS"),
                         (str(tmpdir() / "a.mp4") + ":hidden", "BAD_PARAMS")):
            with self.subTest(to=to):
                with self.assertRaises(BridgeError) as cm:
                    ops.relink(rpc, item="a.mp4", to=to, probe_media=lambda p: {})
                self.assertEqual((cm.exception.code, cm.exception.outcome), (code, "not_committed"))
        self.assertEqual(rpc.calls, [])

    def panel(self, reply=None):
        def relink(p):
            base = {"dryRun": bool(p.get("dryRun")), "relinked": False, "item": "a.mp4", "previousPath": "/gone/a.mp4",
                    "newPath": p["newPath"], "current": {"mediaPath": "/gone/a.mp4", "offline": True, "frameRate": None,
                                                         "mediaSeconds": None},
                    "uses": {"clips": 0, "sequences": [], "complete": False, "error": "x"}, "sameMedia": []}
            if p.get("dryRun"):
                return base
            done = dict(base, relinked=True, returned=True, refreshed=True, mediaPath=p["newPath"], offline=False)
            return reply(done) if reply else done
        return stub_rpc({"ping": ping_reply(), "list_project_items": ITEMS, "relink": relink})

    def test_codec_nothing_to_compare_force_and_verify(self):
        new = self.new_file()
        writes = lambda rpc: [p for a, p in rpc.calls if a == "relink" and not p.get("dryRun")]  # noqa: E731
        rpc = self.panel()
        with self.assertRaises(BridgeError) as cm:
            ops.relink(rpc, item="a.mp4", to=str(new), probe_media=lambda p: {"videoCodec": "vp9", "duration": 5.0})
        self.assertEqual((cm.exception.code, cm.exception.data["codecs"]), ("UNSUPPORTED", ["vp9"]))
        with self.assertRaises(BridgeError) as cm:  # offline item, Premiere reports no facts: nothing to compare
            ops.relink(rpc, item="a.mp4", to=str(new), probe_media=lambda p: BROLL)
        self.assertEqual(cm.exception.code, "PRECONDITION_FAILED")
        self.assertIn("nothing could be compared", str(cm.exception))
        self.assertEqual(writes(rpc), [])
        r = ops.relink(rpc, item="a.mp4", to=str(new), force=True, probe_media=lambda p: BROLL)
        self.assertEqual((r["verified"], r["comparison"]["forced"]), (True, True))
        self.assertIn("incomplete", r["note"])
        with self.assertRaises(BridgeError) as cm:
            ops.relink(self.panel(reply=lambda d: dict(d, relinked=False, returned=False, mediaPath="/gone/a.mp4")),
                       item="a.mp4", to=str(new), force=True, probe_media=lambda p: BROLL)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))
        self.assertIn("Premiere returned false", str(cm.exception))
        with mock.patch("ppro_bridge.ops.shutil.which", return_value=None):
            with self.assertRaises(BridgeError) as cm:
                ops.relink(self.panel(), item="a.mp4", to=str(new))
        self.assertEqual(cm.exception.code, "MISSING_DEPENDENCY")


class AgainstTheFakeTest(unittest.TestCase):
    """The new commands end to end against the fake panel (in-process, every request spec-checked)."""

    def setUp(self):
        self.rpc, self.fake, self.calls = fake_rpc()

    def test_transcribe_then_import_elsewhere(self):
        out = tmpdir() / "stt.json"
        result = ops.transcribe_clip(self.rpc, item="broll_city.mp4", language="en-US", wait=30, poll=1, output=out,
                                     sleep=lambda s: None)
        self.assertTrue(result["hasTranscript"], result)
        _, saved = transcript.read_transcript(out)
        self.assertEqual(transcript.validate(saved), [])
        imported = ops.import_transcript(self.rpc, item="EN/title.mov", file=out)
        self.assertEqual((imported["binPath"], imported["wordsIn"]), ("EN", transcript.count_words(saved)))

    def test_params_transition_and_replace(self):
        params = write_json([{"matchName": "AE.ADBE Opacity", "params": [
            {"index": 0, "timeBase": "clip", "keyframes": [{"t": 0, "value": 0}, {"t": 0.5, "value": 100}]}]}])
        ops.insert_clip(self.rpc, item="EN/title.mov", at=20.0, vtrack=2, atrack=2)
        ops.set_params(self.rpc, track="V3", at=21.0, json_file=params)
        ops.add_transition(self.rpc, track="V3", at=21.0, match_name="AE.ADBE Cross Dissolve New", duration=0.5,
                           edge="start")
        report = ops.replace_clip(self.rpc, track="V3", at=21.0, with_item="CN/title.mov", atrack="A3",
                                  probe_media=lambda p: {"hasAudio": False, "duration": 5.0})
        self.assertTrue(report["verified"], report["problems"])
        self.assertEqual([t["applyToStart"] for t in report["transitions"]], [True])
        self.assertIn("AE.ADBE Opacity#0", report["restoredParams"])

    def test_frames_report_time_size_and_black(self):
        """A black export is flagged: some phone clips exported black while the Program monitor showed them."""
        out = tmpdir()
        one = ops.export_frame(self.rpc, dir=out, at=1.5)
        self.assertEqual((one["pixelWidth"], one["pixelHeight"]), (108, 192))
        self.assertFalse(one["black"])
        self.assertNotIn("warning", one)
        self.assertGreaterEqual(one["exportSeconds"], 0)
        self.assertGreaterEqual(one["waitedSeconds"], 0)
        real = self.fake.solid_png
        self.fake.solid_png = lambda w, h: real(w, h, rgb=(0, 0, 0))
        self.addCleanup(setattr, self.fake, "solid_png", real)
        many = ops.export_frames(self.rpc, dir=out, at="2,3")
        self.assertEqual([f["black"] for f in many["frames"]], [True, True])
        self.assertIn("All black at 2, 3s", many["warning"])


    def test_frame_times_at_or_past_the_end_are_refused(self):
        """QA F-EF1: Premiere answers a time at or past the sequence end with the 0 s frame and says it exported."""
        out = tmpdir()
        end = self.rpc("get_sequence")["durationSeconds"]
        before = len(self.calls)
        for at in (end, end + 1, -1):
            with self.assertRaises(BridgeError) as ctx:
                ops.export_frame(self.rpc, dir=out, at=at)
            self.assertEqual((ctx.exception.code, ctx.exception.outcome), ("BAD_PARAMS", "not_committed"))
        with self.assertRaises(BridgeError) as ctx:
            ops.export_frames(self.rpc, dir=out, at=f"1,{end + 2:g}")
        self.assertEqual(ctx.exception.data["outside"], [end + 2])
        self.assertNotIn("export_frame", [a for a, _ in self.calls[before:]])  # nothing was exported
        self.assertEqual(list(out.iterdir()), [])
        inside = ops.export_frame(self.rpc, dir=out, at=end - FRAME / 2)  # inside the last frame
        self.assertTrue(inside["resolvedPath"])

    def test_an_older_frame_of_the_same_name_is_not_this_exports_result(self):
        """A frame left in the folder by an earlier export is not reported when Premiere writes nothing new."""
        out = tmpdir()
        old = out / "frame_1.500s.png"
        old.write_bytes(self.fake.solid_png(108, 192))
        os.utime(old, (time.time() - 3600,) * 2)
        wrote_nothing = dict(self.fake.HANDLERS, export_frame=lambda p: {
            "exported": True, "dir": p["dir"], "candidates": [p["name"], p["name"] + ".png"], "width": 1080,
            "height": 1920, "atSeconds": p["atSeconds"]})
        with mock.patch.object(self.fake, "HANDLERS", wrote_nothing), mock.patch.object(ops.frames, "POLL", 0.01):
            with mock.patch("ppro_bridge.frames.resolve_exported_frame",
                            side_effect=lambda d, since=None: real_resolve(d, wait=0.2, since=since)):
                one = ops.export_frame(self.rpc, dir=out, at=1.5)
        self.assertIsNone(one["resolvedPath"])
        self.assertEqual(one["staleFiles"], ["frame_1.500s.png"])
        fresh = ops.export_frame(self.rpc, dir=out, at=1.5)  # the fake writes the file: it counts again
        self.assertEqual(Path(fresh["resolvedPath"]).name, "frame_1.500s.png")


real_resolve = ops.frames.resolve_exported_frame


class FingerprintTest(unittest.TestCase):
    """get_sequence's fingerprint, and --expect-fingerprint refusing a write over an edit made in between."""

    def setUp(self):
        self.rpc, self.fake, self.calls = fake_rpc()

    def place(self, rpc, at, **kw):
        return cli.run("insert_clip", dict({"item": "broll_city.mp4", "at": at, "vtrack": 1, "atrack": 1}, **kw), rpc)

    def test_same_timeline_same_fingerprint_and_an_edit_changes_it(self):
        fp = ops.get_sequence(self.rpc)["fingerprint"]
        self.assertRegex(fp, r"^[0-9a-f]{16}$")
        self.assertEqual(ops.get_sequence(self.rpc)["fingerprint"], fp)
        self.assertTrue(self.place(self.rpc, 30.0)["ok"])
        self.assertNotEqual(ops.get_sequence(self.rpc)["fingerprint"], fp)
        # JSON gives 0 for 0.0: both are the same time
        doc = {"video": [{"index": 0, "items": [{"name": "a", "start": 0, "end": 1.5}]}], "audio": []}
        same = {"video": [{"index": 0, "items": [{"name": "a", "start": 0.0, "end": 1.5}]}], "audio": []}
        self.assertEqual(items.fingerprint(doc), items.fingerprint(same))

    def test_a_stale_fingerprint_stops_the_write_before_it_is_sent(self):
        fp = ops.get_sequence(self.rpc)["fingerprint"]
        self.assertTrue(self.place(self.rpc, 30.0, expect_fingerprint=fp)["ok"])  # unchanged: goes ahead
        before = len([a for a, _ in self.calls if a == "insert_clip"])
        reply = self.place(self.rpc, 40.0, expect_fingerprint=fp)  # the first placement changed the timeline
        self.assertEqual((reply["code"], reply["outcome"]), ("PRECONDITION_FAILED", "not_committed"))
        self.assertEqual(len([a for a, _ in self.calls if a == "insert_clip"]), before)

    def test_a_dry_run_against_another_timeline_is_refused_too(self):
        reply = cli.run("remove_items", {"track": ["V1"], "from_s": 0.0, "to_s": 60.0, "dry_run": True,
                                         "expect_fingerprint": "0000000000000000"}, self.rpc)
        self.assertEqual(reply["code"], "PRECONDITION_FAILED")

    def test_the_panel_checks_the_same_fingerprint_as_a_guard_key(self):
        """transport.make_rpc(guard={"fingerprint": fp}): the panel compares the value get_sequence reported."""
        fp = ops.get_sequence(self.rpc)["fingerprint"]
        rpc, fake, calls = fake_rpc(guard={"fingerprint": fp})  # a fresh fake: the same default timeline
        ops.insert_clip(rpc, item="broll_city.mp4", at=30.0, vtrack=1, atrack=1)  # planned on fp: placed
        with self.assertRaises(BridgeError) as cm:  # the timeline changed: refused by the panel, nothing sent on
            ops.insert_clip(rpc, item="broll_city.mp4", at=40.0, vtrack=1, atrack=1)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("PRECONDITION_FAILED", "not_committed"))
        self.assertEqual(cm.exception.data["actual"], ops.get_sequence(rpc)["fingerprint"])
        self.assertEqual(len([c for c in fake.STATE["video"][1] if c["name"] == "broll_city.mp4"]), 1)


class PathCheckTest(unittest.TestCase):
    """Paths Premiere would open are checked as typed: no UNC share (credentials), no NTFS stream, fixed export
    extensions; a refusal sends nothing."""

    def refused(self, fn):
        rpc = stub_rpc({})
        with self.assertRaises(BridgeError) as cm:
            fn(rpc)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("BAD_PARAMS", "not_committed"))
        self.assertEqual(rpc.calls, [])
        return str(cm.exception)

    def test_unc_and_streams_are_refused_before_anything_is_sent(self):
        self.assertIn("UNC", self.refused(lambda r: ops.import_media(r, path=[r"\\evil-host\share\clip.mp4"])))
        self.assertIn("UNC", self.refused(lambda r: ops.export_frame(r, dir="//evil-host/share", at=1.0)))
        self.assertIn("stream", self.refused(lambda r: ops.export_otio(r, output=str(tmpdir() / "t.otio") + ":hidden")))
        self.assertIn(".otio", self.refused(lambda r: ops.export_otio(r, output=str(tmpdir() / "run.bat"))))
        self.assertIn("plain file name", self.refused(lambda r: ops.export_frame(r, dir=str(tmpdir()), at=1.0,
                                                                                   name=r"..\..\x.png")))
        self.assertIn(".png", self.refused(lambda r: ops.export_frame(r, dir=str(tmpdir()), at=1.0, name="x.bat")))

    def test_drive_paths_pass_and_unc_never_does(self):
        mapped = "Z:" + os.sep + os.path.join("NAS", "clip.mp4")  # a mapped drive letter (the release scan flags literals)
        self.assertEqual(paths.check(mapped), mapped)  # a mapped drive, as typed
        long_local = "\\\\?\\" + mapped  # a local long path, as Premiere reports project paths
        self.assertEqual(paths.check(long_local), long_local)
        self.assertEqual(paths.check_name("frame_1", ext=".png"), "frame_1.png")
        # the panel refuses UNC paths whatever the CLI is told, so the CLI has no override either (0.6 dev had one)
        with mock.patch.dict("os.environ", {"PPRO_ALLOW_UNC": "1"}):
            self.assertIn("UNC", self.refused(lambda r: ops.import_media(r, path=[r"\\nas\media\a.mov"])))
        for device in (r"\\.\pipe\x", r"\\?\UNC\nas\media\a.mov", r"\\?\Volume{1}\a.mov", "//./pipe/x"):
            with self.subTest(path=device), self.assertRaises(BridgeError) as cm:
                paths.check(device)
            self.assertEqual(cm.exception.code, "BAD_PARAMS")
        self.assertIsNone(paths.path_problem(long_local))
        self.assertEqual(paths.remote_kind(r"\\.\UNC\nas\x"), "UNC")

    def test_render_needs_a_wav_and_an_epr(self):
        from ppro_bridge import review
        with self.assertRaises(BridgeError):
            review.export_audio(stub_rpc({}), output=str(tmpdir() / "mix.mp3"))
        with self.assertRaises(BridgeError):
            review.resolve_preset(str(tmpdir() / "preset.exe"))


class RangeModeTest(unittest.TestCase):
    """set_volume / set_scale over --from/--to: only the clips entirely inside, unless mode="overlap". On 2 Nov a
    range for one clip also changed the clip starting where it ended (it touches the range's end)."""
    FPS = 60000 / 1001

    def setUp(self):
        self.rpc, self.fake, self.calls = fake_rpc()
        s = lambda t: round(t * self.FPS) / self.FPS
        three = [{"name": n, "start": s(a), "end": s(b), "inPoint": 0.0, "outPoint": s(b) - s(a)}
                 for n, a, b in (("A", 0, 10), ("B", 10, 20), ("C", 20, 30))]
        self.fake.STATE["audio"][0] = [dict(c) for c in three]
        self.fake.STATE["video"][0] = [dict(c) for c in three]
        self.b = (s(10), s(20))

    def levels(self):
        return {c["name"]: round(ops.db_from_level(c["components"][0]["params"][1]["value"]), 2)
                for c in self.fake.STATE["audio"][0]}

    def scales(self):
        return {c["name"]: self.fake.chain(c)[1]["params"][1]["value"] for c in self.fake.STATE["video"][0]}

    def test_volume_range_leaves_the_neighbours_alone(self):
        for c in self.fake.STATE["audio"][0]:
            self.fake.chain(c, "audio")
        frm, to = self.b  # B's own start and end, as list_items reports them
        out = ops.set_volume(self.rpc, track="A1", from_s=frm, to_s=to, db=-6.0)
        self.assertEqual([r["name"] for r in out["clips"]], ["B"])
        # inspect_items counts a clip touching either end (A ends at B's start, C starts at B's end)
        self.assertEqual([s["name"] for s in out["skipped"]], ["A", "C"])
        self.assertEqual(self.levels(), {"A": 0.0, "B": -6.0, "C": 0.0})
        ops.set_volume(self.rpc, track="A1", from_s=frm, to_s=to, db=-3.0, mode="overlap")  # the 0.5 behaviour
        self.assertEqual(self.levels(), {"A": -3.0, "B": -3.0, "C": -3.0})

    def test_scale_range_in_one_step_without_the_neighbours(self):
        before = list(self.fake.TRANSACTIONS)
        out = ops.set_scale(self.rpc, track="V1", from_s=9.0, to_s=21.0, value=80.0)
        self.assertEqual([c["name"] for c in out["clips"]], ["B"])
        self.assertEqual(sorted(s["name"] for s in out["skipped"]), ["A", "C"])
        self.assertEqual(self.scales(), {"A": 100.0, "B": 80.0, "C": 100.0})
        self.assertEqual(len(self.fake.TRANSACTIONS) - len(before), 1)
        ops.set_scale(self.rpc, track="V1", from_s=9.0, to_s=21.0, value=90.0, mode="overlap")
        self.assertEqual(self.scales(), {"A": 90.0, "B": 90.0, "C": 90.0})

    def test_a_range_inside_one_clip_finds_nothing_and_says_how_to_widen_it(self):
        for fn in (lambda: ops.set_volume(self.rpc, track="A1", from_s=12.0, to_s=18.0, db=-6.0),
                   lambda: ops.set_scale(self.rpc, track="V1", from_s=12.0, to_s=18.0, value=50.0, dry_run=True)):
            with self.assertRaises(BridgeError) as cm:
                fn()
            self.assertEqual((cm.exception.code, cm.exception.outcome), ("NOT_FOUND", "not_committed"))
            self.assertIn("--mode overlap", cm.exception.hint)

    def test_at_still_means_the_clip_under_that_time(self):
        out = ops.set_volume(self.rpc, track="A1", at=15.0, db=-6.0)
        self.assertEqual([r["name"] for r in out["clips"]], ["B"])
        self.assertNotIn("rangeMode", out)


class ProbeApiTest(unittest.TestCase):
    DUMP ={"host": {"version": "27.1.0"}, "counts": {"exported": 2, "members": 3, "instances": 2},
            "premierepro": {"AudioFilterFactory": {"type": "object", "members": ["getDisplayNames(0)"]},
                            "Constants": {"type": "object", "members": ["TrackItemType:object"],
                                          "nested": {"TrackItemType": ["CLIP=1"]}}},
            "instances": {"markers": ["createAddMarkerAction(5)", "createRemoveMarkerAction(1)", "getMarkers(1)"],
                          "sequenceEditor": ["createCloneTrackItemAction(6)", "insertMogrtFromPath(4)"]}}

    def test_watch_looks_on_instances_then_exports(self):
        w = apiprobe.watch(self.DUMP)
        self.assertIs(w["Markers#createRemoveMarkerAction"], True)
        self.assertIs(w["Markers#createMoveMarkerAction"], False)
        self.assertIs(w["SequenceEditor#createRazorTrackAction"], False)
        self.assertIs(w["AudioFilterFactory.getDisplayNames"], True)
        self.assertIs(w["AudioFilterFactory.createComponentByDisplayName"], False)
        self.assertEqual(w["Project#save"], "unknown")  # no project instance in this dump
        self.assertIs(w["MogrtText"], False)
        summary = apiprobe.summarize(self.DUMP)
        self.assertIn("Markers#createMoveMarkerAction", summary["missing"])
        self.assertIn("Project#save", summary["unknown"])

    def test_diff_names_added_and_removed_members(self):
        new = json.loads(json.dumps(self.DUMP))
        new["instances"]["sequenceEditor"].append("createRazorTrackAction(2)")
        new["premierepro"]["Constants"]["nested"]["TrackItemType"] = []
        d = apiprobe.diff(self.DUMP, new)
        self.assertEqual(d["added"], ["@sequenceEditor#createRazorTrackAction(2)"])
        self.assertEqual(d["removed"], ["Constants.TrackItemType.CLIP=1"])

    def test_against_the_fake_saves_the_dump_and_diffs(self):
        rpc, _, _ = fake_rpc()
        out = tmpdir() / "api.json"
        first = ops.probe_api(rpc, output=out)
        self.assertEqual(first["output"], str(out.resolve()))
        dump = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("Project", dump["premierepro"])
        again = ops.probe_api(rpc, diff=out, no_instances=True)
        self.assertEqual(again["diff"]["added"], [])
        self.assertTrue(again["diff"]["removed"])  # the instances are not in the second dump
        with self.assertRaises(BridgeError) as cm:
            ops.probe_api(rpc, diff=tmpdir() / "missing.json")
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")


def epr(path: Path, file_type=None, title=None, exporter=None) -> Path:
    """A minimal encoder preset: the XML fields presets.read_preset reads (none at all when no file type)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "<PremiereData Version=\"3\">"
    if title:
        body += f"<PresetName>{title}</PresetName>"
    if exporter:
        body += f"<ExporterClassID>{int.from_bytes(exporter.encode('ascii'), 'big')}</ExporterClassID>"
    if file_type:
        body += f"<ExporterFileType>{int.from_bytes(file_type.encode('ascii'), 'big')}</ExporterFileType>"
    path.write_bytes((body + "</PremiereData>").encode("utf-8"))
    return path


def preset_tree() -> tuple[Path, Path]:
    """(system folder, user folder) laid out like Premiere's MediaIO/systempresets and an AME Presets folder."""
    root = tmpdir()
    system, user = root / "systempresets", root / "Presets"
    epr(system / "4E49434B_48323634" / "00 - Match Source - High bitrate.epr", "H264",
        "($$$/AME/EncoderHost/Presets/9bb5/PresetName=Match Source - High bitrate)", "NICK")
    epr(system / "4E49434B_48323634" / "High Quality 1080 HD.epr", "H264")
    epr(system / "3F3F3F3F_4D6F6F56" / "01 - Match Source.epr")  # no XML fields: the folder name decides
    epr(system / "4d584620_444d5846" / "01 - Match Source.epr")
    epr(system / "3F3F3F3F_57415645" / "Waveform Audio 48kHz 16-bit.epr", "WAVE")
    epr(system / "3F3F3F3F_504E4720" / "PNG Sequence (Match Source).epr", "PNG ")
    epr(system / "47494653_47494666" / "GIF Sequence (Match Source).epr", "GIFf", exporter="GIFS")
    epr(system / "Ingest" / "Media Analysis.epr", "H264")
    epr(user / "Group" / "My vertical HEVC.epr", "HEVC", "My vertical HEVC")
    return system, user


class PresetsTest(unittest.TestCase):
    """list_presets: system presets by their FourCC folders (or their XML), the user's own by their XML, filters,
    and the name resolution export_video uses."""

    def setUp(self):
        self.system, self.user = preset_tree()
        self.real = {"system_dirs": presets.system_dirs, "user_dirs": presets.user_dirs}
        for name, folders in (("system_dirs", [str(self.system)]), ("user_dirs", [str(self.user)])):
            patch = mock.patch.object(presets, name, lambda folders=folders: folders)
            patch.start()
            self.addCleanup(patch.stop)

    def test_fourcc_decoding(self):
        self.assertEqual((presets.fourcc("4E49434B", 16), presets.fourcc("48323634", 16)), ("NICK", "H264"))
        self.assertEqual((presets.fourcc(1211250228), presets.fourcc("444d5846", 16)), ("H264", "DMXF"))
        self.assertIsNone(presets.fourcc("not a number"))
        self.assertIsNone(presets.fourcc(5))  # not printable

    def test_lists_every_preset_with_its_format(self):
        data = ops.list_presets(stub_rpc({}))
        got = {(p["name"], p["format"], p["kind"], p["source"]) for p in data["presets"]}
        self.assertEqual(got, {
            ("00 - Match Source - High bitrate", "H.264", "video", "system"),
            ("High Quality 1080 HD", "H.264", "video", "system"),
            ("01 - Match Source", "QuickTime", "video", "system"),
            ("01 - Match Source", "MXF OP1a (DNx)", "video", "system"),
            ("Waveform Audio 48kHz 16-bit", "Waveform Audio", "audio", "system"),
            ("PNG Sequence (Match Source)", "PNG sequence", "image", "system"),
            ("GIF Sequence (Match Source)", "GIF sequence", "image", "system"),
            ("My vertical HEVC", "HEVC (H.265)", "video", "user")})  # no Ingest/Media Analysis
        self.assertEqual(data["count"], 8)
        first = next(p for p in data["presets"] if p["name"].startswith("00"))
        self.assertEqual((first["title"], first["fileType"], first["extensions"]),
                         ("Match Source - High bitrate", "H264", [".mp4", ".m4v"]))
        self.assertNotIn("notes", data)

    def test_format_filter(self):
        names = lambda fmt: sorted(p["name"] for p in ops.list_presets(stub_rpc({}), format=fmt)["presets"])  # noqa: E731
        self.assertEqual(names("h264"), ["00 - Match Source - High bitrate", "High Quality 1080 HD"])
        self.assertEqual(names("H.264"), names("h264"))
        self.assertEqual(names("quicktime"), ["01 - Match Source"])
        self.assertEqual(names("mov"), ["01 - Match Source"])
        self.assertEqual(names("h265"), ["My vertical HEVC"])
        self.assertEqual(names("hevc"), ["My vertical HEVC"])
        self.assertEqual(names("wav"), ["Waveform Audio 48kHz 16-bit"])
        self.assertEqual(names("mxf"), ["01 - Match Source"])
        self.assertEqual(names("prores raw"), [])

    def test_without_premiere_only_user_presets_and_a_note(self):
        with mock.patch.object(presets, "system_dirs", lambda: []):
            data = ops.list_presets(stub_rpc({}))
        self.assertEqual([p["name"] for p in data["presets"]], ["My vertical HEVC"])
        self.assertIn("No running Premiere", data["notes"][0])

    def test_where_presets_are_looked_for(self):
        exe_dir = self.system.parent / "Adobe Premiere Pro"
        own = exe_dir / "MediaIO" / "systempresets"
        own.mkdir(parents=True)
        running = mock.patch.object(media, "premiere_executables", lambda: [str(exe_dir / "Adobe Premiere Pro.exe")])
        with mock.patch.dict("os.environ", {presets.PRESET_DIR_ENV: str(self.system)}), running:
            self.assertEqual(self.real["system_dirs"](), [str(self.system), str(own)])
        ame = self.system.parent / "Documents" / "Adobe"
        for rel in ("Adobe Media Encoder/26.0/Presets", "Adobe Media Encoder (Beta)/27.0/Presets",
                    "Adobe Media Encoder/25.0"):
            (ame / rel).mkdir(parents=True)
        with mock.patch.object(presets, "documents_dir", lambda: ame.parent):
            self.assertEqual(self.real["user_dirs"](), [str(ame / "Adobe Media Encoder" / "26.0" / "Presets"),
                                                        str(ame / "Adobe Media Encoder (Beta)" / "27.0" / "Presets")])

    def test_resolve_by_name_title_format_and_path(self):
        self.assertEqual(presets.resolve("00 - match source - high bitrate")["format"], "H.264")
        self.assertEqual(presets.resolve("Match Source - High bitrate")["name"], "00 - Match Source - High bitrate")
        with self.assertRaises(BridgeError) as cm:
            presets.resolve("01 - Match Source")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("AMBIGUOUS", "not_committed"))
        self.assertEqual(sorted(m["format"] for m in cm.exception.data["matches"]), ["MXF OP1a (DNx)", "QuickTime"])
        self.assertEqual(presets.resolve("01 - Match Source", "quicktime")["format"], "QuickTime")
        with self.assertRaises(BridgeError) as cm:
            presets.resolve("Match Source High bitrate")
        self.assertEqual(cm.exception.code, "NOT_FOUND")
        self.assertIn("00 - Match Source - High bitrate", cm.exception.data["closeNames"])
        path = self.user / "Group" / "My vertical HEVC.epr"
        self.assertEqual((presets.resolve(str(path))["format"], presets.resolve(str(path))["source"]),
                         ("HEVC (H.265)", "path"))
        with self.assertRaises(BridgeError) as cm:
            presets.resolve(str(self.user / "missing.epr"))
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")
        with self.assertRaises(BridgeError) as cm:
            presets.resolve(r"\\evil-host\share\x.epr")
        self.assertEqual(cm.exception.code, "BAD_PARAMS")

    def test_the_same_preset_in_two_premieres_needs_the_path(self):
        beta = tmpdir() / "beta"  # a release and a beta Premiere running side by side
        epr(beta / "4E49434B_48323634" / "00 - Match Source - High bitrate.epr", "H264")
        with mock.patch.object(presets, "system_dirs", lambda: [str(self.system), str(beta)]):
            with self.assertRaises(BridgeError) as cm:
                presets.resolve("00 - Match Source - High bitrate")
        self.assertEqual((cm.exception.code, len(cm.exception.data["matches"])), ("AMBIGUOUS", 2))
        self.assertIn(".epr path of the one", str(cm.exception))


class ExportVideoTest(unittest.TestCase):
    """export_video: the preset and the output are checked before anything is sent, the render goes through
    export_sequence, and the file is checked against the sequence (mismatches warn, a missing file fails). These stubs
    are panels without the exportJobs feature: the render is one request (ExportJobTest has the job path)."""
    SEQ = dict(SEQ, durationSeconds=79.0)
    VERTICAL = {"hasAudio": True, "duration": 79.02, "videoDuration": 79.0, "width": 1080, "height": 1920,
                "fps": 59.94006}

    def setUp(self):
        self.system, self.user = preset_tree()
        for name, folders in (("system_dirs", [str(self.system)]), ("user_dirs", [str(self.user)])):
            patch = mock.patch.object(presets, name, lambda folders=folders: folders)
            patch.start()
            self.addCleanup(patch.stop)
        self.out = tmpdir() / "renders" / "cut.mp4"

    def panel(self, write=True, seq=None, **extra):
        def export(p):
            if write:
                Path(p["outputPath"]).write_bytes(b"\0" * 64)
            return {"exported": True, "outputPath": p["outputPath"], "preset": p["presetPath"]}
        return stub_rpc({"get_sequence": seq or self.SEQ, "ping": ping_reply(), "export_sequence": export, **extra})

    def refused(self, code, **kw):
        rpc = self.panel()
        with self.assertRaises(BridgeError) as cm:
            ops.export_video(rpc, **dict({"preset": "00 - Match Source - High bitrate", "output": str(self.out)}, **kw))
        self.assertEqual((cm.exception.code, cm.exception.outcome), (code, "not_committed"), cm.exception)
        self.assertEqual(rpc.calls, [])
        return cm.exception

    def test_render_and_check(self):
        rpc = self.panel()
        data = ops.export_video(rpc, preset="00 - Match Source - High bitrate", output=str(self.out),
                                probe_media=lambda p: self.VERTICAL)
        self.assertTrue(data["verified"], data)
        self.assertEqual(data["warnings"], [])
        self.assertEqual(set(data["checks"]), {"frameSize", "fps", "duration"})
        self.assertEqual((data["output"], data["bytes"], data["preset"]["format"]), (str(self.out), 64, "H.264"))
        sent = dict(rpc.calls)["export_sequence"]
        epr_path = self.system / "4E49434B_48323634" / "00 - Match Source - High bitrate.epr"
        self.assertEqual(sent, {"outputPath": str(self.out), "presetPath": str(epr_path)})
        self.assertEqual(data["mode"], "sync")

    def test_a_letterboxing_preset_warns_but_does_not_fail(self):
        """Live: "High Quality 1080 HD" made 1920x1080 of a 1080x1920 sequence (at 29.97)."""
        boxed = dict(self.VERTICAL, width=1920, height=1080, fps=29.97)
        data = ops.export_video(self.panel(), preset="High Quality 1080 HD", output=str(self.out),
                                probe_media=lambda p: boxed)
        self.assertFalse(data["verified"])
        self.assertEqual({k: c["ok"] for k, c in data["checks"].items()},
                         {"frameSize": False, "fps": False, "duration": True})
        self.assertIn("letterboxes", data["warnings"][0])
        short = dict(self.VERTICAL, videoDuration=78.9)
        data = ops.export_video(self.panel(), preset="00 - Match Source - High bitrate", output=str(self.out),
                                overwrite=True, probe_media=lambda p: short)
        self.assertEqual(data["checks"]["duration"]["ok"], False)
        unread = ops.export_video(self.panel(), preset="00 - Match Source - High bitrate", output=str(self.out),
                                  overwrite=True, probe_media=lambda p: {})
        self.assertFalse(unread["verified"])
        self.assertIn("ffprobe", unread["warnings"][0])

    def test_dry_run_sends_no_render_and_warns_about_a_long_one(self):
        rpc = self.panel(seq=dict(self.SEQ, durationSeconds=1200.0))
        data = ops.export_video(rpc, preset="00 - Match Source - High bitrate", output=str(self.out), dry_run=True)
        self.assertEqual([a for a, _ in rpc.calls], ["get_sequence", "ping"])
        self.assertTrue(data["dryRun"])
        self.assertFalse(self.out.parent.exists())
        self.assertIn("limit", data["warnings"][0])
        audio = ops.export_video(rpc, preset="Waveform Audio 48kHz 16-bit", output=str(self.out.with_suffix(".wav")),
                                 dry_run=True)
        self.assertEqual(audio["warnings"], [])

    def test_refusals_before_anything_is_sent(self):
        e = self.refused("BAD_PARAMS", output=str(self.out.with_suffix(".mov")))
        self.assertIn(".mp4", str(e))
        self.refused("BAD_PARAMS", output=r"\\evil-host\share\cut.mp4")
        self.refused("UNSUPPORTED", preset="PNG Sequence (Match Source)", output=str(self.out.with_suffix(".png")))
        self.refused("UNSUPPORTED", preset="GIF Sequence (Match Source)", output=str(self.out.with_suffix(".gif")))
        self.refused("AMBIGUOUS", preset="01 - Match Source", output=str(self.out.with_suffix(".mov")))
        self.refused("NOT_FOUND", preset="Nope")
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_bytes(b"the editor's earlier render")
        e = self.refused("CONFIRM_REQUIRED")
        self.assertIn("--overwrite", str(e))
        self.assertEqual(self.out.read_bytes(), b"the editor's earlier render")
        # live 27.1: a read-only target fails at once with "Unable to initialize export!" (a broken preset's message)
        os.chmod(self.out, stat.S_IREAD)
        self.addCleanup(os.chmod, self.out, stat.S_IWRITE | stat.S_IREAD)
        e = self.refused("BAD_PARAMS", overwrite=True)
        self.assertIn("read-only", str(e))
        self.assertEqual(self.out.read_bytes(), b"the editor's earlier render")

    def test_no_new_file_is_export_failed(self):
        with self.assertRaises(BridgeError) as cm:
            ops.export_video(self.panel(write=False), preset="00 - Match Source - High bitrate", output=str(self.out),
                             probe_media=lambda p: self.VERTICAL)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("EXPORT_FAILED", "not_committed"))
        self.out.write_bytes(b"old")  # an old file at the path is not the render
        os.utime(self.out, (1_000_000_000, 1_000_000_000))
        with self.assertRaises(BridgeError) as cm:
            ops.export_video(self.panel(write=False), preset="00 - Match Source - High bitrate", output=str(self.out),
                             overwrite=True, probe_media=lambda p: self.VERTICAL)
        self.assertEqual(cm.exception.code, "EXPORT_FAILED")

    def test_double_extension_and_a_lost_render(self):
        def doubled(p):
            Path(p["outputPath"] + ".mp4").write_bytes(b"\0")
            return {"exported": True, "outputPath": p["outputPath"], "preset": p["presetPath"]}
        data = ops.export_video(stub_rpc({"get_sequence": self.SEQ, "ping": ping_reply(), "export_sequence": doubled}),
                                preset="00 - Match Source - High bitrate", output=str(self.out),
                                probe_media=lambda p: self.VERTICAL)
        self.assertEqual(data["output"], str(self.out) + ".mp4")

        def timeout(p):
            raise BridgeError("export_sequence: Panel did not answer", "PANEL_TIMEOUT", "not_committed")
        with self.assertRaises(BridgeError) as cm:
            ops.export_video(stub_rpc({"get_sequence": self.SEQ, "ping": ping_reply(), "export_sequence": timeout}),
                             preset="00 - Match Source - High bitrate", output=str(self.out.with_name("b.mp4")))
        self.assertEqual(cm.exception.code, "PANEL_TIMEOUT")
        self.assertIn("still be rendering", cm.exception.data["note"])

    def test_against_the_fake(self):
        """The fake advertises exportJobs: a job, ended at the first export_status poll."""
        rpc, _, calls = fake_rpc()
        sleeps = []
        data = ops.export_video(rpc, preset="00 - Match Source - High bitrate", output=str(self.out),
                                probe_media=lambda p: dict(self.VERTICAL, duration=60.0, videoDuration=60.0),
                                sleep=sleeps.append)
        self.assertTrue(data["verified"], data)
        self.assertEqual([a for a, _ in calls], ["get_sequence", "ping", "export_sequence", "export_status"])
        self.assertEqual((data["mode"], data["polls"], sleeps), ("job", 1, [2.0]))
        self.assertEqual(calls[2][1]["wait"], False)
        self.assertTrue(self.out.is_file())


class ExportJobTest(unittest.TestCase):
    """export_video on a panel with the exportJobs feature: export_sequence {wait: false} starts a job, export_status is
    polled (a fake sleep here) with no time limit, a stall (no progress, no event, no growth of the file) ends the wait,
    and --detach / --job split the wait across calls."""
    SEQ = dict(SEQ, durationSeconds=79.0)
    FULL = {"hasAudio": True, "duration": 79.02, "videoDuration": 79.0, "width": 1080, "height": 1920, "fps": 59.94006}
    SHORT = dict(FULL, duration=30.0, videoDuration=30.0)
    PRESET = "00 - Match Source - High bitrate"

    def setUp(self):
        self.system, self.user = preset_tree()
        for name, folders in (("system_dirs", [str(self.system)]), ("user_dirs", [str(self.user)])):
            patch = mock.patch.object(presets, name, lambda folders=folders: folders)
            patch.start()
            self.addCleanup(patch.stop)
        self.epr = str(self.system / "4E49434B_48323634" / "00 - Match Source - High bitrate.epr")
        self.out = tmpdir() / "renders" / "long.mp4"
        self.sleeps = []

    def view(self, state="running", progress=None, events=None, **kw):
        """A job as export_status reports it."""
        return dict({"jobId": "export-1", "state": state, "progress": progress, "events": events or {},
                     "outputPath": str(self.out), "preset": self.epr, "sequence": "Seq", "error": None,
                     "startedAt": int(time.time() * 1000) - 1000, "elapsedSeconds": 41.5}, **kw)

    def panel(self, steps, files=(), start_state="running", features=("exportJobs",)):
        """export_sequence starts export-1 (and creates the file, as IMMEDIATELY does); the n-th export_status poll
        first writes files[n] bytes to the output (None or past the end: leaves it), then answers steps[n] — a view, or
        a BridgeError to raise; the last step repeats."""
        polls = {"n": 0}

        def start(p):
            Path(p["outputPath"]).write_bytes(b"\0" * 16)
            return {"exported": start_state == "done", "outputPath": p["outputPath"], "preset": p["presetPath"],
                    "jobId": "export-1", "started": True, "state": start_state}

        def status(p):
            n = polls["n"]
            polls["n"] += 1
            if n < len(files) and files[n] is not None:
                self.out.write_bytes(b"\0" * files[n])
            step = steps[min(n, len(steps) - 1)]
            if isinstance(step, BridgeError):
                raise step
            return {"jobs": [step]}

        return stub_rpc({"get_sequence": self.SEQ, "ping": ping_reply(features=list(features)), "export_sequence": start,
                         "export_status": status})

    def export(self, rpc, **kw):
        args = {"preset": self.PRESET, "output": str(self.out), "sleep": self.sleeps.append,
                "probe_media": lambda p: self.FULL}
        return ops.export_video(rpc, **dict(args, **kw))

    def failure(self, rpc, code, outcome, **kw):
        with self.assertRaises(BridgeError) as cm:
            self.export(rpc, **kw)
        self.assertEqual((cm.exception.code, cm.exception.outcome), (code, outcome), cm.exception)
        return cm.exception

    def test_polls_until_done_then_until_the_file_stops_growing(self):
        rpc = self.panel([self.view(progress=0.25, events={"progress": 1}),
                          self.view(progress=0.6, events={"progress": 2}),
                          self.view("done", 1.0, {"progress": 3, "complete": 1}, settledBy="promise")],
                         files=[1000, 2000, 3000])
        data = self.export(rpc)
        self.assertTrue(data["verified"], data)
        self.assertEqual((data["mode"], data["jobId"], data["polls"], data["progressSamples"]), ("job", "export-1", 3, 3))
        self.assertEqual((data["renderSeconds"], data["bytes"], data["job"]["settledBy"]), (41.5, 3000, "promise"))
        self.assertEqual(self.sleeps, [2.0] * 4)  # 3 polls, then one more: the file had grown at the last one
        self.assertEqual(dict(rpc.calls)["export_sequence"], {"outputPath": str(self.out), "presetPath": self.epr,
                                                               "wait": False})
        self.assertNotIn("missedPolls", data)
        self.assertEqual(data["warnings"], [])  # no 10-minute warning: a job has no request limit

    def test_a_job_done_at_once_is_not_polled(self):
        data = self.export(self.panel([self.view("done")], start_state="done"))
        self.assertEqual((data["polls"], self.sleeps), (0, []))

    def test_a_stall_ends_the_wait(self):
        """Nothing changes for --stall-seconds: EXPORT_STALLED (unknown), unless the file holds the whole sequence."""
        stalled = [self.view(progress=0.5, events={"progress": 4})]
        e = self.failure(self.panel(stalled, files=[500]), "EXPORT_STALLED", "unknown", stall_seconds=10,
                         probe_media=lambda p: self.SHORT)
        self.assertEqual((e.data["jobId"], e.data["idleSeconds"], e.data["bytes"]), ("export-1", 10.0, 500))
        self.assertEqual(len(self.sleeps), 6)  # the first poll brought news (progress, bytes), then 5 quiet ones
        self.assertIn("export_video --job export-1", str(e))
        self.out.unlink()
        data = self.export(self.panel(stalled, files=[500]), stall_seconds=10)
        self.assertEqual((data["doneBy"], data["verified"]), ("file", True))
        self.assertIn("never reported the end", data["warnings"][0])

    def test_a_growing_file_is_a_sign_of_life(self):
        """A job whose panel reports nothing (no events) is alive while its file grows."""
        e = self.failure(self.panel([self.view()], files=[100, 200, 300, 400]), "EXPORT_STALLED", "unknown",
                         stall_seconds=4, probe_media=lambda p: self.SHORT)
        self.assertEqual((len(self.sleeps), e.data["polls"], e.data["progressSamples"]), (6, 6, 0))

    def test_premieres_temp_files_are_signs_of_life(self):
        """Live 27.1, H.264: Premiere renders into "<stem><uuid>.m4v" / ".aac" beside the target, writes the .mp4 only at
        the end, and fires no event: the temp files' growth keeps the job alive (it stalled after 4 s without them)."""
        m4v = self.out.parent / "long25268af1-be53-498e-ab1b-da9e1cc84ec5.m4v"
        aac = self.out.parent / "longF15B00AD-33f9-479b-8eb2-b9eacf5e00d0.aac"
        other = self.out.parent / "longer25268af1-be53-498e-ab1b-da9e1cc84ec5.m4v"  # another render's: not counted
        polls = {"n": 0}

        def start(p):
            other.write_bytes(b"\0" * 7)
            return {"exported": False, "outputPath": p["outputPath"], "preset": p["presetPath"], "jobId": "export-1",
                    "started": True, "state": "running"}

        def status(p):
            polls["n"] += 1
            if polls["n"] <= 5:
                m4v.write_bytes(b"\0" * (1000 * polls["n"]))
                aac.write_bytes(b"\0" * (100 * polls["n"]))
                other.write_bytes(b"\0" * 7)
                return {"jobs": [self.view()]}
            m4v.unlink()
            aac.unlink()
            self.out.write_bytes(b"\0" * 6000)
            return {"jobs": [self.view("done", settledBy="promise")]}

        rpc = stub_rpc({"get_sequence": self.SEQ, "ping": ping_reply(features=["exportJobs"]), "export_sequence": start,
                        "export_status": status})
        self.assertEqual(ops._output_size(self.out), None)
        data = self.export(rpc, stall_seconds=4)
        self.assertEqual((data["polls"], data["bytes"], data["verified"]), (6, 6000, True))
        self.assertEqual(len(self.sleeps), 7)  # 6 polls, then one more: the size changed at the last one
        self.assertEqual(ops._output_size(self.out), 6000)

    def test_temp_file_size_is_the_files_own_not_the_listings(self):
        """Live 27.1: a directory listing's size (scandir's DirEntry.stat) of the .m4v Premiere was writing stayed 0 for
        20 s while the file grew, so the watcher stalled. A listing that reports a stale size must not matter: the size
        comes from the file itself (os.stat)."""
        self.out.parent.mkdir(parents=True, exist_ok=True)
        temp = self.out.parent / "long25268af1-be53-498e-ab1b-da9e1cc84ec5.m4v"
        temp.write_bytes(bytes(3000))
        real_scandir = os.scandir

        class Stale:  # a DirEntry whose cached size lags: 0, as Windows reported for a file held open by Premiere
            def __init__(self, e):
                self.name, self.path, self._e = e.name, e.path, e

            def is_file(self):
                return self._e.is_file()

            def stat(self):
                return os.stat_result((0,) * 10)

        class Listing:
            def __init__(self, path):
                self._it = real_scandir(path)

            def __enter__(self):
                return (Stale(e) for e in self._it)

            def __exit__(self, *exc):
                self._it.close()

        with mock.patch.object(ops.os, "scandir", Listing):
            self.assertEqual(ops._output_size(self.out), 3000)

    def test_failed_and_cancelled_jobs(self):
        e = self.failure(self.panel([self.view("failed", error="Encoding failed")]), "EXPORT_FAILED", "not_committed")
        self.assertIn("failed: Encoding failed", str(e))
        self.assertEqual(e.data["job"]["state"], "failed")
        self.out.unlink()
        e = self.failure(self.panel([self.view("cancelled", error="The render was cancelled in Premiere.")]),
                         "EXPORT_FAILED", "not_committed")
        self.assertIn("was cancelled in Premiere", str(e))

    def test_missed_polls_and_a_lost_job(self):
        timeout = BridgeError("export_status: Panel did not answer within 30s", "PANEL_TIMEOUT", "not_committed")
        data = self.export(self.panel([timeout, self.view(progress=0.5, events={"progress": 1}), self.view("done")]))
        self.assertEqual((data["polls"], data["missedPolls"], data["exported"]), (2, 1, True))
        self.out.unlink()
        gone = BridgeError("export_status: No export job export-1 in this panel (none)", "NOT_FOUND", "not_committed")
        e = self.failure(self.panel([self.view(progress=0.5), gone]), "EXPORT_LOST", "unknown")
        self.assertEqual((e.data["jobId"], e.data["output"]), ("export-1", str(self.out)))
        self.out.unlink()
        broken = BridgeError("export_status: nope", "INTERNAL", "not_committed")
        self.failure(self.panel([broken]), "INTERNAL", "not_committed")

    def test_detach_then_resume_with_job(self):
        rpc = self.panel([self.view(progress=0.4, events={"progress": 2}), self.view("done", 1.0, {"progress": 3})])
        start = self.export(rpc, detach=True)
        self.assertEqual((start["detached"], start["jobId"], start["state"]), (True, "export-1", "running"))
        self.assertIn("--job export-1", start["hint"])
        self.assertEqual([a for a, _ in rpc.calls], ["get_sequence", "ping", "export_sequence"])
        look = ops.export_video(rpc, job="export-1", detach=True, sleep=self.sleeps.append)
        self.assertEqual((look["detached"], look["state"], look["progress"], look["resumed"]), (True, "running", 0.4, True))
        done = ops.export_video(rpc, job="export-1", output=str(self.out), preset=self.PRESET, sleep=self.sleeps.append,
                                probe_media=lambda p: self.FULL)
        self.assertEqual((done["resumed"], done["verified"], done["polls"], done["output"]), (True, True, 0, str(self.out)))
        self.assertEqual(self.sleeps, [])
        args = cli.build_parser().parse_args(["export_video", "--job", "export-1", "--detach", "--stall-seconds", "600"])
        self.assertEqual((args.job, args.detach, args.stall_seconds, args.preset, args.output),
                         ("export-1", True, 600.0, None, None))

    def test_refusals(self):
        rpc = self.panel([self.view()])
        for kw in ({"job": "export-1", "output": str(self.out.with_name("other.mp4"))},
                   {"job": "export-1", "preset": "High Quality 1080 HD"},
                   {"job": "export-1", "dry_run": True},
                   {"preset": None}, {"output": None}, {"stall_seconds": 0}):
            with self.subTest(kw=kw):
                self.failure(rpc, "BAD_PARAMS", "not_committed", **kw)
        self.assertNotIn("export_sequence", [a for a, _ in rpc.calls])
        old = self.panel([self.view()], features=())
        self.failure(old, "PANEL_OUTDATED", "not_committed", detach=True)
        self.failure(old, "PANEL_OUTDATED", "not_committed", job="export-1")
        self.assertNotIn("export_sequence", [a for a, _ in old.calls])

    def test_against_the_fake(self):
        """The fake's jobs: progress events per poll, a render error, a job that never ends, one that sends nothing,
        and one render at a time."""
        def run(env, **kw):
            with mock.patch.dict(os.environ, env):
                rpc, fake, calls = fake_rpc()
                self.sleeps.clear()
                return self.export(rpc, overwrite=True, **kw), rpc, calls

        data, _, calls = run({"PPRO_FAKE_EXPORT_POLLS": "3"}, probe_media=lambda p: dict(self.FULL, duration=60.0,
                                                                                         videoDuration=60.0))
        self.assertEqual((data["polls"], data["progressSamples"], data["job"]["events"]), (3, 3, {"progress": 3,
                                                                                                    "complete": 1}))
        self.assertEqual((data["job"]["settledBy"], data["job"]["eventSample"]["progress"]["progress"]),
                         ("promise", 33.33))
        self.assertEqual([a for a, _ in calls].count("export_status"), 3)
        with self.assertRaises(BridgeError) as cm:
            run({"PPRO_FAKE_EXPORT_RESULT": "fail"})
        self.assertEqual(cm.exception.code, "EXPORT_FAILED")
        self.assertTrue(cm.exception.data["job"]["error"].endswith("; Premiere: Encoding failed (fake panel)"))
        with self.assertRaises(BridgeError) as cm:
            run({"PPRO_FAKE_EXPORT_RESULT": "stall"}, stall_seconds=6, probe_media=lambda p: self.SHORT)
        self.assertEqual((cm.exception.code, len(self.sleeps)), ("EXPORT_STALLED", 3))
        data, _, _ = run({"PPRO_FAKE_EXPORT_RESULT": "silent", "PPRO_FAKE_EXPORT_POLLS": "2"},
                         probe_media=lambda p: dict(self.FULL, duration=60.0, videoDuration=60.0))
        self.assertEqual((data["job"]["settledBy"], data["progressSamples"], data["job"]["events"]), ("promise", 0, {}))
        start, rpc, _ = run({"PPRO_FAKE_EXPORT_RESULT": "stall"}, detach=True)
        with self.assertRaises(BridgeError) as cm:
            self.export(rpc, output=str(self.out.with_name("second.mp4")))
        self.assertEqual((cm.exception.code, cm.exception.data["jobId"]), ("EXPORT_RUNNING", start["jobId"]))
        with self.assertRaises(BridgeError) as cm:
            ops.export_video(rpc, job="export-9")
        self.assertEqual(cm.exception.code, "NOT_FOUND")
        self.assertIn("forgets its jobs", cm.exception.data["note"])


class AudioEffectOpsTest(unittest.TestCase):
    def test_against_the_fake(self):
        """降噪 is added by its display name and read back; Premiere arms its params (animation on, no keyframes), the
        reply says so, and set_params still gives one a static value (switching its animation off; live 27.1)."""
        rpc, _, calls = fake_rpc()
        dry = ops.add_audio_effect(rpc, track="A1", at=5.0, name="降噪", dry_run=True)
        self.assertEqual((dry["dryRun"], dry["matchName"]), (True, "c6762838-e451-4d22-9c90-b0eaa734605a"))
        data = ops.add_audio_effect(rpc, track="A1", at=5.0, name="降噪", label="Agent: denoise")
        self.assertTrue(data["verified"])
        self.assertIn("[0, 1, 2, 3, 4, 5]", data["note"])
        self.assertEqual(set(a for a, _ in calls), {"ping", "add_audio_effect"})
        fx = write_json([{"matchName": data["matchName"], "params": [{"index": 1, "value": 0.6}]}])
        self.assertEqual(len(ops.set_params(rpc, track="A1", at=5.0, json_file=fx)["set"]), 1)
        plain = ops.add_audio_effect(rpc, track="A1", at=5.0, name="denoise")
        self.assertNotIn("note", plain)
        fx = write_json([{"matchName": "AE.ADBE DeNoise", "params": [{"index": 1, "value": 25}]}])
        self.assertEqual(ops.set_params(rpc, track="A1", at=5.0, json_file=fx)["set"], ["AE.ADBE DeNoise[1]#1 Amount"])
        with self.assertRaises(BridgeError) as cm:
            ops.add_audio_effect(rpc, track="A1", at=5.0, name="Hall reverb")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("NOT_FOUND", "not_committed"))

    def test_refused_locally_and_verified(self):
        rpc = stub_rpc({})
        for kw in ({"track": "V1", "at": 5.0, "name": "DeNoise"}, {"track": "A1", "at": 5.0, "name": "  "}):
            with self.assertRaises(BridgeError) as cm:
                ops.add_audio_effect(rpc, **kw)
            self.assertEqual(cm.exception.code, "BAD_PARAMS")
        self.assertEqual(rpc.calls, [])
        unverified = {"dryRun": False, "track": "A1", "clip": {}, "displayName": "DeNoise", "matchedBy": "displayName",
                      "matchName": "AE.ADBE DeNoise", "before": [], "after": [], "verified": False}
        rpc = stub_rpc({"ping": ping_reply(), "get_sequence": SEQ, "add_audio_effect": unverified})
        with self.assertRaises(BridgeError) as cm:
            ops.add_audio_effect(rpc, track="A1", at=5.0, name="DeNoise", expect_seq="Seq")
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("VERIFY_FAILED", "committed"))
        self.assertEqual([a for a, _ in rpc.calls], ["ping", "get_sequence", "add_audio_effect"])
        old = stub_rpc({"ping": ping_reply(actions=[a for a in spec.action_names() if a != "add_audio_effect"])})
        with self.assertRaises(BridgeError) as cm:
            ops.add_audio_effect(old, track="A1", at=5.0, name="DeNoise")
        self.assertEqual(cm.exception.code, "PANEL_OUTDATED")


class SelectItemsTest(unittest.TestCase):
    """select_items: the Timeline selection for Premiere's own panels (Enhance Audio can't be added over UXP)."""

    def test_against_the_fake(self):
        rpc, fake, calls = fake_rpc()
        at = ops.select_items(rpc, track=["V1", "A1"], at=5.0, expect_seq="Sequence 01")
        self.assertEqual((at["mode"], at["after"], at["verified"]), ("at", 2, True))
        self.assertEqual([s["track"] for s in at["selected"]], ["V1", "A1"])
        self.assertEqual([a for a, _ in calls], ["ping", "get_sequence", "select_items"])
        self.assertEqual(calls[-1][1], {"dryRun": False, "videoTracks": [0], "audioTracks": [0], "atSeconds": 5.0})
        with self.assertRaises(BridgeError) as cm:  # the interview crosses 10-20 s: not inside
            ops.select_items(rpc, track="A1", from_s=10.0, to_s=20.0)
        self.assertEqual((cm.exception.code, cm.exception.outcome), ("NOT_FOUND", "not_committed"))
        self.assertEqual(len(fake.STATE["selection"]), 2)  # unchanged
        overlap = ops.select_items(rpc, track="A1", from_s=10.0, to_s=20.0, mode="overlap")
        self.assertEqual((overlap["before"], overlap["after"], calls[-1][1]["mode"]), (2, 1, "overlap"))
        cleared = ops.select_items(rpc, clear=True)
        self.assertEqual((cleared["mode"], cleared["after"], calls[-1][1]), ("clear", 0, {"dryRun": False, "clear": True}))

    def test_refused_locally(self):
        rpc = stub_rpc({})
        for kw in ({"track": "A1"}, {"at": 5.0}, {"track": "A1", "at": 5.0, "from_s": 1.0, "to_s": 2.0},
                   {"track": "A1", "from_s": 1.0}, {"track": "A1", "from_s": 3.0, "to_s": 2.0},
                   {"clear": True, "track": "A1"}, {"track": "A1", "at": 5.0, "mode": "all"}, {"track": "X1", "at": 5.0}):
            with self.subTest(kw=kw):
                with self.assertRaises(BridgeError) as cm:
                    ops.select_items(rpc, **kw)
                self.assertEqual((cm.exception.code, cm.exception.outcome), ("BAD_PARAMS", "not_committed"))
        self.assertEqual(rpc.calls, [])
        old = stub_rpc({"ping": ping_reply(actions=[a for a in spec.action_names() if a != "select_items"])})
        with self.assertRaises(BridgeError) as cm:
            ops.select_items(old, track="A1", at=5.0)
        self.assertEqual(cm.exception.code, "PANEL_OUTDATED")


class CliRunTest(unittest.TestCase):
    def test_error_mapping(self):
        def raising(exc):
            def rpc(action, params=None):
                raise exc
            return rpc
        for exc, code in ((ValueError("bad"), "BAD_PARAMS"), (FileNotFoundError("gone"), "FILE_NOT_FOUND"),
                          (PermissionError("denied"), "IO_ERROR"),
                          (BridgeError("x", "NO_PANEL", "not_committed"), "NO_PANEL")):
            reply = cli.run("get_project", {}, raising(exc))
            self.assertEqual((reply["ok"], reply["code"]), (False, code), exc)
            self.assertIn("outcome", reply)

    def test_bad_track_is_bad_params(self):
        reply = cli.run("list_items", {"kind": "all", "track": "X9"}, stub_rpc({}))
        self.assertEqual(reply["code"], "BAD_PARAMS")

    def test_legacy_json_message_becomes_see_data(self):
        reply = BridgeError(json.dumps({"frames": []})).to_reply()
        self.assertEqual(reply, {"ok": False, "error": "see data", "data": {"frames": []}})


if __name__ == "__main__":
    unittest.main()
