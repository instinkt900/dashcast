"""Shared constants — the contract both ends agree on."""

# Native panel resolution of the target display (Raspberry Pi Zero W: 800x480 @ 16bpp).
DEFAULT_WIDTH = 800
DEFAULT_HEIGHT = 480

# Filenames both ends agree on. The server writes them atomically; the display
# fetches one. PNG is for humans/debugging; the .fb is raw framebuffer bytes the
# Pi writes straight to /dev/fb0.
DEFAULT_IMAGE_NAME = "dashboard.png"
DEFAULT_FB_NAME = "dashboard.fb"

# Bytes per pixel for each supported raw framebuffer format.
FB_BYTES_PER_PIXEL = {
    "rgb565": 2,
    "bgr565": 2,
    "rgb888": 3,
    "bgr888": 3,
    "rgba8888": 4,
    "bgra8888": 4,
}


def fb_bytes_per_pixel(fmt: str) -> int:
    try:
        return FB_BYTES_PER_PIXEL[fmt]
    except KeyError:
        raise ValueError(f"unsupported fb_format {fmt!r}; known: {sorted(FB_BYTES_PER_PIXEL)}")


# Fixed size of the pre-rendered "offline" notice box. Both the generator
# (make-offline) and the display compositor agree on this via these constants,
# so the raw .fb blob needs no sidecar dimensions.
OFFLINE_BOX_WIDTH = 420
OFFLINE_BOX_HEIGHT = 150
DEFAULT_OFFLINE_BOX_NAME = "offline_box.fb"
