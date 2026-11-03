"""Checks on file paths before Premiere is asked to read or write them.

Premiere opens whatever path it is given with the user's own rights:
  * a network (UNC) path - \\\\server\\share\\..., //server/share/..., \\\\?\\UNC\\server\\share\\... - makes Windows
    connect to that host and offer the user's credentials, so a path an agent picked up from a web page or a file, or
    a program that got the panel to listen to it, could leak them. Refused as typed, always: map a drive letter for a
    share (Z: and a path), which is fine even though Python resolves it to its UNC form.
  * a Windows device path - \\\\.\\... (named pipes, raw disks, and \\\\.\\UNC\\, which reaches the network too), \\??\\...,
    or \\\\?\\ followed by anything but a drive (\\\\?\\Volume{...}, \\\\?\\GLOBALROOT\\...) - holds no media. Refused.
    \\\\?\\<drive>:\\... is a local long path (Premiere reports project paths that way) and is fine.
  * an NTFS alternate data stream (clip.mov:hidden) writes data no folder listing shows. Refused.
  * exports keep a fixed extension (a frame is a .png, a render a .wav, OTIO an .otio, a preset an .epr), so an
    export can never write, say, a .bat.
`/` and `\\` count the same. The panel and the fake panel refuse the same paths themselves (actions.json params marked
`localPath`; plugin/lib/core.js pathProblem mirrors path_problem below), so a request that does not come from this
CLI cannot hand Premiere one either. Every refusal is BAD_PARAMS, outcome not_committed: nothing was sent.
"""

import os
import re
from pathlib import Path

from .errors import BridgeError

_DRIVE = re.compile(r"^[A-Za-z]:")
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:(\\|$)")


def _bad(message: str) -> BridgeError:
    return BridgeError(message, code="BAD_PARAMS", outcome="not_committed")


def remote_kind(text) -> str | None:
    """"UNC" for a network path, "device" for a Windows device or NT namespace path, None for anything else (a local
    path, relative or absolute, or \\\\?\\<drive>:\\...)."""
    s = str(text).strip().replace("/", "\\")
    if s.startswith("\\\\?\\"):
        rest = s[4:]
        if _DRIVE_ROOT.match(rest):
            return None
        return "UNC" if rest[:4].upper() == "UNC\\" else "device"
    if s.startswith("\\\\.\\"):
        return "UNC" if s[4:8].upper() == "UNC\\" else "device"
    if s.startswith("\\??\\") or s in ("\\\\.", "\\\\?"):
        return "device"
    if s.startswith("\\\\"):
        return "UNC"
    return None


def is_unc(text: str) -> bool:
    return remote_kind(text) == "UNC"


def has_stream(text: str) -> bool:
    """A colon anywhere but right after a drive letter (also after \\\\?\\): an NTFS alternate data stream, a URL, or a
    malformed path."""
    s = str(text).strip().replace("/", "\\")
    if s.startswith("\\\\?\\") and _DRIVE_ROOT.match(s[4:]):
        s = s[4:]
    rest = s[2:] if _DRIVE.match(s) else s
    return ":" in rest


def path_problem(text) -> str | None:
    """Why Premiere must not be handed this path, or None. The panel's pathProblem (plugin/lib/core.js) returns the
    same texts for the same paths: the spec check of every `localPath` param uses them."""
    kind = remote_kind(text)
    if kind == "UNC":
        return ("a network (UNC) path: Premiere would connect to that host with your Windows credentials. Copy the "
                "file to a local disk, or map a drive letter to the share")
    if kind == "device":
        return "a Windows device path (\\\\.\\, \\??\\ or \\\\?\\ without a drive): no media lives there"
    if has_stream(str(text).strip()):
        return "an NTFS data stream or a URL (a colon after the drive): only plain local paths are opened"
    return None


def check(path, *, what: str = "path", ext: str | tuple[str, ...] | None = None) -> str:
    """`path` as typed, checked: no network (UNC) or device path, no data stream, and with `ext` the file must end in
    that extension (or one of them; case-insensitive). Returns the path as a string."""
    text = os.path.expanduser(str(path)).strip()
    if not text:
        raise _bad(f"{what} is empty")
    why = path_problem(text)
    if why:
        raise _bad(f"{what} {text!r} is {why}. Nothing was sent.")
    if ext is not None:
        exts = (ext,) if isinstance(ext, str) else tuple(ext)
        if not text.lower().endswith(tuple(e.lower() for e in exts)):
            raise _bad(f"{what} {text!r} must end in {' or '.join(exts)}. Nothing was sent.")
    return text


def name_problem(text) -> str | None:
    """Why `text` is not a plain file name (no folder, no drive, no stream), or None. The panel's nameProblem mirrors
    it for the `fileName` params (export_frame's name)."""
    s = str(text).strip()
    if not s or any(sep in s for sep in ("/", "\\")) or s in (".", "..") or ":" in s:
        return "must be a plain file name (no folder, drive or stream)"
    return None


def check_name(name: str, *, what: str = "name", ext: str) -> str:
    """A bare file name for a folder the caller chose: no folders in it (no / \\ or ..), no stream, and it ends in
    `ext` (added when it has no extension at all)."""
    text = str(name).strip()
    if not text or any(sep in text for sep in ("/", "\\")) or text in (".", ".."):
        raise _bad(f"{what} {name!r} must be a plain file name (no folders). Nothing was sent.")
    if not Path(text).suffix:
        text += ext
    return check(text, what=what, ext=ext)
