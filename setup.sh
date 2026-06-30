#!/usr/bin/env bash
#
# setup.sh - install dependencies for the Orin Nano stereo recorder.
# Run on the Jetson:  ./setup.sh
#
set -e

echo "==> Installing apt dependencies..."
sudo apt-get update
sudo apt-get install -y \
  v4l-utils \
  python3-flask \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good

echo
echo "==> Checking for the NVIDIA L4T GStreamer elements (ship with JetPack)..."
missing=0
for e in nvv4l2camerasrc nvvidconv nvjpegenc; do
  printf '    %-18s ' "$e"
  if gst-inspect-1.0 "$e" >/dev/null 2>&1; then
    echo OK
  else
    echo "MISSING"
    missing=1
  fi
done

if [ "$missing" -ne 0 ]; then
  echo
  echo "    One or more NVIDIA elements are missing. They come with JetPack/L4T."
  echo "    Reinstall/repair the L4T multimedia stack (nvidia-l4t-gstreamer)."
fi

echo
echo "==> Checking the camera and storage..."
[ -e /dev/video0 ] && echo "    /dev/video0 present" \
                    || echo "    /dev/video0 MISSING - check the ribbon + Arducam driver"
[ -d /mnt/video ] && echo "    /mnt/video present" \
                  || echo "    /mnt/video MISSING - mount your NVMe there (see README)"

echo
echo "Done. Quick test:   python3 record.py --measure --seconds 10"
echo "Web panel:          sudo python3 server.py   (then http://<orin-ip>:8080)"
