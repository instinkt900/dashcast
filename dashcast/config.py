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
    timezone: str = ""

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir) / self.output_name

    @property
    def fb_path(self) -> Path:
        return Path(self.output_dir) / self.fb_name


@dataclass
class ServeConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class DisplayConfig:
    # Point this at the raw framebuffer blob (.fb), not the PNG — the Pi writes
    # these bytes straight to the framebuffer device.
    image_url: str = "http://192.0.2.20:8080/dashboard.fb"
    interval_seconds: float = 30.0
    fetch_timeout_seconds: float = 15.0
    framebuffer: str = "/dev/fb0"
    # Raw pixel format the framebuffer expects; must match [render].fb_format.
    fb_format: str = "rgb565"
    cache_path: str = "/var/lib/dashcast/dashboard.fb"
    # Show the "offline" overlay (dimmed last frame + notice box) after this many
    # seconds without a successful fetch. 0 disables (just keep last frame).
    offline_after_seconds: float = 60.0
    # Pre-rendered notice box (.fb) produced by `dashcast make-offline`.
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
