#!/usr/bin/env bash
# Install/refresh the dashcast SERVE side (Docker).
# Run ON the serve host from the repo root:  bash deploy/install-server.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR/docker"

if [[ ! -f config.toml ]]; then
  echo "docker/config.toml missing — copy and edit it:"
  echo "    cp ../config.example.toml docker/config.toml"
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "docker/.env missing — create it with your HA token:"
  echo "    cp .env.example .env   # then edit DASHCAST_HA_TOKEN"
  exit 1
fi

echo "==> building and starting dashcast container"
docker compose up -d --build

echo
echo "Done. Check it:"
echo "  docker compose logs -f"
echo "  curl -I http://localhost:8080/dashboard.png"
