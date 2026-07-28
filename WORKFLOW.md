# Field Workflow — Offload & Stitch

Operational cheat‑sheet for getting recordings **off the Orin**, **onto external/desktop storage**, and **stitched**. Two machines are involved:

- **Orin** (Linux) — recording, mounting the external SSD, transferring, deleting.
- **Desktop / Mac** — stitching and remuxing (the `StitchPipeline` tool lives here).

Commands note which machine they run on. Replace `game_YYYY-MM-DD_HH-MM-SS` with your actual filename.

---

## Quick reference

| Fact | Value |
|---|---|
| Recordings live on the Orin at | `/mnt/video/*.mkv` (+ `*.tegrastats.log`) |
| Recording bitrate (real game footage, q85) | ~**2.15 GB/min** ≈ **~130 GB/hr** |
| A ~75‑min game | ~**155–170 GB** |
| External SSD (SanDisk, exFAT) mounts at | `/mnt/usb` |
| Stitcher output codec | MPEG‑4 Part 2 (`mp4v`) in `.mp4` |
| Parallel stitch needs an **indexed** file | remux first (see below) |

**Golden rule:** *copy → verify → only then delete.* Never delete a recording off the Orin until the copy is verified.

---

## 1. Offload recordings from the Orin → external SSD

Run all of these **on the Orin** (SSH in).

### 1a. Plug in the SSD and confirm SuperSpeed

Use the **USB 3.x (SuperSpeed) C‑to‑A cable** into a **blue** USB‑A port, then:

```bash
lsusb -t
```

You want the Mass Storage line under the **`10000M`** (or `5000M`) bus. If it shows `480M`, it fell back to USB 2.0 — swap to a real SuperSpeed cable.

### 1b. Find and mount the drive (exFAT)

```bash
lsblk -f
```

Find the exFAT "Extreme SSD" partition (usually `/dev/sda1`), then mount it with the FUSE helper:

```bash
sudo mount.exfat-fuse /dev/sda1 /mnt/usb
```

One‑time install if the helper is missing:

```bash
sudo apt install -y exfat-fuse exfatprogs
```

Confirm it mounted (want `/dev/sda1`, ~1.8T):

```bash
df -h /mnt/usb
```

### 1c. See what you have and pick files

```bash
ls -lht /mnt/video/*.mkv /mnt/video/*.tegrastats.log
```

### 1d. Copy (transfer)

The exFAT mount is root‑owned, so use `sudo`. The `.*` grabs each recording's `.mkv` **and** its `.tegrastats.log`:

```bash
sudo rsync -avh --progress /mnt/video/game_YYYY-MM-DD_HH-MM-SS.* /mnt/usb/
```

Multiple recordings in one go:

```bash
sudo rsync -avh --progress \
  /mnt/video/game_A.* \
  /mnt/video/game_B.* \
  /mnt/usb/
```

### 1e. Verify the copy (before deleting anything)

**Fast check — byte sizes must match exactly.** Source vs destination:

```bash
ls -l /mnt/video/game_YYYY-MM-DD_HH-MM-SS.mkv
ls -l /mnt/usb/game_YYYY-MM-DD_HH-MM-SS.mkv
```

Equal byte counts = the transfer completed (a partial copy can't match the size).

**Bit‑perfect check (optional, slow) — SHA‑256:**

```bash
sha256sum /mnt/video/game_YYYY-MM-DD_HH-MM-SS.mkv
sha256sum /mnt/usb/game_YYYY-MM-DD_HH-MM-SS.mkv
```

Matching hashes = a perfect copy.

### 1f. Unmount safely (always, before unplugging)

```bash
sync && sudo umount /mnt/usb
```

Wait for the prompt to return — **that's the signal it's safe to unplug.** Skipping this can corrupt the file *and* the exFAT filesystem.

### 1g. Delete originals to free the Orin drive

**Only after the copy is verified.** The NVMe fills fast (~130 GB/hr), so offload + delete every 2–3 games.

```bash
sudo rm /mnt/video/game_YYYY-MM-DD_HH-MM-SS.mkv \
        /mnt/video/game_YYYY-MM-DD_HH-MM-SS.tegrastats.log
```

Check free space:

```bash
df -h /mnt/video
```

> A full drive mid‑recording produces an **unfinalized (non‑seekable) MKV** — keep headroom.

---

## 2. Make a recording seekable / indexed (desktop or Mac)

Raw recordings often lack a seek index (they weren't finalized cleanly), so they **won't scrub in a player** and **can't be seeked** by the parallel stitcher. Remux to add an index — fast and lossless (`-c copy`, no re‑encode):

```bash
ffmpeg -fflags +genpts -i "game_YYYY-MM-DD_HH-MM-SS.mkv" -c copy "game_seekable.mkv"
```

Use the `_seekable.mkv` for playback **and** for fast parallel stitching.

---

## 3. Stitch (desktop or Mac)

The stitcher is in `stitching/`. Build once (`stitch.bat` on Windows / `./stitch.command` on Mac); the binary is `build\Release\StitchPipeline.exe` (Windows) or `build/StitchPipeline` (Mac). Run from the `stitching/` folder.

### 3a. Interactive tuner (recommended first)

```bash
./stitch.command        # Mac  (double-click also works)
```
```
stitch.bat              # Windows
```

Opens a browser tuner: **Import source…** → align the far/near edges → **Stitch all frames** (native "save as" dialog for the output). The tuner also shows the **equivalent CLI command** so you can reproduce a tuned render by hand.

### 3b. Command line (headless)

```
build\Release\StitchPipeline.exe --source "game_seekable.mkv" --shift-top 4 --shift-bottom 20 --jobs 6 --out-file "stitched.mp4"
```

Common flags:

| Flag | Meaning |
|---|---|
| `--source <file>` | input video (or image) |
| `--shift-top N` / `--shift-bottom N` | far/near edge alignment (your tuned values) |
| `--crop x,y,w,h` | restrict to a bounding box (full‑canvas coords) |
| `--start N` / `--end N` | frame range (see time→frame below) |
| `--jobs N` | parallel processes (default **4**; ~6 saturates our GPU) |
| `--no-jobs` | force single process |
| `--out-file <path>` | output (keep `.mp4`) |
| `--tune` | open the browser tuner |

### 3c. Stitch a time range only

`--start`/`--end` are **frames**. At **30 fps**: `frame = seconds × 30`.

- 1:30 → `90 × 30 = 2700`
- 3:00 → `180 × 30 = 5400`

```
build\Release\StitchPipeline.exe --source "game_seekable.mkv" --shift-top 4 --shift-bottom 20 --start 2700 --end 5400 --out-file "clip_1m30-3m.mp4"
```

### 3d. Parallel stitch — important

`--jobs N` splits the work across N processes and concats into one file. **It only runs fast on an *indexed* file** — always point `--jobs` at the **remuxed `_seekable.mkv`**, not the raw recording. Needs `ffmpeg` on PATH for the lossless concat.

- Tune `N` to your GPU/VRAM: bump until CPU **or** GPU hits ~90–100% or VRAM fills, then stop.
- Each child logs `seek: indexed jump … (fast)` (good) or `grab-skipping … (SLOW; remux)` (means the input isn't indexed).

---

## 4. Verify a stitched (or any) output — frame counts

Confirm no frames were lost (especially comparing a `--jobs` run to a `--no-jobs` run):

```
ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of csv=p=0 "stitched.mp4"
```

The two counts (parallel vs single) should match; expect roughly `fps × seconds` frames.

---

## 5. Pull files to the desktop over the network (alternative to USB)

If you'd rather transfer over Ethernet instead of the SSD, from the **desktop/Mac** (use the Orin's **wired** IP for full speed):

```bash
rsync -avP joe@192.168.86.150:'/mnt/video/game_YYYY-MM-DD_HH-MM-SS.*' ~/Desktop/orin-recordings/
```

Windows (built‑in `scp`):

```
scp joe@192.168.86.150:"/mnt/video/game_YYYY-MM-DD_HH-MM-SS.*" F:\orin-recordings\
```

Then verify sizes on the desktop:

```powershell
Get-ChildItem F:\orin-recordings\game_YYYY-MM-DD_HH-MM-SS.* | Select Name, Length
```

Compare `Length` to the Orin's `ls -l` byte count.
