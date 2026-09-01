#!/usr/bin/env bash
# grade.sh - the rig's standard post pass, run ON THE MAC (or any box with
# ffmpeg). Applies, in order:
#   1. color matrix   rr=1.37 bb=1.40  - neutralizes the stock Jetson IMX477
#      tuning's teal cast. Measured off sunlit concrete UNDER wbmode=6; only
#      valid while captures use WB=6 (the recorder's baked-in preset).
#   2. clarity        unsharp 13x13 @ 0.35 - large-radius local contrast
#      ("pop" in midtones, not edge sharpening)
#   3. edge sharpen   cas 0.5 - contrast-adaptive, halo-resistant
#
# Usage:
#   ./grade.sh photo.jpg              -> photo_graded.jpg
#   ./grade.sh clip.mkv               -> clip_graded.mp4 (H.264 crf18)
#   ./grade.sh in.jpg out.jpg         -> explicit output path
#
# Cost: stills instant; 4K30 clips re-encode at roughly realtime on the M5
# Pro. One extra lossy generation until this chain moves into the stitcher
# (which already re-encodes, making it free there).
set -euo pipefail

GRADE="colorchannelmixer=rr=1.37:gg=1.0:bb=1.40,unsharp=13:13:0.35,cas=0.5"

in=${1:?usage: grade.sh <image-or-video> [output]}
base=${in%.*}
ext=${in##*.}

case "$(echo "$ext" | tr '[:upper:]' '[:lower:]')" in
    jpg|jpeg|png)
        out=${2:-${base}_graded.$ext}
        ffmpeg -y -v error -i "$in" -vf "$GRADE" -q:v 2 "$out"
        ;;
    mkv|mp4|mov)
        out=${2:-${base}_graded.mp4}
        ffmpeg -y -v error -i "$in" -vf "$GRADE" \
            -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p \
            -c:a copy "$out"
        ;;
    *)
        echo "unsupported extension: .$ext" >&2; exit 1
        ;;
esac
echo "$out"
