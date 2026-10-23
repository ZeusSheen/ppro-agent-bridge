---
name: ppro-bridge
description: Drive Adobe Premiere Pro through the ppro-agent-bridge CLI for transcript-driven, closed-loop video editing. Use when asked to inspect a Premiere project or timeline, transcribe footage (local Whisper or Premiere Speech-to-Text), read or attach transcripts, export frames / contact sheets / OTIO, import media into bins, place clips or sentence-level cuts, execute a JSON edit plan (B-roll, graphics), scale clips to fit/fill, set effect params and keyframes, add transitions, swap clips while keeping their effects, interpret footage frame rates, add timeline markers, remove clips, or assemble a rough cut / best-take edit from talking-head footage. Requires the broker running and the Agent Bridge UXP panel loaded in Premiere.
---

# Premiere Pro Agent Bridge (v0.5.1)

Control a live Premiere Pro session via CLI. Architecture: `python ppro.py <action>`
→ local broker (127.0.0.1:8017) → UXP panel inside Premiere. Every command prints
one JSON object; exit code 0 = success.

Working directory for all commands: the `cli/` folder of this repository.
(`python -m ppro_bridge <action>` from the repository root is the same CLI: the code
lives in the `ppro_bridge/` package, and the scripts in `cli/` are thin wrappers.)

## Preconditions (check before anything else)

Run `python ppro.py ping`. If it fails: the bridge isn't running — ask the user to
double-click `start_bridge.bat` in the repository root (the broker with
`--load-panel`: it starts UXP Developer Tool minimized and loads the panel into
Premiere by itself, also after a Premiere restart; its window must stay open) — or
the panel is loaded but closed (Premiere: Window → UXP Plugins → Agent Bridge).
`python -m ppro_bridge.doctor` reports what this machine is missing (users set up
with `setup_bridge.bat`, a setup window). Report the failure instead of retrying
blindly. `transcribe.py` works without Premiere.
`AUTH_FAILED`: the broker refused the pairing token. The CLI sends it by itself
(from `%USERPROFILE%\.ppro-agent-bridge\token`, which the broker creates), so this
means a CLI older than the broker (0.4 sends none), a different Windows user, or a
`PPRO_BRIDGE_TOKEN` / `PPRO_BRIDGE_TOKEN_FILE` that differs between the two. Never
print or copy the token. The broker logs every request to
`%USERPROFILE%\.ppro-agent-bridge\audit\audit-YYYY-MM-DD.jsonl` (action, outcome,
queue/run ms, the sequence and track counts Premiere reported): read it when you
need to know what happened earlier in a session.

Read the ping reply:
- `"compatible": true` means CLI, broker and panel are the same version, the panel
  speaks protocol 2, and this Premiere build is not denied. Otherwise read
  `warning`: an older panel — or `"stale": true`, the loaded panel's code differs
  from this repository's `plugin/` — needs a Reload in UXP Developer Tool, an older
  broker needs `broker.py` restarted. Commands that need the current panel refuse an
  older one with `PANEL_OUTDATED` rather than editing through it.
- `"stuck"`: a request never finished and the panel refuses everything but ping —
  ask the user to save the project and Reload the panel.
- `"denied"`: this Premiere build is on the panel's denylist (`denied.reason`). The
  panel answers ping only; everything else fails with `HOST_DENIED`. Tell the user
  the reason; do not try to work around it.
- `capabilities`: what this Premiere build offers, each flag `true`, `false` or
  `"unknown"` (not probed yet; some flags need an open sequence). `ping --probe`
  probes again. An action whose capability is `false` fails with `UNSUPPORTED` —
  e.g. `transcribe_clip` needs `capabilities.transcribe` (Premiere 26.5+).

Then run `python ppro.py get_sequence` once: it gives frame size (vertical
sequences are common), fps, duration and every track's name and clip count.
Never assume 1920×1080 or a frame rate.

## Commands

| Command | Purpose |
|---|---|
| `python ppro.py ping [--probe]` | Liveness check + version/protocol handshake (`compatible`, `actions`, `capabilities`, `denied`); `--probe` re-checks capabilities |
| `python ppro.py get_project` | Active project name / path / guid / active sequence |
| `python ppro.py get_sequence` | Frame size, fps, duration, tracks (name, clip count), player position, guid |
| `python ppro.py list_items [--track V3] [--kind video\|audio]` | Clips on the timeline as JSON: start/end (sequence s), in/out (source s), project item, media path |
| `python ppro.py inspect_items [--track V5] [--from s --to s] [--output f.json]` | What an editor changed: each clip's effect chain (Motion, Opacity, filters, with `occurrence`) with param values, keyframes (media time); transitions are listed but read their names/positions from `export_otio` |
| `python ppro.py set_params --track V5 --at s --json f.json [--label "text"] [--expect-seq NAME]` | Effect params and keyframes on the clip under `--at` (see "Effect params and transitions"); on an audio track (`A1`) the params of components the clip already has |
| `python ppro.py set_volume --track A1 (--at s \| --from s --to s) --db -6 [--dry-run] [--label "text"] [--expect-seq NAME]` | Clip volume in dB as Premiere shows it (0 = as recorded, max +15), one clip or every clip in a range; verified |
| `python ppro.py add_transition --track V5 --at s --match-name NAME --duration s --edge start\|end [--double-sided] [--label "text"] [--expect-seq NAME]` | A video transition on the start or end of the clip under `--at`; refused where that edit point already has one (`TRANSITION_EXISTS`); read back from OTIO |
| `python ppro.py replace_clip --track V5 --at s --with "item" [--in s --out s] [--atrack A3] [--drop-outside-keyframes] [--skip-effect NAME] [--expect-seq NAME] [--dry-run]` | Premiere's "Replace With Clip", keeping effect params, keyframes, added filters and the clip's own transitions without touching a neighbour's; verifies by reading back |
| `python ppro.py list_transition_names` | Video transition match names (e.g. `AE.Impact_Film_Dissolve`) |
| `python ppro.py list_project_items` | Project panel clips with bin path and media path |
| `python ppro.py list_transcripts [--jobs]` | Every project clip + hasTranscript flag, plus the Speech-to-Text jobs; `--jobs`: only the jobs |
| `python ppro.py export_transcript --output f.json [--item "bin/name"]` | Export a clip's Premiere transcript |
| `python transcribe.py <media file> --output f.json [--language en] [--model large-v3] [--digest]` | LOCAL Whisper transcription → Adobe-format JSON. GPU auto-detected; on CPU use `--model medium` |
| `python ppro.py transcribe_clip --item "name" [--language en-US] [--wait 600] [--output f.json]` | PREMIERE Speech-to-Text (26.5+) on one clip; waits for it and exports the transcript (`--wait 0`: start only) |
| `python transcript_digest.py f.json [--json] [--min-conf 0.8]` | Transcript → sentence list with timecodes |
| `python ppro.py import_transcript --item "name" --file f.json [--replace]` | Attach a transcript JSON to a clip in Premiere and verify it by reading it back (optional; digest works without it) |
| `python ppro.py export_otio --output f.otio` | Timeline structure as OpenTimelineIO (includes each clip's source media path) |
| `python ppro.py probe_api [--output dump.json] [--diff old.json] [--no-instances]` | This Premiere build's UXP API (every class, method and instance member) and a `watch` list of the calls the UXP port needs (`missing`, `unknown`); `--diff` shows what a Premiere update added or removed |
| `python ppro.py export_frame --dir d [--at sec] [--width W --height H]` | One frame of the program output (default: sequence frame size); real path in `resolvedPath` |
| `python ppro.py export_frames --dir d --at 4,18.5,31.6 [--sheet]` | Several frames at once, optionally a labelled contact sheet (needs Pillow) |
| `python ppro.py import_media --path f [--path f2 ...] [--bin "Name"]` | Import media into the root or a top-level bin (created if missing); returns `newItems` names |
| `python ppro.py insert_clip --item "name" [--at sec] [--vtrack N] [--atrack N] [--mode overwrite\|insert] [--in sec --out sec] [--expect-seq NAME] [--label "text"]` | Place a clip — with `--in/--out`, only that source range ("cut by sentence") |
| `python ppro.py apply_plan plan.json [--dry-run] [--only ids] [--notes-as-markers] [--overwrite] [--any-sequence] [--report r.json]` | Execute a JSON edit plan (see below) |
| `python ppro.py set_scale --track V3 (--at s \| --from s --to s) (--value % \| --fit \| --fill) [--expect-seq NAME] [--dry-run]` | Motion › Scale; fit/fill computed per clip from the media size (ffprobe) |
| `python ppro.py set_fps --item "name" [--item …] --fps 59.94 [--dry-run]` | Interpret footage: override frame rate (changes the item in every sequence) |
| `python ppro.py add_marker --at s --name "…" [--comment "…"] [--duration s] [--color red]` | Sequence marker (flags, notes for the editor) |
| `python ppro.py remove_items --track V3 [--track V4] --from s --to s [--mode inside\|overlap] [--ripple --confirm-ripple] [--expect-seq NAME] [--dry-run]` | Remove clips in a range (default: only clips entirely inside); `--ripple` closes the gap and reports what moved |
| `python ppro.py split_clip --track V1 --at s [--dry-run] [--label "text"] [--expect-seq NAME]` | Split the clip under `--at` without a razor; both halves keep every clip attribute |
| `python ppro.py cut_range --from s --to s [--track V1 ...] [--no-ripple] [--captions V3 --srt f.srt [--srt-offset s]] [--dry-run] [--label "text"] [--expect-seq NAME]` | Remove a time range from every track (or the `--track` ones) and close the gap on all of them in sync |
| `python ppro.py extend_cut --at s [--out s] [--in s] [--track V1 ...] [--extend V1 ...] [--captions V3 --srt f.srt] [--allow-silent] [--dry-run] [--label "text"] [--expect-seq NAME]` | Open up a cut (breathing room) with more of the clips' own source; everything after it moves right on all tracks in sync |
| `python ppro.py snap_edges [--track V1 ...] [--dry-run] [--label "text"] [--expect-seq NAME]` | Put clip edges that drifted microseconds off the frame grid back on it (lint `OFF_GRID`), so the picture cuts with the sound; verified to the microsecond |
| `python ppro.py conform_captions --track V3 --srt f.srt [--offset s] [--dry-run] [--label "text"] [--expect-seq NAME]` | Trim the pieces of a pre-rendered caption movie so no line flashes in or is cut off at a cut |
| `python ppro.py review_cuts [--track A1] [--render] [--words tr.json] [--pre-roll 0.25] [--post-roll 0.35] [--out-dir d] [--output r.json]` | Listening report: breathing room, clipped words, level jumps and clicks at every cut, plus the timeline lint; exit 1 unless clean |
| `python ppro.py lint_timeline [--sliver 0.5]` | Slivers, cuts too close, gaps, overlaps, V/A cut mismatches, clips past the end, edges off the frame grid; exit 1 on an error-severity issue |
| `python ppro.py export_audio --output f.wav [--track A1] [--render]` | The sequence's audio as a WAV: shadow mix of the source files, or Premiere's own render |

Track indices are 0-based (0 = V1) and must exist — the bridge never adds a track.
`--mode overwrite` (default) replaces what's under it; `insert` pushes content
rightward. `--in/--out` come as a pair, in SOURCE seconds. Always pass `--label`
describing the edit intent — it becomes the user's undo-history entry.

**Item names.** When two project items share a name and point at different media
(an EN and a CN render, say), commands refuse with `AMBIGUOUS` and list them;
name one as `"<bin path>/<name>"` (`"/name"` for the root bin), e.g.
`--with "CN/g01.mov"`. `set_fps` needs a unique item even when the media is the same.

**Failures.** A failed reply carries `code` and `outcome`:
- `not_committed` — nothing changed. Fix the cause and retry.
- `committed` — the write ran but did not achieve what was asked (e.g.
  `TRANSCRIPT_NOT_IMPORTED`). Read the message; do not simply repeat it.
- `partial` — a multi-step command stopped half-way; `data.completed` lists the
  steps that landed (e.g. `replace_clip` after the old clip was removed). Look at
  the range, then finish by hand or undo those steps.
- `unknown` (timeouts, disconnects, `PANEL_TIMEOUT`, `PANEL_STUCK`) — the change may
  still land: `list_items` the range before retrying, never repeat a write blindly.
  Read commands never report `unknown`.

`BRIDGE_BUSY` is safe to retry; `PANEL_OUTDATED` means Reload the panel; `BAD_PARAMS`
lists what was wrong with the request (the panel checks every request against its
action spec before running anything).

**Guards.** Pass `--expect-seq "<sequence name>"` on writes when the user may be
clicking around in Premiere; a different active sequence is refused
(`SEQUENCE_MISMATCH`). Underneath, every request can carry a guard that the panel
checks on the very project and sequence it is about to touch — project name, name
prefix or guid, sequence name or guid — and refuses with `PROJECT_MISMATCH` /
`SEQUENCE_MISMATCH` before changing anything. Python callers set it once:
`rpc = transport.make_rpc(guard={...})`, then `ppro_bridge.ops.<command>(rpc, ...)`
(one function per CLI command, same option names).

## Getting a transcript (Whisper-first)

Preferred path — run local Whisper yourself; do not wait for Premiere:
1. Find the source file path: from the user, or from `export_otio` (each clip
   records its media path), or `get_project` context.
2. `python transcribe.py "<path>" --output tr.json --language en` (use `zh` for
   Chinese; omit `--language` to auto-detect). First run downloads model weights.
   No words at all → `NO_SPEECH`, and nothing is written.
3. Whisper confidence values are REAL — `--min-conf 0.8` on the digest is a
   valid quality filter.

Premiere-native transcripts (via `export_transcript`) are also fine when they
already exist, but their confidence field is placeholder 1.0 — judge quality
from the text. A clip without a transcript, or with only Premiere's empty
placeholder, is refused with `NO_TRANSCRIPT`.

**Premiere Speech-to-Text** (`transcribe_clip`, Premiere 26.5+): use it when the
user wants Premiere's own transcript in the project (Text panel, captions).
- `python ppro.py transcribe_clip --item "interview.mp4" --language en-US --output tr.json`
  starts the job, polls every 5 s for up to `--wait` seconds (default 600) and
  exports the result. `--wait 0` returns the `jobId` at once: poll
  `list_transcripts --jobs` (`hasTranscript` is the truth), then `export_transcript`.
- `--language` takes Premiere's language codes (`en-US`, case-insensitive) — not
  the lowercase codes inside transcript JSON (`en-us`). Unknown → `BAD_PARAMS`.
  `LANGUAGE_UNAVAILABLE`: the language pack is not installed — ask the user to
  install it in Premiere (the bridge never triggers that prompt).
- Refused: sequences, clips that already have a transcript with words
  (`TRANSCRIPT_EXISTS`), clips with a job running (`TRANSCRIBE_RUNNING`, which also
  blocks `set_fps` and `import_transcript` on that clip).
- `TRANSCRIBE_TIMEOUT`: the job may still finish — poll `list_transcripts --jobs`.
  `TRANSCRIBE_LOST`: the panel was reloaded and forgot its jobs; check
  `hasTranscript` in `list_transcripts`. `TRANSCRIBE_FAILED`: Premiere refused or
  failed the job.
- Whisper stays the default: real confidences, no project change, works without Premiere.

**Attaching a transcript** (`import_transcript`): the file is checked against the
transcript schema first (`TRANSCRIPT_INVALID`, nothing sent). The panel imports
it, reads it back and reports `wordsIn` / `wordsOut`. A clip that already has words
is refused (`TRANSCRIPT_EXISTS`) unless `--replace` — use that only when the user
agrees. Import works on 27.1 (verified live); some older builds (26.x) import an
empty shell: the clip ends up without words and the reply is
`TRANSCRIPT_NOT_IMPORTED` (outcome `committed`). That is a Premiere bug, not the
file: tell the user and offer the manual route — import the JSON in Premiere's Text
panel (Transcript tab). The digest never needs the import.

## The three layers and their coordinate systems

1. **Semantic** — transcript digest: sentences with speaker/start/end in
   SOURCE-CLIP seconds.
2. **Structure** — OTIO export: which source file each timeline clip uses, its
   source range, its sequence position.
3. **Perception** — `export_frame` renders the program output (all tracks
   composited); `--at` is SEQUENCE seconds. `"black": true` (with a `warning`)
   means the PNG is all black: on a real job some phone clips exported black while
   the Program monitor showed them, so ask the user to look before acting on it.
   `exportSeconds` / `waitedSeconds` say where the time went.

Conversion: `sequence_time = clip_position + (source_time − source_range_start)`.
The two systems coincide only while a full clip sits at position 0 — never rely
on this after the first cut. One source sentence maps to multiple sequence
positions if the clip is used more than once: convert per clip instance via
OTIO, never a global lookup.

## Raw shoot footage: take selection (common case)

Talking-head raw footage (interviews, pieces-to-camera) contains direction
chatter and retakes. In the digest, expect and classify:
- **Direction/setup**: crew talk that is not part of the piece — framing and
  positioning instructions, readiness checks, count-ins, prompt/notes handling.
- **False starts / broken takes**: a passage that stops abruptly, followed by
  direction, followed by the SAME passage again.
- **Clean takes**: the repeated passage's complete, uninterrupted version —
  usually the LAST occurrence.

Rules: cut all direction chatter; for repeated passages keep exactly one take,
preferring the last complete one (earlier takes exist because they failed);
when unsure which take is cleaner, export a frame from each and compare, or ask.

## Transcript-driven rough cut: standard workflow

1. Obtain digest (Whisper-first, above). Read ALL sentences before deciding.
2. Make every content decision in text first: classify takes, pick sentences,
   order them, compute total runtime against the user's target.
3. Merge digest lines that are one spoken sentence split across rows (contiguous
   timestamps, text flows on) — cut at real sentence boundaries, not row breaks.
4. Place cuts one sentence-run at a time: `insert_clip --in <start> --out <end>
   --at <running position> --label "<short intent>"`. Leave breathing room: start
   ~0.25 s before the first word and end ~0.35 s after the last one, inside the
   source pause — never cut at the middle of a pause, and never into the next word
   (`review.pad_cut_points` computes these ranges from word timings; a short pause
   is shared, 60 % to the outgoing tail). Treat timestamps as ±0.2 s.
   To change an edit that already exists (and keep the editor's volume, scaling,
   effects), use `cut_range` / `extend_cut` instead of re-placing ranges — see
   "Cutting without a razor".
5. Listen: `review_cuts` (and `--render` once the mix matters). Fix every `SLIVER`
   in the lint, then the `CLIPPED`/`TIGHT_*` cuts the source allows (below). Then
   look sparsely: `export_frame --at` at 1–3 cut points (mid-grimace faces,
   clipped gestures), not every frame.
6. Report: chosen sentences with timecodes, skipped material and why, total
   runtime, what the listening report still flags, undo labels used.

## Listening to the edit (`review_cuts`, `lint_timeline`)

UXP cannot play or meter audio, so the bridge listens offline. `review_cuts`
builds the dialogue track (default `A1`; `--track` repeats) as a WAV and measures
every cut on it:
- **Shadow mix** (default): rebuilt from the source files with ffmpeg — fast, needs
  only ffmpeg, but clip/track volume, keyframes, audio effects and other tracks
  are NOT applied (the report's `notes` say so). It needs every clip's media file:
  items Premiere generates (Enhance Speech results, nested sequences) have no
  media path, so `review_cuts` and `export_audio` refuse with `NO_MEDIA` (listing
  the clips in `data.clips`) rather than measure silence — run them with `--render`.
- **`--render`**: Premiere renders its own mix (all tracks, volume applied) through
  `export_sequence` with Premiere's "Waveform Audio 48kHz 16-bit" preset, found
  next to the running Premiere (`--preset` or `PPRO_WAV_PRESET` override it), and the
  report adds `loudness` (integrated LUFS, LRA, true peak). A minute renders in a
  second or two. Cuts past the end of a short render are marked `unheard`.

Flags per cut (`details` says what was measured and what to do):
`TIGHT_OUT` (less than `--post-roll`, default 0.35 s, of quiet after the outgoing
words), `TIGHT_IN` (less than `--pre-roll`, 0.25 s, before the incoming words),
`CLIPPED` (speech within 30 ms of the cut: a word cut off), `LEVEL_JUMP` (room tone
jumps > 6 dB; `levelJumpUncertain` when a side has no real pause to measure),
`CLICK` (a waveform step at the cut). With `--words tr.json` — a transcript of the
report's own WAV (`transcribe.py` on it), so times are sequence seconds — it adds
`CUT_IN_WORD` and `LOW_CONF_WORD`. Program head/tail get only CLIPPED/CLICK.
`lint` (also standalone: `lint_timeline`) lists `SLIVER` (a piece shorter than
`--sliver`, 0.5 s — the "weird cut" an editor spots at once), `CUTS_TOO_CLOSE`,
`GAP`, `OVERLAP`, `AV_CUT_MISMATCH` (V1 and A1 cut a few frames apart),
`PAST_END` and `OFF_GRID` (clip edges a few µs off the frame grid: past a boundary,
the picture cuts a frame after the sound; older panels wrote them — Reload the panel,
then `snap_edges` puts every edge back on its frame). Sequence times the bridge
writes land on whole frames. Exit code 1 unless `clean` (no flags and no error-severity lint;
warnings such as a short caption gap are listed but do not count). Breaths count as
sound; picture is not heard — look at faces with `export_frames`.

**Fixing what it flags** (lessons from a real re-cut):
- Whisper word times drift by up to half a second, and the first word after a
  silence is smeared back into it. Decide where a pause really is from the audio
  energy (what `review_cuts` measures), and use the words only to know what is said.
- To know whether a flagged cut actually clips a word, transcribe the report's WAV
  (`transcribe.py review_shadow.wav`) and read the words around the cut: a fragment
  you can hear shows up as a stray word ("And" from a 19-frame sliver); a breath or
  a word ending does not.
- `TIGHT_*` with a real pause in the source → `extend_cut` into it. With no pause
  (the speaker runs sentences together) there is nothing to extend: leave it, or
  choose a different sentence boundary — say so in the report rather than padding.
- A pause you add exposes the edges next to it: a word that ended a few ms early,
  or an incoming clip that starts inside a soft word, was masked by the next word
  and is audible once silence follows. After every `extend_cut`, listen to both
  sides again (and read the mix transcript: a stray "And" is half a word).
- Outgoing words often end in a soft decay (an "l", a nasal) that runs past the
  frame where you can cut. Extend to the frame just before the next sound, then
  fade the last 2 frames: `set_params --track A1 --at <s>` with two Level keyframes
  (`timeBase: "sequence"`, clip end − 2 frames at the current level, clip end at 0).
  That removes the CLICK without cutting the word. The same fade softens the onset
  of a next word that runs on with no gap (end the clip at the original cut, fade
  2 frames) — trimming into run-on speech cuts the previous word instead.
- A fragment next to an SFX hit or a graphic beat: trim it on the dialogue only
  (`cut_range --track A1 --no-ripple`) so picture, SFX and graphics keep their
  timing; the pause it leaves is a lint warning, not an error, when music or SFX
  play under it.

## Cutting without a razor (`split_clip`, `cut_range`, `extend_cut`)

Premiere 27.1 has no razor over UXP, and re-placing source ranges loses what the
editor did to a clip (volume, scaling, effects). These commands build every piece
from the clip itself, so attributes survive:
- `split_clip --track V1 --at s` clones the clip into free timeline 10 s after the
  sequence end, trims the original's tail and the copy's head to the cut, and moves
  the copy into place: 4 undo steps. Video and audio are separate clips here —
  split each track (or use `cut_range`). At a clip edge it does nothing.
- `cut_range --from a --to b` removes `[a, b)` from every track (or the `--track`
  ones) and keeps them in sync: clips crossing both edges are split, clips crossing
  one edge are trimmed, clips inside are removed, then everything after `b` on
  those tracks moves left by `b − a` in ONE undo step (`shift_items`). Premiere's
  own ripple works per track and desyncs tracks with gaps; this does not. Times
  snap to the frame grid. With `--track` the other tracks stay put and `notMoved`
  lists what is now out of sync. Markers do not move (the report says so).
  `--no-ripple` leaves the gap.
- Both `--dry-run` first: they plan the exact steps against a model of the
  timeline and write nothing. A real run reads back every step and the whole
  timeline (`verified`); a failure after the first write is `partial`, with
  `data.completed` and, for a split, `data.parked` (where a parked copy sits).
- Speed-changed clips crossing a cut are refused (`UNSUPPORTED`).
- `extend_cut --at T --out 0.3` / `--in 0.25` is the opposite: breathing room from
  the clips' own source. Everything at/after T moves right by out + in in one
  `shift_items` step, then the clip ending at T gets a longer tail and the clip
  starting there a longer head (3 undo steps for one track pair). `--extend V1
  --extend A1` limits which tracks get longer clips: the others (graphics, captions,
  SFX cut in parallel) keep a short gap there, so nothing from cut material comes
  back; without `--extend`, a clip without enough source is left with a gap and
  listed in `gaps`. Clips crossing T (music under the cut) are untouched. Check the
  source first: extending into the next word is worse than a tight cut.
- Keyframes stay at their media time when a clip gets longer, so the added source
  holds the last (tail) or first (head) keyframe's value. `extend_cut` lists
  keyframed Level and Opacity on the extended clips under `heldKeyframes`, and refuses
  (`KEYFRAMED`, nothing changed) when one would hold the extension silent or at 0
  opacity — a 2-frame fade-out at the old end does exactly that. Move or delete
  those keyframes in Premiere first (the bridge has no command to remove keyframes yet), or pass
  `--allow-silent`. The shadow mix in `review_cuts` does not apply keyframes, so it
  cannot hear this; `--render` can.
- Leave the music out of `--track` when it runs under the cuts: `cut_range` would
  cut it at every edit and the music would jump.
- To tighten a cut, cut the extra pause out with `cut_range` (then `review_cuts`
  again).

## Pre-rendered captions (`conform_captions`)

A caption movie (one long render of all lines, cut into pieces on an upper track
as the A-roll is cut) shows the wrong line for a few frames when a cut lands just
before a line change. With the SRT the movie was rendered from:
- `conform_captions --track V3 --srt captions.srt` trims a piece that starts more
  than 2 frames into a line and would show less than 0.5 s of it to the next line's
  start (a line with longer to run is simply joined in progress), and a piece whose
  tail shows less than 0.5 s of a new line back to that line's start. A frame shows
  the line current at its start, so trims snap to the first frame of the new line.
  SRT times are the movie's own time (`--offset` adds seconds). `changes` lists each
  trim, `flagged` what a trim cannot fix.
- `cut_range` / `extend_cut --captions V3 --srt captions.srt` run it right after
  the edit.
- Captions burnt into the A-roll picture cannot be conformed — cut in pauses.

## Clip volume (`set_volume`)

`set_volume --track A1 --at s --db -6` sets the clip's Volume › Level as Premiere
shows it (0 dB = as recorded, max +15) on that clip, or on every clip in
`--from/--to` (one undo step each), and reads it back. UXP stores Level linearly:
`dB = 20·log10(level) + 15` (0 dB = 0.17782794). A keyframed Level is refused
(`KEYFRAMED`) before anything changes; edit keyframes with `set_params --track A1`
(component "Internal Volume Mono", or "Internal Volume Stereo" for stereo clips — both seen on 27.1; param
names follow Premiere's UI language, so address Level by index 1). Track volume,
pan, audio effects and loudness normalisation are not reachable from UXP: measure
with `review_cuts --render` and adjust clip volume.

## Edit plans: B-roll and graphics in one pass (`apply_plan`)

When the edit is decided up front (B-roll over A-roll, graphics on an upper
track), write it as a plan and execute it instead of placing clips one by one:

```json
{
  "sequence": "Main", "media_root": "./media/",
  "commands": [
    {"command": "insert_clip", "id": "b01", "track": "V3", "timeline_at": 2.0,
     "src": "stock/b01.mp4", "src_in": 9.0, "src_out": 14.0, "scale": "fill"},
    {"command": "insert_clip", "id": "g01", "track": "V4", "timeline_at": 9.2,
     "src": "graphics/g01.mov", "src_in": 0, "src_out": 5.0},
    {"command": "insert_clip", "id": "g02", "track": "V4", "timeline_at": 16.0, "src": null},
    {"command": "note", "id": "flag1", "timeline_at": 9.0, "note": "Check this claim"}
  ]
}
```

- `src` is relative to `media_root` (itself relative to the plan file). `src: null`
  means "built by hand" and is skipped; `note` commands are reported. With
  `--notes-as-markers` both become sequence markers the editor sees.
- Optional per command: `atrack` (e.g. "A4"; default: a free audio track, highest
  first — A1 dialogue is never the default), `scale` ("fit", "fill" or a percent).
- Always `--dry-run` first. It resolves media, matches/plans imports (into a bin
  named after the plan's `sequence`, or `--bin`), snaps both edges of every clip
  to the sequence frame grid, and reports `conflict` wherever the target track is
  already occupied. Nothing is written in a dry run.
- A real run refuses a plan whose `sequence` is not the active sequence
  (`--any-sequence` overrides) and stops — marking the rest `aborted` — if the active
  project or sequence changes mid-run, or a step's outcome is unknown.
- It imports, places each clip (undo label `plan <sequence>: <id>`), applies scale,
  then re-reads the timeline and marks each step `placed` only if the video AND the
  audio leg are really there, to half a frame. Exit code 1 if anything is
  `conflict`/`error`/`failed`/`unverified`/`aborted`.
- A range covering the whole file (`src_in` 0, `src_out` = its length) is placed as
  the whole clip (`placedWhole`), because an out point at the very end loses a frame;
  if the whole clip could run into the next clip, the range is placed instead and
  the step says `endClampRisk`.
- Re-running a plan on a timeline that already has it reports every step as a
  conflict — use `remove_items` on the range (or `--overwrite`) deliberately.

## Finishing tools

- **Scale**: `insert_clip` places media at 1:1 pixels, so a 4K clip in a 1080-wide
  sequence shows as a 2× crop. Check with `export_frames`, then
  `set_scale --track V3 --at <s> --fill` (or `--fit`, or `--value 50`). Keyframed
  Scale is refused rather than overwritten.
- **Frame rate**: `set_fps --item g01.mov --fps 59.94` makes a 60 fps render play
  frame-accurately in a 59.94 sequence.
- **Markers**: put fact-check flags and open questions on the timeline with
  `add_marker` rather than only in chat; colours: green, red, purple, orange,
  yellow, white, blue, cyan. The bridge has no command to remove markers yet (UXP
  has the call; `tests/live/api_smoke.py` checks it): ask the user.
- **Removal**: `remove_items --dry-run` first; the default mode only removes clips
  entirely inside the range, so neighbours are never trimmed by accident. `--ripple`
  moves later clips (graphics, music) along: its dry run lists `mayShift`, a real
  run needs `--confirm-ripple` and reports `shifted`.

## Effect params and transitions (`set_params`, `add_transition`)

`inspect_items` shows each clip's effect chain: components by `matchName` and
`occurrence` (the n-th instance of that match name, in chain order), params by
`index` with their values, and keyframes in media time (`timeBase: "media"`).
`set_params --track V3 --at <s> --json fx.json` writes to the one clip under `--at`;
the file is a list of components (or `{"components": [...]}`):

```json
[{"matchName": "AE.ADBE Opacity", "params": [
   {"index": 0, "timeBase": "clip", "keyframes": [{"t": 0.0, "value": 0}, {"t": 0.5, "value": 100}]}]},
 {"matchName": "AE.ADBE Gaussian Blur 2", "occurrence": 1, "params": [{"index": 0, "value": 12}]}]
```

- `timeBase` is required whenever `keyframes` is given: `media` = source-media
  seconds (what `inspect_items` reports), `clip` = seconds from the clip's first
  frame on the timeline, `sequence` = timeline seconds. Keyframes win over `value`.
- A component that is not on the clip yet is appended (occurrence 2 on a clip
  without the effect creates two instances) and listed in `createdEffects`.
- The whole request is validated before the first change: keyframes outside the
  clip (half a frame of tolerance) → `KEYFRAME_OUT_OF_RANGE`; a param that is already
  keyframed refuses a static value or new keyframes (`KEYFRAMED`); `clip`/`sequence`
  times on a speed-changed clip → `UNSUPPORTED` (give `media` times).
- The reply lists the params `set` and each keyframe's `mediaSeconds`; check the
  result with `inspect_items` (one undo step per call).

`add_transition --track V3 --at <s inside the clip> --match-name "<name>" --duration 0.5
--edge start|end` adds a video transition at that edge, single-sided unless
`--double-sided` (centred on the cut). Names come from `list_transition_names`; the
duration is snapped to whole frames, so Premiere's tick rounding cannot drop one.
An edit point holds one transition, and Premiere keeps the one already there: where
the cut has one — the neighbour's included (the clip before it fading out) — the
command refuses with `TRANSITION_EXISTS` and writes nothing (`data.existing`). The
bridge has no command to delete transitions yet: ask the user to delete it in Premiere first. The
result is read back from OTIO (`verified`, `transition` with seconds `before`/`after`
the cut); nothing there afterwards is `VERIFY_FAILED` (outcome `committed`).

## Swapping clips without losing the editor's work (`replace_clip`)

Language versions, re-rendered graphics, new takes: never delete-and-re-place an
edited clip by hand. First `inspect_items` (and `export_otio` for transitions) to
see what was done to it, then `replace_clip --dry-run`, then for real. It carries
over the source range, Motion/Opacity params and keyframes, added filters
(re-created by match name and occurrence) and the clip's transitions, then re-reads
everything and reports `verified` only if placement is within a frame and every
param, keyframe and transition matches. The new item's audio goes to the highest audio
track that is free over the clip (`audioRoute` in the dry run); when none is free
it refuses until `--atrack` names one. Speed-changed clips are refused. Keyframes
that would land outside the new source range are refused while planning, so the dry
run already says so (`KEYFRAME_OUT_OF_RANGE`); `--drop-outside-keyframes` drops them
instead and lists them in `droppedKeyframes`. If a step fails after the old clip was
removed, the reply has outcome `partial` and `data.completed`: the old clip is gone,
so finish the swap by hand or undo the listed steps. Lessons it encodes:
- **Transitions have an owner and a side.** At the clip's start, one entirely after
  the cut is the clip's own; one entirely before it is the previous clip's
  outgoing-only fade (a name super fading out into the next graphic); one across the
  cut is shared. Mirrored at the end. The dry run lists the clip's own and shared ones
  in `transitions` (`owner`, `before`/`after` seconds) and the neighbours' in
  `neighbourTransitions`. A neighbour's is never re-added on the new clip — but removing
  the old clip deletes it in Premiere, so the swap checks it afterwards and puts it
  back exactly on the neighbour (`status`: `kept`, `restored`, `missing`). A shared
  transition is refused while planning when it is off-centre or the new range lacks
  the handle it covers (frames before the in point / after the out point).
- **Components the new clip cannot get** are refused before anything is written, also
  in the dry run (`UNSUPPORTED`, `data.effects`): Premiere 27's colour-tool container
  `AE.ADBE CompoundComponent` (Color And Contrast, Color Shift, Texture, Unsharp Mask;
  UXP shows none of its settings, so the bridge cannot tell whether it grades
  anything), or anything the panel cannot create (it asks with a `set_params` dry run on
  the old clip; an older panel cannot answer and the report's `notes` say so). Ask the
  user to look at the clip's Effect Controls: if it changes nothing,
  `--skip-effect "AE.ADBE CompoundComponent"` swaps without it (`skippedEffects`);
  otherwise the user swaps that clip in Premiere (Clip > Replace With Clip).
- **Source points land on frames.** Premiere puts an in/out point on the start of the
  frame under it, so `--out 4.554545` (5 µs short of frame 273 at 59.94) would be a
  frame short. An explicit `--in/--out` is snapped to the nearest source frame first
  (`sourceSnap`: the sequence's frames for media at or near its rate, else the file's
  own); the old clip's own range is used as Premiere read it.
- **Interpretation must match.** If the old item was interpreted at 59.94 and the
  new file is 60 fps, run `set_fps --fps 59.94` on the new item first, or clips
  come out a frame short (and adjacent pieces leave a one-frame gap).
- A range covering the whole media is placed as the whole clip (an out point at
  the very end is clamped against the native duration and loses a frame).
- Keyframe times are media time, so they move with the in point when `--in` changes
  (`keyframeShift` in the report).
- Transition durations are snapped to whole frames (+0.1 frame) — otherwise
  Premiere's tick rounding drops one (25 → 24 frames).
- Frame holds (clip menu) and time remapping are not exposed to UXP: pass an
  explicit `--in/--out` over a static stretch of the new media instead.
- Check the new asset itself: a translated title can be bilingual (original
  language first), so the old clip's range may show the wrong half.

## Discipline and limits

- NEVER edit without a digest or explicit user timecodes — no blind cutting.
- Proper nouns from ASR are unreliable (names and places are frequently misspelled).
  Fine for cut decisions; NEVER copy transcript spelling into captions or
  on-screen text without verification.
- Speed-remapped clips break the coordinate formula — unsupported; stop and say so.
- Same-named items with different media are refused (`AMBIGUOUS`); ask which one
  is meant if the bin path doesn't make it obvious.
- Every write lands in Undo history under your `--label`; range placements are
  three labelled steps (`[1/3]`…) — say so when reporting. `set_scale`,
  `set_fps`, `remove_items` and `add_transition` are one step each; `set_params` is
  two when it adds effects (`[add effects]`), `add_marker --color` is two,
  `set_volume` is one per clip, `split_clip` four, `extend_cut` one plus one per
  extended clip, `snap_edges` one per moved or trimmed clip edge, and `replace_clip`, `cut_range` and `conform_captions` several
  labelled steps (`undoSteps` in the reply).
- Before writing to a track, `list_items --track Vn` (or an `apply_plan --dry-run`)
  to see what is already there; overwrite mode replaces whatever is underneath.
- Media with an audio stream placed with `--atrack 0` lands on A1 — usually the
  dialogue. Route B-roll audio to a free track, or let `apply_plan` choose.
- Do not run write operations on production projects without explicit user
  confirmation; prefer test projects or copies. `import_transcript` and
  `transcribe_clip` change the project too.
- If a command errors with a `[step name]` prefix, report the step verbatim.
- Premiere's own Speech-to-Text is available (`transcribe_clip`, 26.5+), but you
  don't need it for editing decisions — transcribe.py is the default.
