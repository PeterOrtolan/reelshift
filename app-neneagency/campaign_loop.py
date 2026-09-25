"""Auto-loop state persistence for reelshift-graph campaigns.

Loop state lives at /data/state/loop_state.json. Survives container restart.

State schema:
    {
        "active": bool,
        "mode": "even24" | "bare",
        "N": int | null,          # posts/account for even24; null for bare
        "chat_id": int,
        "iteration_count": int,
        "last_iteration_started_at": str,
        "last_error": str | null,
    }

Modes:
  even24 — /igcampaign N loop; pulls N videos, spreads across 24h
  bare   — /igcampaign loop; pulls 3 videos, 2 posts/acc in 9-12 and 14-18 NY windows
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_STATE_PATH = Path(os.environ.get(
    "CAMPAIGN_LOOP_STATE", "/data/state/loop_state.json"
))


def _default_state() -> dict:
    return {
        "active": False,
        "mode": "even24",
        "N": None,
        "chat_id": None,
        "iteration_count": 0,
        "last_iteration_started_at": None,
        "last_error": None,
    }


def get_state() -> dict:
    if not _STATE_PATH.is_file():
        return _default_state()
    try:
        import fcntl
        with _STATE_PATH.open() as fh:
            # Shared lock while reading prevents reading a half-written file
            # when multiple concurrent writers (e.g., 10 _post_cb_v3 triggers
            # firing in the same second) are racing on the state file.
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                data = json.load(fh)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("loop_state: failed to load: %s", e)
        return _default_state()
    base = _default_state()
    base.update(data)
    return base


def _save(state: dict) -> None:
    """Persist loop state with exclusive file lock.

    Without locking, concurrent writes (e.g., 10 posts firing in the same
    second each calling loop_mod.stop()) can interleave and corrupt the
    JSON file. The matching shared-lock in get_state() ensures readers
    never see a half-written file.
    """
    import fcntl
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _STATE_PATH.open("w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(state, fh, indent=2)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def is_active() -> bool:
    return get_state()["active"]


def start(N: int, chat_id: int) -> None:
    """Begin (or restart) the loop in even24 mode."""
    state = _default_state()
    state["active"] = True
    state["mode"] = "even24"
    state["N"] = N
    state["chat_id"] = chat_id
    state["iteration_count"] = 0
    state["last_error"] = None
    _save(state)


def start_bare(chat_id: int) -> None:
    """Begin (or restart) the loop in bare mode (3 videos, 2 posts/acc, windows)."""
    state = _default_state()
    state["active"] = True
    state["mode"] = "bare"
    state["N"] = None
    state["chat_id"] = chat_id
    state["iteration_count"] = 0
    state["last_error"] = None
    _save(state)


def record_iteration_start() -> None:
    state = get_state()
    state["iteration_count"] += 1
    state["last_iteration_started_at"] = _now_iso()
    _save(state)


def stop(reason: Optional[str] = None) -> None:
    """Stop the loop; optionally record a reason (surfaced via /loopstatus)."""
    state = get_state()
    state["active"] = False
    if reason:
        state["last_error"] = reason
    _save(state)


def set_n(N: int) -> None:
    """Update N for next iteration. If currently bare, switches to even24."""
    state = get_state()
    if not state["active"]:
        raise RuntimeError("no active loop — start one with /igcampaign N first")
    state["N"] = N
    state["mode"] = "even24"
    _save(state)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
