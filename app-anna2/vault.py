"""SQLite vault for scraped video candidates and approved-for-post items.

Lifecycle: pending -> approved | rejected
           approved -> used (when consumed by /igcampaign)
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

log = logging.getLogger("vault")

DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "state", "vault.db")


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db():
    """Create vault DB + tables if absent. Safe to call repeatedly."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS candidates (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ig_shortcode    TEXT UNIQUE NOT NULL,
                source_type     TEXT NOT NULL,
                source_ref      TEXT NOT NULL,
                video_url       TEXT NOT NULL,
                cover_url       TEXT,
                caption         TEXT,
                like_count      INTEGER,
                comment_count   INTEGER,
                play_count      INTEGER,
                posted_at       TEXT,
                scraped_at      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                approved_at     TEXT,
                used_at         TEXT,
                used_campaign   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_vault_status    ON candidates(status);
            CREATE INDEX IF NOT EXISTS idx_vault_shortcode ON candidates(ig_shortcode);
        """)
    log.info(f"Vault DB initialized at {DB_PATH}")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _int_or_none(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try: return int(v.replace(',', ''))
        except ValueError: return None
    return None


def add_candidate(ig_shortcode, source_type, source_ref, video_url,
                  cover_url=None, caption=None,
                  like_count=None, comment_count=None, play_count=None,
                  posted_at=None):
    """Insert a scraped candidate. Returns the new row id, or None if duplicate."""
    if not ig_shortcode or not video_url:
        raise ValueError("ig_shortcode and video_url are required")
    with _conn() as c:
        try:
            like_count = _int_or_none(like_count)
            comment_count = _int_or_none(comment_count)
            play_count = _int_or_none(play_count)
            cur = c.execute(
                """
                INSERT INTO candidates
                  (ig_shortcode, source_type, source_ref, video_url, cover_url,
                   caption, like_count, comment_count, play_count,
                   posted_at, scraped_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (ig_shortcode, source_type, source_ref, video_url, cover_url,
                 caption, like_count, comment_count, play_count,
                 posted_at, _utc_now_iso()),
            )
            c.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # already in vault


def get(candidate_id):
    with _conn() as c:
        row = c.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        return dict(row) if row else None


def list_by_status(status, limit=None):
    q = "SELECT * FROM candidates WHERE status = ? ORDER BY scraped_at DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, (status,)).fetchall()]


def next_approved(n):
    """Oldest-approved-first — FIFO consumption by /igcampaign."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM candidates WHERE status = 'approved' "
            "ORDER BY approved_at ASC LIMIT ?",
            (int(n),),
        ).fetchall()
        return [dict(r) for r in rows]


def approve(candidate_id):
    with _conn() as c:
        c.execute(
            "UPDATE candidates SET status = 'approved', approved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (_utc_now_iso(), candidate_id),
        )
        c.commit()
        return c.total_changes > 0


def reject(candidate_id):
    with _conn() as c:
        c.execute(
            "UPDATE candidates SET status = 'rejected' "
            "WHERE id = ? AND status = 'pending'",
            (candidate_id,),
        )
        c.commit()
        return c.total_changes > 0


def mark_used(candidate_ids, campaign_id):
    if not candidate_ids:
        return 0
    now = _utc_now_iso()
    placeholders = ",".join("?" * len(candidate_ids))
    with _conn() as c:
        c.execute(
            f"UPDATE candidates SET status = 'used', used_at = ?, used_campaign = ? "
            f"WHERE id IN ({placeholders}) AND status = 'approved'",
            (now, campaign_id, *candidate_ids),
        )
        c.commit()
        return c.total_changes


def status_counts():
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM candidates GROUP BY status"
        ).fetchall()
    out = {"pending": 0, "approved": 0, "rejected": 0, "used": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    return out


def purge(status, older_than_days=None):
    """Delete rows in a given status. Optionally only older than N days.
    Returns rows deleted."""
    with _conn() as c:
        if older_than_days:
            cutoff = datetime.now(timezone.utc).timestamp() - int(older_than_days) * 86400
            cur = c.execute(
                "DELETE FROM candidates WHERE status = ? AND "
                "strftime('%s', scraped_at) < ?",
                (status, str(int(cutoff))),
            )
        else:
            cur = c.execute("DELETE FROM candidates WHERE status = ?", (status,))
        c.commit()
        return cur.rowcount
