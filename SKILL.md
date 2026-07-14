---
name: ppro-bridge
description: Drive Adobe Premiere Pro through the ppro-agent-bridge CLI for transcript-driven, closed-loop video editing. Use when asked to inspect a Premiere project, transcribe footage (local Whisper), read transcripts, export frames or OTIO timelines, import media or transcripts, place clips or sentence-level cuts on the timeline, or assemble a rough cut / best-take edit from talking-head footage. Requires the broker running and the Agent Bridge UXP panel loaded in Premiere.
---

# Premiere Pro Agent Bridge (v0.2)

Control a live Premiere Pro session via CLI. Architecture: `python ppro.py <action>`
→ local broker (127.0.0.1:8017) → UXP panel inside Premiere. Every command prints
one JSON object; exit code 0 = success.

Working directory for all commands: the `cli/` folder of this repository.

## Preconditions (check before anything else)

Run `python ppro.py ping`. If it fails: the broker isn't running (`python broker.py`
in `broker/`, separate terminal, keep open) or the panel isn't loaded (UXP
Developer Tool → Load; Premiere: Window → UXP Plugins → Agent Bridge). Report
the failure instead of retrying blindly. `transcribe.py` works without Premiere.

## Commands

| Command | Purpose |
|---|---|
| `python ppro.py ping` | Liveness check |
| `python ppro.py get_project` / `get_sequence` | Active project / sequence info |
| `python ppro.py list_transcripts` | Every project clip + hasTranscript flag |
| `python ppro.py export_transcript --output f.json [--item "name"]` | Export a clip's Premiere transcript |
| `python transcribe.py <media file> --output f.json [--language en] [--model large-v3] [--digest]` | LOCAL Whisper transcription → Adobe-format JSON. GPU auto-detected; on CPU use `--model medium` |
| `python transcript_digest.py f.json [--json] [--min-conf 0.8]` | Transcript → sentence list with timecodes |
| `python ppro.py import_transcript --item "name" --file f.json` | Attach a transcript JSON to a clip in Premiere (optional; digest works without it) |
| `python ppro.py export_otio --output f.otio` | Timeline structure as OpenTimelineIO (includes each clip's source media path) |
| `python ppro.py export_frame --dir d [--at sec] [--width 640] [--height 360]` | One frame of the program output; real path in `resolvedPath` |
| `python ppro.py import_media --path f [--path f2 ...]` | Import media into project root |
| `python ppro.py insert_clip --item "name" [--at sec] [--vtrack N] [--atrack N] [--mode overwrite\|insert] [--in sec --out sec] [--label "text"]` | Place a clip — with `--in/--out`, only that source range ("cut by sentence") |

Track indices are 0-based (0 = V1). `--mode overwrite` (default) replaces what's
under it; `insert` pushes content rightward. `--in/--out` come as a pair, in
SOURCE seconds. Always pass `--label` describing the edit intent — it becomes
the user's undo-history entry.

## Getting a transcript (Whisper-first)

Preferred path — run local Whisper yourself; do not wait for Premiere:
1. Find the source file path: from the user, or from `export_otio` (each clip
   records its media path), or `get_project` context.
2. `python transcribe.py "<path>" --output tr.json --language en` (use `zh` for
   Chinese; omit `--language` to auto-detect). First run downloads model weights.
3. Whisper confidence values are REAL — `--min-conf 0.8` on the digest is a
   valid quality filter.

Premiere-native transcripts (via `export_transcript`) are also fine when they
already exist, but their confidence field is placeholder 1.0 — judge quality
from the text. A transcript exporting ~170 bytes with `words: []` and language
`und-zz` is an empty placeholder: treat as "no transcript".

## The three layers and their coordinate systems

1. **Semantic** — transcript digest: sentences with speaker/start/end in
   SOURCE-CLIP seconds.
2. **Structure** — OTIO export: which source file each timeline clip uses, its
   source range, its sequence position.
3. **Perception** — `export_frame` renders the program output (all tracks
   composited); `--at` is SEQUENCE seconds.

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
   --at <running position> --label "<short intent>"`. Add ~0.15 s breathing room
   at boundaries; prefer cutting in pauses. Treat timestamps as ±0.2 s.
5. Verify sparsely: `export_frame --at` at 1–3 cut points (mid-grimace faces,
   clipped gestures), not every frame.
6. Report: chosen sentences with timecodes, skipped material and why, total
   runtime, undo labels used.

## Discipline and limits

- NEVER edit without a digest or explicit user timecodes — no blind cutting.
- Proper nouns from ASR are unreliable (names and places are frequently misspelled).
  Fine for cut decisions; NEVER copy transcript spelling into captions or
  on-screen text without verification.
- Speed-remapped clips break the coordinate formula — unsupported; stop and say so.
- Duplicate clip names resolve to the first match; if ambiguous, ask.
- Every write lands in Undo history under your `--label`; range placements are
  three labelled steps (`[1/3]`…) — say so when reporting.
- Do not run write operations on production projects without explicit user
  confirmation; prefer test projects or copies.
- If a command errors with a `[step name]` prefix, report the step verbatim.
- You cannot trigger Premiere's own Speech-to-Text; you don't need to — use
  transcribe.py.
