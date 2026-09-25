"""Persistent local library of scraped Instagram content.

Structure:
  state/library/
    manifest.json          - shortcode -> {type, path (relative), caption, likes, ts, ...}
    reels/<shortcode>.mp4  - downloaded video files
    pics/<shortcode>.jpg   - downloaded image files

Once populated, campaigns pull from here instead of hitting HikerAPI.
"""
import os
import json
import random
import shutil
import logging
from threading import Lock

log = logging.getLogger("library")

DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
LIB_DIR = os.path.join(DATA_DIR, "state", "library")
REELS_DIR = os.path.join(LIB_DIR, "reels")
PICS_DIR = os.path.join(LIB_DIR, "pics")
MANIFEST_PATH = os.path.join(LIB_DIR, "manifest.json")

_lock = Lock()


def _ensure_dirs():
    os.makedirs(REELS_DIR, exist_ok=True)
    os.makedirs(PICS_DIR, exist_ok=True)


def load_manifest():
    if not os.path.exists(MANIFEST_PATH):
        return {}
    try:
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_manifest(m):
    _ensure_dirs()
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=2, sort_keys=True)
    os.replace(tmp, MANIFEST_PATH)


def has(shortcode):
    m = load_manifest()
    return shortcode in m


def _safe_name(shortcode):
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in shortcode)


def add_reel_from_local_path(candidate, tmp_video_path):
    """Move a downloaded mp4 into the library under reels/<shortcode>.mp4."""
    sc = candidate.get("ig_shortcode")
    if not sc:
        raise ValueError("candidate missing ig_shortcode")
    _ensure_dirs()
    name = _safe_name(sc) + ".mp4"
    dest = os.path.join(REELS_DIR, name)
    shutil.move(tmp_video_path, dest)
    with _lock:
        m = load_manifest()
        m[sc] = {
            "type": "reel",
            "path": os.path.relpath(dest, LIB_DIR),
            "video_url": candidate.get("video_url"),
            "cover_url": candidate.get("cover_url"),
            "caption": candidate.get("caption"),
            "like_count": candidate.get("like_count"),
            "comment_count": candidate.get("comment_count"),
            "play_count": candidate.get("play_count"),
        }
        save_manifest(m)
    return dest


def add_pic_from_local_path(candidate, tmp_image_path):
    sc = candidate.get("ig_shortcode")
    if not sc:
        raise ValueError("candidate missing ig_shortcode")
    _ensure_dirs()
    name = _safe_name(sc) + ".jpg"
    dest = os.path.join(PICS_DIR, name)
    shutil.move(tmp_image_path, dest)
    with _lock:
        m = load_manifest()
        m[sc] = {
            "type": "pic",
            "path": os.path.relpath(dest, LIB_DIR),
            "image_url": candidate.get("image_url"),
            "cover_url": candidate.get("cover_url"),
            "caption": candidate.get("caption"),
            "like_count": candidate.get("like_count"),
            "comment_count": candidate.get("comment_count"),
        }
        save_manifest(m)
    return dest


def list_reels(exclude_shortcodes=None):
    excl = set(exclude_shortcodes or [])
    m = load_manifest()
    out = []
    for sc, entry in m.items():
        if entry.get("type") != "reel" or sc in excl:
            continue
        abs_path = os.path.join(LIB_DIR, entry["path"])
        if not os.path.exists(abs_path):
            continue
        out.append({**entry, "ig_shortcode": sc, "abs_path": abs_path})
    return out


def list_pics(exclude_shortcodes=None):
    excl = set(exclude_shortcodes or [])
    m = load_manifest()
    out = []
    for sc, entry in m.items():
        if entry.get("type") != "pic" or sc in excl:
            continue
        abs_path = os.path.join(LIB_DIR, entry["path"])
        if not os.path.exists(abs_path):
            continue
        out.append({**entry, "ig_shortcode": sc, "abs_path": abs_path})
    return out


def pick_reels(n, exclude_shortcodes=None):
    pool = list_reels(exclude_shortcodes)
    random.shuffle(pool)
    return pool[:int(n)]


def pick_pics(n, exclude_shortcodes=None):
    pool = list_pics(exclude_shortcodes)
    random.shuffle(pool)
    return pool[:int(n)]


def stats():
    m = load_manifest()
    reels = [k for k, v in m.items() if v.get("type") == "reel"]
    pics = [k for k, v in m.items() if v.get("type") == "pic"]
    size_bytes = 0
    for entry in m.values():
        p = os.path.join(LIB_DIR, entry["path"])
        try:
            size_bytes += os.path.getsize(p)
        except OSError:
            pass
    return {
        "reels": len(reels),
        "pics": len(pics),
        "total": len(m),
        "size_mb": round(size_bytes / (1024 * 1024), 1),
    }
