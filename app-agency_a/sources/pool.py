"""Pool-level content ingestion — one shared source folder per deployment.

Design: all IG accounts in a deployment share ONE source folder
(agency-wide). Config is a single-provider JSON file at CONFIG_PATH,
editable via bot commands (/setsource, /showsource, /clearsource).

Flow:
  pull_pool(count) → downloads `count` new videos from the configured
    source, returns SourceItems. Caller (bot handler) then feeds each
    local_path to videos_mod.add_source_video() to enter the existing
    campaign pipeline.

  clear_pool() → after /igqueueclear runs videos_mod.clear_sources(),
    this bulk-marks SQLite entries as cleared and best-effort deletes
    any leftover temp files.

Dedup: pool items are tracked under the synthetic account "_pool" in
SQLite. `known_remote_ids('gdrive', '_pool')` prevents re-downloading
anything we've ever pulled for the pool, regardless of status.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from .base import SourceItem
from .state import SourceState

log = logging.getLogger(__name__)

POOL_KEY = "_pool"

_STATE_DB = Path(os.environ.get("SOURCES_DB", "/data/state/sources.db"))
_INBOX_DIR = Path(os.environ.get(
    "SOURCES_INBOX", "/data/state/sources_pending"
))
_CONFIG_PATH = Path(os.environ.get(
    "SOURCES_CONFIG", "/data/state/source_config.json"
))


# ---- config ----

def get_source_config() -> Optional[dict]:
    """Return {'provider': str, 'folder_ref': str} or None if unset."""
    if not _CONFIG_PATH.is_file():
        return None
    try:
        with _CONFIG_PATH.open() as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("source_config: failed to load: %s", e)
        return None


def set_source_config(provider: str, folder_ref: str) -> None:
    """Overwrite the source config. Provider must be 'gdrive' or 'dropbox'."""
    if provider not in ("gdrive", "dropbox"):
        raise ValueError(f"bad provider: {provider!r}")
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _CONFIG_PATH.open("w") as fh:
        json.dump({"provider": provider, "folder_ref": folder_ref}, fh, indent=2)


def clear_source_config() -> bool:
    """Delete the config file. Returns True if it existed."""
    if _CONFIG_PATH.is_file():
        _CONFIG_PATH.unlink()
        return True
    return False


# ---- URL parsing ----

def parse_source_url(raw: str) -> tuple[str, str]:
    """Auto-detect provider from a URL or bare folder ID.

    Returns (provider, folder_ref). Raises ValueError if unrecognized.
    Accepts:
      - Google Drive folder URL (drive.google.com/drive/folders/<ID>)
      - Google Drive folderview URL (?id=<ID>)
      - Google Drive bare folder ID (25+ chars, alphanum/-/_)
      - Dropbox shared link URL (www.dropbox.com/scl/fo/...)
    """
    s = raw.strip()

    if s.startswith(("https://drive.google.com/", "http://drive.google.com/")):
        p = urlparse(s)
        parts = [x for x in p.path.split("/") if x]
        for i, part in enumerate(parts):
            if part == "folders" and i + 1 < len(parts):
                return ("gdrive", parts[i + 1])
        q = parse_qs(p.query)
        if "id" in q:
            return ("gdrive", q["id"][0])
        raise ValueError("Drive URL didn't contain a folder ID")

    if s.startswith(("https://www.dropbox.com/", "https://dropbox.com/")):
        # dropbox provider detects http-prefix and treats as shared link
        return ("dropbox", s)

    if s.startswith("http"):
        raise ValueError(f"Unrecognized URL: {s[:60]}")

    # Bare-string fallback — assume gdrive folder ID
    if _looks_like_gdrive_id(s):
        return ("gdrive", s)

    raise ValueError("Not a Drive URL, Dropbox URL, or Drive folder ID")


def _looks_like_gdrive_id(s: str) -> bool:
    return len(s) >= 20 and all(c.isalnum() or c in "-_" for c in s)


# ---- pull & clear ----

def pull_pool(count: int) -> list[SourceItem]:
    """Fetch EXACTLY `count` fresh videos from the configured source.

    All-or-nothing: if fewer than `count` fresh videos are available
    after applying the known-ids filter and age embargo, NO downloads
    happen and an empty list is returned. If some downloads succeed
    but others fail (network mid-run), the successful ones are rolled
    back (local files deleted, SQLite rows removed) so a retry starts
    from the same state.

    Returns list of length `count`, or empty list on any failure.
    """
    if count <= 0:
        return []

    cfg = get_source_config()
    if not cfg:
        log.info("pool: no source configured")
        return []

    # Late import to avoid circular: __init__.py provides _get_provider
    from . import _get_provider
    provider = _get_provider(cfg["provider"])
    if provider is None:
        log.warning("pool: provider %s unavailable", cfg["provider"])
        return []

    folder_ref = cfg["folder_ref"]
    state = SourceState(_STATE_DB)

    try:
        known = state.known_remote_ids(cfg["provider"], POOL_KEY)
        remotes = provider.list_new(folder_ref, exclude_ids=known)
    except Exception:
        log.exception("pool: list from %s failed", cfg["provider"])
        return []

    # Age embargo (skip freshly-uploaded content)
    min_age_days = float(os.environ.get("SOURCES_MIN_AGE_DAYS", "0"))
    if min_age_days > 0:
        cutoff = int(time.time()) - int(min_age_days * 86400)
        before = len(remotes)
        remotes = [r for r in remotes if (r.modified_at or 0) <= cutoff]
        excluded = before - len(remotes)
        if excluded:
            log.info(
                "pool: age filter (%.1fd) excluded %d/%d files",
                min_age_days, excluded, before,
            )

    # All-or-nothing precheck
    if len(remotes) < count:
        log.info(
            "pool: only %d fresh videos available, need %d — no pull",
            len(remotes), count,
        )
        return []

    downloaded: list[SourceItem] = []
    _INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for remote in remotes[:count]:
        safe = _sanitize(remote.name)
        dest = _INBOX_DIR / f"{uuid.uuid4().hex[:8]}_{safe}"
        try:
            provider.download(folder_ref, remote.remote_id, dest)
        except Exception:
            log.exception("pool: download %s failed", remote.name)
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        item = state.record_download(
            provider=cfg["provider"],
            account=POOL_KEY,
            remote_id=remote.remote_id,
            remote_name=remote.name,
            local_path=str(dest),
        )
        downloaded.append(item)

    # Rollback on partial success — atomicity
    if len(downloaded) < count:
        log.warning(
            "pool: %d/%d downloads succeeded, rolling back",
            len(downloaded), count,
        )
        for item in downloaded:
            try:
                Path(item.local_path).unlink(missing_ok=True)
            except OSError:
                pass
            state.delete_item(item.id)
        return []

    log.info("pool: pulled %d videos", len(downloaded))
    return downloaded


def clear_pool() -> int:
    """Best-effort clean up the pending-inbox dir and mark all pool
    entries as cleared. Called from /igqueueclear alongside
    videos_mod.clear_sources().

    Returns the count of SQLite rows transitioned.
    """
    # Nuke leftover temp files
    try:
        if _INBOX_DIR.is_dir():
            for f in _INBOX_DIR.iterdir():
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError:
        pass

    state = SourceState(_STATE_DB)
    return state.mark_pool_cleared(POOL_KEY)


def _sanitize(name: str) -> str:
    keep = "-_.() "
    return "".join(c if c.isalnum() or c in keep else "_" for c in name)[:120]
