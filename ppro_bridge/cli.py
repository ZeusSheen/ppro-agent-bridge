"""
ppro — command-line client for ppro-agent-bridge.

This is the surface an AI agent (or a human) talks to. Always prints one JSON object to stdout; exit code 0 on
success, 1 on failure.

Usage (from the repo root: python -m ppro_bridge <command> …; from cli/: python ppro.py <command> …):
  ping [--probe]
  get_project | get_sequence | list_project_items | list_transition_names
  list_items [--track V3]                    clips on the timeline as JSON
  inspect_items [--track V5] [--output f.json]
  export_frame  --dir frames --at 12.5
  export_frames --dir frames --at 4,18.5,31.6 --sheet
  export_otio   --output timeline.otio
  probe_api     [--output dump.json] [--diff old.json] [--no-instances]
  list_transcripts [--jobs] | export_transcript --output f.json [--item "bin/name"]
  import_transcript --item "name" --file f.json [--replace]
  transcribe_clip --item "name" [--language en-US] [--wait 600] [--output f.json]
  import_media  --path a.mp4 --path b.mov [--bin "B-roll"]
  insert_clip   --item "name" --at 4.0 [--in 1.0 --out 3.5]
  apply_plan    plan.json [--dry-run]
  replace_clip  --track V5 --at 12 --with "CN/g01.mov" [--dry-run]
  set_scale     --track V3 --at 4.0 --fill
  set_params    --track V3 --at 4.0 --json params.json
  add_transition --track V3 --at 4.0 --match-name "AE.ADBE Cross Dissolve New" --duration 0.5 --edge start
  set_fps       --item g01.mov --fps 59.94
  add_marker    --at 9.0 --name "Fact check" --comment "..." --color red
  remove_items  --track V3 --from 35 --to 53 [--dry-run]

Requires: Python 3.10+, `pip install websockets`, broker running, Premiere open with the Agent Bridge panel
loaded. Optional: ffprobe on PATH (fit/fill scaling, audio detection), Pillow (contact sheets).
"""

import argparse
import inspect
import json

from . import ops, transport
from .console import utf8_stdio
from .errors import BridgeError
from .ops import MARKER_COLORS

__all__ = ["MARKER_COLORS", "build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ppro", description="Agent Bridge CLI for Premiere Pro")
    sub = parser.add_subparsers(dest="action", required=True)

    def add(name, **kw):
        return sub.add_parser(name, **kw)

    expect_seq = dict(dest="expect_seq", metavar="NAME", default=None,
                      help="Refuse unless this is the active sequence (guards against a UI switch)")

    pg = add("ping", help="Liveness check + version/protocol handshake")
    pg.add_argument("--probe", action="store_true", help="Re-run the panel's capability probe")
    add("get_project", help="Active project name / path / active sequence")
    add("get_sequence", help="Active sequence: frame size, fps, duration, tracks, player position")

    pli = add("list_items", help="Clips on the active sequence's tracks, as JSON")
    pli.add_argument("--kind", choices=["video", "audio", "all"], default="all")
    pli.add_argument("--track", default=None, help="Only this track, e.g. V3 or A1")

    pin = add("inspect_items", help="Effect chains (params, keyframes) and transitions of timeline clips")
    pin.add_argument("--track", action="append", default=None, help="Video track, e.g. V5 (repeatable; default all)")
    pin.add_argument("--from", dest="from_s", type=float, default=None)
    pin.add_argument("--to", dest="to_s", type=float, default=None)
    pin.add_argument("--output", default=None, help="Write the (large) result to a JSON file and print a summary")

    add("list_project_items", help="Project panel clips with bin and media path")

    pf = add("export_frame", help="Export one frame of the active sequence")
    pf.add_argument("--dir", required=True, help="Output directory (created if missing)")
    pf.add_argument("--name", default=None, help="Output file name (default: frame_<seconds>.png)")
    pf.add_argument("--at", type=float, default=None, help="Sequence time in seconds (default: player position)")
    pf.add_argument("--width", type=int, default=None, help="Default: the sequence frame width")
    pf.add_argument("--height", type=int, default=None, help="Default: the sequence frame height")

    pfs = add("export_frames", help="Export several frames, optionally as a contact sheet")
    pfs.add_argument("--dir", required=True)
    pfs.add_argument("--at", required=True, help="Comma-separated sequence seconds, e.g. 4,18.5,31.6")
    pfs.add_argument("--width", type=int, default=None)
    pfs.add_argument("--height", type=int, default=None)
    pfs.add_argument("--sheet", nargs="?", const="", default=None,
                     help="Also build a contact sheet (default path: <dir>/contact-sheet.jpg)")
    pfs.add_argument("--cols", type=int, default=4)
    pfs.add_argument("--thumb-width", type=int, default=300)

    po = add("export_otio", help="Export active sequence as OpenTimelineIO")
    po.add_argument("--output", required=True, help="Full output path ending in .otio")

    ppa = add("probe_api", help="This Premiere build's UXP API (classes, methods, instance members) and a watch list")
    ppa.add_argument("--output", default=None, help="Save the full dump here (JSON)")
    ppa.add_argument("--diff", default=None, help="An earlier dump to compare with (added / removed members)")
    ppa.add_argument("--no-instances", action="store_true", help="Skip the members of real instances")
    ppa.add_argument("--max-members", type=int, default=None, help="Cap per class / object (default 300)")

    plt = add("list_transcripts", help="Project clips with hasTranscript, plus the panel's Speech-to-Text jobs")
    plt.add_argument("--jobs", dest="jobs_only", action="store_true", help="Only the Speech-to-Text job table")

    add("list_transition_names", help="Video transition match names available in Premiere")

    pt = add("export_transcript", help="Export a clip's transcript as Adobe transcript JSON")
    pt.add_argument("--item", default=None,
                    help='Clip name or "<bin path>/<name>" (default: first clip with a transcript)')
    pt.add_argument("--output", required=True, help="Where to save the transcript JSON")

    pit = add("import_transcript", help="Import an Adobe-format transcript JSON onto a clip (checked locally first)")
    pit.add_argument("--item", required=True, help='Clip name, or "<bin path>/<name>" when names repeat')
    pit.add_argument("--file", required=True, help="Transcript JSON file (e.g. from transcribe.py)")
    pit.add_argument("--replace", action="store_true", help="Replace a transcript the clip already has")

    ptc = add("transcribe_clip", help="Premiere Speech-to-Text on a clip (26.5+): start, wait, export")
    ptc.add_argument("--item", required=True, help='Clip name, or "<bin path>/<name>" when names repeat')
    ptc.add_argument("--language", default=None,
                     help="Premiere language such as en-US (Speech-to-Text codes, not transcript JSON codes)")
    ptc.add_argument("--wait", type=float, default=600.0,
                     help="Seconds to wait for the transcript (default 600; 0 = start and return)")
    ptc.add_argument("--output", default=None, help="Save the finished transcript JSON here")

    pi = add("import_media", help="Import media files into the project root or a bin")
    pi.add_argument("--path", action="append", required=True, help="File path (repeatable)")
    pi.add_argument("--bin", default=None, help="Top-level bin name (created if missing)")

    pm = add("add_marker", help="Add a sequence marker")
    pm.add_argument("--at", type=float, required=True)
    pm.add_argument("--name", required=True)
    pm.add_argument("--comment", default="")
    pm.add_argument("--duration", type=float, default=0.0)
    pm.add_argument("--type", default="Comment", choices=["Comment", "Chapter", "Segmentation", "WebLink"])
    pm.add_argument("--color", default=None, help=f"{', '.join(MARKER_COLORS)} or 0–7")
    pm.add_argument("--label", default=None)

    pc = add("insert_clip", help="Place a clip (or a source range of it) on the timeline")
    pc.add_argument("--item", required=True, help='Project item name, or "<bin path>/<name>" when names repeat')
    pc.add_argument("--expect-seq", **expect_seq)
    pc.add_argument("--at", type=float, default=0.0, help="Sequence time in seconds (default 0)")
    pc.add_argument("--vtrack", type=int, default=0, help="Video track, 0-based (0 = V1)")
    pc.add_argument("--atrack", type=int, default=0, help="Audio track, 0-based (0 = A1)")
    pc.add_argument("--mode", choices=["overwrite", "insert"], default="overwrite")
    pc.add_argument("--in", dest="in_s", type=float, default=None, help="Source in point, seconds")
    pc.add_argument("--out", dest="out_s", type=float, default=None, help="Source out point, seconds")
    pc.add_argument("--label", default=None, help="Undo history label")

    ps = add("set_scale", help="Set Motion > Scale on clips of one video track")
    ps.add_argument("--track", required=True, help="Video track, e.g. V3")
    ps.add_argument("--at", type=float, default=None, help="Target the clip under this sequence time")
    ps.add_argument("--from", dest="from_s", type=float, default=None)
    ps.add_argument("--to", dest="to_s", type=float, default=None)
    g = ps.add_mutually_exclusive_group(required=True)
    g.add_argument("--value", type=float, help="Scale percent, e.g. 50")
    g.add_argument("--fit", action="store_true", help="Fit inside the frame (letterbox), per clip")
    g.add_argument("--fill", action="store_true", help="Fill the frame (crop), per clip")
    ps.add_argument("--dry-run", action="store_true")
    ps.add_argument("--label", default=None)
    ps.add_argument("--expect-seq", **expect_seq)

    pfp = add("set_fps", help="Interpret footage: override project items' frame rate")
    pfp.add_argument("--item", action="append", required=True,
                     help='Project item name or "<bin path>/<name>" (repeatable; each must name one item)')
    pfp.add_argument("--fps", type=float, required=True)
    pfp.add_argument("--dry-run", action="store_true", help="List where the items are used in the active sequence")
    pfp.add_argument("--label", default=None)

    pr = add("remove_items", help="Remove clips from tracks within a time range")
    pr.add_argument("--track", action="append", required=True, help="V3, A2, … (repeatable)")
    pr.add_argument("--from", dest="from_s", type=float, required=True)
    pr.add_argument("--to", dest="to_s", type=float, required=True)
    pr.add_argument("--mode", choices=["inside", "overlap"], default="inside",
                    help="inside (default): only clips entirely in the range")
    pr.add_argument("--ripple", action="store_true", help="Close the gap (moves later clips; needs --confirm-ripple)")
    pr.add_argument("--confirm-ripple", action="store_true", help="Confirm a real --ripple run")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--label", default=None)
    pr.add_argument("--expect-seq", **expect_seq)

    pp = add("apply_plan", help="Execute a JSON edit plan (insert_clip / note commands)")
    pp.add_argument("plan", help="Plan JSON file")
    pp.add_argument("--dry-run", action="store_true", help="Resolve and check everything, change nothing")
    pp.add_argument("--media-root", default=None, help="Override the plan's media_root")
    pp.add_argument("--bin", default=None, help="Bin for imported media (default: the plan's sequence name)")
    pp.add_argument("--no-bin", action="store_true", help="Import into the project root")
    pp.add_argument("--only", default=None, help="Comma-separated command ids to run")
    pp.add_argument("--overwrite", action="store_true", help="Place even where the target track is occupied")
    pp.add_argument("--any-sequence", action="store_true",
                    help="Run even though the plan names a different sequence than the active one")
    pp.add_argument("--notes-as-markers", action="store_true",
                    help="Turn note commands and src-less graphics into sequence markers")
    pp.add_argument("--report", default=None, help="Also write the JSON report to this file")

    prc = add("replace_clip", help="Replace the clip under a time with another item, keeping effects + transitions")
    prc.add_argument("--track", required=True, help="Video track, e.g. V5")
    prc.add_argument("--at", type=float, required=True, help="Any sequence time inside the clip to replace")
    prc.add_argument("--with", dest="with_item", required=True,
                     help='Project item to put there, or "<bin path>/<name>" when names repeat')
    prc.add_argument("--in", dest="in_s", type=float, default=None, help="Source in (default: the old clip's)")
    prc.add_argument("--out", dest="out_s", type=float, default=None, help="Source out (default: in + old length)")
    prc.add_argument("--atrack", default=None,
                     help="Audio track for the new item's audio (default: the highest track free over the clip; "
                          "refused when none is free)")
    prc.add_argument("--expect-seq", **expect_seq)
    prc.add_argument("--no-effects", action="store_true", help="Don't carry effect params over")
    prc.add_argument("--no-transitions", action="store_true", help="Don't carry transitions over")
    prc.add_argument("--drop-outside-keyframes", action="store_true",
                     help="Leave out keyframes that would land outside the new source range (default: refuse)")
    prc.add_argument("--skip-effect", dest="skip_effects", action="append", default=None, metavar="MATCH_NAME",
                     help='Leave this component behind (repeatable), e.g. an ungraded "AE.ADBE CompoundComponent" '
                          "the new clip cannot get (default: refuse)")
    prc.add_argument("--dry-run", action="store_true")
    prc.add_argument("--label", default=None)

    psp = add("set_params", help="Set effect params / keyframes on the clip under a time, from a JSON file")
    psp.add_argument("--track", required=True,
                     help="V3, or A1 (audio: params of components the clip already has, e.g. its Volume)")
    psp.add_argument("--at", type=float, required=True, help="Any sequence time inside the clip")
    psp.add_argument("--json", dest="json_file", required=True,
                     help='A components list or {"components": [...]}; keyframes need "timeBase": '
                          "media | clip | sequence")
    psp.add_argument("--label", default=None)
    psp.add_argument("--expect-seq", **expect_seq)

    psv = add("set_volume", help="Clip volume in dB on the audio clip under a time, or every clip in a range")
    psv.add_argument("--track", required=True, help="Audio track, e.g. A1")
    psv.add_argument("--db", type=float, required=True,
                     help="Volume > Level as Premiere shows it: 0 = as recorded, -6 = half, at most +15")
    psv.add_argument("--at", type=float, default=None, help="Any sequence time inside the clip")
    psv.add_argument("--from", dest="from_s", type=float, default=None)
    psv.add_argument("--to", dest="to_s", type=float, default=None)
    psv.add_argument("--dry-run", action="store_true")
    psv.add_argument("--label", default=None)
    psv.add_argument("--expect-seq", **expect_seq)

    pat = add("add_transition", help="Add a video transition at the start or end of the clip under a time")
    pat.add_argument("--track", required=True, help="Video track, e.g. V3")
    pat.add_argument("--at", type=float, required=True, help="Any sequence time inside the clip")
    pat.add_argument("--match-name", required=True, help="Transition match name (see list_transition_names)")
    pat.add_argument("--duration", type=float, required=True, help="Seconds (snapped to whole frames)")
    pat.add_argument("--edge", required=True, choices=["start", "end"])
    pat.add_argument("--double-sided", action="store_true", help="Centre the transition on the cut")
    pat.add_argument("--label", default=None)
    pat.add_argument("--expect-seq", **expect_seq)

    psp = add("split_clip", help="Split the clip under a time without a razor (clone + trim; keeps clip attributes)")
    psp.add_argument("--track", required=True, help="V1, A2, …")
    psp.add_argument("--at", type=float, required=True, help="Sequence time of the split (snapped to the frame grid)")
    psp.add_argument("--dry-run", action="store_true")
    psp.add_argument("--label", default=None)
    psp.add_argument("--expect-seq", **expect_seq)

    pcr = add("cut_range", help="Remove a time range from every track and close the gap on all of them in sync")
    pcr.add_argument("--from", dest="from_s", type=float, required=True)
    pcr.add_argument("--to", dest="to_s", type=float, required=True)
    pcr.add_argument("--track", action="append", default=None, help="Only these tracks (repeatable; default all)")
    pcr.add_argument("--no-ripple", action="store_true", help="Leave the gap (do not move later clips)")
    pcr.add_argument("--captions", default=None, help="Caption track (e.g. V3) to conform afterwards (needs --srt)")
    pcr.add_argument("--srt", default=None, help="SRT of the caption lines (times = the caption movie's time)")
    pcr.add_argument("--srt-offset", type=float, default=0.0, help="Seconds to add to SRT times (default 0)")
    pcr.add_argument("--dry-run", action="store_true")
    pcr.add_argument("--label", default=None)
    pcr.add_argument("--expect-seq", **expect_seq)

    pec = add("extend_cut", help="Open up a cut (breathing room) with more of the clips' own source; keeps sync")
    pec.add_argument("--at", type=float, required=True, help="The cut point (sequence s)")
    pec.add_argument("--out", dest="out_by", type=float, default=0.0,
                     help="Seconds more of the outgoing clip (after its out point)")
    pec.add_argument("--in", dest="in_by", type=float, default=0.0,
                     help="Seconds more of the incoming clip (before its in point)")
    pec.add_argument("--track", action="append", default=None, help="Only these tracks move (repeatable; default all)")
    pec.add_argument("--extend", action="append", default=None,
                     help="Only these tracks get longer clips (repeatable; default every track with an edge there)")
    pec.add_argument("--captions", default=None, help="Caption track (e.g. V3) to conform afterwards (needs --srt)")
    pec.add_argument("--srt", default=None, help="SRT of the caption lines (times = the caption movie's time)")
    pec.add_argument("--srt-offset", type=float, default=0.0, help="Seconds to add to SRT times (default 0)")
    pec.add_argument("--allow-silent", action="store_true",
                     help="Extend even where a keyframed fade holds the added source at silence or 0 opacity")
    pec.add_argument("--dry-run", action="store_true")
    pec.add_argument("--label", default=None)
    pec.add_argument("--expect-seq", **expect_seq)

    psn = add("snap_edges", help="Put clip edges that drifted off the frame grid back on it (lint OFF_GRID)")
    psn.add_argument("--track", action="append", default=None, help="Only these tracks (repeatable; default all)")
    psn.add_argument("--dry-run", action="store_true")
    psn.add_argument("--label", default=None)
    psn.add_argument("--expect-seq", **expect_seq)

    pcc = add("conform_captions", help="Trim caption-movie pieces so no line flashes in or gets cut at a cut point")
    pcc.add_argument("--track", required=True, help="Caption track, e.g. V3")
    pcc.add_argument("--srt", required=True, help="SRT of the caption lines (times = the caption movie's time)")
    pcc.add_argument("--offset", type=float, default=0.0, help="Seconds to add to SRT times (default 0)")
    pcc.add_argument("--dry-run", action="store_true")
    pcc.add_argument("--label", default=None)
    pcc.add_argument("--expect-seq", **expect_seq)

    prv = add("review_cuts", help="Listening report: breathing room, clipped words, level jumps and clicks at every cut")
    prv.add_argument("--track", action="append", default=None, help="Dialogue track(s) to listen to (default A1)")
    prv.add_argument("--pre-roll", type=float, default=0.25, help="Quiet wanted before speech after a cut (s)")
    prv.add_argument("--post-roll", type=float, default=0.35, help="Quiet wanted after speech before a cut (s)")
    prv.add_argument("--words", default=None, help="Transcript JSON of the NEW mix, for word-level checks")
    prv.add_argument("--render", action="store_true", help="Analyse Premiere's own render (all tracks, volume applied)")
    prv.add_argument("--preset", default=None, help="Encoder preset for --render (default: Premiere's WAV preset)")
    prv.add_argument("--sliver", type=float, default=0.5, help="Shortest acceptable piece between cuts (s)")
    prv.add_argument("--out-dir", default=None, help="Where the WAVs go (default: a temp folder)")
    prv.add_argument("--output", default=None, help="Also write the report to this JSON file")

    plt = add("lint_timeline", help="Slivers, cuts too close, gaps/overlaps, A/V cut mismatches on the active sequence")
    plt.add_argument("--sliver", type=float, default=0.5)
    plt.add_argument("--min-gap-frames", type=int, default=1)

    pea = add("export_audio", help="The sequence audio as a WAV: shadow mix of the source files, or Premiere's render")
    pea.add_argument("--output", required=True)
    pea.add_argument("--track", action="append", default=None, help="Audio track(s) for the shadow mix (default A1)")
    pea.add_argument("--render", action="store_true", help="Render Premiere's own mix (export_sequence)")
    pea.add_argument("--preset", default=None)

    return parser


def run(action: str, kwargs: dict, rpc) -> dict:
    """Run one command; returns the reply object the CLI prints."""
    try:
        data = getattr(ops, action)(rpc, **kwargs)
        reply = {"ok": True, "data": data}
        if action == "apply_plan" and isinstance(data, dict):
            reply["ok"] = bool(data.get("ok", True))
        elif action == "review_cuts" and isinstance(data, dict):
            reply["ok"] = bool(data.get("clean", True))  # exit 1 when something was flagged
        elif action == "lint_timeline" and isinstance(data, dict):
            reply["ok"] = not any(i.get("severity") == "error" for i in data.get("issues") or [])
    except BridgeError as e:
        reply = e.to_reply()
    except FileNotFoundError as e:
        reply = {"ok": False, "error": str(e), "code": "FILE_NOT_FOUND", "outcome": "not_committed"}
    except OSError as e:
        reply = {"ok": False, "error": str(e), "code": "IO_ERROR", "outcome": "not_committed"}
    except ValueError as e:
        reply = {"ok": False, "error": str(e), "code": "BAD_PARAMS", "outcome": "not_committed"}
    return reply


def main(argv=None, *, rpc=None, probe_media=None) -> int:
    """argv None = run as a program (reads sys.argv, switches stdout/stderr to UTF-8); a list = called in-process
    (stdio left alone). `rpc` / `probe_media` replace the defaults (the 0.3.3 shim passes its module's)."""
    if argv is None:
        utf8_stdio()
    args = build_parser().parse_args(argv)
    kwargs = {k: v for k, v in vars(args).items() if k != "action"}
    if probe_media is not None and "probe_media" in inspect.signature(getattr(ops, args.action)).parameters:
        kwargs["probe_media"] = probe_media
    reply = run(args.action, kwargs, rpc or transport.rpc)
    print(json.dumps(reply, indent=2, ensure_ascii=False))
    return 0 if reply.get("ok") else 1
