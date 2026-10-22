"""The broker's audit log: one JSON line per request, refusal and panel (dis)connection.

Files are audit-YYYY-MM-DD.jsonl (local date) in %USERPROFILE%\\.ppro-agent-bridge\\audit (see auth.data_dir);
PPRO_AUDIT_DIR moves them, PPRO_AUDIT_DIR=off turns the log off. Files older than KEEP_DAYS are pruned when the
broker starts. A line carries what is needed to reconstruct a session afterwards — which action, against which
sequence (and how many tracks Premiere reported), how it ended, and how long it queued and ran — with params
shortened so a transcript or a long path list cannot bloat it. Writing never raises: a full disk or a locked
file must not stop the bridge.

Stdlib only: the broker imports this.
"""

import datetime as dt
import json
import os
import re
from pathlib import Path

from . import auth, spec

DIR_ENV = "PPRO_AUDIT_DIR"
KEEP_DAYS = 30
MAX_STRING = 200
MAX_ITEMS = 20
MAX_PARAMS_BYTES = 2048
MAX_ERROR = 500
NAME_RE = re.compile(r"^audit-(\d{4})-(\d{2})-(\d{2})\.jsonl$")


def audit_dir() -> Path | None:
    """Where the log goes; None when PPRO_AUDIT_DIR is "off"."""
    value = os.environ.get(DIR_ENV)
    if value and value.strip().lower() == "off":
        return None
    return Path(value) if value else auth.data_dir() / "audit"


def shorten(value, depth: int = 0):
    """`value` with long strings cut to MAX_STRING characters and long lists to MAX_ITEMS items (each cut says how
    much is missing); nesting below 6 levels becomes "…"."""
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + f"…(+{len(value) - MAX_STRING} chars)"
    if depth >= 6 and isinstance(value, (dict, list)):
        return "…"
    if isinstance(value, dict):
        return {str(k): shorten(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        head = [shorten(v, depth + 1) for v in value[:MAX_ITEMS]]
        return head + ([f"…(+{len(value) - MAX_ITEMS} items)"] if len(value) > MAX_ITEMS else [])
    return value


def shorten_params(params) -> dict:
    """Params for the log, at most about MAX_PARAMS_BYTES: shortened, and if still too big, only the top-level
    numbers, booleans and short strings, the rest named under "omitted"."""
    if not isinstance(params, dict):
        return {"invalid": shorten(repr(params))}
    short = shorten(params)
    if len(json.dumps(short, ensure_ascii=False)) <= MAX_PARAMS_BYTES:
        return short
    kept = {k: v for k, v in short.items()
            if isinstance(v, (bool, int, float)) or v is None or (isinstance(v, str) and len(v) <= 80)}
    kept["omitted"] = sorted(k for k in short if k not in kept)
    return kept


def summary(action, data) -> dict:
    """The reply facts worth keeping: sequence name, guid and track counts, the player position, frame size,
    undo labels and step counts, the panel's version on ping."""
    if not isinstance(data, dict):
        return {}
    out = {}
    if action == "get_sequence":
        out.update({k: data[k] for k in ("name", "guid", "durationSeconds", "playerPositionSeconds") if k in data})
    if action == "get_project":
        out.update({k: data[k] for k in ("name", "guid", "activeSequence") if k in data})
    if action == "ping":
        out.update({k: data[k] for k in ("plugin", "version", "protocol", "host", "stuck", "denied") if k in data})
    if isinstance(data.get("sequence"), str):
        out["sequence"] = data["sequence"]
    for key, name in (("videoTracks", "videoTracks"), ("audioTracks", "audioTracks"), ("video", "videoTracks"),
                      ("audio", "audioTracks")):
        if isinstance(data.get(key), list):
            out[name] = len(data[key])
    for key in ("undoLabel", "undoSteps", "width", "height", "exported"):
        if key in data and isinstance(data[key], (str, int, float, bool)):
            out[key] = data[key]
    return out


def request_record(msg: dict, reply: dict, *, queue_ms=None, exec_ms=None) -> dict:
    """The log line for one controller request and the reply the broker sends back."""
    action = msg.get("action") if isinstance(msg, dict) else None
    rec = {"event": "request", "id": msg.get("id") if isinstance(msg, dict) else None, "action": action}
    kind = spec.kind(action) if isinstance(action, str) else None
    if kind:
        rec["kind"] = kind
    if isinstance(msg, dict):
        rec["params"] = shorten_params(msg.get("params") or {})
        if msg.get("guard"):
            rec["guard"] = shorten(msg["guard"])
        if msg.get("timeoutSeconds") is not None:
            rec["timeoutSeconds"] = msg["timeoutSeconds"]
    rec["ok"] = bool(reply.get("ok"))
    if not rec["ok"]:
        for key in ("code", "outcome"):
            if reply.get(key) is not None:
                rec[key] = reply[key]
        if reply.get("error") is not None:
            error = str(reply["error"])
            rec["error"] = error if len(error) <= MAX_ERROR else error[:MAX_ERROR] + "…"
    if queue_ms is not None:
        rec["queueMs"] = round(queue_ms)
    if exec_ms is not None:
        rec["execMs"] = round(exec_ms)
    if reply.get("warnings"):
        rec["warnings"] = len(reply["warnings"])
    facts = summary(action, reply.get("data"))
    if facts:
        rec["summary"] = facts
    return rec


class AuditLog:
    """Appends JSON lines to today's file in `directory`; `failures` counts writes that did not land."""

    def __init__(self, directory, *, now=None):
        self.directory = Path(directory)
        self.now = now or (lambda: dt.datetime.now().astimezone())
        self.failures = 0
        self.last_error = None

    def path_for(self, when: dt.datetime) -> Path:
        return self.directory / f"audit-{when:%Y-%m-%d}.jsonl"

    def write(self, record: dict) -> bool:
        when = self.now()
        line = {"ts": when.isoformat(timespec="milliseconds"), **record}
        try:
            text = json.dumps(line, ensure_ascii=False, default=str) + "\n"
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(self.path_for(when), "a", encoding="utf-8", newline="\n") as f:
                f.write(text)
            return True
        except (OSError, TypeError, ValueError) as e:
            self.failures += 1
            self.last_error = str(e)
            return False

    def prune(self, keep_days: int = KEEP_DAYS) -> list[str]:
        """Delete this log's own files (audit-YYYY-MM-DD.jsonl) dated more than keep_days ago; returns their names.
        Other files in the folder are never touched."""
        cutoff = self.now().date() - dt.timedelta(days=keep_days)
        removed = []
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return removed
        for p in entries:
            m = NAME_RE.match(p.name)
            if not m or not p.is_file():
                continue
            try:
                day = dt.date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                continue
            if day < cutoff:
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError:
                    pass
        return removed
