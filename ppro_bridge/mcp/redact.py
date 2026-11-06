"""Keep the pairing token out of everything the MCP server says: tool results, errors, tool lists and log lines.

The server never sends the token: like the CLI it proves it to the broker (DOC-8). No reply carries it, but a tool
can be pointed at any file, and some failures quote what they read (a line of an SRT, a transcript, a plan), so a
call aimed at the token file could echo it. Every text the server hands out passes through scrub(), which replaces each token value
seen so far (PPRO_BRIDGE_TOKEN, the token file, as sent and as JSON would escape it) with "<redacted>".
"""

import json
import logging
import os
import threading

from .. import auth

REDACTED = "<redacted>"
MIN_SECRET = 16  # shorter values are not tokens (auth.MIN_LENGTH is 32); replacing them would mangle ordinary text

_seen: set[str] = set()
_lock = threading.Lock()


def _candidates() -> set[str]:
    found = {auth.read_token() or ""}
    found.add((os.environ.get(auth.TOKEN_ENV) or "").strip())
    try:
        found.add(auth.token_path().read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        pass
    return {s for s in found if len(s) >= MIN_SECRET}


def secrets() -> set[str]:
    """Every token value seen in this process (they are kept: the file may be replaced while the server runs)."""
    with _lock:
        _seen.update(_candidates())
        return set(_seen)


def forms() -> list[str]:
    """Every text form a token can take in output: as sent, and as JSON escapes it; longest first."""
    out = set()
    for secret in secrets():
        out.update((secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret)[1:-1]))
    return sorted(out, key=len, reverse=True)


FRAGMENT = 16  # a run this long of a token (a cut-off quote of the file: "…(+N chars)") is redacted too


def _fragments(form: str):
    """Every FRAGMENT-long piece of a token form (a regex alternation finds any of them in one pass)."""
    return {form[i:i + FRAGMENT] for i in range(len(form) - FRAGMENT + 1)}


def scrub(text: str, _forms: list[str] | None = None) -> str:
    """`text` with every token value (and its JSON-escaped form) replaced by "<redacted>", and so is any run of at
    least FRAGMENT characters of one (a message that quotes the token file cut short would otherwise leave most of
    it readable: 40 of 43 characters are 18 bits from the whole token)."""
    if not isinstance(text, str) or not text:
        return text
    found = forms() if _forms is None else _forms
    for form in found:
        if form in text:
            text = text.replace(form, REDACTED)
    import re
    for form in found:
        if len(form) < FRAGMENT:
            continue
        pieces = _fragments(form)
        pattern = re.compile("|".join(re.escape(p) for p in sorted(pieces)))
        out, at = [], 0
        for m in pattern.finditer(text):
            if m.start() < at:
                continue
            start, end = m.start(), m.end()
            while end < len(text) and text[start:end + 1] in form:  # widen to the whole run of the token
                end += 1
            while start > at and text[start - 1:end] in form:
                start -= 1
            out.append(text[at:start] + REDACTED)
            at = end
        if out:
            text = "".join(out) + text[at:]
    return text


def scrub_obj(obj, _forms: list[str] | None = None):
    """A copy of a JSON-like value with scrub() applied to every string in it, keys included."""
    f = forms() if _forms is None else _forms
    if isinstance(obj, str):
        return scrub(obj, f)
    if isinstance(obj, dict):
        return {scrub(k, f) if isinstance(k, str) else k: scrub_obj(v, f) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v, f) for v in obj]
    return obj


class RedactingFormatter(logging.Formatter):
    """A log formatter whose output (message, arguments and traceback alike) has passed through scrub()."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def log_handler(stream=None, fmt: str = "%(asctime)s %(levelname)s %(name)s: %(message)s") -> logging.Handler:
    """A stderr handler for the server's logging that never writes a token (stdout is the protocol's)."""
    import sys
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(RedactingFormatter(fmt))
    return handler
