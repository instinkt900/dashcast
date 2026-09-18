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

import gzip
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


def _fetch(url: str, timeout: float, if_modified_since: str | None) -> tuple[bytes | None, str | None]:
    """GET the frame, conditionally. Returns (data, last_modified); data is None
    when the server answered 304, i.e. the frame we already have is still current.

    The conditional GET is what keeps this affordable. A full frame is 768 KB and
    we poll every few seconds, but the dashboard usually hasn't changed — an
    If-Modified-Since turns those polls into a bodyless 304 of a couple hundred
    bytes. Without it the Pi Zero W's single-antenna 2.4 GHz WiFi is pinned near
    saturation, transfers start stalling past the fetch timeout, and a run of
    those stalls looks like an outage to the offline-overlay logic.
    """
    headers = {"User-Agent": "dashcast-display"}
    if if_modified_since:
        headers["If-Modified-Since"] = if_modified_since
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted LAN URL)
            if resp.status != 200:
                raise urllib.error.HTTPError(url, resp.status, "unexpected status", resp.headers, None)
            return resp.read(), resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        # urllib treats any non-2xx as an error, including the 304 we asked for.
        if exc.code == 304:
            return None, if_modified_since
        raise


def _atomic_write(dst: Path, data: bytes) -> None:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def _unpack(blob: bytes) -> bytes:
    """Return raw framebuffer bytes, inflating first if the blob is gzip'd.

    Sniffing the two-byte gzip magic rather than reading a config flag means
    `image_url` can point at either `dashboard.fb` or `dashboard.fb.gz` and this
    just works — so the two ends can be upgraded in either order, with no flag
    day and nothing to keep in sync. Framebuffer.write still length-checks the
    result, which doubles as an integrity check on the archive.
    """
    if blob[:2] == b"\x1f\x8b":
        return gzip.decompress(blob)
    return blob


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
    # The cache holds whatever came off the wire, so it may be gzip'd.
    if cache.is_file():
        try:
            last_good = _unpack(cache.read_bytes())
            fb.write(last_good)
            shown_hash = hashlib.sha256(last_good).hexdigest()
            log.info("painted cached frame (%d bytes)", len(last_good))
        except Exception as exc:
            log.warning("could not paint cached frame: %s", exc)

    interval = cfg.display.interval_seconds
    grace = cfg.display.offline_after_seconds
    min_failures = cfg.display.offline_min_failures
    refresh = cfg.display.full_refresh_seconds
    last_modified: str | None = None  # validator for the frame we're showing
    failures = 0  # consecutive failures to *reach* the server
    last_full = 0.0  # monotonic time of the last unconditional fetch
    try:
        while _running:
            start = time.monotonic()

            # Normally revalidate what we already have. Periodically ask
            # unconditionally so a diverged cached frame can't stick forever
            # (the server holds mtime steady while the dashboard is unchanged,
            # so 304s can otherwise continue indefinitely).
            validator = last_modified
            if refresh > 0 and (start - last_full) >= refresh:
                validator = None

            # --- network phase: could we reach the server at all? ---
            try:
                data, lm = _fetch(cfg.display.image_url, cfg.display.fetch_timeout_seconds, validator)
            except Exception as exc:
                failures += 1
                stale = time.monotonic() - last_success
                # Require BOTH a long dry spell and several consecutive failures.
                # One slow transfer that trips the fetch timeout is not an outage,
                # and treating it as one is what used to flash the offline notice
                # over a perfectly healthy dashboard.
                if (
                    offline_box is not None
                    and not offline_shown
                    and grace > 0
                    and stale >= grace
                    and failures >= min_failures
                ):
                    base = last_good if last_good is not None else bytes(expected)
                    try:
                        fb.write(_render_offline_frame(base, offline_box, cfg, bpp))
                        offline_shown = True
                        log.warning(
                            "server unreachable for %.0fs (%d consecutive failures) — showing offline overlay",
                            stale,
                            failures,
                        )
                    except Exception as werr:
                        log.error("failed to draw offline overlay: %s", werr)
                else:
                    log.warning(
                        "fetch failed (%d in a row, %.0fs stale; keeping current frame): %s",
                        failures,
                        stale,
                        exc,
                    )
            else:
                # A 200 *or* a 304 both prove the server is up — that, and only
                # that, is what the offline overlay is about.
                failures = 0
                last_success = time.monotonic()
                if validator is None:
                    last_full = last_success
                if lm:
                    last_modified = lm

                # --- paint phase: kept separate so a framebuffer fault is
                # reported as a display problem, not mistaken for an outage. ---
                try:
                    if data is None:
                        # Nothing new. Only touch the panel if it's currently
                        # showing the offline overlay rather than a live frame.
                        if offline_shown and last_good is not None:
                            fb.write(last_good)
                            offline_shown = False
                            log.info("server reachable again; restored last live frame")
                        else:
                            log.debug("not modified; keeping current frame")
                    else:
                        frame = _unpack(data)  # data may be the gzip'd blob
                        last_good = frame  # newest frame we hold, painted or not
                        new_hash = hashlib.sha256(frame).hexdigest()
                        # Force a write when recovering from offline, even if the
                        # frame is byte-identical to what was up before.
                        if new_hash != shown_hash or offline_shown:
                            fb.write(frame)
                            # Cache what came off the wire, not the inflated frame:
                            # ~25 KB per update instead of 768 KB, which matters for
                            # SD card wear on a Pi that has been running for months.
                            _atomic_write(cache, data)
                            if offline_shown:
                                log.info("server recovered; resumed live frames")
                            else:
                                log.info(
                                    "updated display (%d B on the wire -> %d B frame)",
                                    len(data),
                                    len(frame),
                                )
                            shown_hash = new_hash
                            offline_shown = False
                        else:
                            log.debug("unchanged; keeping current frame")
                except Exception as exc:
                    log.error("could not write frame to %s: %s", cfg.display.framebuffer, exc)
                    # Drop the validator so the next poll refetches in full rather
                    # than getting a 304 and leaving the panel permanently behind.
                    last_modified = None

            if once:
                break
            elapsed = time.monotonic() - start
            time.sleep(max(0.0, interval - elapsed))
    finally:
        fb.close()
    return 0
