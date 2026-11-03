"""codeHash: a fingerprint of the panel's files, so the CLI can tell a stale (not reloaded) panel.

The panel computes the same value from its plugin folder and reports it in ping. FNV-1a, 32 bit, lowercase 8-hex,
over index.html, main.js, manifest.json and every lib/*.js, sorted by relative POSIX path; for each file the bytes
of `relpath (UTF-8) + NUL + file bytes + NUL`.
"""

from pathlib import Path

FIXED_FILES = ("index.html", "main.js", "manifest.json")
FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193


def _fnv1a(h: int, data: bytes) -> int:
    for byte in data:
        h = ((h ^ byte) * FNV_PRIME) & 0xFFFFFFFF
    return h


def code_hash(plugin_dir) -> str | None:
    """The panel code hash of a plugin folder; None when the folder or one of the fixed files is missing."""
    root = Path(plugin_dir)
    if not root.is_dir() or not all((root / f).is_file() for f in FIXED_FILES):
        return None
    rel = list(FIXED_FILES)
    lib = root / "lib"
    if lib.is_dir():
        rel += [f"lib/{p.name}" for p in lib.iterdir() if p.is_file() and p.suffix == ".js"]
    try:
        return hash_files({name: (root / name).read_bytes() for name in rel})
    except OSError:
        return None


def hash_files(files: dict) -> str | None:
    """The same hash over in-memory files {relative POSIX path: bytes}, e.g. a .ccx package before it is written.
    Only the files the panel hashes count; None when one of the fixed files is missing."""
    if not all(f in files for f in FIXED_FILES):
        return None
    hashed = [n for n in files if n in FIXED_FILES or (n.startswith("lib/") and n.count("/") == 1
                                                         and n.endswith(".js"))]
    h = FNV_OFFSET
    for name in sorted(hashed):
        h = _fnv1a(h, name.encode("utf-8") + b"\0" + files[name] + b"\0")
    return f"{h:08x}"
