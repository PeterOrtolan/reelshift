import os
from datetime import datetime, timedelta
import json
import asyncio
import logging
import signal
from dotenv import load_dotenv

# Load env BEFORE importing modules that need IG_ACCESS_TOKEN
load_dotenv(os.path.join(os.environ.get("REELSHIFT_DATA_DIR", "."), ".env"))

from bot_worker import build_application, _trigger_loop_iteration
import scheduler as sched_mod
import campaign_loop as loop_mod
import insights as insights_mod
from insights_poller import run_refresh as run_insights_refresh


# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
for noisy in ("httpx", "telegram", "apscheduler", "botocore", "boto3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("main")

DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
BOT_CONFIG_PATH = os.path.join(DATA_DIR, "configs", "bot.json")


def load_bot_config():
    if not os.path.exists(BOT_CONFIG_PATH):
        raise FileNotFoundError(f"Missing {BOT_CONFIG_PATH}")
    with open(BOT_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("telegram_bot_token"):
        raise ValueError("bot.json missing telegram_bot_token")
    cfg.setdefault("allowed_telegram_user_ids", [])
    cfg.setdefault("timezone", "UTC")
    cfg.setdefault("windows", [])
    return cfg


async def run_bot():
    cfg = load_bot_config()
    log.info(f"Allowed user IDs: {cfg['allowed_telegram_user_ids']}")
    try:
        insights_mod.init_db()
        log.info("Insights DB ready")
    except Exception as e:
        log.warning(f"Insights DB init warning: {e}")
    log.info(f"Timezone: {cfg['timezone']}")
    log.info(f"Windows: {len(cfg['windows'])} configured")

    app = build_application(cfg)

    # Optional dashboard bot (read-only, password-auth)
    dashboard_app = None
    _dash_token = os.environ.get("AGENCY_BOT_TOKEN")
    _dash_pass = os.environ.get("AGENCY_BOT_PASSWORD")
    if _dash_token and _dash_pass:
        try:
            from bot_worker import build_dashboard_application
            dashboard_app = build_dashboard_application(_dash_token, _dash_pass)
            log.info("Dashboard bot configured from .env")
        except Exception as e:
            log.warning(f"Dashboard bot init failed: {e}")
            dashboard_app = None

    await app.initialize()
    await app.start()
    if dashboard_app:
        try:
            await dashboard_app.initialize()
            await dashboard_app.start()
            log.info("Dashboard bot started")
        except Exception as e:
            log.warning(f"Dashboard bot start failed: {e}")
            dashboard_app = None

    sched = app.bot_data["sched"]
    sched.start()
    try:
        default_ig_token = os.environ.get("IG_ACCESS_TOKEN", "")
        sched.scheduler.add_job(
            run_insights_refresh,
            trigger="interval",
            hours=1,
            args=[default_ig_token],
            id="insights_poller",
            replace_existing=True,
            next_run_time=datetime.now(),
        )
        log.info("Insights poller registered: every 6 hours")
    except Exception as e:
        log.warning(f"Insights poller registration failed: {e}")

    state = sched_mod.load_state()
    if state:
        sched.reschedule_pending(state)
        log.info(f"Rehydrated state: {sched_mod.queue_summary(state)}")
    else:
        log.info("No existing queue (today.json absent) — fresh start")

    # Resume variant generation if interrupted by container restart.
    # Handles the case where _run_variant_generation was killed mid-run
    # (e.g., container recreated for env var update).
    try:
        _pending_gen = sum(1 for it in state.get("queue", [])
                           if it.get("status") == "pending")
        if _pending_gen > 0:
            log.info(f"Found {_pending_gen} pending items — resuming variant generation in 15s")
            _gen_fn = app.bot_data.get("_run_variant_generation")
            _ls = loop_mod.get_state()
            _chat_id = _ls.get("chat_id") if _ls else None
            if not _chat_id:
                _chat_id = cfg.get("allowed_telegram_user_ids", [8710485277])[0]
            if _gen_fn and _chat_id:
                sched.scheduler.add_job(
                    lambda: asyncio.ensure_future(_gen_fn(app, _chat_id)),
                    trigger="date",
                    run_date=datetime.now() + timedelta(seconds=15),
                    id="resume_variant_gen",
                    replace_existing=True,
                )
    except Exception as e:
        log.warning(f"Resume variant gen check failed: {e}")

    # Auto-resume the loop on startup if it was active but the queue is
    # fully drained (or no queue exists at all).
    # Scenario: container restarts AFTER "Day complete!" fires but BEFORE
    # _trigger_loop_iteration runs. Also covers: queue was cleared (today.json
    # deleted by previous iter's auto-clear) but loop is still active —
    # next iter should be triggered. Without this, the loop would stay
    # "active but idle" forever.
    # NOTE: this block is OUTSIDE `if state:` so it runs even when today.json
    # doesn't exist (state is None). When state is None, _pending is 0 (empty
    # queue), so the trigger fires correctly.
    try:
        if loop_mod.is_active():
            _pending = sum(
                1 for it in (state.get("queue", []) if state else [])
                if it.get("status") not in ("posted", "failed", "cleared")
            )
            if _pending == 0:
                log.info("Loop active + queue drained on startup → scheduling resume in 10s")
                sched.scheduler.add_job(
                    _trigger_loop_iteration,
                    trigger="date",
                    run_date=datetime.now() + timedelta(seconds=10),
                    args=[app],
                    id="loop_resume_on_startup",
                    replace_existing=True,
                )
    except Exception as e:
        log.warning(f"Loop auto-resume check failed: {e}")

    await app.updater.start_polling(drop_pending_updates=True)
    if dashboard_app:
        try:
            await dashboard_app.updater.start_polling(drop_pending_updates=True)
            log.info("Dashboard bot polling started")
        except Exception as e:
            log.warning(f"Dashboard polling failed: {e}")
    log.info("Bot is live. Press Ctrl+C to stop.")

    stop_event = asyncio.Event()

    def _stop(*_):
        log.info("Shutdown signal received...")
        stop_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                pass
    except Exception:
        pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("Stopping bot...")
        try:
            sched.stop()
        except Exception as e:
            log.warning(f"Scheduler stop warning: {e}")
        if dashboard_app:
            try:
                await dashboard_app.updater.stop()
                await dashboard_app.stop()
                await dashboard_app.shutdown()
            except Exception as e:
                log.warning(f"Dashboard shutdown warning: {e}")
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as e:
            log.warning(f"Shutdown warning: {e}")
        log.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
