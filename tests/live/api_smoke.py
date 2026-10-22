#!/usr/bin/env python3
"""Batch 0 of the UXP port: find out which documented UXP calls really work on this Premiere build.

  python tests/live/api_smoke.py --media DIR [--case NAME ...] [--audio-effect NAME] [--mogrt auto|PATH]
                                 [--allow-track-add] [--phone-media FILE] [--watch-tracks] [--video-preset EPR]
                                 [--keep] [--url ws://127.0.0.1:8017] [--out DIR]

1. probe_api: the full API inventory of this build (probe_api.json) and the watch list of calls the port needs.
2. api_smoke, case by case (the panel's fixed test cases, see plugin/lib/smoke.js), on a scratch clip that this run
   places after the end of the active sequence: every step is pass / fail / absent / error / skipped, read back by
   the panel, and each case puts back what it changed.
3. Checks from this side: the FCP XML / AAF files the converter case wrote, the project file's time stamp around
   Project#save, a video export through export_sequence with an H.264 preset (time, ffprobe), export_frame timings
   (backlog item e), black frames on --phone-media (item c), and with --watch-tracks how long get_sequence takes to
   see a track you add by hand (item d).

Safety (on top of live_smoke.py's, whose machinery this reuses): refuses unless the open project's name starts with
PAB_TEST, and the panel refuses api_smoke itself in any other project. Every request is pinned to the project and
sequence (guard). The .prproj is copied into the report folder before the first write. Writes go to the scratch
region (top video track, highest audio track, past the sequence end) and to objects the cases create and remove
(a marker, a bin, a subclip, a sequence clone). Opt-ins: --mogrt (inserts a template: font dialogs are possible),
--allow-track-add (Premiere may add tracks that no API removes), --phone-media, --watch-tracks (asks you to add a
track). A case whose outcome is unknown, or a stuck panel, stops the remaining cases; cleanup still runs.

Output: report.json, api_smoke.md (one row per step: case | api | status | detail | ms) and probe_api.json in --out.
Exit code: 0 no fail/error, 1 at least one, 2 refused or could not start.
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from live_smoke import PREFIX, REPO, Refused, Smoke, norm_path  # noqa: E402

CASES = ["markers", "clip_props", "track", "selection", "blend_mode", "keyframes", "components", "lumetri",
         "transitions", "audio_filter", "sequence_points", "properties", "project_items", "relink", "source_monitor",
         "converter_exports", "mogrt", "sequences", "implicit_track", "save"]  # cleanup_bin runs last, always
PREFERRED_AUDIO = ("DeNoise", "降噪", "Parametric Equalizer", "参数均衡器", "Hard Limiter", "强制限幅", "Dynamics", "动态")
STATUS = {"pass": "PASS", "fail": "FAIL", "error": "FAIL", "absent": "KNOWN_ISSUE", "skipped": "SKIP"}
STOP_CODES = {"PANEL_STUCK", "PANEL_TIMEOUT", "PANEL_DISCONNECTED", "TIMEOUT", "CONNECTION_CLOSED"}


def detail_of(reply, api) -> dict:
    """The dict detail of one step of a case reply ({} when there is none)."""
    for step in (reply or {}).get("steps") or []:
        if step.get("api") == api and isinstance(step.get("detail"), dict):
            return step["detail"]
    return {}


def find_video_preset(explicit=None):
    """An H.264 'Match Source' preset next to the running Premiere (or --video-preset)."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    from ppro_bridge import media
    for exe in media.premiere_executables():
        root = Path(exe).parent / "MediaIO" / "systempresets"
        if not root.is_dir():
            continue
        found = sorted(root.rglob("*.epr"))
        for want in ("Match Source - Adaptive High Bitrate", "Match Source - High bitrate", "Match Source"):
            hits = [p for p in found if want.lower() in p.stem.lower() and "48323634" in p.parent.name]
            if hits:
                return str(hits[0])
    return None


class ApiSmoke(Smoke):
    def __init__(self, args, lib):
        if not args.out:
            args.out = str(Path(os.environ.get("TEMP") or "/tmp") / f"pab_api_{time.strftime('%Y%m%d_%H%M%S')}")
        super().__init__(args, lib)
        self.bin = f"PAB_API_{self.run_id}"
        self.label = f"PAB_API {self.run_id}"
        self.cases = {}
        self.report["cases"] = self.cases

    # ------------------------------------------------------------------ setup

    def setup(self):
        media = Path(self.args.media)
        self.files = {"broll": (media / "broll_audio.mp4").resolve()}
        if self.args.phone_media:
            self.files["phone"] = Path(self.args.phone_media).resolve()
        missing = [str(p) for p in self.files.values() if not p.is_file()]
        if missing:
            self.record("FAIL", "setup: media", "missing " + ", ".join(missing))
            return False
        self.before_timeline = self.timeline()
        if not self.backup() or not self.plan_region():
            return False
        self.writes_allowed = True
        try:
            self.ops.import_media(self.rpc, path=[str(p) for p in self.files.values()], bin=self.bin)
        except Exception as e:  # noqa: BLE001
            self.error("setup: import_media", e, write=True)
            return False
        items = self.ops.list_project_items(self.rpc).get("items") or []
        for key, path in self.files.items():
            hits = [i for i in items if norm_path(i.get("mediaPath")) == norm_path(path) and i.get("bin") == self.bin]
            if hits:
                self.items[key] = hits[0]
        if "broll" not in self.items:
            self.record("FAIL", "setup: import_media", f"broll_audio.mp4 not found in bin {self.bin}")
            return False
        self.left["projectItems"].append(f"bin {self.bin} (imported media)")
        clip = self.insert("setup: scratch clip", "broll", self.region_start)
        if not clip:
            return False
        self.scratch = clip
        self.mid = clip["start"] + min(2.0, (clip["end"] - clip["start"]) / 2)
        self.copy = self.out / f"broll_copy_{self.run_id}.mp4"
        shutil.copy2(self.files["broll"], self.copy)
        return self.record("PASS", "setup", f"scratch clip {clip['name']} {clip['start']:.3f}-{clip['end']:.3f}s "
                                            f"on {self.vlabel}/{self.alabel}")

    def params(self, case):
        base = {"case": case, "videoTrack": self.vt, "audioTrack": self.at, "atSeconds": round(self.mid, 6),
                "itemName": self.ref("broll"), "outputDir": str(self.out)}
        if case in ("markers", "sequence_points"):
            base["atSeconds"] = round(self.region_start + 1.0, 6)
        if case == "relink":
            base["copyPath"] = str(self.copy)
        if case == "implicit_track":
            base["allowTrackAdd"] = True
        if case == "converter_exports" and self.args.aaf:
            base["allowAaf"] = True
        if "/" in base["itemName"]:
            base["binPath"], base["itemName"] = base["itemName"].rsplit("/", 1)
        if self.args.allow_project:
            base["allowProject"] = self.args.allow_project
        return base

    # ------------------------------------------------------------------ cases

    def run_case(self, case, extra=None):
        name = f"api: {case}"
        if self.writes_stopped:
            return self.record("SKIP", name, "stopped after an uncertain write")
        params = self.params(case)
        params.update(extra or {})
        try:
            reply = self.rpc("api_smoke", params)
        except self.BridgeError as e:
            self.error(name, e, write=True)
            if e.code in STOP_CODES and not self.writes_stopped:
                self.writes_stopped = f"{name}: {e.code}"
            return None
        finally:
            self.capture()
        key = case if case not in self.cases else f"{case} #{len([k for k in self.cases if k.startswith(case)]) + 1}"
        self.cases[key] = reply
        for step in reply.get("steps") or []:
            detail = json.dumps(step.get("detail"), ensure_ascii=False) if step.get("detail") is not None else ""
            self.record(STATUS.get(step["status"], "FAIL"), f"{name}: {step['api']}",
                        (detail[:240] + ("…" if len(detail) > 240 else "")) + f" ({step.get('ms', 0)} ms)")
        if reply.get("leftovers"):
            self.left.setdefault("caseLeftovers", []).extend(f"{case}: {x}" for x in reply["leftovers"])
            self.record("FAIL", f"{name}: leftovers", "; ".join(reply["leftovers"]))
        return reply

    def audio_filter(self):
        listing = self.run_case("audio_filter")
        if not listing:
            return
        names = detail_of(listing, "AudioFilterFactory.getDisplayNames").get("names") or []
        pick = self.args.audio_effect or next((n for want in PREFERRED_AUDIO for n in names if n == want), None)
        if not pick:
            return self.record("SKIP", "api: audio_filter (add)", "no preferred effect name in the list; pass --audio-effect")
        self.run_case("audio_filter", {"displayName": pick})

    def mogrt(self):
        info = self.run_case("mogrt")
        if not info or not self.args.mogrt:
            return
        path = self.args.mogrt
        if path == "auto":
            folder = detail_of(info, "SequenceEditor.getInstalledMogrtPath").get("path")
            found = sorted(Path(folder).rglob("*.mogrt")) if folder and Path(folder).is_dir() else []
            if not found:
                return self.record("SKIP", "api: mogrt (insert)", f"no .mogrt under {folder!r}")
            path = str(found[0])
        self.run_case("mogrt", {"mogrtPath": path})

    def save(self):
        proj = Path((self.project.get("path") or "").removeprefix("\\\\?\\"))
        before = proj.stat().st_mtime if proj.is_file() else None
        reply = self.run_case("save")
        after = proj.stat().st_mtime if proj.is_file() else None
        if before is None:
            self.record("SKIP", "check: Project#save wrote the file", "no saved project file to compare")
        elif reply is not None:
            self.check("check: Project#save wrote the file", before is not None and after is not None and after > before,
                       f"mtime {before} -> {after}")

    def converter_files(self):
        reply = self.cases.get("converter_exports") or {}
        for step in reply.get("steps") or []:
            path = step["detail"].get("path") if isinstance(step.get("detail"), dict) else None
            if step["status"] == "pass" and path:
                p = Path(path)
                self.check(f"check: {p.suffix} file written", p.is_file() and p.stat().st_size > 0,
                           f"{p} ({p.stat().st_size if p.is_file() else 0} bytes)")

    # ------------------------------------------------------------------ checks from this side

    def export_video(self):
        preset = find_video_preset(self.args.video_preset)
        if not preset:
            return self.record("SKIP", "check: export_sequence (video)", "no H.264 Match Source preset found; pass --video-preset")
        out = self.out / f"export_{self.run_id}.mp4"
        t0 = time.monotonic()
        try:
            self.rpc("export_sequence", {"outputPath": str(out), "presetPath": preset})
        except Exception as e:  # noqa: BLE001
            return self.error("check: export_sequence (video)", e)
        seconds = time.monotonic() - t0
        info = self.media.probe_media(str(out)) if out.is_file() else {}
        self.check("check: export_sequence (video)", out.is_file() and out.stat().st_size > 0,
                   f"{seconds:.1f}s; {info.get('width')}x{info.get('height')} {info.get('fps')} fps "
                   f"{info.get('duration')}s; preset {Path(preset).name}", seconds=seconds, probe=info)

    def frame_timings(self, at, tag, sizes=((None, None), (270, 480))):
        frames = self.out / "frames"
        frames.mkdir(exist_ok=True)
        rows = []
        for w, h in sizes:
            for attempt in (1, 2):
                try:
                    r = self.ops.export_frame(self.rpc, dir=frames, name=f"{tag}_{w or 'seq'}_{attempt}.png", at=at,
                                              width=w, height=h)
                except Exception as e:  # noqa: BLE001
                    self.error(f"check: export_frame {tag}", e)
                    continue
                rows.append({"size": f"{w}x{h}" if w else "sequence", "attempt": attempt,
                             "exportSeconds": r.get("exportSeconds"), "waitedSeconds": r.get("waitedSeconds"),
                             "black": r.get("black"), "pixels": [r.get("pixelWidth"), r.get("pixelHeight")]})
        self.report.setdefault("frames", {})[tag] = rows
        detail = "; ".join(f"{r['size']}#{r['attempt']} {r['exportSeconds']}s+{r['waitedSeconds']}s"
                           + (" BLACK" if r["black"] else "") for r in rows)
        self.record("PASS" if rows else "FAIL", f"check: export_frame timings ({tag})", detail)
        return rows

    def phone_media(self):
        if "phone" not in self.items:
            return
        at = self.snap(self.scratch["end"] + 2.0)
        clip = self.insert("check: place phone media", "phone", at)
        if not clip:
            return
        times = [clip["start"] + f * (clip["end"] - clip["start"]) for f in (0.1, 0.5, 0.9)]
        black = []
        for t in times:
            rows = self.frame_timings(t, f"phone_{t:.2f}", sizes=((None, None),))
            black += [t for r in rows if r["black"]]
        self.record("KNOWN_ISSUE" if black else "PASS", "check: phone media frames",
                    f"black at {sorted(set(round(t, 2) for t in black))}" if black else "no black frames",
                    item=self.items["phone"])

    def sample_frames(self):
        """Read only: frames across the real sequence, before any write (black frames: backlog item c)."""
        n = self.args.sample_frames or 0
        end = float(self.seq.get("durationSeconds") or 0)
        if n <= 0 or end <= 0:
            return
        times = [self.snap(end * (i + 0.5) / n) for i in range(n)]
        black = []
        for t in times:
            rows = self.frame_timings(t, f"sample_{t:.2f}", sizes=((None, None),))
            black += [round(t, 2) for r in rows if r["black"]]
        self.record("KNOWN_ISSUE" if black else "PASS", "check: sampled sequence frames",
                    f"{len(times)} times, black at {sorted(set(black))}" if black else f"{len(times)} times, none black")

    def remove_bin(self):
        if "broll" not in self.items:
            return
        reply = self.run_case("cleanup_bin", {"binName": self.bin})
        if reply and not reply.get("leftovers") and all(st["status"] == "pass" for st in reply.get("steps") or []):
            self.left["projectItems"] = [x for x in self.left["projectItems"] if not x.startswith(f"bin {self.bin}")]

    def watch_tracks(self):
        if not self.args.watch_tracks or not sys.stdin.isatty():
            return self.record("SKIP", "check: track count after adding a track", "needs --watch-tracks and a terminal")
        n0 = len(self.ops.get_sequence(self.rpc).get("videoTracks") or [])
        input(f"\nThe sequence has {n0} video tracks. Add ONE video track in Premiere (Sequence > Add Tracks), "
              "then press Enter here... ")
        t0 = time.monotonic()
        seen = None
        while time.monotonic() - t0 < 30:
            n = len(self.ops.get_sequence(self.rpc).get("videoTracks") or [])
            if n > n0:
                seen = time.monotonic() - t0
                break
            time.sleep(0.25)
        self.left["projectItems"].append("the video track you added by hand")
        self.record("PASS" if seen is not None and seen < 1 else "KNOWN_ISSUE", "check: track count after adding a track",
                    f"get_sequence saw it after {seen:.2f}s" if seen is not None else "not seen within 30 s")

    # ------------------------------------------------------------------ run

    def write_markdown(self):
        lines = ["# api_smoke " + self.run_id, "", f"Host: {json.dumps((self.ping or {}).get('hostInfo'))}", "",
                 "| case | api | status | detail | ms |", "|---|---|---|---|---|"]
        for case, reply in self.cases.items():
            for s in reply.get("steps") or []:
                detail = json.dumps(s.get("detail"), ensure_ascii=False) if s.get("detail") is not None else ""
                detail = detail.replace("|", "\\|")[:300]
                lines.append(f"| {case} | {s['api']} | {s['status']} | {detail} | {s.get('ms', '')} |")
        (self.out / "api_smoke.md").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    def run(self):
        try:
            self.bootstrap()
        except Refused as e:
            self.record("FAIL", "bootstrap", f"refused: {e}")
            return self.finish(2)
        except self.BridgeError as e:
            self.record("FAIL", "bootstrap", f"{e.code} ({e.outcome}): {e}")
            return self.finish(2)
        named = self.args.allow_project and (self.project or {}).get("name") == self.args.allow_project
        if not self.is_test_project and not named:
            self.record("FAIL", "bootstrap", f"api_smoke runs only in a {PREFIX}* project or the one --allow-project names "
                                             f"(open: {(self.project or {}).get('name')!r})")
            return self.finish(2)
        try:
            self.check_ping()
            if "api_smoke" not in (self.ping.get("actions") or []):
                self.record("FAIL", "bootstrap", "the loaded panel has no api_smoke: load the v0.5 panel and Reload")
                return self.finish(2)
            summary = self.ops.probe_api(self.rpc, output=self.out / "probe_api.json")
            self.report["probe"] = summary
            self.record("PASS", "probe_api", f"{summary['counts']}; missing {summary['missing']}; "
                                             f"unknown {summary['unknown']}")
            self.sample_frames()
            if self.setup():
                wanted = self.args.case or CASES
                for case in [c for c in CASES if c in wanted]:
                    if case == "audio_filter":
                        self.audio_filter()
                    elif case == "mogrt":
                        self.mogrt()
                    elif case == "save":
                        continue  # after the cleanup: the saved file must not hold this run's clips and bin
                    elif case == "implicit_track" and not self.args.allow_track_add:
                        self.record("SKIP", "api: implicit_track", "opt-in: --allow-track-add")
                    else:
                        self.run_case(case)
                self.converter_files()
                self.export_video()
                self.frame_timings(self.mid, "scratch")
                self.phone_media()
                self.watch_tracks()
        except Exception as e:  # noqa: BLE001
            self.error("harness: unexpected error", e)
        finally:
            try:
                if self.placements or getattr(self, "scratch", None):
                    self.cleanup()
                if not self.args.keep and not self.left.get("clips"):
                    self.remove_bin()
            except Exception as e:  # noqa: BLE001 - e.g. a stuck panel: report what is left instead of a traceback
                self.error("cleanup: unexpected error", e)
                self.left["clips"] = self.left.get("clips") or [
                    {k: c.get(k) for k in ("track", "name", "start", "end")} for c in self.placements]
            if "save" in (self.args.case or CASES) and getattr(self, "scratch", None):
                clean = (not self.left.get("clips") and not self.left.get("caseLeftovers")
                         and not any(x.startswith(f"bin {self.bin}") for x in self.left.get("projectItems") or []))
                if clean:
                    self.save()
                else:
                    self.record("SKIP", "api: save", "not saving: this run left something in the project")
            self.write_markdown()
        return self.finish(1 if any(r["status"] == "FAIL" for r in self.results) else 0)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="api_smoke", description="UXP API smoke test (PAB_TEST* projects only).")
    p.add_argument("--media", required=True, help="folder with broll_audio.mp4 (tests/live/make_media.py)")
    p.add_argument("--case", action="append", choices=CASES, help="only these cases (repeatable; default all)")
    p.add_argument("--audio-effect", help="audio effect display name for audio_filter (default: a known DeNoise/EQ name)")
    p.add_argument("--mogrt", help="'auto' (first template in Premiere's folder) or a .mogrt path; default: no insert")
    p.add_argument("--allow-track-add", action="store_true", help="run implicit_track (may add tracks no API removes)")
    p.add_argument("--phone-media", help="a phone clip to place and export frames from (black-frame check)")
    p.add_argument("--watch-tracks", action="store_true", help="ask you to add a track, time when get_sequence sees it")
    p.add_argument("--video-preset", help="an H.264 .epr for the video export check (default: found next to Premiere)")
    p.add_argument("--aaf", action="store_true", help="converter_exports also writes an AAF (a modal log may block it)")
    p.add_argument("--keep", action="store_true", help="leave the scratch clips (and the test bin) in place")
    p.add_argument("--allow-project", help="the exact name of a non-PAB_TEST project you agreed to test in "
                                           "(its .prproj is backed up first)")
    p.add_argument("--sample-frames", type=int, default=0, help="read only: export N frames across the sequence first")
    p.add_argument("--url", help="broker URL (default: PPRO_BRIDGE_URL or ws://127.0.0.1:8017)")
    p.add_argument("--out", help="report folder (default: pab_api_<run id> in the temp directory)")
    args = p.parse_args(argv)
    # live_smoke's bootstrap: a named project passes as "any project" (read rules), then run() checks the exact name
    args.any_project = bool(args.allow_project)
    args.write = not args.allow_project
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if str(REPO) not in sys.path:
        sys.path.insert(1, str(REPO))
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
    return ApiSmoke(args, lib).run()


if __name__ == "__main__":
    sys.exit(main())
