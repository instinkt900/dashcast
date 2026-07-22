"""Render side (runs on the serve host, inside the Playwright Docker image).

Keeps ONE Chromium context alive, injects the HA long-lived token so the
dashboard renders authenticated, and screenshots at the panel's native
resolution on an interval. The latest PNG is written atomically and served
over HTTP with `Cache-Control: no-store` so the display always gets fresh bytes.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dashcast.config import Config, load_config

log = logging.getLogger("dashcast.serve")

# Injected before any page script runs. HA's frontend reads `hassTokens` from
# localStorage on load; we prime it with the long-lived token and a far-future
# expiry so it never tries to refresh. Tuned during first-light testing.
_INIT_SCRIPT = """
(() => {
  const token = %s;
  const hassUrl = %s;
  const tenYears = 10 * 365 * 24 * 60 * 60 * 1000;
  window.localStorage.setItem('hassTokens', JSON.stringify({
    access_token: token,
    token_type: 'Bearer',
    expires_in: Math.floor(tenYears / 1000),
    hassUrl: hassUrl,
    clientId: hassUrl + '/',
    expires: Date.now() + tenYears,
    refresh_token: '',
  }));
  // Skip the "remember this device" onboarding nudge if present.
  window.localStorage.setItem('onboardingDone', 'true');
})();
"""


class _Handler(SimpleHTTPRequestHandler):
    """Serves the output directory, always fresh, with a /healthz endpoint."""

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/healthz", "/health"):
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *a):  # quieter access log
        log.debug("http %s", fmt % a)


def _start_http_server(cfg: Config) -> ThreadingHTTPServer:
    directory = str(cfg.render.output_path.parent)
    handler = partial(_Handler, directory=directory)
    httpd = ThreadingHTTPServer((cfg.serve.host, cfg.serve.port), handler)
    t = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    t.start()
    log.info(
        "serving %s on http://%s:%d/%s",
        directory,
        cfg.serve.host,
        cfg.serve.port,
        cfg.render.output_name,
    )
    return httpd


def _atomic_write(dst: Path, data: bytes) -> None:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)  # atomic within the same filesystem


def png_to_framebuffer(png: bytes, width: int, height: int, fmt: str) -> bytes:
    """Convert a PNG to raw framebuffer bytes the Pi can dump straight to
    /dev/fb0. Heavy lifting stays here on the powerful box so the display stays
    dependency-free. Little-endian, matching the Pi's native fb byte order."""
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.size != (width, height):
        img = img.resize((width, height))
    arr = np.asarray(img, dtype=np.uint16)  # HxWx3
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    if fmt in ("rgb565", "bgr565"):
        hi, lo = (r, b) if fmt == "rgb565" else (b, r)
        packed = ((hi >> 3) << 11) | ((g >> 2) << 5) | (lo >> 3)
        return packed.astype("<u2").tobytes()
    if fmt in ("rgb888", "bgr888"):
        order = (0, 1, 2) if fmt == "rgb888" else (2, 1, 0)
        return arr[:, :, order].astype(np.uint8).tobytes()
    if fmt in ("rgba8888", "bgra8888"):
        order = (0, 1, 2) if fmt == "rgba8888" else (2, 1, 0)
        rgb = arr[:, :, order].astype(np.uint8)
        alpha = np.full((height, width, 1), 255, dtype=np.uint8)
        return np.concatenate([rgb, alpha], axis=2).tobytes()
    raise ValueError(f"unsupported fb_format {fmt!r}")


class Renderer:
    """Owns the persistent browser; recreates the page if it dies."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._needs_nav = True  # navigate on first capture / after a page recreate
        self._last_nav = 0.0  # monotonic time of last full load

    def __enter__(self) -> "Renderer":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",  # required inside the unprivileged LXC
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--hide-scrollbars",
            ],
        )
        self._new_page()
        return self

    def __exit__(self, *exc):
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # pragma: no cover
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass

    def _new_page(self) -> None:
        r = self.cfg.render
        self._context = self._browser.new_context(
            viewport={"width": r.width, "height": r.height},
            device_scale_factor=r.device_scale_factor,
        )
        token_js = json.dumps(self.cfg.home_assistant.token)
        url_js = json.dumps(self.cfg.home_assistant.url.rstrip("/"))
        self._context.add_init_script(_INIT_SCRIPT % (token_js, url_js))
        self._page = self._context.new_page()
        self._page.set_default_navigation_timeout(r.nav_timeout_ms)
        self._needs_nav = True

    def _ensure_page(self) -> None:
        if self._page is None or self._page.is_closed():
            log.warning("page gone; recreating context")
            try:
                if self._context:
                    self._context.close()
            except Exception:
                pass
            self._new_page()

    def _settle(self) -> None:
        r = self.cfg.render
        if r.extra_css:
            self._page.add_style_tag(content=r.extra_css)
        if r.wait_after_load_ms:
            self._page.wait_for_timeout(r.wait_after_load_ms)
        self._last_nav = time.monotonic()

    def capture(self) -> bytes:
        r = self.cfg.render
        self._ensure_page()
        page = self._page

        # Screenshot-only model: load once, then just capture. HA's websocket
        # keeps the DOM live between shots; a periodic reload guards against drift.
        if self._needs_nav:
            page.goto(self.cfg.home_assistant.dashboard_url, wait_until=r.wait_until)
            self._needs_nav = False
            self._settle()
        elif r.reload_interval_seconds and (time.monotonic() - self._last_nav) >= r.reload_interval_seconds:
            log.info("periodic reload (safety net)")
            page.reload(wait_until=r.wait_until)
            self._settle()

        # Exact-viewport shot (not full_page) — we want the panel, not the doc.
        return page.screenshot(type="png")


def run_serve(config_path: str, once: bool = False) -> int:
    cfg = load_config(config_path)
    if not cfg.home_assistant.token:
        log.warning("no HA token set (DASHCAST_HA_TOKEN / token_file) — dashboard will likely show a login screen")

    r = cfg.render
    out = r.output_path
    out.parent.mkdir(parents=True, exist_ok=True)

    def _publish(png: bytes) -> str:
        _atomic_write(out, png)
        msg = f"{out.name} ({len(png)} B)"
        if r.fb_format:
            fb = png_to_framebuffer(png, r.width, r.height, r.fb_format)
            _atomic_write(r.fb_path, fb)
            msg += f" + {r.fb_path.name} ({len(fb)} B, {r.fb_format})"
        return msg

    with Renderer(cfg) as renderer:
        if once:
            log.info("wrote %s", _publish(renderer.capture()))
            return 0

        _start_http_server(cfg)
        interval = r.interval_seconds
        while True:
            start = time.monotonic()
            try:
                log.info("captured %s", _publish(renderer.capture()))
            except Exception as exc:  # keep the loop alive; last good image stays served
                log.error("capture failed: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            elapsed = time.monotonic() - start
            time.sleep(max(0.0, interval - elapsed))
