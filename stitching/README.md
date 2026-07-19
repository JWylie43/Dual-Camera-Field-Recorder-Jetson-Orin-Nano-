# stitching

Calibration-driven cylindrical panorama stitcher for the dual-camera rig.

`stitch_pipeline.cpp` takes the combined **LEFT|RIGHT** feed (a single image or a
video) and stitches it into one wide cylindrical panorama. Alignment comes purely
from the rig's **calibration** — the per-camera intrinsics and the stereo extrinsic
rotation `R`. There is **no feature detection** (no BRISK / matcher / findHomography).

## How it works

1. Read the calibration from `../calibration`: `left_intrinsics.json`,
   `right_intrinsics.json`, `stereo_extrinsics.json`.
2. Build one **remap table per camera** that folds *undistortion + cylindrical
   projection + extrinsic rotation* into a single lookup. These depend only on the
   calibration, so they're computed **once** per run.
3. For each frame: split into left/right halves, `remap` both onto the common
   cylinder, and composite with a **hard seam** at the centre of the overlap.

The per-frame work runs on `cv::UMat`, so it uses the **GPU via OpenCL** when
available (falls back to CPU otherwise) — the same source runs on macOS and the
desktop with no changes.

## Build

Requires OpenCV (4.x or 5.x) and CMake.

```bash
# macOS:  brew install cmake opencv
cmake -S . -B build && cmake --build build
```

Produces the `StitchPipeline` executable in `build/`.

## Run

```bash
# single image -> pipeline_out/pano.jpg (+ overlap_5050.jpg)
./build/StitchPipeline --source ../calibration/images/calib_114.jpg

# video -> pipeline_out/stitched_video.mp4
./build/StitchPipeline --source recording.mp4 --start 300 --end 900
```

The source type is auto-detected by extension: image files stitch a single frame;
video files (`.mp4/.mkv/.mov/...`) loop over frames.

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--source PATH` | (required) | Combined LEFT\|RIGHT image or video (alias: `--image`) |
| `--calib-dir DIR` | `../calibration` | Folder with the intrinsics/extrinsics JSON |
| `--out DIR` | `pipeline_out` | Output folder |
| `--start N` | `0` | First video frame to process |
| `--end N` | `-1` | Last video frame (`-1` = end of clip) |
| `--degrees D` | `0` | Horizon roll correction, degrees |
| `--seam X` | auto | Override the hard-seam column (default: overlap centre) |

## Outputs

- `pano.jpg` (image mode) or `stitched_video.mp4` (video mode)
- `overlap_5050.jpg` — a 50/50 blend of the overlap band; a well-aligned rig shows
  features as a single sharp image there, not a ghost/double.

## Files

| File | Purpose |
|---|---|
| `stitch_pipeline.cpp` | The stitcher (only source file) |
| `CMakeLists.txt` | Build config |
| `include/json.hpp` | Bundled JSON parser (reads the calibration files) |

## Notes

- **Calibration first.** Generate the JSON files with the tools in `../calibration`
  before stitching. Re-run calibration if the cameras are disturbed in their housing.
- **Exposure/seam.** In high-contrast scenes the seam may show a brightness step
  (one camera facing a bright window). Exposure compensation and seam feathering are
  future additions; on evenly-lit outdoor footage it isn't an issue.
