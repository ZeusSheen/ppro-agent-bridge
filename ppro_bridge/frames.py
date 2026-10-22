"""Frame export helpers: find the file Premiere really wrote, and build contact sheets."""

import struct
import time
from pathlib import Path

POLL = 0.05       # s between looks for the written file
BLACK_MAX = 16    # 8-bit luma: a frame whose brightest pixel is no brighter than this is all black
BLACK_WARNING = ("Premiere exported an all-black frame. On a real job some phone clips came out black here while "
                 "the Program monitor showed them fine (an export_frame problem, not the edit): look in Premiere "
                 "before acting on it.")


def resolve_exported_frame(data: dict, *, wait: float = 5.0) -> dict:
    """Work around Adobe's double-extension bug and async file writes.

    Polls the candidate file names (export_frame's `candidates`) for up to `wait` seconds and sets `resolvedPath`
    and `waitedSeconds`. If still missing, `resolvedPath` is None and `dirListing` shows what the directory holds.
    Returns `data`."""
    directory = data.get("dir")
    candidates = data.get("candidates") or []
    if not directory:
        return data

    start = time.monotonic()
    deadline = start + wait
    while True:
        data["waitedSeconds"] = round(time.monotonic() - start, 3)
        for name in candidates:
            p = Path(directory) / name
            if p.is_file():
                data["resolvedPath"] = str(p)
                return data
        # fuzzy pass: anything sharing the stem (Premiere may rename)
        stem = candidates[0].split(".")[0] if candidates else ""
        if stem:
            stem_hits = [f.name for f in Path(directory).glob(f"{stem}*") if f.is_file()]
            if len(stem_hits) == 1:
                data["resolvedPath"] = str(Path(directory) / stem_hits[0])
                data["note"] = "Resolved by stem match; Premiere altered the filename."
                return data
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL)

    data["resolvedPath"] = None
    try:
        data["dirListing"] = sorted(f.name for f in Path(directory).iterdir() if f.is_file())[:50]
    except OSError:
        data["dirListing"] = None
    data["note"] = (f"No matching file after {wait:g}s of polling. Check dirListing for the actual output name, "
                    "or the sequence may be shorter than the requested --at time.")
    return data


def frame_check(path) -> dict:
    """The written PNG's size (from its header) and, with Pillow, `black`: True when no pixel is brighter than
    BLACK_MAX."""
    out = {}
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            out["pixelWidth"], out["pixelHeight"] = struct.unpack(">II", head[16:24])
    except OSError:
        return out
    try:
        from PIL import Image
    except ImportError:
        return out
    try:
        with Image.open(path) as im:
            out["black"] = im.convert("L").getextrema()[1] <= BLACK_MAX
    except OSError:
        pass
    return out


def export_one_frame(rpc, directory, at: float | None = None, name: str | None = None, width=None,
                     height=None) -> dict:
    """Export one program frame into `directory` (sequence seconds `at`, default the player position); returns the
    panel's data plus `resolvedPath`, the time spent (`exportSeconds` in Premiere, `waitedSeconds` for the file),
    the PNG's size and `black` (with a `warning`). Raises BridgeError when the panel refuses."""
    if name is None:
        name = f"frame_{at:.3f}s.png" if at is not None else "frame_playhead.png"
    params = {"dir": str(directory), "name": name}
    if width and height:
        params["width"], params["height"] = width, height
    if at is not None:
        params["atSeconds"] = at
    start = time.monotonic()
    data = rpc("export_frame", params)
    data["exportSeconds"] = round(time.monotonic() - start, 3)
    resolve_exported_frame(data)
    if data.get("resolvedPath"):
        data.update(frame_check(data["resolvedPath"]))
        if data.get("black"):
            data["warning"] = BLACK_WARNING
    return data


def make_contact_sheet(frames: list[tuple[float, str]], out: Path, cols: int, thumb_w: int) -> str | None:
    """A labelled grid of the frames; None when Pillow is missing or there is nothing to show."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    tiles = []
    for at, path in frames:
        im = Image.open(path).convert("RGB")
        h = round(im.height * thumb_w / im.width)
        im = im.resize((thumb_w, h))
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, thumb_w, 18), fill=(0, 0, 0))
        d.text((5, 3), f"{at:.2f}s", fill=(255, 220, 0))
        tiles.append(im)
    if not tiles:
        return None
    th = max(t.height for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (thumb_w * min(cols, len(tiles)), th * rows), (20, 20, 20))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * thumb_w, (i // cols) * th))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    return str(out)
