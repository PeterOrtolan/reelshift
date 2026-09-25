"""
iglogin_client.py - Instagram Login OAuth flow + token management.

Separate from the existing FB Login / Business Portfolio path. This handles
the newer "Instagram API with Instagram Login" auth flow, which doesn't
require Facebook Pages.

Tokens are stored in a JSON file keyed by IG user_id.
"""
import os
import time
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

log = logging.getLogger("iglogin")

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
SHORT_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
LONG_TOKEN_URL = "https://graph.instagram.com/access_token"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
GRAPH_BASE = "https://graph.instagram.com/v21.0"

SCOPES = "instagram_business_basic,instagram_business_content_publish"


class IGLoginClient:
    def __init__(self, app_id, app_secret, redirect_uri, tokens_path):
        self.app_id = str(app_id)
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.tokens_path = str(tokens_path)
        Path(self.tokens_path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(self.tokens_path).exists():
            self._save({})

    def _load(self):
        try:
            with open(self.tokens_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, data):
        tmp = self.tokens_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.tokens_path)

    def build_authorize_url(self, state=""):
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "response_type": "code",
        }
        if state:
            params["state"] = state
        q = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        return f"{AUTHORIZE_URL}?{q}"

    def exchange_code(self, code):
        """Full flow: code -> short token -> long token -> save."""
        code = code.rstrip("#_").strip()
        # Step 1: short-lived token (1h)
        r = requests.post(SHORT_TOKEN_URL, data={
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
            "code": code,
        }, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"short-token exchange failed [{r.status_code}]: {r.text}")
        short = r.json()
        short_token = short["access_token"]
        user_id = str(short.get("user_id", ""))
        # Step 2: long-lived token (60d)
        r = requests.get(LONG_TOKEN_URL, params={
            "grant_type": "ig_exchange_token",
            "client_secret": self.app_secret,
            "access_token": short_token,
        }, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"long-token exchange failed [{r.status_code}]: {r.text}")
        long_ = r.json()
        long_token = long_["access_token"]
        expires_in = long_.get("expires_in", 60 * 24 * 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        # Step 3: fetch username
        username = user_id
        try:
            r = requests.get(f"{GRAPH_BASE}/me", params={
                "fields": "id,username",
                "access_token": long_token,
            }, timeout=15)
            if r.status_code < 400:
                j = r.json()
                username = j.get("username") or username
                if not user_id and j.get("id"):
                    user_id = str(j["id"])
        except Exception as e:
            log.warning(f"username fetch failed: {e}")
        # Save
        data = self._load()
        record = {
            "user_id": user_id,
            "username": username,
            "access_token": long_token,
            "expires_at": expires_at,
            "connected_at": datetime.utcnow().isoformat(),
        }
        data[user_id] = record
        self._save(data)
        return record

    def refresh_all(self, days_threshold=15, proxy_by_user_id=None):
        data = self._load()
        results = []
        threshold = datetime.utcnow() + timedelta(days=days_threshold)
        proxy_by_user_id = proxy_by_user_id or {}
        for uid, info in list(data.items()):
            try:
                exp = datetime.fromisoformat(info["expires_at"])
            except Exception:
                continue
            if exp > threshold:
                continue
            try:
                _proxy = proxy_by_user_id.get(uid)
                _proxies = {"http": _proxy, "https": _proxy} if _proxy else None
                r = requests.get(REFRESH_URL, params={
                    "grant_type": "ig_refresh_token",
                    "access_token": info["access_token"],
                }, timeout=30, proxies=_proxies)
                if r.status_code >= 400:
                    raise RuntimeError(f"{r.status_code}: {r.text}")
                new = r.json()
                info["access_token"] = new["access_token"]
                info["expires_at"] = (datetime.utcnow() + timedelta(
                    seconds=new.get("expires_in", 60 * 24 * 3600))).isoformat()
                info["last_refreshed"] = datetime.utcnow().isoformat()
                data[uid] = info
                results.append({"username": info.get("username"), "status": "ok"})
            except Exception as e:
                results.append({"username": info.get("username"), "status": "error", "error": str(e)})
        self._save(data)
        return results

    def list_tokens(self):
        return list(self._load().values())

    def get_token(self, user_id):
        return self._load().get(str(user_id))


    def publish_image(self, user_id, image_url, caption="", proxy_url=None):
        """Post a photo via IG Login API. Returns media_id."""
        rec = self.get_token(user_id)
        if not rec:
            raise RuntimeError(f"No IG Login token stored for user_id {user_id}")
        token = rec["access_token"]
        _proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        # Step 1: create container (no media_type needed for images)
        r = requests.post(
            f"{GRAPH_BASE}/{user_id}/media",
            data={"image_url": image_url, "caption": caption, "access_token": token},
            timeout=60, proxies=_proxies,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"image container create failed [{r.status_code}]: {r.text}")
        container_id = r.json().get("id")
        # Step 2: publish (images finalize instantly, no polling needed)
        import time
        time.sleep(2)
        r = requests.post(
            f"{GRAPH_BASE}/{user_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
            timeout=60, proxies=_proxies,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"image publish failed [{r.status_code}]: {r.text}")
        return r.json().get("id")

    def publish_reel(self, user_id, video_url, caption="", share_to_feed=True, poll_interval=10, poll_max_tries=60, proxy_url=None, cover_url=None):
        """Post a video Reel via IG Login API. Returns media_id.

        Uses the graph.instagram.com endpoints (different from graph.facebook.com FB-Login path).
        Two-step process: create container, poll until FINISHED, then publish.
        """
        rec = self.get_token(user_id)
        if not rec:
            raise RuntimeError(f"No IG Login token stored for user_id {user_id}")
        token = rec["access_token"]
        _proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        # Step 1: create media container
        payload = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false",
            "access_token": token,
        }
        if cover_url:
            payload["cover_url"] = cover_url
        r = requests.post(f"{GRAPH_BASE}/{user_id}/media", data=payload, timeout=60, proxies=_proxies)
        if r.status_code >= 400:
            raise RuntimeError(f"container create failed [{r.status_code}]: {r.text}")
        container_id = r.json().get("id")
        if not container_id:
            raise RuntimeError(f"no container id in response: {r.text}")
        log.info(f"IG Login publish: container {container_id} created for user {user_id}")

        # Step 2: poll container status
        # Instagram's status API is racy - can transiently report ERROR then recover to FINISHED.
        # Only give up on ERROR after N consecutive polls; treat EXPIRED as terminal.
        last_status = None
        consecutive_errors = 0
        error_retry_max = 5  # 5 * poll_interval seconds of ERROR before giving up
        for i in range(poll_max_tries):
            time.sleep(poll_interval)
            r = requests.get(
                f"{GRAPH_BASE}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
                timeout=30,
                proxies=_proxies,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"container poll failed [{r.status_code}]: {r.text}")
            j = r.json()
            last_status = j.get("status_code", "?")
            log.info(f"IG Login publish: container {container_id} status={last_status} (poll {i+1})")
            if last_status == "FINISHED":
                break
            elif last_status == "EXPIRED":
                raise RuntimeError(f"container expired: {j}")
            elif last_status == "ERROR":
                consecutive_errors += 1
                if consecutive_errors >= error_retry_max:
                    raise RuntimeError(f"container ERROR after {error_retry_max} consecutive polls: {j}")
                log.warning(f"transient ERROR, retrying ({consecutive_errors}/{error_retry_max})")
            else:
                consecutive_errors = 0
        else:
            raise RuntimeError(f"container status polling timeout (last status: {last_status})")

        # Step 3: publish
        r = requests.post(
            f"{GRAPH_BASE}/{user_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
            timeout=60,
            proxies=_proxies,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"publish failed [{r.status_code}]: {r.text}")
        media_id = r.json().get("id")
        log.info(f"IG Login publish: SUCCESS media_id={media_id}")
        return media_id

    def remove(self, user_id):
        data = self._load()
        removed = data.pop(str(user_id), None)
        self._save(data)
        return removed
