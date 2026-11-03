"""Encoder presets (.epr) on disk, for list_presets and export_video: Premiere's system presets next to the running
executable (MediaIO/systempresets) and the user's own (Adobe Media Encoder's Presets folders, which Premiere's Export
dialog saves to as well), plus the check of a rendered file against the sequence.

A system preset folder is named after the exporter's two FourCCs in hex: 4E49434B_48323634 is "NICK" "H264", the
H.264 exporter. The second one is the file type, i.e. the format. A preset file says the same in its XML
(<ExporterFileType>1211250228</ExporterFileType> = 0x48323634 = "H264"), so a user preset in a flat folder has a
format too. Nothing here talks to Premiere.
"""

import difflib
import html
import os
import re
from pathlib import Path

from . import media, paths
from .errors import BridgeError

PRESET_DIR_ENV = "PPRO_PRESET_DIR"
SYSTEM_PARTS = ("MediaIO", "systempresets")
AME_APPS = ("Adobe Media Encoder", "Adobe Media Encoder (Beta)")
HEAD_BYTES = 16384  # the fields read below sit in the first ~2 KB of a preset

_FOLDER = re.compile(r"^([0-9A-Fa-f]{8})_([0-9A-Fa-f]{8})$")

# File-type FourCC -> (format, kind, output extensions; None = not checked). kind "image" is a numbered still
# sequence. Extensions are what Premiere's exporter writes for that format; unlisted file types are reported by their
# FourCC with no extension check.
FORMATS = {
    "H264": ("H.264", "video", (".mp4", ".m4v")),
    "H26B": ("H.264 Blu-ray", "video", (".m4v",)),
    "HEVC": ("HEVC (H.265)", "video", (".mp4",)),
    "MooV": ("QuickTime", "video", (".mov",)),
    "AVIV": ("AVI", "video", (".avi",)),
    "WMV ": ("Windows Media", "video", (".wmv",)),
    "MP4 ": ("3GPP", "video", (".3gp",)),
    "GIFf": ("Animated GIF", "video", (".gif",)),
    "flv ": ("FLV", "video", (".flv",)),
    "MXF ": ("MXF", "video", (".mxf",)),
    "PMXF": ("MXF OP1a", "video", (".mxf",)),
    "DMXF": ("MXF OP1a (DNx)", "video", (".mxf",)),
    "JMXF": ("MXF OP1a (JPEG 2000)", "video", (".mxf",)),
    "MXFX": ("MXF OP1a (XDCAM)", "video", (".mxf",)),
    "MX10": ("AS-10", "video", (".mxf",)),
    "MX11": ("AS-11", "video", (".mxf",)),
    "mpg2": ("MPEG2", "video", None),
    "dvd ": ("MPEG2-DVD", "video", None),
    "mbd ": ("MPEG2 Blu-ray", "video", None),
    "hbd ": ("H.264 Blu-ray", "video", None),
    "WAVE": ("Waveform Audio", "audio", (".wav",)),
    "AIFF": ("AIFF", "audio", (".aif", ".aiff")),
    "MP3 ": ("MP3", "audio", (".mp3",)),
    "AAC ": ("AAC Audio", "audio", (".aac", ".m4a", ".mp4")),
    "PNG ": ("PNG sequence", "image", (".png",)),
    "JPEG": ("JPEG sequence", "image", (".jpg", ".jpeg")),
    "TIFF": ("TIFF sequence", "image", (".tif", ".tiff")),
    "TPIC": ("Targa sequence", "image", (".tga",)),
    "DIBB": ("BMP sequence", "image", (".bmp",)),
    "DPX ": ("DPX sequence", "image", (".dpx",)),
    "oEXR": ("OpenEXR sequence", "image", (".exr",)),
    "GIFS": ("GIF sequence", "image", (".gif",)),
}
ALIASES = {"HEVC": ("h265",), "MooV": ("mov", "quicktime"), "WAVE": ("wav",), "AIFF": ("aif",)}

# One measurement (Premiere 27.1 b21, 22 Oct 2026): 79 s of 1080x1920 59.94 with H.264 "00 - Match Source - High
# bitrate" rendered in 52.7 s. Only used to warn that a long render may outlast the bridge's request limit.
MEASURED_RENDER_RATIO = 52.7 / 79


def _norm(text) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(text).lower())


def fourcc(value, base: int = 10) -> str | None:
    """A FourCC from a number: decimal as a preset's XML has it (1211250228), or base=16 for a folder name's hex
    ("48323634") -> "H264"; None when it is not four printable ASCII characters."""
    try:
        n = int(str(value).strip(), base)
        text = n.to_bytes(4, "big").decode("ascii")
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return None
    return text if text.isprintable() else None


def format_of(file_type: str | None) -> dict:
    """{format, fileType, kind, extensions} for a file-type FourCC (unknown ones keep their FourCC as the name)."""
    if file_type in FORMATS:
        name, kind, exts = FORMATS[file_type]
    else:
        name, kind, exts = ((file_type or "unknown").strip() or "unknown"), "other", None
    return {"format": name, "fileType": file_type, "kind": kind, "extensions": list(exts) if exts else None}


def _field(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return html.unescape(m.group(1)).strip() if m else None


def read_preset(path, source: str) -> dict:
    """One preset: name (file stem), title (its PresetName; Adobe's own carry a localisation key with the English
    default after "="), format from <ExporterFileType>, else from the folder name, and the path."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            text = f.read(HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        text = ""
    m = _FOLDER.match(p.parent.name)
    exporter = fourcc(_field(text, "ExporterClassID") or "") or (fourcc(m.group(1), 16) if m else None)
    file_type = fourcc(_field(text, "ExporterFileType") or "") or (fourcc(m.group(2), 16) if m else None)
    if exporter == "GIFS":  # the GIF sequence exporter writes GIFf files, one per frame
        file_type = "GIFS"
    title = _field(text, "PresetName") or ""
    if title.startswith("($$$/") and "=" in title:
        title = title.split("=", 1)[1].rstrip(")").strip()
    out = {"name": p.stem, "title": title or p.stem}
    out.update(format_of(file_type))
    out.update(source=source, path=str(p))
    return out


def system_dirs() -> list[str]:
    """Folders of Premiere's own presets: PPRO_PRESET_DIR (os.pathsep-separated), then MediaIO/systempresets next to
    every running Premiere executable (release and beta)."""
    out = []
    for part in (os.environ.get(PRESET_DIR_ENV) or "").split(os.pathsep):
        if part.strip() and os.path.isdir(part.strip()):
            out.append(part.strip())
    for exe in media.premiere_executables():
        d = os.path.join(os.path.dirname(exe), *SYSTEM_PARTS)
        if os.path.isdir(d) and os.path.normcase(d) not in {os.path.normcase(x) for x in out}:
            out.append(d)
    return out


def documents_dir() -> Path:
    """The user's Documents folder (it may be redirected, e.g. to a synced cloud folder: the shell folder in the registry wins)."""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "Personal")
            docs = Path(os.path.expandvars(value))
            if docs.is_dir():
                return docs
        except OSError:
            pass
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"


def user_dirs() -> list[str]:
    """The user's preset folders that exist: Documents/Adobe/<Adobe Media Encoder [(Beta)]>/<version>/Presets."""
    base = documents_dir() / "Adobe"
    out = []
    for app in AME_APPS:
        root = base / app
        if root.is_dir():
            out += [str(p) for p in sorted(root.glob("*/Presets")) if p.is_dir()]
    return out


def _scan(folder: str, source: str) -> list[dict]:
    found = []
    for root, dirs, files in os.walk(folder):
        if source == "system" and root == folder:
            dirs[:] = [d for d in dirs if d.lower() != "ingest"]  # Media Analysis: an ingest preset, not an export
        for f in sorted(files):
            if f.lower().endswith(".epr"):
                found.append(read_preset(os.path.join(root, f), source))
    return found


def matches_format(preset: dict, fmt) -> bool:
    """`fmt` ("h264", "H.264", "hevc", "quicktime", "mov", "wav", "mxf" …) names this preset's format: its name or
    FourCC (punctuation and case ignored), the start of its name, an alias, or an extension it writes."""
    q = _norm(fmt)
    if not q:
        return True
    name, ft = _norm(preset.get("format")), _norm(preset.get("fileType") or "")
    exts = [_norm(e) for e in preset.get("extensions") or []]
    return q in (name, ft) or name.startswith(q) or q in ALIASES.get(preset.get("fileType"), ()) or q in exts


def discover(fmt=None, *, system=None, user=None) -> dict:
    """Every preset in the system and user folders (`system` / `user`: folder lists, default system_dirs() /
    user_dirs()), optionally only one format. Returns {presets, systemDirs, userDirs, notes}."""
    system = system_dirs() if system is None else list(system)
    user = user_dirs() if user is None else list(user)
    presets, seen = [], set()
    for folder, source in [(d, "system") for d in system] + [(d, "user") for d in user]:
        for p in _scan(folder, source):
            key = os.path.normcase(os.path.abspath(p["path"]))
            if key not in seen and (fmt is None or matches_format(p, fmt)):
                seen.add(key)
                presets.append(p)
    notes = []
    if not system:
        notes.append("No running Premiere found and PPRO_PRESET_DIR is not set: Premiere's own presets (its "
                     "MediaIO/systempresets folder) are listed while Premiere runs. Only user presets are listed.")
    if not user:
        notes.append("No user preset folder (Documents/Adobe/Adobe Media Encoder/<version>/Presets) exists.")
    return {"presets": presets, "systemDirs": system, "userDirs": user, "notes": notes}


def _brief(p: dict) -> dict:
    return {k: p[k] for k in ("name", "format", "source", "path")}


def resolve(value, fmt=None, *, system=None, user=None) -> dict:
    """The preset `value` names: an .epr path (checked: no UNC, no stream, must exist), or a preset name — the file
    name without .epr, or its title — matched case-insensitively among discover(fmt). NOT_FOUND lists the closest
    names, AMBIGUOUS every match (narrow it with fmt or pass the path)."""
    text = str(value or "").strip()
    if not text:
        raise BridgeError("--preset is empty", code="BAD_PARAMS", outcome="not_committed")
    if text.lower().endswith(".epr") or "/" in text or "\\" in text:
        checked = paths.check(text, what="--preset", ext=".epr")
        p = Path(checked).expanduser()
        if not p.is_file():
            raise BridgeError(f"Preset not found: {p}", code="FILE_NOT_FOUND", outcome="not_committed")
        return read_preset(p.resolve(), "path")
    found = discover(fmt, system=system, user=user)
    pool = found["presets"]
    key = text.lower()
    hits = [p for p in pool if p["name"].lower() == key] or [p for p in pool if p["title"].lower() == key]
    if len(hits) == 1:
        return hits[0]
    if hits:
        formats = sorted({h["format"] for h in hits})
        how = "pass --format or the .epr path" if len(formats) > 1 else "pass the .epr path of the one you mean"
        raise BridgeError(f'{len(hits)} presets are called "{text}" ({", ".join(formats)}): {how}. Nothing was '
                          "rendered.", code="AMBIGUOUS", outcome="not_committed",
                          data={"matches": [_brief(h) for h in hits]})
    names = sorted({p["name"] for p in pool})
    close = difflib.get_close_matches(text, names, n=8, cutoff=0.5) or \
        [n for n in names if key in n.lower()][:8]
    where = f" in {fmt}" if fmt else ""
    raise BridgeError(f'No preset "{text}"{where}' + (f'; close: {", ".join(close)}' if close else "") +
                      ". list_presets shows them all; or pass the .epr path. Nothing was rendered.",
                      code="NOT_FOUND", outcome="not_committed",
                      data={"closeNames": close, "notes": found["notes"], "searched": found["systemDirs"] +
                            found["userDirs"]})


def render_warning(duration, preset: dict, limit_seconds: float) -> str | None:
    """A warning when a render of `duration` seconds may outlast the bridge's request limit (MEASURED_RENDER_RATIO;
    audio renders are far faster and are not warned about)."""
    if preset.get("kind") != "video" or not isinstance(duration, (int, float)):
        return None
    estimate = duration * MEASURED_RENDER_RATIO
    if estimate <= 0.8 * limit_seconds:
        return None
    return (f"The sequence is {duration / 60:.1f} min long: at the measured H.264 speed (79 s took 52.7 s) the render "
            f"takes about {estimate / 60:.0f} min, near or past the bridge's {limit_seconds / 60:.0f}-minute limit for "
            "one request. If it times out, Premiere keeps rendering and the panel reports stuck (Reload it): render "
            "long sequences from Premiere's Export dialog instead.")


def check_render(info: dict, seq: dict, preset: dict) -> tuple[dict, list[str]]:
    """The rendered file (media.probe_media `info`) against the sequence: frame size and frame rate (video presets),
    length within one frame (+1 ms). Returns (checks, warnings); every check has file / sequence / ok."""
    if not info:
        return {}, ["ffprobe could not read the file (not on PATH, or not a media file): frame size, frame rate and "
                    "length were not checked."]
    checks, warnings = {}, []
    fps = seq.get("fps") if isinstance(seq.get("fps"), (int, float)) and seq.get("fps") > 0 else None
    if preset.get("kind") == "video":
        if not info.get("width"):
            checks["video"] = {"file": False, "sequence": True, "ok": False}
            warnings.append("The file has no video stream.")
        else:
            got, want = [info["width"], info["height"]], [seq.get("width"), seq.get("height")]
            ok = got == want
            checks["frameSize"] = {"file": got, "sequence": want, "ok": ok}
            if not ok:
                warnings.append(f"The file is {got[0]}x{got[1]} but the sequence is {want[0]}x{want[1]}: the preset "
                                "scales or letterboxes the picture (a Match Source preset keeps the sequence's size).")
            if info.get("fps") and fps:
                ok = abs(info["fps"] - fps) <= 0.01
                checks["fps"] = {"file": round(info["fps"], 3), "sequence": fps, "ok": ok}
                if not ok:
                    warnings.append(f"The file runs at {info['fps']:.3f} fps, the sequence at {fps:g}: the preset "
                                    "converts the frame rate.")
    length = (info.get("videoDuration") if preset.get("kind") == "video" else None) or info.get("duration")
    want = seq.get("durationSeconds")
    if isinstance(length, (int, float)) and isinstance(want, (int, float)):
        tol = (1 / fps if fps else 1 / 30) + 1e-3
        ok = abs(length - want) <= tol
        checks["duration"] = {"file": round(length, 4), "sequence": round(want, 4), "ok": ok}
        if not ok:
            warnings.append(f"The file is {length:.3f} s long, the sequence {want:.3f} s: more than a frame apart "
                            "(a work area or in/out range, or a preset that changes timing).")
    if preset.get("kind") == "audio" and not info.get("hasAudio"):
        checks["audio"] = {"file": False, "sequence": True, "ok": False}
        warnings.append("The file has no audio stream.")
    return checks, warnings
