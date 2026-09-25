"""SQLite cache + Graph API fetching for Instagram post insights.

Read-only data layer. Posting code never touches this module.
"""
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

import requests

log = logging.getLogger("insights")

DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "state", "insights.db")
BASE_URL = "https://graph.facebook.com/v25.0"


def init_db():
    """Create insights DB + tables if absent. Safe to call repeatedly."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                media_id        TEXT PRIMARY KEY,
                ig_user_id      TEXT NOT NULL,
                username        TEXT NOT NULL,
                timestamp       TEXT NOT NULL,
                permalink       TEXT,
                caption         TEXT,
                media_type      TEXT,
                like_count      INTEGER,
                comments_count  INTEGER,
                views           INTEGER,
                last_polled     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_posts_username  ON posts(username);
            CREATE INDEX IF NOT EXISTS idx_posts_timestamp ON posts(timestamp);
            CREATE INDEX IF NOT EXISTS idx_posts_iguser    ON posts(ig_user_id);
        """)
    log.info(f"Insights DB initialized at {DB_PATH}")


def _get_extra_db_paths():
    """Return list of valid extra insights.db paths from EXTRA_INSIGHTS_DBS env."""
    raw = os.environ.get("EXTRA_INSIGHTS_DBS", "")
    paths = []
    for p in raw.split(","):
        p = p.strip()
        if p and os.path.exists(p):
            paths.append(p)
    return paths


@contextmanager
def db_conn():
    """Read connection. Reads see a posts_all view union of main+extras.
    Writes go to main.posts directly (use 'posts', not 'posts_all')."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH, uri=True)
    conn.row_factory = sqlite3.Row
    union_parts = ["SELECT * FROM main.posts"]
    for i, p in enumerate(_get_extra_db_paths()):
        alias = f"ext_{i}"
        try:
            conn.execute(f"ATTACH DATABASE 'file:{p}?mode=ro' AS {alias}")
            conn.execute(f"SELECT 1 FROM {alias}.posts LIMIT 1")
            union_parts.append(f"SELECT * FROM {alias}.posts")
        except sqlite3.Error as e:
            log.warning(f"Could not attach extra DB {p}: {e}")
    try:
        conn.execute(f"CREATE TEMP VIEW posts_all AS {' UNION ALL '.join(union_parts)}")
    except sqlite3.Error as e:
        log.warning(f"Could not create posts_all view: {e}")
    try:
        yield conn
    finally:
        conn.close()


def fetch_recent_media(ig_user_id: str, token: str, limit: int = 30, base_url: str = None):
    """Recent media for one IG. Returns list of dicts (or [] on error)."""
    _base = base_url or BASE_URL
    try:
        r = requests.get(
            f"{_base}/{ig_user_id}/media",
            params={
                "fields": "id,timestamp,permalink,caption,media_type,like_count,comments_count",
                "limit": limit,
                "access_token": token,
            },
            timeout=30,
        ).json()
        if "error" in r:
            log.error(f"[{ig_user_id}] media fetch error: {r['error'].get('message','?')}")
            return []
        return r.get("data", [])
    except Exception as e:
        log.error(f"[{ig_user_id}] media fetch exception: {e}")
        return []


def fetch_media_views(media_id: str, token: str):
    """Get views for a Reel. Returns int or None (perm missing / not a Reel)."""
    try:
        r = requests.get(
            f"{BASE_URL}/{media_id}/insights",
            params={"metric": "views", "access_token": token},
            timeout=15,
        ).json()
        if "error" in r:
            return None
        for d in r.get("data", []):
            if d.get("name") == "views":
                vals = d.get("values", [])
                if vals:
                    return vals[0].get("value")
        return None
    except Exception:
        return None


# ---------- Upserts ----------

def upsert_post(row: dict):
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO posts (media_id, ig_user_id, username, timestamp, permalink,
                               caption, media_type, like_count, comments_count, views, last_polled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_id) DO UPDATE SET
                like_count     = excluded.like_count,
                comments_count = excluded.comments_count,
                views          = COALESCE(excluded.views, posts.views),
                permalink      = excluded.permalink,
                caption        = excluded.caption,
                last_polled    = excluded.last_polled
        """, (
            row["media_id"], row["ig_user_id"], row["username"], row["timestamp"],
            row["permalink"], row["caption"], row["media_type"],
            row["like_count"], row["comments_count"], row["views"], row["last_polled"],
        ))
        conn.commit()


def refresh_account(ig_user_id: str, username: str, token: str, with_views: bool = False, base_url: str = None):
    """Fetch + cache recent media for one account. Returns count of upserts."""
    media_list = fetch_recent_media(ig_user_id, token, base_url=base_url)
    now_iso = datetime.now(timezone.utc).isoformat()
    upserts = 0
    for m in media_list:
        views = fetch_media_views(m["id"], token) if with_views else None
        caption = (m.get("caption") or "")[:500]
        upsert_post({
            "media_id":       m["id"],
            "ig_user_id":     ig_user_id,
            "username":       username,
            "timestamp":      m.get("timestamp", ""),
            "permalink":      m.get("permalink", ""),
            "caption":        caption,
            "media_type":     m.get("media_type", ""),
            "like_count":     m.get("like_count"),
            "comments_count": m.get("comments_count"),
            "views":          views,
            "last_polled":    now_iso,
        })
        upserts += 1
    return upserts


def refresh_all(accounts, default_token, with_views_fn=None):
    """Refresh insights for all accounts. Returns total upsert count.
    IG Login accounts use per-account token + graph.instagram.com;
    FB Login accounts use default_token + graph.facebook.com."""
    import json
    total = 0
    # Load per-account IG Login tokens (keyed by ig_user_id)
    iglogin_path = os.path.join(DATA_DIR, "state", "iglogin_tokens.json")
    iglogin = {}
    try:
        with open(iglogin_path) as f:
            iglogin = json.load(f) or {}
    except Exception:
        iglogin = {}
    IG_BASE = "https://graph.instagram.com/v22.0"
    for acc in accounts:
        ig_id = str(acc["ig_user_id"])
        rec = iglogin.get(ig_id)
        if rec and rec.get("access_token"):
            token = rec["access_token"]
            base = IG_BASE
        else:
            token = acc.get("ig_access_token") or default_token
            base = None  # use default FB Login base
        wv = with_views_fn(token) if with_views_fn else False
        try:
            n = refresh_account(acc["ig_user_id"], acc["model_name"], token, with_views=wv, base_url=base)
            total += n
            log.info(f"Refreshed {n} posts for @{acc['model_name']}")
        except Exception as e:
            log.error(f"Refresh failed for @{acc.get('model_name','?')}: {e}")
    return total


# ---------- Queries ----------

def query_summary(days: int = 7):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS post_count,
                   COUNT(DISTINCT ig_user_id) AS account_count,
                   COALESCE(SUM(like_count), 0) AS total_likes,
                   COALESCE(SUM(comments_count), 0) AS total_comments,
                   COALESCE(SUM(views), 0) AS total_views,
                   COUNT(views) AS posts_with_views
            FROM posts_all WHERE timestamp >= ?
        """, (since,)).fetchone()
        return dict(row) if row else {}


def query_today():
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM posts_all WHERE timestamp >= ?
            ORDER BY timestamp DESC
        """, (start,)).fetchall()
        return [dict(r) for r in rows]


def query_account(username: str, limit: int = 5):
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM posts_all WHERE username = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (username, limit)).fetchall()
        return [dict(r) for r in rows]


def query_top(days: int = 7, limit: int = 5):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM posts_all WHERE timestamp >= ?
            ORDER BY COALESCE(views, 0) DESC, COALESCE(like_count, 0) DESC
            LIMIT ?
        """, (since, limit)).fetchall()
        return [dict(r) for r in rows]


def get_last_polled():
    with db_conn() as conn:
        row = conn.execute("SELECT MAX(last_polled) AS last FROM posts").fetchone()
        return row["last"] if row else None
