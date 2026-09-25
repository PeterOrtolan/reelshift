"""Scrape targets: hashtags + creator handles to feed the Apify scraper.

Stored as JSON at configs/scrape_targets.json:
  {"hashtags": ["viraltag", ...], "creators": ["username", ...]}
"""
import os
import json
from threading import Lock

DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
PATH = os.path.join(DATA_DIR, "configs", "scrape_targets.json")

_lock = Lock()


def _load():
    if not os.path.exists(PATH):
        return {"hashtags": [], "creators": []}
    try:
        with open(PATH) as f:
            data = json.load(f)
            data.setdefault("hashtags", [])
            data.setdefault("creators", [])
            return data
    except (json.JSONDecodeError, ValueError):
        return {"hashtags": [], "creators": []}


def _save(data):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PATH)


def _norm_hashtag(h):
    return h.strip().lstrip("#").lower()


def _norm_creator(c):
    return c.strip().lstrip("@").lower()


def add_hashtag(tag):
    tag = _norm_hashtag(tag)
    if not tag:
        raise ValueError("Empty hashtag")
    with _lock:
        d = _load()
        if tag in d["hashtags"]:
            return False
        d["hashtags"].append(tag)
        _save(d)
        return True


def add_creator(handle):
    handle = _norm_creator(handle)
    if not handle:
        raise ValueError("Empty creator handle")
    with _lock:
        d = _load()
        if handle in d["creators"]:
            return False
        d["creators"].append(handle)
        _save(d)
        return True


def remove(name):
    """Remove by name from either list. Returns 'hashtag' | 'creator' | None."""
    key_hash = _norm_hashtag(name)
    key_creator = _norm_creator(name)
    with _lock:
        d = _load()
        if key_hash in d["hashtags"]:
            d["hashtags"].remove(key_hash)
            _save(d)
            return "hashtag"
        if key_creator in d["creators"]:
            d["creators"].remove(key_creator)
            _save(d)
            return "creator"
        return None


def list_all():
    return _load()
