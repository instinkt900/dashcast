"""Display side (runs on the Pi Zero W).

Deliberately tiny: stdlib only. Fetch the raw framebuffer blob over HTTP and
write it straight to /dev/fb0. No browser, no X, no image library, no child
process — just bytes onto the framebuffer. Frame swaps are a single seek+write,
so there's no flicker, and the last good frame stays on screen if the network
or server drops.

If the render server stays unreachable past a grace period, we dim the last
frame and composite a pre-rendered "offline" notice box over it, so a frozen
dashboard can't be mistaken for a live one.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path

from dashcast.config import Config, load_config
from dashcast.constants import (
    OFFLINE_BOX_HEIGHT,
    OFFLINE_BOX_WIDTH,
    fb_bytes_per_pixel,
)

log = logging.getLogger("dashcast.display")

_running = True


def _install_signal_handlers() -> None:
    def _stop(signum, _frame):
        global _running
        log.info("received signal %s; shutting down", signum)
        _running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def _fetch(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dashcast-display"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted LAN URL)
        if resp.status != 200:
            raise urllib.error.HTTPError(url, resp.status, "unexpected status", resp.headers, None)
        return resp.read()


def _atomic_write(dst: Path, data: bytes) -> None:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def _dim_rgb565(data: bytes) -> bytes:
    """Halve the brightness of an RGB565 buffer with the classic shift+mask
    trick: (px >> 1) & 0x7BEF clears the bits that bleed across channels.
    Assumes a little-endian framebuffer (true for armv6 Raspberry Pi)."""
    px = array("H")
    px.frombytes(data)
    for i in range(len(px)):
        px[i] = (px[i] >> 1) & 0x7BEF
    return px.tobytes()


def _blit_center(frame: bytearray, box: bytes, bw: int, bh: int, fw: int, fh: int, bpp: int) -> None:
    """Copy the box's rows into the centre of the frame buffer, in place."""
    cx = (fw - bw) // 2
    cy = (fh - bh) // 2
    row = bw * bpp
    for r in range(bh):
        dst = ((cy + r) * fw + cx) * bpp
        frame[dst : dst + row] = box[r * row : (r + 1) * row]


class Framebuffer:
    """Writes full frames to the framebuffer device. Reopens on error."""

    def __init__(self, device: str, expected_bytes: int):
        self.device = device
        self.expected_bytes = expected_bytes
        self._fh = None

    def _open(self):
        if self._fh is None:
            self._fh = open(self.device, "r+b", buffering=0)
        return self._fh

    def write(self, data: bytes) -> None:
        if len(data) != self.expected_bytes:
            raise ValueError(
                f"frame is {len(data)} bytes, expected {self.expected_bytes} "
                f"for this panel/format — is [render].fb_format in sync?"
            )
        try:
            fh = self._open()
            fh.seek(0)
            fh.write(data)
        except OSError:
            self.close()  # force reopen next time
            raise

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


def _load_offline_box(cfg: Config, bpp: int) -> bytes | None:
    path = cfg.display.offline_box_path
    if not path or cfg.display.offline_after_seconds <= 0:
        return None
    p = Path(path)
    if not p.is_file():
        log.warning("offline box %s not found; will just keep last frame when server is down", p)
        return None
    box = p.read_bytes()
    want = OFFLINE_BOX_WIDTH * OFFLINE_BOX_HEIGHT * bpp
    if len(box) != want:
        log.warning("offline box %s is %d bytes, expected %d — ignoring", p, len(box), want)
        return None
    return box


def _render_offline_frame(base: bytes, box: bytes | None, cfg: Config, bpp: int) -> bytes:
    """Dim the base frame and composite the notice box in the centre."""
    fw, fh = cfg.render.width, cfg.render.height
    if cfg.display.fb_format == "rgb565":
        dimmed = _dim_rgb565(base)
    else:  # dimming trick is rgb565-specific; fall back to the frame as-is
        dimmed = base
    frame = bytearray(dimmed)
    if box is not None:
        _blit_center(frame, box, OFFLINE_BOX_WIDTH, OFFLINE_BOX_HEIGHT, fw, fh, bpp)
    return bytes(frame)


def run_display(config_path: str, once: bool = False) -> int:
    cfg = load_config(config_path)
    _install_signal_handlers()

    bpp = fb_bytes_per_pixel(cfg.display.fb_format)
    expected = cfg.render.width * cfg.render.height * bpp
    cache = Path(cfg.display.cache_path)
    cache.parent.mkdir(parents=True, exist_ok=True)

    fb = Framebuffer(cfg.display.framebuffer, expected)
    offline_box = _load_offline_box(cfg, bpp)
    shown_hash: str | None = None
    last_good: bytes | None = None
    last_success = time.monotonic()  # grace period counts from startup
    offline_shown = False

    # Paint a cached frame immediately so boot doesn't leave console text on screen.
    if cache.is_file():
        try:
            last_good = cache.read_bytes()
            fb.write(last_good)
            shown_hash = hashlib.sha256(last_good).hexdigest()
            log.info("painted cached frame (%d bytes)", len(last_good))
        except Exception as exc:
            log.warning("could not paint cached frame: %s", exc)

    interval = cfg.display.interval_seconds
    grace = cfg.display.offline_after_seconds
    try:
        while _running:
            start = time.monotonic()
            try:
                data = _fetch(cfg.display.image_url, cfg.display.fetch_timeout_seconds)
                new_hash = hashlib.sha256(data).hexdigest()
                # Force a write when recovering from offline, even if the frame
                # is byte-identical to what was up before the outage.
                if new_hash != shown_hash or offline_shown:
                    fb.write(data)
                    _atomic_write(cache, data)
                    if offline_shown:
                        log.info("server recovered; resumed live frames")
                    else:
                        log.info("updated display (%d bytes)", len(data))
                    shown_hash = new_hash
                    offline_shown = False
                else:
                    log.debug("unchanged; keeping current frame")
                last_good = data
                last_success = time.monotonic()
            except Exception as exc:
                stale = time.monotonic() - last_success
                if offline_box is not None and not offline_shown and grace > 0 and stale >= grace:
                    base = last_good if last_good is not None else bytes(expected)
                    try:
                        fb.write(_render_offline_frame(base, offline_box, cfg, bpp))
                        offline_shown = True
                        log.warning("server unreachable for %.0fs — showing offline overlay", stale)
                    except Exception as werr:
                        log.error("failed to draw offline overlay: %s", werr)
                else:
                    log.error("fetch/display failed (keeping current frame): %s", exc)

            if once:
                break
            elapsed = time.monotonic() - start
            time.sleep(max(0.0, interval - elapsed))
    finally:
        fb.close()
    return 0
