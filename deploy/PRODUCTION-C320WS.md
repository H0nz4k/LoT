# Production rollout — Tapo C320WS

This feature is in **draft PR #4 and PR #5**, not deployed by a GitHub commit.
Camera: LAN IP `192.168.1.53`, MAC `CC:BA:BD:4E:35:CC`.

## Blocking conditions before production
- Validate GUI and camera integration against actual C320WS, incl. authentication and playback.
- Confirm current Docker volume, service name and host-specific compose environment.
- Confirm no public Cloudflare / reverse-proxy route exposes go2rtc API 1984, WebRTC 8555, RTSP 554, recordings or camera credentials.
- Current WebRTC player opens a separate LAN-only tab, **not embedded live video**. Current camera registration validates port 554, not Camera Account credentials. These are outstanding GUI-only requirements.
- Video recording and scene-change logging are **opt-in**, with no automatic person recognition.

## Backups and rollback
`backup-and-deploy.sh` is a guarded starting point for the **existing production checkout** only. It builds before shutdown, stops IoT for consistent SQLite volume backup, and saves original image ID, .env and Git commit in `/var/backups/hanzhub-iot`. It must not be run until the preceding blockers are cleared. A deployment may need further adjustments for the actual HUB environment and Docker image naming.

To restore data after the service has stopped, review the volume name and run a one-off tar extraction using the saved archive. **Never overwrite a running SQLite database**. Verify the service and current module list after any rollback.

## Live stream
Camera addon uses `camera/setup.py` for local RTSP credentials, `camera/docker-compose.yml` for optional go2rtc and ffmpeg recorder. Do not publish host-network ports 1984/8555. Go2rtc WebRTC over HTTPS/cloud requires separate secure integration, and is not delivered by this initial LAN-only addon.
