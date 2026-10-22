"""Console encoding for the three entry points (ppro CLI, transcribe, transcript digest)."""

import sys


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
