# HanzHub camera addon — Tapo C320WS

## Live stream (WebRTC)
We use a standalone **go2rtc** process to convert local RTSP to WebRTC; browser playback uses go2rtc's own `stream.html`. This is optional and separate from the existing IoT API.

1. In Tapo app enable **Camera Account** (not TP-Link cloud credentials) and ensure TCP 554 is reachable from HUB.
2. On HUB: `cd camera && python3 setup.py` and input LAN IP and two separate credentials (camera and viewer).
3. Start with `docker compose up -d go2rtc`.
4. On LAN open `http://HUB-IP:1984/stream.html?src=tapo_c320ws`, authenticate using the viewer credentials.
5. For optional 24/7 recording run `docker compose --profile record up -d`; for approximate pixel-scene change events also enable `--profile motion`.

**Security:** do not publish go2rtc (1984/TCP, 8555/TCP+UDP) on the Internet, including Cloudflare Tunnel. The addon uses host networking for WebRTC; explicitly firewall these ports for trusted LAN clients. API username/password and camera credentials reside in `camera/runtime/` (ignored by git). Container logs and diagnostic exports must not include RTSP credentials. For remote viewing, set up a separately authenticated, HTTPS WebRTC reverse proxy and suitable ICE/TURN access before exposing the player. HTTP viewer works on LAN; an HTTPS dashboard should not iframe an HTTP viewer due to mixed content.

**Limitations:** preliminary optional addon, not production deployed; the viewer and recorder have not been tested with a physical C320WS. ffmpeg scene score triggers on lighting changes, not verified human/object motion; no push notifications, cloud upload or AI classification. Recorder stores continuous 60-second MP4 segments, retaining ~7 days **only when the ffmpeg command restarts**; add a scheduled retention job for reliable cleanup during uninterrupted recording. Check storage capacity/permissions and local privacy requirements before enabling recording.
