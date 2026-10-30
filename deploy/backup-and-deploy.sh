#!/usr/bin/env bash
# Run from an existing production checkout of H0nz4k/LoT on HUB.
# Preserves .env, TinyTuya bindings and the existing named Docker volume.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo "Run as root or sudo"; exit 1; }
command -v docker >/dev/null
command -v git >/dev/null
command -v curl >/dev/null
test -f docker-compose.yml
test -d .git
test -f .env || { echo "Missing production .env; refusing to deploy"; exit 1; }
test "$(git remote get-url origin | sed 's#\.git$##')" = "https://github.com/H0nz4k/LoT" || { echo "Unexpected repository"; exit 1; }
test -z "$(git status --porcelain)" || { echo "Uncommitted local changes; abort"; exit 1; }
docker inspect hanzhub_iot >/dev/null
old_image="$(docker inspect --format '{{.Image}}' hanzhub_iot)"
volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/hanzhub-iot"}}{{.Name}}{{end}}{{end}}' hanzhub_iot)"
[ -n "$volume" ] || { echo "Existing SQLite volume not found"; exit 1; }
backup="/var/backups/hanzhub-iot/tapo-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
chmod 700 "$backup"
old_commit="$(git rev-parse HEAD)"
git fetch origin feature/tapo-c320ws-live
echo "Image: $old_image; SQLite volume: $volume; old revision: $old_commit"
# Build first; never change a running container on failed build.
docker compose build hanzhub_iot
docker compose stop hanzhub_iot
# File-level backup taken when SQLite is shut down (avoids WAL inconsistency).
docker run --rm -v "$volume:/source:ro" -v "$backup:/backup" alpine:3.20 \
  sh -c 'cd /source && tar -cpf /backup/data.tar .'
cp -p .env "$backup/env"
echo "$old_commit" > "$backup/previous-commit"
echo "$old_image" > "$backup/previous-image"
chmod 600 "$backup/"*
# Deploy from the fetched branch only if the running project can be fast-forwarded cleanly.
git merge --ff-only origin/feature/tapo-c320ws-live || {
  docker compose up -d --no-build hanzhub_iot
  exit 1
}
if ! docker compose up -d --no-build hanzhub_iot || ! curl -fsS --retry 12 --retry-delay 2 http://127.0.0.1:4011/api/iot/health; then
  echo "Deployment failed. Rolling back previous image and code."
  docker compose stop hanzhub_iot || true
  git reset --hard "$old_commit"
  docker tag "$old_image" hanzhub-iot-hanzhub_iot:latest
  docker compose up -d --no-build hanzhub_iot || true
  echo "Inspect and if necessary restore volume backup at $backup/data.tar"
  exit 1
fi
echo "IoT health OK. Backup: $backup. This does not start go2rtc or enable recording."
