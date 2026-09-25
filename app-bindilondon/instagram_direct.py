"""Direct Instagram hashtag reels scraper (authenticated via sessionid cookie).

Uses Instagram's private /api/v1/tags/<tag>/sections/ JSON endpoint with a
sessionid cookie from a logged-in browser. Optional per-call proxy_url.

Sessionid comes from env IG_SESSIONID and expires periodically — re-paste
via /opt/reelshift-graph/deployments/anna2/.env when scrapes start returning
HTML instead of JSON.
"""
import os
import logging
import requests

log = logging.getLogger("igdirect")

IG_APP_ID = "936619743392459"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")

DEFAULT_PROXY = os.environ.get("SCRAPE_PROXY_URL", "")


def _sessionid():
    return os.environ.get("IG_SESSIONID", "").strip()


def _csrftoken():
    return os.environ.get("IG_CSRFTOKEN", "").strip()


def _headers(referer):
    sid = _sessionid()
    if not sid:
        raise RuntimeError(
            "IG_SESSIONID not set. Paste a logged-in Instagram sessionid into "
            "/opt/reelshift-graph/deployments/anna2/.env as IG_SESSIONID=... and restart"
        )
    return {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": IG_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "X-ASBD-ID": "129477",
        "Referer": referer,
        "Origin": "https://www.instagram.com",
        "Cookie": f"sessionid={sid}; csrftoken={_csrftoken()}",
        "X-CSRFToken": _csrftoken(),
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


def _proxies(proxy_url=None):
    p = proxy_url or DEFAULT_PROXY
    return {"http": p, "https": p} if p else None


def _iter_media_from_sections(sections):
    for section in sections or []:
        layout = section.get("layout_content") or {}
        for medias in ("medias", "one_by_two_item", "fill_items"):
            entries = layout.get(medias) or []
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries:
                media = entry.get("media") if isinstance(entry, dict) else None
                if media:
                    yield media
        for arr in layout.get("two_by_two_item") or []:
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict) and item.get("media"):
                        yield item["media"]


def _pick_video_url(media):
    vv = media.get("video_versions") or []
    if not vv:
        return None
    try:
        best = max(vv, key=lambda v: v.get("width", 0) * v.get("height", 0))
        return best.get("url")
    except Exception:
        return vv[0].get("url")


def _is_reel(media):
    if (media.get("product_type") or "").lower() == "clips":
        return True
    return media.get("media_type") == 2 and bool(_pick_video_url(media))


def _normalize(media, source_type, source_ref):
    if not _is_reel(media):
        return None
    video_url = _pick_video_url(media)
    if not video_url:
        return None
    caption = ""
    cap = media.get("caption") or {}
    if isinstance(cap, dict):
        caption = cap.get("text") or ""
    cover_url = None
    ivs = media.get("image_versions2", {}).get("candidates") or []
    if ivs:
        cover_url = ivs[0].get("url")
    return {
        "ig_shortcode":  media.get("code") or media.get("shortcode") or str(media.get("pk")),
        "source_type":   source_type,
        "source_ref":    source_ref,
        "video_url":     video_url,
        "cover_url":     cover_url,
        "caption":       (caption or "")[:2000],
        "like_count":    media.get("like_count"),
        "comment_count": media.get("comment_count"),
        "play_count":    media.get("play_count") or media.get("view_count"),
        "posted_at":     None,
    }


def scrape_hashtag(tag, max_items=10, proxy_url=None):
    tag = tag.lstrip("#").lower()
    referer = f"https://www.instagram.com/explore/tags/{tag}/"
    url = f"https://www.instagram.com/api/v1/tags/{tag}/sections/"
    reels = []
    seen_codes = set()

    # Try "top" first (higher reel yield), then "recent" (freshness)
    for tab in ("top", "recent"):
        try:
            r = requests.post(
                url,
                data={"include_persistent": "0", "max_id": "", "page": "0", "surface": "grid", "tab": tab},
                headers=_headers(referer),
                proxies=_proxies(proxy_url),
                timeout=45,
            )
        except Exception as e:
            log.warning(f"IG direct fetch failed for #{tag} tab={tab}: {e}")
            continue
        if r.status_code >= 400:
            log.warning(f"IG direct #{tag} tab={tab} [{r.status_code}]: {r.text[:150]}")
            continue
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"IG direct returned non-JSON (sessionid/csrftoken expired?): {r.text[:150]}")
        for media in _iter_media_from_sections(data.get("sections") or []):
            c = _normalize(media, "hashtag", f"#{tag}")
            if c and c["ig_shortcode"] not in seen_codes:
                seen_codes.add(c["ig_shortcode"])
                reels.append(c)
                if len(reels) >= max_items:
                    log.info(f"IG direct #{tag}: {len(reels)} reels (max reached)")
                    return reels

    log.info(f"IG direct #{tag}: {len(reels)} reels")
    return reels
