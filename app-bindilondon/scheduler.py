"""
scheduler.py - Queue management, schedule generation, APScheduler integration.
"""

import os
import fcntl
import json
import random
import logging
import datetime as dt
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("scheduler")

DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
STATE_PATH = os.path.join(DATA_DIR, "state", "today.json")


# ---------- supported timezones ----------

TZ_ALIASES = {
    "EST": "America/New_York", "EDT": "America/New_York",
    "CST": "America/Chicago",  "CDT": "America/Chicago",
    "MST": "America/Denver",   "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "GMT": "Etc/GMT", "UTC": "Etc/UTC",
    "EAT": "Africa/Nairobi",
    "CET": "Europe/Berlin", "CEST": "Europe/Berlin",
    "IST": "Asia/Kolkata",
    "JST": "Asia/Tokyo",
    "BST": "Europe/London",
}


def resolve_timezone(name):
    if not name:
        raise ValueError("Timezone is required")
    canonical = TZ_ALIASES.get(name.upper(), name)
    try:
        return ZoneInfo(canonical)
    except Exception:
        raise ValueError(f"Unknown timezone: {name}")


# ---------- state persistence ----------

def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(state, f, indent=2, default=str)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def update_item(queue_idx, updates):
    """Atomically update one queue item's fields. Returns the updated state."""
    if not os.path.exists(STATE_PATH):
        return None
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            content = f.read()
            import json as _json
            state = _json.loads(content) if content else {}
            if "queue" in state and 0 <= queue_idx < len(state["queue"]):
                for k, v in updates.items():
                    state["queue"][queue_idx][k] = v
                f.seek(0)
                f.truncate()
                _json.dump(state, f, indent=2, default=str)
            return state
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def clear_state():
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)


# ---------- schedule input parsing ----------

def _parse_hhmm(s):
    h, m = s.split(":")
    h = int(h); m = int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return (h, m)


def parse_schedule_input(text, default_tz_name):
    parts = text.strip().split()
    if not parts:
        raise ValueError("Empty schedule input")

    tz = None
    if ":" not in parts[-1] and "-" not in parts[-1]:
        try:
            tz = resolve_timezone(parts[-1])
            parts = parts[:-1]
        except ValueError:
            raise ValueError(f"Could not parse timezone: {parts[-1]}")
    if tz is None:
        tz = resolve_timezone(default_tz_name)

    if not parts:
        raise ValueError("No times provided")

    windows = []
    for p in parts:
        if "-" in p:
            try:
                left, right = p.split("-", 1)
                start = _parse_hhmm(left)
                end = _parse_hhmm(right)
            except (ValueError, IndexError):
                raise ValueError(f"Bad range: '{p}' (expected HH:MM-HH:MM)")
            if (end[0] * 60 + end[1]) <= (start[0] * 60 + start[1]):
                raise ValueError(f"Range '{p}' end must be after start")
            windows.append((start, end))
        else:
            try:
                t = _parse_hhmm(p)
            except (ValueError, IndexError):
                raise ValueError(f"Bad time: '{p}' (expected HH:MM)")
            windows.append((t, t))

    return windows, tz


def assign_post_times(num_accounts, windows, tz, jitter_minutes=20):
    today = dt.datetime.now(tz).date()
    result = {}

    for post_num, ((sh, sm), (eh, em)) in enumerate(windows, start=1):
        start = dt.datetime.combine(today, dt.time(sh, sm), tzinfo=tz)
        end = dt.datetime.combine(today, dt.time(eh, em), tzinfo=tz)

        if start == end:
            start = start - dt.timedelta(minutes=jitter_minutes)
            end = end + dt.timedelta(minutes=jitter_minutes)

        window_seconds = max(int((end - start).total_seconds()), 60)

        used = set()
        times_list = []
        max_tries = num_accounts * 20
        tries = 0
        while len(times_list) < num_accounts and tries < max_tries:
            offset = random.randint(0, window_seconds)
            if offset not in used:
                used.add(offset)
                times_list.append(start + dt.timedelta(seconds=offset))
            tries += 1
        while len(times_list) < num_accounts:
            times_list.append(start + dt.timedelta(seconds=random.randint(0, window_seconds)))

        random.shuffle(times_list)
        result[post_num] = times_list

    return result

def assign_post_times_even(
    accounts,
    num_posts,
    tz,
    start_at=None,
    account_max_offset_min=60,
    account_min_offset_min=0,
):
    """
    Distribute num_posts posts per account evenly across 24 hours,
    starting from start_at (defaults to now in tz).

    Each account gets a random constant offset (0-60min by default) added
    to every slot time so 30 accounts don't fire at the same second.

    Returns dict: {post_num (1..N): {model_name: datetime}}
    (matches the shape returned by assign_post_times)
    """
    if num_posts < 1:
        raise ValueError("num_posts must be >= 1")

    if start_at is None:
        start_at = dt.datetime.now(tz)
    elif start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=tz)

    slot_spacing = dt.timedelta(hours=24) / num_posts

    # Each account gets its own constant offset, kept the same across all slots
    account_offsets = {}
    for acc in accounts:
        name = acc["model_name"]
        offset_min = random.randint(account_min_offset_min, account_max_offset_min)
        account_offsets[name] = dt.timedelta(minutes=offset_min)

    result = {}
    for post_num in range(1, num_posts + 1):
        base = start_at + slot_spacing * (post_num - 1)
        result[post_num] = {
            name: base + off for name, off in account_offsets.items()
        }
    return result

# ---------- queue building ----------

def build_queue(accounts, num_videos, ppa):
    queue = []
    for account_idx, acc in enumerate(accounts):
        for post_num in range(1, ppa + 1):
            video_idx = (account_idx + post_num - 1) % num_videos
            queue.append({
                "account": acc["model_name"],
                "ig_user_id": acc["ig_user_id"],
                "ig_access_token": acc.get("ig_access_token"),
                "post_num": post_num,
                "video_idx": video_idx,
                "status": "pending",
                "variant_url": None,
                "variant_r2_key": None,
                "scheduled_at": None,
                "media_id": None,
                "caption": None,
                "error": None,
            })
    return queue


def queue_summary(state):
    if not state or "queue" not in state:
        return {"total": 0}
    counts = {"total": len(state["queue"])}
    for item in state["queue"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


# ---------- APScheduler integration ----------

class CampaignScheduler:
    def __init__(self, post_callback):
        self.scheduler = AsyncIOScheduler()
        self.post_callback = post_callback
        self.started = False

    def start(self):
        if not self.started:
            self.scheduler.start()
            self.started = True
            log.info("APScheduler started")

    def stop(self):
        if self.started:
            self.scheduler.shutdown(wait=False)
            self.started = False

    def schedule_item(self, queue_item_id, run_at):
        if run_at <= dt.datetime.now(run_at.tzinfo):
            run_at = dt.datetime.now(run_at.tzinfo) + dt.timedelta(seconds=5)
        self.scheduler.add_job(
            self._fire,
            "date",
            run_date=run_at,
            args=[queue_item_id],
            id=f"post_{queue_item_id}",
            replace_existing=True,
            misfire_grace_time=600,
        )
        log.info(f"Scheduled item {queue_item_id} for {run_at.isoformat()}")

    async def _fire(self, queue_item_id):
        try:
            await self.post_callback(queue_item_id)
        except Exception:
            log.exception(f"Post callback raised for item {queue_item_id}")

    def clear_all_jobs(self):
        for job in list(self.scheduler.get_jobs()):
            try:
                job.remove()
            except Exception:
                pass

    def reschedule_pending(self, state):
        if not state or "queue" not in state:
            return
        for idx, item in enumerate(state["queue"]):
            if item["status"] != "scheduled":
                continue
            if not item.get("scheduled_at"):
                continue
            try:
                run_at = dt.datetime.fromisoformat(item["scheduled_at"])
                self.schedule_item(idx, run_at)
            except Exception as e:
                log.warning(f"Could not reschedule item {idx}: {e}")
