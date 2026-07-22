# dashcast

Render a Home Assistant dashboard on a powerful machine and display it as a flat
image on a weak one.

The problem: a **Raspberry Pi Zero W** (single-core armv6, 427 MB RAM, 800×480
panel) can't run Chromium reliably — it crashes and it's slow. dashcast moves the
browser onto a capable box, screenshots the dashboard at the panel's native
resolution, packs it into raw framebuffer bytes, and lets the Pi just fetch those
bytes and write them straight to `/dev/fb0`. **No browser, no X, no image
library, no subprocess on the Pi** — just bytes onto the framebuffer.

One codebase, two roles, one config file:

| role | runs on | what it does |
|------|---------|--------------|
| `dashcast serve` | a capable box, in Docker | Playwright + Chromium → screenshot → pack to RGB565 → serve over HTTP |
| `dashcast display` | the Pi | stdlib-only fetch loop → write bytes to `/dev/fb0` |
| `dashcast make-offline` | dev/server (needs Pillow) | pre-render the "offline" notice box shipped to the Pi |

## How it works

```
                 SERVE side (Docker)                          DISPLAY side (Pi)
   ┌───────────────────────────────────────┐        ┌─────────────────────────────┐
   │ Playwright Chromium @ 800×480          │        │ dashcast display            │
   │  · inject HA long-lived token          │  HTTP  │  · GET dashboard.fb every 5s │
   │  · navigate ONCE, then screenshot each │ ─────► │  · seek(0)+write to /dev/fb0 │
   │    interval (periodic reload safety)   │  :8080 │  · redraw only on change     │
   │  · pack PNG → RGB565 (dashboard.fb)    │        │  · server down > grace →     │
   │  · atomic write .png + .fb             │        │    dim last frame + notice   │
   │ built-in HTTP server (no-store)        │        │    (recovers automatically)  │
   └───────────────────────────────────────┘        └─────────────────────────────┘
```

Key properties:

- **Capture is "screenshot-only":** the browser navigates to the dashboard once
  at startup, then just screenshots on each interval (HA's websocket keeps the
  DOM live). A full reload happens every `reload_interval_seconds` as a safety
  net. Cheap enough for a fast refresh.
- **The HA long-lived token lives only on the serve side** (env / Docker `.env`),
  never on the Pi. The Pi never talks to Home Assistant — only to the image
  endpoint.
- Both `dashboard.png` (for eyeballing in a browser) and `dashboard.fb` (raw
  bytes for the panel) are served; the display fetches the `.fb`.
- **Offline handling:** if the server is unreachable for longer than
  `offline_after_seconds`, the Pi dims the last frame and composites a
  pre-rendered "Dashboard offline" notice, then recovers on its own when the
  server returns.

## Example setup

Addresses below are placeholders (RFC 5737 documentation IPs) — substitute your
own. Nothing here needs public addresses; a flat LAN is fine.

| piece | example address | notes |
|-------|-----------------|-------|
| Home Assistant | `192.0.2.10:8123` | a dashboard path, e.g. `/lovelace/0`; ideally a kiosk-mode user |
| serve host | `192.0.2.20` | any Docker-capable box (4 cores / a couple GB RAM is plenty) |
| display device | `192.0.2.30` | e.g. a Raspberry Pi Zero W, fb0 = 800×480 RGB565 |

The serve host must reach Home Assistant; the display device must reach the
serve host's HTTP port. The display never talks to Home Assistant directly.

## Repo layout

```
dashcast/
  cli.py           entrypoint / subcommand dispatch
  config.py        one TOML → typed config (token from env/file, never in TOML)
  constants.py     resolution, fb formats, offline-box size — the shared contract
  serve.py         persistent Chromium, token injection, screenshot loop, HTTP server
  display.py       stdlib-only fetch loop, framebuffer writes, offline overlay
  offline.py       pre-render the offline notice box (Pillow)
config.example.toml  the whole system in one file
docker/              Dockerfile + compose + .env.example for the serve side
deploy/
  dashcast-display.service   systemd unit for the Pi
  install-server.sh          build + run on the serve side
  install-pi.sh              convert the Pi to a dashcast display
  assets/offline_box.fb      pre-rendered offline notice (generated)
```

## Commands

Run via `python -m dashcast <command>` (or `dashcast <command>` if installed).

### `serve` — render + host the image (serve side)
```bash
dashcast serve -c config.toml          # run the screenshot + HTTP loop
dashcast serve -c config.toml --once   # capture a single frame and exit (no server)
```
Serves `http://<host>:<port>/dashboard.png`, `/dashboard.fb`, and `/healthz`.

### `display` — show the image on the framebuffer (Pi)
```bash
dashcast display -c config.toml          # fetch loop → /dev/fb0
dashcast display -c config.toml --once   # fetch and paint one frame, then exit
```
Add `-v` for debug logging (shows each poll, including "unchanged" cycles).

### `make-offline` — pre-render the offline notice box (needs Pillow)
```bash
dashcast make-offline -c config.toml -o deploy/assets/offline_box.fb \
  --text "Dashboard offline" --subtext "Can't reach the render server"
```
Produces a raw `.fb` blob sized to `OFFLINE_BOX_*` in the panel's `fb_format`.
`install-pi.sh` ships it to `/etc/dashcast/offline_box.fb`.

## Setup — serve side

1. Create a long-lived token in HA: **Profile → Security → Long-lived access tokens**.
2. Configure and add the secret:
   ```bash
   cp config.example.toml docker/config.toml   # edit [home_assistant] + [render]
   cp docker/.env.example docker/.env           # paste the token into DASHCAST_HA_TOKEN
   ```
3. (Optional) regenerate the offline box if you changed the wording/format:
   ```bash
   docker compose run --rm dashcast make-offline -c /app/config.toml -o /data/offline_box.fb ...
   ```
4. Build and run:
   ```bash
   bash deploy/install-server.sh          # or: cd docker && docker compose up -d --build
   ```
5. Verify:
   ```bash
   curl -I http://localhost:8080/dashboard.png     # open in a browser to eyeball
   curl -so /dev/null -w '%{size_download}\n' http://localhost:8080/dashboard.fb   # == width*height*bpp
   ```

## Setup — display side (the Pi)

1. Copy the repo to the Pi (e.g. `rsync -az ./ pi@<pi>:dashcast/`), then:
   ```bash
   sudo bash deploy/install-pi.sh
   sudo nano /etc/dashcast/config.toml      # set [display] image_url to the server
   sudo systemctl start dashcast-display
   journalctl -u dashcast-display -f
   ```
2. `install-pi.sh` disables the FullPageOS Chromium kiosk (`lightdm`), frees
   `tty1` (disables `getty@tty1`), boots to console, installs the offline box,
   and enables the service. It prints exact revert steps.

## Common operations

**Change the HA token / kiosk user** — edit `docker/.env`, then **recreate** the
container (a plain `restart` keeps the old env):
```bash
cd docker && docker compose up -d --force-recreate
```

**Change dashboard, resolution, or refresh interval** — edit `docker/config.toml`
(`dashboard_path`, `width/height`, `interval_seconds`) and the Pi's
`/etc/dashcast/config.toml` (`interval_seconds`), then restart each side.

**Redeploy code changes:**
```bash
# serve side (code baked into image):
cd docker && docker compose up -d --build
# Pi:
sudo cp -r ~/dashcast/dashcast /opt/dashcast/ && sudo systemctl restart dashcast-display
```

**Watch logs:**
```bash
docker compose logs -f                        # serve side
journalctl -u dashcast-display -f             # Pi
```

**Revert the Pi to the FullPageOS kiosk:**
```bash
sudo systemctl disable --now dashcast-display
sudo systemctl enable --now getty@tty1
sudo systemctl enable --now lightdm
sudo systemctl set-default graphical.target
```

**Free disk on the serve side** (tight hosts) — safe to prune build cache and
dangling images without touching other containers:
```bash
docker builder prune -f && docker image prune -f
```

## Configuration reference

`[home_assistant]`
| key | default | meaning |
|-----|---------|---------|
| `url` | — (required) | HA base URL reachable from the serve side |
| `dashboard_path` | `/lovelace/0` | view to capture |
| `token_file` | — | optional; else `DASHCAST_HA_TOKEN` env var |

`[render]`
| key | default | meaning |
|-----|---------|---------|
| `width` / `height` | 800 / 480 | must match the panel |
| `device_scale_factor` | 1.0 | Playwright DPR |
| `interval_seconds` | 5 | seconds between screenshots |
| `wait_until` | `networkidle` | Playwright load state (on navigate/reload) |
| `wait_after_load_ms` | 4000 | settle after a load/reload |
| `nav_timeout_ms` | 60000 | navigation timeout |
| `reload_interval_seconds` | 300 | full reload safety net (0 = never) |
| `fb_format` | `rgb565` | raw format; `""` = PNG only |
| `output_dir` / `output_name` / `fb_name` | `/data` / `dashboard.png` / `dashboard.fb` | outputs |
| `extra_css` | — | CSS injected before each shot |

`[serve]`
| key | default | meaning |
|-----|---------|---------|
| `host` / `port` | `0.0.0.0` / 8080 | HTTP bind |

`[display]`
| key | default | meaning |
|-----|---------|---------|
| `image_url` | `…/dashboard.fb` | raw blob URL (NOT the `.png`) |
| `interval_seconds` | 5 | poll interval |
| `fetch_timeout_seconds` | 15 | per-fetch timeout |
| `framebuffer` | `/dev/fb0` | device to write |
| `fb_format` | `rgb565` | must match `[render].fb_format` |
| `cache_path` | `/var/lib/dashcast/dashboard.fb` | last-good frame cache |
| `offline_after_seconds` | 60 | show offline overlay after this staleness (0 = keep last frame) |
| `offline_box_path` | `/etc/dashcast/offline_box.fb` | pre-rendered notice box |

## Troubleshooting

- **Blank / login-screen dashboard:** token injection needs tuning — check
  `_INIT_SCRIPT` in `serve.py` and that `[home_assistant].url` exactly matches
  HA's own base URL. Remember a token change needs `--force-recreate`.
- **Wrong colors / garbled image:** `[render].fb_format` and
  `[display].fb_format` must match the panel. Check with `fbset -s` (this panel is
  `rgb565`, `rgba 5/11,6/5,5/0`). Supported: rgb565, bgr565, rgb888, bgr888,
  rgba8888, bgra8888.
- **No flicker by design:** frames are a single `seek(0)+write`. `install-pi.sh`
  disables `getty@tty1` and the service quiets the cursor/blanking so nothing
  draws over the image.
- **Live content** (cameras, animations) only updates as fast as
  `interval_seconds`; it is not real-time.
- **Prefer PNG + a viewer instead of raw framebuffer?** Set `[render].fb_format
  = ""` to serve only PNG and point something like `feh --reload` at the `.png`.
