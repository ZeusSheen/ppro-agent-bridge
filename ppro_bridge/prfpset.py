"""Premiere effect presets (.prfpset) for list_effect_presets and apply_preset: where the files are, what a preset
holds, and how it becomes set_params requests. The panel is not changed for this: set_params does every write.

The format, as checked on 27.0 beta's Factory and Lumetri Presets (en_US and zh_CN) and on a user file saved by
26.0 (2 Nov 2026):
  * <PremiereData> holds flat objects linked by ObjectID / ObjectRef: Tree -> RootBin -> BinTreeItem (a folder of the
    Effects panel: TreeItemBase/Name, Items/Item) -> TreeItem (one preset) -> FilterPresetItem -> its FilterPresets,
    either one <FilterPreset ObjectRef> (Factory Presets) or a <FilterPresets> list (Lumetri Presets, user presets)
    -> FilterPreset (FilterMatchName, Type, anchors, MediaType) -> Component (VideoFilterComponent: MatchName,
    Component/Params/Param Index -> the param objects).
  * A <FilterPresets> list is in reverse Effect Controls order: user presets end with the intrinsic Opacity, which
    Effect Controls shows above every filter.
  * FilterPreset/Type: 0 = keyframes stretched by clip length / (AnchorOutPoint - AnchorInPoint), 1 = anchored to the
    in point, 2 = anchored to the out point. Keyframe times are absolute ticks (254016000000 per second); Adobe's own
    anchors sit at 1 h (a still's default in point). Offsets are signed: a key may lie before the anchor in point.
  * A param's static value is the 2nd field of <StartKeyframe> (<CurrentValue> is often a stale 0). <Keyframes> is
    "ticks,value,interpolation,…;…" with interpolation 0 linear, 4 hold, 5 bezier; the other fields (ease influence
    and velocity, a point's path handles) cannot be set over UXP.
  * Param indexes are the <Params> order (<Node><Children> lists them differently). Names are in the language of the
    Premiere that saved the preset (one user file mixes Chinese and English effects), some params have none, an
    older preset can carry an older translation (Motion's anchor point: 定位点 then, 锚点 on 27.1), and
    <ParameterID> is an AE param id or -1, not an index.
  * Values by param ClassID: numbers, booleans ("true"/"false"), integers (pop-ups), points "x:y" (normalised 0-1,
    as set_params takes Motion's Position), colours (a 64-bit integer, 16 bits per channel, alpha first:
    0xff00ff00ff00ff00 is opaque white), and ArbVideoComponentParam blobs (base64: LUTs, curves, mask paths), which
    UXP cannot write. Lumetri presets carry 14 of them, and their 98 params no longer line up with 27.1's 130.
  * Masks are SubComponents (AE.ADBE AEMask2) of the effect they limit; UXP cannot create them.
Nothing here talks to Premiere except apply_preset (`apply`), through the rpc it is given.
"""

import difflib
import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from . import framegrid, media, paths
from .errors import BridgeError
from .presets import documents_dir

TICKS_PER_SECOND = 254016000000
PRESET_DIR_ENV = "PPRO_EFFECT_PRESET_DIR"
USER_FILE = "Effect Presets and Custom Items.prfpset"
USER_APPS = ("Premiere Pro", "Premiere Pro (Beta)")
DEFAULT_LOCALE = "en_US"

VIDEO = "228cda18-3625-4d2d-951e-348879e4ed93"  # FilterPreset/MediaType of a video effect (as in .prproj files)
AUDIO = "80b8e3d5-6dca-4195-aefb-cb5f407ab009"
INTRINSIC = ("AE.ADBE Motion", "AE.ADBE Opacity")  # on every video clip: a preset writes the existing one
BUILTIN = ("AE.ADBE Time Remapping",)
# Params of these intrinsics whose order has not changed since CS6 (Motion: Position, Scale, Scale Width, Uniform
# Scale, Rotation, Anchor Point, Anti-flicker Filter — 27.1 appends four Crop params; Opacity: Opacity, Blend Mode).
# Trusted by index even when the preset's names are in another language.
STABLE_PARAMS = {"AE.ADBE Motion": 7, "AE.ADBE Opacity": 2}
# Old match names that 26.5.1 no longer lists, and the effect that replaced them (both "Transform").
ALIASES = {"AE.ADBE Geometry": "AE.ADBE Geometry2"}
ANCHORS = {0: "scale", 1: "in", 2: "out"}
KINDS = {  # param ClassID -> value kind
    "fe47129e-6c94-4fc0-95d5-c056a517aaf3": "number",
    "a4ff2d6e-7ac2-44f8-9d52-17d9ca50e542": "number",
    "cc12343e-f113-4d3b-ae05-b287db77d461": "boolean",
    "6e02e8bb-2569-46b2-8ab1-4ab11c43e9c8": "integer",
    "0fde4e9f-f895-4ba3-b0fe-9a6feafda583": "colour",
    "ca81d347-309b-44d2-acc7-1c572efb973c": "point",
    "313e54d4-6903-49ad-b0bf-8262cdd10f4e": "arb",
}
WRITABLE = ("number", "integer", "boolean", "point")
GROUP_CONTROLS = ("11", "12", "16")  # boolean "params" that open, close or pad a group of the Effect Controls UI
HOLD, BEZIER = 4, 5
EASED = 3  # Premiere's unnamed mode that eases both ends (SKILL.md, measured on 27.1): the stand-in for bezier
INTERPOLATION = {0: "linear", HOLD: "hold", BEZIER: EASED}
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯豈-﫿]")
_LOCALE = re.compile(r"^([A-Za-z]{2})[_-]([A-Za-z]{2})$")


def _bad(message: str, **data) -> BridgeError:
    return BridgeError(message, code="BAD_PARAMS", outcome="not_committed", data=data or None)


def _fold(text) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold().strip()


def norm_name(text) -> str:
    """A param name for comparison: accents, case, spaces and punctuation ignored ("色相饱和度 曲线" = "色相饱和度曲线")."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in text if not unicodedata.combining(c) and c.isalnum()).casefold()


def norm_locale(value) -> str | None:
    """"zh_cn" / "zh-CN" -> "zh_CN"; None for an empty value; BAD_PARAMS for anything else."""
    if value is None or not str(value).strip():
        return None
    m = _LOCALE.match(str(value).strip())
    if not m:
        raise _bad(f"--locale {value!r} is not a locale such as zh_CN or en_US")
    return f"{m.group(1).lower()}_{m.group(2).upper()}"


# ---------------------------------------------------------------- finding the files

def _version_key(name: str):
    parts = name.split(".")
    return tuple(int(p) for p in parts) if parts and all(p.isdigit() for p in parts) else None


def user_files() -> list[str]:
    """The user's own preset files: Documents/Adobe/Premiere Pro [(Beta)]/<version>/Profile-*/Effect Presets and Custom
    Items.prfpset, from the newest version folder of each app that has one (release and beta keep separate ones)."""
    base = documents_dir() / "Adobe"
    out = []
    for app in USER_APPS:
        root = base / app
        if not root.is_dir():
            continue
        versions = sorted((d for d in root.iterdir() if d.is_dir() and _version_key(d.name)),
                          key=lambda d: _version_key(d.name), reverse=True)
        for folder in versions:
            found = sorted(folder.glob(f"Profile-*/{USER_FILE}"))
            if found:
                out += [str(f) for f in found]
                break
    return out


def bundled_dirs(locale: str | None) -> tuple[list[str], list[str]]:
    """Folders of Premiere's own preset files (Factory Presets, Lumetri Presets): PPRO_EFFECT_PRESET_DIR
    (os.pathsep-separated folders or .prfpset files), then LocalizedPresets/<locale>/Effect Presets next to every
    running Premiere executable (en_US when that locale is missing). Returns (folders or files, notes)."""
    out, notes = [], []
    for part in (os.environ.get(PRESET_DIR_ENV) or "").split(os.pathsep):
        part = part.strip()
        if part and (os.path.isdir(part) or part.lower().endswith(".prfpset") and os.path.isfile(part)):
            out.append(part)
    for exe in media.premiere_executables():
        root = Path(exe).parent / "LocalizedPresets"
        want = root / (locale or DEFAULT_LOCALE) / "Effect Presets"
        folder = want if want.is_dir() else root / DEFAULT_LOCALE / "Effect Presets"
        if not folder.is_dir():
            continue
        if folder != want:
            notes.append(f"{root} has no {locale} presets: listing {DEFAULT_LOCALE}.")
        if os.path.normcase(str(folder)) not in {os.path.normcase(x) for x in out}:
            out.append(str(folder))
    return out, notes


def discover(locale: str | None, *, bundled=None, user=None) -> dict:
    """Every preset file: Premiere's own (`bundled`: folders or files, default bundled_dirs(locale)) and the user's
    (`user`: files, default user_files()). Returns {files: [{path, source}], notes}."""
    notes = []
    if bundled is None:
        bundled, notes = bundled_dirs(locale)
    user = user_files() if user is None else list(user)
    files, seen = [], set()

    def add(path, source):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            files.append({"path": str(path), "source": source})

    for entry in bundled:
        if os.path.isdir(entry):
            for name in sorted(os.listdir(entry)):
                if name.lower().endswith(".prfpset"):
                    add(os.path.join(entry, name), "bundled")
        else:
            add(entry, "bundled")
    for path in user:
        add(path, "user")
    if not bundled:
        notes.append(f"No running Premiere found and {PRESET_DIR_ENV} is not set: Premiere's own presets (Factory, "
                     "Lumetri) are listed while Premiere runs. Only the user's presets are listed.")
    if not user:
        notes.append(f"No user preset file (Documents/Adobe/Premiere Pro/<version>/Profile-*/{USER_FILE}) exists.")
    return {"files": files, "notes": notes}


def check_file(value) -> str:
    """--file: a .prfpset path as typed (no UNC, no stream) that exists."""
    text = paths.check(value, what="--file", ext=".prfpset")
    p = Path(text).expanduser()
    if not p.is_file():
        raise BridgeError(f"Preset file not found: {p}", code="FILE_NOT_FOUND", outcome="not_committed")
    return str(p.resolve())


# ---------------------------------------------------------------- parsing

def _index(el) -> int:
    try:
        return int(el.get("Index"))
    except (TypeError, ValueError):
        return 0


def _int(text, default=0) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(text).strip()))
        except (TypeError, ValueError):
            return default


def _value(kind: str, raw):
    """A preset value as set_params takes it (None when it does not parse): float, int, bool, [x, y], or the packed
    colour integer."""
    if raw is None:
        return None
    raw = str(raw).strip()
    try:
        if kind == "number":
            return float(raw)
        if kind in ("integer", "colour"):
            return _int(raw, None)
        if kind in ("boolean", "group"):
            return {"true": True, "false": False, "1": True, "0": False}.get(raw.lower())
        if kind == "point":
            x, y = raw.split(":")
            return [float(x), float(y)]
    except ValueError:
        return None
    return None


def _guess(raw) -> str:
    """The kind of a param whose ClassID is not in KINDS, from its value."""
    raw = str(raw or "").strip()
    if raw.lower() in ("true", "false"):
        return "boolean"
    if ":" in raw:
        return "point"
    try:
        float(raw)
    except ValueError:
        return "unknown"
    return "number"


def colour_hex(packed) -> str | None:
    """A preset colour (16 bits per channel, alpha first) as #RRGGBB plus the alpha in hex: '#ffffff ff'."""
    if not isinstance(packed, int) or isinstance(packed, bool):
        return None
    a, r, g, b = ((packed >> s) & 0xFFFF for s in (48, 32, 16, 0))
    return f"#{r >> 8:02x}{g >> 8:02x}{b >> 8:02x} {a >> 8:02x}"


def _param(index: int, el) -> dict:
    kind = "arb" if el.tag.startswith("Arb") else KINDS.get((el.get("ClassID") or "").lower())
    control = (el.findtext("ParameterControlType") or "").strip()
    start = (el.findtext("StartKeyframe") or "").split(",")
    raw = start[1] if len(start) > 1 else el.findtext("CurrentValue")
    if kind is None:
        kind = _guess(raw)
    if kind == "boolean" and control in GROUP_CONTROLS:
        kind = "group"
    keys = []
    if kind not in ("arb", "group"):
        for chunk in (el.findtext("Keyframes") or "").split(";"):
            fields = chunk.strip().split(",")
            if len(fields) < 2 or not fields[0].strip():
                continue
            keys.append([_int(fields[0]), _value(kind, fields[1]), _int(fields[2]) if len(fields) > 2 else 0])
    value = _value(kind, raw)
    if kind in WRITABLE and (value is None or any(k[1] is None for k in keys)):
        kind = "unknown"
    return {"index": index, "name": el.findtext("Name"), "kind": kind, "control": control, "value": value,
            "keys": keys}


def _effect(objs: dict, fp) -> dict:
    comp = objs.get(fp.find("Component").get("ObjectRef")) if fp.find("Component") is not None else None
    match = (fp.findtext("FilterMatchName") or (comp.findtext("MatchName") if comp is not None else "") or "").strip()
    media_type = (fp.findtext("MediaType") or "").strip().lower()
    if media_type == VIDEO or (not media_type and comp is not None and comp.tag.startswith("Video")):
        kind = "video"
    else:
        kind = "audio" if media_type == AUDIO or (comp is not None and "Audio" in comp.tag) else "other"
    typ = _int(fp.findtext("Type"), 0)
    params = []
    plist = comp.find("Component/Params") if comp is not None else None
    for p in sorted(plist if plist is not None else [], key=_index):
        el = objs.get(p.get("ObjectRef"))
        if el is not None:
            params.append(_param(_index(p), el))
    subs = comp.find("SubComponents") if comp is not None else None
    return {"matchName": match, "displayName": comp.findtext("Component/DisplayName") if comp is not None else None,
            "kind": kind, "type": typ, "anchor": ANCHORS.get(typ), "anchorIn": _int(fp.findtext("AnchorInPoint")),
            "anchorOut": _int(fp.findtext("AnchorOutPoint")), "masks": len(subs) if subs is not None else 0,
            "params": params}


_CACHE = {}


def read_file(path, source: str = "path") -> list[dict]:
    """Every preset in one .prfpset file, in Effects panel order: {name, bin (folders below the root, "/"-joined),
    file, source, description, effects (in Effect Controls order)}. Parsed files are cached by size and time."""
    p = Path(path)
    try:
        stat = p.stat()
    except OSError as e:
        raise BridgeError(f"Cannot read {p}: {e}", code="FILE_NOT_FOUND", outcome="not_committed") from e
    key = (os.path.normcase(str(p.resolve())), stat.st_size, stat.st_mtime_ns)
    if key in _CACHE:
        return [dict(x, source=source) for x in _CACHE[key]]
    data = p.read_bytes()
    if b"<!DOCTYPE" in data[:65536] or b"<!ENTITY" in data[:65536]:
        raise _bad(f"{p.name}: a document type declaration is not part of a Premiere preset file; refused.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise _bad(f"{p.name} is not a Premiere preset file ({e})") from e
    if root.tag != "PremiereData":
        raise _bad(f"{p.name} is not a Premiere preset file (its root is <{root.tag}>, not <PremiereData>)")
    objs = {el.get("ObjectID"): el for el in root if el.get("ObjectID") is not None}
    tree = next((el for el in root if el.tag == "Tree" and el.get("ObjectID") is not None), None)
    rb = tree.find("RootBin") if tree is not None else None
    presets, visited = [], set()

    def walk(oid, bins, depth):
        el = objs.get(oid)
        if el is None or oid in visited or depth > 64:
            return
        visited.add(oid)
        if el.tag == "BinTreeItem":
            sub = bins + [el.findtext("TreeItemBase/Name") or ""] if depth else bins  # the root bin is not shown
            items = el.find("Items")
            for item in sorted(items if items is not None else [], key=_index):
                walk(item.get("ObjectRef"), sub, depth + 1)
        elif el.tag == "TreeItem":
            data_ref = el.find("TreeItemBase/Data")
            fpi = objs.get(data_ref.get("ObjectRef")) if data_ref is not None else None
            if fpi is None or fpi.tag != "FilterPresetItem":
                return  # a transition preset or another kind of item
            listed = fpi.find("FilterPresets")
            refs = sorted(listed, key=_index) if listed is not None else [fpi.find("FilterPreset")]
            stored = [objs.get(r.get("ObjectRef")) for r in refs if r is not None]
            stored = [fp for fp in stored if fp is not None and fp.tag == "FilterPreset"]
            presets.append({"name": (el.findtext("TreeItemBase/Name") or "").strip(), "bin": "/".join(bins),
                            "file": str(p), "source": source,
                            "description": next((d for d in (fp.findtext("Description") for fp in stored) if d), ""),
                            "effects": [_effect(objs, fp) for fp in reversed(stored)]})

    if rb is not None:
        walk(rb.get("ObjectRef"), [], 0)
    _CACHE[key] = presets
    return [dict(x, source=source) for x in presets]


def load(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """(every preset of these files, per file {path, source, count} or {path, source, error})."""
    presets, report = [], []
    for f in files:
        try:
            found = read_file(f["path"], f["source"])
        except BridgeError as e:
            report.append({"path": f["path"], "source": f["source"], "error": str(e)})
            continue
        presets += found
        report.append({"path": f["path"], "source": f["source"], "count": len(found)})
    return presets, report


# ---------------------------------------------------------------- what can be replayed

def is_animated(effect: dict) -> bool:
    return any(p["keys"] for p in effect["params"] if p["kind"] in WRITABLE)


def effect_target(effect: dict, available=None) -> tuple[str, str | None]:
    """(the match name to write, None) or (match name, why the effect is skipped). `available`: the video match
    names this Premiere can add (list_effects), or None when unknown."""
    m = effect["matchName"]
    if effect["kind"] != "video":
        return m, ("an audio effect: the bridge adds audio effects by display name only (add_audio_effect)"
                   if effect["kind"] == "audio" else "not a video effect")
    if m in BUILTIN:
        return m, "a built-in component the bridge does not write (time remapping)"
    if effect["masks"]:
        return m, (f"limited by {effect['masks']} mask(s), which UXP cannot create; applied unmasked it would change "
                   "the whole frame")
    if m in INTRINSIC or available is None or m in available:
        return m, None
    alias = ALIASES.get(m)
    if alias and alias in available:
        return alias, None
    return m, "this Premiere does not offer it (not in list_effects --kind video)" + \
        (f", nor {alias}" if alias else "")


SKIPPED_PARAMS = {  # kind -> (why apply_preset skips such a param, the short form list_effect_presets gives)
    "arb": ("arbitrary data (a LUT, curve or path) that UXP cannot write", "arbitrary-data (LUT, curve) param(s)"),
    "colour": ("a colour: writing colours over UXP is unverified, set it by hand", "colour param(s)"),
    "unknown": ("a value the bridge cannot read", "unreadable param(s)"),
}


def param_skips(effect: dict) -> list[dict]:
    """Params set_params cannot write: arbitrary data, colours, values that do not parse ({index, name, reason};
    a colour also gives its value as hex)."""
    out = []
    for p in effect["params"]:
        if p["kind"] in SKIPPED_PARAMS:
            row = {"index": p["index"], "name": p["name"], "reason": SKIPPED_PARAMS[p["kind"]][0]}
            if p["kind"] == "colour":
                row["value"] = colour_hex(p["value"])
            out.append(row)
    return out


def summary(preset: dict, available=None, file_index=None) -> dict:
    """One preset for list_effect_presets: effects, animation, anchoring, and whether apply_preset can replay it
    ("full", "partial" with reasons, "none"). Param names and counts are checked against a clip only by apply_preset.
    `file_index` replaces the file's path by its index in the listing's `files` (a long list stays readable)."""
    reasons, usable = [], 0
    anchors = {e["anchor"] or "scale" for e in preset["effects"] if e["kind"] == "video" and is_animated(e)}
    for e in preset["effects"]:
        target, why = effect_target(e, available)
        if why:
            reasons.append(f"{e['matchName']}: {why}")
            continue
        usable += 1
        kinds = Counter(p["kind"] for p in e["params"] if p["kind"] in SKIPPED_PARAMS)
        reasons += [f"{target}: {n} {SKIPPED_PARAMS[k][1]} skipped" for k, n in sorted(kinds.items())]
    reasons = list(dict.fromkeys(reasons))  # two masked Offsets give one line
    out = {"name": preset["name"], "bin": preset["bin"], "source": preset["source"]}
    out.update({"fileIndex": file_index} if file_index is not None else {"file": preset["file"]})
    out["effects"] = [e["matchName"] for e in preset["effects"]]
    out["animated"] = bool(anchors)
    out["anchor"] = (anchors.pop() if len(anchors) == 1 else "mixed") if anchors else None
    out["replay"] = "none" if not usable else ("partial" if reasons else "full")
    if reasons:
        out["reasons"] = reasons
    return out


def _signature(preset: dict) -> str:
    return json.dumps(preset["effects"], sort_keys=True)


def resolve(presets: list[dict], query) -> tuple[dict, list[str]]:
    """The preset `query` names: its name (exact, then ignoring case) or its bin path and name ("预设/模糊/快速模糊入点";
    the leading bins may be left off). Copies with the same content (two Premiere versions running) count as one.
    AMBIGUOUS lists every match with its bin and file, NOT_FOUND the closest names. Returns (preset, notes)."""
    text = str(query or "").strip()
    if not text:
        raise _bad("--preset is empty")
    key = _fold(text)
    hits = [p for p in presets if p["name"] == text] or [p for p in presets if _fold(p["name"]) == key]
    if not hits and "/" in text:
        hits = [p for p in presets if _fold(f"{p['bin']}/{p['name']}") == key
                or _fold(f"{p['bin']}/{p['name']}").endswith("/" + key)]
    distinct = {}
    for h in hits:
        distinct.setdefault(_signature(h), h)
    if len(distinct) == 1:
        notes = [f"{len(hits)} identical copies of this preset were found; using {hits[0]['file']}."] \
            if len(hits) > 1 else []
        return hits[0], notes
    if hits:
        raise BridgeError(f'{len(distinct)} different presets are called "{text}": name one by its bin path '
                          '("<bin>/<name>") or pass --file. Nothing was changed.', code="AMBIGUOUS",
                          outcome="not_committed",
                          data={"matches": [{"name": h["name"], "bin": h["bin"], "file": h["file"],
                                             "effects": [e["matchName"] for e in h["effects"]]} for h in hits]})
    names = sorted({p["name"] for p in presets})
    close = difflib.get_close_matches(text, names, n=8, cutoff=0.5) or [n for n in names if key in _fold(n)][:8]
    raise BridgeError(f'No effect preset "{text}"' + (f'; close: {", ".join(close)}' if close else "") +
                      ". list_effect_presets shows them all. Nothing was changed.", code="NOT_FOUND",
                      outcome="not_committed", data={"closeNames": close})


def search(presets: list[dict], text) -> list[dict]:
    key = _fold(text)
    return [p for p in presets if key in _fold(p["name"]) or key in _fold(p["bin"])]


# ---------------------------------------------------------------- keyframes on a clip

def clip_times(effect: dict, keys: list, duration: float) -> list[tuple]:
    """The preset's keys as (seconds from the clip's first frame, value, interpolation), by the anchoring Type:
    scale (0) stretches the preset's span over the clip, in (1) keeps offsets from the in point, out (2) from the
    out point. Offsets keep their sign (a key may precede the anchor)."""
    a_in, a_out = effect["anchorIn"], effect["anchorOut"]
    span = (a_out - a_in) / TICKS_PER_SECOND
    out = []
    for ticks, value, mode in keys:
        if effect["type"] == 1:
            t = (ticks - a_in) / TICKS_PER_SECOND
        elif effect["type"] == 2:
            t = duration + (ticks - a_out) / TICKS_PER_SECOND
        else:
            t = (ticks - a_in) / TICKS_PER_SECOND * (duration / span if span > 0 else 1.0)
        out.append((t, value, mode))
    return sorted(out, key=lambda k: k[0])


def _lerp(kind: str, a, b, f: float):
    if kind == "number":
        return a + (b - a) * f
    if kind == "point":
        return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]
    return a  # booleans and pop-ups step


def value_at(kind: str, keys: list, t: float):
    """The preset curve's value at `t`: held before the first and after the last key and after a hold key, linear in
    between (a bezier segment is approximated by its straight line)."""
    if t <= keys[0][0]:
        return keys[0][1]
    for (ta, va, ma), (tb, vb, _) in zip(keys, keys[1:]):
        if t <= tb:
            if ma == HOLD or tb <= ta:
                return va if t < tb else vb
            return _lerp(kind, va, vb, (t - ta) / (tb - ta))
    return keys[-1][1]


def _mode_at(keys: list, t: float) -> int:
    mode = keys[0][2]
    for k in keys:
        if k[0] <= t:
            mode = k[2]
    return mode


def fit_keys(kind: str, keys: list, duration: float, tol: float) -> tuple[list, list]:
    """(keys inside the clip, times of the keys that fell outside). set_params takes keys inside the clip only, so a
    key past either edge is replaced by one on that edge holding the preset curve's value there: the clip shows what
    Premiere shows of the preset. Keys within `tol` (half a frame) of an edge are put on it."""
    lo, hi = -tol, duration + tol
    inside = [(min(max(t, 0.0), duration), v, m) for t, v, m in keys if lo <= t <= hi]
    outside = [t for t, _, _ in keys if not lo <= t <= hi]
    if not outside:
        return inside, []
    out = []
    if any(t < lo for t, _, _ in keys) and not any(t == 0.0 for t, _, _ in inside):
        out.append((0.0, value_at(kind, keys, 0.0), _mode_at(keys, 0.0)))
    out += inside
    if any(t > hi for t, _, _ in keys) and not any(t == duration for t, _, _ in inside):
        out.append((duration, value_at(kind, keys, duration), _mode_at(keys, duration)))
    return out, outside


def _clean(v):
    if isinstance(v, float):
        return round(v, 6) + 0.0
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def interpolation_for(keys: list) -> tuple[object, str | None]:
    """set_params' interpolation for one param (it sets one mode on all its new keys) from the preset keys' modes
    (the last key's mode shapes no segment): linear, hold, bezier -> 3 (eased). Mixed modes: the most common one."""
    modes = [k[2] for k in (keys[:-1] if len(keys) > 1 else keys)]
    counts = Counter(INTERPOLATION.get(m, "linear") for m in modes)
    pick = counts.most_common(1)[0][0]
    note = None
    if len(counts) > 1:
        note = f"mixed interpolation ({', '.join(str(k) for k in counts)}): {pick} used for every key"
    unknown = sorted({m for m in modes if m not in INTERPOLATION})
    if unknown:
        note = (note + "; " if note else "") + f"interpolation code(s) {unknown} taken as linear"
    return pick, note


def param_request(effect: dict, p: dict, duration: float, tol: float) -> tuple[dict, dict | None]:
    """One set_params param entry for a clip `duration` seconds long, and what changed on the way (trimmed keys,
    a curve that is constant on the clip, mixed interpolation) or None."""
    if not p["keys"]:
        return {"index": p["index"], "value": _clean(p["value"])}, None
    keys, outside = fit_keys(p["kind"], clip_times(effect, p["keys"], duration), duration, tol)
    info = {}
    if outside:
        info["keysOutsideClip"] = [round(t, 6) for t in outside]
    values = {json.dumps(_clean(v)) for _, v, _ in keys}
    if len(values) == 1:
        info["held"] = "the preset's animation is constant on this clip: written as a static value"
        return {"index": p["index"], "value": _clean(keys[0][1])}, info
    interp, note = interpolation_for(keys)
    if note:
        info["interpolation"] = note
    req = {"index": p["index"], "timeBase": "clip",
           "keyframes": [{"t": round(t, 6) + 0.0, "value": _clean(v)} for t, v, _ in keys], "interpolation": interp}
    return req, info or None


# ---------------------------------------------------------------- checking params against this Premiere

def compare_names(preset_params: list[dict], live: dict, match_name: str) -> tuple[str | None, list[str]]:
    """Param names of the preset against the live effect's ({index: name}) by index. Returns (why the effect must be
    skipped or None, notes). A name found at another index means the params moved (27.1's Lumetri); a name found
    nowhere is taken as a renamed param (an older translation); no name in common, in another script (Chinese vs
    Latin), means another language: only the stable params of Motion and Opacity are trusted then."""
    pre = {p["index"]: norm_name(p["name"]) for p in preset_params}
    liv = {i: norm_name(n) for i, n in live.items() if n is not None}
    pairs = [(i, pre[i], liv[i]) for i in pre if i in liv and pre[i] and liv[i]]
    if not pairs:
        return None, []
    same = [i for i, a, b in pairs if a == b]
    diff = [(i, a, b) for i, a, b in pairs if a != b]
    if not diff:
        return None, []
    raw_pre = {p["index"]: p["name"] for p in preset_params}
    moved = [i for i, a, b in diff
             if any(n == a for j, n in liv.items() if j != i) or any(n == b for j, n in pre.items() if j != i)]
    if moved:
        i = moved[0]
        return (f"its params changed: the preset's #{i} ({raw_pre[i]!r}) is not this Premiere's #{i} "
                f"({live.get(i)!r})", [])
    other_script = all(bool(_CJK.search(raw_pre[i] or "")) != bool(_CJK.search(live.get(i) or "")) for i, _, _ in diff)
    if not same and other_script:
        stable = STABLE_PARAMS.get(match_name, 0)
        if all(i < stable for i in pre):
            return None, [f"{match_name}: the preset's param names are in another language; its first {stable} "
                          "params keep a fixed order, so they are written by index"]
        return ("its param names are in another language, so the param order cannot be checked against this "
                "Premiere's", [])
    return None, [f"{match_name}: param #{i} is called {raw_pre[i]!r} in the preset and {live.get(i)!r} here "
                  "(taken as a renamed param)" for i, _, _ in diff]


_SET_ENTRY = re.compile(r"^(?P<m>.*)\[(?P<o>\d+)\]#(?P<i>\d+)(?: (?P<n>.*))?$", re.S)


def set_names(entries, match_name: str, occurrence: int) -> dict:
    """{index: name} of one component from a set_params reply's `set` ("<match>[<occ>]#<index> <name>"); a param
    the panel could not name yet (an effect that lists its params only once added) is left out."""
    out = {}
    for entry in entries or []:
        m = _SET_ENTRY.match(str(entry))
        if m and m.group("m") == match_name and int(m.group("o")) == occurrence and m.group("n") is not None:
            out[int(m.group("i"))] = m.group("n")
    return out


# ---------------------------------------------------------------- apply_preset

def _brief(preset: dict) -> dict:
    return {"name": preset["name"], "bin": preset["bin"], "file": preset["file"], "source": preset["source"]}


def _speed(clip: dict) -> float:
    try:
        return float(clip.get("speed", 1))
    except (TypeError, ValueError):
        return 1.0


def _same(a, b) -> bool:
    if isinstance(a, dict) and "x" in a:
        a = [a.get("x"), a.get("y")]
    if isinstance(b, dict) and "x" in b:
        b = [b.get("x"), b.get("y")]
    if isinstance(a, bool) or isinstance(b, bool):
        return all(isinstance(v, (bool, int, float)) for v in (a, b)) and bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 1e-3 * max(1.0, abs(a), abs(b))
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return all(_same(x, y) for x, y in zip(a, b))
    return a == b


def pick_clips(clips: list, at, frm, to, mode, tol) -> tuple[list, list]:
    """(the target clips, the clips that only overlap the range: mode inside). `at`: the clip under it (the one
    starting there at a cut); mode inside: clips entirely in [frm, to] with `tol` of slack; overlap: every clip
    reaching more than `tol` into it."""
    if at is not None:
        hit = [c for c in clips if c["start"] - 1e-6 <= at < c["end"] - 1e-6] or \
              [c for c in clips if c["start"] - 1e-6 <= at <= c["end"] + 1e-6]
        return hit[:1], []
    if mode == "overlap":
        return [c for c in clips if c["start"] < to - tol and c["end"] > frm + tol], []
    inside = [c for c in clips if c["start"] >= frm - tol and c["end"] <= to + tol]
    return inside, [{"name": c.get("name"), "start": c.get("start"), "end": c.get("end")}
                    for c in clips if c not in inside and c["start"] < to - tol and c["end"] > frm + tol]


def plan_effects(preset: dict, available) -> tuple[list[dict], list[dict], list[str]]:
    """(the effects to write, in Effect Controls order: {position, matchName (the live one), effect, intrinsic,
    params (writable), skippedParams, ignoredParams}; the skipped effects {position, matchName, reason}; notes)."""
    plan, skipped, notes = [], [], []
    for pos, e in enumerate(preset["effects"]):
        target, why = effect_target(e, available)
        if why:
            skipped.append({"position": pos, "matchName": e["matchName"], "reason": why})
            continue
        plan.append({"position": pos, "matchName": target, "effect": e, "intrinsic": target in INTRINSIC,
                     "params": [p for p in e["params"] if p["kind"] in WRITABLE], "skippedParams": param_skips(e),
                     "ignoredParams": sum(1 for p in e["params"] if p["kind"] == "group")})
        if target != e["matchName"]:
            notes.append(f"{e['matchName']} is written as {target} (the effect that replaced it).")
    return plan, skipped, notes


def clip_components(plan: list, clip: dict, tol: float) -> tuple[list, list]:
    """(set_params components for one clip, what was adjusted on the way). Intrinsics address the clip's own
    component (occurrence 1); every added effect is a new instance after those of the same name the clip has."""
    duration = clip["end"] - clip["start"]
    have = Counter(x.get("matchName") for x in clip.get("components") or [])
    seen, comps, adjusted = Counter(), [], []
    for item in plan:
        m = item["matchName"]
        if item["intrinsic"]:
            occ = 1
        else:
            seen[m] += 1
            occ = have[m] + seen[m]
        params = []
        for p in item["params"]:
            req, info = param_request(item["effect"], p, duration, tol)
            params.append(req)
            if info:
                adjusted.append(dict({"matchName": m, "occurrence": occ, "index": p["index"]}, **info))
        if params or not item["intrinsic"]:  # an added effect is added even when none of its params can be written
            comps.append({"matchName": m, "occurrence": occ, "params": params})
    return comps, adjusted


def check_live(rpc, track_idx: int, clip: dict, plan: list) -> tuple[list[dict], list[str], list[int]]:
    """Each planned effect's params against this Premiere's, by index, once (on `clip`): an intrinsic's from the
    clip's chain, an added effect's through a set_params dry run of its params (it creates a factory instance and
    commits nothing; the reply names the params, a refusal means they do not fit). Returns (skips, notes, the
    positions of added effects whose params the dry run could not name: 27.1 names them only once the effect is on
    a clip, so check_after_adding checks those)."""
    chain = clip.get("components") or []
    have, seen = Counter(c.get("matchName") for c in chain), Counter()
    skips, notes, unchecked = [], [], []
    for item in plan:
        e, m = item["effect"], item["matchName"]
        size = max((p["index"] for p in e["params"]), default=-1) + 1
        why = None
        if item["intrinsic"]:
            comp = next((x for x in chain if x.get("matchName") == m), None)
            live = {p.get("index"): p.get("name") for p in (comp or {}).get("params") or []}
            if comp is None:
                why = f"the clip has no {m} component"
            elif size > len(live):
                why = f"the preset has {size} params, this Premiere's {m} has {len(live)}"
        else:
            seen[m] += 1
            occ = have[m] + seen[m]
            probe = [{"matchName": m, "occurrence": occ,
                      "params": [{"index": p["index"], "value": _clean(p["keys"][0][1] if p["keys"] else p["value"])}
                                 for p in item["params"]]}]
            try:
                reply = rpc("set_params", {"videoTrack": track_idx, "atSeconds": (clip["start"] + clip["end"]) / 2,
                                           "components": probe, "dryRun": True})
            except BridgeError as err:
                if err.code not in ("NOT_FOUND", "BAD_PARAMS"):
                    raise
                reply, why = {}, f"a set_params dry run refused the preset's params for it ({err})"
            live = set_names(reply.get("set"), m, occ)
            if not why and item["params"] and not live:
                unchecked.append(item["position"])
                continue
        if not why:
            why, more = compare_names(e["params"], live, m)
            notes += more
        if why:
            skips.append({"position": item["position"], "matchName": e["matchName"], "reason": why})
    return skips, notes, unchecked


def check_after_adding(rpc, track_idx: int, clip: dict, plan: list, undo: str) -> dict:
    """The added effects whose params a dry run could not name (27.1 lists an effect's params only once it is on a
    clip): add the plan's effects to `clip` without params (one undo step, "<undo> [add effects]"), read them back,
    compare their params with the preset's like check_live, and take each misfit off again (one step each, the last
    instance first). The others stay: the clip's set_params request then finds them in place.
    Returns {skips, notes, added, removed, undoSteps}."""
    mid = (clip["start"] + clip["end"]) / 2
    have, seen, comps, where = Counter(c.get("matchName") for c in clip.get("components") or []), Counter(), [], {}
    for item in plan:
        if not item["intrinsic"]:
            seen[item["matchName"]] += 1
            where[item["position"]] = (item["matchName"], have[item["matchName"]] + seen[item["matchName"]])
            comps.append({"matchName": item["matchName"], "occurrence": where[item["position"]][1], "params": []})
    reply = rpc("set_params", {"videoTrack": track_idx, "atSeconds": mid, "components": comps, "undoLabel": undo})
    out = {"added": reply.get("createdEffects") or [], "removed": [], "skips": [], "notes": [],
           "undoSteps": 1 if reply.get("createdEffects") else 0}
    doc = rpc("inspect_items", {"videoTracks": [track_idx], "fromSeconds": mid, "toSeconds": mid})
    chain = next((c.get("components") or [] for t in doc.get("tracks") or [] for c in t.get("clips") or []
                  if c.get("start", 0) - 1e-6 <= mid < c.get("end", 0)), [])
    misfits = []
    for item in (i for i in plan if i.get("unchecked")):
        m, occ = where[item["position"]]
        same = [x for x in chain if x.get("matchName") == m]
        live = {p.get("index"): p.get("name") for p in (same[occ - 1].get("params") or [])} if occ <= len(same) else {}
        size = max((p["index"] for p in item["effect"]["params"]), default=-1) + 1
        if occ > len(same):
            why = f"{m}[{occ}] was not on the clip after adding it"
        elif size > len(live):
            why = f"the preset has {size} params, this Premiere's {m} has {len(live)}"
        else:
            why, more = compare_names(item["effect"]["params"], live, m)
            out["notes"] += more
        if why:
            out["skips"].append({"position": item["position"], "matchName": item["effect"]["matchName"],
                                 "reason": why + " (checked on the first clip after adding it, then taken off again)"})
            if occ <= len(same):
                misfits.append((m, occ))
    for m, occ in sorted(misfits, key=lambda x: -x[1]):  # the last instance first: the others keep their numbers
        try:
            rpc("remove_effect", {"videoTrack": track_idx, "atSeconds": mid, "matchName": m, "occurrence": occ,
                                  "undoLabel": f"{undo} [take off {m}]"})
        except BridgeError as err:
            err.outcome = "partial" if err.outcome in (None, "not_committed") else err.outcome
            err.data = dict(err.data or {}, completed=[f"added {', '.join(out['added'])}"] +
                            [f"took off {x}" for x in out["removed"]])
            raise
        out["removed"].append(f"{m}[{occ}]")
        out["undoSteps"] += 1
    return out


def _keyframed_intrinsics(targets: list, plan: list) -> list[dict]:
    out = []
    for c in targets:
        for item in (i for i in plan if i["intrinsic"]):
            comp = next((x for x in c.get("components") or [] if x.get("matchName") == item["matchName"]), None)
            live = {p.get("index"): p for p in (comp or {}).get("params") or []}
            for p in item["params"]:
                lp = live.get(p["index"])
                if lp and lp.get("keyframed") and lp.get("keyframes"):
                    out.append({"clip": c.get("name"), "start": c["start"], "end": c["end"],
                                "matchName": item["matchName"], "index": p["index"], "name": lp.get("name")})
    return out


def _notes_for(plan: list) -> list[str]:
    notes = []
    keyed = [p for i in plan for p in i["params"] if p["keys"]]
    if any(k[2] == BEZIER for p in keyed for k in p["keys"]):
        notes.append("Bezier keyframes are written with Premiere's eased interpolation (mode 3, both ends eased): the "
                     "preset's own ease handles (influence, velocity) cannot be set over UXP.")
    if any(p["kind"] == "point" for p in keyed):
        notes.append("Animated points move between their keyframes without the preset's spatial (path) handles, "
                     "which UXP cannot set.")
    if any(i["effect"]["anchor"] == "scale" and any(p["keys"] for p in i["params"]) for i in plan):
        notes.append("Keyframes of a scaling preset (Type 0) are stretched to each clip's length.")
    if any(i["ignoredParams"] for i in plan):
        notes.append("Group headers of the Effect Controls (boolean pseudo-params) were left alone.")
    return notes


def verify(clip: dict | None, comps: list) -> list[str]:
    """What differs between the components written and the clip read back: a missing component, a static value, or
    a param's keyframe count or first / last value."""
    if clip is None:
        return ["the clip was not found when reading it back"]
    out = []
    chain = clip.get("components") or []
    for comp in comps:
        same = [x for x in chain if x.get("matchName") == comp["matchName"]]
        if comp["occurrence"] > len(same):
            out.append(f"{comp['matchName']}[{comp['occurrence']}] is not on the clip")
            continue
        live = {p.get("index"): p for p in same[comp["occurrence"] - 1].get("params") or []}
        for p in comp["params"]:
            where = f"{comp['matchName']}[{comp['occurrence']}]#{p['index']}"
            lp = live.get(p["index"])
            if lp is None:
                out.append(f"{where} is missing")
            elif "keyframes" in p:
                got = lp.get("keyframes") or []
                if not lp.get("keyframed") or len(got) != len(p["keyframes"]):
                    out.append(f"{where}: {len(got)} keyframe(s) read back, {len(p['keyframes'])} written")
                elif not (_same(got[0].get("value"), p["keyframes"][0]["value"])
                          and _same(got[-1].get("value"), p["keyframes"][-1]["value"])):
                    out.append(f"{where}: keyframe values read back differently")
            elif not _same(lp.get("value"), p["value"]):
                out.append(f"{where}: {lp.get('value')!r} read back, {p['value']!r} written")
    return out


def apply(rpc, track_idx: int, query, *, at=None, frm=None, to=None, mode="inside", file=None, locale=None,
          ping=None, seq=None, dry_run=False, label=None) -> dict:
    """apply_preset: resolve the preset, read the clips, check every effect against this Premiere, build one
    set_params request per clip, dry-run all of them, then (unless dry_run) write them and read the clips back.
    `ping` (the handshake's reply) gives the UI locale, `seq` (get_sequence) the frame rate."""
    loc = norm_locale(locale) or norm_locale(((ping or {}).get("hostInfo") or {}).get("uiLocale")) or DEFAULT_LOCALE
    found = {"files": [{"path": check_file(file), "source": "path"}], "notes": []} if file else discover(loc)
    presets, files = load(found["files"])
    try:
        preset, notes = resolve(presets, query)
    except BridgeError as e:
        if e.code == "NOT_FOUND":
            e.data = dict(e.data or {}, files=files, notes=found["notes"])
        raise
    seq = seq or rpc("get_sequence")
    tol = 0.5 / (framegrid.fps_from_timebase(seq.get("timebase"), seq.get("fps") or 30.0) or 30.0)
    tl = f"V{track_idx + 1}"

    available = {e.get("matchName") for e in rpc("list_effects", {"kind": "video"}).get("effects") or []
                 if e.get("kind", "video") == "video" and e.get("matchName")}
    if not available:
        available = None
        notes.append("list_effects named no video effects: whether this Premiere offers each effect was not checked.")
    plan, skipped, more = plan_effects(preset, available)
    notes += more

    def refuse(message, code="UNSUPPORTED", **data):
        raise BridgeError(message + " Nothing was changed.", code=code, outcome="not_committed",
                          data=dict({"preset": _brief(preset), "skipped": skipped}, **data))

    if not plan:
        refuse(f'Nothing of the preset "{preset["name"]}" can be applied over UXP (see data.skipped).')

    doc = rpc("inspect_items", {"videoTracks": [track_idx], "fromSeconds": at if at is not None else frm,
                                "toSeconds": at if at is not None else to})
    clips = [c for t in doc.get("tracks") or [] for c in t.get("clips") or []
             if isinstance(c.get("start"), (int, float)) and isinstance(c.get("end"), (int, float))]
    targets, outside = pick_clips(clips, at, frm, to, mode, tol)
    if not targets:
        where = f"at {at}s" if at is not None else \
            f"{'reaching into' if mode == 'overlap' else 'entirely inside'} {frm}–{to}s"
        raise BridgeError(f"No clip on {tl} {where}. Nothing was changed.", code="NOT_FOUND", outcome="not_committed",
                          data={"onlyOverlapping": outside} if outside else None,
                          hint="--mode overlap includes the clips that only overlap the range" if outside else None)
    if any(p["keys"] for i in plan for p in i["params"]):
        fast = [f"{c.get('name')} at {c['start']}s plays at {_speed(c)}x" for c in targets
                if abs(_speed(c) - 1) > 1e-6]
        if fast:
            refuse("An animated preset needs clips at speed 1 (its keyframes are placed in clip time): " +
                   "; ".join(fast) + ".", clips=fast)
    keyed = _keyframed_intrinsics(targets, plan)
    if keyed:
        k = keyed[0]
        raise BridgeError("Keyframed on the clip, so the preset would have to replace those keyframes: " +
                          "; ".join(f"{x['clip']} at {x['start']}s {x['matchName']} #{x['index']} {x['name']}"
                                    for x in keyed) + ". Nothing was changed.", code="KEYFRAMED",
                          outcome="not_committed", data={"keyframed": keyed},
                          hint=f"when replacing them is what the user wants, remove them first: remove_keyframes "
                               f"--track {tl} --at {round((k['start'] + k['end']) / 2, 3)} --match-name "
                               f"\"{k['matchName']}\" --param {k['index']} --all")

    live_skips, more, unchecked = check_live(rpc, track_idx, targets[0], plan)
    notes += more
    skipped = sorted(skipped + live_skips, key=lambda s: s["position"])
    dropped = {s["position"] for s in live_skips}
    plan = [dict(i, unchecked=i["position"] in unchecked) for i in plan if i["position"] not in dropped]
    if not plan:
        refuse(f'Nothing of the preset "{preset["name"]}" fits this Premiere\'s effects (see data.skipped).')
    if unchecked:
        added = ", ".join(i["matchName"] for i in plan if i["unchecked"])
        notes.append(f"This Premiere names an added effect's params only once it is on a clip: a real run adds "
                     f"{added} to the first clip, compares the params with the preset's and takes an effect whose "
                     "params do not line up off again (skipped, checkedOnClip)." if dry_run else
                     f"This Premiere names an added effect's params only once it is on a clip: {added} were "
                     "checked on the first clip right after adding them (checkedOnClip).")
    undo = label or f"Agent: preset {preset['name']} on {tl}"

    def build():
        # Every request is dry-run before the first write: a refusal on the third clip changes nothing on the first.
        rows, requests = [], []
        for c in targets:
            comps, adjusted = clip_components(plan, c, tol)
            request = {"videoTrack": track_idx, "atSeconds": round((c["start"] + c["end"]) / 2, 6),
                       "components": comps, "undoLabel": undo}
            dry = rpc("set_params", dict(request, dryRun=True))
            row = {"name": c.get("name"), "start": c["start"], "end": c["end"], "set": dry.get("set") or [],
                   "createdEffects": dry.get("createdEffects") or []}
            row["undoSteps"] = (1 if row["createdEffects"] else 0) + (1 if row["set"] else 0) + \
                (1 if any(p.get("keyframes") for comp in comps for p in comp["params"]) else 0)
            if adjusted:
                row["adjusted"] = adjusted
            if dry_run:
                row["request"] = request
            rows.append(row)
            requests.append(request)
        return rows, requests

    def report():
        out = {"preset": _brief(preset), "track": tl, "dryRun": bool(dry_run), "locale": loc,
               "effects": [{"position": i["position"], "matchName": i["matchName"], "intrinsic": i["intrinsic"],
                            "animated": any(p["keys"] for p in i["params"]),
                            "anchor": i["effect"]["anchor"] if any(p["keys"] for p in i["params"]) else None,
                            "params": len(i["params"]), "skippedParams": i["skippedParams"],
                            "paramsChecked": not (i["unchecked"] and dry_run)} for i in plan],
               "skipped": skipped,
               "replay": "partial" if skipped or any(i["skippedParams"] for i in plan) else "full",
               "clips": rows, "undoSteps": sum(r["undoSteps"] for r in rows) + (checked or {}).get("undoSteps", 0)}
        if checked:
            out["checkedOnClip"] = {k: checked[k] for k in ("added", "removed", "undoSteps")}
        if at is None:
            out.update(rangeMode=mode, onlyOverlapping=outside)
        if notes:
            out["notes"] = notes
        return out

    checked = None
    rows, requests = build()
    notes += _notes_for(plan)
    if dry_run:
        return report()
    if unchecked:
        checked = check_after_adding(rpc, track_idx, targets[0], plan, undo)
        notes += checked["notes"]
        skipped = sorted(skipped + checked["skips"], key=lambda s: s["position"])
        dropped = {s["position"] for s in checked["skips"]}
        plan = [i for i in plan if i["position"] not in dropped]
        if not plan:
            raise BridgeError(f'Nothing of the preset "{preset["name"]}" fits this Premiere\'s effects (see '
                              "data.skipped): the effects were added to the first clip to check their params and "
                              "taken off again, so the clip is as it was (the undo steps are in data.checkedOnClip).",
                              code="UNSUPPORTED", outcome="committed",
                              data={"preset": _brief(preset), "skipped": skipped, "checkedOnClip": checked})
    done = [f"added {', '.join(checked['added'])} to the first clip to check them"] + \
        [f"took off {x}" for x in checked["removed"]] if checked else []
    if checked:
        try:
            rows, requests = build()  # the first clip has its effects now: its request finds them in place
        except BridgeError as err:  # the first clip has changed already: not "nothing changed"
            err.outcome = "partial" if err.outcome in (None, "not_committed") else err.outcome
            err.data = dict(err.data or {}, completed=done)
            raise

    out = report()
    for row, request in zip(rows, requests):
        try:
            reply = rpc("set_params", request)
        except BridgeError as err:
            if done:
                err.outcome = "partial" if err.outcome in (None, "not_committed") else err.outcome
                err.data = dict(err.data or {}, completed=done)
            raise
        row["set"] = reply.get("set") or row["set"]
        row["createdEffects"] = reply.get("createdEffects") or []
        if reply.get("interpolation"):
            row["interpolation"] = reply["interpolation"]
        done.append(f"{row['name']} at {row['start']}s")
    after = rpc("inspect_items", {"videoTracks": [track_idx], "fromSeconds": min(r["start"] for r in rows),
                                  "toSeconds": max(r["end"] for r in rows)})
    back = [c for t in after.get("tracks") or [] for c in t.get("clips") or []]
    for row, request in zip(rows, requests):
        clip = next((c for c in back if isinstance(c.get("start"), (int, float))
                     and abs(c["start"] - row["start"]) < 1e-4 and c.get("name") == row["name"]), None)
        mismatches = verify(clip, request["components"])
        row["verified"] = not mismatches
        if mismatches:
            row["mismatches"] = mismatches
    out["verified"] = all(r["verified"] for r in rows)
    if not out["verified"]:
        raise BridgeError("The preset was written, but the clips read back differently (see data.clips[].mismatches).",
                          code="VERIFY_FAILED", outcome="committed", data=out)
    return out


def list_presets(rpc, *, file=None, search_text=None, locale=None) -> dict:
    """list_effect_presets: every preset on disk with its effects and whether apply_preset can replay it. When the
    bridge answers, Premiere's UI locale picks the bundled files and list_effects marks effects this Premiere cannot
    add; without it the files are still listed (`premiere` says what was not checked)."""
    loc = norm_locale(locale)
    premiere = {"checked": False}
    info = None
    try:
        info = rpc("ping")
    except BridgeError as e:
        premiere["why"] = f"{e.code}: {e}"
    if info is not None:
        host = info.get("hostInfo") or {}
        premiere["host"] = f"{host.get('name') or 'premierepro'} {host.get('version') or ''}".strip()
        loc = loc or norm_locale(host.get("uiLocale"))
    loc = loc or DEFAULT_LOCALE
    found = {"files": [{"path": check_file(file), "source": "path"}], "notes": []} if file else discover(loc)
    presets, files = load(found["files"])
    if search_text:
        presets = search(presets, search_text)
    available = None
    if info is not None and "list_effects" in (info.get("actions") or []) and (info.get("protocol") or 1) >= 2:
        try:
            reply = rpc("list_effects", {"kind": "video"})
            available = {e.get("matchName") for e in reply.get("effects") or [] if e.get("matchName")}
            premiere["checked"] = True
        except BridgeError as e:
            premiere["why"] = f"{e.code}: {e}"
    elif info is not None:
        premiere["why"] = "the loaded panel has no list_effects (reload it)"
    index = {os.path.normcase(f["path"]): i for i, f in enumerate(files)}
    rows = [summary(p, available, index.get(os.path.normcase(p["file"]))) for p in presets]
    out = {"count": len(rows), "presets": rows, "files": files, "locale": loc, "premiere": premiere,
           "replay": dict(Counter(r["replay"] for r in rows))}
    if search_text:
        out["search"] = search_text
    notes = list(found["notes"])
    if not premiere["checked"]:
        notes.append("Effects were not checked against Premiere (list_effects): a preset marked full may still use "
                     "an effect this Premiere does not offer.")
    notes.append("Param names and counts are checked against the clip by apply_preset (--dry-run shows it).")
    out["notes"] = notes
    return out
