# Render side. We only use Chromium, so instead of the full Playwright image
# (Chromium + Firefox + WebKit, ~2 GB) we build a slim image with just Chromium
# and its system deps — friendlier on a tight disk.
# Keep the playwright pip version in sync with the browser it installs.
# Pin bookworm: Playwright 1.49's install-deps uses Debian-bookworm package
# names (trixie's t64 library renames break `--with-deps`).
FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install Playwright + only the Chromium browser and the OS libraries it needs,
# plus Pillow/numpy for packing screenshots into raw framebuffer bytes.
RUN pip install --no-cache-dir playwright==1.49.0 pillow numpy \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# App code (no third-party Python deps beyond Playwright, installed above).
COPY pyproject.toml /app/pyproject.toml
COPY dashcast /app/dashcast

# Config is mounted at runtime; data dir is a volume.
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["python", "-m", "dashcast"]
CMD ["serve", "--config", "/app/config.toml"]
