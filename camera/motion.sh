#!/bin/sh
set -eu
mkdir -p /events
# FFmpeg scene change detection is not AI/object detection.
# Timestamps appear in log when visual change exceeds the configured threshold.
while :; do
  ffmpeg -hide_banner -nostdin -loglevel info -rtsp_transport tcp -i "$CAMERA_RTSP" \
    -an -vf "fps=2,select='gt(scene,0.12)',showinfo" -f null - \
    2>>/events/motion.log || true
  sleep 5
done
