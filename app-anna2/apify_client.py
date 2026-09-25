"""Apify wrappers for Instagram Reels discovery.

- Creator reels: apify~instagram-reel-scraper (clean, one call = reels only)
- Hashtag reels: apify~instagram-hashtag-scraper with over-fetch + strict filter
  (the actor returns mixed feed content; we keep only productType=clips items,
  so we fetch max_items * OVERFETCH raw to end up with roughly max_items reels)
"""
import os
import logging
import requests

log = logging.getLogger("apify")

APIFY_TOKEN         = os.environ.get("APIFY_TOKEN", "")
APIFY_ACTOR_REELS   = os.environ.get("APIFY_ACTOR_REELS",   "apify~instagram-reel-scraper")
APIFY_ACTOR_HASHTAG = os.environ.get("APIFY_ACTOR_HASHTAG", "apify~instagram-hashtag-scraper")
APIFY_BASE = "https://api.apify.com/v2"

HASHTAG_OVERFETCH = int(os.environ.get("APIFY_HASHTAG_OVERFETCH", "10"))
HASHTAG_MAX_FETCH = int(os.environ.get("APIFY_HASHTAG_MAX_FETCH", "100"))


class ApifyError(RuntimeError):
    pass


def _run_sync(actor, actor_input, timeout=300):
    if not APIFY_TOKEN:
        raise ApifyError("APIFY_TOKEN not configured (set in .env)")
    url = f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items"
    log.info(f"Apify run: actor={actor} input={actor_input}")
    r = requests.post(
        url,
        params={"token": APIFY_TOKEN, "timeout": timeout, "format": "json"},
        json=actor_input,
        timeout=timeout + 15,
    )
    if r.status_code >= 400:
        raise ApifyError(f"Apify run [{actor}] failed [{r.status_code}]: {r.text[:500]}")
    try:
        return r.json()
    except ValueError:
        raise ApifyError(f"Apify [{actor}] returned non-JSON: {r.text[:200]}")


def _video_url(item):
    for k in ("videoUrl", "video_url", "mediaUrl", "media_url"):
        v = item.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    urls = item.get("videoUrls") or item.get("video_urls")
    if isinstance(urls, list) and urls:
        first = urls[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict) and first.get("url"):
            return first["url"]
    return None


def _is_strict_reel(item):
    """A REAL reel — top-level video with productType=clips or type=Video and a videoUrl."""
    if not _video_url(item):
        return None if item.get("childPosts") else False
    pt = (item.get("productType") or "").lower()
    t  = (item.get("type") or "").lower()
    return pt == "clips" or t in ("video", "reel", "reels")


def _first_video_url(item):
    """Kept for compatibility with older callers/tests."""
    u = _video_url(item)
    if u:
        return u
    for child in item.get("childPosts", []) or []:
        u = _video_url(child)
        if u:
            return u
    return None


def _normalize(item, source_type, source_ref):
    if not _is_strict_reel(item):
        return None
    video_url = _video_url(item)
    if not video_url:
        return None

    caption = item.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")

    return {
        "ig_shortcode":  item.get("shortCode") or item.get("shortcode") or item.get("code") or item.get("id"),
        "source_type":   source_type,
        "source_ref":    source_ref,
        "video_url":     video_url,
        "cover_url":     item.get("displayUrl") or item.get("thumbnailUrl") or item.get("thumbnail_url"),
        "caption":       (caption or "")[:2000],
        "like_count":    item.get("likesCount") or item.get("likes_count"),
        "comment_count": item.get("commentsCount") or item.get("comments_count"),
        "play_count":    item.get("videoPlayCount") or item.get("videoViewCount") or item.get("play_count"),
        "posted_at":     item.get("timestamp") or item.get("taken_at"),
    }


def scrape_hashtag(tag, max_items=10):
    """Over-fetch mixed hashtag posts, keep only real reels."""
    tag = tag.lstrip("#")
    fetch_n = min(int(max_items) * HASHTAG_OVERFETCH, HASHTAG_MAX_FETCH)
    items = _run_sync(APIFY_ACTOR_HASHTAG, {
        "hashtags":     [tag],
        "resultsLimit": fetch_n,
    })
    normalized = [_normalize(i, "hashtag", f"#{tag}") for i in items]
    reels = [c for c in normalized if c]
    log.info(f"scrape_hashtag(#{tag}): raw={len(items)} reels={len(reels)} kept={min(len(reels), max_items)}")
    return reels[:int(max_items)]


def scrape_creator(handle, max_items=10):
    handle = handle.lstrip("@")
    items = _run_sync(APIFY_ACTOR_REELS, {
        "username":     [handle],
        "usernames":    [handle],
        "resultsLimit": int(max_items),
    })
    normalized = [_normalize(i, "creator", f"@{handle}") for i in items]
    reels = [c for c in normalized if c]
    log.info(f"scrape_creator(@{handle}): raw={len(items)} reels={len(reels)}")
    return reels
