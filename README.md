# Premiere Pro Agent Bridge

A minimal, auditable bridge that lets an AI agent (or any script) drive a live
Adobe Premiere Pro session for **transcript-driven, closed-loop video editing** —
built entirely on Premiere's official UXP APIs (GA since 25.6), with fully local
Whisper transcription.

> **Status: experimental.** Tested on Premiere Pro 26.x / Windows 11.
> Eleven whitelisted operations, ~1,200 lines total, reviewable in one sitting.

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
the whole pipeline can be tested headlessly.

## Commands

| Command | Purpose |
|---|---|
| `ping` | Liveness check |
| `get_project` / `get_sequence` | Active project / sequence info |
| `list_transcripts` | Every project clip + whether it has a transcript |
| `transcribe.py <media> --output f.json [--language en] [--model large-v3]` | **Local Whisper** → Adobe-format transcript JSON (no cloud, no key) |
| `export_transcript --output f.json [--item "name"]` | Export a Premiere-native transcript |
| `transcript_digest.py f.json [--json] [--min-conf 0.8]` | Transcript → sentence list with timecodes |
| `import_transcript --item "name" --file f.json` | Attach a transcript JSON to a clip in Premiere |
| `export_otio --output f.otio` | Timeline structure as OpenTimelineIO (includes source media paths) |
| `export_frame --dir d [--at sec] [--width 640] [--height 360]` | One frame of the program output |
| `import_media --path f [...]` | Import media into the project root |
| `insert_clip --item "name" [--at sec] [--vtrack N] [--atrack N] [--mode overwrite\|insert] [--in sec --out sec] [--label "text"]` | Place a clip — or, with `--in/--out`, just that source range ("cut by sentence") |

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
- Tested on one machine, one Premiere version. Expect edges.

## Roadmap

- Unique-ID clip addressing; clip type/word-count in `list_transcripts`
- Sequence-level transcripts and SRT/caption export for finished cuts
- Speaker diarization option in the Whisper path
- macOS validation

## Acknowledgments

API usage patterns were verified against Adobe's official
[uxp-premiere-pro-samples](https://github.com/AdobeDocs/uxp-premiere-pro-samples).
This project is not affiliated with or endorsed by Adobe, OpenAI, or Anthropic.

## License

MIT
