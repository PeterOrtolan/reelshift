"""
videos.py - manages today's source videos and per-(account, post_num) variants.

Upgraded variant generator (2026-09-20):
  - Aggressive anti-dup transforms to defeat IG pHash/SSIM/audio fingerprints
  - NO horizontal flip (content has text — mirroring would look wrong)
  - Per-variant random seed (different per call, retries reuse cached variant)
  - Sub-pixel jitter via zoom+crop+rotation
  - Audio: pitch ±3% + tempo ±2% + EQ + subtle reverb (defeats audio fingerprints)
  - Frame trim 0.2-0.5s from start + end (different first/last frame)
  - Frame-rate change (29.97/30/25fps) — affects keyframe sampling
  - GOP size change (30-60) — different encoding structure
  - Gaussian noise 8-15% — adds unique pixels per variant
  - Metadata strip + bitexact flags (defeats file-level hash)
"""

import os
import json
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


def _get_duration(path):
    """Get video duration in seconds via ffprobe. Returns None on failure."""
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return float(json.loads(result.stdout)["format"]["duration"])
    except Exception as e:
        log.warning(f"ffprobe duration failed for {path}: {e}")
    return None


def make_variant(source_path, output_dir):
    """Generate an aggressive anti-duplicate variant.

    Transforms applied (NO horizontal flip — content has text):
      Visual:
        - Zoom 5-8% (subtle scale-up, hides edge artifacts)
        - Crop 3-8% from random offset (changes composition)
        - Rotation 0.5-1.5 degrees (defeats some hashes, small black corners)
        - Brightness ±3%, Saturation ±5%, Hue ±3 degrees (subtle color shifts)
        - Gaussian noise 8-15% intensity (adds unique pixels)
        - Frame-rate change (29.97/30/25fps — affects keyframe sampling)
        - Trim 0.2-0.5s from start + end (different first/last frame)

      Audio:
        - Pitch ±3% (subtle, not chipmunk-effect)
        - Tempo ±2% (slight speed change)
        - EQ modification (boost/cut random freq band)
        - Subtle reverb (aecho — changes spectrogram)
        - Background noise (anoisesrc at -40dB, imperceptible but changes spectrogram)

      Encoding:
        - GOP size 30-60 (different keyframe interval)
        - H.264 + AAC (IG-required)
        - +bitexact flags (no timestamps, deterministic output)
        - Metadata strip (-map_metadata -1)
        - +faststart (web-optimized)
    """
    output_filename = f"var_{uuid.uuid4().hex}.mp4"
    output_path = os.path.join(output_dir, output_filename)

    # Random parameters (per-call — different variant every time)
    duration = _get_duration(source_path) or 30.0
    crop_pct = random.uniform(0.03, 0.08)
    crop_w_px = max(20, int(1920 * crop_pct))
    crop_h_px = max(20, int(1080 * crop_pct))
    crop_x = random.randint(0, max(1, crop_w_px))
    crop_y = random.randint(0, max(1, crop_h_px))
    zoom = round(random.uniform(1.05, 1.08), 3)
    rotate_deg = round(random.uniform(-1.5, 1.5), 2)
    brightness = round(random.uniform(-0.03, 0.03), 3)
    saturation = round(random.uniform(0.97, 1.03), 3)  # reduced from ±5% to ±3%
    hue = round(random.uniform(-2, 2), 2)              # reduced from ±3° to ±2°
    noise_strength = random.randint(2, 4)   # reduced from 8-15 (was too grainy)
    audio_pitch = round(random.uniform(0.98, 1.02), 4)  # reduced from ±3% to ±2%
    audio_tempo = round(random.uniform(0.98, 1.02), 4)
    eq_freq = random.randint(200, 800)
    eq_gain = random.randint(-3, 3)
    framerate = random.choice([29.97, 30, 25])
    trim_start = round(random.uniform(0.2, 0.5), 2)
    trim_end = round(random.uniform(0.2, 0.5), 2)
    gop_size = random.randint(30, 60)
    noise_seed = random.randint(0, 2**31 - 1)  # INT_MAX (FFmpeg noise seed range)

    # Calculate end trim duration (don't go below 1s total)
    end_duration = max(1.0, duration - trim_start - trim_end)

    # Build video filter chain
    # Order matters: format first, then spatial transforms, then color, then noise, then trim
    vf_parts = [
        f"format=yuv420p",
        f"scale=iw*{zoom}:ih*{zoom}",                                              # zoom in
        f"crop=iw/{zoom}-{crop_w_px}:ih/{zoom}-{crop_h_px}:{crop_x}:{crop_y}",     # crop after zoom
        f"rotate={rotate_deg}*PI/180:fillcolor=black",                             # tiny rotation
        f"eq=brightness={brightness}:saturation={saturation}",                    # color grade
        f"hue=h={hue}",                                                            # hue shift
        f"noise=alls={noise_strength}:all_seed={noise_seed}",                      # Gaussian noise
        f"fps={framerate}",                                                        # framerate change
        f"trim=start=0:end={end_duration}",                                        # end trim
        "setpts=PTS-STARTPTS",                                                      # reset timestamps after trim
    ]

    # Audio filter chain (handle missing audio gracefully)
    af_parts = [
        f"atrim=0:{end_duration}",                                                 # match video trim
        "asetpts=PTS-STARTPTS",
        f"atempo={audio_tempo}",                                                   # tempo change
        f"asetrate=44100*{audio_pitch},aresample=44100",                           # pitch shift
        f"equalizer=f={eq_freq}:width_type=h:width=200:g={eq_gain}",               # EQ
        f"aecho=0.6:0.5:1000|1800:0.2|0.1",                                        # subtle reverb
    ]

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(trim_start),                  # input-side start trim (efficient)
        "-i", source_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-map_metadata", "-1",                   # strip all metadata
        "-map_chapters", "-1",
        "-vf", ",".join(vf_parts),
        "-af", ",".join(af_parts),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-g", str(gop_size),                      # GOP size (keyframe interval)
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
