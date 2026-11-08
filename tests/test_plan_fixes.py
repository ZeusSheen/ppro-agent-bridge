"""apply_plan: whole-media placement, the active-sequence guard, and checking both legs."""

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import BridgeError, fake_rpc

from ppro_bridge import items, media, ops
from ppro_bridge import plan as edit_plan

SEQ_FPS = 60000 / 1001


def run_plan(commands, rpc, probe, dry_run=False, any_sequence=False, sequence="Sequence 01", convert=None):
    tmp = Path(tempfile.mkdtemp())
    for c in commands:
        if c.get("src"):
            (tmp / c["src"]).write_bytes(b"synthetic")
    plan = tmp / "plan.json"
    plan.write_text(json.dumps({"sequence": sequence, "media_root": ".", "commands": commands}), encoding="utf-8")
    args = argparse.Namespace(plan=str(plan), dry_run=dry_run, media_root=None, bin=None, no_bin=False, only=None,
                              overwrite=False, notes_as_markers=False, report=None, any_sequence=any_sequence)
    return edit_plan.run(args, rpc=rpc, require_panel=lambda actions: {}, parse_track=items.parse_track,
                         probe_media=probe, fit_scale=media.fit_scale, convert_audio=convert)


def probe_for(**media):
    return lambda path: dict(media)


def inserts(calls):
    return [p for a, p in calls if a == "insert_clip"]


class WholeMediaTest(unittest.TestCase):
    def test_full_range_is_placed_whole(self):
        rpc, fake, calls = fake_rpc()
        fake_seconds = 300 / SEQ_FPS  # a 300-frame 59.94 render
        os.environ["PPRO_FAKE_MEDIA_SECONDS"] = str(fake_seconds)  # what the fake gives the imported item
        report = run_plan([{"command": "insert_clip", "id": "g01", "track": "V3", "timeline_at": 2.0,
                            "src": "g01.mov", "src_in": 0, "src_out": round(fake_seconds, 3)}],
                          rpc, probe_for(hasAudio=False, duration=fake_seconds, frames=300, fps=SEQ_FPS))
        step = report["steps"][0]
        self.assertTrue(step.get("placedWhole"), step)
        self.assertEqual(step["status"], "placed", step)
        self.assertNotIn("inSeconds", inserts(calls)[0])

    def test_partial_range_sends_in_and_out(self):
        rpc, fake, calls = fake_rpc()
        report = run_plan([{"command": "insert_clip", "id": "g01", "track": "V3", "timeline_at": 2.0,
                            "src": "g01.mov", "src_in": 0, "src_out": 4.0}],
                          rpc, probe_for(hasAudio=False, duration=5.005, frames=300, fps=SEQ_FPS))
        self.assertNotIn("placedWhole", report["steps"][0])
        sent = inserts(calls)[0]
        self.assertEqual(sent["inSeconds"], 0.0)
        # the exact frame boundary, not a 4-decimal rounding of it (rounding down loses the frame)
        self.assertEqual(sent["outSeconds"], round(4.0 * SEQ_FPS) / SEQ_FPS)

    def test_whole_clip_that_could_overrun_the_next_one_is_placed_as_a_range(self):
        rpc, fake, calls = fake_rpc()
        # a 60 fps, 60 s file: interpreted at 59.94 it runs 3.6 frames longer, into the clip at 62 s
        fake.STATE["video"][2].append({"name": "next.mov", "start": 62.0, "end": 64.0, "inPoint": 0.0,
                                       "outPoint": 2.0, "scale": 100.0})
        report = run_plan([{"command": "insert_clip", "id": "long", "track": "V3", "timeline_at": 2.0,
                            "src": "long.mov", "src_in": 0, "src_out": 60.0}],
                          rpc, probe_for(hasAudio=False, duration=60.0, frames=3600, fps=60.0))
        step = report["steps"][0]
        self.assertIn("endClampRisk", step)
        self.assertIn("inSeconds", inserts(calls)[0])

    def test_range_past_a_short_picture_stops_at_its_last_frame(self):
        # a news download: 9.5 s of picture in a 10 s file (the audio runs on); the plan asks for the whole file
        rpc, fake, calls = fake_rpc()
        report = run_plan([{"command": "insert_clip", "id": "n01", "track": "V3", "timeline_at": 2.01,
                            "src": "n01.mp4", "src_in": 0, "src_out": 10.0}],
                          rpc, probe_for(hasAudio=True, duration=10.0, videoDuration=9.5, frames=285, fps=30.0),
                          dry_run=True)
        step = report["steps"][0]
        self.assertEqual(step["pictureEnds"], 9.5)
        self.assertNotIn("placedWhole", step)
        self.assertLessEqual(step["_out"] if "_out" in step else step["srcOut"], 9.5 + 1e-9)
        self.assertTrue(any("picture ends at 9.500s" in w for w in report["warnings"]), report["warnings"])

    def test_range_starting_after_the_picture_is_an_error(self):
        rpc, _, calls = fake_rpc()
        report = run_plan([{"command": "insert_clip", "id": "n01", "track": "V3", "timeline_at": 2.0,
                            "src": "n01.mp4", "src_in": 9.8, "src_out": 10.0}],
                          rpc, probe_for(hasAudio=True, duration=10.0, videoDuration=9.5, frames=285, fps=30.0))
        self.assertEqual(report["steps"][0]["status"], "error")
        self.assertFalse(inserts(calls))

    def test_a_codec_premiere_cannot_import_is_an_error_before_anything_is_sent(self):
        rpc, _, calls = fake_rpc()
        report = run_plan([{"command": "insert_clip", "id": "yt", "track": "V3", "timeline_at": 2.0,
                            "src": "yt.mp4", "src_in": 0, "src_out": 3.0}],
                          rpc, probe_for(hasAudio=True, duration=10.0, videoCodec="av1", audioCodecs=["opus"]))
        step = report["steps"][0]
        self.assertEqual(step["status"], "error")
        self.assertIn("av1, opus", step["why"])
        self.assertIn("ffmpeg", step["transcode"])
        self.assertFalse({"import_media", "insert_clip"} & {a for a, _ in calls})

    def test_picture_a_frame_short_still_counts_as_the_whole_file(self):
        rpc, _, calls = fake_rpc()
        report = run_plan([{"command": "insert_clip", "id": "g01", "track": "V3", "timeline_at": 2.0,
                            "src": "g01.mov", "src_in": 0, "src_out": 5.005}],
                          rpc, probe_for(hasAudio=False, duration=5.005, videoDuration=5.005 - 1 / SEQ_FPS,
                                         frames=299, fps=SEQ_FPS), dry_run=True)
        self.assertNotIn("pictureEnds", report["steps"][0])


class ConvertAudioTest(unittest.TestCase):
    """A FLAC (6 Nov 2026: Premiere's modal "file format not supported" dialog, importFiles never settled) is
    converted to a WAV beside it, and the WAV is imported and placed."""
    FLAC = dict(hasAudio=True, coverArt=True, audioCodecs=["flac"], duration=171.626304)
    CMD = [{"command": "insert_clip", "id": "m01", "track": "V3", "timeline_at": 2.0, "src": "song.flac",
            "src_in": 10.0, "src_out": 40.0}]

    def converter(self, made, error=None):
        def convert(path, info):
            made.append(path)
            if error:
                return {"from": path, "error": error, "command": media.transcode_command(path, info)}
            wav = os.path.splitext(path)[0] + ".wav"
            Path(wav).write_bytes(b"RIFF")
            return {"from": path, "to": wav, "reused": False, "codecs": ["flac"]}
        return convert

    def test_dry_run_says_where_the_wav_goes_and_writes_nothing(self):
        rpc, _, calls = fake_rpc()
        made = []
        report = run_plan(self.CMD, rpc, probe_for(**self.FLAC), dry_run=True, convert=self.converter(made))
        step = report["steps"][0]
        self.assertEqual(step["status"], "planned", step)
        self.assertTrue(step["convert"]["from"].endswith("song.flac"))
        self.assertTrue(step["convert"]["to"].endswith("song.wav"))
        self.assertFalse(step["convert"]["reuse"])
        self.assertEqual(report["imports"], [step["convert"]["to"]])
        self.assertFalse(Path(step["convert"]["to"]).exists())
        self.assertEqual(made, [])
        self.assertFalse({"import_media", "insert_clip"} & {a for a, _ in calls})

    def test_real_run_imports_and_places_the_wav(self):
        rpc, _, calls = fake_rpc()
        made = []
        report = run_plan(self.CMD, rpc, probe_for(**self.FLAC), convert=self.converter(made))
        step = report["steps"][0]
        self.assertEqual(len(made), 1)
        self.assertEqual([p["paths"] for a, p in calls if a == "import_media"], [[step["convert"]["to"]]])
        self.assertTrue(step["convert"]["written"])
        self.assertEqual(step["status"], "placed", step)  # only an audio leg: a WAV has no picture
        self.assertTrue(report["ok"], report)

    def test_failed_conversion_fails_the_step_and_imports_nothing(self):
        rpc, _, calls = fake_rpc()
        report = run_plan(self.CMD, rpc, probe_for(**self.FLAC), convert=self.converter([], error="disk full"))
        step = report["steps"][0]
        self.assertEqual(step["status"], "failed")
        self.assertIn("disk full", step["why"])
        self.assertIn("pcm_s24le", step["transcode"])
        self.assertFalse({"import_media", "insert_clip"} & {a for a, _ in calls})
        self.assertFalse(report["ok"])


class SequenceGuardTest(unittest.TestCase):
    CMD = [{"command": "insert_clip", "id": "b01", "track": "V3", "timeline_at": 2.0, "src": "b01.mp4",
            "src_in": 0, "src_out": 3.0}]

    def test_real_run_refuses_another_sequence(self):
        rpc, _, calls = fake_rpc()
        with self.assertRaises(BridgeError) as cm:
            run_plan(self.CMD, rpc, probe_for(hasAudio=True), sequence="Other")
        self.assertEqual(cm.exception.code, "SEQUENCE_MISMATCH")
        self.assertFalse(inserts(calls))

    def test_dry_run_only_warns(self):
        rpc, _, _ = fake_rpc()
        report = run_plan(self.CMD, rpc, probe_for(hasAudio=True), dry_run=True, sequence="Other")
        self.assertTrue(any("will refuse" in w for w in report["warnings"]))

    def test_any_sequence_overrides(self):
        rpc, _, calls = fake_rpc()
        report = run_plan(self.CMD, rpc, probe_for(hasAudio=True), sequence="Other", any_sequence=True)
        self.assertEqual(len(inserts(calls)), 1)
        self.assertTrue(report["ok"], report)

    def test_switching_sequence_mid_plan_stops_it(self):
        rpc, fake, calls = fake_rpc()
        original = fake.HANDLERS["get_sequence"]

        def switching(p):
            seq = original(p)
            if inserts(calls):  # after the first placement, the editor clicks another sequence
                seq["name"] = "Sequence 02"
            return seq

        fake.HANDLERS["get_sequence"] = switching
        cmds = [dict(self.CMD[0], id=f"b0{i}", timeline_at=2.0 + 4 * i) for i in range(3)]
        report = run_plan(cmds, rpc, probe_for(hasAudio=True))
        statuses = [s["status"] for s in report["steps"]]
        self.assertEqual(statuses, ["placed", "aborted", "aborted"])
        self.assertEqual(len(inserts(calls)), 1)
        self.assertFalse(report["ok"])


class BothLegsTest(unittest.TestCase):
    def test_audio_leg_is_checked(self):
        rpc, _, _ = fake_rpc()
        # ffprobe says the .mov has audio, but the (fake) timeline gets no audio item for it
        report = run_plan([{"command": "insert_clip", "id": "g01", "track": "V3", "timeline_at": 2.0,
                            "src": "g01.mov", "src_in": 0, "src_out": 2.0}],
                          rpc, probe_for(hasAudio=True, duration=5.0, frames=300, fps=SEQ_FPS))
        step = report["steps"][0]
        self.assertEqual(step["status"], "unverified")
        self.assertIn("audio", step["why"])

    def test_video_and_audio_found(self):
        rpc, _, _ = fake_rpc()
        report = run_plan([{"command": "insert_clip", "id": "b01", "track": "V3", "timeline_at": 2.0,
                            "src": "b01.mp4", "src_in": 1.0, "src_out": 3.0}],
                          rpc, probe_for(hasAudio=True, duration=10.0, frames=600, fps=SEQ_FPS))
        step = report["steps"][0]
        self.assertEqual(step["status"], "placed", step)
        self.assertEqual(step["atrack"], "A4")


class RunPlanTest(unittest.TestCase):
    """ops.apply_plan / plan.run_plan: the panel handshake first, then the 0.3.3 engine."""

    def write_plan(self, commands):
        tmp = Path(tempfile.mkdtemp())
        for c in commands:
            if c.get("src"):
                (tmp / c["src"]).write_bytes(b"synthetic")
        plan = tmp / "plan.json"
        plan.write_bytes(b"\xef\xbb\xbf" + json.dumps({"sequence": "Sequence 01", "media_root": ".",
                                                        "commands": commands}).encode("utf-8"))
        return plan

    def test_dry_run_pings_first_and_writes_nothing(self):
        rpc, _, calls = fake_rpc()
        plan = self.write_plan(SequenceGuardTest.CMD)
        report = ops.apply_plan(rpc, plan=plan, dry_run=True, probe_media=probe_for(hasAudio=True))
        self.assertTrue(report["ok"], report)
        self.assertEqual(calls[0][0], "ping")
        self.assertFalse({"import_media", "insert_clip"} & {a for a, _ in calls})

    def test_panel_without_an_action_is_refused(self):
        rpc, fake, calls = fake_rpc()
        original = fake.HANDLERS["ping"]
        fake.HANDLERS["ping"] = lambda p: dict(original(p), actions=["ping", "get_sequence"])
        with self.assertRaises(BridgeError) as cm:
            ops.apply_plan(rpc, plan=self.write_plan(SequenceGuardTest.CMD), probe_media=probe_for(hasAudio=True))
        self.assertEqual(cm.exception.code, "PANEL_OUTDATED")
        self.assertEqual([a for a, _ in calls], ["ping"])

    def test_missing_or_broken_plan_file(self):
        rpc, _, calls = fake_rpc()
        with self.assertRaises(BridgeError) as cm:
            ops.apply_plan(rpc, plan=Path(tempfile.mkdtemp()) / "none.json")
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")
        bad = Path(tempfile.mkdtemp()) / "bad.json"
        bad.write_bytes(b"{nope")
        with self.assertRaises(BridgeError) as cm:
            ops.apply_plan(rpc, plan=bad)
        self.assertEqual(cm.exception.code, "BAD_PARAMS")
        self.assertEqual(calls, [])


class VideoOnlyTest(unittest.TestCase):
    """"video_only": the B-roll picture without its audio leg (three times by hand on the 7-8 Nov 2026 jobs)."""

    STEP = {"command": "insert_clip", "id": "b1", "track": "V2", "timeline_at": 2.0, "src": "b1.mp4", "src_in": 0,
            "src_out": 4.0, "video_only": True}

    def probe(self):
        return probe_for(hasAudio=True, hasVideo=True, duration=10.0, frames=600, fps=SEQ_FPS, width=1080, height=1920)

    def test_the_audio_leg_goes_and_the_picture_stays(self):
        rpc, fake, calls = fake_rpc()
        report = run_plan([self.STEP], rpc, self.probe())
        step = report["steps"][0]
        self.assertEqual((step["status"], step["videoOnly"], step["audioLeg"]), ("placed", True, "removed"), step)
        self.assertEqual(len(fake.STATE["video"][1]), 1)
        self.assertEqual(fake.STATE["audio"][3], [])
        self.assertEqual(len(fake.STATE["audio"][0]), 1)  # the dialogue stays
        removals = [p for a, p in calls if a == "remove_items"]
        self.assertEqual((len(removals), removals[0]["audioTracks"], removals[0]["videoTracks"]), (1, [3], []))
        self.assertTrue(report["ok"])

    def test_dry_run_only_says_so(self):
        rpc, fake, calls = fake_rpc()
        report = run_plan([self.STEP], rpc, self.probe(), dry_run=True)
        self.assertTrue(report["steps"][0]["videoOnly"])
        self.assertFalse([a for a, _ in calls if a in ("insert_clip", "remove_items")])

    def test_a_clip_inside_the_leg_stops_the_removal(self):
        rpc, fake, calls = fake_rpc()
        real = fake.HANDLERS["insert_clip"]

        def with_a_stowaway(p):  # something else ends up within the leg's span on its track
            out = real(p)
            fake.STATE["audio"][p["audioTrack"]].append({"name": "sfx.wav", "start": 3.0, "end": 3.5, "inPoint": 0.0,
                                                         "outPoint": 0.5})
            return out
        fake.HANDLERS["insert_clip"] = with_a_stowaway
        report = run_plan([self.STEP], rpc, self.probe())
        self.assertEqual(report["steps"][0]["status"], "unverified", report["steps"][0])
        self.assertIn("sfx.wav", report["steps"][0]["why"])
        self.assertFalse([a for a, _ in calls if a == "remove_items"])
        self.assertFalse(report["ok"])

    def test_refused_while_planning(self):
        rpc, fake, calls = fake_rpc()
        bad = dict(self.STEP, video_only="yes")
        self.assertEqual(run_plan([bad], rpc, self.probe(), dry_run=True)["steps"][0]["status"], "error")
        music = probe_for(hasAudio=True, hasVideo=False, duration=10.0)
        step = run_plan([dict(self.STEP, src="m.wav")], rpc, music, dry_run=True)["steps"][0]
        self.assertEqual(step["status"], "error", step)
        self.assertIn("no picture", step["why"])


if __name__ == "__main__":
    unittest.main()
