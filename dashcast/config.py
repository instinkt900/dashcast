"""Configuration loading, shared by both ends.

One TOML file describes the whole system; each end reads the section it needs.
The HA long-lived token is never stored in the TOML — it comes from the
DASHCAST_HA_TOKEN env var or a token_file, so the config can live in git safely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # py3.11+ (the Pi has 3.11, the Docker image has 3.12)
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - fallback for py<3.11
    import tomli as tomllib  # type: ignore

from dashcast.constants import (
    DEFAULT_FB_NAME,
    DEFAULT_HEIGHT,
    DEFAULT_IMAGE_NAME,
    DEFAULT_WIDTH,
)


class ConfigError(RuntimeError):
    pass


@dataclass
class HomeAssistantConfig:
    url: str
    dashboard_path: str = "/lovelace/0"
    token: str = ""  # injected from env/file, not from TOML

    @property
    def dashboard_url(self) -> str:
        return self.url.rstrip("/") + "/" + self.dashboard_path.lstrip("/")


@dataclass
class RenderConfig:
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    device_scale_factor: float = 1.0
    interval_seconds: float = 30.0
    wait_until: str = "networkidle"  # playwright load state
    wait_after_load_ms: int = 4000
    nav_timeout_ms: int = 60000
    # Screenshot-only model: navigate once, then just capture each interval and
    # let HA's websocket keep the DOM live. Do a full reload this often (seconds)
    # as a safety net against drift/leaks. 0 disables periodic reloads.
    reload_interval_seconds: float = 300.0
    output_dir: str = "/data"
    output_name: str = DEFAULT_IMAGE_NAME
    # Raw framebuffer output for the direct-to-/dev/fb0 display path.
    # Set fb_format = "" to disable and serve only PNG (e.g. for an feh setup).
    fb_format: str = "rgb565"
    fb_name: str = DEFAULT_FB_NAME
    # Extra CSS injected before each screenshot — handy for hiding scrollbars,
    # HA header, etc. on a kiosk view.
    extra_css: str = ""
    # Scheduled "dimmed hours": during the window [dim_start, dim_end) the served
    # image is dimmed to `dim_brightness` (fraction of full, 1.0 = off) to cut
    # night-time glare and panel wear. Times are "HH:MM" (24h) in `timezone`
    # (empty = the render host's local time). Empty start/end disables dimming.
    dim_start: str = ""
    dim_end: str = ""
    dim_brightness: float = 0.4
    # IANA zone for the dim schedule AND for the capture browser's clock. Without
    # it headless Chromium runs in the container's zone (UTC), and any dashboard
    # card that formats a date with plain JS shows the wrong day near midnight.
    timezone: str = ""
    # Colour correction for the panel, applied to every frame before it is
    # published (so the PNG shows what the panel is sent). Small TFT panels
    # tend to show light UI colours washed out next to a desktop monitor.
    # 1.0 everywhere = off.
    #   color_gamma: out = in ** gamma per channel. > 1 darkens mid-tones and
    #     pulls pale colours away from white; a 3-item list [r, g, b] also
    #     corrects a colour cast.
    #   color_gain: out = in * gain per channel (white balance). Scales dark and
    #     light colours alike, so unlike gamma it does not crush a dark theme.
    #     A 3-item list [r, g, b] corrects a colour cast: a blue-shifted panel
    #     wants the lowest blue gain.
    #   color_contrast / color_saturation: PIL ImageEnhance factors.
    color_gamma: float | list = 1.0
    color_gain: float | list = 1.0
    color_contrast: float = 1.0
    color_saturation: float = 1.0

    @property
    def gamma_rgb(self) -> tuple[float, float, float]:
        g = self.color_gamma
        return tuple(float(x) for x in g) if isinstance(g, list) else (float(g),) * 3  # type: ignore[return-value]

    @property
    def gain_rgb(self) -> tuple[float, float, float]:
        g = self.color_gain
        return tuple(float(x) for x in g) if isinstance(g, list) else (float(g),) * 3  # type: ignore[return-value]

    @property
    def color_correction(self) -> bool:
        return (self.gamma_rgb != (1.0, 1.0, 1.0) or self.gain_rgb != (1.0, 1.0, 1.0)
                or self.color_contrast != 1.0 or self.color_saturation != 1.0)

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir) / self.output_name

    @property
    def fb_path(self) -> Path:
        return Path(self.output_dir) / self.fb_name

    @property
    def fb_gz_path(self) -> Path:
        """gzip'd twin of fb_path, published alongside it. rgb565 dashboard
        pixels are mostly long flat runs, so this lands around 30x smaller than
        the raw blob — which is what the Pi actually fetches."""
        return self.fb_path.with_suffix(self.fb_path.suffix + ".gz")


@dataclass
class ServeConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    # /healthz answers 503 once this long passes without a successful capture.
    # Without it /healthz only proved the HTTP thread was alive, and a wedged
    # Chromium would keep reporting healthy while the Pi showed an old dashboard.
    # Must comfortably exceed a full reload (nav_timeout_ms + wait_after_load_ms)
    # so a slow periodic reload isn't reported as an outage. 0 disables the check.
    stale_after_seconds: float = 120.0
    # The process exits (status 1) once this long passes without a successful
    # capture, so the container's restart policy starts it fresh. Docker only
    # reports an unhealthy container; it never restarts one. Keep it above
    # stale_after_seconds so the in-process recovery (new page, relaunched
    # Chromium) gets its chance first. 0 disables the exit.
    exit_after_seconds: float = 300.0


@dataclass
class DisplayConfig:
    # Point this at the gzip'd framebuffer blob (.fb.gz), not the PNG — the Pi
    # inflates it and writes the bytes straight to the framebuffer device. Plain
    # .fb also works; the display sniffs the gzip magic either way.
    image_url: str = "http://192.0.2.20:8080/dashboard.fb.gz"
    interval_seconds: float = 30.0
    # Kept deliberately short. The payload is ~25 KB, so a healthy fetch is well
    # under a second and a long timeout buys nothing — it just burns the
    # offline_after_seconds budget on one dead attempt instead of retrying. At 8s
    # a congested minute still gets ~7 attempts rather than ~4, so there are more
    # chances to catch a working moment.
    fetch_timeout_seconds: float = 8.0
    framebuffer: str = "/dev/fb0"
    # Raw pixel format the framebuffer expects; must match [render].fb_format.
    fb_format: str = "rgb565"
    cache_path: str = "/var/lib/dashcast/dashboard.fb"
    # Stamp the "outdated" badge into the frame's top-right corner after this many
    # seconds without reaching the server. 0 disables (just keep the last frame,
    # unannotated).
    #
    # 60s is deliberately eager. The threshold used to be 180s because the old
    # indicator was a centred card over a dimmed frame — obnoxious enough that
    # firing it on the Pi's routine 40–100s WiFi stalls was worse than saying
    # nothing. Now that it's a small corner badge that obscures nothing, showing
    # it early is just honest: the frame really is a minute stale, and you can
    # still read every number on it. Raise this if the badge feels twitchy.
    offline_after_seconds: float = 60.0
    # ...and only once this many fetches have failed back to back. Note this is
    # the weaker of the two guards: a dropped-link error (EHOSTUNREACH) fails
    # instantly rather than timing out, so the counter races up in seconds and
    # raising it alone barely helps. offline_after_seconds is the real lever.
    offline_min_failures: int = 3
    # Occasionally re-fetch unconditionally instead of sending an
    # If-Modified-Since, so a cached frame that has diverged from the server's
    # can't persist. The server holds mtime steady while the dashboard is
    # unchanged, so 304s would otherwise continue indefinitely. 0 disables.
    full_refresh_seconds: float = 300.0
    # Uptime Kuma "Push" monitor URL (or anything that accepts a GET heartbeat).
    # Called after a frame is confirmed current — fetched (200) or revalidated
    # (304) AND written to the panel — so one heartbeat covers the whole chain:
    # Pi up, WiFi up, dashcast running, server reachable. Empty disables.
    # The URL carries Kuma's push token, so set it only in the untracked
    # /etc/dashcast/config.toml, never in config.example.toml.
    push_url: str = ""
    # Minimum gap between heartbeats. Set the Kuma monitor's heartbeat interval to
    # about twice this, with a couple of retries, so the Pi's routine 40–100s WiFi
    # stalls don't page you.
    push_interval_seconds: float = 60.0
    # Pre-rendered "OUTDATED" corner badge (.fb) produced by `dashcast make-offline`.
    # Must match OFFLINE_BOX_WIDTH/HEIGHT or the display ignores it (and draws
    # nothing) rather than scribbling the wrong number of bytes into the panel.
    offline_box_path: str = "/etc/dashcast/offline_box.fb"


@dataclass
class Config:
    home_assistant: HomeAssistantConfig
    render: RenderConfig
    serve: ServeConfig
    display: DisplayConfig


def _load_token(ha_section: dict) -> str:
    # Priority: explicit env var > token_file referenced in config > empty.
    env_token = os.environ.get("DASHCAST_HA_TOKEN", "").strip()
    if env_token:
        return env_token
    token_file = ha_section.get("token_file")
    if token_file:
        p = Path(os.path.expanduser(token_file))
        if not p.is_file():
            raise ConfigError(f"token_file does not exist: {p}")
        return p.read_text(encoding="utf-8").strip()
    return ""


def load_config(path: str | os.PathLike) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    with p.open("rb") as fh:
        raw = tomllib.load(fh)

    ha_raw = raw.get("home_assistant", {})
    if "url" not in ha_raw:
        raise ConfigError("[home_assistant].url is required")
    ha = HomeAssistantConfig(
        url=ha_raw["url"],
        dashboard_path=ha_raw.get("dashboard_path", "/lovelace/0"),
        token=_load_token(ha_raw),
    )

    render = RenderConfig(**_known(raw.get("render", {}), RenderConfig))
    render.dim_brightness = min(1.0, max(0.0, render.dim_brightness))
    for key in ("color_gamma", "color_gain"):
        g = getattr(render, key)
        if not (isinstance(g, (int, float)) or (isinstance(g, list) and len(g) == 3
                                                 and all(isinstance(x, (int, float)) for x in g))):
            raise ConfigError(f"[render].{key} must be a number or a list of 3 numbers [r, g, b]")
    if min(render.gamma_rgb) <= 0:
        raise ConfigError("[render].color_gamma values must be > 0")
    if min(render.gain_rgb) < 0:
        raise ConfigError("[render].color_gain values must be >= 0")
    if render.color_contrast < 0 or render.color_saturation < 0:
        raise ConfigError("[render].color_contrast and color_saturation must be >= 0")
    serve = ServeConfig(**_known(raw.get("serve", {}), ServeConfig))
    display = DisplayConfig(**_known(raw.get("display", {}), DisplayConfig))

    return Config(home_assistant=ha, render=render, serve=serve, display=display)


def _known(section: dict, cls) -> dict:
    """Keep only keys the dataclass declares, so unknown TOML keys warn loudly
    instead of silently exploding the constructor."""
    allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in [{cls.__name__}]: {sorted(unknown)}")
    return {k: v for k, v in section.items() if k in allowed}
