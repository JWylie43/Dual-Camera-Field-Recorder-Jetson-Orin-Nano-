#!/usr/bin/env bash
#
# stitch.command — double-click convenience launcher for the panorama tuner.
#
# The StitchPipeline binary is self-contained: run with no --source it opens
# native file dialogs (input + output) and launches the browser tuner itself.
# This script just builds it on first run and then runs it, so you can launch
# the whole flow by double-clicking in Finder.
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
