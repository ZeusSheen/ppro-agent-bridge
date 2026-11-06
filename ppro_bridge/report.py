"""A problem report to paste into a GitHub issue: versions, the bridge's health, recent failures — and nothing private.

  python -m ppro_bridge.report            the report as text (what to paste)
  python -m ppro_bridge.report --json     the same as JSON
  python -m ppro_bridge.report --no-ping  without asking the broker (when it is not running)

Every field is taken from a whitelist (SECURITY_REVIEW.md, "Problem report"): the bridge, broker and panel versions,
the ping's compatibility, capabilities and build, the Premiere and Windows versions, each doctor check's name and
pass/fail, the update check's latest version and whether it is newer (ping's `update`, else update.json), and the last
failed requests from the audit log (time, action, code, outcome). Never the pairing token,
project or sequence names, media paths, request params or doctor details; the user's home folder becomes "~" in what
is left. As a last line of defence the text is searched for the token before it is returned, and a hit is cut out.
The panel's "Copy a problem report" button builds the same report from its own side.
"""

import argparse
import datetime as _dt
import json
import os
import platform
import re
import sys
from pathlib import Path

from . import audit, auth
from . import update as updates
from ._version import VERSION

RECENT_FAILURES = 8          # failed requests from the audit log
RECENT_DAYS = 2              # audit files read (today and yesterday)
PING_KEYS = ("version", "cliVersion", "brokerVersion", "protocol", "compatible", "stale", "warning", "modules")
CAPABILITY_VALUES = (True, False, "unknown")


def _home_patterns() -> list[re.Pattern]:
    homes = {str(Path.home())}
    if os.environ.get("USERPROFILE"):
        homes.add(os.environ["USERPROFILE"])
    out = []
    for h in sorted(homes, key=len, reverse=True):
        h = h.rstrip("\\/")
        if len(h) < 3:
            continue
        body = r"[\\/]".join(re.escape(part) for part in re.split(r"[\\/]", h))
        out.append(re.compile(body, re.I))
    return out


# Anything path-like that is left becomes "[path]", the same shapes as the panel's sanitize (plugin/lib/support.js):
# file URLs, UNC paths, drive paths and POSIX home or volume paths, each to the next quote, <, >, | or line end (a path
# may hold spaces). Media and projects often live outside the home folder (on another drive), so "~" alone is not enough.
_END = "^\"'<>|\n"  # a path runs to the next quote, <, >, | or line end
_PATH_SHAPES = (
    (re.compile(r"file://[" + _END + "]*", re.I), "[path]"),
    (re.compile(r"\\\\[" + _END + "]+"), "[path]"),
    (re.compile(r"\b[A-Za-z]:[\\/][" + _END + "]*"), "[path]"),
    (re.compile(r"(^|[\s\"'(=])/(?:Users|home|Volumes|private|var|tmp|mnt|media)/[" + _END + "]*"), r"\1[path]"),
)


def redact(text: str) -> str:
    """The user's home folder (either slash, any case) becomes "~"; any other path-like text becomes "[path]"."""
    for pat in _home_patterns():
        text = pat.sub("~", text)
    for pat, repl in _PATH_SHAPES:
        text = pat.sub(repl, text)
    return text


def _clean(value, depth=0):
    """Strings redacted and cut to 300 characters; dicts and lists cleaned the same way (6 levels deep at most)."""
    if depth > 6:
        return "…"
    if isinstance(value, str):
        value = redact(value)
        return value if len(value) <= 300 else value[:300] + "…"
    if isinstance(value, dict):
        return {str(k): _clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in value[:50]]
    return value


# ping's `warning` is free text (ops.ping joins one sentence per problem, and a sentence can quote a name: the
# action a stuck panel hangs on, a denylist reason). The report keeps only which kinds of warning there were, as
# fixed labels (QA-3): nothing of the text itself, so no project or sequence name can ride along.
WARNING_KINDS = (
    ("Panel is ", "panel version differs from the CLI"),
    ("Panel speaks protocol", "panel protocol differs from the CLI"),
    ("Broker is ", "broker version differs from the CLI"),
    ("The panel is stuck", "panel stuck"),
    ("denylist", "Premiere build denied"),
    ("is a .ccx install whose code differs", "installed panel differs from this checkout"),
    ("The panel is stale", "panel stale"),
)
_MODULE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def warning_kinds(text) -> str | None:
    """The fixed labels of the warnings in ping's `warning` text ("other" for one it does not know), or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    kinds = [label for marker, label in WARNING_KINDS if marker in text]
    return "; ".join(kinds) if kinds else "other"


def _modules(value):
    """ping's `modules` (a short tag or a list of module names) when it has that shape, else left out."""
    if isinstance(value, str) and _MODULE_NAME.match(value):
        return value
    if isinstance(value, (list, tuple)):
        names = [v for v in value if isinstance(v, str) and _MODULE_NAME.match(v)][:40]
        return names or None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def from_ping(data: dict | None) -> dict:
    """The whitelisted part of a `ppro.py ping` reply (its warning as kinds only: warning_kinds)."""
    if not isinstance(data, dict) or not data:
        return {}
    out = {k: data[k] for k in PING_KEYS if k in data}
    if "warning" in out:
        out["warning"] = warning_kinds(out["warning"])
        if out["warning"] is None:
            del out["warning"]
    if "modules" in out:
        out["modules"] = _modules(out["modules"])
        if out["modules"] is None:
            del out["modules"]
    host = data.get("hostInfo") if isinstance(data.get("hostInfo"), dict) else {}
    out["premiere"] = {k: host.get(k) for k in ("name", "version", "buildNumber", "uiLocale") if host.get(k) is not None}
    caps = data.get("capabilities")
    if isinstance(caps, dict):
        out["capabilities"] = {str(k): v for k, v in caps.items() if v in CAPABILITY_VALUES}
    build = data.get("build")
    if isinstance(build, dict):
        out["build"] = {k: build[k] for k in ("dev", "packaged") if isinstance(build.get(k), bool)}
    stuck = data.get("stuck")
    out["stuck"] = {"action": stuck.get("action"), "seconds": stuck.get("seconds")} if isinstance(stuck, dict) \
        else bool(stuck)
    denied = data.get("denied")
    out["denied"] = {"reason": denied.get("reason")} if isinstance(denied, dict) else bool(denied)
    if isinstance(data.get("features"), list):
        out["features"] = [str(f) for f in data["features"]][:30]
    upd = update_fields(data.get("update"))
    if upd:
        out["update"] = upd
    return _clean(out)


def update_fields(upd) -> dict | None:
    """{latest, newer, skipped} of ping's `update` (or updates.status()); {"off": True} when the check is off; None
    when nothing is known. The release URL and dates are left out (they say nothing about the problem)."""
    if not isinstance(upd, dict):
        return None
    if upd.get("enabled") is False:
        return {"off": True}
    parts = updates.parse_version(upd.get("latest"))
    if parts is None:
        return None
    return {"latest": updates.version_text(parts), "newer": upd.get("newer") is True,
            "skipped": upd.get("skipped") is True}


def from_checks(results) -> list[dict]:
    """doctor.checks(): name, ok, required only (details can hold paths)."""
    return [{"name": str(r.get("name")), "ok": bool(r.get("ok")), "required": bool(r.get("required", True))}
            for r in results or [] if isinstance(r, dict)]


def recent_failures(folder: Path | None = None, today: _dt.date | None = None, limit=RECENT_FAILURES) -> list[dict]:
    """The last failed requests and refusals in the audit log: ts, event, action, code, outcome (no params)."""
    folder = audit.audit_dir() if folder is None else folder
    if folder is None or not folder.is_dir():
        return []
    today = today or _dt.date.today()
    rows = []
    for back in range(RECENT_DAYS - 1, -1, -1):
        day = today - _dt.timedelta(days=back)
        path = folder / f"audit-{day:%Y-%m-%d}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            failed = rec.get("event") == "refused" or rec.get("ok") is False or (
                rec.get("outcome") not in (None, "ok", "committed_ok") and rec.get("code"))
            if failed:
                rows.append(_failure_row(rec))
    return _clean(rows[-limit:])


_FAILURE_SHAPES = {"ts": re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.+\-Z]{1,30}$"),
                   "event": re.compile(r"^[a-z_]{1,30}$"), "code": re.compile(r"^[A-Z][A-Z0-9_]{1,39}$"),
                   "outcome": re.compile(r"^[a-z_]{1,30}$"), "reason": re.compile(r"^[a-z_]{1,40}$")}


def _failure_row(rec: dict) -> dict:
    """One audit record as the report lists it (QA-3): each field only in its fixed shape - a time, a word, an error
    code - and the action only when it is a spec action or CLI command ("unknown" otherwise: a client may send any
    text as an action), so no name or message can ride along."""
    from . import spec  # light: actions.json
    row = {k: rec[k] for k, shape in _FAILURE_SHAPES.items() if isinstance(rec.get(k), str) and shape.match(rec[k])}
    action = rec.get("action")
    if action is not None:
        known = set(spec.action_names()) | set(spec.command_names())
        row["action"] = action if isinstance(action, str) and action in known else "unknown"
    return {k: row[k] for k in ("ts", "event", "action", "code", "outcome", "reason") if k in row}


def build(ping=None, checks=None, failures=None, local_update=None) -> dict:
    """The report as plain data. `ping`: a ping reply (None: not asked); `checks`: doctor.checks() results;
    `local_update`: updates.status(), used when the ping has no `update`."""
    pinged = from_ping(ping) if ping is not None else None
    return {
        "bridge": VERSION,
        "python": platform.python_version(),
        "windows": f"{platform.system()} {platform.release()} ({platform.version()})",
        "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "ping": pinged,
        "update": (pinged or {}).get("update") or update_fields(local_update),
        "checks": from_checks(checks) if checks is not None else None,
        "recentFailures": failures if failures is not None else [],
    }


def as_text(rep: dict) -> str:
    lines = ["### Premiere Agent Bridge problem report", "",
             f"- Bridge {rep['bridge']}, Python {rep['python']}, {rep['windows']}",
             f"- Created {rep['created']}"]
    ping = rep.get("ping")
    if ping is None:
        lines.append("- Ping: not asked")
    elif not ping:
        lines.append("- Ping: no answer (broker or panel not running)")
    else:
        prem = ping.get("premiere") or {}
        lines.append(f"- Premiere {prem.get('version', '?')} (build {prem.get('buildNumber', '?')}, "
                     f"{prem.get('uiLocale', '?')})")
        lines.append(f"- Panel {ping.get('version', '?')}, broker {ping.get('brokerVersion', '?')}, protocol "
                     f"{ping.get('protocol', '?')}, compatible: {ping.get('compatible')}"
                     + (", STALE" if ping.get("stale") else "")
                     + (f", stuck on {ping['stuck'].get('action')}" if isinstance(ping.get("stuck"), dict) else "")
                     + (f", denied ({ping['denied'].get('reason')})" if isinstance(ping.get("denied"), dict) else ""))
        if ping.get("build"):
            lines.append(f"- Build: {json.dumps(ping['build'])}")
        if ping.get("warning"):
            lines.append(f"- Warning: {ping['warning']}")
        caps = ping.get("capabilities") or {}
        if caps:
            off = sorted(k for k, v in caps.items() if v is False)
            unknown = sum(v == "unknown" for v in caps.values())
            lines.append(f"- Capabilities: {sum(v is True for v in caps.values())} yes, {len(off)} no"
                         + (f" ({', '.join(off)})" if off else "") + f", {unknown} not probed yet")
    upd = rep.get("update")
    if isinstance(upd, dict) and upd.get("off"):
        lines.append("- Update check: off")
    elif isinstance(upd, dict) and upd.get("latest"):
        lines.append(f"- Update: {upd['latest']} is available" + (" (skipped)" if upd.get("skipped") else "")
                     if upd.get("newer") else f"- Update: up to date (latest release {upd['latest']})")
    else:
        lines.append("- Update: not checked yet")
    checks = rep.get("checks")
    if checks:
        bad = [c["name"] + ("" if c["required"] else " (optional)") for c in checks if not c["ok"]]
        lines.append(f"- Checks: {len(checks) - len(bad)} of {len(checks)} pass" + (f"; failing: {', '.join(bad)}"
                                                                                  if bad else ""))
    fails = rep.get("recentFailures") or []
    if fails:
        lines += ["", "Recent failures (audit log):"]
        lines += [f"- {f.get('ts', '?')} {f.get('action') or f.get('event')}: {f.get('code') or f.get('reason')}"
                  f" ({f.get('outcome', '-')})" for f in fails]
    lines += ["", "No token, project names, media paths or request params are in this report."]
    return "\n".join(lines) + "\n"


def scrub_token(text: str) -> str:
    """Cut the pairing token out of `text` if it is somehow there (it is never put in)."""
    try:
        token = auth.read_token() if hasattr(auth, "read_token") else None
    except Exception:  # noqa: BLE001 - a report must not fail on the token file
        token = None
    if token and len(token) >= 8 and token in text:
        text = text.replace(token, "[removed]")
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ppro_bridge.report", description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--no-ping", action="store_true", help="do not ask the broker")
    args = ap.parse_args(argv)
    ping = None
    if not args.no_ping:
        try:
            from . import ops, transport
            ping = ops.ping(transport.make_rpc())
        except Exception:  # noqa: BLE001 - a missing broker is part of what the report says
            ping = {}
    try:
        from . import doctor
        checks = doctor.checks(upia=False)
    except Exception:  # noqa: BLE001
        checks = None
    try:
        local = updates.status()
    except Exception:  # noqa: BLE001 - a report must not fail on the update cache
        local = None
    rep = build(ping, checks, recent_failures(), local)
    out = json.dumps(rep, ensure_ascii=False, indent=1) if args.json else as_text(rep)
    sys.stdout.write(scrub_token(out) + ("\n" if args.json else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
