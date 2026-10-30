#!/bin/sh
set -eu
mkdir -p /recordings
# 60-second segments; background cleanup keeps the retention limit during long sessions.
(while :; do
  find /recordings -type f -name 'tapo-*.mp4' -mtime +7 -delete
  sleep 3600
done) &
while :; do
  ffmpeg -hide_banner -nostdin -loglevel warning -rtsp_transport tcp -i "$CAMERA_RTSP" \
    -map 0:v:0 -an -c:v copy -f segment -segment_time 60 -reset_timestamps 1 \
    -strftime 1 '/recordings/tapo-%Y%m%d-%H%M%S.mp4' || true
  find /recordings -type f -name 'tapo-*.mp4' -mtime +7 -delete
  sleep 5
done
