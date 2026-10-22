#!/usr/bin/env python3
"""Live smoke test: run the real Agent Bridge panel through its paces in a Premiere TEST project.

  python tests/live/live_smoke.py --media DIR [--write] [--keep] [--any-project] [--url ws://127.0.0.1:8017]
                                  [--out DIR] [--speech FILE] [--stt-language en-US] [--stt-wait 300]

Media in DIR (tests/live/make_media.py makes them): broll_audio.mp4 (video + audio, >= 5 s), EN/title.mov and
CN/title.mov (same name, different media), optionally speech_en.wav (spoken English, for Speech-to-Text).

Safety rules (they are the point of this file):
- Refuses unless the open project's name starts with PAB_TEST. --any-project allows the READ phase in another
  project; --write never runs outside a PAB_TEST* project.
- Every request carries the guard {projectPrefix: "PAB_TEST", projectGuid, sequenceGuid} (names when the panel
  reports no guid), so a project or sequence switch mid-run is refused by the panel, never written to.
- Without --write, write actions are blocked before they leave this process (checked against the action spec).
- Before writing, the saved .prproj is copied into the report folder (a fake panel has no file: skipped).
- Writes go only to a scratch region after the end of the active sequence: the top video track and the highest
  audio track. Media is imported into a new bin PAB_SMOKE_<run id> (+ PAB_SMOKE_<run id>_CN for the same-named
  CN title); markers are named "PAB_SMOKE <run id> ...".
- Cleanup removes clips only when a remove_items dry run lists exactly the clips this run placed; never ripple.
  (Placements are read back from the scratch region: don't edit past the sequence end while it runs.)
  The bridge cannot remove project items or markers: those stay and are listed in the report. --keep leaves the
  clips too.
- A write whose outcome is unknown or partial stops all further writes (cleanup still runs, guarded as above).

Every check prints one line: PASS, FAIL, SKIP or KNOWN_ISSUE. The JSON report goes to --out (default: a
pab_smoke_<run id> folder in the temp directory). Exit code: 0 no FAIL, 1 at least one FAIL, 2 refused or could
not start.
"""

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(1, str(REPO))

PREFIX = "PAB_TEST"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tx_fixture_en.json"
MEDIA_FILES = {"broll": "broll_audio.mp4", "title_en": "EN/title.mov", "title_cn": "CN/title.mov",
               "speech": "speech_en.wav"}
OPACITY = "AE.ADBE Opacity"
MOTION = "AE.ADBE Motion"  # param 1 is Scale
BLUR = "AE.ADBE Gaussian Blur 2"
PREFERRED_TRANSITIONS = ("AE.ADBE Cross Dissolve New", "AE.ADBE Cross Dissolve")
CAPABILITY_VALUES = (True, False, "unknown")
EPS = 1e-3


class Refused(Exception):
    """The run must not continue (wrong project, no panel, outdated panel)."""


class WriteBlocked(Exception):
    """A write action was attempted while writes are not enabled."""


def word_count(doc) -> int:
    return sum(1 for seg in (doc or {}).get("segments") or [] for w in seg.get("words") or []
               if w.get("type", "word") == "word")


def norm_path(p) -> str:
    p = str(p or "")
    if p.startswith("\\\\?\\"):  # Windows long-path prefix
        p = p[4:]
    return os.path.normcase(os.path.normpath(p)) if p else ""


class Smoke:
    def __init__(self, args, lib):
        self.args = args
        self.ops, self.replace, self.spec, self.transport, self.media = (
            lib["ops"], lib["replace"], lib["spec"], lib["transport"], lib["media"])
        self.BridgeError = lib["BridgeError"]
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.bin = f"PAB_SMOKE_{self.run_id}"
        self.bin_cn = f"{self.bin}_CN"
        self.label = f"PAB_SMOKE {self.run_id}"
        # Absolute: export_frame / export_otio paths are resolved by Premiere, not by this process.
        self.out = (Path(args.out).expanduser() if args.out else Path(tempfile.gettempdir()) / f"pab_smoke_{self.run_id}").resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.url = args.url or self.transport.broker_url()
        self.results = []
        self.writes_allowed = False
        self.writes_stopped = None
        self.placements = []
        self.left = {"projectItems": [], "markers": [], "clips": []}
        self.report = {"runId": self.run_id, "url": self.url, "write": bool(args.write), "out": str(self.out),
                       "results": self.results, "leftBehind": self.left}
        self.rpc = None
        self.items = {}
        self.p1 = self.p2 = None
        self.transcript_ok = False

    # ------------------------------------------------------------------ reporting

    def record(self, status, name, detail="", **data):
        entry = {"status": status, "name": name, "detail": detail}
        if data:
            entry["data"] = data
        self.results.append(entry)
        print(f"{status:<11} {name}  {detail}".rstrip(), flush=True)
        return status == "PASS"

    def check(self, name, ok, detail="", **data):
        return self.record("PASS" if ok else "FAIL", name, detail, **data)

    def error(self, name, e, *, write=False):
        """Record an unexpected failure; an uncertain write stops all further writes."""
        if isinstance(e, self.BridgeError):
            detail = f"{e.code or 'ERROR'} ({e.outcome or '?'}): {e}"
            data = {"code": e.code, "outcome": e.outcome}
            if e.data is not None:
                data["data"] = e.data
            uncertain = e.outcome in ("unknown", "partial")
        else:
            detail, data, uncertain = f"{type(e).__name__}: {e}", {}, True
        self.record("FAIL", name, detail, **data)
        if write and uncertain and not self.writes_stopped:
            self.writes_stopped = f"{name}: {detail}"
            self.record("FAIL", "write: stopped", "an uncertain write stops the write phase; cleanup still runs")

    def expect_error(self, name, fn, code):
        """fn must fail with `code` and outcome not_committed."""
        try:
            fn()
        except self.BridgeError as e:
            ok = e.code == code and (e.outcome in (None, "not_committed"))
            return self.check(name, ok, f"{e.code} ({e.outcome})" + ("" if ok else f", wanted {code}: {e}"))
        except Exception as e:  # noqa: BLE001 - reported, not raised
            return self.check(name, False, f"{type(e).__name__}: {e}, wanted {code}")
        return self.check(name, False, f"succeeded, wanted {code}")

    def can_write(self, name):
        if self.writes_stopped:
            self.record("SKIP", name, "write phase stopped")
            return False
        return True

    # ------------------------------------------------------------------ connection

    def make_rpc(self, guard):
        raw = self.transport.make_rpc(url=self.url, guard=guard)

        def rpc(action, params=None):
            if self.spec.kind(action) != "read" and not self.writes_allowed:  # unknown actions count as writes
                raise WriteBlocked(f"{action} is a write; this run is read-only (--write)")
            return raw(action, params)

        rpc.guard = guard
        return rpc

    def bootstrap(self):
        """Ping, find the project, refuse unless PAB_TEST*, then pin project + sequence for every later call."""
        rpc0 = self.make_rpc({"projectPrefix": PREFIX})
        try:
            ping = self.ops.ping(rpc0)
        except self.BridgeError as e:
            raise Refused(f"ping failed: {e.code}: {e}") from e
        self.report["ping"] = ping
        self.ping = ping
        try:
            project = self.ops.get_project(rpc0)
        except self.BridgeError as e:
            if e.code == "HOST_DENIED":
                self.project = None
                return
            if e.code != "PROJECT_MISMATCH" or not self.args.any_project:
                raise Refused(f"the open project is not a {PREFIX}* project ({e.code}: {e})") from e
            project = self.ops.get_project(self.make_rpc(None))
        name = project.get("name") or ""
        self.is_test_project = name.startswith(PREFIX)
        if not self.is_test_project and not self.args.any_project:
            raise Refused(f'the open project "{name}" does not start with {PREFIX}')
        if not self.is_test_project and self.args.write:
            raise Refused(f'--write only runs in a {PREFIX}* project; the open project is "{name}"')
        self.project = project
        guard = {"projectPrefix": PREFIX} if self.is_test_project else {}
        if project.get("guid"):
            guard["projectGuid"] = str(project["guid"])
        else:
            guard["project"] = name
        seq = self.ops.get_sequence(self.make_rpc(dict(guard)))
        if seq.get("guid"):
            guard["sequenceGuid"] = str(seq["guid"])
        else:
            guard["sequence"] = seq.get("name")
        self.guard = guard
        self.rpc = self.make_rpc(guard)
        self.seq = seq
        self.fps = float(seq.get("fps") or 30)
        try:
            self.frame = int(seq["timebase"]) / 254016000000
        except (KeyError, TypeError, ValueError):
            self.frame = 1.0 / self.fps
        self.report.update(project={k: project.get(k) for k in ("name", "path", "guid", "activeSequence")},
                           sequence={k: seq.get(k) for k in ("name", "guid", "width", "height", "fps")},
                           guard=guard)
        self.record("PASS", "bootstrap", f'project "{name}", sequence "{seq.get("name")}", guard {sorted(guard)}')

    def snap(self, t):
        return round(t / self.frame) * self.frame

    # ------------------------------------------------------------------ read phase

    def read_phase(self):
        self.check_ping()
        if self.ping.get("denied"):
            return False
        self.check_read_replies()
        self.check_guards()
        return True

    def check_ping(self):
        ping, spec = self.ping, self.spec
        self.check("read: ping protocol", ping.get("protocol") == spec.protocol(),
                   f"panel {ping.get('protocol')}, spec {spec.protocol()}")
        compatible = ping.get("compatible")
        if compatible is None:  # older ops: work it out
            from ppro_bridge import VERSION
            compatible = (ping.get("version") == VERSION and ping.get("brokerVersion") == VERSION
                          and ping.get("protocol") == spec.protocol() and not ping.get("denied"))
        self.check("read: ping compatible", bool(compatible), ping.get("warning", ""))
        self.check("read: panel not stuck", not ping.get("stuck"), json.dumps(ping.get("stuck")))
        missing = [a for a in spec.action_names() if a not in (ping.get("actions") or [])]
        self.check("read: panel has every spec action", not missing, ", ".join(missing))
        host = ping.get("hostInfo") or {}
        self.record("PASS" if ping.get("modules") in ("commonjs", "bundle", "fake") else "FAIL", "read: modules",
                    f"{ping.get('modules')} on {ping.get('host')} (build {host.get('buildNumber', '?')})")
        if ping.get("stale"):
            self.record("FAIL", "read: panel code is current", "codeHash differs from plugin/: Reload the panel",
                        codeHash=ping.get("codeHash"), localCodeHash=ping.get("localCodeHash"))
        elif ping.get("codeHash") and ping.get("localCodeHash"):
            self.record("PASS", "read: panel code is current", ping["codeHash"])
        else:
            self.record("SKIP", "read: panel code is current", "no codeHash/localCodeHash to compare")
        self.check_capabilities(ping, "read: capabilities")
        if ping.get("denied"):
            denied = ping["denied"]
            self.record("SKIP", "read: host denied", f"{denied.get('reason')} (match {denied.get('match')})")
            self.expect_error("read: ping-only mode refuses other actions",
                              lambda: self.make_rpc(None)("get_sequence"), "HOST_DENIED")
            return
        try:
            probed = self.ops.ping(self.rpc, probe=True)
            caps = probed.get("capabilities") or {}
            unknown = sorted(k for k, v in caps.items() if v == "unknown")
            self.check_capabilities(probed, "read: ping --probe")
            self.report["capabilities"] = caps
            self.record("PASS" if "capabilitiesProbedAt" in probed else "SKIP", "read: probe timestamp",
                        f"still unknown: {', '.join(unknown) or 'none'}")
        except Exception as e:  # noqa: BLE001
            self.error("read: ping --probe", e)

    def check_capabilities(self, ping, name):
        caps = ping.get("capabilities")
        if not isinstance(caps, dict):
            return self.check(name, False, f"capabilities is {type(caps).__name__}")
        known = set(self.spec.load().get("capabilities") or {})
        bad = {k: v for k, v in caps.items() if v not in CAPABILITY_VALUES}
        missing, extra = sorted(known - set(caps)), sorted(set(caps) - known)
        return self.check(name, not bad and not missing and not extra,
                          f"{sum(v is True for v in caps.values())} true, "
                          f"{sum(v is False for v in caps.values())} false, "
                          f"{sum(v == 'unknown' for v in caps.values())} unknown"
                          + (f"; bad values {bad}" if bad else "") + (f"; missing {missing}" if missing else "")
                          + (f"; not in spec {extra}" if extra else ""))

    def read_params(self):
        frames = self.out / "frames"
        frames.mkdir(exist_ok=True)
        return {
            "ping": {}, "get_project": {}, "get_sequence": {}, "list_items": {"kinds": ["video", "audio"]},
            "inspect_items": {"fromSeconds": 0.0, "toSeconds": 0.5, "maxKeyframes": 5},
            "list_project_items": {}, "list_transcripts": {}, "list_transition_names": {}, "export_transcript": {},
            "export_frame": {"dir": str(frames), "name": "pab_smoke_read.png", "atSeconds": 0.0},
            "export_otio": {"outputPath": str(self.out / "read.otio")},
        }

    def check_read_replies(self):
        """Every read action answers with at least the reply keys the spec lists."""
        params = self.read_params()
        for action in self.spec.load()["actions"]:
            name = action["name"]
            if action["kind"] != "read":
                continue
            if name not in params:
                self.record("SKIP", f"read reply: {name}", "no read params known to this harness")
                continue
            try:
                data = self.rpc(name, params[name])
            except self.BridgeError as e:
                if name == "export_transcript" and e.code == "NO_TRANSCRIPT":
                    self.record("SKIP", f"read reply: {name}", "no clip with a transcript yet (checked in --write)")
                else:
                    self.error(f"read reply: {name}", e)
                continue
            missing = [k for k in action.get("reply") or [] if k not in data]
            if name == "list_transcripts" and "clips" not in data:
                missing.append("clips (not jobsOnly)")
            self.check(f"read reply: {name}", not missing, "missing " + ", ".join(missing) if missing else "")
        for name in ("get_project", "get_sequence"):
            have = (self.project if name == "get_project" else self.seq).get("guid")
            self.record("PASS" if have else "SKIP", f"read: {name} guid", str(have) if have else "not reported")

    def check_guards(self):
        """The panel refuses a request pinned to another project or sequence (reads only: nothing can change)."""
        seq_name = self.seq.get("name") or ""
        self.expect_error("guard: other project prefix",
                          lambda: self.make_rpc({"projectPrefix": "PAB_SMOKE_NO_SUCH_PROJECT_"})("get_sequence"),
                          "PROJECT_MISMATCH")
        if self.guard.get("projectGuid"):
            self.expect_error("guard: other project guid",
                              lambda: self.make_rpc({"projectGuid": "pab-smoke-no-such-guid"})("get_sequence"),
                              "PROJECT_MISMATCH")
        self.expect_error("guard: other sequence name",
                          lambda: self.make_rpc({"sequence": seq_name + " (pab smoke: no such sequence)"})(
                              "list_items", {"kinds": ["video"]}), "SEQUENCE_MISMATCH")
        self.expect_error("spec: unknown param refused", lambda: self.rpc("get_sequence", {"pabSmokeBogus": 1}),
                          "BAD_PARAMS")
        # Deliberately not in the spec: sent past the read-only block (still guarded); the panel refuses it unrun.
        self.expect_error("spec: unknown action refused",
                          lambda: self.transport.make_rpc(url=self.url, guard=self.rpc.guard)("pab_smoke_no_such_action", {}),
                          "UNKNOWN_ACTION")

    # ------------------------------------------------------------------ timeline helpers

    def timeline(self):
        data = self.rpc("list_items", {"kinds": ["video", "audio"]})
        out = []
        for kind in ("video", "audio"):
            for t in data.get(kind) or []:
                label = t.get("label") or ("V" if kind == "video" else "A") + str(t["index"] + 1)
                for it in t.get("items") or []:
                    if it.get("start") is None or it.get("end") is None:
                        continue
                    out.append({"kind": kind, "index": t["index"], "track": label, "name": it.get("name"),
                                "start": it["start"], "end": it["end"], "inPoint": it.get("inPoint"),
                                "outPoint": it.get("outPoint"), "mediaPath": it.get("mediaPath")})
        return out

    def region_items(self):
        lo = self.region_start - self.frame / 2 - EPS
        return [c for c in self.timeline() if c["start"] >= lo]

    def key(self, c):
        return (c["track"], c.get("name"), round(c["start"] / self.frame))

    def clip_at(self, track_label, start, items=None):
        hits = [c for c in (items if items is not None else self.region_items())
                if c["track"] == track_label and abs(c["start"] - start) <= self.frame / 2 + 1e-6]
        return hits[0] if len(hits) == 1 else None

    def capture(self):
        """The scratch region holds only what this run put there: record it after every write."""
        self.placements = self.region_items()

    def inspect_clip(self, at):
        data = self.ops.inspect_items(self.rpc, track=[self.vlabel], from_s=at, to_s=at)
        clips = [c for t in data.get("tracks") or [] for c in t.get("clips") or []
                 if c["start"] <= at + EPS and c["end"] > at + EPS]
        if len(clips) != 1:
            raise AssertionError(f"expected one clip on {self.vlabel} at {at:.3f}, found {len(clips)}")
        return clips[0]

    @staticmethod
    def component(clip, match_name, occurrence=1):
        comps = [c for c in clip.get("components") or [] if c.get("matchName") == match_name]
        for i, c in enumerate(comps, 1):
            if (c.get("occurrence") or i) == occurrence:
                return c
        return None

    @staticmethod
    def param(component, index):
        return next((p for p in (component or {}).get("params") or [] if p.get("index") == index), None)

    def write_json(self, name, doc):
        path = self.out / name
        path.write_bytes(json.dumps(doc, indent=1).encode("utf-8"))
        return path

    # ------------------------------------------------------------------ write phase

    def write_phase(self):
        media = Path(self.args.media)
        files = {k: (media / v).resolve() for k, v in MEDIA_FILES.items()}
        if self.args.speech:
            files["speech"] = Path(self.args.speech).resolve()
        if not files["broll"].is_file():
            self.record("FAIL", "write: media", f"{MEDIA_FILES['broll']} not found in --media (tests/live/make_media.py)")
            return
        self.files = {k: p for k, p in files.items() if p.is_file()}
        self.record("PASS", "write: media", ", ".join(sorted(self.files)))
        if not self.backup():
            return
        self.before_timeline = self.timeline()
        self.before_items = self.ops.list_project_items(self.rpc).get("items") or []
        if not self.plan_region():
            return
        self.writes_allowed = True
        try:
            if self.import_media():
                self.insert_whole()
                self.insert_range()
                self.scale()
                self.params_three_time_bases()
                self.transition()
                self.replace_title()
                self.marker()
                self.transcripts()
                self.speech_to_text()
                self.insert_mode_insert()
                self.interpret_native_rate()
        except Exception as e:  # noqa: BLE001 - a harness bug must not skip cleanup
            self.error("write: unexpected", e, write=True)
        finally:
            self.cleanup()
            self.compare_after()
            self.writes_allowed = False

    def backup(self):
        if self.ping.get("modules") == "fake":
            self.record("SKIP", "write: backup .prproj", "fake panel: no project file")
            return True
        raw = (self.project.get("path") or "").strip()
        if raw.startswith("\\\\?\\"):
            raw = raw[4:]
        src = Path(raw) if raw else None
        if not src or not src.is_file():
            self.record("FAIL", "write: backup .prproj", f"no saved project file at {raw!r}: save the project first")
            return False
        dest = self.out / f"{src.stem}.backup_{self.run_id}{src.suffix}"
        try:
            if dest.exists():
                raise OSError(f"{dest} already exists")
            shutil.copy2(src, dest)
        except OSError as e:
            self.record("FAIL", "write: backup .prproj", f"could not copy {src}: {e}: not writing")
            return False
        self.report["backup"] = str(dest)
        return self.record("PASS", "write: backup .prproj", str(dest))

    def plan_region(self):
        seq = self.ops.get_sequence(self.rpc)
        videos, audios = seq.get("videoTracks") or [], seq.get("audioTracks") or []
        if not videos or not audios:
            self.record("FAIL", "write: scratch region", "the sequence needs at least one video and one audio track")
            return False
        self.vt, self.at = videos[-1]["index"], audios[-1]["index"]
        self.vlabel, self.alabel = f"V{self.vt + 1}", f"A{self.at + 1}"
        end = max([seq.get("durationSeconds") or 0.0] + [c["end"] for c in self.before_timeline])
        self.region_start = self.snap(math.ceil(end) + 5.0)
        if self.region_items():
            self.record("FAIL", "write: scratch region", f"not empty after {self.region_start:.3f}s: not writing")
            return False
        self.report["region"] = {"start": self.region_start, "videoTrack": self.vlabel, "audioTrack": self.alabel}
        return self.record("PASS", "write: scratch region",
                           f"{self.vlabel}/{self.alabel} from {self.region_start:.3f}s (sequence ends {end:.3f}s)")

    def import_media(self):
        if not self.can_write("write: import_media"):
            return False
        main = [str(self.files[k]) for k in ("broll", "title_en", "speech") if k in self.files]
        try:
            got = self.ops.import_media(self.rpc, path=main, bin=self.bin)
            new = list(got.get("newItems") or [])
            if "title_cn" in self.files:
                got_cn = self.ops.import_media(self.rpc, path=[str(self.files["title_cn"])], bin=self.bin_cn)
                new += list(got_cn.get("newItems") or [])
        except Exception as e:  # noqa: BLE001
            self.error("write: import_media", e, write=True)
            return False
        items = self.ops.list_project_items(self.rpc).get("items") or []
        for key, path in self.files.items():
            want_bin = self.bin_cn if key == "title_cn" else self.bin
            hits = [i for i in items if norm_path(i.get("mediaPath")) == norm_path(path)
                    and (i.get("bin") or "") == want_bin]
            if hits:
                self.items[key] = hits[0]
        missing = [k for k in self.files if k not in self.items]
        return self.check("write: import_media", not missing,
                          f"bin {self.bin}: {len(self.items)} item(s), newItems {new}"
                          + (f"; not found in the bin: {missing}" if missing else ""))

    def ref(self, key):
        item = self.items[key]
        return f"{item['bin']}/{item['name']}" if item.get("bin") else item["name"]

    def insert(self, name, key, at, in_s=None, out_s=None, mode="overwrite"):
        if not self.can_write(name):
            return None
        try:
            self.ops.insert_clip(self.rpc, item=self.ref(key), at=at, vtrack=self.vt, atrack=self.at,
                                 mode=mode, in_s=in_s, out_s=out_s, label=f"{self.label}: {name}",
                                 expect_seq=self.seq.get("name"))
        except Exception as e:  # noqa: BLE001
            self.error(name, e, write=True)
            return None
        finally:
            self.capture()
        clip = self.clip_at(self.vlabel, at, self.placements)
        if not clip:
            self.record("FAIL", name, f"no clip found on {self.vlabel} at {at:.3f}s after placing")
        return clip

    def insert_whole(self):
        at = self.region_start
        clip = self.insert("write: insert whole clip", "broll", at)
        self.p1 = clip
        if not clip:
            return
        info = self.media.probe_media(str(self.files["broll"]))
        length = clip["end"] - clip["start"]
        audio = [c for c in self.placements if c["track"] == self.alabel and abs(c["start"] - at) <= self.frame]
        detail = f"{length:.4f}s at {clip['start']:.4f}, audio leg on {self.alabel}: {bool(audio)}"
        wants = [info["frames"] / info["fps"]] if info.get("frames") and info.get("fps") else []
        wants += [info["duration"]] if info.get("duration") else []
        if wants:
            ok = any(abs(length - w) <= self.frame + 1e-6 for w in wants)
            self.check("write: insert whole clip", ok, detail + f", media {' / '.join(f'{w:.4f}' for w in wants)}s")
        else:
            self.check("write: insert whole clip", length > 0, detail + " (no ffprobe: length not checked)")
        if info.get("hasAudio"):
            self.check("write: insert whole clip audio leg", bool(audio), self.alabel)

    def insert_range(self):
        base = (self.p1["end"] if self.p1 else self.region_start) + 2.0
        at = self.snap(base)
        info = self.media.probe_media(str(self.files["broll"]))
        mfps = info.get("fps") or self.fps
        in_s, out_s = round(1.0 * mfps) / mfps, round(4.0 * mfps) / mfps  # on the media's frame grid
        clip = self.insert("write: insert source range", "broll", at, in_s, out_s)
        self.p2 = clip
        if not clip:
            return
        same_rate = abs(mfps - self.fps) < 0.01
        tol = (self.frame / 2 if same_rate else self.frame) + 1e-6
        length = clip["end"] - clip["start"]
        want = out_s - in_s
        problems = []
        if abs(clip["start"] - at) > self.frame / 2 + 1e-6:
            problems.append(f"start {clip['start']:.4f} != {at:.4f}")
        if clip.get("inPoint") is None or abs(clip["inPoint"] - in_s) > 0.5 / mfps + 1e-6:
            problems.append(f"in {clip.get('inPoint')} != {in_s:.4f}")
        if abs(length - want) > tol:
            problems.append(f"length {length:.4f} != {want:.4f}")
        self.check("write: insert source range (frame-exact)", not problems,
                   "; ".join(problems) or f"{in_s:.4f}-{out_s:.4f} at {at:.4f}, {round(length / self.frame)} frames")

    def mid(self, clip):
        return (clip["start"] + clip["end"]) / 2

    def scale(self):
        name = "write: set_scale"
        if not self.p2:
            return self.record("SKIP", name, "no range clip")
        if not self.can_write(name):
            return
        at = self.mid(self.p2)
        try:
            self.ops.set_scale(self.rpc, track=self.vlabel, at=at, value=50.0, label=f"{self.label}: scale 50",
                               expect_seq=self.seq.get("name"))
            back = self.ops.set_scale(self.rpc, track=self.vlabel, at=at, value=50.0, dry_run=True)
        except Exception as e:  # noqa: BLE001
            return self.error(name, e, write=True)
        before = [c.get("scaleBefore") for c in back.get("clips") or []]
        before = [b.get("value") if isinstance(b, dict) else b for b in before]  # tolerate a {value} wrapper
        self.check(name, len(before) == 1 and before[0] is not None and abs(before[0] - 50.0) < 1e-3,
                   f"read back {before}")

    def set_params(self, name, at, components, label):
        path = self.write_json(f"{name.replace(' ', '_').replace(':', '')}.json", components)
        return self.ops.set_params(self.rpc, track=self.vlabel, at=at, json_file=str(path),
                                   label=f"{self.label}: {label}", expect_seq=self.seq.get("name"))

    def params_three_time_bases(self):
        if not self.p2:
            return self.record("SKIP", "write: set_params", "no range clip")
        clip, at = self.p2, self.mid(self.p2)
        ip, start = clip.get("inPoint") or 0.0, clip["start"]
        cases = [  # (label, component, occurrence, timeBase, [t...], expected media seconds)
            ("media", OPACITY, 1, "media", [ip + 0.2, ip + 0.8], [ip + 0.2, ip + 0.8]),
            ("clip", BLUR, 1, "clip", [0.3, 0.9], [ip + 0.3, ip + 0.9]),
            ("sequence", BLUR, 2, "sequence", [start + 0.4, start + 1.0], [ip + 0.4, ip + 1.0]),
        ]
        values = ([0.0, 100.0], [0.0, 20.0], [5.0, 25.0])
        for (label, match, occ, base, ts, want), vals in zip(cases, values):
            name = f"write: set_params {label} time"
            if not self.can_write(name):
                continue
            comp = {"matchName": match, "occurrence": occ, "params": [
                {"index": 0, "timeBase": base, "keyframes": [{"t": t, "value": v} for t, v in zip(ts, vals)]}]}
            try:
                got = self.set_params(name, at, [comp], f"keyframes ({label} time)")
            except Exception as e:  # noqa: BLE001
                self.error(name, e, write=True)
                continue
            kf = [k for k in got.get("keyframes") or [] if k.get("matchName") == match and k.get("occurrence", 1) == occ]
            secs = kf[0].get("mediaSeconds") if kf else None
            ok = bool(secs) and len(secs) == len(want) and all(abs(a - b) <= 1e-3 for a, b in zip(secs, want))
            created = got.get("createdEffects") or []
            if match == BLUR and len(created) != 1:
                ok = False
            self.check(name, ok, f"mediaSeconds {secs}, wanted {[round(w, 4) for w in want]}, created {created}")
        self.capture()
        try:
            back = self.inspect_clip(at)
        except Exception as e:  # noqa: BLE001
            return self.error("write: set_params read-back", e)
        problems = []
        for label, match, occ, base, ts, want in cases:
            p = self.param(self.component(back, match, occ), 0)
            got = [k.get("t") for k in (p or {}).get("keyframes") or []]
            if not p or not p.get("keyframed") or len(got) != len(want) or any(
                    abs(a - b) > self.frame / 2 + 1e-6 for a, b in zip(got, want)):
                problems.append(f"{match}[{occ}] keyframes {got}, wanted {[round(w, 4) for w in want]}")
            elif p.get("timeBase", "media") != "media":
                problems.append(f"{match}[{occ}] timeBase {p.get('timeBase')}")
        self.check("write: set_params read-back (inspect_items)", not problems, "; ".join(problems))
        self.components_after_params = len(back.get("components") or [])
        self.param_negatives(at)

    def param_negatives(self, at):
        # The first component would create a third blur; the second is out of range. A request validated as a
        # whole changes nothing; one applied component by component would leave the extra blur behind.
        out_of_range = [{"matchName": BLUR, "occurrence": 3, "params": [{"index": 0, "value": 1.0}]},
                        {"matchName": MOTION, "params": [
                            {"index": 1, "timeBase": "clip", "keyframes": [{"t": 999.0, "value": 80.0}]}]}]
        self.expect_error("write: set_params keyframe out of range refused",
                          lambda: self.set_params("neg out of range", at, out_of_range, "must be refused"),
                          "KEYFRAME_OUT_OF_RANGE")
        try:
            n = len(self.inspect_clip(at).get("components") or [])
            self.check("write: refused set_params changed nothing", n == self.components_after_params,
                       f"{n} components, before {self.components_after_params}")
        except Exception as e:  # noqa: BLE001
            self.error("write: refused set_params changed nothing", e)
        no_base = [{"matchName": MOTION, "params": [{"index": 1, "keyframes": [{"t": 0.1, "value": 80.0}]}]}]
        self.expect_error("write: set_params keyframes without timeBase refused",
                          lambda: self.set_params("neg no timebase", at, no_base, "must be refused"), "BAD_PARAMS")
        static = [{"matchName": OPACITY, "params": [{"index": 0, "value": 50.0}]}]
        self.expect_error("write: set_params onto keyframed param refused",
                          lambda: self.set_params("neg keyframed", at, static, "must be refused"), "KEYFRAMED")

    def pick_transition(self):
        names = self.ops.list_transition_names(self.rpc).get("names") or []
        for want in PREFERRED_TRANSITIONS:
            if want in names:
                return want
        return next((n for n in names if "dissolve" in n.lower()), names[0] if names else None)

    def add_transition(self, name, clip, edge):
        match = self.pick_transition()
        if not match:
            self.record("SKIP", name, "list_transition_names returned nothing")
            return None
        duration = (round(0.5 * self.fps) + 0.1) / self.fps  # whole frames + 0.1: no tick rounding loss
        self.ops.add_transition(self.rpc, track=self.vlabel, at=self.mid(clip), match_name=match, duration=duration,
                                edge=edge, label=f"{self.label}: {match} at {edge}", expect_seq=self.seq.get("name"))
        return match

    def otio_transitions(self, tag):
        path = self.out / f"{tag}.otio"
        self.ops.export_otio(self.rpc, output=str(path))
        doc = json.loads(path.read_bytes().decode("utf-8-sig"))
        return self.replace.otio_transitions(self.rpc, self.vt, doc)

    def transition(self):
        name = "write: add_transition + OTIO read-back"
        if not self.p2:
            return self.record("SKIP", name, "no range clip")
        if not self.can_write(name):
            return
        try:
            match = self.add_transition(name, self.p2, "start")
            if not match:
                return
            found = [t for t in self.otio_transitions("after_transition")
                     if t.get("matchName") == match and abs(t["at"] - self.p2["start"]) <= 1.5 * self.frame]
        except Exception as e:  # noqa: BLE001
            return self.error(name, e, write=True)
        finally:
            self.capture()
        self.check(name, len(found) == 1, f"{match} at {self.p2['start']:.3f}s: {found}")

    def replace_title(self):
        name = "write: replace_clip EN -> CN"
        if "title_en" not in self.items or "title_cn" not in self.items:
            return self.record("SKIP", name, "EN/title.mov and CN/title.mov are needed")
        base = (self.p2["end"] if self.p2 else self.region_start) + 2.0
        clip = self.insert("write: insert title (EN)", "title_en", self.snap(base))
        if not clip or not self.can_write(name):
            return
        at, ip = self.mid(clip), clip.get("inPoint") or 0.0
        length = clip["end"] - clip["start"]
        try:
            k2 = ip + min(2.0, length - 2 * self.frame)
            self.set_params("title keys", at, [{"matchName": OPACITY, "params": [
                {"index": 0, "timeBase": "media", "keyframes": [{"t": ip + 0.5, "value": 100.0},
                                                                 {"t": k2, "value": 40.0}]}]}], "title keyframes")
            self.add_transition("write: title transition", clip, "end")
        except Exception as e:  # noqa: BLE001
            self.capture()
            return self.error("write: title effects", e, write=True)
        self.capture()
        cn = self.ref("title_cn")
        self.expect_error("write: replace_clip refuses keyframes outside the new range (dry run)",
                          lambda: self.ops.replace_clip(self.rpc, track=self.vlabel, at=at, with_item=cn, in_s=0.0,
                                                        out_s=1.0, dry_run=True, probe_media=self.media.probe_media),
                          "KEYFRAME_OUT_OF_RANGE")
        try:
            dropped = self.ops.replace_clip(self.rpc, track=self.vlabel, at=at, with_item=cn, in_s=0.0, out_s=1.0,
                                            drop_outside_keyframes=True, dry_run=True,
                                            probe_media=self.media.probe_media)
            self.check("write: replace_clip --drop-outside-keyframes (dry run)", bool(dropped.get("droppedKeyframes")),
                       json.dumps(dropped.get("droppedKeyframes")))
            plan = self.ops.replace_clip(self.rpc, track=self.vlabel, at=at, with_item=cn, dry_run=True,
                                         expect_seq=self.seq.get("name"), probe_media=self.media.probe_media)
            self.check("write: replace_clip dry run", plan.get("dryRun") is True,
                       f"audioRoute {plan.get('audioRoute')}, placeWhole {plan.get('placeWhole')}")
        except Exception as e:  # noqa: BLE001
            return self.error("write: replace_clip dry runs", e)
        try:
            rep = self.ops.replace_clip(self.rpc, track=self.vlabel, at=at, with_item=cn,
                                        expect_seq=self.seq.get("name"), label=f"{self.label}: replace",
                                        probe_media=self.media.probe_media)
        except Exception as e:  # noqa: BLE001
            self.capture()
            return self.error(name, e, write=True)
        self.capture()
        after = self.clip_at(self.vlabel, clip["start"], self.placements)
        swapped = after and norm_path(after.get("mediaPath")) == norm_path(self.items["title_cn"].get("mediaPath"))
        self.check(name, bool(rep.get("verified")) and bool(swapped),
                   f"verified {rep.get('verified')}, now {after and after.get('mediaPath')}, problems {rep.get('problems')}")

    def marker(self):
        name = "write: add_marker"
        if not self.can_write(name):
            return
        end = max([c["end"] for c in self.placements] or [self.region_start])
        marker = f"{self.label} marker"
        try:
            got = self.ops.add_marker(self.rpc, at=self.snap(end + 1.0), name=marker,
                                      comment="left by tests/live/live_smoke.py", color="cyan",
                                      label=f"{self.label}: marker")
        except Exception as e:  # noqa: BLE001
            return self.error(name, e, write=True)
        self.left["markers"].append(marker)
        self.check(name, bool(got.get("added")), marker)

    def transcripts(self):
        name = "write: import_transcript (fixture)"
        if not self.can_write(name):
            return
        ref = self.ref("broll")
        words_in = word_count(json.loads(FIXTURE.read_bytes().decode("utf-8")))
        self.transcript_ok = False
        try:
            got = self.ops.import_transcript(self.rpc, item=ref, file=str(FIXTURE))
        except self.BridgeError as e:
            if e.code == "TRANSCRIPT_NOT_IMPORTED":
                host = self.ping.get("hostInfo") or {}
                return self.record("KNOWN_ISSUE", name, f"Premiere imported an empty transcript on "
                                   f"{host.get('version', self.ping.get('host'))} build {host.get('buildNumber', '?')}"
                                   " (import by hand in the Text panel)", code=e.code, outcome=e.outcome)
            return self.error(name, e, write=True)
        except Exception as e:  # noqa: BLE001
            return self.error(name, e, write=True)
        self.transcript_ok = bool(got.get("imported")) and got.get("wordsOut") == words_in
        self.check(name, self.transcript_ok, f"wordsIn {got.get('wordsIn')}, wordsOut {got.get('wordsOut')}, "
                   f"method {got.get('method')}" + (f", warning {got['warning']}" if got.get("warning") else ""))
        if not self.transcript_ok:
            return
        self.expect_error("write: import_transcript without --replace refused",
                          lambda: self.ops.import_transcript(self.rpc, item=ref, file=str(FIXTURE)),
                          "TRANSCRIPT_EXISTS")
        try:
            out = self.out / "tx_export.json"
            self.ops.export_transcript(self.rpc, output=str(out), item=ref)
            n = word_count(json.loads(out.read_bytes().decode("utf-8-sig")))
            self.check("write: export_transcript round trip", n == words_in, f"{n} words, imported {words_in}")
        except Exception as e:  # noqa: BLE001
            self.error("write: export_transcript round trip", e)
        if self.capability("transcribe") is False:
            return self.record("SKIP", "write: transcribe_clip refuses a transcribed clip", "capability false")
        self.expect_error("write: transcribe_clip refuses a transcribed clip",
                          lambda: self.ops.transcribe_clip(self.rpc, item=ref, language=self.args.stt_language,
                                                           wait=0),
                          "TRANSCRIPT_EXISTS")

    def capability(self, flag):
        caps = self.report.get("capabilities") or self.ping.get("capabilities") or {}
        return caps.get(flag, "unknown")

    def speech_to_text(self):
        name = "write: transcribe_clip (Speech-to-Text)"
        if "speech" not in self.items:
            return self.record("SKIP", name, "no speech_en.wav (tests/live/make_media.py --speech)")
        if self.capability("transcribe") is False:
            return self.record("SKIP", name, "this Premiere build has no Speech-to-Text API (capability false)")
        if not self.can_write(name):
            return
        out = self.out / "stt.json"
        try:
            self.ops.transcribe_clip(self.rpc, item=self.ref("speech"), language=self.args.stt_language,
                                     wait=self.args.stt_wait, output=str(out), poll=self.args.stt_poll)
        except self.BridgeError as e:
            if e.code in ("LANGUAGE_UNAVAILABLE", "UNSUPPORTED"):
                return self.record("SKIP", name, f"{e.code}: {e}")
            return self.error(name, e)
        except Exception as e:  # noqa: BLE001
            return self.error(name, e)
        n = word_count(json.loads(out.read_bytes().decode("utf-8-sig"))) if out.is_file() else 0
        self.check(name, n > 0, f"{n} words in {out.name}")

    def insert_mode_insert(self):
        """insert_clip --mode insert, at the very end of the scratch region: nothing lies after it to shift."""
        name = "write: insert_clip mode insert"
        self.capture()
        end = max([c["end"] for c in self.placements] + [self.region_start])
        at = self.snap(end + 2.0)
        clip = self.insert(name, "broll", at, 0.0, 1.0, mode="insert")
        if clip:
            self.check(name, abs(clip["start"] - at) <= self.frame / 2 + 1e-6, f"placed at {clip['start']:.4f}s")

    def interpret_native_rate(self):
        """set_fps on the smoke B-roll item, to the file's own rate: the write path runs, nothing changes length."""
        name = "write: set_fps (native rate, smoke item only)"
        fps = self.media.probe_media(str(self.files["broll"])).get("fps")
        if not fps:
            return self.record("SKIP", name, "media frame rate unknown (no ffprobe)")
        if not self.can_write(name):
            return
        ref = self.ref("broll")
        try:
            dry = self.ops.set_fps(self.rpc, item=[ref], fps=round(fps, 3), dry_run=True)
            uses = dry.get("usesInActiveSequence") or []
            outside = [u for u in uses if u.get("start") is not None and u["start"] < self.region_start - self.frame]
            if outside:
                return self.record("FAIL", name, f"{ref} is used outside the scratch region: not interpreting",
                                   uses=outside)
            got = self.ops.set_fps(self.rpc, item=[ref], fps=round(fps, 3), label=f"{self.label}: {name}")
        except Exception as e:  # noqa: BLE001
            return self.error(name, e, write=True)
        self.left.setdefault("interpreted", []).append({"item": ref, "fps": round(fps, 3)})
        self.check(name, bool(got.get("items")), f"{ref} at {round(fps, 3)} fps")

    # ------------------------------------------------------------------ cleanup and diff

    def cleanup(self):
        name = "cleanup: remove placed clips"
        self.capture()
        if not self.placements:
            return self.record("SKIP", name, "nothing was placed")
        if self.args.keep:
            self.left["clips"] = [{k: c[k] for k in ("track", "name", "start", "end")} for c in self.placements]
            return self.record("SKIP", name, f"--keep: {len(self.placements)} clip(s) left in the scratch region")
        tracks = sorted({c["track"] for c in self.placements})
        lo = self.region_start - self.frame / 2
        hi = max(c["end"] for c in self.placements) + self.frame / 2
        try:
            dry = self.ops.remove_items(self.rpc, track=tracks, from_s=lo, to_s=hi, mode="inside", dry_run=True)
            would = sorted(self.key(dict(c, track=c.get("track"))) for c in dry.get("removed") or [])
            want = sorted(self.key(c) for c in self.placements)
            if would != want:
                self.left["clips"] = [{k: c[k] for k in ("track", "name", "start", "end")} for c in self.placements]
                return self.record("FAIL", name, "dry run does not match the recorded placements: not removing",
                                   wouldRemove=would, recorded=want)
            self.ops.remove_items(self.rpc, track=tracks, from_s=lo, to_s=hi, mode="inside",
                                  label=f"{self.label}: cleanup", expect_seq=self.seq.get("name"))
        except Exception as e:  # noqa: BLE001
            return self.error(name, e)
        left = self.region_items()
        self.left["clips"] = [{k: c[k] for k in ("track", "name", "start", "end")} for c in left]
        self.check(name, not left, f"removed {len(want)} clip(s) on {', '.join(tracks)}"
                   + (f"; still there: {len(left)}" if left else ""))

    def compare_after(self):
        def clip_key(c):
            return (c["track"], c.get("name"), round(c["start"], 3), round(c["end"], 3),
                    round(c.get("inPoint") or 0, 3), round(c.get("outPoint") or 0, 3))

        def item_key(i):
            return (i.get("bin") or "", i.get("name"), norm_path(i.get("mediaPath")))

        try:
            after_tl = self.timeline()
            after_items = self.ops.list_project_items(self.rpc).get("items") or []
        except Exception as e:  # noqa: BLE001
            return self.error("after: diff", e)
        before, after = {clip_key(c) for c in self.before_timeline}, {clip_key(c) for c in after_tl}
        if self.args.keep:
            self.record("SKIP", "after: timeline restored", f"--keep: {len(after - before)} clip(s) added")
        else:
            self.check("after: timeline restored", before == after,
                       f"added {sorted(after - before)}, removed {sorted(before - after)}" if before != after else "")
        bi, ai = {item_key(i) for i in self.before_items}, {item_key(i) for i in after_items}
        new, gone = sorted(ai - bi), sorted(bi - ai)
        foreign = [k for k in new if not k[0].startswith(self.bin)]
        self.left["projectItems"] = [f"{k[0]}/{k[1]}" for k in new]
        self.check("after: project items", not foreign and not gone,
                   f"{len(new)} new in {self.bin}*" + (f"; outside the smoke bins: {foreign}" if foreign else "")
                   + (f"; missing: {gone}" if gone else ""))

    # ------------------------------------------------------------------ run

    def run(self):
        try:
            self.bootstrap()
        except Refused as e:
            self.record("FAIL", "bootstrap", f"refused: {e}")
            return self.finish(2)
        except self.BridgeError as e:  # e.g. NO_SEQUENCE, PANEL_OUTDATED on the pinned rpc
            self.record("FAIL", "bootstrap", f"{e.code} ({e.outcome}): {e}")
            return self.finish(2)
        try:
            if self.project is None:  # host denied: ping-only mode
                self.check_ping()
                self.record("SKIP", "write phase", "host denied: the panel answers ping only")
            elif not self.read_phase():
                self.record("SKIP", "write phase", "host denied: the panel answers ping only")
            elif self.args.write:
                self.write_phase()
            else:
                self.record("SKIP", "write phase", "read-only run (add --write)")
        except Exception as e:  # noqa: BLE001 - report a harness or panel surprise instead of a traceback
            self.error("harness: unexpected error", e)
        return self.finish(1 if any(r["status"] == "FAIL" for r in self.results) else 0)

    def finish(self, code):
        counts = {s: sum(1 for r in self.results if r["status"] == s) for s in ("PASS", "FAIL", "SKIP", "KNOWN_ISSUE")}
        self.report.update(counts=counts, exitCode=code, finishedAt=datetime.now().isoformat(timespec="seconds"))
        path = self.out / "report.json"
        path.write_bytes(json.dumps(self.report, indent=1, ensure_ascii=False, default=str).encode("utf-8"))
        print(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped, "
              f"{counts['KNOWN_ISSUE']} known issue(s); report {path}")
        if any(self.left.values()):
            print("left behind: " + json.dumps(self.left, ensure_ascii=False))
        return code


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="live_smoke", description="Live smoke test (PAB_TEST* projects only).")
    p.add_argument("--media", required=True, help="folder with broll_audio.mp4, EN/title.mov, CN/title.mov")
    p.add_argument("--write", action="store_true", help="also run the write checks (PAB_TEST* projects only)")
    p.add_argument("--keep", action="store_true", help="leave the placed clips in the scratch region")
    p.add_argument("--any-project", action="store_true", help="allow the read phase in a non-PAB_TEST project")
    p.add_argument("--url", help="broker URL (default: PPRO_BRIDGE_URL or ws://127.0.0.1:8017)")
    p.add_argument("--out", help="report folder (default: pab_smoke_<run id> in the temp directory)")
    p.add_argument("--speech", help="speech file for Speech-to-Text (default: speech_en.wav in --media)")
    p.add_argument("--stt-language", default="en-US", help="Premiere Speech-to-Text language (default en-US)")
    p.add_argument("--stt-wait", type=float, default=300.0, help="seconds to wait for Speech-to-Text")
    p.add_argument("--stt-poll", type=float, default=5.0, help="seconds between Speech-to-Text polls")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        from ppro_bridge import media, ops, replace, spec, transport
        from ppro_bridge.console import utf8_stdio
        from ppro_bridge.errors import BridgeError
    except ImportError as e:
        print(f"FAIL        bootstrap  cannot import ppro_bridge ({e})", flush=True)
        return 2
    utf8_stdio()
    if not Path(args.media).is_dir():
        print(f"FAIL        bootstrap  --media {args.media!r} is not a folder", flush=True)
        return 2
    lib = {"ops": ops, "replace": replace, "spec": spec, "transport": transport, "media": media,
           "BridgeError": BridgeError}
    return Smoke(args, lib).run()


if __name__ == "__main__":
    sys.exit(main())
