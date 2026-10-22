"""Reading a probe_api dump: which of the calls the UXP port depends on this Premiere build has, and what changed
since an earlier dump (a new Premiere build can add, say, a razor action - or take one away).

Members are in the panel's notation: "name(2)" a method and its arity, "name{get,set}" an accessor, "name:type" a data
property. A watch entry looks on a real instance first (the probe lists those under `instances`), then on the
export's prototype or static members; True / False, or "unknown" when the dump has neither (no open project, or no
clip on the timeline to look at).
"""

import re

# (label, instance key in the dump, export name, member) - instance or export may be None
WATCH = (
    ("Markers#createRemoveMarkerAction", "markers", None, "createRemoveMarkerAction"),
    ("Markers#createMoveMarkerAction", "markers", None, "createMoveMarkerAction"),
    ("Marker#createSetNameAction", "marker", None, "createSetNameAction"),
    ("VideoClipTrackItem#createRemoveVideoTransitionAction", "videoClipTrackItem", None, "createRemoveVideoTransitionAction"),
    ("VideoClipTrackItem#createSetDisabledAction", "videoClipTrackItem", None, "createSetDisabledAction"),
    ("VideoClipTrackItem#createSetNameAction", "videoClipTrackItem", None, "createSetNameAction"),
    ("VideoComponentChain#createInsertComponentAction", "videoComponentChain", None, "createInsertComponentAction"),
    ("VideoComponentChain#createRemoveComponentAction", "videoComponentChain", None, "createRemoveComponentAction"),
    ("AudioComponentChain#createAppendComponentAction", "audioComponentChain", None, "createAppendComponentAction"),
    ("ComponentParam#createRemoveKeyframeAction", "videoComponentParam", None, "createRemoveKeyframeAction"),
    ("ComponentParam#createRemoveKeyframeRangeAction", "videoComponentParam", None, "createRemoveKeyframeRangeAction"),
    ("ComponentParam#createSetInterpolationAtKeyframeAction", "videoComponentParam", None,
     "createSetInterpolationAtKeyframeAction"),
    ("Project#getSequences", "project", None, "getSequences"),
    ("Project#setActiveSequence", "project", None, "setActiveSequence"),
    ("Project#deleteSequence", "project", None, "deleteSequence"),
    ("Project#save", "project", None, "save"),
    ("Sequence#createCloneAction", "sequence", None, "createCloneAction"),
    ("Sequence#getSettings", "sequence", None, "getSettings"),
    ("Sequence#createSetSettingsAction", "sequence", None, "createSetSettingsAction"),
    ("Sequence#setPlayerPosition", "sequence", None, "setPlayerPosition"),
    ("Sequence#setSelection", "sequence", None, "setSelection"),
    ("Sequence#createSetInPointAction", "sequence", None, "createSetInPointAction"),
    ("VideoTrack#createSetNameAction", "videoTrack", None, "createSetNameAction"),
    ("SequenceEditor#createRazorTrackAction", "sequenceEditor", None, "createRazorTrackAction"),
    ("SequenceEditor#insertMogrtFromPath", "sequenceEditor", None, "insertMogrtFromPath"),
    ("ClipProjectItem#changeMediaFilePath", "clipProjectItem", None, "changeMediaFilePath"),
    ("ClipProjectItem#createSubClipAction", "clipProjectItem", None, "createSubClipAction"),
    ("FolderItem#createMoveItemAction", "rootFolder", None, "createMoveItemAction"),
    ("FolderItem#createRemoveItemAction", "rootFolder", None, "createRemoveItemAction"),
    ("FolderItem#createRenameBinAction", "rootFolder", None, "createRenameBinAction"),
    ("AudioFilterFactory.createComponentByDisplayName", None, "AudioFilterFactory", "createComponentByDisplayName"),
    ("AudioFilterFactory.getDisplayNames", None, "AudioFilterFactory", "getDisplayNames"),
    ("VideoFilterFactory.getMatchNames", None, "VideoFilterFactory", "getMatchNames"),
    ("Properties.getProperties", None, "Properties", "getProperties"),
    ("ProjectConverter.exportAsFinalCutProXML", None, "ProjectConverter", "exportAsFinalCutProXML"),
    ("ProjectConverter.exportAAF", None, "ProjectConverter", "exportAAF"),
    ("WorkAreaUtils.setWorkAreaInOutPoints", None, "WorkAreaUtils", "setWorkAreaInOutPoints"),
    ("SourceMonitor.openProjectItem", None, "SourceMonitor", "openProjectItem"),
    ("Metadata.getXMPMetadata", None, "Metadata", "getXMPMetadata"),
    ("MediaManager.purgeMediaCache", None, "MediaManager", "purgeMediaCache"),
    ("MogrtText", None, "MogrtText", None),
)

_NAME = re.compile(r"^([^({:]+)")


def member_names(members) -> set:
    """{"getName", ...} from ["getName(0)", "guid{get}", "name:string", "...12 more"]."""
    out = set()
    for m in members or []:
        if not isinstance(m, str) or m.startswith("..."):
            continue
        hit = _NAME.match(m)
        if hit:
            out.add(hit.group(1))
    return out


def _export_members(info) -> set:
    return member_names((info or {}).get("static")) | member_names((info or {}).get("proto")) | \
        member_names((info or {}).get("members"))


def has(dump, instance, export, member):
    """True / False / "unknown" for one watch entry."""
    instances = dump.get("instances") or {}
    exports = dump.get("premierepro") or {}
    if member is None:
        return export in exports
    if instance and instance in instances:
        if member in member_names(instances[instance]):
            return True
        if not export:
            return False
    if export:
        return member in _export_members(exports.get(export)) if export in exports else False
    return "unknown"


def watch(dump) -> dict:
    return {label: has(dump, inst, exp, member) for label, inst, exp, member in WATCH}


def flatten(dump) -> set:
    """Every member as one token: "Export.static", "Export#proto", "Export:member", "@instance#member"."""
    out = set()
    for name, info in (dump.get("premierepro") or {}).items():
        out.add(name)
        for m in info.get("static") or []:
            out.add(f"{name}.{m}")
        for m in info.get("proto") or []:
            out.add(f"{name}#{m}")
        for m in info.get("members") or []:
            out.add(f"{name}:{m}")
        for group, values in (info.get("nested") or {}).items():
            for v in values:
                out.add(f"{name}.{group}.{v}")
    for inst, members in (dump.get("instances") or {}).items():
        for m in members:
            out.add(f"@{inst}#{m}")
    return {t for t in out if "..." not in t}


def diff(old, new) -> dict:
    a, b = flatten(old), flatten(new)
    return {"from": (old.get("host") or {}), "to": (new.get("host") or {}),
            "added": sorted(b - a), "removed": sorted(a - b)}


def summarize(dump) -> dict:
    w = watch(dump)
    return {"host": dump.get("host"), "counts": dump.get("counts"), "problems": dump.get("problems") or [],
            "watch": w, "missing": sorted(k for k, v in w.items() if v is False),
            "unknown": sorted(k for k, v in w.items() if v == "unknown")}
