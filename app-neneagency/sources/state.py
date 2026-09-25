"""SQLite state for source items — dedup, download tracking, post status.

One row per (provider, account, remote_id). Status lifecycle:

    pending  → after download, before Instagram publish
    posted   → after successful Instagram publish (ig_media_id set)
    cleared  → after /igqueueclear moved file to _posted/ on remote

Failed downloads leave no row (we clean up local file and don't insert),
so retrying is a no-op.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .base import SourceItem


_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT    NOT NULL,
    account        TEXT    NOT NULL,
    remote_id      TEXT    NOT NULL,
    remote_name    TEXT    NOT NULL,
    local_path     TEXT    NOT NULL,
    downloaded_at  INTEGER NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pending',
    posted_at      INTEGER,
    cleared_at     INTEGER,
    ig_media_id    TEXT,
    UNIQUE (provider, account, remote_id)
);

CREATE INDEX IF NOT EXISTS idx_source_items_status
    ON source_items (account, status);
"""


class SourceState:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- reads ----

    def known_remote_ids(self, provider: str, account: str) -> set[str]:
        """All remote_ids we've ever seen for (provider, account).

        Passed to provider.list_new() so we skip already-downloaded items,
        including ones that are posted-but-not-yet-cleared (still on remote).
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT remote_id FROM source_items WHERE provider=? AND account=?",
                (provider, account),
            ).fetchall()
        return {r["remote_id"] for r in rows}

    def pending_for_account(self, account: str, limit: int) -> list[SourceItem]:
        """Downloaded but not yet posted, oldest-first. Feeds igcampaign."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM source_items "
                "WHERE account=? AND status='pending' "
                "ORDER BY downloaded_at ASC LIMIT ?",
                (account, limit),
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def posted_items(self, account: Optional[str] = None) -> list[SourceItem]:
        """Items ready to be moved to _posted/ on remote."""
        with self._conn() as c:
            if account:
                rows = c.execute(
                    "SELECT * FROM source_items WHERE status='posted' AND account=?",
                    (account,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM source_items WHERE status='posted'"
                ).fetchall()
        return [_row_to_item(r) for r in rows]

    # ---- writes ----

    def record_download(
        self,
        provider: str,
        account: str,
        remote_id: str,
        remote_name: str,
        local_path: str,
    ) -> SourceItem:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO source_items "
                "(provider, account, remote_id, remote_name, local_path, downloaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (provider, account, remote_id, remote_name, local_path, int(time.time())),
            )
            row = c.execute(
                "SELECT * FROM source_items WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return _row_to_item(row)

    def mark_posted(self, item_id: int, ig_media_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE source_items SET status='posted', posted_at=?, ig_media_id=? "
                "WHERE id=? AND status='pending'",
                (int(time.time()), ig_media_id, item_id),
            )

    def mark_cleared(self, item_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE source_items SET status='cleared', cleared_at=? "
                "WHERE id=? AND status='posted'",
                (int(time.time()), item_id),
            )

    def mark_pool_cleared(self, account: str) -> int:
        """Bulk-mark all pending/posted items for `account` as cleared.

        Used by /igqueueclear after videos_mod.clear_sources() wipes the
        campaign pool — no per-item lookup needed. Returns row count.
        """
        with self._conn() as c:
            cur = c.execute(
                "UPDATE source_items SET status='cleared', cleared_at=? "
                "WHERE account=? AND status IN ('pending', 'posted')",
                (int(time.time()), account),
            )
            return cur.rowcount

    def delete_item(self, item_id: int) -> None:
        """Hard-delete a source item row. Used by pool rollback on
        partial-pull failure — the local file is expected to be gone
        already, and we want the remote_id available for re-download."""
        with self._conn() as c:
            c.execute("DELETE FROM source_items WHERE id=?", (item_id,))


def _row_to_item(r: sqlite3.Row) -> SourceItem:
    return SourceItem(
        id=r["id"],
        provider=r["provider"],
        account=r["account"],
        remote_id=r["remote_id"],
        remote_name=r["remote_name"],
        local_path=r["local_path"],
        status=r["status"],
        ig_media_id=r["ig_media_id"],
    )
