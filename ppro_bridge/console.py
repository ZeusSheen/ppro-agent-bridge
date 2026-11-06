"""Console encoding for the three entry points (ppro CLI, transcribe, transcript digest), and how the bridge starts
Python again (isolated_flags)."""

import sys
from pathlib import Path


def utf8_stdio() -> None:
    """Write stdout/stderr as UTF-8 whatever the console or pipe encoding is.

    On Windows a pipe defaults to the ANSI code page (cp1252, cp936, ...): a Chinese clip name then either fails to
    encode or reaches the caller in an encoding it does not expect. Every main() calls this first. Characters that
    cannot be written at all (lone surrogates from odd file names) come out as backslash escapes."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


def bundled_python(executable=None) -> bool:
    """`executable` (default: this interpreter) is the installer's embeddable Python: a python3XX._pth beside it."""
    try:
        return any(Path(executable or sys.executable).parent.glob("python3*._pth"))
    except OSError:
        return False


def isolated_flags(executable=None) -> list[str]:
    """The flags for starting `executable` (default: this interpreter) again: ["-I"] for the installer's Python -
    its ._pth turns `import site` on, which would put the per-user site-packages folder on
    sys.path and run any .pth or usercustomize planted there (QA-8) -, [] for any other Python, whose user site may
    hold the bridge's own packages (pip install --user)."""
    return ["-I"] if bundled_python(executable) else []
