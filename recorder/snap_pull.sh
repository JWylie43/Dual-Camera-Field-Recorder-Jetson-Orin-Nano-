#!/usr/bin/env bash
# snap_pull.sh - run ON THE MAC. Captures a tuned 4K still on the Orin via
# snap.sh, pulls it to the Desktop with a timestamped name, and opens it in
# Preview (view at 1:1 with cmd+0 there). Pass snap.sh dials as arguments:
#
#   ./recorder/snap_pull.sh EXP_MS=2 GAIN=1
#   ./recorder/snap_pull.sh EE=0 TNR=0 FLIP=2
#   ./recorder/snap_pull.sh                      # all-auto, recorder defaults
#
# Dial reference: see the header of recorder/snap.sh.
# Prereqs: `ssh orin` works keylessly; camera-rig service stopped on the Orin;
# repo pulled on the Orin (~/orin-recorder).
set -euo pipefail

out=~/Desktop/snap_$(date +%H%M%S).jpg
ssh orin "cd ~/orin-recorder/recorder && $* ./snap.sh"
scp -q orin:/tmp/snap.jpg "$out"
open "$out"
echo "$out"
