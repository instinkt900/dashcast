"""Generate the pre-rendered "stale frame" badge.

Runs where Pillow is available (dev machine or the server Docker image) — NOT on
the Pi. Produces a raw framebuffer blob (OFFLINE_BOX_WIDTH x OFFLINE_BOX_HEIGHT
in the panel's fb_format) that gets shipped to the Pi and composited into the
top-right corner of the last good frame when the render server can't be reached.

Deliberately a small corner badge rather than a centred card over a dimmed
frame: a dashboard that's a few minutes old is still useful, so the job here is
to annotate it, not to hide it.
"""

from __future__ import annotations

import io
import logging

from dashcast.config import load_config
from dashcast.constants import OFFLINE_BOX_HEIGHT, OFFLINE_BOX_WIDTH

log = logging.getLogger("dashcast.offline")

_RED = (210, 58, 58)
_TEXT = (255, 255, 255)


def render_offline_box(text: str, fb_format: str) -> bytes:
    """Render the badge to raw framebuffer bytes: a solid red pill with the label
    centred in it."""
    from PIL import Image, ImageDraw

    from dashcast.serve import png_to_framebuffer

    w, h = OFFLINE_BOX_WIDTH, OFFLINE_BOX_HEIGHT
    img = Image.new("RGB", (w, h), _RED)
    draw = ImageDraw.Draw(img)

    # Square corners on purpose. The display blits these bytes straight into the
    # framebuffer, which has no alpha, so rounded ends would have to be filled
    # with *some* opaque colour — and we can't know what the dashboard pixels
    # underneath look like. Faking it would leave four wrong-coloured notches on
    # some frames. A crisp tag with a darker 1px edge reads cleanly over anything.
    draw.rectangle([0, 0, w - 1, h - 1], fill=_RED, outline=(150, 30, 30), width=1)

    font = _fit_font(draw, text, w - 16, h - 8)
    draw.text((w // 2, h // 2), text, font=font, fill=_TEXT, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return png_to_framebuffer(buf.getvalue(), w, h, fb_format)


def _fit_font(draw, text: str, max_w: int, max_h: int):
    """Largest DejaVu Bold that fits `text` inside the badge.

    The badge size is a fixed contract with the display, so an over-long --text
    must shrink to fit rather than silently spill past the edge.
    """
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        for size in range(20, 9, -1):
            try:
                font = ImageFont.truetype(path, size)
            except OSError:
                break  # this path isn't usable at all; try the next one
            l, t, r, b = draw.textbbox((0, 0), text, font=font)
            if (r - l) <= max_w and (b - t) <= max_h:
                if size < 20:
                    log.info("shrank badge text to %dpx so %r fits", size, text)
                return font
    log.warning("no truetype font found (or %r never fits); using bitmap fallback", text)
    return ImageFont.load_default()


def run_make_offline(config_path: str, out_path: str, text: str) -> int:
    cfg = load_config(config_path)
    fmt = cfg.render.fb_format or cfg.display.fb_format or "rgb565"
    blob = render_offline_box(text, fmt)
    with open(out_path, "wb") as fh:
        fh.write(blob)
    log.info(
        "wrote %s (%d bytes, %dx%d, %s, text=%r)",
        out_path,
        len(blob),
        OFFLINE_BOX_WIDTH,
        OFFLINE_BOX_HEIGHT,
        fmt,
        text,
    )
    return 0
