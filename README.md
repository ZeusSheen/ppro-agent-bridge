# Premiere Pro Agent Bridge

A minimal, auditable bridge that lets an AI agent (or any script) drive a live
Adobe Premiere Pro session for **transcript-driven, closed-loop video editing** —
built entirely on Premiere's official UXP APIs (GA since 25.6), with fully local
Whisper transcription.

> **Status: experimental (v0.3.2).** Tested on Premiere Pro 26.x–27.1 / Windows 11.
> Twenty whitelisted panel operations plus CLI orchestration (edit plans,
> contact sheets, effect-preserving clip swaps), reviewable in one sitting.

## Why this exists

Most "AI video editing" agents are **open-loop**: a prompt goes in, a finished
render comes out, and no project file survives in the middle — so a human can
never take over, refine, or trust the result. Editing is not a generation task;
it is a search task that converges through feedback (cut, look, adjust).

This bridge closes the loop by giving an agent three "eyes" on a real Premiere
project, each with its own cost profile:

| Layer | Artifact | Answers | Cost |
|---|---|---|---|
| Semantic | transcript digest (sentences + timecodes) | *what is being said, when* | cheapest — most decisions happen here |
| Structure | OpenTimelineIO export | *what sits where on the timeline* | pure arithmetic |
| Perception | rendered program-output frames | *what it actually looks like* | most expensive — used sparsely |

Decisions are made in the text domain, executed in the structure domain, and
spot-checked in the perception domain. The Premiere project file stays at the
center, so humans and agents can hand work back and forth at any point.

## Architecture

```
agent / terminal
      |   python cli/ppro.py <action>     (JSON out, exit codes)
      |   python cli/transcribe.py        (local Whisper, no Premiere needed)
      v
broker/broker.py          WebSocket hub, binds 127.0.0.1:8017 ONLY
      ^
      |   UXP panels cannot listen on sockets, so the panel dials OUT
      v
plugin/                   UXP panel inside Premiere (whitelisted executor)
      v
Premiere Pro project      every write is a named entry in Undo history
```

## Requirements

- Adobe Premiere Pro **25.6+** (UXP GA; frame/OTIO export need it), 26.x recommended
- Adobe **UXP Developer Tool** (install via Creative Cloud)
- Python **3.10+**, `pip install -r requirements.txt`
- Optional, for local transcription: `pip install faster-whisper`
  (GPU: also `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` — the CLI puts
  their DLLs on the search path automatically)
- Premiere: `Settings → Plugins → Enable developer mode`, then restart Premiere

## Install & run

1. Start the broker (keep this terminal open):
   ```
   cd broker
   python broker.py
   ```
2. Load the panel: UXP Developer Tool → **Add Plugin** → select
   `plugin/manifest.json` → **Load**. Open it in Premiere via
   `Window → UXP Plugins → Agent Bridge`. The status dot turns green when
   connected to the broker.
3. Smoke test (with any project open):
   ```
   cd cli
   python ppro.py ping
   python ppro.py get_project
   ```

No Premiere at hand? `python broker/fake_panel.py` stands in for the panel so
the whole pipeline can be tested headlessly. It keeps an in-memory project and
timeline, so `apply_plan`, `list_items`, `set_scale` and `remove_items` can be
exercised end to end. To test without disturbing a live Premiere session, run it
against a second broker, in three terminals (Windows `cmd` shown; PowerShell:
`$env:PPRO_BRIDGE_PORT = "8018"`, bash: `export PPRO_BRIDGE_PORT=8018`):

```
:: terminal 1
set PPRO_BRIDGE_PORT=8018
python broker/broker.py

:: terminal 2
set PPRO_BRIDGE_URL=ws://127.0.0.1:8018
python broker/fake_panel.py

:: terminal 3
set PPRO_BRIDGE_URL=ws://127.0.0.1:8018
python cli/ppro.py ping
```

## Commands

| Command | Purpose |
|---|---|
| `ping` | Liveness check + version handshake (`compatible`, supported `actions`) |
| `get_project` | Active project / sequence name |
| `get_sequence` | Frame size, fps, duration, every track's name and clip count |
| `list_items [--track V3]` | Timeline clips as JSON (sequence start/end, source in/out, media path) — no OTIO round-trip |
| `inspect_items [--track V5]` | Effect chains (params, keyframes, added filters) of timeline clips — what the editor changed |
| `replace_clip --track V5 --at s --with "item"` | "Replace With Clip" that keeps effect params, filters and edge transitions, verified by read-back |
| `list_transition_names` | Video transition match names |
| `list_project_items` | Project panel clips with bin and media path |
| `list_transcripts` | Every project clip + whether it has a transcript |
| `transcribe.py <media> --output f.json [--language en] [--model large-v3]` | **Local Whisper** → Adobe-format transcript JSON (no cloud, no key) |
| `export_transcript --output f.json [--item "name"]` | Export a Premiere-native transcript |
| `transcript_digest.py f.json [--json] [--min-conf 0.8]` | Transcript → sentence list with timecodes |
| `import_transcript --item "name" --file f.json` | Attach a transcript JSON to a clip in Premiere |
| `export_otio --output f.otio` | Timeline structure as OpenTimelineIO (includes source media paths) |
| `export_frame --dir d [--at sec] [--width W --height H]` | One frame of the program output (default: the sequence's frame size) |
| `export_frames --dir d --at 4,18.5,31.6 [--sheet]` | Several frames, optionally a labelled contact sheet |
| `import_media --path f [...] [--bin "Name"]` | Import media into the root or a bin (created if missing); returns the new item names |
| `insert_clip --item "name" [--at sec] [--vtrack N] [--atrack N] [--mode overwrite\|insert] [--in sec --out sec] [--label "text"]` | Place a clip — or, with `--in/--out`, just that source range ("cut by sentence") |
| `apply_plan plan.json [--dry-run] [--notes-as-markers] [--report r.json]` | Execute a JSON edit plan: imports, frame-snapped placements, free audio-track routing, conflict check, post-placement verification |
| `set_scale --track V3 --at sec (--fit \| --fill \| --value %)` | Motion › Scale, fit/fill computed per clip from the media size |
| `set_fps --item "name" --fps 59.94` | Interpret footage (frame-rate override) |
| `add_marker --at sec --name "…" [--comment] [--color red]` | Sequence marker |
| `remove_items --track V3 --from s --to s [--dry-run]` | Remove clips in a range (default: only those entirely inside) |

Everything prints a single JSON object. `insert_clip` with a source range runs
three labelled transactions (set range → place → clear range), each one step in
Premiere's Undo history.

### Example: sentence-level cut, end to end

```
python transcribe.py footage.mp4 --output tr.json --language en
python transcript_digest.py tr.json
#  13. [00:52.8 - 00:56.4] Speaker 1: The second point is even more important ...
python ppro.py insert_clip --item "footage.mp4" --at 0 --in 52.8 --out 56.4 \
    --vtrack 1 --atrack 1 --label "sentence 13"
python ppro.py export_frame --dir ./frames --at 2.0   # eyeball the cut
```

The full workflow (coordinate math, take selection for raw footage, editing
discipline, failure handling) ships pre-wired for both major agent ecosystems:
**Claude Code** auto-loads `.claude/skills/ppro-bridge/SKILL.md`, and **Codex**
(and anything else following the AGENTS.md convention) reads `AGENTS.md`.
Open your agent in the repo root and it can run the loop autonomously;
`SKILL.md` at the root is the master copy.

## Transcription notes

- Whisper runs entirely on your machine: weights download once from Hugging
  Face, audio never leaves the host. CUDA is auto-detected; on CPU prefer
  `--model medium`.
- Whisper's word confidence values are real — `--min-conf` on the digest is a
  usable quality filter. Premiere-native transcripts export placeholder `1.0`
  confidence; judge those by reading the text.
- Transcript times are **source-clip seconds**; timeline positions are
  **sequence seconds**. The OTIO export's source ranges convert between them:
  `sequence_time = clip_position + (source_time − source_range_start)`.

## Security model

- The broker binds **127.0.0.1 only**. No remote access, by design — do not
  port-forward it.
- The broker refuses WebSocket handshakes from web pages. Browsers let any site
  open a socket to localhost; such a request carries the site's `http(s)`
  Origin, which the CLI and the UXP panel do not send, and is rejected with 403.
- The panel manifest asks for `network: all` because UXP has been reported to
  reject `localhost`/`127.0.0.1` as listed domains; the code only ever dials the
  local broker.
- The panel executes a **fixed whitelist**; there is intentionally no
  eval / run-arbitrary-script command. New capability = a new named handler
  in `plugin/main.js`, reviewable in a diff.
- Every write operation lands in Premiere's Undo history under a caller-supplied
  label — the human always has a one-keystroke rollback.
- Transcription is local; nothing is uploaded anywhere.
- Treat any agent bridge as shell-equivalent access to the host app. Run it on
  test projects or copies first.

## Known limitations

- Duplicate clip names resolve to the first match (unique-ID addressing is on
  the roadmap).
- No speaker diarization in the Whisper path (single speaker stamped).
- Speed-remapped clips are deliberately unsupported (v1 fails loudly rather
  than drifting silently).
- There is no UXP API to trigger Premiere's own Speech-to-Text — which is why
  local Whisper is the primary path.
- Frame export inherits a known Adobe double-extension quirk; the CLI resolves
  the real filename automatically (`resolvedPath`).
- Placed media is not auto-scaled: a 4K clip in an HD-wide sequence lands as a
  2× crop. Use `set_scale --fit/--fill` (or `"scale"` in a plan).
- Premiere 27.1 rejects `importFiles` without a real target bin; v0.3 passes the
  root bin (or the named `--bin`).
- Tested on one machine. Expect edges.

## Changelog

- **0.3.2** — `replace_clip` (CLI, `cli/replace.py`) on new panel actions `set_params`
  (values/keyframes, re-creates filters by match name), `add_transition`,
  `list_transition_names`. Proven on a language-version swap of 11 edited clips:
  before/after effect + transition reports identical. Frame-accurate checks
  (tolerance < 1 frame) caught and fixed interpretation (60 vs 59.94), end-of-media
  clamping and transition tick rounding. The broker now refuses connections from
  web pages (browser `Origin` check); needs `websockets>=14`. Added LICENSE,
  requirements.txt, .gitignore/.gitattributes.
- **0.3.1** — `inspect_items`: read-only effect chains (Motion, Opacity, filters,
  params, keyframes) so an agent can see an editor's changes before touching clips.
- **0.3.0** — Learned from a real B-roll/graphics job on a vertical 59.94 sequence.
  `get_sequence` reports frame size, fps, duration and tracks; new `list_items`,
  `list_project_items`, `export_frames` (contact sheets), `apply_plan` (JSON edit
  plans with dry-run, frame snapping, audio routing, conflict check, verification),
  `set_scale`, `set_fps`, `add_marker`, `remove_items`; `import_media --bin` and
  `newItems`; frame export defaults to the sequence size; version handshake in
  `ping`; fixed `import_media` on Premiere 27.1; stateful fake panel; port override
  for headless tests.
- **0.2** — Local Whisper transcription (`transcribe.py`), sentence digest,
  `import_transcript`, sentence-level `insert_clip --in/--out`, AGENTS.md for Codex.

## Roadmap

- Range placement as one undo step (subclip-based, Premiere 26.3+)
- Unique-ID clip addressing; clip type/word-count in `list_transcripts`
- Sequence-level transcripts and SRT/caption export for finished cuts
- Speaker diarization option in the Whisper path
- macOS validation

## Acknowledgments

API usage patterns were verified against Adobe's official
[uxp-premiere-pro-samples](https://github.com/AdobeDocs/uxp-premiere-pro-samples).
This project is not affiliated with or endorsed by Adobe, OpenAI, or Anthropic.

## License

MIT — see [LICENSE](LICENSE).
