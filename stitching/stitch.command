#!/usr/bin/env bash
#
# stitch.command — double-click convenience launcher for the panorama tuner.
#
# Run with no arguments, StitchPipeline opens the browser tuner immediately.
# Inside the page: click "Import source…" (a native file dialog opens) to load a
# video/image, align the far/near edges, then click "Stitch all frames" — which
# pops a native "save as" dialog to choose the output path + filename.
# This script just builds the binary on first run and then runs it, so you can
# launch the whole flow by double-clicking in Finder.
#
# On Windows, double-click build\StitchPipeline.exe directly (it does the same).
set -e
cd "$(dirname "$0")"

if [ ! -x build/StitchPipeline ]; then
  echo "==> Building StitchPipeline (first run)…"
  cmake -S . -B build >/dev/null
  cmake --build build >/dev/null
fi

exec ./build/StitchPipeline
