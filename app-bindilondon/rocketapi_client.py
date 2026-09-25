"""RocketAPI wrapper for Instagram profile scraping.

Uses POST endpoints with paginated max_id. Returns candidates in the
same shape as hikerapi_client so /sync stays interchangeable.
"""
import os
import logging
import requests

log = logging.getLogger("rocketapi")

RC_KEY = os.environ.get("ROCKETAPI_KEY", "")
RC_BASE = "https://v1.rocketapi.io/instagram"

_uid_cache = {}


class RocketAPIError(RuntimeError):
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


def _post(path, body, timeout=45):
    if not RC_KEY:
        raise RocketAPIError("ROCKETAPI_KEY not configured")
    r = requests.post(
        f"{RC_BASE}/{path.lstrip('/')}",
        json=body,
        headers={"Authorization": f"Token {RC_KEY}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RocketAPIError(f"[{r.status_code}] {path}: {r.text[:300]}")
    try:
        return r.json()
    except ValueError:
        raise RocketAPIError(f"non-JSON from {path}: {r.text[:200]}")


def resolve_user_id(handle):
    handle = handle.lstrip("@").lower()
    if handle in _uid_cache:
        return _uid_cache[handle]
    d = _post("user/get_info_by_username", {"username": handle})
    body = d.get("response", {}).get("body", {})
    user = body.get("user") or body.get("data", {}).get("user") or body
    uid = user.get("pk") or user.get("id")
    if not uid:
        raise RocketAPIError(f"Could not resolve user_id for @{handle}")
    _uid_cache[handle] = int(uid)
    return int(uid)


def _pick_video_url(media):
    vv = media.get("video_versions") or []
    if not vv:
        return None
    try:
        best = max(vv, key=lambda v: v.get("width", 0) * v.get("height", 0))
        return best.get("url")
    except Exception:
        return vv[0].get("url")


def _pick_image_url(media):
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
        if v: return v
    return None


def _extract_caption(media):
    c = media.get("caption")
    if isinstance(c, dict):
        return c.get("text") or ""
    if isinstance(c, str):
        return c
    if media.get("caption_text"):
        return media["caption_text"]
    return ""


def _reel_from_media(m, source_ref):
    if m.get("media_type") != 2 and (m.get("product_type") or "").lower() != "clips":
        return None
    video_url = _pick_video_url(m)
    if not video_url:
        return None
    return {
        "ig_shortcode":  m.get("code") or str(m.get("pk") or m.get("id")),
        "source_type":   "creator",
        "source_ref":    source_ref,
        "video_url":     video_url,
        "cover_url":     _pick_image_url(m),
        "caption":       (_extract_caption(m) or "")[:2000],
        "like_count":    _safe_int(m.get("like_count")),
        "comment_count": _safe_int(m.get("comment_count")),
        "play_count":    _safe_int(m.get("play_count") or m.get("view_count")),
        "posted_at":     m.get("taken_at"),
    }


def _pic_from_media(m, source_ref):
    mt = m.get("media_type")
    if mt == 2 or (m.get("product_type") or "").lower() == "clips":
        return None
    if mt == 8:
        children = m.get("carousel_media") or m.get("resources") or []
        image_url = _pick_image_url(children[0]) if children else _pick_image_url(m)
    else:
        image_url = _pick_image_url(m)
    if not image_url:
        return None
    return {
        "ig_shortcode":  m.get("code") or str(m.get("pk") or m.get("id")),
        "source_type":   "creator",
        "source_ref":    source_ref,
        "image_url":     image_url,
        "cover_url":     image_url,
        "caption":       (_extract_caption(m) or "")[:2000],
        "like_count":    _safe_int(m.get("like_count")),
        "comment_count": _safe_int(m.get("comment_count")),
        "play_count":    None,
        "posted_at":     m.get("taken_at"),
    }


def _paginate(endpoint, uid, max_items):
    collected = []
    seen = set()
    max_id = None
    pages = 0
    max_pages = 40  # up to 480 items
    while len(collected) < max_items and pages < max_pages:
        body = {"id": uid, "count": 12}
        if max_id:
            body["max_id"] = max_id
        d = _post(endpoint, body)
        b = d.get("response", {}).get("body", {})
        items = b.get("items", [])
        for it in items:
            m = it.get("media") if isinstance(it, dict) and "media" in it else it
            code = m.get("code") or str(m.get("pk"))
            if code in seen:
                continue
            seen.add(code)
            collected.append(m)
            if len(collected) >= max_items:
                break
        pi = b.get("paging_info", {})
        more = pi.get("more_available", False) or b.get("more_available", False)
        max_id = pi.get("max_id") or b.get("next_max_id")
        pages += 1
        if not more or not max_id:
            break
    log.info(f"rocketapi {endpoint}: {pages} pages, {len(collected)} items")
    return collected


def scrape_profile_reels(handle, max_items=200):
    handle = handle.lstrip("@")
    uid = resolve_user_id(handle)
    medias = _paginate("user/get_clips", uid, int(max_items))
    reels = [c for c in (_reel_from_media(m, f"@{handle}") for m in medias) if c]
    log.info(f"rocketapi reels @{handle}: {len(reels)} reels")
    return reels


def scrape_profile_pics(handle, max_items=200):
    handle = handle.lstrip("@")
    uid = resolve_user_id(handle)
    medias = _paginate("user/get_media", uid, int(max_items) * 2)  # over-fetch since we filter reels out
    pics = [c for c in (_pic_from_media(m, f"@{handle}") for m in medias) if c]
    log.info(f"rocketapi pics @{handle}: {len(pics)} pics")
    return pics[:int(max_items)]
