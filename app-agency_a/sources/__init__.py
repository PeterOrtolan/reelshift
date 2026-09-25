"""Content source ingestion for reelshift-graph.

Public API — everything the bot needs:

    from app.sources import pull_next, mark_posted, clear_posted, load_account

Flow:
    /igcampaign         → items = pull_next(username, 1)
    /igcampaign N       → items = pull_next(username, N)
    (after publish)     → mark_posted(item.id, ig_media_id)
    /igqueueclear       → clear_posted(account=None)   # all accounts

Config — configs/accounts/<username>.json:

    {
        "ig_username": "someacct",
        "ig_user_id": "1784...",
        "sources": {
            "gdrive":  { "folder_id":   "1AbcXyZ..." },
            "dropbox": {
                "folder_path": "/reels/someacct",
                "min_age_days": 14
            }
        }
    }

Either or both providers are optional. If both are configured,
pull_next drains gdrive first, then dropbox, in that order.

Age embargo:
  `min_age_days` on any provider skips files uploaded within the last
  N days. Prevents the bot from consuming freshly-arrived content that
  hasn't been manually vetted. Env fallback: SOURCES_MIN_AGE_DAYS.
  Default 0 (no filter).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from .base import SourceItem, SourceProvider
from .state import SourceState
from .pool import (
    POOL_KEY,
    clear_pool,
    clear_source_config,
    get_source_config,
    parse_source_url,
    pull_pool,
    set_source_config,
)

log = logging.getLogger(__name__)

_INBOX_DIR = Path(os.environ.get("INBOX_DIR", "/data/inbox"))
_STATE_DB = Path(os.environ.get("SOURCES_DB", "/data/state/sources.db"))
_ACCOUNTS_DIR = Path(os.environ.get("ACCOUNTS_DIR", "configs/accounts"))

_state: Optional[SourceState] = None
_providers: dict[str, SourceProvider] = {}


def _get_state() -> SourceState:
    global _state
    if _state is None:
        _state = SourceState(_STATE_DB)
    return _state


def _get_provider(name: str) -> Optional[SourceProvider]:
    """Lazy provider construction; returns None if creds missing."""
    if name in _providers:
        return _providers[name]
    try:
        if name == "gdrive":
            from .gdrive import GDriveSource
            _providers[name] = GDriveSource()
        elif name == "dropbox":
            from .dropbox_src import DropboxSource
            _providers[name] = DropboxSource()
        else:
            log.warning("unknown source provider: %s", name)
            return None
    except Exception as e:
        log.warning("provider %s unavailable: %s", name, e)
        return None
    return _providers[name]


def load_account(username: str) -> dict:
    """Read configs/accounts/<username>.json. Callers may already do this;
    exposed here so the sources module doesn't require it."""
    p = _ACCOUNTS_DIR / f"{username}.json"
    with p.open() as fh:
        return json.load(fh)


def pull_next(username: str, count: int = 1) -> list[SourceItem]:
    """Return up to `count` unposted SourceItems for `username`, downloading
    from configured remotes as needed.

    Order of operations per username:
      1. Return any items already downloaded but not yet posted (pending).
      2. If still short, fetch new files from each configured provider
         (gdrive first, then dropbox), oldest-first.
      3. Stops as soon as `count` items are ready.

    Providers or remotes that fail are logged and skipped — a broken
    Dropbox never blocks a working Drive.
    """
    state = _get_state()

    ready = state.pending_for_account(username, limit=count)
    if len(ready) >= count:
        return ready[:count]

    try:
        cfg = load_account(username).get("sources", {}) or {}
    except FileNotFoundError:
        log.warning("no account config for %s", username)
        return ready
    if not cfg:
        return ready

    for provider_name in ("gdrive", "dropbox"):
        if len(ready) >= count:
            break
        pcfg = cfg.get(provider_name)
        if not pcfg:
            continue
        provider = _get_provider(provider_name)
        if provider is None:
            continue

        folder_ref = (
            pcfg.get("folder_id")       # gdrive
            or pcfg.get("folder_path")  # dropbox: path in owned Dropbox
            or pcfg.get("shared_link")  # dropbox: URL to an external folder
        )
        if not folder_ref:
            log.warning("%s: %s config missing folder ref", username, provider_name)
            continue

        try:
            known = state.known_remote_ids(provider_name, username)
            remotes = provider.list_new(folder_ref, exclude_ids=known)
        except Exception:
            log.exception("%s: %s list failed", username, provider_name)
            continue

        # Age embargo: never consume content newer than N days. Prevents the
        # bot from catching up to freshly-uploaded content that hasn't been
        # manually piloted yet. Per-account override wins over env default.
        min_age_default = float(os.environ.get("SOURCES_MIN_AGE_DAYS", "0"))
        min_age_days = float(pcfg.get("min_age_days", min_age_default))
        if min_age_days > 0:
            cutoff = int(time.time()) - int(min_age_days * 86400)
            before = len(remotes)
            remotes = [r for r in remotes if (r.modified_at or 0) <= cutoff]
            filtered = before - len(remotes)
            if filtered:
                log.info(
                    "%s: %s age filter (%.1fd) excluded %d/%d files",
                    username, provider_name, min_age_days, filtered, before,
                )

        needed = count - len(ready)
        for remote in remotes[:needed]:
            item = _download_one(provider, provider_name, username, folder_ref, remote)
            if item is not None:
                ready.append(item)

    return ready[:count]


def _download_one(provider, provider_name, username, folder_ref, remote) -> Optional[SourceItem]:
    dest_dir = _INBOX_DIR / username
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Prefix with uuid so name collisions across remotes can't clobber.
    safe_name = _sanitize(remote.name)
    dest_path = dest_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"

    try:
        provider.download(folder_ref, remote.remote_id, dest_path)
    except Exception:
        log.exception("%s: %s download of %s failed", username, provider_name, remote.name)
        # Clean partial file so a retry starts fresh
        try:
            dest_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    state = _get_state()
    return state.record_download(
        provider=provider_name,
        account=username,
        remote_id=remote.remote_id,
        remote_name=remote.name,
        local_path=str(dest_path),
    )


def mark_posted(item_id: int, ig_media_id: str) -> None:
    """Call after a successful publish. Idempotent."""
    _get_state().mark_posted(item_id, ig_media_id)


def clear_posted(account: Optional[str] = None) -> tuple[int, int]:
    """Move all `status='posted'` items to `_posted/` on their remote and
    flip status to `cleared`. Local files are also deleted from INBOX_DIR.

    Returns (moved_count, failed_count).
    Pass `account=None` to sweep all accounts.
    """
    state = _get_state()
    items = state.posted_items(account)
    if not items:
        return (0, 0)

    # Group by (provider, account) so we resolve folder_ref once per group
    from collections import defaultdict
    grouped: dict[tuple[str, str], list[SourceItem]] = defaultdict(list)
    for it in items:
        grouped[(it.provider, it.account)].append(it)

    moved = 0
    failed = 0
    for (provider_name, acct), group in grouped.items():
        provider = _get_provider(provider_name)
        if provider is None:
            failed += len(group)
            continue
        try:
            cfg = load_account(acct).get("sources", {}).get(provider_name, {})
        except FileNotFoundError:
            failed += len(group)
            continue
        folder_ref = cfg.get("folder_id") or cfg.get("folder_path")
        if not folder_ref:
            failed += len(group)
            continue

        for it in group:
            try:
                provider.mark_consumed(folder_ref, it.remote_id, it.remote_name)
            except Exception:
                log.exception("%s: mark_consumed failed for %s", acct, it.remote_name)
                failed += 1
                continue

            state.mark_cleared(it.id)
            try:
                Path(it.local_path).unlink(missing_ok=True)
            except OSError:
                pass
            moved += 1

    return (moved, failed)


def _sanitize(name: str) -> str:
    keep = "-_.() "
    return "".join(c if c.isalnum() or c in keep else "_" for c in name)[:120]
