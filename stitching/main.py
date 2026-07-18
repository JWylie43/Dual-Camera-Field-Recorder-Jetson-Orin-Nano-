import numpy as np
import os
import tempfile
from moviepy.audio.io.AudioFileClip import AudioFileClip
from moviepy.tools import subprocess_call
import shutil
import json
from datetime import datetime
import webbrowser
import subprocess
import librosa
import numpy as np
from scipy.signal import correlate
from pathlib import Path
import time
import cv2


def butter_bandpass_filter(data, lowcut, highcut, samplerate, order=5):
    from scipy.signal import butter, filtfilt

    nyquist = 0.5 * samplerate
    low = lowcut / nyquist
    high = highcut / nyquist
    b, a = butter(order, [low, high], btype="band")
    return filtfilt(b, a, data)


def calculate_robust_audio_delay(
    file1_path, file2_path, comparison_length_sec: int = 300
):
    """
    Calculate the delay between two audio files using cross-correlation.
    Returns delay in milliseconds.
    """
    y1, sr1 = librosa.load(file1_path, sr=None)
    y2, sr2 = librosa.load(file2_path, sr=None)

    min_length = min(len(y1), len(y2))
    y1 = y1[:min_length]
    y2 = y2[:min_length]

    # Take first 5 minutes or shorter if needed
    y1 = y1[: min(comparison_length_sec * sr1, len(y1))]
    y2 = y2[: min(comparison_length_sec * sr2, len(y2))]

    y1_filtered = butter_bandpass_filter(y1, 300, 8000, sr1)
    y2_filtered = butter_bandpass_filter(y2, 300, 8000, sr2)

    y1_norm = y1_filtered / np.sqrt(np.sum(y1_filtered**2))
    y2_norm = y2_filtered / np.sqrt(np.sum(y2_filtered**2))

    corr = correlate(y1_norm, y2_norm, mode="full")
    max_idx = np.argmax(corr)
    delay_samples = max_idx - (len(y1_norm) - 1)
    delay_ms = (delay_samples / sr1) * 1000
    confidence = np.max(corr)

    print(f"[Delay] {delay_ms:.2f} ms (confidence={confidence:.3f})")
    return {
        "delay_ms": delay_ms,
        "confidence": confidence,
        "delay_samples": delay_samples,
    }


def get_video_metadata(video_path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format_tags=creation_time:stream_tags=timecode:format=duration",
            "-print_format",
            "json",
            video_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    data = json.loads(result.stdout)
    # print("\n data", data)
    creation_time = datetime.strptime(
        data.get("format", {}).get("tags", {}).get("creation_time", None),
        "%Y-%m-%dT%H:%M:%S.%fZ",
    )
    h, m, s, f = map(
        int,
        data.get("streams", {})[0].get("tags", {}).get("timecode", None).split(":"),
    )
    timecode = h * 3600 + m * 60 + s
    duration = data.get("format").get("duration")
    return {
        "video_path": video_path,
        "creation_time": creation_time,
        "timecode": timecode,
        "duration": duration,
    }


def create_concat_list(folder: str, temp_list_path: str = None):
    """
    Create an FFmpeg concat list file for all MP4s in a folder,
    ordered by their embedded recording timestamp (creation_time metadata).
    Prints the sorted order to the console.
    """
    mp4s = [f for f in os.listdir(folder) if f.lower().endswith(".mp4")]
    if not mp4s:
        raise FileNotFoundError(f"No mp4 files found in {folder}")

    # Build list of (filename, creation_time)
    metadata_list = [get_video_metadata(os.path.join(folder, v)) for v in mp4s]
    metadata_list.sort(
        key=lambda m: (
            m["creation_time"],
            m["timecode"],
        )
    )
    print("metadata_list", metadata_list)

    # Write FFmpeg concat list
    with open(temp_list_path, "w") as f:
        for item in metadata_list:
            fullpath = item["video_path"].replace("\\", "/")
            f.write(f"file '{fullpath}'\n")

    print(f"[INFO] Concat list written to {temp_list_path}\n")
    return metadata_list


def get_video_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
    )
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def copy_files_with_shutil(src_dir, dst_dir):
    os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)


def ffmpeg_extract_subclip(
    filename: str, t1: float, t2: float, target_name: str = None
):
    """Makes a new video file playing video file ``filename`` between
    the times ``t1`` and ``t2``. t1 and t2 are in seconds."""
    name, ext = os.path.splitext(filename)
    if not target_name:
        T1, T2 = [int(1000 * t) for t in [t1, t2]]
        target_name = "%sSUB%d_%d.%s" % (name, T1, T2, ext)

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        "%0.2f" % t1,
        "-i",
        filename,
        "-t",
        "%0.2f" % (t2 - t1),
        "-vcodec",
        "copy",
        "-acodec",
        "copy",
        target_name,
    ]

    print("cmd", cmd)

    subprocess_call(cmd)


# def syncVideos(left_video_path, right_video_path, start, end):
#     current_file = Path(__file__).resolve()
#     current_root = current_file.parent
#     output_folder = os.path.join(current_root, "sync_output")
#     os.makedirs(output_folder, exist_ok=True)
#     left_audio_path = os.path.join(output_folder, "left_audio.wav")
#     right_audio_path = os.path.join(output_folder, "right_audio.wav")
#     AudioFileClip(left_video_path).write_audiofile(
#         left_audio_path, ffmpeg_params=["-ac", "1"]
#     )
#     AudioFileClip(right_video_path).write_audiofile(
#         right_audio_path, ffmpeg_params=["-ac", "1"]
#     )
#     delay_result = calculate_robust_audio_delay(left_audio_path, right_audio_path)
#     delay_ms = delay_result["delay_ms"]
#     delay_sec = delay_ms / 1000.0

#     synced_left = os.path.join(output_folder, "synced_left.mp4")
#     synced_right = os.path.join(output_folder, "synced_right.mp4")
#     final_duration = end - start - abs(delay_sec)
#     # Cut both videos and audio using adjusted times
#     left_cut_video_path = os.path.join(output_folder, "cut_left.mp4")
#     right_cut_video_path = os.path.join(output_folder, "cut_right.mp4")
#     ffmpeg_extract_subclip(left_video_path, start, end, left_cut_video_path)
#     ffmpeg_extract_subclip(right_video_path, start, end, right_cut_video_path)
#     if delay_sec > 0:
#         video1_start_time = delay_sec
#         video1_end_time = final_duration + delay_sec
#         video2_start_time = 0
#         video2_end_time = final_duration

#         ffmpeg_extract_subclip(
#             left_cut_video_path, video1_start_time, video1_end_time, synced_left
#         )
#         ffmpeg_extract_subclip(
#             right_cut_video_path, video2_start_time, video2_end_time, synced_right
#         )

#     else:
#         delay_sec = abs(delay_sec)

#         video1_start_time = 0
#         video1_end_time = final_duration
#         video2_start_time = delay_sec
#         video2_end_time = final_duration + delay_sec

#         ffmpeg_extract_subclip(
#             left_cut_video_path, video1_start_time, video1_end_time, synced_left
#         )
#         ffmpeg_extract_subclip(
#             right_cut_video_path, video2_start_time, video2_end_time, synced_right
#         )


def syncVideos(left_video_path, right_video_path, start, end):
    current_file = Path(__file__).resolve()
    current_root = current_file.parent
    output_folder = os.path.join(current_root, "sync_output")
    os.makedirs(output_folder, exist_ok=True)
    left_audio_path = os.path.join(output_folder, "left_audio.wav")
    right_audio_path = os.path.join(output_folder, "right_audio.wav")
    AudioFileClip(left_video_path).write_audiofile(
        left_audio_path, ffmpeg_params=["-ac", "1"]
    )
    AudioFileClip(right_video_path).write_audiofile(
        right_audio_path, ffmpeg_params=["-ac", "1"]
    )
    delay_result = calculate_robust_audio_delay(left_audio_path, right_audio_path)
    delay_ms = delay_result["delay_ms"]
    delay_sec = delay_ms / 1000.0

    left_cut_video_path = os.path.join(output_folder, "cut_left.mp4")
    right_cut_video_path = os.path.join(output_folder, "cut_right.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-i",
        left_video_path,
        "-t",
        str(end - start),
        "-vcodec",
        "copy",
        "-acodec",
        "copy",
        left_cut_video_path,
    ]
    subprocess_call(cmd)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-i",
        right_video_path,
        "-t",
        str(end - start),
        "-vcodec",
        "copy",
        "-acodec",
        "copy",
        right_cut_video_path,
    ]
    subprocess_call(cmd)
    synced_left = os.path.join(output_folder, "synced_left.mp4")
    synced_right = os.path.join(output_folder, "synced_right.mp4")
    if delay_sec > 0:
        video1_start_time = delay_sec
        video2_start_time = 0
    else:
        video1_start_time = 0
        video2_start_time = abs(delay_sec)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(video1_start_time),
        "-i",
        left_cut_video_path,
        "-vcodec",
        "copy",
        "-acodec",
        "copy",
        synced_left,
    ]
    subprocess_call(cmd)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(video2_start_time),
        "-i",
        right_cut_video_path,
        "-vcodec",
        "copy",
        "-acodec",
        "copy",
        synced_right,
    ]
    subprocess_call(cmd)


def concat_and_sync_videos(
    left_folder, right_folder, start_time=0, end_time=None, prompt=True
):
    function_start_time = time.time()

    current_file = Path(__file__).resolve()
    current_root = current_file.parent
    exe_path = os.path.join(current_root, "build\\Debug\\StitchingApplication.exe")
    output_folder = os.path.join(current_root, "output")
    config_path = os.path.join(current_root, "stitching_config.json")
    debug_image_path = os.path.join(
        current_root, os.path.join(output_folder), "debugOverlapped.jpg"
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        # output_folder = temp_dir
        # os.makedirs(output_folder, exist_ok=True)
        # print(f"Writing files to temporary directory: {output_folder}")

        # # --- Step 1: Concatenate & trim videos ---
        # left_list = os.path.join(output_folder, "left_list.txt")
        # right_list = os.path.join(output_folder, "right_list.txt")

        # create_concat_list(left_folder, left_list)
        # create_concat_list(right_folder, right_list)

        # left_concat = os.path.join(output_folder, "left_concat.mp4")
        # right_concat = os.path.join(output_folder, "right_concat.mp4")
        # left_concat_start_time = time.time()
        # subprocess_call(
        #     [
        #         "ffmpeg",
        #         "-y",
        #         "-f",
        #         "concat",
        #         "-safe",
        #         "0",
        #         "-ss",
        #         str(start_time),
        #         "-t",
        #         str(end_time - start_time),
        #         "-i",
        #         left_list,
        #         "-c",
        #         "copy",
        #         left_concat,
        #     ]
        # )
        # print(
        #     f"✅ Left concat completed in {time.time() - left_concat_start_time} seconds"
        # )
        # right_concat_start_time = time.time()
        # subprocess_call(
        #     [
        #         "ffmpeg",
        #         "-y",
        #         "-f",
        #         "concat",
        #         "-safe",
        #         "0",
        #         "-ss",
        #         str(start_time),
        #         "-t",
        #         str(end_time - start_time),
        #         "-i",
        #         right_list,
        #         "-c",
        #         "copy",
        #         right_concat,
        #     ],
        # )
        # print(
        #     f"✅ Right concat completed in {time.time() - right_concat_start_time} seconds"
        # )
        
        # # # --- Step 2: Calculate delay and sync videos ---
        # delay_start_time = time.time()
        # left_audio_path = os.path.join(output_folder, "left_audio.wav")
        # right_audio_path = os.path.join(output_folder, "right_audio.wav")
        # AudioFileClip(left_concat).write_audiofile(
        #     left_audio_path, ffmpeg_params=["-ac", "1"]
        # )
        # AudioFileClip(right_concat).write_audiofile(
        #     right_audio_path, ffmpeg_params=["-ac", "1"]
        # )
        # left_audio_start_time = time.time()
        # subprocess_call(
        #     [
        #         "ffmpeg",
        #         "-y",
        #         "-ss",
        #         "0",  # start time (0 = beginning)
        #         "-t",
        #         "300",  # duration in seconds (5 minutes = 300 s)
        #         "-i",
        #         left_concat,
        #         "-vn",  # no video
        #         "-ac",
        #         "1",  # mono
        #         left_audio_path,
        #     ]
        # )
        # print(
        #     f"✅ Left autio completed in {time.time() - left_audio_start_time} seconds"
        # )
        # right_audio_start_time = time.time()
        # subprocess_call(
        #     [
        #         "ffmpeg",
        #         "-y",
        #         "-ss",
        #         "0",  # start time (0 = beginning)
        #         "-t",
        #         "300",  # duration in seconds (5 minutes = 300 s)
        #         "-i",
        #         right_concat,
        #         "-vn",  # no video
        #         "-ac",
        #         "1",  # mono
        #         right_audio_path,
        #     ]
        # )
        # print(
        #     f"✅ Right autio completed in {time.time() - right_audio_start_time} seconds"
        # )
        # delay_result = calculate_robust_audio_delay(left_audio_path, right_audio_path)
        # delay_ms = delay_result["delay_ms"]
        # delay_sec = delay_ms / 1000.0

        synced_left = os.path.join(output_folder, "synced_left.mp4")
        synced_right = os.path.join(output_folder, "synced_right.mp4")
        # if delay_sec > 0:
        #     video1_start_time = delay_sec
        #     video2_start_time = 0
        # else:
        #     video1_start_time = 0
        #     video2_start_time = abs(delay_sec)

        # print(f"✅ Delay completed in {time.time() - delay_start_time} seconds")
        # left_sync_start_time = time.time()
        # subprocess_call(
        #     [
        #         "ffmpeg",
        #         "-y",
        #         "-ss",
        #         str(video1_start_time),
        #         "-i",
        #         left_concat,
        #         "-vcodec",
        #         "copy",
        #         "-acodec",
        #         "copy",
        #         synced_left,
        #     ]
        # )
        # print(f"✅ Left sync completed in {time.time() - left_sync_start_time} seconds")
        # right_sync_start_time = time.time()

        # subprocess_call(
        #     [
        #         "ffmpeg",
        #         "-y",
        #         "-ss",
        #         str(video2_start_time),
        #         "-i",
        #         right_concat,
        #         "-vcodec",
        #         "copy",
        #         "-acodec",
        #         "copy",
        #         synced_right,
        #     ]
        # )
        # print(
        #     f"✅ Right sync completed in {time.time() - right_sync_start_time} seconds"
        # )

        # --- Step 3: Write stitching_config.json ---
        config = {
            "mode": "calculate",
            "left_video_path": synced_left,
            "right_video_path": synced_right,
            "output_dir": output_folder,
            "degrees": -7,
            "seam_x": 4625,
            "box_x":1000,
            "box_y": 2000,
            "box_w": 6500,
            "box_h": 2300,
        }
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"[INFO] Config written to {config_path}")

        # --- Step 5: Call C++ StitchingApplication ---
        print("[INFO] Running StitchingApplication.exe...")
        subprocess.run([exe_path, config_path], check=True, cwd=output_folder)
        print("[INFO] Stitching complete.")

        # --- Step 6: Open debugOverlapped.jpg and prompt user ---
        def open_debug_image():
            if os.path.exists(debug_image_path):
                print(f"Opening {debug_image_path}...")
                webbrowser.open(debug_image_path)
            else:
                print("Warning: debugOverlapped.jpg not found!")

        if prompt:
            open_debug_image()

        while True:
            if prompt:
                user_input = (
                    input(
                        '\nEnter "stitch" to finalize, or press any other key to rerun debug: '
                    )
                    .strip()
                    .lower()
                )

                if user_input == "stitch":
                    new_mode = "stitch"
                    print("\n[INFO] User selected STITCH mode.")
                else:
                    new_mode = "debug"
                    print("\n[INFO] User selected DEBUG mode (rerunning).")
            else:
                new_mode = "stitch"

            # --- Update config file mode ---
            with open(config_path, "r") as f:
                config = json.load(f)
            config["mode"] = new_mode
            frame_ranges = config.get("frame_ranges", [])
            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)
            print(f"[INFO] Updated config file with mode='{new_mode}'")

            # --- Rerun StitchingApplication.exe ---
            subprocess.run([exe_path, config_path], check=True, cwd=output_folder)

            if new_mode == "stitch":
                if not frame_ranges:
                    subprocess_call([
            "ffmpeg",
            "-y",
            "-i", r"F:\StitchingApplication\output\stitched_video.mp4",
            "-i", synced_left,
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0",
            r"F:\StitchingApplication\output\stitched_with_audio.mp4",
        ])
                else:
                    apply_audio_from_frame_ranges(
                    source_video=synced_left,
                    stitched_video=r"F:\StitchingApplication\output\stitched_video.mp4",
                    frame_ranges=frame_ranges,
                    output_path=r"F:\StitchingApplication\output\stitched_with_audio.mp4"
                )
                print("\n[INFO] Stitching complete. Exiting pipeline.")
                break
            else:
                print("\n[INFO] Debug rerun complete. Opening updated debug image...")
                open_debug_image()



def apply_audio_from_frame_ranges(
    source_video,
    stitched_video,
    frame_ranges,
    output_path,
    temp_dir=None,
):
    """
    Extracts audio segments from source_video using given frame_ranges
    and applies them to stitched_video to produce output_path.

    Parameters:
        source_video (str): path to the synced input video (e.g., left_synced.mp4)
        stitched_video (str): path to the video (from OpenCV pipeline) without audio
        frame_ranges (list[tuple[int,int]]): list of (start_frame, end_frame)
        output_path (str): final stitched video path with audio
        temp_dir (str|None): optional temp directory to store intermediate clips
    """

    # --- 1. Setup temp directory ---
    cleanup_temp = False
    if temp_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="audio_extract_")
        cleanup_temp = True
    os.makedirs(temp_dir, exist_ok=True)

    # --- 2. Determine FPS ---
    cap = cv2.VideoCapture(source_video)
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    if not fps or fps <= 0:
        raise ValueError("Could not read FPS from video.")

    print(f"[INFO] Using FPS = {fps:.2f}")

    # --- 3. Convert frame ranges to time segments ---
    segments = []
    for start, end in frame_ranges:
        t1 = start / fps
        t2 = end / fps
        if t2 <= t1:
            continue
        segments.append((t1, t2))
    if not segments:
        raise ValueError("No valid frame ranges provided.")

    print(f"[INFO] Processing {len(segments)} audio segments...")

    # --- 4. Extract audio segments ---
    segment_paths = []
    for i, (t1, t2) in enumerate(segments):
        seg_path = os.path.join(temp_dir, f"segment_{i}.wav")
        subprocess_call( [
            "ffmpeg",
            "-y",
            "-ss", f"{t1:.3f}",
            "-to", f"{t2:.3f}",
            "-i", source_video,
            "-vn",
            "-ac", "1",  # mono
            "-ar", "48000",  # 48kHz
            seg_path,
        ])
        segment_paths.append(seg_path)
        print(f"  ↳ Extracted segment {i}: {t1:.2f}s → {t2:.2f}s")

    # --- 5. Concatenate audio segments ---
    concat_list_path = os.path.join(temp_dir, "concat_list.txt")
    with open(concat_list_path, "w") as f:
        for seg in segment_paths:
            f.write(f"file '{seg.replace('\\', '/')}'\n")

    concatenated_audio = os.path.join(temp_dir, "stitched_audio.wav")
    subprocess_call([
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        concatenated_audio,
    ])
    print(f"[INFO] Concatenated audio saved to {concatenated_audio}")

    # --- 6. Merge concatenated audio with stitched video ---
    subprocess_call([
        "ffmpeg",
        "-y",
        "-i", stitched_video,
        "-i", concatenated_audio,
        "-c:v", "copy",
        "-c:a", "aac",
        "-map", "0:v:0",
        "-map", "1:a:0",
        output_path,
    ])
    print(f"[INFO] Final video with audio written to {output_path}")

    # --- 7. Cleanup ---
    if cleanup_temp:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        print("[INFO] Temporary files cleaned up.")


# syncVideos(
#     r"F:\StitchingApplication\output\left_full.mp4",
#     r"F:\StitchingApplication\output\right_full.mp4",
#     start=60,
#     end=1064,
# )

# apply_audio_from_frame_ranges(
#     source_video=r"F:\StitchingApplication\output\synced_left.mp4",
#     stitched_video=r"F:\StitchingApplication\output\stitched_video.mp4",
#     frame_ranges=[
#         [1541, 2338],
#         [4892, 10007],
#         [12715, 14271],
#         [16751, 18635],
#         [21325, 22615],
#         [25319, 30820],
#         [33213, 34471],
#     ],
#     output_path=r"F:\StitchingApplication\output\stitched_with_audio.mp4"
# )

concat_and_sync_videos(
    r"F:\StitchingApplication\input\left",
    r"F:\StitchingApplication\input\right",
    60,
    1656,
    prompt=False,
)
# concat_and_sync_videos(
#     r"C:\Users\brown\Desktop\stitching_application\test_left",
#     r"C:\Users\brown\Desktop\stitching_application\test_right",
#     prompt=True,
# )
