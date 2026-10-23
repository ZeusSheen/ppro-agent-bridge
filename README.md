# Premiere Pro Agent Bridge

A minimal, auditable bridge that lets an AI agent (or any script) drive a live
Adobe Premiere Pro session for **transcript-driven, closed-loop video editing** —
built entirely on Premiere's official UXP APIs (GA since 25.6), with fully local
Whisper transcription.

> **Status: experimental (v0.5.1).** Tested on Premiere Pro 27.1 / Windows 11
> (earlier releases also ran on 26.x). macOS is not supported yet.
> Twenty-one whitelisted panel operations, checked against one action spec, plus
> a Python library and CLI on top (edit plans, contact sheets, effect-preserving
> clip swaps), reviewable in one sitting.

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
agent / terminal / Python
      |   python cli/ppro.py <action>     (JSON out, exit codes; = python -m ppro_bridge)
      |   python cli/transcribe.py        (local Whisper, no Premiere needed)
      |   from ppro_bridge import ops     (the same commands as functions)
      v
broker/broker.py          WebSocket hub, binds 127.0.0.1:8017 ONLY
      ^
      |   UXP panels cannot listen on sockets, so the panel dials OUT
      v
plugin/                   UXP panel inside Premiere (whitelisted executor)
      v
Premiere Pro project      every write is a named entry in Undo history
```

- `ppro_bridge/` — the Python package: transport, item resolution, edit plans,
  clip swaps, transcripts, one function per CLI command (`ops`), and
  `actions.json`, the action spec. `cli/*.py` are thin wrappers, so
  `cd cli && python ppro.py …` keeps working.
- `plugin/` — `main.js` loads small modules from `plugin/lib/` (core, timeline,
  project, effects, capabilities, handlers); `lib/spec.js` is generated from the
  action spec, so the panel refuses unknown actions and bad params itself.
- `broker/fake_panel.py` — an in-memory stand-in that answers all 21 actions.

## Requirements

- Adobe Premiere Pro **26.3+** (OTIO export needs 26.2, transcript checks 26.3,
  Speech-to-Text 26.5); tested on 27.1
- **Windows** (macOS untested: Premiere restricts plain-HTTP connections there)
- Adobe **UXP Developer Tool** (install via Creative Cloud)
- Python **3.10+**, `pip install -r requirements.txt`
- Optional, for local transcription: `pip install faster-whisper`
  (GPU: also `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` — the CLI puts
  their DLLs on the search path automatically)
- Optional, for the test suite: Node.js (runs the panel code against mocks)
- Premiere: `Settings → Plugins → Enable developer mode`, then restart Premiere

## Install & run

1. Once: double-click **`setup_bridge.bat`**. The setup opens in its own window
   (Edge app mode: no address bar or tabs) and goes through the machine step by
   step: Python, the Python packages, Premiere (at least the panel manifest's
   minimum, 26.3), UXP Developer Tool and developer mode, then the optional
   ffmpeg, Pillow and faster-whisper, and last a desktop shortcut, starting the
   bridge with Windows and starting it now. Fixes are buttons, and it checks again
   whenever you come back to the window. Without Python it opens a page that
   explains how to install it. `setup_bridge.bat --console` is the text version
   with y/n questions (`python -m ppro_bridge.doctor`; without `--interactive` it
   only reports); `python -m ppro_bridge.setup_ui --demo` shows the window without
   touching the machine.
2. Every day: double-click **`start_bridge.bat`** (keep its window open). It runs
   the broker with `--load-panel`: UXP Developer Tool is started minimized when it
   is not running, and the panel is loaded into Premiere whenever Premiere
   connects to it — at start-up, after a Premiere restart, after UXP Developer Tool
   restarts. No clicking **Load**. Open the panel in Premiere via
   `Window → UXP Plugins → Agent Bridge`; the status dot turns green when it is
   connected to the broker.

   Why UXP Developer Tool still has to run: Premiere only connects to a developer
   service that UXP Developer Tool announces through Adobe's own IPC; a look-alike
   service is never contacted. The bridge drives UXP Developer Tool's service
   instead (`ppro_bridge/devloader.py`, the command-line endpoint Adobe's
   `uxp plugin load` used; `python -m ppro_bridge.devloader` loads once).

   By hand, as before: `python broker/broker.py` (without `--load-panel`), then UXP
   Developer Tool → **Add Plugin** → `plugin/manifest.json` → **Load**.
3. Smoke test (with any project open):
   ```
   cd cli
   python ppro.py ping
   python ppro.py get_project
   ```
   `ping` says `"compatible": true` when CLI, broker and panel are the same
   version, the panel speaks protocol 2 and this Premiere build is not on the
   panel's denylist; otherwise its `warning` says what to restart or reload.

Pairing is automatic: on its first start the broker writes a random token to
`%USERPROFILE%\.ppro-agent-bridge\token`, and the CLI (and any script using
`ppro_bridge`) sends it on every connection. (Not in `%LOCALAPPDATA%`: Windows
gives packaged apps such as the Claude desktop app a private copy of it, so a CLI
run from one would read a different token than a broker started by double-click.) A client without it is refused with
`AUTH_FAILED` — that includes a CLI older than 0.5, so run the CLI and the broker
from the same version. `PPRO_BRIDGE_TOKEN` (the token itself) or
`PPRO_BRIDGE_TOKEN_FILE` (another file) override this for both sides. The broker
also keeps an audit log, one JSON line per request, in
`%USERPROFILE%\.ppro-agent-bridge\audit\audit-YYYY-MM-DD.jsonl` (30 days kept;
`PPRO_AUDIT_DIR` moves it, `PPRO_AUDIT_DIR=off` turns it off).

No Premiere at hand? `python broker/fake_panel.py` stands in for the panel so
the whole pipeline can be tested headlessly. It keeps an in-memory project and
timeline, so `apply_plan`, `list_items`, `set_scale`, `replace_clip` and
`remove_items` can be exercised end to end. To test without disturbing a live
Premiere session, run it against a second broker, in three terminals (Windows
`cmd` shown; PowerShell: `$env:PPRO_BRIDGE_PORT = "8018"`, bash:
`export PPRO_BRIDGE_PORT=8018`):

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

The test suite starts its own broker and fake panel on free ports (synthetic data
only, nothing touches Premiere): `python -m unittest discover -s tests`.

## Commands

| Command | Purpose |
|---|---|
| `ping [--probe]` | Liveness check + version/protocol handshake (`compatible`, `actions`, `capabilities`, `denied`); `--probe` re-checks capabilities |
| `get_project` | Active project / sequence name and guid |
| `get_sequence` | Frame size, fps, duration, every track's name and clip count |
| `list_items [--track V3]` | Timeline clips as JSON (sequence start/end, source in/out, media path) — no OTIO round-trip |
| `inspect_items [--track V5]` | Effect chains (params, keyframes in media time, added filters with `occurrence`) of timeline clips — what the editor changed |
| `set_params --track V5 --at s --json fx.json` | Effect params and keyframes on one clip, with an explicit time base (`media`, `clip`, `sequence`); validated as a whole before anything changes. Audio tracks too (`A1`: the clip's own Volume) |
| `set_volume --track A1 --at s --db -6` | Clip volume in dB as Premiere shows it, on one clip or every clip in `--from/--to`; verified |
| `add_transition --track V5 --at s --match-name NAME --duration s --edge start\|end` | A video transition on a clip's start or end, read back from OTIO; refused where the edit point already has one (`TRANSITION_EXISTS`) |
| `replace_clip --track V5 --at s --with "item" [--atrack A3] [--skip-effect NAME] [--dry-run]` | "Replace With Clip" that keeps effect params, keyframes, filters and the clip's own transitions without touching a neighbour's, verified by read-back; the new audio goes to a free track or the swap is refused, and so is a component the new clip cannot get |
| `list_transition_names` | Video transition match names |
| `list_project_items` | Project panel clips with bin and media path |
| `list_transcripts [--jobs]` | Every project clip + whether it has a transcript, plus Speech-to-Text jobs |
| `transcribe.py <media> --output f.json [--language en] [--model large-v3]` | **Local Whisper** → Adobe-format transcript JSON (no cloud, no key) |
| `transcribe_clip --item "name" [--language en-US] [--wait 600] [--output f.json]` | **Premiere Speech-to-Text** (26.5+) on one clip, waited for and exported |
| `export_transcript --output f.json [--item "bin/name"]` | Export a Premiere-native transcript |
| `transcript_digest.py f.json [--json] [--min-conf 0.8]` | Transcript → sentence list with timecodes |
| `import_transcript --item "name" --file f.json [--replace]` | Attach a transcript JSON to a clip in Premiere, verified by reading it back |
| `export_otio --output f.otio` | Timeline structure as OpenTimelineIO (includes source media paths) |
| `probe_api [--output dump.json] [--diff old.json]` | This Premiere build's UXP API, with a watch list of the calls the UXP port needs |
| `export_frame --dir d [--at sec] [--width W --height H]` | One frame of the program output (default: the sequence's frame size) |
| `export_frames --dir d --at 4,18.5,31.6 [--sheet]` | Several frames, optionally a labelled contact sheet |
| `import_media --path f [...] [--bin "Name"]` | Import media into the root or a bin (created if missing); returns the new item names |
| `insert_clip --item "name" [--at sec] [--vtrack N] [--atrack N] [--mode overwrite\|insert] [--in sec --out sec] [--label "text"]` | Place a clip — or, with `--in/--out`, just that source range ("cut by sentence") |
| `apply_plan plan.json [--dry-run] [--notes-as-markers] [--any-sequence] [--report r.json]` | Execute a JSON edit plan: imports, frame-snapped placements, free audio-track routing, conflict check, verification of both legs |
| `set_scale --track V3 --at sec (--fit \| --fill \| --value %)` | Motion › Scale, fit/fill computed per clip from the media size |
| `set_fps --item "name" --fps 59.94 [--dry-run]` | Interpret footage (frame-rate override; affects every sequence) |
| `add_marker --at sec --name "…" [--comment] [--color red]` | Sequence marker |
| `remove_items --track V3 --from s --to s [--ripple --confirm-ripple] [--dry-run]` | Remove clips in a range (default: only those entirely inside) |
| `split_clip --track V1 --at s [--dry-run]` | Split a clip without a razor (clone, trim, move); both halves keep volume, scaling and effects |
| `cut_range --from s --to s [--track V1 …] [--captions V3 --srt f.srt] [--dry-run]` | Remove a time range from every track and close the gap on all of them in sync, in one undo step for the ripple; optionally conform a caption movie |
| `extend_cut --at s [--out s] [--in s] [--extend V1 …] [--dry-run]` | Open up a cut (breathing room) with more of the clips' own source, everything after it moving right on all tracks in one step |
| `snap_edges [--track V1 …] [--dry-run]` | Put clip edges that drifted microseconds off the frame grid back on it, so the picture cuts with the sound |
| `conform_captions --track V3 --srt f.srt [--dry-run]` | Trim the pieces of a pre-rendered caption movie so no line flashes in at a cut |
| `review_cuts [--track A1] [--render] [--words tr.json]` | **Listening report**: breathing room, clipped words, room-tone jumps and clicks at every cut, loudness with `--render`, plus the timeline lint |
| `lint_timeline` | Slivers, cuts too close, gaps, overlaps, V/A cut mismatches, clips past the end, edges off the frame grid |
| `export_audio --output f.wav [--render]` | The sequence's audio as a WAV (shadow mix of the sources, or Premiere's render) |

Everything prints a single JSON object; failures carry a `code` and an `outcome`:
`not_committed` (nothing changed), `committed` (the write ran but did not do what
was asked), `partial` (some steps of a multi-step command landed: see
`data.completed`) or `unknown` (check the timeline before retrying).
Writes accept `--expect-seq "<name>"` and refuse when another sequence is active.
Where two project items share a name but not their media, address one as
`"<bin path>/<name>"`. `insert_clip` with a source range runs three labelled
transactions (set range → place → clear range), each one step in Premiere's Undo
history; the range is cleared even if placing fails.

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
`SKILL.md` at the root is the master copy (the copies are generated, see
"Versioning").

## Python library

```python
from ppro_bridge import ops, transport

rpc = transport.make_rpc(guard={"projectPrefix": "PAB_TEST"})  # every call pinned to a PAB_TEST* project
seq = ops.get_sequence(rpc)
clips = ops.list_items(rpc, track="V1")
ops.add_marker(rpc, at=4.0, name="Check this claim", color="red")
```

`ops` has one keyword-only function per CLI command, with the CLI's option names;
each returns the command's data and raises `BridgeError` (`code`, `outcome`,
`data`) on failure. `rpc` is always passed in, so nothing reaches a live Premiere by
accident. The guard (`project`, `projectPrefix`, `projectGuid`, `sequence`,
`sequenceGuid`) is checked by the panel on the project and sequence it is about to
touch; a mismatch is refused with `PROJECT_MISMATCH` / `SEQUENCE_MISMATCH` before
anything changes.

## Protocol and capabilities

- Requests and replies follow `ppro_bridge/actions.json` (protocol 2): every
  action's params, reply keys, error codes and outcome rule. The panel, the fake
  panel and the CLI are unit-tested against it; the panel checks each request's
  params before running a handler (`BAD_PARAMS`, `UNKNOWN_ACTION`).
- `ping` reports `protocol`, `hostInfo` (Premiere version, UI locale), `modules`,
  `capabilities` (per-build feature flags: Speech-to-Text, razor, MOGRT text,
  guids, …; each `true`, `false` or `"unknown"` until probed — `ping --probe`
  probes again), `loadedAt` and a `codeHash` of the loaded panel files. The CLI
  hashes its own `plugin/` folder and warns when the loaded panel is stale.
- Commands that need protocol 2 (set_params, replace_clip, transcripts) refuse an
  older panel with `PANEL_OUTDATED` instead of editing through it.
- A host denylist in the panel (empty in releases) can mark a Premiere build as
  unsafe: the panel then answers `ping` only (with the reason) and refuses
  everything else with `HOST_DENIED`.

## Listening, razor-free cuts and captions

UXP gives a panel no way to play or meter audio and, on 27.1, no razor. The bridge
works around both without leaving Premiere's own edit model:

- **Listening** — `review_cuts` rebuilds the dialogue track from the source files
  with ffmpeg (fast; volume not applied) or, with `--render`, has Premiere render its
  own mix through `EncoderManager.exportSequence` with Premiere's WAV preset (about a
  second per minute; adds integrated loudness and true peak). Every cut is measured
  on 10 ms windows against each clip's own noise floor: too little breathing room
  before or after the words (0.25 s / 0.35 s by default), a word cut off, room tone
  jumping, a click. The same report lints the timeline for slivers, near-miss V/A
  cuts and gaps. `review.pad_cut_points` turns word timings into cut ranges with
  breathing room that never reach into a neighbouring word.
- **Cutting** — `split_clip` and `cut_range` clone the clip (Premiere's track-item
  clone keeps every attribute), trim the pieces and move them; the ripple after a
  cut is one transaction across all chosen tracks, so tracks with gaps stay in sync
  (Premiere's own ripple works per track). `extend_cut` does the opposite — it
  opens a cut up with more of the clips' own source for breathing room. Every step
  is read back; dry runs plan the exact steps without writing.
- **Captions** — for a pre-rendered caption movie cut along with the A-roll,
  `conform_captions` reads the SRT it was rendered from and trims pieces that would
  flash a line for a few frames at a cut.
- **Volume** — Volume › Level is linear in UXP (`dB = 20·log10(level) + 15`);
  `set_volume` speaks dB. Track volume, pan and audio effects are out of UXP's reach.

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
- **Premiere Speech-to-Text** (`transcribe_clip`, 26.5+) runs Premiere's own
  transcription on one clip. It returns at once with a job id; the CLI polls
  `list_transcripts --jobs` every 5 s (up to `--wait` seconds) and exports the
  result. Languages are Premiere's codes such as `en-US` — transcript JSON uses
  lowercase codes such as `en-us`. A missing language pack is refused
  (`LANGUAGE_UNAVAILABLE`) rather than triggering Premiere's install prompt. Jobs
  live in the panel: a panel reload forgets them (`TRANSCRIBE_LOST`; the transcript
  may still land — check `list_transcripts`).
- `import_transcript` validates the file against the transcript schema, imports
  it, reads it back and compares word counts (verified working on 27.1 build 17).
  Some older builds (26.x) were reported to import an empty shell; the bridge then
  reports `TRANSCRIPT_NOT_IMPORTED` (outcome `committed`) — import the file by hand
  in Premiere's Text panel instead. A clip that already has words needs `--replace`.

## Versioning

`VERSION` is the only place the version is written by hand. `python
scripts/sync_version.py` copies it into the panel manifest, broker, fake panel,
package and generated panel spec, sets the headings of `SKILL.md` and this README,
and regenerates `AGENTS.md` and the two skill copies from `SKILL.md`. `--check`
writes nothing and lists every drift (and a missing changelog entry); a unit test
runs it. Edit `SKILL.md`, then run the script (`--only docs` regenerates just the
copies).

## Live smoke test (contributors)

`tests/live/live_smoke.py` exercises the real panel in a Premiere **test
project**. It refuses to run unless the open project's name starts with `PAB_TEST`
and pins every request to that project and its active sequence by guid, so if
someone switches projects mid-run the panel refuses instead of writing elsewhere.

```
python tests/live/live_smoke.py --media <test media folder>            # read-only checks
python tests/live/live_smoke.py --media <test media folder> --write    # plus writes
```

The read phase checks ping, capabilities, guards and every read action's reply
against the spec. With `--write` it first copies the saved `.prproj` into its
report folder, imports the test media into a new bin `PAB_SMOKE_<run id>`, works
only in a scratch region after the end of the active sequence (top video track,
a free audio track) — inserts, ranges, scale, effect params in all three time
bases, a transition, `replace_clip`, a marker, transcript import and Speech-to-Text
— then removes exactly the clips it placed (never ripple) and compares the
timeline and project with how it found them. The bridge cannot remove project
items or markers, so the `PAB_SMOKE_*` bins and markers stay (the report lists
them); `--keep` also leaves the clips. Each check prints PASS, FAIL, SKIP or
KNOWN_ISSUE (e.g. Premiere's empty transcript import); any FAIL exits 1.
`tests/live/make_media.py <folder>` generates the media it expects with ffmpeg
(`broll_audio.mp4`, `EN/title.mov`, `CN/title.mov`, optionally `speech_en.wav`).
The unit suite rehearses the harness against the fake panel.

`tests/live/api_smoke.py --media <test media folder>` is the API check that comes
before porting more editing commands: it saves `probe_api`'s inventory of the build
and runs the panel's `api_smoke` cases (removing markers, transitions, effects and
keyframes, cloning and activating sequences, audio effects by display name, bins,
relinking, custom properties, sequence settings, FCP XML export, AAF export with
`--aaf` (Premiere waits on a modal log), MOGRT insertion with `--mogrt`, ...) on a
scratch clip in a `PAB_TEST*` project. Each step reports
pass, fail, absent, error or skipped; each case puts back what it changed and lists
anything it could not under `leftovers`. It also times a video export and
`export_frame`, checks a phone clip for black frames (`--phone-media`) and, with
`--watch-tracks`, times how long `get_sequence` takes to see a track you add. Reports:
`report.json`, `api_smoke.md` and `probe_api.json` in `--out`.

## Security model

- The broker binds **127.0.0.1 only**. No remote access, by design — do not
  port-forward it.
- The broker accepts only two kinds of WebSocket handshake: none with an `Origin`
  header (the CLI) and `Origin: file://` (the UXP panel). Browsers let any page open
  a socket to localhost, but always send an Origin — the page's `http(s)` origin, or
  `null` from sandboxed frames and data URLs — and are refused with 403.
- Controllers (the CLI, scripts, later the MCP server) must send the pairing token
  from `%USERPROFILE%\.ppro-agent-bridge\token`, compared in constant time; a
  missing or wrong one gets `AUTH_FAILED` and the connection is closed before
  anything reaches Premiere. That keeps out local programs and other Windows users
  that cannot read your profile. The panel may still register without the token (it
  cannot read the file yet; a wrong one is refused), so a local program could stand
  in for the panel while it is not connected — closing that is still to do.
- The audit log records every request (action, shortened params, guard, outcome,
  queue and run time, the sequence and track counts Premiere reported), every
  refusal and every panel connection, so a session can be reconstructed afterwards.
- One request reaches Premiere at a time (concurrent UXP calls can crash it). A
  request whose outcome is uncertain keeps the queue closed until the panel answers,
  and a handler that never returns marks the panel stuck instead of piling on.
- The panel executes a **fixed whitelist** and checks every request against the
  action spec first (unknown actions and params are refused); there is
  intentionally no eval / run-arbitrary-script command. New capability = a new
  named handler in `plugin/lib/`, plus its entry in `actions.json`, reviewable in
  a diff.
- Requests can be pinned to a project and sequence (name, prefix or guid); the
  live smoke test uses this so it can only ever write into `PAB_TEST*` projects.
- The panel manifest asks for `network: all` (UXP has been reported to reject
  `localhost`/`127.0.0.1` as listed domains) and `localFileSystem: fullAccess`;
  the code only dials the local broker and touches the paths it is given.
  Tightening both needs a live test of a packaged panel and is planned for v0.5.
- Every write operation lands in Premiere's Undo history under a caller-supplied
  label — the human always has a one-keystroke rollback.
- Transcription is local; nothing is uploaded anywhere (Premiere's own
  Speech-to-Text runs inside Premiere, under Adobe's terms).
- Treat any agent bridge as shell-equivalent access to the host app. Run it on
  test projects or copies first.

## Known limitations

- Same-named project items with different media must be addressed by bin path
  (`"CN/title.mov"`); the bridge refuses to guess.
- No speaker diarization in the Whisper path (single speaker stamped).
- Speed-remapped clips are deliberately unsupported (v1 fails loudly rather
  than drifting silently); keyframes on speed-changed clips only in media time.
- Some older Premiere builds (26.x) were reported to import transcripts as an empty
  shell (`TRANSCRIPT_NOT_IMPORTED`; 27.1 imports correctly), and importing over an
  existing transcript was reported to delete it. Import by hand in the Text panel
  when that happens.
- Premiere 27.1.0 (builds 17 and 21) has no native razor (`capabilities.razor` is false);
  `split_clip` / `cut_range` split by clone + trim instead (more undo steps than a
  razor cut). Markers do not move with `cut_range`.
- The listening report's shadow mix ignores clip/track volume, effects and other
  tracks (`--render` hears Premiere's real mix). It hears sound, not meaning: a
  breath counts as sound, and picture problems show only in the lint. It also needs
  each clip's media file: Enhance Speech results and other generated items have no
  media path, so the shadow mix refuses with `NO_MEDIA` — use `--render` there.
- Speech-to-Text jobs are tracked by the panel and lost when it reloads.
- UXP cannot add tracks, change speed, link/unlink clips or create captions; the
  bridge refuses rather than working around these silently. It has no commands to
  remove markers, transitions, keyframes or project items yet (the UXP calls exist;
  `tests/live/api_smoke.py` checks them).
- Frame export inherits a known Adobe double-extension quirk; the CLI resolves
  the real filename automatically (`resolvedPath`).
- Placed media is not auto-scaled: a 4K clip in an HD-wide sequence lands as a
  2× crop. Use `set_scale --fit/--fill` (or `"scale"` in a plan).
- Premiere 27.1 rejects `importFiles` without a real target bin; the bridge passes
  the root bin (or the named `--bin`).
- Windows only; tested on Premiere 27.1 on one machine. Expect edges.

## Changelog

- **0.5.1** — The setup window (`setup_bridge.bat`) now matches the project page: pure
  black, black and white, a thin glass rim of light on its panels, serif step titles and
  white main buttons. The blurred background haze is gone (it banded on near-black), and
  so are the grey halos around cards. Green, amber and red still mark ready, optional and
  needs setup, always with an icon and a word. Nothing else changed.
- **0.5.0** — Hardening, quick deploy and groundwork for a larger UXP port. Fixes
  from real jobs after 0.4.0: the shadow mix refuses clips without a media file (`NO_MEDIA`: Enhance Speech results and other
  generated items used to mix as silence). From a real language-version swap:
  `replace_clip` attributes transitions by owner and side, so a neighbour's
  outgoing-only fade at the clip's start is no longer taken as the clip's own (it is
  put back exactly when the removal takes it), refuses off-centre or handle-less
  shared transitions and components the new clip cannot get (Premiere 27's colour-tool
  container, or anything the panel's new `set_params` dry run cannot create;
  `--skip-effect` leaves one behind) before writing, and snaps an explicit
  `--in/--out` to the nearest source frame; `add_transition` refuses an edit point
  that already has a transition (`TRANSITION_EXISTS`: Premiere keeps the old one) and
  reads the result back from OTIO. From a festival-video re-cut: `extend_cut` lists
  keyframed Level/Opacity on the clips it lengthens (`heldKeyframes`) and refuses an
  extension that a fade's keyframe would hold silent or invisible (`KEYFRAMED`;
  `--allow-silent`); dry runs of `remove_items`, `set_scale` and `set_volume` honour
  `--expect-seq`; `export_frame(s)` flag all-black PNGs (`black`, with a warning),
  report the PNG's size and time the export (`exportSeconds`, `waitedSeconds`), and
  poll for the file every 50 ms instead of 500 ms. Hardening: a **pairing token**
  (`ppro_bridge/auth.py`, created by the broker in `%USERPROFILE%\.ppro-agent-bridge\token`,
  sent by the CLI by itself; others get `AUTH_FAILED`) and a JSONL **audit log** of
  every request, refusal and panel connection (`ppro_bridge/audit.py`, 30 days kept).
  Groundwork for porting a large CEP tool catalogue to UXP: `probe_api` (panel action
  and CLI) lists every class, method and instance member of the build's UXP API and
  checks a watch list; the panel's `api_smoke` test cases with
  `tests/live/api_smoke.py` find out, in a `PAB_TEST*` project, which documented
  calls really work. On Premiere 27.1 (build 21) almost every call it tried does —
  removing markers, transitions, effects and keyframes, cloning and deleting
  sequences, subclips, relinking, FCP XML export; a sequence's frame size did not
  change through `createSetSettingsAction`, and AAF export waits on a modal log
  (opt-in: `--aaf`). Quick deploy: `setup_bridge.bat` opens a setup window
  (`ppro_bridge/setup_ui.py`, Edge app mode) that checks the machine step by step
  and runs the fixes from buttons (`--console`: the text `doctor` check, with y/n
  fixes), and `start_bridge.bat` runs the broker with `--load-panel`, which starts
  UXP Developer Tool minimized and loads the panel whenever Premiere connects to
  it, so the panel survives Premiere restarts without clicking Load.
  **Breaking:** a 0.4 CLI cannot talk to a 0.5 broker (it sends no token).
- **0.4.0** — Foundations. The CLI is now the `ppro_bridge` package (`python -m
  ppro_bridge`; `cli/ppro.py` and the other scripts keep working as wrappers), with
  one function per command in `ppro_bridge.ops`. One action spec
  (`ppro_bridge/actions.json`, protocol 2) that the panel, the fake panel and the CLI
  are tested against: the panel checks params before any handler runs, every error
  has a `code`, outcomes add `committed` and `partial`, and requests can carry a
  project/sequence guard (name, prefix or guid). `ping` reports protocol, host info,
  per-build capabilities (`--probe`), a code hash (the CLI warns when the loaded panel
  is stale) and a host denylist (ping-only mode, `HOST_DENIED`); commands that need
  the new panel refuse an old one (`PANEL_OUTDATED`). The panel is split into
  modules and builds all of a transaction's actions before adding any. New:
  `transcribe_clip` (Premiere Speech-to-Text, 26.5+), `set_params` and
  `add_transition` on the CLI, with an explicit keyframe time base (media, clip or
  sequence) and `occurrence` for repeated effects, the whole request validated
  first. `import_transcript` validates the file, verifies the import by reading it
  back (`TRANSCRIPT_NOT_IMPORTED` for Premiere's empty-import bug) and needs
  `--replace` to overwrite; `export_transcript` takes bin paths. `replace_clip`
  refuses keyframes that would fall outside the new range
  (`--drop-outside-keyframes`) and reports `partial` when it stops after the
  removal. `transcribe.py` writes valid Adobe language codes and refuses silence.
  One `VERSION` file (`scripts/sync_version.py`), a fake panel that answers all 25
  actions, panel tests on Node, and a live smoke test that only writes into
  `PAB_TEST*` projects. Also new, from a real re-cut job: a **listening report**
  (`review_cuts`, `lint_timeline`, `export_audio`: shadow mix or Premiere's own render
  via the new `export_sequence` action, breathing room / clipped words / room-tone
  jumps / clicks per cut, slivers, loudness), **razor-free editing** (`split_clip`,
  `cut_range` with a one-step, sync-keeping ripple, `extend_cut` for breathing room; panel actions `clone_clip`,
  `trim_clip`, `shift_items`) that keeps every clip attribute, `conform_captions` for
  pre-rendered caption movies, `set_volume` in dB, and `set_params` / `inspect_items`
  on audio tracks. Sequence positions, ends, moves and clone offsets land on exact
  frame ticks (a microsecond past a frame boundary showed a trimmed clip one frame
  longer); `lint_timeline` flags edges that drifted off the grid (`OFF_GRID`) and
  `snap_edges` puts them back.
  **Breaking for scripts:** the panel refuses unknown or
  mistyped params (`BAD_PARAMS`); `set_params` keyframes need a `timeBase`;
  `export_transcript` refuses empty placeholder transcripts (`NO_TRANSCRIPT`) and
  ambiguous names; `import_transcript` refuses to overwrite without `--replace`;
  CLI output no longer carries the broker's `id`/`broker` keys, error messages start
  with `<action>: `, and `import_media` lists missing files under `data.missing`;
  failed reads report `not_committed` instead of `unknown`; the CLI, `transcribe.py`
  and `transcript_digest.py` write UTF-8 to stdout/stderr; the digest attaches
  punctuation tokens and honours their sentence ends, so sentence counts can change;
  transcripts written by 0.3.x `transcribe.py` (`zh-cn`, `se-se`, …) are upgraded on
  import with a warning. `cli/ppro.py` keeps its 0.3.3 names (`rpc`, `call`,
  `BROKER_URL`, `cmd_*`, …), and assigning them still redirects the CLI.
- **0.3.3** — Safety release. Fixes the cause of "one frame short" placements: Premiere
  floors source in/out points to whole frames, and a frame boundary passed as float seconds
  (1.001 → 1.000999…) landed a tick short and lost its frame; the panel now nudges every
  in/out/position by 1 µs and `apply_plan` sends exact, unrounded times (measured on 27.1).
  The broker only accepts the CLI and the panel (the web-page
  check also refuses `null` and localhost pages now), sends one request at a time,
  reports `outcome` on every failure and its own version in `ping`. The panel runs
  requests in order with a watchdog. `replace_clip` refuses same-named items with
  different media, sends new audio to a free track instead of the last one, moves
  keyframes with the in point and verifies them, and refuses speed-changed clips.
  `apply_plan` refuses or stops when the active sequence isn't the plan's, places
  whole-file ranges as whole clips without overrunning the next clip, and verifies the
  audio leg too, to half a frame. `insert_clip` clears its source range even when
  placing fails and refuses missing tracks; `set_fps` refuses ambiguous names and
  has `--dry-run`; `remove_items --ripple` needs `--confirm-ripple` and reports what
  moved. Manifest minimum is now 26.3. Added a unit test suite.
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

- **v0.6 — MCP and more commands:** a stdio MCP server on the same `ppro_bridge`
  library, `.ccx` packaging of the panel (no UXP Developer Tool needed), and new
  commands on the calls `api_smoke` verified: removing markers, transitions,
  effects and keyframes, sequence clones as restore points before risky edits,
  subclips, relinking.
- **Hardening still to do:** the panel sending the pairing token too, a write
  guard and a sequence fingerprint as a precondition on writes, path checks,
  tighter manifest permissions (`network`, `localFileSystem`), and failed reads
  reported as failures rather than as an empty timeline.
- Later: native razor once Premiere exposes it (fewer undo steps than clone + trim),
  range placement as one undo step via subclips, unique-ID clip addressing, sequence-level
  transcripts and SRT/caption export, a speaker diarization option in the Whisper
  path, macOS validation.

## Acknowledgments

API usage patterns were verified against Adobe's official
[uxp-premiere-pro-samples](https://github.com/AdobeDocs/uxp-premiere-pro-samples).
The setup window's animated icons come from
[Material Line Icons](https://github.com/cyberalien/line-md) by Vjacheslav Trushkin
and [SVG Spinners](https://github.com/n3r4zzurr0/svg-spinners) by Utkarsh Verma,
both MIT-licensed and found via [movin.design/icons](https://movin.design/icons);
see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
This project is not affiliated with or endorsed by Adobe, OpenAI, or Anthropic.

## License

MIT — see [LICENSE](LICENSE).
