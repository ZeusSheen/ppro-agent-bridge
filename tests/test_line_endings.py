"""Every text file of the repo uses LF line endings (no carriage returns).

In a git checkout the files are `git ls-files` (tracked plus untracked-but-not-ignored); otherwise the tree is
walked, skipping caches, local settings and the working-output patterns of .gitignore.
"""

import helpers  # noqa: F401  (first: points PPRO_BRIDGE_URL at a dead port, so nothing here can reach 8017)

import fnmatch
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "frames", "reports", "research_notes"}
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".tif", ".tiff", ".mp4", ".mov", ".mxf",
          ".wav", ".mp3", ".m4a", ".aac", ".zip", ".gz", ".7z", ".pdf", ".pyc", ".pyo", ".ccx", ".prproj",
          ".woff", ".woff2", ".ttf", ".otf", ".exe", ".dll", ".so"}


def _gitignore_patterns():
    path = REPO / ".gitignore"
    if not path.is_file():
        return [], []
    ignore, keep = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        (keep if line.startswith("!") else ignore).append(line.lstrip("!"))
    return ignore, keep


def _matches(rel: str, pattern: str) -> bool:
    pattern = pattern.rstrip("/")
    if "/" in pattern:
        return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.replace("**/", ""))
    return any(fnmatch.fnmatch(part, pattern) for part in rel.split("/"))


def _walk():
    ignore, keep = _gitignore_patterns()
    for path in REPO.rglob("*"):
        rel = path.relative_to(REPO).as_posix()
        if not path.is_file() or SKIP_DIRS & set(rel.split("/")[:-1]):
            continue
        if any(_matches(rel, p) for p in ignore) and not any(_matches(rel, p) for p in keep):
            continue
        yield rel


def _git_files():
    git = shutil.which("git")
    if not git:
        return None
    try:
        top = subprocess.run([git, "rev-parse", "--show-toplevel"], cwd=REPO, capture_output=True, text=True,
                             timeout=30)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != REPO:
            return None
        out = subprocess.run([git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def text_files():
    files = _git_files()
    if files is None:
        files = list(_walk())
    for rel in sorted(files):
        path = REPO / rel
        if not path.is_file() or path.suffix.lower() in BINARY or SKIP_DIRS & set(rel.split("/")[:-1]):
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        yield rel, data


class LineEndings(unittest.TestCase):
    def test_no_carriage_returns(self):
        checked, bad = 0, []
        for rel, data in text_files():
            checked += 1
            if b"\r" in data:
                line = data[:data.index(b"\r")].count(b"\n") + 1
                bad.append(f"{rel}:{line}")
        self.assertGreater(checked, 10, "found suspiciously few text files")
        self.assertEqual(bad, [], "CR line endings (write files with newline='\\n' / write_bytes): " + ", ".join(bad))

    def test_the_walker_skips_ignored_output(self):
        self.assertTrue(_matches("frames/a.png", "frames/"))
        self.assertTrue(_matches("work/tr_clip.json", "tr*.json"))
        self.assertTrue(_matches("tests/live/fixtures/tx_fixture_en.json", "tests/**/*.json"))
        self.assertFalse(_matches("README.md", "*.otio"))


if __name__ == "__main__":
    unittest.main()
