"""
videos.py - manages today's source videos and per-(account, post_num) variants.
"""

import os
import uuid
import random
import shutil
import subprocess
import logging

from r2 import upload_file

log = logging.getLogger("videos")

DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
SOURCES_DIR = os.path.join(DATA_DIR, "state", "sources")
os.makedirs(SOURCES_DIR, exist_ok=True)


def add_source_video(local_input_path):
    if not os.path.exists(local_input_path):
        raise FileNotFoundError(local_input_path)

    new_name = f"src_{uuid.uuid4().hex}.mp4"
    new_path = os.path.join(SOURCES_DIR, new_name)
    shutil.move(local_input_path, new_path)
    return new_path


def list_sources():
    if not os.path.isdir(SOURCES_DIR):
        return []
    return [
        os.path.join(SOURCES_DIR, f)
        for f in sorted(os.listdir(SOURCES_DIR))
        if f.endswith(".mp4")
    ]


def clear_sources():
    if not os.path.isdir(SOURCES_DIR):
        return
    for f in os.listdir(SOURCES_DIR):
        try:
            os.remove(os.path.join(SOURCES_DIR, f))
        except Exception as e:
            log.warning(f"Could not remove source {f}: {e}")


def assign_videos(num_accounts, num_videos, ppa):
    assignments = []
    for account_idx in range(num_accounts):
        for post_num in range(1, ppa + 1):
            video_idx = (account_idx + post_num - 1) % num_videos
            assignments.append((account_idx, post_num, video_idx))
    return assignments


def make_variant(source_path, output_dir):
    output_filename = f"var_{uuid.uuid4().hex}.mp4"
    output_path = os.path.join(output_dir, output_filename)

    crop_x = random.randint(0, 4)
    crop_y = random.randint(0, 4)
    crop_w = random.randint(2, 6)
    crop_h = random.randint(2, 6)
    speed = round(random.uniform(0.985, 1.015), 4)
    brightness = round(random.uniform(-0.02, 0.02), 4)
    saturation = round(random.uniform(0.95, 1.05), 4)
    hue_shift = round(random.uniform(-2, 2), 2)
    audio_pitch = round(random.uniform(0.995, 1.005), 4)

    vf = (
        f"format=yuv420p,"
        f"scale='if(gt(ih,1920),-2,iw)':'if(gt(ih,1920),1920,ih)',"
        f"crop=in_w-{crop_x + crop_w}:in_h-{crop_y + crop_h}:{crop_x}:{crop_y},"
        f"setpts={1/speed}*PTS,"
        f"eq=brightness={brightness}:saturation={saturation},"
        f"hue=h={hue_shift}"
    )
    af = f"atempo={speed},asetrate=44100*{audio_pitch},aresample=44100"

    cmd = [
        "ffmpeg", "-y",
        "-i", source_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart",
        "-fflags", "+bitexact",
        "-flags:v", "+bitexact",
        "-flags:a", "+bitexact",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"ffmpeg variant FAILED for {source_path}:\n{result.stderr[-2000:]}")
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError("Variant generation failed")

    return output_path


def make_variant_and_upload(source_path, output_dir):
    variant_path = make_variant(source_path, output_dir)
    try:
        url, key = upload_file(variant_path, content_type="video/mp4")
        return url, key
    finally:
        try:
            os.remove(variant_path)
        except Exception:
            pass


def upload_original_only(source_path, output_dir=None):
    """
    Elvie fork: skip ffmpeg, upload original file directly to R2.
    Signature matches make_variant_and_upload so bot_worker can swap them.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(source_path)
    url, key = upload_file(source_path, content_type="video/mp4")
    log.info("upload_original_only: %s -> %s", os.path.basename(source_path), key)
    return url, key
