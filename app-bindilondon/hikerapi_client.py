"""HikerAPI wrapper for Instagram profile scraping (auth-backed, sees age-gated).

Uses the v1 endpoints (flat JSON, cleaner than v2).
Two entry points:
  scrape_profile_reels(handle, max_items) - reels only via /v1/user/clips
  scrape_profile_pics(handle, max_items)  - non-video posts via /v1/user/medias

Caches username -> user_id lookup in-process for the session.
"""
import os
import logging
import requests

log = logging.getLogger("hikerapi")

HK_KEY = os.environ.get("HIKERAPI_KEY", "")
HK_BASE = "https://api.hikerapi.com"

_uid_cache = {}


class HikerAPIError(RuntimeError):
    pass


def _safe_int(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try: return int(v.replace(",", ""))
        except ValueError: return None
    return None


def _request(path, params=None, timeout=45):
    if not HK_KEY:
        raise HikerAPIError("HIKERAPI_KEY not configured")
    r = requests.get(
        f"{HK_BASE}/{path.lstrip('/')}",
        params=params or {},
        headers={"accept": "application/json", "x-access-key": HK_KEY},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise HikerAPIError(f"[{r.status_code}] {path}: {r.text[:300]}")
    try:
        return r.json()
    except ValueError:
        raise HikerAPIError(f"non-JSON from {path}: {r.text[:200]}")


def resolve_user_id(handle):
    handle = handle.lstrip("@").lower()
    if handle in _uid_cache:
        return _uid_cache[handle]
    data = _request("v2/user/by/username", {"username": handle})
    user = data.get("user") or data
    uid = user.get("pk") or user.get("id")
    if not uid:
        raise HikerAPIError(f"Could not resolve user_id for @{handle}: {data}")
    _uid_cache[handle] = int(uid)
    return int(uid)


def _pick_video_url(media):
    """Pick the highest-quality video_versions entry."""
    vv = media.get("video_versions") or []
    if not vv:
        return None
    try:
        best = max(vv, key=lambda v: v.get("width", 0) * v.get("height", 0))
        return best.get("url")
    except Exception:
        return vv[0].get("url")


def _pick_image_url(media):
    """Pick the highest-quality image URL. Handles HikerAPI flat image_versions list."""
    iv = media.get("image_versions")
    if isinstance(iv, list) and iv:
        try:
            best = max(iv, key=lambda v: v.get("width", 0) * v.get("height", 0))
            return best.get("url")
        except Exception:
            first = iv[0]
            return first.get("url") if isinstance(first, dict) else None
    ivs = media.get("image_versions2") or {}
    candidates = ivs.get("candidates") or []
    if candidates:
        try:
            best = max(candidates, key=lambda v: v.get("width", 0) * v.get("height", 0))
            return best.get("url")
        except Exception:
            return candidates[0].get("url")
    for k in ("thumbnail_url", "display_url"):
        v = media.get(k)
        if v:
            return v
    return None


def _extract_caption(media):
    c = media.get("caption_text")
    if isinstance(c, str) and c:
        return c
    c = media.get("caption")
    if isinstance(c, dict):
        return c.get("text") or ""
    if isinstance(c, str):
        return c
    return ""


def _reel_from_media(media, source_ref):
    if media.get("media_type") != 2 and (media.get("product_type") or "").lower() != "clips":
        return None
    video_url = _pick_video_url(media)
    if not video_url:
        return None
    return {
        "ig_shortcode":  media.get("code") or str(media.get("pk") or media.get("id")),
        "source_type":   "creator",
        "source_ref":    source_ref,
        "video_url":     video_url,
        "cover_url":     _pick_image_url(media),
        "caption":       (_extract_caption(media) or "")[:2000],
        "like_count":    _safe_int(media.get("like_count")),
        "comment_count": _safe_int(media.get("comment_count")),
        "play_count":    _safe_int(media.get("play_count") or media.get("view_count") or media.get("video_play_count")),
        "posted_at":     media.get("taken_at"),
    }


def _pic_from_media(media, source_ref):
    """A pic: media_type=1 (image) OR media_type=8 (carousel with first item image)."""
    mt = media.get("media_type")
    if mt == 2 or (media.get("product_type") or "").lower() == "clips":
        return None  # skip reels/videos here
    # For carousel: use first image child
    if mt == 8:
        children = media.get("resources") or media.get("carousel_media") or media.get("children") or []
        if children:
            first = children[0]
            image_url = _pick_image_url(first)
        else:
            image_url = _pick_image_url(media)
    else:
        image_url = _pick_image_url(media)
    if not image_url:
        return None
    return {
        "ig_shortcode":  media.get("code") or str(media.get("pk") or media.get("id")),
        "source_type":   "creator",
        "source_ref":    source_ref,
        "image_url":     image_url,
        "cover_url":     image_url,
        "caption":       (_extract_caption(media) or "")[:2000],
        "like_count":    _safe_int(media.get("like_count")),
        "comment_count": _safe_int(media.get("comment_count")),
        "play_count":    None,
        "posted_at":     media.get("taken_at"),
    }


def _paginated_items(endpoint, uid, max_items):
    """Loop through HikerAPI pages until we have max_items or no more_available.
    Dedups by media code/shortcode to avoid overlapping-page duplicates."""
    collected = []
    seen_codes = set()
    max_id = None
    pages = 0
    max_pages = 30
    while len(collected) < max_items and pages < max_pages:
        params = {"user_id": uid, "count": 12}
        if max_id:
            params["max_id"] = max_id
        raw = _request(endpoint, params)
        r = raw.get("response") if isinstance(raw, dict) else None
        if isinstance(raw, list):
            items = raw
            more = False
            next_id = None
        elif isinstance(r, dict):
            items = r.get("items") or []
            more = r.get("more_available", False)
            next_id = r.get("next_max_id")
            # Some endpoints (e.g. /v2/user/clips) put pagination under paging_info
            pi = r.get("paging_info") or {}
            if isinstance(pi, dict):
                if not next_id:
                    next_id = pi.get("max_id") or pi.get("next_max_id")
                if not more:
                    more = pi.get("more_available", False)
        else:
            items = raw.get("items") or [] if isinstance(raw, dict) else []
            more = raw.get("more_available", False) if isinstance(raw, dict) else False
            next_id = raw.get("next_max_id") if isinstance(raw, dict) else None
        for it in items:
            m = it.get("media") if isinstance(it, dict) and "media" in it else it
            code = m.get("code") or m.get("shortcode") or str(m.get("pk") or m.get("id"))
            if code in seen_codes:
                continue
            seen_codes.add(code)
            collected.append(m)
        pages += 1
        if not more or not next_id:
            break
        max_id = next_id
    log.info(f"hikerapi {endpoint}: {pages} page(s), {len(collected)} raw items")
    return collected


def scrape_profile_reels(handle, max_items=10):
    handle = handle.lstrip("@")
    uid = resolve_user_id(handle)
    medias = _paginated_items("v2/user/clips", uid, int(max_items))
    reels = []
    for m in medias:
        c = _reel_from_media(m, f"@{handle}")
        if c:
            reels.append(c)
        if len(reels) >= max_items:
            break
    log.info(f"hikerapi reels @{handle}: {len(reels)} reels")
    return reels[:int(max_items)]


def scrape_profile_pics(handle, max_items=10):
    handle = handle.lstrip("@")
    uid = resolve_user_id(handle)
    medias = _paginated_items("v2/user/medias", uid, int(max_items) * 2)  # over-fetch since we filter reels out
    pics = []
    for m in medias:
        c = _pic_from_media(m, f"@{handle}")
        if c:
            pics.append(c)
        if len(pics) >= max_items:
            break
    log.info(f"hikerapi pics @{handle}: {len(pics)} pics")
    return pics[:int(max_items)]
