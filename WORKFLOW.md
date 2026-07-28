# Field Workflow — Offload & Stitch

Operational cheat‑sheet for getting recordings **off the Orin**, **onto external/desktop storage**, and **stitched**. Two machines are involved:

- **Orin** (Linux) — recording, mounting the external SSD, transferring, deleting.
- **Desktop / Mac** — stitching and remuxing (the `StitchPipeline` tool lives here).

Commands note which machine they run on. Replace `game_YYYY-MM-DD_HH-MM-SS` with your actual filename.

---

## Quick reference

| Fact | Value |
|---|---|
| Recordings live on the Orin at | `/mnt/video/` |
| Each take produces | `game_TS.mkv` (video) · `game_TS_aN.wav` (audio, one per mic segment) · `game_TS.sync.json` (A/V sync anchors) · optional `game_TS.tegrastats.log` |
| Recording bitrate (real game footage, q85) | ~**2.15 GB/min** ≈ **~130 GB/hr** (audio adds only ~**0.35 GB/hr**) |
| A ~75‑min game | ~**155–170 GB** |
| External SSD (SanDisk, exFAT) mounts at | `/mnt/usb` |
| Copy glob — grab a whole take | `game_TS*` (**no dot** — the dot form misses `_aN.wav`) |
| Audio is a SEPARATE file, synced in post | `merge_av.py` reads the sidecar (see §2.5) |
| Stitcher output codec | MPEG‑4 Part 2 (`mp4v`) in `.mp4` (**drops audio** — re‑add with `merge_av.py`) |
| Parallel stitch needs an **indexed** file | remux first (see below) |
| Auto‑offload on plug‑in (optional) | `field-offload/` — udev + systemd (see §1h) |

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
ls -lht /mnt/video/game_*
```

### 1d. Copy (transfer)

The exFAT mount is root‑owned, so use `sudo`. Use the trailing `*` (**not** `.*`) so the glob grabs the whole take — `.mkv`, every `_aN.wav`, the `.sync.json`, and any `.tegrastats.log`:

```bash
sudo rsync -avh --progress /mnt/video/game_YYYY-MM-DD_HH-MM-SS* /mnt/usb/
```

> ⚠️ The old `game_TS.*` pattern (dot) **misses the audio** — `game_TS_a1.wav` has no dot right after the timestamp. Always use `game_TS*`.

Multiple recordings in one go:

```bash
sudo rsync -avh --progress \
  /mnt/video/game_A* \
  /mnt/video/game_B* \
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
sudo rm /mnt/video/game_YYYY-MM-DD_HH-MM-SS*
```

(That removes the whole take — video, audio segments, sidecar, and log.)

Check free space:

```bash
df -h /mnt/video
```

> A full drive mid‑recording produces an **unfinalized (non‑seekable) MKV** — keep headroom.

### 1h. Automatic offload on plug‑in (optional, hands‑free)

Instead of the manual mount + rsync above, you can have the Orin **auto‑mount the SSD and copy everything the moment you plug it in**. Files live in [`field-offload/`](field-offload/): a udev rule fires on the labeled drive → a systemd service → a script that mounts, `rsync`s all of `/mnt/video`, and unmounts.

- **Copy only** — it never deletes from the Orin (the golden rule holds; delete stays manual after you verify).
- **Only *this* drive triggers it** — it matches the SSD's filesystem **UUID** (`5E64-018F`), unique to this drive. (Not the label: this SanDisk's label `Extreme SSD` is the factory default shared by every Extreme.) A random USB stick — even another SanDisk Extreme — does nothing.
- **Incremental** — re‑plugging only copies new takes.

**Install (once, on the Orin):**

```bash
sudo ~/orin-recorder/field-offload/install.sh
```

No labeling needed — it's keyed to the UUID. Confirm the UUID still matches with `lsblk -f`. If you ever **reformat or replace** the SSD, its UUID changes: update it in both `orin-offload.sh` and `99-orin-offload.rules`, then re‑run `install.sh`.

**Use it:** just plug the SSD in. Watch progress and see when it's safe to unplug:

```bash
tail -f /var/log/orin-offload.log
```

The log prints `unmounted /mnt/usb - safe to unplug.` when the copy is done and the drive is released.

---

## 2. Make a recording seekable / indexed (desktop or Mac)

Raw recordings often lack a seek index (they weren't finalized cleanly), so they **won't scrub in a player** and **can't be seeked** by the parallel stitcher. Remux to add an index — fast and lossless (`-c copy`, no re‑encode):

```bash
ffmpeg -fflags +genpts -i "game_YYYY-MM-DD_HH-MM-SS.mkv" -c copy "game_seekable.mkv"
```

Use the `_seekable.mkv` for playback **and** for fast parallel stitching.

---

## 2.5 Add the recorded audio (desktop or Mac)

Audio is recorded as a **separate `.wav`** (not muxed into the MKV — that keeps the video bulletproof if the mic drops), and aligned in post from the `.sync.json` sidecar. `recorder/merge_av.py` reads the sidecar and muxes video + every audio segment into one synced file, delaying each segment by its captured offset.

**Onto the raw recording:**

```bash
python3 recorder/merge_av.py "game_YYYY-MM-DD_HH-MM-SS.sync.json"
```

→ writes `game_YYYY-MM-DD_HH-MM-SS.withaudio.mkv`.

**Onto a stitched panorama** (the stitcher drops audio, so re‑attach it after §3). The frame timeline is unchanged, so the same sidecar aligns it:

```bash
python3 recorder/merge_av.py "game_YYYY-MM-DD_HH-MM-SS.sync.json" --video "stitched.mp4" --out "stitched_withaudio.mkv"
```

Notes:
- Keep the `.mkv`, `_aN.wav`, and `.sync.json` **together in one folder** — `merge_av.py` finds the video/WAVs relative to the sidecar.
- No audio segments in the sidecar → the take was video‑only (no mic seen); nothing to merge, and that's fine.
- A take with no mic at first, then plugged in mid‑game, merges with correct **silence before the mic came online** — that's the anchor alignment, not a bug.

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
rsync -avP joe@192.168.86.150:'/mnt/video/game_YYYY-MM-DD_HH-MM-SS*' ~/Desktop/orin-recordings/
```

Windows (built‑in `scp`):

```
scp joe@192.168.86.150:"/mnt/video/game_YYYY-MM-DD_HH-MM-SS*" F:\orin-recordings\
```

(Trailing `*`, not `.*`, so the audio `_aN.wav` and `.sync.json` come along too.)

Then verify sizes on the desktop:

```powershell
Get-ChildItem F:\orin-recordings\game_YYYY-MM-DD_HH-MM-SS.* | Select Name, Length
```

Compare `Length` to the Orin's `ls -l` byte count.
