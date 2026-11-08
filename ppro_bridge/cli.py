"""
ppro — command-line client for ppro-agent-bridge.

This is the surface an AI agent (or a human) talks to. Always prints one JSON object to stdout; exit code 0 on
success, 1 on failure.

Usage (from the repo root: python -m ppro_bridge <command> …; from cli/: python ppro.py <command> …):
  ping [--probe]
  get_project | get_sequence | list_project_items | list_transition_names
  set_active_sequence --name "02 Police"     open another sequence for the next commands
  select_items  --track A1 --from 0 --to 60  select clips for Premiere's panels | select_items --clear
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
  subclip       --item interview.mp4 --in 12.5 --out 31 --name "Answer 3" [--bin "Selects"]
  relink        --item g01.mov --to <renders>/g01_v2.mov [--dry-run]
  add_marker    --at 9.0 --name "Fact check" --comment "..." --color red
  list_markers | remove_marker --name "Fact check" [--at 9.0] [--dry-run]
  list_sequences | clone_sequence [--name "Seq"] [--keep 5] [--dry-run] | save_project
  remove_items  --track V3 --from 35 --to 53 [--dry-run]
  list_presets  [--format h264]              encoder presets on disk (no panel needed)
  export_video  --preset "00 - Match Source - High bitrate" --output out.mp4 [--dry-run | --detach] | --job ID
  export_aaf    --output cut.aaf [--embed-audio] [--audio-format wav] [--detach] | --job ID   (click OK on the log)
  export_xml    --output cut.xml [--overwrite]   Final Cut Pro 7 XML, no dialog
  list_effects  [--kind audio] | add_audio_effect --track A1 --at 5 --name "DeNoise" [--dry-run]
  list_effect_presets [--search blur] [--file f.prfpset] [--locale zh_CN]   effect presets on disk
  apply_preset  --track V2 --at 4.0 --preset "PiP 25% LL" [--dry-run]
  apply_ops     --json ops.json --label "tidy" [--dry-run]   whitelisted small writes, one transaction
  query         --what clips|clip|markers|bins|sequence [--track V1] [--at s] [--from s --to s] [--bin path]

Requires: Python 3.10+, `pip install websockets`, broker running, Premiere open with the Agent Bridge panel
loaded. Optional: ffprobe on PATH (fit/fill scaling, audio detection), Pillow (contact sheets).
"""

import argparse
import json
import os
import sys

from . import ops, spec
from .console import utf8_stdio
from .errors import BridgeError
from .ops import MARKER_COLORS

__all__ = ["MARKER_COLORS", "build_parser", "main"]


def _columns() -> int:
    """shutil.get_terminal_size().columns without importing shutil (which loads bz2 and lzma, ~9 ms of a CLI call):
    argparse asks for it on every add_argument."""
    try:
        columns = int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        columns = 0
    if columns <= 0:
        try:
            columns = os.get_terminal_size(sys.__stdout__.fileno()).columns
        except (AttributeError, ValueError, OSError):
            columns = 0
    return columns or 80


class _Formatter(argparse.HelpFormatter):
    """argparse's HelpFormatter with the width it would pick itself (terminal columns - 2), from _columns()."""

    def __init__(self, prog, indent_increment=2, max_help_position=24, width=None, **kw):
        super().__init__(prog, indent_increment, max_help_position, _columns() - 2 if width is None else width, **kw)


class _Skipped:
    """Stands in for a subcommand's parser that build_parser(only=...) does not build."""

    def add_argument(self, *args, **kw):
        return None

    def add_mutually_exclusive_group(self, *args, **kw):
        return self


def build_parser(only: str | None = None) -> argparse.ArgumentParser:
    """The CLI's parser; only: build just that subcommand (main does, when argv names one: the other 55 cost ~10 ms
    per call), with the same usage, help and errors for it."""
    parser = argparse.ArgumentParser(prog="ppro", description="Agent Bridge CLI for Premiere Pro",
                                     formatter_class=_Formatter)
    sub = parser.add_subparsers(dest="action", required=True)

    def add(name, **kw):
        if only is not None and name != only:
            return _Skipped()
        return sub.add_parser(name, formatter_class=_Formatter, **kw)

    expect_seq = dict(dest="expect_seq", metavar="NAME", default=None,
                      help="Refuse unless this is the active sequence (guards against a UI switch)")

    pg = add("ping", help="Liveness check + version/protocol handshake")
    pg.add_argument("--probe", action="store_true", help="Re-run the panel's capability probe")
    add("get_project", help="Active project name / path / active sequence")
    add("get_sequence", help="Active sequence: frame size, fps, duration, tracks, player position")
    pas = add("set_active_sequence", help="Open another sequence in the Timeline (by name or guid) for the next commands")
    pas.add_argument("--name", default=None, help="Exact sequence name")
    pas.add_argument("--guid", default=None, help="Sequence guid (when two sequences share a name)")
    psl = add("select_items", help="Select clips in the Timeline for Premiere's own panels (e.g. Properties > "
                                   "Enhance Audio on every selected clip); read back")
    psl.add_argument("--track", action="append", default=None, help="V1, A1, … (repeatable)")
    psl.add_argument("--at", type=float, default=None, help="Any sequence time: the clip under it on each track")
    psl.add_argument("--from", dest="from_s", type=float, default=None)
    psl.add_argument("--to", dest="to_s", type=float, default=None)
    psl.add_argument("--mode", choices=["inside", "overlap"], default="inside",
                     help="With --from/--to: inside (default) = clips entirely in the range; overlap = touching it")
    psl.add_argument("--clear", action="store_true", help="Empty the selection instead")
    psl.add_argument("--dry-run", action="store_true", help="List the clips it would select, change nothing")
    psl.add_argument("--expect-seq", **expect_seq)

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
    pi.add_argument("--force", action="store_true",
                    help="Send every file as it is (default: audio-only FLAC/Opus/Vorbis becomes a WAV beside it, other codecs Premiere refuses — VP9, AV1, Opus — are refused)")

    pm = add("add_marker", help="Add a sequence marker")
    pm.add_argument("--at", type=float, required=True)
    pm.add_argument("--name", required=True)
    pm.add_argument("--comment", default="")
    pm.add_argument("--duration", type=float, default=0.0)
    pm.add_argument("--type", default="Comment", choices=["Comment", "Chapter", "Segmentation", "WebLink"])
    pm.add_argument("--color", default=None, help=f"{', '.join(MARKER_COLORS)} or 0–7")
    pm.add_argument("--label", default=None)

    add("list_markers", help="The active sequence's markers: guid, name, comments, start, duration, type, colour")
    prm = add("remove_marker", help="Remove one sequence marker by guid, or by name (+ --at when names repeat)")
    prm.add_argument("--guid", default=None, help="Marker guid (from list_markers)")
    prm.add_argument("--name", default=None, help="Exact marker name")
    prm.add_argument("--at", type=float, default=None,
                     help="The marker's start in sequence seconds (half a frame of tolerance), when names repeat")
    prm.add_argument("--dry-run", action="store_true", help="Name the marker that would go; change nothing")
    prm.add_argument("--label", default=None)
    prm.add_argument("--expect-seq", **expect_seq)
    add("list_sequences", help="Every sequence in the project: name, guid, active, bridge restore point")
    pcs = add("clone_sequence", help="Restore point: clone a sequence as \"<name> [restore date time]\", prune old ones")
    pcs.add_argument("--name", default=None, help="Sequence to clone (default: the active one)")
    pcs.add_argument("--guid", default=None, help="Sequence guid (when two sequences share a name)")
    pcs.add_argument("--as", dest="as_name", default=None,
                     help="A plain copy under this name instead of a restore point (not tagged, nothing pruned)")
    pcs.add_argument("--keep", type=int, default=5,
                     help="Restore points of that sequence to keep, the new one included (default 5); the oldest "
                          "bridge restore points beyond it are deleted")
    pcs.add_argument("--dry-run", action="store_true", help="Show the clone's name and what pruning would delete")
    pcs.add_argument("--label", default=None)
    add("save_project", help="Save the project and check that the project file's time stamp changed")

    pc = add("insert_clip", help="Place a clip (or a source range of it) on the timeline")
    pc.add_argument("--item", required=True, help='Project item name, or "<bin path>/<name>" when names repeat')
    pc.add_argument("--expect-seq", **expect_seq)
    pc.add_argument("--at", type=float, default=0.0, help="Sequence time in seconds (default 0)")
    pc.add_argument("--vtrack", type=int, default=0, help="Video track, 0-based (0 = V1)")
    pc.add_argument("--atrack", type=int, default=None, help="Audio track, 0-based (default 0 = A1; with --video-only "
                    "a free one)")
    pc.add_argument("--video-only", action="store_true",
                    help="The picture without its sound: the audio leg lands on a free audio track and is taken off")
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
    ps.add_argument("--mode", choices=["inside", "overlap"], default="inside",
                    help="With --from/--to: inside (default) = only clips entirely in the range; overlap = every clip "
                         "touching it")
    ps.add_argument("--dry-run", action="store_true")
    ps.add_argument("--label", default=None)
    ps.add_argument("--expect-seq", **expect_seq)

    pfp = add("set_fps", help="Interpret footage: override project items' frame rate")
    pfp.add_argument("--item", action="append", required=True,
                     help='Project item name or "<bin path>/<name>" (repeatable; each must name one item)')
    pfp.add_argument("--fps", type=float, required=True)
    pfp.add_argument("--dry-run", action="store_true", help="List where the items are used in the active sequence")
    pfp.add_argument("--label", default=None)

    psc = add("subclip", help="Make a subclip of a project item (a source range, snapped to its frames) in a bin")
    psc.add_argument("--item", required=True, help='Source project item, or "<bin path>/<name>" when names repeat')
    psc.add_argument("--in", dest="in_s", type=float, required=True, help="Source in point, seconds")
    psc.add_argument("--out", dest="out_s", type=float, required=True, help="Source out point, seconds")
    psc.add_argument("--name", required=True, help="Name of the new subclip")
    psc.add_argument("--bin", default=None, help="Bin path for it (default: the source item's bin; \"\" = root; a "
                     "missing top-level bin is created)")
    g = psc.add_mutually_exclusive_group()
    g.add_argument("--video-only", action="store_true", help="Take only the picture")
    g.add_argument("--audio-only", action="store_true", help="Take only the sound")
    psc.add_argument("--allow-duplicate-name", action="store_true",
                     help="Make it even when the bin already holds a clip of that name (default: refuse)")
    psc.add_argument("--dry-run", action="store_true", help="Check and snap everything, change nothing")
    psc.add_argument("--label", default=None)

    prl = add("relink", help="Point a project item at another media file (checked against the old media first)")
    prl.add_argument("--item", required=True, help='Project item, or "<bin path>/<name>" (must name exactly one)')
    prl.add_argument("--to", required=True, help="The new media file")
    prl.add_argument("--force", action="store_true",
                     help="Relink even when duration / frame rate / streams differ or cannot be compared")
    prl.add_argument("--dry-run", action="store_true", help="Compare and list the uses, change nothing")

    pr = add("remove_items", help="Remove clips from tracks within a time range")
    pr.add_argument("--track", action="append", required=True, help="V3, A2, … (repeatable)")
    pr.add_argument("--from", dest="from_s", type=float, required=True)
    pr.add_argument("--to", dest="to_s", type=float, required=True)
    pr.add_argument("--mode", choices=["inside", "overlap"], default="inside",
                    help="inside (default): only clips entirely in the range")
    pr.add_argument("--only-name", action="append", default=None,
                    help="Refuse unless every clip it would remove has this clip name (repeatable)")
    pr.add_argument("--ripple", action="store_true", help="Close the gap (moves later clips; needs --confirm-ripple)")
    pr.add_argument("--confirm-ripple", action="store_true", help="Confirm a real --ripple run")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--label", default=None)
    pr.add_argument("--expect-seq", **expect_seq)

    prp = add("remove_project_items", help="Remove bins / project items from the project (refused while in use)")
    prp.add_argument("--bin", action="append", default=None, help='Bin path, e.g. "PAB_TEST_06" or "B-roll/Old" '
                     "(repeatable; everything inside goes too)")
    prp.add_argument("--item", action="append", default=None, help='Project item: name or "<bin path>/<name>" '
                     "(repeatable)")
    prp.add_argument("--dry-run", action="store_true", help="List what would go and where it is used")
    prp.add_argument("--label", default=None)

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
    psp.add_argument("--dry-run", action="store_true", help="Check the whole request against the clip; change nothing")
    psp.add_argument("--label", default=None)
    psp.add_argument("--expect-seq", **expect_seq)

    psv = add("set_volume", help="Clip volume in dB on the audio clip under a time, or every clip in a range")
    psv.add_argument("--track", required=True, help="Audio track, e.g. A1")
    psv.add_argument("--db", type=float, required=True,
                     help="Volume > Level as Premiere shows it: 0 = as recorded, -6 = half, at most +15")
    psv.add_argument("--at", type=float, default=None, help="Any sequence time inside the clip")
    psv.add_argument("--from", dest="from_s", type=float, default=None)
    psv.add_argument("--to", dest="to_s", type=float, default=None)
    psv.add_argument("--mode", choices=["inside", "overlap"], default="inside",
                     help="With --from/--to: inside (default) = only clips entirely in the range; overlap = every clip "
                          "touching it")
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

    prt = add("remove_transition", help="Remove the video transition at the start or end of the clip under a time")
    prt.add_argument("--track", required=True, help="Video track, e.g. V3")
    prt.add_argument("--at", type=float, required=True, help="Any sequence time inside the clip")
    prt.add_argument("--edge", required=True, choices=["start", "end"])
    prt.add_argument("--dry-run", action="store_true", help="Name the transition that would go (and whose it is)")
    prt.add_argument("--label", default=None)
    prt.add_argument("--expect-seq", **expect_seq)

    pre = add("remove_effect", help="Remove one added effect (filter) from the clip under a time")
    pre.add_argument("--track", required=True, help="V3, or A1 (audio effects)")
    pre.add_argument("--at", type=float, required=True, help="Any sequence time inside the clip")
    pre.add_argument("--match-name", required=True, help="Component match name (inspect_items lists them)")
    pre.add_argument("--occurrence", type=int, default=1, help="n-th instance of that match name in the chain (default 1)")
    pre.add_argument("--dry-run", action="store_true")
    pre.add_argument("--label", default=None)
    pre.add_argument("--expect-seq", **expect_seq)

    prk = add("remove_keyframes", help="Remove keyframes of one effect param: a time range, or all (animation off)")
    prk.add_argument("--track", required=True, help="V3, or A1 (e.g. Volume > Level)")
    prk.add_argument("--at", type=float, required=True, help="Any sequence time inside the clip")
    prk.add_argument("--match-name", required=True, help="Component match name (inspect_items lists them)")
    prk.add_argument("--occurrence", type=int, default=1, help="n-th instance of that match name in the chain (default 1)")
    prk.add_argument("--param", type=int, required=True, help="The param's index in that component (inspect_items)")
    prk.add_argument("--all", dest="all_keys", action="store_true", help="Every keyframe (switches animation off)")
    prk.add_argument("--from", dest="from_s", type=float, default=None, help="Range start (inclusive), in --time-base")
    prk.add_argument("--to", dest="to_s", type=float, default=None, help="Range end (inclusive), in --time-base")
    prk.add_argument("--time-base", choices=["media", "clip", "sequence"], default=None,
                     help="Seconds of the media (what inspect_items reports), of the clip, or of the sequence")
    prk.add_argument("--keep-value", action="store_true",
                     help="When every keyframe goes: keep the value the param had at the middle of the clip")
    prk.add_argument("--dry-run", action="store_true")
    prk.add_argument("--label", default=None)
    prk.add_argument("--expect-seq", **expect_seq)

    def render_check(p):
        p.add_argument("--render-check", action="store_true",
                       help="Afterwards render the sequence (audio) and a frame per video piece, and check that every "
                            "piece the edit made, trimmed or moved plays (exit 1 with RENDER_CHECK_FAILED if not)")
        p.add_argument("--render-dir", default=None, help="Where --render-check writes its WAV and frames (default: "
                                                          "a temp folder)")
        p.add_argument("--preset", default=None, help="WAV preset for --render-check (default: Premiere's own)")

    psp = add("split_clip", help="Split the clip under a time without a razor (clone + trim; keeps clip attributes)")
    psp.add_argument("--track", required=True, help="V1, A2, …")
    psp.add_argument("--at", type=float, required=True, help="Sequence time of the split (snapped to the frame grid)")
    psp.add_argument("--dry-run", action="store_true")
    psp.add_argument("--label", default=None)
    psp.add_argument("--expect-seq", **expect_seq)
    render_check(psp)

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
    render_check(pcr)

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
    render_check(pec)

    psh = add("shift_items", help="Move everything at/after a time on all tracks (or --track ones) by whole frames")
    psh.add_argument("--from", dest="from_s", type=float, required=True, help="Clips starting at/after this move")
    psh.add_argument("--by", type=float, required=True, help="Seconds to move (negative: left)")
    psh.add_argument("--track", action="append", default=None, help="Only these tracks (repeatable; default all)")
    psh.add_argument("--dry-run", action="store_true")
    psh.add_argument("--label", default=None)
    psh.add_argument("--expect-seq", **expect_seq)

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

    plp = add("list_presets", help="Encoder presets (.epr) on disk: Premiere's own and yours, with their format")
    plp.add_argument("--format", default=None, help="Only this format, e.g. h264, hevc, quicktime, wav, mxf")

    pev = add("export_video", help="Render the active sequence with an encoder preset and check the file (ffprobe)")
    pev.add_argument("--preset", default=None,
                     help='Preset name from list_presets (e.g. "00 - Match Source - High bitrate") or an .epr path '
                          "(required unless --job)")
    pev.add_argument("--output", default=None,
                     help="Output file; its extension must fit the format (.mp4 for H.264) (required unless --job)")
    pev.add_argument("--format", default=None, help="Narrow a preset name to this format (h264, quicktime, …)")
    pev.add_argument("--overwrite", action="store_true", help="Replace an existing output file")
    pev.add_argument("--dry-run", action="store_true", help="Resolve the preset and check the paths; render nothing")
    pev.add_argument("--job", default=None, metavar="ID",
                     help="Wait for the render job ID (from --detach or EXPORT_STALLED) and check its file")
    pev.add_argument("--detach", action="store_true",
                     help="Return the render job at once (with --job: one look at it) instead of waiting")
    pev.add_argument("--stall-seconds", type=float, default=180.0,
                     help="Give up waiting (EXPORT_STALLED) after this long with no progress, no panel event and no "
                          "growth of the output file (default 180; there is no other time limit)")

    paa = add("export_aaf", help="Export the active sequence as an AAF (a job; Premiere may hold it with its modal "
                                 "AAF export log until someone clicks OK) and check the file")
    paa.add_argument("--output", default=None, help="Output file ending in .aaf (required unless --job)")
    paa.add_argument("--embed-audio", dest="embed_audio", action=argparse.BooleanOptionalAction, default=None,
                     help="Audio inside the AAF (--no-embed-audio: beside it); omitted: Premiere's default")
    paa.add_argument("--audio-format", choices=["wav", "aiff"], default=None)
    paa.add_argument("--sample-rate", type=int, default=None, help="Hz, e.g. 48000")
    paa.add_argument("--bits-per-sample", type=int, choices=[16, 24], default=None)
    paa.add_argument("--handle-frames", type=int, default=None, help="Handle frames around each clip")
    paa.add_argument("--mixdown-video", action=argparse.BooleanOptionalAction, default=None)
    paa.add_argument("--render-audio-effects", action=argparse.BooleanOptionalAction, default=None)
    paa.add_argument("--trim-sources", action=argparse.BooleanOptionalAction, default=None)
    paa.add_argument("--overwrite", action="store_true", help="Replace an existing output file")
    paa.add_argument("--dry-run", action="store_true", help="Check the path and the panel; export nothing")
    paa.add_argument("--job", default=None, metavar="ID",
                     help="Wait for the AAF job ID (from --detach or EXPORT_STALLED) and check its file")
    paa.add_argument("--detach", action="store_true",
                     help="Return the job at once (with --job: one look at it) instead of waiting")
    paa.add_argument("--stall-seconds", type=float, default=180.0,
                     help="Give up waiting (EXPORT_STALLED) after this long with no state change and no new bytes "
                          "in the .aaf (default 180; there is no other time limit)")

    pax = add("export_xml", help="Export the active sequence as Final Cut Pro 7 XML (no dialog) and check the file")
    pax.add_argument("--output", required=True, help="Output file ending in .xml")
    pax.add_argument("--overwrite", action="store_true", help="Replace an existing output file")

    ple = add("list_effects", help="Effects Premiere can add: video filters (match + display name), audio filters")
    ple.add_argument("--kind", choices=["audio", "video"], default=None)

    pae = add("add_audio_effect", help="Add an audio effect by its (localised) display name to the clip under a time")
    pae.add_argument("--track", required=True, help="Audio track, e.g. A1")
    pae.add_argument("--at", type=float, default=None, help="Any sequence time inside the clip")
    pae.add_argument("--from", dest="from_s", type=float, default=None, help="With --to: every clip in this range")
    pae.add_argument("--to", dest="to_s", type=float, default=None)
    pae.add_argument("--mode", choices=["inside", "overlap"], default="inside",
                     help="With --from/--to: inside (default) only clips entirely in the range")
    pae.add_argument("--name", required=True,
                     help='Display name as list_effects shows it, in the UI language (e.g. "降噪"), or a match name')
    pae.add_argument("--dry-run", action="store_true")
    pae.add_argument("--label", default=None)
    pae.add_argument("--expect-seq", **expect_seq)

    plep = add("list_effect_presets", help="Effect presets (.prfpset) on disk: Premiere's own and yours, with their "
                                           "effects and whether apply_preset can replay them")
    plep.add_argument("--file", default=None, help="Only this .prfpset file")
    plep.add_argument("--search", default=None, help="Only presets whose name or bin path contains this text")
    plep.add_argument("--locale", default=None,
                      help="Which of Premiere's localised preset files, e.g. zh_CN (default: its UI language, "
                           "else en_US)")

    pap = add("apply_preset", help="Apply an effect preset (.prfpset) to the clip under a time or the clips in a range "
                                   "(through set_params); what UXP cannot write is skipped and reported")
    pap.add_argument("--track", required=True, help="Video track, e.g. V2")
    pap.add_argument("--at", type=float, default=None, help="Any sequence time inside the clip")
    pap.add_argument("--from", dest="from_s", type=float, default=None)
    pap.add_argument("--to", dest="to_s", type=float, default=None)
    pap.add_argument("--mode", choices=["inside", "overlap"], default="inside",
                     help="With --from/--to: inside (default) = clips entirely in the range; overlap = reaching "
                          "into it")
    pap.add_argument("--preset", required=True,
                     help='Preset name as list_effect_presets shows it, or "<bin path>/<name>" when names repeat')
    pap.add_argument("--file", default=None, help="Look the preset up in this .prfpset file only")
    pap.add_argument("--locale", default=None,
                     help="Which of Premiere's localised preset files (default: its UI language)")
    pap.add_argument("--dry-run", action="store_true", help="Show the set_params request per clip; change nothing")
    pap.add_argument("--label", default=None)
    pap.add_argument("--expect-seq", **expect_seq)

    pao = add("apply_ops", help="Several whitelisted small writes (markers, clip name / disabled, a static param "
                                "value, sequence in / out and playhead, track and bin names) in one transaction, "
                                "one undo step; each read back")
    pao.add_argument("--json", dest="ops", required=True,
                     help='A JSON file holding a list of ops ([{"op": "marker.add", "atSeconds": 2, "name": "x"}, ...] '
                          'or {"ops": [...]}), or that JSON inline. Ops: ' + ", ".join(ops.OP_NAMES))
    pao.add_argument("--dry-run", action="store_true", help="Resolve and check every op; change nothing")
    pao.add_argument("--label", default=None)
    pao.add_argument("--expect-seq", **expect_seq)

    pq = add("query", help="Whitelisted reads by name: clips, one clip's components, markers, bins, the sequence")
    pq.add_argument("--what", required=True, choices=list(ops.QUERY_NAMES))
    pq.add_argument("--track", action="append", default=None,
                    help="clips: V1, A1, … (repeatable; default every track); clip: exactly one")
    pq.add_argument("--at", type=float, default=None, help="clips / clip: a sequence time (the clip under it)")
    pq.add_argument("--from", dest="from_s", type=float, default=None, help="clips: clips touching [from, to]")
    pq.add_argument("--to", dest="to_s", type=float, default=None)
    pq.add_argument("--bin", default=None, help='bins: a bin path, e.g. "Selects/Day 1" (default: the root)')
    pq.add_argument("--max-keyframes", type=int, default=None, help="clip: keyframes listed per param (default 50)")

    for name, sp in sub.choices.items():
        if (spec.command(name) or {}).get("kind") == "write":
            sp.add_argument("--expect-fingerprint", default=None, metavar="FP",
                            help="Refuse the first write unless the timeline still has this fingerprint "
                                 "(get_sequence's), so nothing is written over an edit made in between")
    return parser


def run(action: str, kwargs: dict, rpc) -> dict:
    """Run one command; returns the reply object the CLI prints. kwargs["expect_fingerprint"], when set, wraps `rpc`
    in items.fingerprint_guard (checked before the first write, or after a command that wrote nothing)."""
    try:
        expected = kwargs.pop("expect_fingerprint", None)
        if expected:
            from . import items
            rpc = items.fingerprint_guard(rpc, expected)
        data = getattr(ops, action)(rpc, **kwargs)
        if expected:
            rpc.verify()
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


def _keywords(fn) -> tuple:
    """The parameter names of a plain function (what inspect.signature lists, without importing inspect)."""
    code = fn.__code__
    return code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]


def main(argv=None, *, rpc=None, probe_media=None) -> int:
    """argv None = run as a program (reads sys.argv, switches stdout/stderr to UTF-8); a list = called in-process
    (stdio left alone). `rpc` / `probe_media` replace the defaults (the 0.3.3 shim passes its module's)."""
    if argv is None:
        utf8_stdio()
    first = (sys.argv[1:] if argv is None else argv)[:1]
    args = build_parser(only=first[0] if first and first[0] in spec.command_names() else None).parse_args(argv)
    kwargs = {k: v for k, v in vars(args).items() if k != "action"}
    if probe_media is not None and "probe_media" in _keywords(getattr(ops, args.action)):
        kwargs["probe_media"] = probe_media
    if rpc is None:
        from . import transport  # here: a command that never reaches the panel does not need it
        rpc = transport.rpc
    reply = run(args.action, kwargs, rpc)
    print(json.dumps(reply, indent=2, ensure_ascii=False))
    return 0 if reply.get("ok") else 1
