#!/usr/bin/env bash
#
# stitch.command — one-click launcher for the panorama tuner.
#
#   Double-click this file in Finder (macOS) or run ./stitch.command
#   1. Pops a native dialog to pick the input video/image (anywhere on disk)
#   2. Pops a native dialog to pick the output destination + filename
#   3. Opens the browser tuner already loaded with that file; align the
#      far/near edges, then click "Stitch all frames" to render to your output.
#
# Smart seam, multi-band blend, and exposure match are all on by default.
set -e

# Always work from this script's directory (Finder launches from $HOME).
cd "$(dirname "$0")"

# Build the binary on first run (or if it's missing).
if [ ! -x build/StitchPipeline ]; then
  echo "==> Building StitchPipeline (first run)…"
  cmake -S . -B build >/dev/null
  cmake --build build >/dev/null
fi

OS="$(uname -s)"

# pick_input  -> echoes a POSIX path, or nothing if cancelled
pick_input() {
  case "$OS" in
    Darwin) osascript -e 'POSIX path of (choose file with prompt "Select the input video or image")' 2>/dev/null ;;
    Linux)  zenity --file-selection --title="Select the input video or image" 2>/dev/null ;;
  esac
}

# pick_output <default-name>  -> echoes a POSIX path, or nothing if cancelled
pick_output() {
  local def="$1"
  case "$OS" in
    Darwin) osascript -e "POSIX path of (choose file name with prompt \"Save the stitched output as\" default name \"$def\")" 2>/dev/null ;;
    Linux)  zenity --file-selection --save --confirm-overwrite --filename="$def" 2>/dev/null ;;
  esac
}

if [ "$OS" != "Darwin" ] && [ "$OS" != "Linux" ]; then
  echo "Native file dialogs are only wired up for macOS and Linux here."
  echo "On Windows, run directly:  build\\StitchPipeline.exe --source <in> --out-file <out> --tune"
  exit 1
fi

INPUT="$(pick_input)"
if [ -z "$INPUT" ]; then echo "No input selected — cancelled."; exit 0; fi

# Suggest an output extension based on the input type (bash 3.2-safe lowercasing).
LOWER="$(printf '%s' "$INPUT" | tr '[:upper:]' '[:lower:]')"
case "$LOWER" in
  *.jpg|*.jpeg|*.png|*.bmp|*.tif|*.tiff) DEF="stitched.jpg" ;;
  *)                                     DEF="stitched.mp4" ;;
esac

OUTPUT="$(pick_output "$DEF")"
if [ -z "$OUTPUT" ]; then echo "No output selected — cancelled."; exit 0; fi

echo "Input:  $INPUT"
echo "Output: $OUTPUT"
echo "Launching tuner…"
exec ./build/StitchPipeline --source "$INPUT" --out-file "$OUTPUT" --tune
