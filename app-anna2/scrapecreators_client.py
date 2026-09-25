import os
import logging
import requests

log = logging.getLogger("scrapecreators")

SC_API_KEY = os.environ.get("SCRAPECREATORS_API_KEY", "")
SC_BASE = "https://api.scrapecreators.com"


class ScrapeCreatorsError(RuntimeError):
    pass


def _safe_int(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try: return int(v.replace(',', ''))
        except ValueError: return None
    return None


def _normalize(post, source_ref):
    video_url = post.get("video_url")
    if not video_url:
        return None
    return {
        "ig_shortcode":  post.get("shortcode") or post.get("id"),
        "source_type":   "hashtag",
        "source_ref":    source_ref,
        "video_url":     video_url,
        "cover_url":     post.get("thumbnail_src") or post.get("display_url"),
        "caption":       (post.get("caption") or "")[:2000],
        "like_count":    _safe_int(post.get("like_count") or post.get("likes")),
        "comment_count": _safe_int(post.get("comment_count") or post.get("comments")),
        "play_count":    _safe_int(post.get("play_count") or post.get("video_view_count")),
        "posted_at":     None,
    }


def scrape_hashtag(tag, max_items=10):
    if not SC_API_KEY:
        raise ScrapeCreatorsError("SCRAPECREATORS_API_KEY not configured")
    tag = tag.lstrip("#").lower()
    r = requests.get(
        f"{SC_BASE}/v1/instagram/search/hashtag",
        params={"hashtag": tag},
        headers={"x-api-key": SC_API_KEY},
        timeout=60,
    )
    if r.status_code >= 400:
        raise ScrapeCreatorsError(f"[{r.status_code}] {r.text[:300]}")
    try:
        data = r.json()
    except ValueError:
        raise ScrapeCreatorsError(f"non-JSON: {r.text[:200]}")
    posts = data.get("posts") or []
    reels = [_normalize(p, f"#{tag}") for p in posts]
    reels = [c for c in reels if c]
    log.info(f"scrapecreators #{tag}: {len(posts)} posts, {len(reels)} reels (credits_remaining={data.get('credits_remaining')})")
    return reels[:int(max_items)]
