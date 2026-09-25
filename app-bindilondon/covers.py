import os
import json
import random
import shutil
import uuid


DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
POOL_DIR = os.path.join(DATA_DIR, "configs", "covers")
STATE_PATH = os.path.join(DATA_DIR, "state", "used_covers.json")

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _load_pool():
    """Return sorted list of cover filenames (basenames) currently in the pool dir."""
    if not os.path.isdir(POOL_DIR):
        return []
    return sorted(
        f for f in os.listdir(POOL_DIR)
        if os.path.splitext(f)[1].lower() in ALLOWED_EXTS
        and os.path.isfile(os.path.join(POOL_DIR, f))
    )


def _load_used():
    if not os.path.exists(STATE_PATH):
        return []
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _save_used(used):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(used, f)


def pick_cover():
    """Pick a random unused cover filename. Wraps around when the pool is exhausted.
    Returns the absolute path to the cover file, or None if the pool is empty."""
    pool = _load_pool()
    if not pool:
        return None

    used = [name for name in _load_used() if name in pool]
    unused = [name for name in pool if name not in used]

    if not unused:
        print(f"[covers] Full cycle complete, resetting pool")
        used = []
        unused = pool[:]

    chosen = random.choice(unused)
    used.append(chosen)
    _save_used(used)
    print(f"[covers] Picked {chosen} (used {len(used)}/{len(pool)})")
    return os.path.join(POOL_DIR, chosen)


def pool_status():
    pool = _load_pool()
    used = [name for name in _load_used() if name in pool]
    return {
        "total": len(pool),
        "used": len(used),
        "remaining": len(pool) - len(used),
    }


def add_cover(src_path, original_ext=None):
    """Copy an image file into the covers pool with a unique name.
    src_path: absolute path to a temp file (e.g. what Telegram just downloaded).
    original_ext: e.g. '.jpg' — used to pick the stored extension when src has none.
    Returns the stored filename."""
    ext = (os.path.splitext(src_path)[1] or original_ext or "").lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"Unsupported image extension: {ext!r} (allowed: {sorted(ALLOWED_EXTS)})")

    os.makedirs(POOL_DIR, exist_ok=True)
    name = f"cover_{uuid.uuid4().hex[:12]}{ext}"
    dst = os.path.join(POOL_DIR, name)
    shutil.copyfile(src_path, dst)
    return name


def list_covers():
    pool = _load_pool()
    used = set(_load_used())
    return [(name, name in used) for name in pool]


def remove_cover(name):
    path = os.path.join(POOL_DIR, name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Cover not found: {name}")
    os.remove(path)

    used = _load_used()
    _save_used([n for n in used if n != name])
    return name


def reset_used():
    _save_used([])
