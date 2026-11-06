"""The broker's daily update check, and settings.json (the user's choices that the broker keeps).

Only the broker checks (SECURITY_REVIEW.md, section 1, "Update check"). In a background thread, at most once in
INTERVAL (24 h; an attempt counts whatever its outcome, so restarting the broker does not ask again), it sends one
HTTPS GET to GitHub's releases/latest API for this repository (API_URL): User-Agent ppro-agent-bridge/<version>, no
cookies, no machine or user identifier. Redirects are followed only to the same scheme and host, at most MAX_BYTES are
read, and the request gives up after TIMEOUT seconds. Of the answer only three fields are kept: tag_name (as a version
x.y.z), published_at, and html_url, and the URL only when it is a release page of this repository (release_url:
RELEASES_URL followed by plain path segments). The release text (`body`) is never kept or shown. The result is
cached in <folder>/update.json, so the next broker start shows it without asking again.

  <folder>  PPRO_SETTINGS_DIR, else %USERPROFILE%\\.ppro-agent-bridge (auth.data_dir(), beside the token)
  off       PPRO_UPDATE_CHECK=0 (or false / off / no), or settings.json {"updateCheck": false}: nothing is sent, and
            ping's `update` is null
  skip      settings.json {"skipVersion": "x.y.z"}: written by the broker when the panel's "Skip this version" is
            clicked (Checker.skip: only the latest release the broker knows, and only one newer than the bridge);
            that version is not announced again, a newer one is

ping (and the panel's "registered" notice) carries `update`: {latest, url, published, newer, skipped, checkedAt} or
null (check off, or nothing known yet). status() is the same, read from the cache without asking GitHub, for doctor,
the setup window and the problem report.

Stdlib only: the broker, doctor and the setup window import this.
"""

import calendar
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import auth
from ._version import VERSION

REPO_URL = "https://github.com/ZeusSheen/ppro-agent-bridge"   # the only repository the bridge ever points at
RELEASES_URL = REPO_URL + "/releases/"
ISSUES_URL = REPO_URL + "/issues/new"
API_URL = REPO_URL.replace("https://github.com/", "https://api.github.com/repos/", 1) + "/releases/latest"
INTERVAL = 24 * 3600         # seconds between two attempts
TIMEOUT = 5.0                # seconds for the whole request
MAX_BYTES = 2 * 1024 * 1024  # a releases/latest answer is a few KB (0.6.0: about 6 KB)
CHECK_ENV = "PPRO_UPDATE_CHECK"
DIR_ENV = "PPRO_SETTINGS_DIR"
SETTINGS = "settings.json"
CACHE = "update.json"
OFF_WORDS = ("0", "false", "off", "no")

_TAG = re.compile(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})")                       # a release tag: v0.7.1
_STRICT = re.compile(r"(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})")  # what the panel may send: 0.7.1
_PUBLISHED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_SEGMENT = re.compile(r"[A-Za-z0-9._~+-]{1,100}")
_OUTCOME = re.compile(r"[a-z0-9_]{1,24}")


class UpdateError(Exception):
    """One failed attempt; `outcome` is the short word the audit log records (timeout, http_403, redirect, ...)."""

    def __init__(self, outcome: str, message: str = ""):
        super().__init__(message or outcome)
        self.outcome = outcome


class SettingsError(OSError):
    """settings.json cannot be read as a JSON object, or cannot be written."""


# ---------------------------------------------------------------- values

def parse_version(text) -> tuple | None:
    """(0, 7, 1) for "0.7.1" or "v0.7.1"; None for anything else (pre-release suffixes included)."""
    m = _TAG.fullmatch(text) if isinstance(text, str) else None
    return tuple(int(g) for g in m.groups()) if m else None


def strict_version(text) -> str | None:
    """`text` when it is exactly x.y.z (no "v", no leading zeros, no suffix), else None: what skip_version accepts."""
    return text if isinstance(text, str) and _STRICT.fullmatch(text) else None


def version_text(parts: tuple) -> str:
    return ".".join(str(p) for p in parts)


def release_url(url) -> str | None:
    """`url` when it is a release page of this repository - RELEASES_URL and then plain path segments (letters,
    digits, . _ ~ + -; never "." or "..", "%", "?", "#", "@" or a backslash) - else None. Nothing else is kept or
    opened."""
    if not isinstance(url, str) or len(url) > 300 or not url.startswith(RELEASES_URL):
        return None
    segments = url[len(RELEASES_URL):].split("/")
    if any(not _SEGMENT.fullmatch(s) or s in (".", "..") for s in segments):
        return None
    return url


def stamp(t: float) -> str:
    return time.strftime(_STAMP, time.gmtime(t))


def parse_stamp(text) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(text, _STAMP))) if isinstance(text, str) else None
    except (ValueError, OverflowError):
        return None


def parse_release(doc) -> dict:
    """The kept fields of a releases/latest answer: {latest, url, published}; UpdateError("bad_response") when
    tag_name is not a version. Nothing else of `doc` is looked at."""
    if not isinstance(doc, dict):
        raise UpdateError("bad_response", "the answer is not a JSON object")
    parts = parse_version(doc.get("tag_name"))
    if parts is None:
        raise UpdateError("bad_response", "tag_name is not a version x.y.z")
    published = doc.get("published_at")
    return {"latest": version_text(parts), "url": release_url(doc.get("html_url")),
            "published": published if isinstance(published, str) and _PUBLISHED.fullmatch(published) else None}


# ---------------------------------------------------------------- files

def settings_dir() -> Path:
    return Path(os.environ.get(DIR_ENV) or auth.data_dir())


def _read_object(path: Path) -> dict | None:
    """The JSON object in `path`; None when the file is missing; SettingsError when it holds something else."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise SettingsError(f"cannot read {path.name} ({e})") from e
    try:
        data = json.loads(text)
    except ValueError as e:
        raise SettingsError(f"{path.name} is not JSON ({e})") from e
    if not isinstance(data, dict):
        raise SettingsError(f"{path.name} is not a JSON object")
    return data


def _write_object(path: Path, data: dict) -> None:
    """Write `data` as JSON, replacing the file in one step (a temporary file beside it, then os.replace)."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SettingsError(f"cannot write {path.name} ({e})") from e


def read_settings(folder=None) -> dict:
    """settings.json as a dict; {} when it is missing or unreadable (a broken file is never overwritten: see
    write_setting)."""
    try:
        return _read_object(Path(folder or settings_dir()) / SETTINGS) or {}
    except SettingsError:
        return {}


def write_setting(key: str, value, folder=None) -> dict:
    """Set one key of settings.json, keeping every other key as it is on disk now; returns the new settings.
    SettingsError (nothing written) when the file exists but is not a JSON object, or cannot be written."""
    path = Path(folder or settings_dir()) / SETTINGS
    data = _read_object(path) or {}
    data[key] = value
    _write_object(path, data)
    return data


def _off(value) -> bool:
    """False, 0, or one of OFF_WORDS ("0", "false", "off", "no", any case)."""
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    return isinstance(value, str) and value.strip().lower() in OFF_WORDS


def enabled(environ=None, settings=None) -> tuple[bool, str | None]:
    """(on, why it is off): PPRO_UPDATE_CHECK=0, else settings.json updateCheck false."""
    environ = os.environ if environ is None else environ
    if _off(environ.get(CHECK_ENV, "")):
        return False, f"{CHECK_ENV}={environ.get(CHECK_ENV)}"
    if _off((settings or {}).get("updateCheck", True)):
        return False, "settings.json updateCheck: false"
    return True, None


def load_cache(folder=None) -> dict:
    """update.json, each field checked again (anything that does not fit is dropped); {} when there is none."""
    try:
        raw = _read_object(Path(folder or settings_dir()) / CACHE) or {}
    except SettingsError:
        return {}
    out = {}
    parts = parse_version(raw.get("latest"))
    if parts is not None:
        out["latest"] = version_text(parts)
        out["url"] = release_url(raw.get("url"))
        published = raw.get("published")
        out["published"] = published if isinstance(published, str) and _PUBLISHED.fullmatch(published) else None
    for key in ("checkedAt", "attemptedAt"):
        if parse_stamp(raw.get(key)) is not None:
            out[key] = raw[key]
    if isinstance(raw.get("outcome"), str) and _OUTCOME.fullmatch(raw["outcome"]):
        out["outcome"] = raw["outcome"]
    return out


def save_cache(cache: dict, folder=None) -> None:
    keys = ("latest", "url", "published", "checkedAt", "attemptedAt", "outcome")
    _write_object(Path(folder or settings_dir()) / CACHE, {k: cache.get(k) for k in keys if k in cache})


# ---------------------------------------------------------------- the request

class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the same scheme and host (GitHub answers a renamed repository with a 301 to another
    api.github.com path); anything else ends the attempt."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (new.scheme, new.netloc.lower()) != (old.scheme, old.netloc.lower()):
            raise UpdateError("redirect", "a redirect to another host was refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str = API_URL, timeout: float = TIMEOUT, version: str = VERSION) -> dict:
    """GET `url` (the broker always asks API_URL; tests pass a local server) and return the JSON object.
    UpdateError with the outcome on any failure. No cookie handler is installed, so none is sent or kept."""
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                         _SameOriginRedirects())
    req = urllib.request.Request(url, headers={"User-Agent": f"ppro-agent-bridge/{version}",
                                               "Accept": "application/vnd.github+json"})
    deadline = time.monotonic() + timeout  # the socket timeout is per read: a trickling answer must not run on
    try:
        with opener.open(req, timeout=timeout) as resp:
            if "json" not in (resp.headers.get("Content-Type") or "").lower():
                raise UpdateError("bad_response", "the answer is not JSON")
            chunks, size = [], 0
            read = getattr(resp, "read1", resp.read)  # read1: what one recv brought, so the deadline is looked at
            while size <= MAX_BYTES:
                if time.monotonic() > deadline:
                    raise UpdateError("timeout", "the answer took longer than the time limit")
                chunk = read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
    except UpdateError:
        raise
    except urllib.error.HTTPError as e:
        e.close()
        raise UpdateError(f"http_{e.code}") from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise UpdateError("timeout") from None
        if isinstance(reason, ssl.SSLError):
            raise UpdateError("tls") from None
        raise UpdateError("network") from None
    except (TimeoutError, socket.timeout):
        raise UpdateError("timeout") from None
    except ssl.SSLError:
        raise UpdateError("tls") from None
    except (OSError, ValueError, http.client.HTTPException):
        raise UpdateError("network") from None
    if len(data) > MAX_BYTES:
        raise UpdateError("too_large")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("bad_response", "the answer is not JSON") from None


# ---------------------------------------------------------------- what ping reports

def snapshot(cache: dict, settings: dict, current: str = VERSION) -> dict | None:
    """ping's `update`: {latest, url, published, newer, skipped, checkedAt}, or None when no release is known."""
    latest = parse_version(cache.get("latest"))
    if latest is None:
        return None
    mine = parse_version(current)
    newer = mine is not None and latest > mine
    skip = parse_version(strict_version(settings.get("skipVersion")))
    return {"latest": version_text(latest), "url": release_url(cache.get("url")), "published": cache.get("published"),
            "newer": newer, "skipped": bool(newer and skip is not None and latest <= skip),
            "checkedAt": cache.get("checkedAt")}


class Checker:
    """The broker's update state: the cache and settings in memory, one attempt at a time (run, in a thread), the
    skip (from the panel). Thread-safe."""

    def __init__(self, folder=None, version: str = VERSION, url: str = API_URL, environ=None, clock=time.time,
                 timeout: float = TIMEOUT):
        self.folder = Path(folder) if folder else settings_dir()
        self.version, self.url, self.clock, self.timeout = version, url, clock, timeout
        self._lock = threading.Lock()
        self.settings = read_settings(self.folder)
        self.enabled, self.off_reason = enabled(environ, self.settings)
        self.cache = load_cache(self.folder)

    def snapshot(self) -> dict | None:
        with self._lock:
            return snapshot(self.cache, self.settings, self.version) if self.enabled else None

    def wait_seconds(self) -> float:
        """Seconds until the next attempt is due (0: now). An attempt stamped more than an hour in the future (the
        clock was set back) does not hold the next one off."""
        now = self.clock()
        with self._lock:
            last = parse_stamp(self.cache.get("attemptedAt"))
        if last is None or last > now + 3600:
            return 0.0
        return max(0.0, last + INTERVAL - now)

    def run(self) -> dict:
        """One attempt (blocking: the broker calls it in a background thread). Never raises; returns the audit record
        {"event": "update_check", "outcome", "ms", and latest / newer when it worked}."""
        started = self.clock()
        t0 = time.monotonic()
        rec = {"event": "update_check"}
        try:
            found = parse_release(fetch(self.url, self.timeout, self.version))
            rec["outcome"] = "ok"
        except UpdateError as e:
            found, rec["outcome"] = None, e.outcome
        except Exception as e:  # noqa: BLE001 - a check must never take the broker down
            found, rec["outcome"] = None, "error"
            rec["error"] = type(e).__name__
        rec["ms"] = round((time.monotonic() - t0) * 1000)
        with self._lock:
            self.cache["attemptedAt"] = stamp(started)
            self.cache["outcome"] = rec["outcome"]
            if found is not None:
                self.cache.update(found, checkedAt=stamp(started))
            cache = dict(self.cache)
            snap = snapshot(self.cache, self.settings, self.version)
        try:
            save_cache(cache, self.folder)
        except SettingsError:
            rec["cacheWritten"] = False
        if snap is not None and found is not None:
            rec["latest"], rec["newer"] = snap["latest"], snap["newer"]
        return rec

    def skip(self, version) -> tuple[bool, str | None]:
        """Skip this version (the panel's button): (True, None) once settings.json says so, else (False, why).
        Only an exact x.y.z that is the latest release the broker knows, and newer than this bridge."""
        wanted = strict_version(version)
        if wanted is None:
            return False, "not a version x.y.z"
        if not self.enabled:
            return False, "the update check is off"
        with self._lock:
            latest = self.cache.get("latest")
            newer = snapshot(self.cache, self.settings, self.version) or {}
        if wanted != latest or not newer.get("newer"):
            return False, "only the newer release the broker announced can be skipped"
        if self.settings.get("skipVersion") == wanted:
            return True, None
        try:
            data = write_setting("skipVersion", wanted, self.folder)
        except SettingsError as e:
            return False, str(e)
        with self._lock:
            self.settings = data
        return True, None


def status(folder=None, environ=None, current: str = VERSION) -> dict:
    """The update state as the cache and settings hold it (nothing is sent): {enabled, off, latest, newer, skipped,
    url, checkedAt, text}. `text` is one line for doctor and the problem report."""
    folder = Path(folder) if folder else settings_dir()
    settings = read_settings(folder)
    on, why = enabled(environ, settings)
    snap = snapshot(load_cache(folder), settings, current) if on else None
    out = {"enabled": on, "off": why, "latest": None, "newer": False, "skipped": False, "url": None,
           "checkedAt": None}
    if snap:
        out.update({k: snap[k] for k in ("latest", "newer", "skipped", "url", "checkedAt")})
    if not on:
        text = f"check off ({why})"
    elif not snap:
        text = "not checked yet (the broker asks GitHub once a day)"
    elif snap["newer"] and snap["skipped"]:
        text = f"{snap['latest']} is available, skipped (this is {current})"
    elif snap["newer"]:
        text = f"{snap['latest']} is available (this is {current})" + (f": {snap['url']}" if snap["url"] else "")
    else:
        when = (snap["checkedAt"] or "")[:10]
        text = f"up to date (latest release {snap['latest']}" + (f", checked {when})" if when else ")")
    out["text"] = text
    return out
