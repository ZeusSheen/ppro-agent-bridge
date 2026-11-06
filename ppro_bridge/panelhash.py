"""codeHash: a fingerprint of the panel's files, so the CLI can tell a stale (not reloaded) panel.

The panel computes the same value from its plugin folder and reports it in ping. FNV-1a, 32 bit, lowercase 8-hex,
over index.html, main.js, manifest.json and every lib/*.js, sorted by relative POSIX path; for each file the bytes
of `relpath (UTF-8) + NUL + file bytes + NUL`.
Hashing those ~400 KB byte by byte in Python takes ~30 ms of every `ping`, so code_hash can remember the result in
a cache file next to the files' sizes and modification times, and reads the files again only when one changed.
"""

import json
import os
import time
from pathlib import Path

FIXED_FILES = ("index.html", "main.js", "manifest.json")
FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193
SETTLED_NS = 2_000_000_000  # a hash is cached only once every file is this much older than now (a racy write)


def _fnv1a(h: int, data: bytes) -> int:
    for byte in data:
        h = ((h ^ byte) * FNV_PRIME) & 0xFFFFFFFF
    return h


def code_hash(plugin_dir, cache=None) -> str | None:
    """The panel code hash of a plugin folder; None when the folder or one of the fixed files is missing.
    cache: a JSON file (best effort: unreadable, stale or unwritable is the same as none) holding the last hash with
    the folder and every hashed file's size and modification time; when they all still match, that hash is returned
    without reading the files."""
    root = Path(plugin_dir)
    if not root.is_dir() or not all((root / f).is_file() for f in FIXED_FILES):
        return None
    rel = list(FIXED_FILES)
    lib = root / "lib"
    if lib.is_dir():
        rel += [f"lib/{p.name}" for p in lib.iterdir() if p.is_file() and p.suffix == ".js"]
    key = None
    if cache is not None:
        try:
            key = {"dir": os.path.abspath(root),
                   "files": sorted([n, st.st_size, st.st_mtime_ns] for n in rel for st in ((root / n).stat(),))}
            saved = json.loads(Path(cache).read_text(encoding="utf-8"))
            if isinstance(saved, dict) and {k: saved.get(k) for k in key} == key and _is_hash(saved.get("hash")):
                return saved["hash"]
        except (OSError, ValueError):
            pass
    try:
        h = hash_files({name: (root / name).read_bytes() for name in rel})
    except OSError:
        return None
    if key and h and time.time_ns() - max(f[2] for f in key["files"]) > SETTLED_NS:
        _store(Path(cache), dict(key, hash=h))
    return h


def _is_hash(value) -> bool:
    return isinstance(value, str) and len(value) == 8 and all(c in "0123456789abcdef" for c in value)


def _store(cache: Path, doc: dict):
    """Write the cache file (whole, then renamed into place); a failure only means no cache."""
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(json.dumps(doc).encode("utf-8"))
        os.replace(tmp, cache)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


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
