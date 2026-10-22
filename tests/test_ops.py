"""ops: the command functions with a stub panel — the handshake (PANEL_OUTDATED), transcribe_clip polling,
replace_clip's keyframe pre-check and partial outcome, set_params/add_transition request shaping, ping's
compatibility/stale checks, ffprobe failures, and the CLI's error mapping."""

import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from helpers import BridgeError, fake_rpc, ping_reply, stub_rpc

from ppro_bridge import apiprobe, cli, media, ops, panelhash, replace, spec, transcript
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


class OtherOpsTest(unittest.TestCase):
    def test_import_media_missing_files(self):
        rpc = stub_rpc({})
        with self.assertRaises(BridgeError) as cm:
            ops.import_media(rpc, path=[str(tmpdir() / "gone.mp4")])
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")
        self.assertEqual(len(cm.exception.data["missing"]), 1)
        self.assertEqual(rpc.calls, [])

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


class ProbeApiTest(unittest.TestCase):
    DUMP = {"host": {"version": "27.1.0"}, "counts": {"exported": 2, "members": 3, "instances": 2},
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
