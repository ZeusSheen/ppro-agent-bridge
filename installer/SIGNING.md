# Signing the installer (SignPath Foundation, free for open source)

An unsigned `PremiereAgentBridge-Setup-<version>.exe` makes Windows SmartScreen say "Windows protected your PC",
which looks worse than the zip's `.bat` files. A self-signed certificate does not help (Windows does not trust it).
The free route is the **SignPath Foundation**: it signs releases of open-source projects with its own certificate,
provided they are built from source on a trusted build system (GitHub Actions here). Paid alternatives, if it says no:
Certum's open-source code signing certificate (low cost) or Azure Trusted Signing (check availability for an
individual developer in your country first).

This file is the maintainer's checklist and a draft application. Nothing in it is submitted automatically.

## What the Foundation requires (from signpath.org/terms, read 2026-11-06)

| Requirement | Premiere Agent Bridge |
|---|---|
| OSI-approved licence for all components, no commercial dual licensing | MIT (`LICENSE`). Bundled: CPython (PSF License), pip (MIT), websockets (BSD-3-Clause), and with the MCP SDK its MIT/BSD/Apache/PSF dependencies (`THIRD_PARTY_NOTICES.md` to be updated with the bundled list) |
| Built from your own source, in a verifiable way; build scripts count as source | `.github/workflows/release.yml` builds on a GitHub-hosted runner from the tagged commit with `scripts/build_installer.py`; every downloaded input is pinned by SHA-256 (`installer/python.lock.json`, `installer/requirements.lock`) |
| Upstream binaries | Only our own `Setup.exe` is signed. The bundled `python.exe` / DLLs are already signed by the Python Software Foundation and are shipped unchanged |
| Actively maintained, already released in the form to be signed, functionality documented on the download page | Releases 0.2-0.6 as zips; 0.7.0 adds the installer. Apply once 0.7.0's unsigned installer is on the Releases page (the "released in the form to be signed" condition) |
| Reputation check for executable programs | GitHub repository, release history, project website (GitHub Pages) |
| MFA for every team member on SignPath and GitHub | Turn on two-factor authentication on GitHub and on SignPath before applying |
| Roles: Authors, Reviewers, Approvers | One maintainer holds all three (see the policy text below) |
| Certificate issued to SignPath Foundation | The publisher shown by Windows will be "SignPath Foundation", not the project |
| Product name and version in the file metadata, consistent | `VersionInfoProductName=Premiere Agent Bridge`, `VersionInfoProductVersion=<VERSION>` in `PremiereAgentBridge.iss` |
| A "Code signing policy" on the home page and download page, with the attribution, the roles and a privacy statement | Text below, for README and the website |
| No data sent elsewhere without a privacy policy, shown at install, with an opt-out | The bridge talks to 127.0.0.1 only. 0.7's update check (one HTTPS GET of GitHub's latest-release record a day, no identifiers, `PPRO_UPDATE_CHECK=0` turns it off) must be named in the privacy statement and in the installer |
| System changes announced; uninstall provided | Per-user install, no admin rights; start at login is an unticked choice in the wizard; uninstall in Settings > Apps keeps `%USERPROFILE%\.ppro-agent-bridge` unless the user says otherwise |
| No malware, PUPs or hacking tools | A local editing bridge; its security review is `SECURITY_REVIEW.md` |

## Draft application (fill in the bracketed parts, then submit at https://signpath.org/apply)

- **Project name:** Premiere Agent Bridge (ppro-agent-bridge)
- **Repository:** [https://github.com/<owner>/<repo>]
- **Homepage / download page:** [the GitHub Pages site] / [the repository's Releases page]
- **Licence:** MIT
- **Description:** A minimal, auditable bridge that lets an AI coding agent (Claude Code, Codex) or any script drive a
  live Adobe Premiere Pro session for transcript-driven video editing, using only Premiere's official UXP APIs. It is
  a local broker (Python, bound to 127.0.0.1 and ::1 only), a command-line tool and an MCP server, and a UXP panel
  inside Premiere; every edit lands in Premiere's undo history. Windows only. Transcription runs locally (Whisper).
- **What to sign:** one file per release, `PremiereAgentBridge-Setup-<version>.exe`, an Inno Setup installer that
  installs the bridge per user (no admin rights) together with the embeddable CPython from python.org and pinned
  wheels from PyPI. The zip release stays unsigned (scripts only).
- **Build:** GitHub Actions, GitHub-hosted `windows-2025` runner, workflow `.github/workflows/release.yml`, triggered
  by a version tag. It runs `scripts/build_installer.py`, which downloads the embeddable Python and the wheels and
  refuses any file whose SHA-256 differs from the committed lock files, then compiles `installer/PremiereAgentBridge.iss`
  with the runner's Inno Setup. The unsigned installer is uploaded with `actions/upload-artifact`, then submitted with
  `signpath/github-action-submit-signing-request`.
- **Release cadence:** a release every few weeks while the project is young (0.2 to 0.6 in about six weeks), then
  roughly monthly; each one approved by hand.
- **Team:** [name / GitHub account], maintainer: author, reviewer and approver. MFA on GitHub and SignPath.
- **Reputation:** [stars / downloads / links where it is mentioned], releases since [first release date].

## Code signing policy (for README and the website, once approved)

> Free code signing provided by [SignPath.io](https://signpath.io), certificate by
> [SignPath Foundation](https://signpath.org).
>
> Team roles: committers and reviewers: [maintainer]; approvers: [maintainer].
>
> Privacy: this program will not transfer any information to other networked systems unless specifically requested
> by the user or the person installing or operating it. The bridge connects only to 127.0.0.1 / ::1 on this computer.
> The one exception is the update check: once a day the broker asks GitHub for the latest release's version number
> (no cookies, no machine or user identifiers); set `PPRO_UPDATE_CHECK=0` to turn it off.

## SignPath setup after approval

1. In SignPath: add the predefined **GitHub.com** trusted build system to the organization and link it to the project.
2. Create an **artifact configuration** (the default ZIP upload: GitHub Actions artifacts arrive zipped) that signs the
   one installer inside:

   ```xml
   <?xml version="1.0" encoding="utf-8"?>
   <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
     <zip-file>
       <pe-file path="PremiereAgentBridge-Setup-*.exe" max-matches="1">
         <authenticode-sign/>
       </pe-file>
     </zip-file>
   </artifact-configuration>
   ```

   Add metadata restrictions for product name `Premiere Agent Bridge` and the version, as the Foundation asks.
3. Create the signing policy (e.g. `release-signing`) with manual approval, and an API token for a submitter.
4. In GitHub (Settings > Secrets and variables > Actions): secret `SIGNPATH_API_TOKEN`; variables
   `SIGNPATH_ORGANIZATION_ID`, `SIGNPATH_PROJECT_SLUG`, `SIGNPATH_SIGNING_POLICY_SLUG`. The workflow's signing steps
   run only when all four are set; they check the Authenticode signature and write the signed file's SHA-256 (signing
   changes the file, so the hash published with the release must be the signed one).
5. Optional: install the SignPath GitHub App (audit-log checks, private repositories), and set build policies such as
   `require_github_hosted`.

## Until it is approved

- The installer is built and uploaded **unsigned**. SmartScreen shows "Windows protected your PC" on download; the
  user clicks **More info > Run anyway**. Say so next to the download button, with the SHA-256 to compare
  (`Get-FileHash .\PremiereAgentBridge-Setup-<version>.exe -Algorithm SHA256`).
- The zip stays on the Releases page as before (`1 Install ...bat`, `2 Start ...bat`), for anyone who prefers it.
- The AI-assisted install (`installer/AI_INSTALL_PROMPT.md`) checks the SHA-256 before anything runs.
- Later, with a signed installer: a winget manifest (`winget install`).
