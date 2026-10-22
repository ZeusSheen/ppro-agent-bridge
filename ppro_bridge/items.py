"""Project items, tracks and the panel handshake: what every command resolves before it writes."""

import os

from . import spec
from ._version import VERSION
from .errors import BridgeError


def parse_track(track) -> tuple[str, int]:
    """"V3" -> ("video", 2), "A1" -> ("audio", 0); a bare int means a video track (0-based)."""
    s = str(track).strip().upper()
    if s.isdigit():
        return "video", int(s)
    if len(s) >= 2 and s[0] in "VA" and s[1:].isdigit() and int(s[1:]) >= 1:
        return ("video" if s[0] == "V" else "audio"), int(s[1:]) - 1
    raise ValueError(f'Bad track "{track}" — use V1, V2, A1, …')


def item_ref(item: dict) -> str:
    return (f"{item['bin']}/" if item.get("bin") else "") + item["name"]


def resolve_item(ref: str, items: list[dict] | None = None, unique: bool = False, *, rpc=None) -> dict:
    """The project item a user means: an exact name, or "<bin path>/<name>" ("/name" = root bin).

    Same-named items with different media are ambiguous (an EN and a CN render, say); with unique=True any second
    match is (interpretation is per item). Pass `items` (list_project_items) or `rpc` to fetch them."""
    if items is None:
        if rpc is None:
            raise TypeError("resolve_item needs items or rpc")
        items = rpc("list_project_items").get("items", [])
    hits = [i for i in items if i["name"] == ref]
    if not hits and "/" in ref:
        bin_path, name = ref.rsplit("/", 1)
        hits = [i for i in items if i["name"] == name and (i.get("bin") or "") == bin_path]
    if not hits:
        raise BridgeError(f'No project item "{ref}".', code="NOT_FOUND", outcome="not_committed")
    if len(hits) > 1 and (unique or len({os.path.normcase(i.get("mediaPath") or "") for i in hits}) > 1):
        raise BridgeError(f'{len(hits)} project items are named "{hits[0]["name"]}" '
                          f'({", ".join(item_ref(i) for i in hits)}). Name one as "<bin path>/<name>".',
                          code="AMBIGUOUS", outcome="not_committed",
                          data={"matches": [item_ref(i) for i in hits]})
    return hits[0]


def check_expect_seq(rpc, expected: str | None) -> dict | None:
    """Refuse to write when the active sequence is not the one the caller means; returns get_sequence data."""
    if expected is None:
        return None
    seq = rpc("get_sequence")
    if seq.get("name") != expected:
        raise BridgeError(f'The active sequence is "{seq.get("name")}", not "{expected}"; nothing was changed.',
                          code="SEQUENCE_MISMATCH", outcome="not_committed")
    return seq


def require_panel(rpc, actions, *, min_protocol: int = 1) -> dict:
    """Ping and make sure the loaded panel knows the given actions and speaks at least `min_protocol`
    (a 0.3.3 panel reports no protocol: 1). Refuses with PANEL_OUTDATED; returns the ping data."""
    info = rpc("ping")
    protocol = info.get("protocol") or 1
    missing = [a for a in actions if a not in (info.get("actions") or [])]
    if missing or protocol < min_protocol:
        why = []
        if missing:
            why.append(f"lacks {', '.join(missing)}")
        if protocol < min_protocol:
            why.append(f"speaks protocol {protocol}, this command needs {min_protocol}")
        raise BridgeError(f"The loaded panel ({info.get('plugin')}) {' and '.join(why)}. This CLI is {VERSION}: "
                          f"load/reload the Agent Bridge {VERSION} panel in UXP Developer Tool.",
                          code="PANEL_OUTDATED", outcome="not_committed",
                          data={"missing": missing, "panelProtocol": protocol, "needProtocol": min_protocol})
    return info


def require_command(rpc, name: str) -> dict | None:
    """The handshake a CLI command needs (actions.json `commands`): with requirePanel or minProtocol >= 2, every
    action it calls must exist and the panel's protocol must be high enough. Returns the ping data, or None when
    the command needs no handshake."""
    cmd = spec.command(name)
    if cmd is None:
        raise ValueError(f"unknown command {name!r}")
    if not cmd.get("requirePanel") and (cmd.get("minProtocol") or 1) < 2:
        return None
    return require_panel(rpc, cmd.get("calls") or [], min_protocol=cmd.get("minProtocol") or 1)
