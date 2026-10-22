"""scripts/sync_version.py: one VERSION, targeted version edits, generated docs, --check.

The golden excerpt below is the start of the released v0.3.3 SKILL.md and AGENTS.md, byte for byte
(non-ASCII as escapes): the AGENTS.md rule must keep reproducing what shipped.
"""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sv = _load("_test_sync_version", REPO / "scripts" / "sync_version.py")
panel_spec = _load("_test_panel_spec", REPO / "scripts" / "panel_spec.py")

FROZEN_SKILL_HEAD = (
    "---\n"
    "name: ppro-bridge\n"
    "description: Drive Adobe Premiere Pro through the ppro-agent-bridge CLI for transcript-driven, closed-loop "
    "video editing. Use when asked to inspect a Premiere project or timeline, transcribe footage (local Whisper), "
    "read transcripts, export frames / contact sheets / OTIO, import media into bins, place clips or sentence-level "
    "cuts, execute a JSON edit plan (B-roll, graphics), scale clips to fit/fill, interpret footage frame rates, add "
    "timeline markers, remove clips, or assemble a rough cut / best-take edit from talking-head footage. Requires "
    "the broker running and the Agent Bridge UXP panel loaded in Premiere.\n"
    "---\n"
    "\n"
    "# Premiere Pro Agent Bridge (v0.3.3)\n"
    "\n"
    "Control a live Premiere Pro session via CLI. Architecture: `python ppro.py <action>`\n"
    "\u2192 local broker (127.0.0.1:8017) \u2192 UXP panel inside Premiere. Every command prints\n"
    "one JSON object; exit code 0 = success.\n"
    "\n"
    "Working directory for all commands: the `cli/` folder of this repository.\n"
    "\n"
    "## Preconditions (check before anything else)\n"
)
FROZEN_AGENTS_HEAD = (
    "# Agent instructions: Premiere Pro Agent Bridge\n"
    "\n"
    "This file follows the AGENTS.md convention (read by Codex and other coding agents).\n"
    "Claude Code users: the same content is packaged as a skill at `.claude/skills/ppro-bridge/SKILL.md` and is "
    "loaded automatically.\n"
    "\n"
    "# Premiere Pro Agent Bridge (v0.3.3)\n"
    "\n"
    "Control a live Premiere Pro session via CLI. Architecture: `python ppro.py <action>`\n"
    "\u2192 local broker (127.0.0.1:8017) \u2192 UXP panel inside Premiere. Every command prints\n"
    "one JSON object; exit code 0 = success.\n"
    "\n"
    "Working directory for all commands: the `cli/` folder of this repository.\n"
    "\n"
    "## Preconditions (check before anything else)\n"
)

MANIFEST = """{
  "id": "org.example.panel",
  "name": "Example",
  "version": "0.0.1",
  "main": "index.html",
  "host": [
    {
      "app": "premierepro",
      "minVersion": "26.3.0"
    }
  ]
}
"""
SKILL = """---
name: ppro-bridge
description: Example skill.
---

# Premiere Pro Agent Bridge (v0.0.1)

Body text mentioning v0.0.1 stays as it is.
"""
README = """# Premiere Pro Agent Bridge

> **Status: experimental (v0.0.1).** Tested on one build.

## Changelog

- **0.0.1** \u2014 First (v0.0.1) release; this line is history and stays.
"""
MINIMAL_SPEC = {"protocol": 2, "defaultTimeoutSeconds": 30, "actions": []}


def write(root: Path, rel: str, text: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def make_repo(root: Path, version="1.2.3", main_js=True):
    write(root, "VERSION", version + "\n")
    write(root, "plugin/manifest.json", MANIFEST)
    write(root, "broker/broker.py", 'import os\n\nVERSION = "0.0.1"\nPORT = 1\n')
    write(root, "broker/fake_panel.py", 'VERSION = "0.0.1"\nFPS = 60000 / 1001\n')
    if main_js:
        write(root, "plugin/main.js", 'const VERSION = "0.0.1";\nconst x = 1;\n')
    write(root, "SKILL.md", SKILL)
    write(root, "README.md", README)
    write(root, "ppro_bridge/actions.json", json.dumps(MINIMAL_SPEC))


class AgentsRule(unittest.TestCase):
    def test_frozen_v033_excerpt_is_reproduced_byte_for_byte(self):
        docs = sv.generated_docs(FROZEN_SKILL_HEAD)
        self.assertEqual(docs["AGENTS.md"].encode("utf-8"), FROZEN_AGENTS_HEAD.encode("utf-8"))
        for rel in (".claude/skills/ppro-bridge/SKILL.md", ".agents/skills/ppro-bridge/SKILL.md"):
            self.assertEqual(docs[rel].encode("utf-8"), FROZEN_SKILL_HEAD.encode("utf-8"))

    def test_exactly_one_blank_line_after_the_header(self):
        for gap in ("", "\n", "\n\n\n"):
            out = sv.agents_md("---\nname: x\n---\n" + gap + "# Title\n\ntext\n")
            self.assertEqual(out, sv.AGENTS_HEADER + "\n# Title\n\ntext\n")

    def test_header_is_four_lines(self):
        self.assertEqual(sv.AGENTS_HEADER.count("\n"), 4)
        self.assertTrue(sv.AGENTS_HEADER.startswith("# Agent instructions: Premiere Pro Agent Bridge\n\n"))

    def test_crlf_and_bom_are_normalised(self):
        skill = "\ufeff---\r\nname: x\r\n---\r\n\r\n# T\r\nline\r\n"
        docs = sv.generated_docs(sv.lf(skill.lstrip("\ufeff")))
        for text in docs.values():
            self.assertNotIn("\r", text)
        self.assertEqual(docs["AGENTS.md"], sv.AGENTS_HEADER + "\n# T\nline\n")

    def test_a_skill_without_frontmatter_is_refused(self):
        with self.assertRaises(sv.SyncError):
            sv.agents_md("# No frontmatter\n")
        with self.assertRaises(sv.SyncError):
            sv.agents_md("---\nname: x\n# never closed\n")


class SyncOnSyntheticRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def read(self, rel):
        return (self.root / rel).read_bytes().decode("utf-8")

    def test_check_lists_every_drift_then_sync_fixes_them(self):
        make_repo(self.root)
        problems, written = sv.sync(self.root, check=True)
        self.assertEqual(written, [])
        joined = "\n".join(problems)
        for rel in ("plugin/manifest.json", "broker/broker.py", "broker/fake_panel.py", "plugin/main.js",
                    "SKILL.md", "README.md", "ppro_bridge/_version.py", "plugin/lib/spec.js", "AGENTS.md",
                    ".claude/skills/ppro-bridge/SKILL.md", ".agents/skills/ppro-bridge/SKILL.md"):
            self.assertIn(rel + ":", joined)
        self.assertIn("changelog", joined)
        self.assertFalse((self.root / "AGENTS.md").exists(), "--check must not write")
        self.assertEqual(self.read("broker/broker.py"), 'import os\n\nVERSION = "0.0.1"\nPORT = 1\n')

        problems, written = sv.sync(self.root)
        self.assertEqual(problems, [])
        self.assertIn("plugin/manifest.json", written)
        problems, _ = sv.sync(self.root, check=True)
        self.assertEqual(len(problems), 1, problems)  # only the changelog entry, which is written by hand
        self.assertIn("changelog", problems[0])

        write(self.root, "README.md", self.read("README.md").replace(
            "## Changelog\n\n", "## Changelog\n\n- **1.2.3** \u2014 New.\n"))
        self.assertEqual(sv.sync(self.root, check=True)[0], [])
        self.assertEqual(sv.sync(self.root)[1], [], "a second sync writes nothing")

    def test_edits_touch_only_the_version_text(self):
        make_repo(self.root)
        sv.sync(self.root)
        self.assertEqual(self.read("plugin/manifest.json"), MANIFEST.replace('"version": "0.0.1"', '"version": "1.2.3"'))
        self.assertIn('"minVersion": "26.3.0"', self.read("plugin/manifest.json"))
        self.assertEqual(self.read("broker/broker.py"), 'import os\n\nVERSION = "1.2.3"\nPORT = 1\n')
        self.assertEqual(self.read("broker/fake_panel.py"), 'VERSION = "1.2.3"\nFPS = 60000 / 1001\n')
        self.assertEqual(self.read("plugin/main.js"), 'const VERSION = "1.2.3";\nconst x = 1;\n')
        skill = self.read("SKILL.md")
        self.assertEqual(skill, SKILL.replace("(v0.0.1)", "(v1.2.3)"))
        self.assertIn("mentioning v0.0.1 stays", skill)
        readme = self.read("README.md")
        self.assertIn("(v1.2.3).**", readme)
        self.assertIn("First (v0.0.1) release", readme, "only the part above the first ## heading changes")
        self.assertEqual(self.read("ppro_bridge/_version.py"),
                         '# Generated by scripts/sync_version.py from VERSION. Do not edit.\nVERSION = "1.2.3"\n')
        self.assertEqual(self.read("plugin/lib/spec.js"), panel_spec.render(MINIMAL_SPEC, "1.2.3"))
        self.assertEqual(self.read("AGENTS.md"), sv.agents_md(skill))
        self.assertEqual(self.read(".claude/skills/ppro-bridge/SKILL.md"), skill)
        self.assertEqual(self.read(".agents/skills/ppro-bridge/SKILL.md"), skill)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"\r", path.read_bytes(), path.name)

    def test_crlf_files_are_drift_and_get_lf(self):
        make_repo(self.root)
        sv.sync(self.root)
        (self.root / "broker/broker.py").write_bytes(b'import os\r\n\r\nVERSION = "1.2.3"\r\nPORT = 1\r\n')
        problems, _ = sv.sync(self.root, check=True)
        self.assertTrue(any(p.startswith("broker/broker.py:") for p in problems), problems)
        sv.sync(self.root)
        self.assertEqual(self.read("broker/broker.py"), 'import os\n\nVERSION = "1.2.3"\nPORT = 1\n')

    def test_optional_version_lines_may_be_absent(self):
        make_repo(self.root, main_js=False)
        problems, written = sv.sync(self.root)
        self.assertEqual(problems, [])
        self.assertNotIn("plugin/main.js", written)
        self.assertFalse((self.root / "plugin/main.js").exists())

    def test_only_docs_touches_only_the_generated_docs(self):
        make_repo(self.root)
        problems, written = sv.sync(self.root, only_docs=True)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(written), sorted(["AGENTS.md", ".claude/skills/ppro-bridge/SKILL.md",
                                                  ".agents/skills/ppro-bridge/SKILL.md"]))
        self.assertIn("(v0.0.1)", self.read("AGENTS.md"), "--only docs copies SKILL.md as it is")
        self.assertIn('"version": "0.0.1"', self.read("plugin/manifest.json"))
        self.assertEqual(sv.sync(self.root, check=True, only_docs=True)[0], [])

    def test_bad_version_and_ambiguous_lines_are_refused(self):
        make_repo(self.root, version="0.4")
        with self.assertRaises(sv.SyncError):
            sv.sync(self.root, check=True)
        make_repo(self.root)
        write(self.root, "broker/broker.py", 'VERSION = "0.0.1"\nVERSION = "0.0.2"\n')
        with self.assertRaises(sv.SyncError):
            sv.sync(self.root)
        self.assertFalse((self.root / "AGENTS.md").exists(), "nothing is written when a rule fails")

    def test_main_exit_codes(self):
        make_repo(self.root)
        old = sv.REPO
        sv.REPO = self.root
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(sv.main(["--check"]), 1)
                self.assertEqual(sv.main([]), 0)
                self.assertEqual(sv.main(["--check", "--only", "docs"]), 0)
                self.assertEqual(sv.main(["--check"]), 1)  # the changelog entry is still missing
            self.assertIn("DRIFT", out.getvalue())
        finally:
            sv.REPO = old


class RepoIsInSync(unittest.TestCase):
    def test_check_passes_on_the_repo(self):
        """Fails until `python scripts/sync_version.py` has run and README.md has a changelog entry for VERSION."""
        problems, _ = sv.sync(REPO, check=True)
        self.assertEqual(problems, [], "run python scripts/sync_version.py:\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
