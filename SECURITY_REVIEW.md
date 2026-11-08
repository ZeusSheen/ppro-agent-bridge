# Security review

Status (6 Nov 2026): the 0.7 release-gate review is done. Two blue teams reviewed and fixed the code, two red teams
attacked it, a QA pass checked the merge, and the supervisor verified the main fixes live on Premiere 27.1. The fixes
are merged into the 0.7 tree (v0.7-dev); this file describes that tree. 43 findings (section 4): 39 fixed, 2 partly
fixed (DOC-9 / BB-10: the panel does not refuse an existing file by itself), 1 open (QA-9, an installer shortcut),
and BA-4 is a record of what held. Still to do outside the code: the GitHub `release` environment and SignPath policy
(DOC-2), and the first real installer build, which runs the suite under the bundled Python (QA-2) and confirms BB-8
and QA-8. Section 5 lists the risks accepted, with reasons.

## 1. Threat model

### What is protected

- **The user's projects and media.** Writes go through Premiere's API; each is an undo step, but a removal with
  ripple, a relink, `remove_project_items`, an export or render over an existing file is real damage if it is not
  wanted. Files Premiere writes (exports, renders, frames) and files the bridge writes itself (converted WAVs, contact
  sheets, reports, JSON outputs) can land anywhere the user can write.
- **The Windows account's NTLM hash.** Anything that opens a network path (`\\host\share\…`) - Premiere, ffmpeg,
  ffprobe, Pillow, Python's own `resolve()` / `is_file()` / `read_text()` - offers the user's Windows credentials to
  that host.
- **The pairing token** (`%USERPROFILE%\.ppro-agent-bridge\token`): whoever holds it controls the panel, and so
  Premiere, as the CLI does. Since 0.7 it is proved, never sent.
- **Everything that can start a program:** the panel's Start bridge button (`shell.openPath` under manifest
  `launchProcess` `.bat` / `.pyw`), the panel's "View" / "Report a problem" links (`shell.openExternal`, `https`),
  devloader starting UXP Developer Tool (whose service can load any folder as a plugin), `start_bridge.bat` /
  `start_bridge.pyw`, the setup window (it runs `pip install`), the installer and the Python it installs, the release
  workflow that builds and signs the installer.
- **The user's privacy:** project and sequence names (often a client's), media paths (the Windows user name is in
  them), transcripts. They appear in the audit log and in MCP replies (by design: the agent works on them), and must
  not appear in a problem report.

### Who attacks

- **Another Windows user on a shared computer.** Cannot read the token file (its ACL, below). Loopback ports are
  machine-wide: while no broker runs, that user can listen on 127.0.0.1:8017 and [::1]:8017 and play the broker, or
  connect to a running broker and play the panel. Pairing (below) makes both useless without the token.
- **Any local process running as the same user.** It can read the token: the bridge cannot defend against that, only
  avoid widening it (the token on no command line, in no log, reply or report, and never on the wire).
- **A web page in the user's browser:** `fetch` / WebSocket to `127.0.0.1` and `localhost` with its own Origin (or
  `null` from sandboxed frames and `file:` pages), DNS rebinding, `*.localhost` names. Also against services that are
  not ours but that the bridge starts: UXP Developer Tool on 127.0.0.1:14001.
- **A malicious file the agent is given:** an edit plan, a transcript JSON, an SRT, an OTIO file, a `.prfpset`
  preset, a `.mogrt`, a media pack (a FLAC next to a planted symbolic link), a file name with quotes, `$(…)`, `&`,
  `..` or a network path, a symbolic link or junction with a local name that points at a share.
- **Prompt injection:** words in a transcript, a caption file, a web page, a file name or a Premiere marker that tell
  the agent to run a destructive command - through the CLI or the MCP server - or a suggested command line that is
  shell syntax when the agent runs it.
- **The supply chain:** a tampered installer, embedded Python or wheel; a moved tag of a GitHub Action used by the
  release workflow; a forged or intercepted "new version available" answer (needs a certificate the system trusts for
  api.github.com); a file planted beside the bridge's scripts.

### Entry points and the controls on them (0.7, as merged)

| Entry point | Controls in the code | What is left (section 4 / 5) |
|---|---|---|
| Broker WebSocket, controllers (`broker/broker.py`, `ws://127.0.0.1:8017`, `[::1]:8017`) | `listen`: 127.0.0.1 and ::1 only; `check_origin`: no `Origin` or `file://`, else 403; **pairing** (`pair`, `ppro_bridge/auth.py` "Pairing"): hello with a nonce, the broker's challenge nonce, the client's `HMAC-SHA256(token, "ppro-agent-bridge/1 client controller <nonces>")`, then the broker's own proof, which the client checks before sending a request; a wrong or missing proof is AUTH_FAILED, close 1008, and nothing else is said; a 0.5 / 0.6 CLI's plain token still accepted, compared in constant time (`auth.matches`); `PREAUTH_MAX` 1 MiB per message before the proof, `MAX_MESSAGE` 16 MiB after (`set_max_size`); `MAX_UNPAIRED` 64 connections in the hello (more: 1013); `HELLO_TIMEOUT` 10 s for the whole hello; one request at a time (`panel_lock`), BRIDGE_BUSY, PANEL_TIMEOUT keeps the queue closed until the late reply; `write_guard` 300 writes a minute; audit records shortened params, never the hello | Listening sockets without SO_EXCLUSIVEADDRUSE (BA-4, accepted); `apply_ops` counts as one write whatever its ops (at most 200) |
| Broker WebSocket, the panel | The same pairing, both ways: a panel that cannot prove the token is refused with 4401 (`PANEL_AUTH` "required"); plugin id must be `org.pproagentbridge.panel` (4403); a live panel is never replaced (`ping_within`, 4409); `panel_event`: only `skip_version`, only from the registered panel | `PPRO_PANEL_AUTH=legacy` / settings `"panelAuth": "legacy"` lets a 0.6 panel in without proof (section 5) |
| Broker start-up | `listen` + `bind_failure` / `port_holder`: exits when either loopback address of 8017 is held, naming the PID (netstat, tasklist; argument lists); warns when `PPRO_BRIDGE_TOKEN_FILE` points away from the file the panel reads (QA-7) | Protects only when the broker is started; pairing protects the rest |
| UXP Developer Tool's service (`127.0.0.1:14001`, Adobe's; `--load-panel` uses its `/socket/cli`) | Not ours, no controls of its own: measured live (RA-5) it answers "101 Switching Protocols" to a WebSocket handshake with any Origin (none, `http://evil.example`, `null`, `file://`), needs no credential, and can load any folder as a plugin into Premiere. 0.7 (RA-5, QA-1): UDT runs only while the bridge loads the panel - `devloader.watch` polls for a new Premiere process (`Adobe Premiere Pro.exe` / `Adobe Premiere Pro (Beta).exe`, tasklist every 3 s) without UDT, starts UDT minimized for it, loads, waits until the broker sees the panel answering (at most 20 s) and ends the UDT it started (`stop_udt`: its process tree, by PID); also after a failed load, a Premiere that never connects (120 s) and when the broker stops. No UDT is started where the panel needs no load. A loaded panel keeps running once UDT's processes are gone (live: three pings over 15 s, same `loadedAt`) | The seconds UDT runs while it loads (live: 9.3 s); a UDT the user opens themselves (section 5) |
| Broker update check (outbound; `ppro_bridge/update.py`) | `Checker.run` in a daemon thread (`update_loop`), at most once in 24 h, attempts count (`update.json`); `fetch`: HTTPS with `ssl.create_default_context()`, one deadline for the whole request (BA-2; the body read with `read1`), at most 2 MiB, `_SameOriginRedirects`, no cookie handler, User-Agent `ppro-agent-bridge/<version>`, JSON content type required; `parse_release` keeps `tag_name` (x.y.z), `published_at`, `html_url` only through `release_url`; `body` never read; off with `PPRO_UPDATE_CHECK=0` or settings `updateCheck: false` (and the installer's unticked task, DOC-5); `Checker.skip` takes only the announced newer x.y.z; a settings.json that does not parse is never overwritten | The deadline starts after the response headers (QA-7, left as is); `release_url` also admits this repository's `/releases/download/…` asset URLs |
| CLI, scripts, MCP server as clients (`transport.py`, `wsclient.py`, `mcp/connection.py`) | `transport.Pairing` for every client (`call_sync`, `call`, MCP `Connection`): the token is proved, never sent; the request goes only after the broker's proof checks out, else `BROKER_UNVERIFIED` and nothing more is sent (DOC-8); `wsclient.connect`: `ws://` only, `Sec-WebSocket-Accept` checked, no extensions / subprotocols, 64 KiB handshake cap, frame and message caps before reading, a deadline on every read (BA-4); `paths.check` on every path before anything is sent | A non-loopback `PPRO_BRIDGE_URL` is not refused: it gets no token and a request only after proving the token (BA-4, accepted); no token rotation |
| MCP server (`python -m ppro_bridge.mcp`, `ppro_bridge/mcp/`) | stdio only - measured: its only socket calls are asyncio's own self-pipe `socketpair` on 127.0.0.1 (an ephemeral listener closed at once); `_protocol_stdout` moves fd 1 to stderr; one tool call at a time; `redact.scrub` / `scrub_obj` on every result, error, tool list and log line: the token as sent, JSON-escaped, and any 16-character run of it (`FRAGMENT`, BA-3); `tools.py`: arguments parsed by the CLI's own argparse (values starting `--` stay values, unknown and hidden keys refused), GATES - removals, ripples, relink, replace and (DOC-7) `export_aaf` / `export_xml` with `overwrite` over a file, `export_otio` / `export_audio` / `export_transcript` over an existing file - run as a dry run or are refused unless `confirm: true`, `destructiveHint` on all of them; `DATA_RULE` in every description and the instructions; `transcribe` runs as a subprocess with an argument list (`-I` under the bundled Python, QA-8) | The confirm gate stops mistakes, not a hijacked agent (section 5); the MCP SDK imports its web stack (starlette, uvicorn, httpx2, python-multipart, cryptography) even over stdio, but starts no server |
| Panel requests (`plugin/main.js`, `plugin/lib/pairing.js`, `core.js`) | `pairing.js`: the panel reads the token file itself (home from UXP's `os.homedir()`, else a UXP storage folder's path; `fs.readFile`, else `localFileSystem.getEntryWithUrl`), proves it in its hello and runs nothing - sends nothing but the hello and its proof - until the broker's "registered" carries the broker's proof for this handshake; a request first, no proof, a wrong one, a challenge out of turn or no token file drops the socket ("Not paired: …", backoff; fail closed, no override); fixed whitelist of handlers; `checkParams` against actions.json (unknown actions and params refused); no eval; `pathProblem` refuses UNC, device paths and NTFS streams in every `localPath` param; `extensions` rules on export paths (below); project / sequence / fingerprint guards; `MAX_TRANSACTION_ACTIONS` 1000 per undo step | The panel cannot see links (UXP has no lstat / readlink): a request that does not come through the CLI is checked as typed only - pairing keeps such requests out |
| Panel `apply_ops` / `query` (`handlers_ops.js`) | An op is only a key of `OPS` (spec variants: `marker.*`, `clip.set_name`, `clip.set_disabled`, `param.set`, `sequence.set_in_out`, `sequence.set_player_position`, `track.rename`, `bin.create`, `bin.rename`); at most 200 ops; every op planned and every Action built before one commits (`core.runTransaction`), one undo label; `checkConflicts`; read back (VERIFY_FAILED, committed); `query` only `clips`, `clip`, `markers`, `bins`, `sequence`, read strictly | - |
| Panel exports (`handlers_read.js` export_frame / otio / sequence / aaf / xml) | `pathProblem`; export_frame's name a plain file name; spec `extensions` (BB-10, DOC-9): `.otio`, `.xml`, `.aaf`, `.png` frames, `.epr` presets, and for renders the media extensions the preset formats write - `.bat`, `.cmd`, `.ps1`, `.vbs`, `.lnk` refused by the panel itself; AAF jobs one at a time; CLI side (`ops.py`): `paths.check(ext=…)`, `_check_new_output` refuses an existing file without `--overwrite` and a read-only one | The panel does not refuse an existing file by itself (DOC-9 / BB-10 partly fixed): only the CLI asks for `--overwrite` |
| Panel program starts (`plugin/lib/launcher.js`, `support.js`, `start_bridge.*`) | Start bridge: `openPath` only on the user's click (UXP asks first), only a local `start_bridge.bat` (or the `.pyw` beside it) in a folder whose listing shows `broker\broker.py` and `ppro_bridge\__init__.py`; a `launcher.json` path whose folder cannot be listed is not opened (RA-4, BB-7); `start_bridge.pyw`, `broker.py` and the `cli/` scripts drop their own folder from `sys.path` first, the `.bat` files set `NoDefaultCurrentDirectoryInExePath` (RA-3); the start command keeps a path with `&` / `^` whole, `cmd /d` (BB-6); links: `support.openUrl` hands UXP only `allowedUrl` (this repository's release pages and `…/issues/new`), checked again in `cleanUpdate` | Manifest `schemes: ["https"]` lets the panel's code open any https URL: the restriction is `allowedUrl` alone |
| Problem reports (`ppro_bridge/report.py`, `support.js` `buildReport`) | Whitelists: versions, Premiere version / build / UI language, capability and build flags, doctor check names and pass/fail, update version, recent failures as time / action / code / outcome; `report.redact`: home folder to `~`, then anything path-like to `[path]` (the panel's `sanitize` shapes, RB-5); ping's warning only as fixed labels (`warning_kinds`), modules only as identifiers, each audit field only in its shape, actions only when a spec action or CLI command (QA-3); `scrub_token` as a last check; the panel shows the whole text first and asks for no clipboard permission | - |
| FLAC / Opus / Vorbis conversion (`media.convert_to_wav`, from `ops.import_media`, `plan.py`) | `paths.check` (literal and links) first; ffprobe / ffmpeg with argument lists, absolute paths, `-nostdin`, 900 s timeout; ffmpeg writes a file `tempfile.mkstemp` has just created beside the target, never a planted `<name>.wav.part`, renamed only once ffprobe finds PCM of the right length; a link named like the target counts as taken; a WAV holding other audio is never overwritten (RB-1); suggested ffmpeg command lines quote file names so `$(…)` stays text (BB-3) | - |
| Inputs (plan, transcript, SRT, OTIO, presets, params JSON) | `paths.check` on every file the CLI opens, stats or writes, by its literal text and by following links without opening them (`paths.link_problem`, `paths.resolved` keeps a mapped drive's letter; RB-3, RB-6, BB-1, BB-2); plan containment: a `src` that leaves `media_root` is an error step, a `media_root` outside the plan file's folder is BAD_PARAMS, checked on the text and again on the resolved real paths (RB-2, QA-6), `--media-root` is the explicit way out; transcript schema check (`TRANSCRIPT_INVALID`); presets: only the prolog is parsed for the document type, in whatever encoding expat reads (BB-4), parsed with `xml.etree.ElementTree` (no external entities) on the bundled 3.14.10, replayed through `set_params` only; replace_clip reads its OTIO from a fresh temporary folder (BB-9) | - |
| Setup window (`ppro_bridge/setup_ui.py`, local HTTP on 127.0.0.1) | Secret path per run (18 random bytes, constant-time compare); Host exactly `127.0.0.1:<port>`; Origin and `Sec-Fetch-Site` checked; every POST needs `X-Bridge-Setup: 1` (no CORS preflight is answered) and a JSON object; SO_EXCLUSIVEADDRUSE; body cap; actions from a fixed table (pip installs only `requirements.txt`, `pillow`, `faster-whisper`, with `--disable-pip-version-check`); shortcut script quotes paths for PowerShell with every quote character it treats as one (BB-5) | Optional packages come unpinned from PyPI (section 5) |
| Token file, audit log, settings (`%USERPROFILE%\.ppro-agent-bridge`) | Measured on this machine (icacls, 6 Nov, permissions only): folder, `token` and `audit` carry only the profile's inherited SYSTEM, Administrators and the user - no other standard user can read them. 0.7 (DOC-6): a token file, data folder or audit folder the bridge creates gets an ACL of its own, this user and SYSTEM, nothing inherited (`auth.restrict_to_owner`: icacls with an argument list), so it stays private also where `PPRO_BRIDGE_TOKEN_FILE` / `PPRO_AUDIT_DIR` move it; `ensure_token`: exclusive create, ACL set before the token is written, `secrets.token_urlsafe(32)`; audit keeps 30 days and records no token (refusals only missing / wrong / malformed / no_proof); tests never touch the real folder (an audit hook in `tests/helpers.py`, QA-4) | Existing files keep their ACL; administrators and same-user processes can read the token (section 5) |
| Manifest (`plugin/manifest.json`) | `network.domains: ["ws://localhost/"]`; `launchProcess.extensions: [".bat", ".pyw"]`, `schemes: ["https"]` (UXP asks the user); no clipboard permission; `localFileSystem: fullAccess` | `fullAccess` kept: the panel must read the token (section 5) |
| Installer (`installer/PremiereAgentBridge.iss`) | Per user (`PrivilegesRequired=lowest`, `{userpf}`); every start of the bundled Python with `-I` (no user site, no `PYTHON*` variables; QA-8); "Check GitHub once a day" task, ticked by default, unticked writes `updateCheck: false` through `update.write_setting` (DOC-5); start at login an unticked task; `RemoveDroppedFiles` deletes only `SafeRelativePath` names of the old file list; `{app}\python` and `{app}\dist` deleted whole only from a folder of ours (BB-8); shortcuts removed only when their target is inside `{app}`; the data folder deleted only on an explicit Yes; unsigned until SignPath approves | The install takes over a zip install's Start-menu shortcut and the uninstall deletes it (QA-9, open) |
| Installer build and release (`scripts/build_installer.py`, `installer/*.lock`, `.github/workflows/release.yml`) | CPython **3.14.10** embeddable (sha256 `b553e7da…361c`, from python.org's `index-windows.json`; DOC-3), pip **26.3** (DOC-4); `fetch`: https only, SHA-256 checked (cached or fresh); `parse_requirements_lock`: `name==version` + `--hash` only; `check_wheels`; `pip download/install --isolated --require-hashes --only-binary :all:`, install `--no-index`; `extract_python` refuses entries outside the folder; workflow: every action pinned to a commit SHA (checkout v5.1.0, setup-python v6.3.0, upload-artifact v4.6.2, signpath v3.0) with Dependabot, `permissions: {}` at the top and per job, `persist-credentials: false`, no `pull_request_target`, tag checked against VERSION, the whole suite run under the staged bundled Python with `-I` before any upload or signing (QA-2), signing only for a version tag in its own job behind `environment: release`, the SignPath token an environment secret the build job never has (DOC-1, DOC-2); `ChineseSimplified.isl` byte-identical to Inno Setup's tag `is-6_7_1` | The `release` environment's reviewer and tag rule, and the SignPath policy's ref, are GitHub / SignPath settings for the maintainer (DOC-2) |
| Prompt injection | SKILL.md: no editing without a digest or the user's timecodes, dry runs first, writes on production projects only with the user's confirmation; MCP: `DATA_RULE`, confirm gate, `destructiveHint` so the MCP client asks; every write an undo step with a label; suggested commands shell-safe (BB-3) | The bridge cannot tell an injected instruction from the user's (section 5) |

### Rules 0.7 was designed to, and where the code stands

- **Pairing (new in the review):** the token is proved with HMAC-SHA256 over both nonces, the client first, then the
  broker; neither side's proof can be obtained without the token, and nothing but the challenge is said to an
  unproven client. Holds for the panel, the CLI, scripts and the MCP server; verified live (section 3).
- **MCP server:** stdio only (measured); a client like the CLI; the token in no result, error, log line or
  description; delete / ripple / replace / overwrite only with `confirm: true`; data-not-instructions in every
  description. Holds.
- **Update check:** as designed (row above); on by default (the user's decision of 6 Nov), named and switchable in
  the installer. The panel stays on `ws://localhost/` and opens only `allowedUrl` pages.
- **Problem report:** as designed; paths and names cut by shape, not only the home folder.
- **Installer:** per user, no admin, Python and wheels pinned and SHA-256-checked at build, current versions, the
  suite run under the bundle, data kept unless the user says Yes. Signing through SignPath Foundation once approved;
  until then the zip stays available.
- **Website:** static pages, no third-party scripts or analytics. Not part of this repository; not reviewed here.

### Open issues carried from 0.6

1. *Fixed in 0.7 (RA-2):* while no broker ran, a program listening on `localhost:8017` received the real panel and
   could send it whitelisted commands. The panel now pairs (`plugin/lib/pairing.js`, row above) and runs nothing
   before the broker's proof. A broker older than 0.7 cannot prove it: a 0.7 panel needs a 0.7 broker. Verified live:
   a fake broker on 8017 got four hellos without the token and no answered request.
2. *Fixed in 0.7 (RA-1):* the panel registered without the token. The broker now registers a panel only after it
   proved the token; a wrong proof is refused with 4401. A 0.6 panel (no pairing) is refused with 4401 by default
   ("reload or reinstall the panel"; verified live); `PPRO_PANEL_AUTH=legacy` or settings.json
   `"panelAuth": "legacy"` lets one in (a warning at start and at each registration, audited `"token": "legacy"`),
   which reopens this issue while it is set. Default closed because the broker and the panel ship together.
   *Controller leg (DOC-8, fixed):* the CLI, `transport.call` and the MCP server sent the token itself to whatever
   answered on the port; they now pair the same way. The broker still takes a 0.5 / 0.6 CLI's plain token (those old
   clients risk their own token). Not covered: a token given only in `PPRO_BRIDGE_TOKEN` (the panel cannot read
   environment variables: it pairs only when the file holds the same token; the broker warns).
3. *Fixed in the CLI (RB-3 / BB-1):* a local path that is a symbolic link or junction to a share passed the UNC check
   (and the CLI's own `resolve()` contacted the host). `paths.check` now follows each link by reading it (os.lstat /
   os.readlink, never opening the target) and refuses one that ends on a network or device path, for every path the
   CLI hands on or opens itself (RB-6, BB-2). The panel cannot see links; issues 1-2 are the fix for requests that do
   not come through the CLI.
4. *Kept (section 5):* `localFileSystem: fullAccess`. Since 0.7 the panel needs it to read the pairing token in the
   user's profile; whether Premiere's import and export calls keep working under `plugin` is still not measured.

## 2. Blue team: code review

- **Blue A (and A2) - authentication and the hub.** `broker/broker.py`, `ppro_bridge/auth.py`, `transport.py`,
  `wsclient.py`, `mcp/connection.py`, `mcp/redact.py`, `mcp/tools.py`, `update.py`, `audit.py`, `devloader.py`,
  `setup_ui.py`, `plugin/main.js`, `plugin/lib/pairing.js`, `broker/fake_panel.py`. Questions: does each end prove the
  token without sending it; limits before the proof; the update check's timeouts; the token in logs, errors, audit
  records, MCP replies; the confirm gate; UDT's exposure. Fixed RA-1, RA-2, RA-5, DOC-6, DOC-7, DOC-8, BA-1 to BA-3;
  BA-4 records what held (wsclient's framing rules, the setup window's CSRF guards, the update check's TLS and
  redirects, the confirm gate's argument mapping, no token in the audit log).
- **Blue B (and B2) - paths, files, inputs, launchers, installer.** `ppro_bridge/paths.py`, `core.js`
  (`pathProblem`, `extensions`), `media.py`, `ops.py` (every path caller), `plan.py`, `frames.py`, `prfpset.py`,
  `report.py`, `replace.py`, `doctor.py`, `launcher.js`, `start_bridge.*`, `cli/*.py`, `installer/*`,
  `scripts/build_installer.py`, `release.yml`. Questions: links and junctions, traversal in plans, writing through
  planted links, every input and output path through one check, extension rules in the panel, shell syntax in
  suggested commands and start commands, planted modules beside scripts, the installer's deletes, the pinned
  versions. Fixed RA-3, RA-4, RB-1 to RB-6, BB-1 to BB-9, DOC-1 to DOC-5, QA-2 to QA-4, QA-6 to QA-8; DOC-9 / BB-10
  partly.
- **QA - the merge.** Re-ran every repro and the suite on the merged tree; found QA-1 (a fix missing from the merge),
  QA-2 (the bundle never run before release), QA-3 to QA-8, and with the supervisor QA-9.
- **Doc - threat model, supply chain, ACLs.** Every pin in `installer/requirements.lock` checked against PyPI's
  `vulnerabilities` field and GitHub's advisory ranges (6 Nov 2026): only pip 25.3 was affected (DOC-4, now 26.3);
  websockets 16.1, mcp 2.3.0, starlette 1.10.0, uvicorn 0.54.0, sse-starlette 3.6.0, pydantic 2.14.3, pyjwt 2.15.1,
  cryptography 50.0.2, httpx2 2.16.0, python-multipart 0.0.33, anyio 4.14.2 and the rest clean at their pins; every
  wheel hash matches PyPI's file; the four GitHub Actions have no advisory. Over stdio the MCP server loads starlette,
  uvicorn, sse_starlette, httpx2, python_multipart and cryptography (`import mcp.types` pulls them in) but starts no
  server; pyjwt, h11, jsonschema and truststore are not loaded. Every subprocess call in the code reviewed uses an
  argument list; none uses a shell.

## 3. Red team: attack simulation

Only against this machine's bridge, with scripts written for the purpose (stdlib, `findings/repro`, each on a free
port with a temporary token and state folder, never 8017 or the real token); no attack tools downloaded, no system
or security settings changed, writes only in scratch folders. NTLM exposure was shown with unreachable addresses
(TEST-NET 192.0.2.1, `pab-guard.invalid`) and never by contacting a host; no hash was captured. Scenarios that need
port 8017, a live Premiere or a second user were run by the supervisor (3.1).

| Scenario | Attacker | Result |
|---|---|---|
| Port pre-binding before the broker starts | another user / local program | Held: the broker exits 1 and names the holder (`held_broker_controls.py`) |
| Panel impersonation | local program | RA-1 (fixed: pairing) |
| Fake broker: driving the panel, harvesting a client's token | another user / local program | RA-2, DOC-8 (fixed: pairing); DOC-9 (exports, partly fixed). The panel's own UNC check held against a hostile broker (RA-2) |
| Client authentication: no, wrong, whitespace-padded token; wrong plugin id; wrong panel token; a second panel | local program | Held: AUTH_FAILED (exact, constant time), 4403, AUTH_FAILED, 4409 (`held_broker_controls.py`, now with pairing) |
| Browser-style requests: `Origin: http://evil`, `Origin: null` | web page | Held: 403 at the handshake |
| UXP Developer Tool's service | web page / local program | RA-5 (fixed: UDT runs only while loading; residual in section 5) |
| Resource exhaustion: nested hello, big hello, idle connections, trickling update answer | local program / network | Nested hello held; BA-1 (big hello, connection flood), BA-2 (trickle) fixed |
| MCP server: token in replies, confirm gate, arguments | prompt injection | BA-3 (token fragment), DOC-7 (ungated overwrites) fixed; argument mapping held (BA-4) |
| Update check: forged answer, rogue `skip_version`, settings | network / impostor panel | Held: a non-version, an unannounced version and a broken settings.json are refused or left alone (`held_r2_misc.py`); the impostor that could send `skip_version` is gone with RA-1 |
| Malicious inputs: plan, transcript, SRT, params JSON, preset | malicious file | RB-2, QA-6 (plan containment), RB-6, BB-2 (input and output paths), BB-4 (preset type check) fixed |
| Links and junctions | malicious file | RB-3 / BB-1, RB-1 fixed |
| FLAC conversion | malicious file | RB-1 fixed |
| Exports | prompt injection / fake broker | RB-4 fixed; DOC-9 partly fixed (no panel-side overwrite check) |
| Shell syntax in names and paths | malicious file / prompt injection | BB-3 (suggested ffmpeg commands), BB-5 (PowerShell shortcut script), BB-6 (cmd start command) fixed; doctor's shortcut against a folder named `x'; $(ni PS_PWNED.txt); '` held (`held_r2_misc.py`) |
| Planted files beside the bridge | local program | RA-3 (planted module / python.exe), RA-4 / BB-7 (launcher.json), QA-8 (user site) fixed |
| Audit log injection | local program | Held: one record is one line of valid JSON, newlines escaped (`held_r2_misc.py`) |
| Setup window actions | web page / local program | Held: a crafted action name is refused (400); pip actions take no argument (`held_r2_misc.py`) |
| `apply_ops` / `query` | token holder | Held in review: whitelist by spec variant, all-or-nothing transaction |
| Problem report | privacy | RB-5, QA-3 fixed |
| Installer and release | supply chain | DOC-1 to DOC-5, QA-2, BB-8 fixed; QA-9 open |

### 3.1 Live verification (supervisor, 6 Nov 2026, Premiere 27.1)

- **Pairing on 27.1:** the panel read the token with `os.homedir()` and `fs.readFile`, paired with the broker and
  worked normally.
- **A 0.6 panel** (no pairing) was refused by the 0.7 broker.
- **Fake broker on 8017** (the real broker stopped): the panel sent four hellos, none with the token, and answered no
  request.
- **UXP Developer Tool (RA-5):** 127.0.0.1:14001 accepted a WebSocket handshake with any Origin; after the fix, UDT was
  open 9.3 s during a load and then closed; the loaded panel kept running.
- **Start bridge** worked with the new launcher check (bridge folder listing).
- **Exports** worked on C: and D: under the new path and extension rules.
- **The CPython 3.14.10 bundle** (staged installer layout, `-I`): the suite passes, 1091 tests OK, 7 skipped (QA-2).
- **Installer test:** found QA-9 (Start-menu shortcut taken over, then deleted on uninstall); the user's shortcut was
  recreated.

## 4. Findings

Status as each finding file (`findings/<ID>.md`) says on 6 Nov 2026. "Repro" scripts are in `findings/repro`; each
prints VULNERABLE before the fix and SAFE after it unless noted.

| ID | Severity | Finding | Status | Regression test / proof |
|---|---|---|---|---|
| RA-1 | high | A local program registers as the panel without the pairing token | fixed-in-blueA | `tests/test_connection.py` PairingTest, PairingPolicyTest; repro RA-1 SAFE; `held_broker_controls.py` SAFE; live: 0.6 panel refused |
| RA-2 | high | The panel does not authenticate the broker (a port holder drives Premiere) | fixed-in-blueA | `test_connection.py` PanelConnectionTest.test_an_unproven_broker_gets_nothing_run; repro RA-2 SAFE; live: fake broker on 8017 |
| RA-3 | medium | start_bridge.pyw / .bat run a planted file on launch | fixed-in-blueB | `tests/test_launcher.py` PlantedModuleTest; repro RA-3 SAFE |
| RA-4 | medium | A planted launcher.json makes Start bridge open an attacker-chosen file (= BB-7) | fixed-in-blueB | `tests/test_launcher.py`; repro RA-4 SAFE; live: Start bridge with the check |
| RA-5 | medium | The bridge auto-starts and keeps alive UDT's unauthenticated load service (14001) | fixed-in-blueA2 (merged, QA-1) | `tests/test_devloader.py` UdtWindowTest; `RA-5_fix_udt_window.py` SAFE (`RA-5_udt_autostart_exposure.py` models Adobe's protocol: VULNERABLE by design); live: 9.3 s, then closed |
| RB-1 | high | FLAC/Opus->WAV conversion overwrites a file through a planted `.wav.part` link | fixed-in-blueB | `tests/test_input_hardening.py` WavPartFileTest; repro RB-1 SAFE |
| RB-2 | low | Plan `media_root` / `src` with `..` or absolute paths escape the plan folder | fixed-in-blueB | `test_input_hardening.py` PlanContainmentTest (the repro models plan.run and still prints VULNERABLE by design) |
| RB-3 | high | A local-named link / junction to a share defeats the UNC guard (= BB-1) | fixed-in-blueB | `tests/test_path_guard.py` LinkGuardTest; repro RB-3 SAFE |
| RB-4 | medium | `export_frames --sheet` skips `paths.check` | fixed-in-blueB | `test_path_guard.py` (every path option); repro RB-4 SAFE |
| RB-5 | low | The problem report redacted only the home folder | fixed-in-blueB | `test_input_hardening.py` ReportPathsTest; repro RB-5 SAFE (with QA-3) |
| RB-6 | medium | Input-file paths read with no `paths.check` (UNC -> NTLM) | fixed-in-blueB | `test_path_guard.py` (a share in every path option, refused before any file call); repro RB-6 SAFE |
| BA-1 | low | Before any proof the broker took 16 MiB messages from unlimited connections | fixed-in-blueA | `test_connection.py` test_a_big_hello_is_cut_off_before_the_proof, test_idle_connections_in_the_hello_are_capped, test_a_malformed_hello_and_a_silent_client_get_nothing; repro BA-1 |
| BA-2 | low | The update check's 5 s timeout was per read (a trickling answer ran for hours) | fixed-in-blueA | `tests/test_update.py` test_a_trickling_answer_ends_at_the_whole_request_deadline (QA-7); repro BA-2 |
| BA-3 | low | MCP redaction missed a cut-off quote of the token | fixed-in-blueA | `tests/test_mcp.py` test_a_cut_off_token_is_redacted_too; repro BA-3 |
| BA-4 | info | Reviewed and held: network, IPC, authentication | accepted (info) | `tests/test_wsclient.py` FakeBrokerTest (hostile server) |
| BB-1 | high | A path through a link to a share passed every check; the CLI's own resolve() followed it (= RB-3) | fixed-in-blueB | `test_path_guard.py` LinkGuardTest; repro RB-3 SAFE |
| BB-2 | medium | Files the CLI opens or writes itself skipped the path checks; JSON outputs took any path | fixed-in-blueB | `test_path_guard.py` (a registry of every string option); `BB_repros.py BB-2` SAFE |
| BB-3 | medium | Suggested ffmpeg commands embedded file names verbatim (`$(…)` is shell syntax) | fixed-in-blueB | `test_input_hardening.py` ShellSafeCommandTest; `BB_repros.py BB-3` |
| BB-4 | low | The .prfpset type check was a UTF-8 byte search over 64 KiB | fixed-in-blueB | `test_input_hardening.py` PresetDoctypeTest; `BB_repros.py BB-4` |
| BB-5 | low | doctor's shortcut script quoted paths for PowerShell with `'` only | fixed-in-blueB | `tests/test_doctor.py` (a folder named `it's Bob's; $(x)`); `BB_repros.py BB-5` |
| BB-6 | low | The setup window's and start_bridge.pyw's start command split a path holding `&` or `^` | fixed-in-blueB | `tests/test_setup_ui.py` test_start_command_keeps_a_path_with_cmd_characters_whole (real cmd.exe), `test_launcher.py`; `BB_repros.py BB-6` |
| BB-7 | low | launcher.json's path was opened unchecked (= RA-4) | fixed-in-blueB | `test_launcher.py` (shares, device path, stream, other.bat, planted folder) |
| BB-8 | low | Uninstalling deleted `{app}\python` and `{app}\dist` whole, even in a folder of the user's own | fixed-in-blueB (needs the ISCC build and an install/uninstall into a non-empty folder to confirm) | `tests/test_build_installer.py` test_uninstall_removes_python_and_dist_whole_only_from_a_folder_of_ours (static) |
| BB-9 | info | replace_clip read its OTIO export from a fixed name in %TEMP% | fixed-in-blueB | `test_input_hardening.py` OtioTempFileTest (QA-7) |
| BB-10 | info | The panel's export rules are now spec rules; the panel's overwrite check is still missing | partly fixed-in-blueB (see DOC-9) | `tests/test_panel.py` REFUSED_PATHS, `tests/test_spec.py` |
| DOC-1 | medium | release.yml ran third-party actions by mutable tag, one with the SignPath token | fixed-in-blueB | `test_build_installer.py` ReleaseWorkflowTest; SHAs verified from GitHub's API |
| DOC-2 | low | A manual run on any branch could submit an installer for signing | fixed-in-blueB (the maintainer creates the `release` environment and limits the SignPath policy to `refs/tags/v*`) | `test_build_installer.py` ReleaseWorkflowTest (workflow shape) |
| DOC-3 | medium | The installer bundled CPython 3.12.10 without 3.12's later security fixes | fixed-in-blueB | Lock on 3.14.10 (hash from index-windows.json); live: suite under the staged bundle, 1091 OK |
| DOC-4 | low | The installer bundled pip 25.3 (five advisories) | fixed-in-blueB | Lock on pip 26.3 (hash from PyPI); PyPI / GitHub advisory check of every pin |
| DOC-5 | low | The installer did not mention the update check or offer the opt-out | fixed-in-blueB | `test_build_installer.py` (the settings writer against a temp settings.json) |
| DOC-6 | low | Token file and audit log relied on the folder's inherited ACL | fixed-in-blueA | `tests/test_auth_audit.py` test_a_created_token_file_and_folder_are_this_users_only |
| DOC-7 | low | MCP: export_aaf / export_xml replaced an existing file with no confirm gate | fixed-in-blueA | `tests/test_mcp.py` test_the_gated_set_is_the_documented_one, test_conditional_gates |
| DOC-8 | high | Clients sent the token itself to whatever answered on the broker's port | fixed-in-blueA | `tests/test_wsclient.py` FakeBrokerTest, `test_mcp.py` hello test; repro DOC-8 SAFE; live: fake broker on 8017 got no token |
| DOC-9 | medium | The panel wrote exports to any path and extension | partly fixed-in-blueB (extensions; no panel-side overwrite check) | `test_panel.py` REFUSED_PATHS (`.bat` `.cmd` `.ps1` `.vbs` `.lnk` refused), `test_spec.py`; live: exports on C: and D: |
| QA-1 | medium | The RA-5 fix was not in the merged tree | fixed (supervisor merge) | `RA-5_fix_udt_window.py` SAFE on v0.7-dev; live 9.3 s |
| QA-2 | medium | The bundled CPython was never run before release | fixed-in-blueB2 | `test_build_installer.py` ReleaseWorkflowTest (the suite step and its place); 1091 OK under the staged 3.14.10 bundle |
| QA-3 | low | RB-5 fixed paths only; a project or sequence name still passed | fixed-in-blueB2 | `tests/test_report.py` NamesStayOutTest; repro RB-5 SAFE |
| QA-4 | low | Tests reached the real `%USERPROFILE%\.ppro-agent-bridge` (mkdir) | fixed-in-blueB2 | `tests/helpers.py` audit hook for the whole suite; `test_doctor.py` DataFolderTest |
| QA-5 | low | `--job ID --output <mapped drive>` refused after the `paths.resolved` change | fixed-in-qa | test_ops, test_path_guard, test_input_hardening, test_mcp (212 tests) |
| QA-6 | low | RB-2's containment was lexical: a junction inside the plan folder led out | fixed-in-blueB2 | `test_input_hardening.py` test_a_junction_inside_the_plan_folder_does_not_lead_out |
| QA-7 | info | Small leftovers of the merge | fixed-in-blueB2 | Unit tests for BA-2 and BB-9 (above) |
| QA-8 | low | The bundled Python's ._pth turned site on (user site, planted .pth) | fixed-in-blueB2 (needs the real build: the release workflow's suite step runs with `-I`) | `test_build_installer.py` BundledPythonIsolatedTest |
| QA-9 | low | The installer takes over a zip install's Start-menu shortcut, and its uninstall deletes it | open | Live (supervisor); fix proposed in QA-9.md |

## 5. Accepted risks

- **UDT after this review (found in real use, 8 Nov 2026).** One failed `tasklist` (the process list) sent the broker
  into the old way meant for systems without one: UDT started, not closed, and restarted when it went away, for the
  broker's lifetime (seen in the audit log: open at least 11 minutes). Fixed: on Windows a list that cannot be read
  starts nothing until it reads again (`tests/test_devloader.py`, UdtWindowTest).
- **UXP Developer Tool while it loads the panel (RA-5 residual).** UDT's service (Adobe's, 127.0.0.1:14001, any
  Origin, no credential) is up for the seconds the bridge needs to load the panel into a Premiere that started (live:
  9.3 s; bounds in the code: 60 s for UDT to come up, 120 s for Premiere to connect, 20 s for the panel to answer). A
  same-user program, or a web page the browser lets reach loopback, can use that window. Accepted because loading
  needs UDT and the window is now seconds instead of the whole session; an installed .ccx panel needs no UDT at all.
  Upstream, UDT's service should check Origin and require a credential.
- **A UDT the user opens themselves.** It is used and left open (the bridge closes only a UDT it started; if the user
  opens UDT while the bridge's own runs, Windows hands them that instance, which the bridge closes after its load),
  and so is a UDT left running when the broker is killed rather than stopped. The user's own choice and outside the
  bridge's control.
- **`PPRO_PANEL_AUTH=legacy` while it is set.** A panel that cannot prove the token (0.6 and older) is registered,
  which reopens open issue 2. Accepted as an explicit, logged and audited opt-in for a transition; the default is
  closed.
- **`localFileSystem: fullAccess` is kept.** The panel must read the pairing token in the user's profile, which the
  `plugin` level cannot reach. The marginal power is small: every handler already hands Premiere paths that it reads
  and writes with the user's full rights, and `fullAccess` adds no rights beyond the user's account. The panel's own
  file reads stay few (its plugin folder, the listing of the bridge folder, the token file), and no handler returns
  the content of a file read through UXP; with pairing, only a token holder can send the panel anything. Revisit when
  Premiere's import and export calls are measured under `plugin`.
- **Same-user processes can read the token, by design.** A per-user secret cannot protect against the user's own
  programs; they can drive Premiere as the CLI does. The bridge only avoids widening it: the token is never sent,
  logged, put on a command line, in a reply or a report, and its file is this user's and SYSTEM's only.
- **Administrators can read the token and the audit log.** Windows gives them every file.
- **The MCP confirm gate does not stop a hijacked agent.** The agent that read injected text is the one that passes
  `confirm: true`. The gate catches mistakes; the protection against injection is the MCP client's own approval of
  destructive tools (`destructiveHint`), the dry-run reply, and undo.
- **The panel does not refuse an existing export file by itself (DOC-9 / BB-10).** UXP's existence check for an
  arbitrary path is unmeasured on 27.1. The CLI asks for `--overwrite`, the panel keeps the extension, and pairing
  keeps other programs off the panel.
- **Optional packages from the setup window come unpinned from PyPI** (Pillow, faster-whisper): the same trust as the
  user running `pip install`, with the bundled pip current.
- **The update check reveals the user's IP address to GitHub once a day.** It sends nothing else and can be turned
  off, in the installer too.
- **Listening sockets without SO_EXCLUSIVEADDRUSE; a non-loopback `PPRO_BRIDGE_URL` is not refused (BA-4).** Another
  user cannot bind beside the broker (asyncio sets no SO_REUSEADDR on Windows); a same-user process could, but can
  read the token anyway and learns nothing from a connection it takes; a remote URL gets no token and a request only
  after proving the token.
