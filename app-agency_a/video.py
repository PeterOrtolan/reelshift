import os
import subprocess
import uuid


def process_video(input_path, output_dir="."):
    """
    Re-encode a video to be Instagram Reels compliant and strip all metadata.

    - Strips ALL metadata (no EXIF, no GPS, no creation tools, etc.)
    - H.264 video, AAC audio (IG's required codecs)
    - Max bitrate suitable for Reels
    - Adds 'faststart' so the video starts playing immediately
    - Output is a fresh file with a UUID name (no link to original filename)
    """
    output_filename = f"{uuid.uuid4().hex}.mp4"
    output_path = os.path.join(output_dir, output_filename)

    cmd = [
        "ffmpeg",
        "-y",                                  # overwrite output if exists
        "-i", input_path,                      # input
        "-map", "0:v:0",                       # only first video stream (skip spatial audio etc.)
        "-map", "0:a:0?",                      # first audio stream if present
        "-map_metadata", "-1",                 # strip ALL metadata
        "-map_chapters", "-1",                 # strip chapter markers
        "-vf", "format=yuv420p,scale='if(gt(ih,1920),-2,iw)':'if(gt(ih,1920),1920,ih)'",  # 8-bit + downscale 4K
        "-c:v", "libx264",                     # H.264 video codec
        "-preset", "medium",                   # encoding speed/quality balance
        "-crf", "23",                          # quality (lower=better, 18-28 range)
        "-pix_fmt", "yuv420p",                 # IG-required pixel format
        "-c:a", "aac",                         # AAC audio codec
        "-b:a", "128k",                        # audio bitrate
        "-ar", "44100",                        # audio sample rate
        "-movflags", "+faststart",             # web-optimized: moov atom at front
        "-fflags", "+bitexact",                # deterministic, no timestamps
        "-flags:v", "+bitexact",
        "-flags:a", "+bitexact",
        output_path,
    ]

    print(f"  ffmpeg: processing {input_path} -> {output_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  ffmpeg FAILED:\n{result.stderr}")
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError("ffmpeg processing failed")

    size_in = os.path.getsize(input_path)
    size_out = os.path.getsize(output_path)
    print(f"  ffmpeg OK: {size_in:,} bytes -> {size_out:,} bytes")
    return output_path


def get_video_info(path):
    """Quick info dump for debugging — uses ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout