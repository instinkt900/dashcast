"""Generate the pre-rendered "offline" notice box.

Runs where Pillow is available (dev machine or the server Docker image) — NOT on
the Pi. Produces a raw framebuffer blob (OFFLINE_BOX_WIDTH x OFFLINE_BOX_HEIGHT
in the panel's fb_format) that gets shipped to the Pi and composited over a
dimmed last frame when the render server is unreachable.
"""

from __future__ import annotations

import io
import logging

from dashcast.config import load_config
from dashcast.constants import OFFLINE_BOX_HEIGHT, OFFLINE_BOX_WIDTH

log = logging.getLogger("dashcast.offline")


def render_offline_box(text: str, subtext: str, fb_format: str) -> bytes:
    """Render the notice box to raw framebuffer bytes."""
    from PIL import Image, ImageDraw

    from dashcast.serve import png_to_framebuffer

    w, h = OFFLINE_BOX_WIDTH, OFFLINE_BOX_HEIGHT
    img = Image.new("RGB", (w, h), (24, 24, 27))  # dark card
    draw = ImageDraw.Draw(img)

    # Rounded border + a warning accent strip down the left edge.
    draw.rounded_rectangle([1, 1, w - 2, h - 2], radius=16, outline=(220, 60, 60), width=3)
    draw.rounded_rectangle([1, 1, 14, h - 2], radius=8, fill=(220, 60, 60))

    title_font, sub_font, glyph_font = _load_fonts()

    # Warning glyph
    draw.text((44, h // 2), "⚠", font=glyph_font, fill=(240, 200, 60), anchor="lm")
    # Title + subtitle
    draw.text((104, h // 2 - 16), text, font=title_font, fill=(245, 245, 245), anchor="lm")
    draw.text((104, h // 2 + 20), subtext, font=sub_font, fill=(170, 170, 175), anchor="lm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return png_to_framebuffer(buf.getvalue(), w, h, fb_format)


def _load_fonts():
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            title = ImageFont.truetype(path, 28)
            sub = ImageFont.truetype(path, 18)
            glyph = ImageFont.truetype(path, 44)
            return title, sub, glyph
        except OSError:
            continue
    default = ImageFont.load_default()
    log.warning("no truetype font found; using tiny bitmap fallback")
    return default, default, default


def run_make_offline(config_path: str, out_path: str, text: str, subtext: str) -> int:
    cfg = load_config(config_path)
    fmt = cfg.render.fb_format or cfg.display.fb_format or "rgb565"
    blob = render_offline_box(text, subtext, fmt)
    with open(out_path, "wb") as fh:
        fh.write(blob)
    log.info("wrote %s (%d bytes, %dx%d, %s)", out_path, len(blob), OFFLINE_BOX_WIDTH, OFFLINE_BOX_HEIGHT, fmt)
    return 0
