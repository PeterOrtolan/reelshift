import os
import json
import random


DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
POOL_PATH = os.path.join(DATA_DIR, "configs", "captions.txt")
STATE_PATH = os.path.join(DATA_DIR, "state", "used_captions.json")


def _load_pool():
    if not os.path.exists(POOL_PATH):
        return []
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def _load_used():
    if not os.path.exists(STATE_PATH):
        return []
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []


def _save_used(used):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(used, f)


def pick_captions(n):
    pool = _load_pool()
    if not pool:
        raise ValueError("Caption pool is empty. Add captions with /igcaption.")

    used = [i for i in _load_used() if i < len(pool)]
    captions = []

    for _ in range(n):
        unused = [i for i in range(len(pool)) if i not in used]
        if not unused:
            print(f"[captions] Full cycle complete, resetting pool")
            used = []
            unused = list(range(len(pool)))

        chosen = random.choice(unused)
        used.append(chosen)
        captions.append(pool[chosen])
        print(f"[captions] Picked #{chosen} (used {len(used)}/{len(pool)})")

    _save_used(used)
    return captions


def pool_status():
    pool = _load_pool()
    used = [i for i in _load_used() if i < len(pool)]
    return {
        "total": len(pool),
        "used": len(used),
        "remaining": len(pool) - len(used),
    }


def add_caption(text):
    text = text.strip()
    if not text:
        raise ValueError("Caption is empty")

    os.makedirs(os.path.dirname(POOL_PATH), exist_ok=True)

    needs_newline = False
    if os.path.exists(POOL_PATH) and os.path.getsize(POOL_PATH) > 0:
        with open(POOL_PATH, "rb") as f:
            f.seek(-1, 2)
            needs_newline = f.read(1) != b"\n"

    with open(POOL_PATH, "a", encoding="utf-8") as f:
        if needs_newline:
            f.write("\n")
        f.write(text + "\n")

    return len(_load_pool()) - 1


def list_captions():
    pool = _load_pool()
    used = set(_load_used())
    return [(i, t, i in used) for i, t in enumerate(pool)]


def remove_caption(index):
    pool = _load_pool()
    if index < 0 or index >= len(pool):
        raise IndexError(f"Caption index {index} out of range (0..{len(pool) - 1})")

    removed = pool.pop(index)
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        for line in pool:
            f.write(line + "\n")

    used = _load_used()
    new_used = []
    for i in used:
        if i == index:
            continue
        if i > index:
            new_used.append(i - 1)
        else:
            new_used.append(i)
    _save_used(new_used)

    return removed


def reset_used():
    _save_used([])
