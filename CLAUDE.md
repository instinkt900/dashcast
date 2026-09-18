# CLAUDE.md — dashcast

Context for working on this repo.

## Purpose

**Split-brain Home Assistant dashboard display.** Render a Lovelace dashboard on
a powerful box, show it as a flat image on a weak one. The target display is a
**Raspberry Pi Zero W** driving an 800×480 @ 16bpp panel — too underpowered to run
a Chromium kiosk smoothly. So:

- the **serve side** (a capable box, in Docker) runs headless Chromium, screenshots
  the authenticated dashboard on an interval, and serves the image over HTTP;
- the **display side** (the Pi) fetches a **raw framebuffer blob** and writes the
  bytes straight to `/dev/fb0` — no browser, no X, no image library, stdlib only.

One codebase, one config file, two roles.

## The two-ends contract (read this first)

- The serve side writes three files atomically to its data dir: `dashboard.png`
  (for humans/debugging), `dashboard.fb` (**raw framebuffer bytes**) and
  `dashboard.fb.gz` (gzip'd twin of the `.fb`). The Pi fetches the **`.fb.gz`**,
  never the PNG. Measured on a real frame: 768,000 B raw → **25,022 B gzip'd
  (~30x)**, which also beats the PNG (54,090 B) while needing only stdlib `zlib`
  to unpack instead of an image decoder the Pi hasn't got.
- The display **sniffs the gzip magic bytes** rather than reading a config flag,
  so `image_url` may point at either file and the two ends can be upgraded in
  either order. Keep it that way — it means no flag day.
- **The serve side only rewrites a file when its bytes actually change, and the Pi
  fetches with `If-Modified-Since`.** These two halves are load-bearing together:
  file **mtime is the cache validator**, so republishing an identical frame would
  bump mtime and force the Pi to re-pull the whole 768 KB blob every poll. A
  Pi Zero W's WiFi can't sustain that — it stalls, fetches trip
  `fetch_timeout_seconds`, and the display used to mistake that for an outage and
  flash the offline notice while nothing was down. Don't "simplify" either half
  back to an unconditional write/fetch. Unchanged frames cost a bodyless 304.
- **`fb_format` must match on both ends** (default `rgb565`; see
  `constants.FB_BYTES_PER_PIXEL` for supported formats). Panel native size is
  800×480 (`constants.DEFAULT_WIDTH/HEIGHT`).
- Frame swap on the Pi is a single seek+write to the framebuffer → no flicker, and
  the **last good frame stays on screen** if the network/server drops. After a
  grace period unreachable, the Pi dims the last frame and composites a
  pre-rendered **offline notice box** over it, so a frozen dashboard can't be
  mistaken for a live one.

## Stack

- **Serve side:** Python 3.12 in Docker. **Playwright (Chromium only)** for
  rendering, **Pillow + numpy** to pack screenshots into raw framebuffer bytes,
  **tzdata** (so `zoneinfo` resolves IANA names inside the slim image). HTTP is
  stdlib **`http.server.ThreadingHTTPServer`** — no FastAPI/uvicorn. Chromium runs
  `--no-sandbox` (containerised/LXC).
- **Display side (Pi):** Python **3.11 stdlib only** — no third-party imports on
  the `display` path. `fbi` is present but the service writes the framebuffer
  directly. Runs under a systemd unit that owns tty1.
- **Config:** one TOML, dataclasses, `_known()` rejects unknown keys. The HA
  long-lived token comes from `DASHCAST_HA_TOKEN` (env) or a `token_file` — **never
  the TOML**, so config can live in git.
- No test framework in the repo; verification is manual (`--once`, `/healthz`,
  eyeball the Pi).

## Layout

```
dashcast/            Python package
  cli.py             entrypoint: serve | display | make-offline
  __main__.py        `python -m dashcast`
  serve.py           RENDER side: persistent Chromium context, capture loop,
                     atomic PNG+.fb write, stdlib HTTP server + /healthz, dimmed-hours
  display.py         PI side: fetch .fb over HTTP → /dev/fb0, last-frame hold,
                     offline overlay. STDLIB ONLY — keep it that way.
  offline.py         render the offline notice box (Pillow) → raw .fb blob
  config.py          TOML loader; dataclasses; _known(); token from env/file
  constants.py       panel res (800x480), shared filenames, fb bytes-per-pixel
docker/              Dockerfile (slim, chromium-only), docker-compose.yml
deploy/              install-server.sh, install-pi.sh, dashcast-display.service, assets/
config.example.toml  the only tracked config; real config.toml is gitignored
```

## Commands

```
dashcast serve       -c config.toml [--once]   # render + host image (serve side, Docker)
dashcast display     -c config.toml [--once]   # fetch .fb, write framebuffer (Pi)
dashcast make-offline -c config.toml -o out.fb [--text ..] [--subtext ..]
```

- `serve` keeps ONE Chromium context alive, navigates once, then screenshots each
  interval (HA's websocket keeps the DOM live); it does a full reload every
  `reload_interval_seconds` (default 300) as a drift/leak safety net. Identical
  frames are **not** republished (see the contract above), and images are served
  with `Cache-Control: no-cache, must-revalidate` — revalidate before reuse, *not*
  `no-store`, which would forbid the Pi from holding the frame it revalidates.
- `serve --once` captures a single screenshot and exits (no HTTP) — handy for dev.
- `make-offline` needs Pillow, so run it on the server/dev box, **not** the Pi;
  the resulting `.fb` is shipped to the Pi.

## Configuration

One TOML with four sections (template: `config.example.toml`):

- `[home_assistant]` — `url`, `dashboard_path` (default `/lovelace/0`); token via
  env/file only.
- `[render]` — size, `device_scale_factor`, `interval_seconds`, `wait_until`,
  `wait_after_load_ms`, `reload_interval_seconds`, `output_dir`/`output_name`,
  `fb_format`/`fb_name`, `extra_css` (kiosk CSS injected before capture), plus the
  **dimmed-hours** keys below.
- `[serve]` — `host`/`port` (8080).
- `[display]` — `image_url` (point at the **`.fb.gz`**), `framebuffer` (`/dev/fb0`),
  `fb_format` (must match `[render]`), `cache_path`, `offline_box_path`, plus the
  offline-trigger keys: `offline_after_seconds` **and** `offline_min_failures`
  (both must be satisfied — elapsed time alone gave false offline notices), and
  `full_refresh_seconds` (how often to skip the `If-Modified-Since` so a diverged
  cached frame can't stick).

**What counts as "offline":** only failing to *reach* the server. A 304 counts as
reached, and a framebuffer write error is logged as a display fault rather than an
outage — otherwise a bad `/dev/fb0` write masquerades as a server outage.

**Dimmed hours** (render-side): during `[dim_start, dim_end)` the served image is
dimmed to `dim_brightness` (fraction of full; `1.0` = off) via
`PIL.ImageEnhance.Brightness`, applied in `_publish` **before** the atomic write so
both the PNG and the `.fb` come out dimmed. Times are `"HH:MM"` (24h) in
`[render].timezone` (empty = render host local time); `start > end` wraps midnight;
`start == end` disables. `dim_brightness` is clamped to `[0,1]` in `load_config`.

### Two `config.toml` files (known gotcha)
The repo-root `config.toml` is the dev/working copy. `docker/config.toml` is what
the container bind-mounts read-only (compose resolves `./config.toml` relative to
the `docker/` dir). Both are gitignored; only `config.example.toml` is tracked.

## Deployment

**Do not put the serve host / Pi hostnames or IPs in the repo** — the display
`image_url` example uses an RFC 5737 placeholder (`192.0.2.20`); keep it that way.

**Serve side (Docker):** image **bakes code in via `COPY`**, so code changes need
an **image rebuild** — a restart won't pick them up. Container listens on 8080
(compose maps `8080:8080`). HA token from `docker/.env` (`DASHCAST_HA_TOKEN`);
`config.toml` bind-mounted read-only; data in the named volume `dashcast-data` →
`/data`; healthcheck hits `/healthz`. Deploy on the host from the repo clone:
```
git pull --ff-only && cd docker && docker compose up -d --build
```
`deploy/install-server.sh` wraps this. **The serve host has a tight disk** — if a
rebuild fails on space, `docker image prune` / stop + `docker rmi dashcast:latest`
before rebuilding. Verify with `curl :8080/healthz`.

**Display side (Pi):** not Docker — stdlib Python + systemd. Run
`sudo bash deploy/install-pi.sh` on the Pi (after rsyncing the repo there). It
installs the package to `/opt/dashcast`, config to `/etc/dashcast/config.toml`
(won't overwrite an existing one), ensures `fbi`, disables the FullPageOS
lightdm/Chromium kiosk (boots to console), and installs + enables the
`dashcast-display` systemd service. The service owns tty1, blanks the cursor/console
so nothing draws over frames, and runs `python3 -m dashcast display`.

## Conventions & guardrails

- **`display.py` (and anything it imports on the Pi path) must stay stdlib-only.**
  Pillow/numpy/Playwright are serve-side only. The Pi has no third-party deps.
- **No internal network details in the repo.** Use RFC 5737 placeholders
  (`192.0.2.x`) in examples/config. **Leak-scan every commit** for internal
  hostnames/IPs before pushing.
- **Secrets are gitignored** (`docker/.env`, `docker/config.toml`, `config.toml`,
  `*.token`); only `*.example` is tracked. The HA token never goes in the TOML or
  shell history.
- **README is intentionally scope-limited** — it does *not* document deployment
  (that's this file's job). Don't add deploy how-tos to the README.
- Match the surrounding code style and the "explain the *why*" docstring tone.
  Commit to `master` (the serve host pulls it); keep commits scoped and
  leak-scanned. Personal project — deploy when the user asks.
