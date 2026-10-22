"""Frame-grid arithmetic shared by apply_plan and replace_clip.

Premiere counts time in ticks (254016000000 per second); a sequence's timebase is
ticks per frame, so the exact rate is TICKS / timebase — 59.94 is really 60000/1001.
"""

TICKS_PER_SECOND = 254016000000


def fps_from_timebase(timebase, fallback: float | None = None) -> float | None:
    try:
        ticks = int(timebase)
    except (TypeError, ValueError):
        return fallback
    return TICKS_PER_SECOND / ticks if ticks > 0 else fallback


def source_rate(media: dict, seq_fps: float) -> tuple[float, str]:
    """The frame rate a project item's source points fall on, and which grid that is ("sequence" | "media").
    The interpretation is not readable over UXP: a file within 0.2 % of the sequence rate (60 vs 59.94) is taken to
    be interpreted at the sequence rate (what replace_clip and set_fps expect), any other known rate is the file's
    own, and an unknown one (no ffprobe) is the sequence's."""
    native = media.get("fps")
    if native and abs(native / seq_fps - 1) > 0.002:
        return native, "media"
    return seq_fps, "sequence"


def covers_whole_media(src_in: float, src_out: float, media_duration: float | None, fps: float) -> bool:
    """A range from the first frame to (nearly) the last. Placing it with an explicit out point
    gets that point clamped against the native duration and loses a frame, so such ranges are
    placed as the whole clip instead."""
    if not media_duration:
        return False
    return src_in <= 0.5 / fps and src_out >= media_duration - 1.5 / fps


def whole_extents(media: dict, seq_fps: float) -> list[float]:
    """Seconds a whole-clip placement may occupy. The project item's interpretation is not
    readable over UXP, so both the file's own rate and the sequence rate (the usual override,
    e.g. 60 interpreted as 59.94) are possible; frames / rate gives each length."""
    frames, native = media.get("frames"), media.get("fps")
    if frames and native:
        return sorted({frames / native, frames / seq_fps})
    return [media["duration"]] if media.get("duration") else []
