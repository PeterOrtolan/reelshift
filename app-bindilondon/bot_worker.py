import os
import hikerapi_client
import rocketapi_client
import library
import re
import json
import asyncio
import logging
import tempfile
import time
import datetime as dt
from functools import wraps

import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import captions
import covers
import vault
import campaign_loop as loop_mod
import scrape_targets
import apify_client
import instagram_direct
import accounts as accounts_mod
import videos as videos_mod
import link_downloader as link_dl_mod
import iglogin_client as iglogin_mod
import scheduler as sched_mod
import insights as insights_mod
from r2 import upload_file, delete_file
from video import process_video  # used by manual /postnow path

log = logging.getLogger("bot_worker")
TOKEN = os.getenv("IG_ACCESS_TOKEN")
BASE_URL = "https://graph.facebook.com/v25.0"

_NUMBERING_RE = re.compile(r"^\s*(?:\d+[.):\]]\s+|[-*•]\s+)")


def _strip_numbering(line):
    return _NUMBERING_RE.sub("", line).strip()


# ---------------- auth ----------------

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        allowed = context.application.bot_data.get("allowed_user_ids", [])
        user_id = update.effective_user.id
        if allowed and user_id not in allowed:
            target = update.message or (update.callback_query and update.callback_query.message)
            if target:
                await target.reply_text(f"⛔ Not authorized. Your Telegram ID: {user_id}")
            log.warning(f"Rejected user {user_id}")
            return
        return await func(update, context)
    return wrapper


# ---------------- helpers ----------------

def _admin_chat_id(context):
    """Return first allowed user id (used for proactive notifications)."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    return allowed[0] if allowed else None


# ---------------- basic commands ----------------


# --- cross-campaign shortcode tracking (used by /igcampaign auto-drain) ---
USED_SHORTCODES_PATH = os.path.join(
    os.environ.get("REELSHIFT_DATA_DIR", "."), "used_shortcodes.json"
)

def _load_used_shortcodes():
    try:
        with open(USED_SHORTCODES_PATH) as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
        return set(data.get("shortcodes", []))
    except Exception:
        return set()

def _save_used_shortcodes(codes):
    try:
        d = os.path.dirname(USED_SHORTCODES_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(USED_SHORTCODES_PATH, "w") as f:
            json.dump(sorted(codes), f, indent=2)
    except Exception as e:
        logging.getLogger(__name__).warning("could not save used_shortcodes: %s", e)


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accs = accounts_mod.list_accounts()
    s = captions.pool_status()
    state = sched_mod.load_state() or {}
    qsummary = sched_mod.queue_summary(state)

    msg = (
        f"👋 *Reelshift bot ready*\n\n"
        f"Accounts: {len(accs)}\n"
        f"Caption pool: {s['used']}/{s['total']} used, {s['remaining']} remaining\n"
        f"Today's queue: {qsummary.get('total', 0)} items "
        f"({qsummary.get('posted', 0)} posted, {qsummary.get('scheduled', 0)} scheduled)\n\n"
        f"*Daily flow:*\n"
        f"1. /igcampaign → send videos → /igcampaigngo\n"
        f"2. /igschedule → set times\n"
        f"3. Watch /today for status\n\n"
        f"Type /help for full command list."
    )
    await update.message.reply_text(msg, )


@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "*Account commands*\n"
        "/myig — list IG accounts\n"
        "/igadd `<json>` — add account\n"
        "/igremove `<name>` — remove account\n\n"
        "*Caption commands*\n"
        "/igcaption `<text>` — add caption (alone for bulk)\n"
        "/captions — list captions\n"
        "/removecaption `<idx>` — delete\n"
        "/poolstatus — pool stats\n"
        "/resetpool — reset cycle\n\n"
        "*Campaign commands*\n"
        "/igcampaign — start collecting videos\n"
        "/igcampaigngo — generate variants\n"
        "/igschedule — set posting times\n"
        "/igqueuestatus — queue summary\n"
        "/igqueueclear — wipe queue + variants\n"
        "/today — brief status\n"
        "/today verbose — full per-account schedule\n"
        "/postnow — manual posting (backup)\n"
        "/cancel — cancel pending input"
    )
    await update.message.reply_text(msg, )


@admin_only
async def cmd_myig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accs = accounts_mod.list_accounts()
    if not accs:
        await update.message.reply_text("No accounts. /igadd `{json}`", )
        return
    lines = [f"• @{a['model_name']} (`{a['ig_user_id']}`)" for a in accs]
    await update.message.reply_text("📋 Accounts:\n" + "\n".join(lines), )


@admin_only
async def cmd_igadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        await update.message.reply_text(
            "Usage:\n`/igadd {\"model_name\":\"name\",\"ig_user_id\":\"178410...\"}`",
            
        )
        return
    # If no ig_access_token in payload, use the session active token (if set)
    try:
        _cfg_dict = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _cfg_dict = None
    if isinstance(_cfg_dict, dict) and "ig_access_token" not in _cfg_dict:
        _active = context.application.bot_data.get("active_token")
        if _active:
            _cfg_dict["ig_access_token"] = _active
            raw = json.dumps(_cfg_dict)

    try:
        cfg = accounts_mod.add_account(raw)
        await update.message.reply_text(
            f"✅ Added @{cfg['model_name']} (`{cfg['ig_user_id']}`)",
            
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


@admin_only
async def cmd_igremove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /igremove <model_name>")
        return
    name = context.args[0]
    if accounts_mod.remove_account(name):
        await update.message.reply_text(f"🗑️ Removed @{name}")
    else:
        await update.message.reply_text(f"❌ @{name} not found")


# ---------------- captions ----------------

@admin_only
async def cmd_igcaption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if text:
        try:
            idx = captions.add_caption(text)
            await update.message.reply_text(f"✅ Added caption #{idx}")
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
    else:
        context.user_data["awaiting_captions"] = True
        await update.message.reply_text(
            "📥 Send all captions, one per line:\n\n"
            "1. Caption one\n"
            "2. Caption two\n\n"
            "Numbers and bullets stripped automatically.\nSend /cancel to abort."
        )


@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    canceled = []
    if context.user_data.pop("awaiting_captions", None):
        canceled.append("caption import")
    if context.user_data.pop("awaiting_schedule", None):
        canceled.append("schedule input")
    if context.user_data.pop("awaiting_covers", None):
        canceled.append("cover upload")

    if canceled:
        await update.message.reply_text("❌ Cancelled: " + ", ".join(canceled))
    else:
        await update.message.reply_text("Nothing to cancel.")


@admin_only
async def cmd_captions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = captions.list_captions()
    if not items:
        await update.message.reply_text("Pool empty. /igcaption `<text>`",
                                        )
        return
    lines = [f"`{i:>2}` [{'✓' if used else ' '}] {t}" for i, t, used in items]
    await _send_chunked(update.message, lines)


async def _send_chunked(msg, lines, ):
    text = "\n".join(lines)
    if len(text) <= 3900:
        await msg.reply_text(text)
        return
    chunk, size = [], 0
    for line in lines:
        if size + len(line) + 1 > 3900:
            await msg.reply_text("\n".join(chunk))
            chunk, size = [line], len(line)
        else:
            chunk.append(line)
            size += len(line) + 1
    if chunk:
        await msg.reply_text("\n".join(chunk))


@admin_only
async def cmd_removecaption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removecaption <index>")
        return
    try:
        idx = int(context.args[0])
        removed = captions.remove_caption(idx)
        await update.message.reply_text(f"✅ Removed: {removed}")
    except (ValueError, IndexError) as e:
        await update.message.reply_text(f"❌ {e}")


@admin_only
async def cmd_poolstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = captions.pool_status()
    await update.message.reply_text(
        f"📊 {s['used']}/{s['total']} used, {s['remaining']} remaining"
    )


@admin_only
async def cmd_resetpool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    captions.reset_used()
    await update.message.reply_text("🔄 Pool reset")


# ---- Loop trigger resilience helpers (added to enable automode) ----
# Same code as in app/, app-agency_a/, app-neneagency/, elvie-graph/app/ —
# see those files for full rationale.
_trigger_lock = asyncio.Lock()


async def _send_loop_notify(bot, chat_id, text, max_attempts=3):
    """Best-effort Telegram notification with RetryAfter retry.
    Returns True on success, False on failure. Always non-fatal."""
    from telegram.error import RetryAfter
    for attempt in range(max_attempts):
        try:
            await bot.send_message(chat_id, text)
            return True
        except RetryAfter as e:
            if attempt + 1 == max_attempts:
                log.warning("loop notify: flood control, max attempts (%d) reached; skipping", max_attempts)
                return False
            wait = e.retry_after + 1
            log.info("loop notify: flood control, waiting %ds before retry %d/%d", wait, attempt + 1, max_attempts)
            await asyncio.sleep(wait)
        except Exception as e:
            log.warning("loop notify: send failed (non-fatal): %s", e)
            return False
    return False


async def _trigger_loop_iteration(app):
    """Auto-start the next campaign iteration after the last post fires.

    Constructs a fake /igcampaign N Update and dispatches through the normal
    handler chain. For bindilondon, only even24 mode is supported (since bare
    mode in this fork = manual upload, no auto-pull).

    Resilience features (same as other forks):
      1. Dedupe lock: only one trigger runs at a time.
      2. Iteration runs FIRST, notification LAST.
      3. Notification is best-effort.
      4. RetryAfter is retried with the recommended delay.
    """
    import time as _time
    from datetime import datetime as _dt
    from telegram import Update, Message, Chat, User, MessageEntity

    state = loop_mod.get_state()
    if not state.get("active"):
        return
    chat_id = state.get("chat_id")
    mode = state.get("mode", "even24")
    N = state.get("N")
    if not chat_id:
        log.warning("loop trigger: state missing chat_id: %s", state)
        loop_mod.stop("state missing chat_id")
        return
    if mode == "even24" and not N:
        log.warning("loop trigger: even24 mode missing N: %s", state)
        loop_mod.stop("state missing N for even24")
        return
    if mode == "bare":
        log.warning("loop trigger: bare mode not supported in bindilondon (uses library, not auto-pull); stopping loop")
        loop_mod.stop("bare mode unsupported in bindilondon fork")
        return

    if _trigger_lock.locked():
        log.info("loop trigger: another iteration in progress, skipping this call")
        return
    async with _trigger_lock:
        loop_mod.record_iteration_start()
        it = loop_mod.get_state()["iteration_count"]
        log.info("loop: triggering iteration %d (even24, N=%d)", it, N)
        _notify = f"🔁 Loop iteration {it}: auto-pulling {N} new reel(s) from library (even24)..."
        _cmd_text = f"/igcampaign {N}"

        try:
            user = User(id=chat_id, is_bot=False, first_name="loop")
            chat = Chat(id=chat_id, type=Chat.PRIVATE)
            message = Message(
                message_id=int(_time.time()),
                date=_dt.now(),
                chat=chat,
                from_user=user,
                text=_cmd_text,
                entities=(MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len("/igcampaign")),),
            )
            message.set_bot(app.bot)
            update = Update(update_id=int(_time.time()), message=message)
            update.set_bot(app.bot)
            await app.process_update(update)
        except Exception as e:
            log.exception("loop iteration trigger failed")
            loop_mod.stop(f"iteration trigger raised: {e}")
            await _send_loop_notify(
                app.bot,
                chat_id,
                f"❌ Auto-loop crashed while triggering iteration {it}.\n"
                f"Loop stopped. Restart with /igcampaign N.\n\n"
                f"Error: {e}"
            )
            return

        await _send_loop_notify(app.bot, chat_id, _notify)


@admin_only
async def cmd_stoploop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stoploop — halt the auto-loop; current scheduled posts still fire."""
    state = loop_mod.get_state()
    if not state.get("active"):
        await update.message.reply_text("ℹ️ Loop is already stopped.")
        return
    loop_mod.stop("stopped by user")
    it = state.get("iteration_count", 0)
    await update.message.reply_text(
        f"🛑 Loop stopped after {it} iteration(s).\n"
        f"Currently scheduled posts will still fire, but no new campaign "
        f"will auto-start. Use /igcampaign N to start a new loop."
    )


@admin_only
async def cmd_setn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setn M — change loop N without triggering a campaign now."""
    if not context.args:
        await update.message.reply_text("Usage: /setn <N>")
        return
    try:
        new_n = int(context.args[0])
    except ValueError:
        await update.message.reply_text(f"❌ Bad N: {context.args[0]!r}")
        return
    if new_n < 1 or new_n > 500:
        await update.message.reply_text(f"❌ N must be 1..500, got {new_n}")
        return
    loop_mod.set_n(new_n)
    await update.message.reply_text(f"✅ Loop N is now {new_n}. Takes effect on next iteration.")


@admin_only
async def cmd_loopstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/loopstatus — show current loop state."""
    state = loop_mod.get_state()
    if not state.get("active"):
        await update.message.reply_text(
            "🛑 *Loop: stopped*\n"
            f"Start one with `/igcampaign N` (N = posts/account across 24h).",
            parse_mode="Markdown",
        )
        return
    it = state.get("iteration_count", 0)
    last = state.get("last_iteration_started_at", "?")
    err = state.get("last_error")
    msg = (
        f"🔁 *Loop: active*\n"
        f"Mode: even24\n"
        f"N: {state.get('N')}\n"
        f"Iterations so far: {it}\n"
        f"Last started: {last}\n"
        f"Last error: {err or 'none'}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


@admin_only
async def cmd_looptrigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DEBUG: manually invoke _trigger_loop_iteration for testing."""
    state = loop_mod.get_state()
    if not state.get("active"):
        await update.message.reply_text(
            "ℹ️ Loop is not active. Use /igcampaign N to start one first, "
            "then /looptrigger to simulate iteration completion."
        )
        return
    await update.message.reply_text("🧪 Manually triggering loop iteration...")
    await _trigger_loop_iteration(context.application)


# ---------------- campaign collection ----------------

@admin_only
async def cmd_igcampaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enter collection mode. Subsequent videos go into today's source pool."""
    # NEW: /igcampaign [N] — parse optional post count for even-24h mode
    _n_arg = update.message.text.partition(" ")[2].strip()
    igcampaign_n = None
    if _n_arg:
        try:
            igcampaign_n = int(_n_arg)
        except ValueError:
            await update.message.reply_text(f"❌ Bad N: {_n_arg!r}. Use /igcampaign or /igcampaign N (integer).")
            return
        if igcampaign_n < 1 or igcampaign_n > 500:
            await update.message.reply_text(f"❌ N must be 1..500, got {igcampaign_n}")
            return
    context.user_data["igcampaign_n"] = igcampaign_n
    # Also stash in app-level bot_data so background completion handler can read it
    context.application.bot_data["igcampaign_n"] = igcampaign_n
    
    state = sched_mod.load_state() or {}
    # Auto-clear residue (posted/failed items) instead of blocking.
    # Only block if there are PENDING items (not yet posted).
    _pending_active = any(
        item.get("status") not in ("posted", "failed", "cleared")
        for item in state.get("queue", [])
    )
    if _pending_active:
        await update.message.reply_text(
            "⚠️ A campaign is in progress.\n"
            "Use /igqueueclear to wipe it before starting a new one."
        )
        return
    # Auto-clear residue if the queue is done but not manually cleared —
    # keeps sched state, R2, and sources aligned for the fresh pull below.
    if state.get("queue"):
        _scheduler = context.application.bot_data.get("sched")
        if _scheduler is not None:
            _scheduler.clear_all_jobs()
        for _item in state["queue"]:
            _key = _item.get("variant_r2_key")
            if _key:
                try:
                    delete_file(_key)
                except Exception:
                    pass
        try:
            videos_mod.clear_sources()
        except Exception:
            log.exception("auto-clear: videos_mod.clear_sources failed")
        sched_mod.clear_state()
        context.application.bot_data["collecting"] = False
        log.info("igcampaign: auto-cleared %d completed queue items", len(state["queue"]))
        await update.message.reply_text(
            f"🗑️ Auto-cleared {len(state['queue'])} completed items from previous campaign."
        )

    # Auto-drain from library when N is given
    if igcampaign_n:
        used = _load_used_shortcodes()
        picks = library.pick_reels(igcampaign_n, exclude_shortcodes=used)
        if not picks:
            await update.message.reply_text(
                "❌ Library empty (or all reels already used).\n"
                "Run /sync first, or /igcampaign (no N) for manual upload."
            )
            return
        if len(picks) < igcampaign_n:
            await update.message.reply_text(
                f"⚠️ Library only has {len(picks)} unused reel(s). Capping N to {len(picks)}."
            )
            igcampaign_n = len(picks)
            context.user_data["igcampaign_n"] = igcampaign_n
            context.application.bot_data["igcampaign_n"] = igcampaign_n
        added = 0
        new_used = set(used)
        for reel in picks:
            try:
                videos_mod.add_source_video(reel["abs_path"])
                new_used.add(reel["ig_shortcode"])
                added += 1
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "add_source_video(%s) failed: %s", reel.get("ig_shortcode"), e
                )
        _save_used_shortcodes(new_used)
        await update.message.reply_text(
            f"📚 Loaded {added} reel(s) from library.\n"
            f"📅 Even-24h mode: {igcampaign_n} posts/account, spacing = {24/igcampaign_n:.2f}h.\n\n"
            f"Auto-running /igcampaigngo to generate variants + schedule + start loop..."
        )
        # Auto-run /igcampaigngo so the flow is fully autonomous:
        # /igcampaign N → pull reels → generate variants → schedule → start loop.
        # User no longer needs to manually type /igcampaigngo.
        try:
            await cmd_igcampaigngo(update, context)
        except Exception as e:
            log.exception("auto igcampaigngo failed after library load")
            try:
                await update.message.reply_text(
                    f"❌ Auto /igcampaigngo failed: {e}\n"
                    f"Run /igcampaigngo manually to continue."
                )
            except Exception:
                pass
        return

    # Fallback: manual upload mode when no N is given
    context.application.bot_data["collecting"] = True
    n = len(videos_mod.list_sources())
    await update.message.reply_text(
        f"📥 Send videos one at a time.\n"
        f"Already collected: {n}\n\n"
        f"When done, use /igcampaigngo."
    )


async def handle_campaign_video(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                media):
    """Download a video sent during collection mode."""
    msg = update.message
    status = await msg.reply_text("📥 Downloading...")

    tmpdir = tempfile.mkdtemp(prefix="reelshift_dl_")
    raw_path = os.path.join(tmpdir, "input.mp4")
    try:
        tg_file = await media.get_file()
        await tg_file.download_to_drive(raw_path)
    except Exception as e:
        await status.edit_text(f"❌ Download failed: {e}")
        return

    try:
        new_path = videos_mod.add_source_video(raw_path)
    finally:
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass

    n = len(videos_mod.list_sources())
    await status.edit_text(
        f"✅ Video {n} added!\nSend more or /igcampaigngo to start the campaign."
    )


@admin_only
async def cmd_igcampaigngo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger variant generation."""
    sources = videos_mod.list_sources()
    accs = accounts_mod.list_accounts()
    bot_cfg = context.application.bot_data.get("bot_config", {})
    # NEW: /igcampaign N override — variant count matches scheduled slots
    igcampaign_n_override = context.user_data.get("igcampaign_n")
    if igcampaign_n_override:
        ppa = igcampaign_n_override
    else:
        ppa = len(bot_cfg.get("windows", []))

    if not sources:
        await update.message.reply_text("❌ No videos collected. /igcampaign first.")
        return
    if not accs:
        await update.message.reply_text("❌ No accounts. /igadd first.")
        return
    if ppa < 1:
        await update.message.reply_text("❌ No windows configured in bot.json.")
        return

    state = sched_mod.load_state() or {}
    if state.get("queue"):
        await update.message.reply_text(
            "⚠️ Queue already exists. Use /igqueueclear to start fresh."
        )
        return

    context.application.bot_data["collecting"] = False
    queue = sched_mod.build_queue(accs, len(sources), ppa)
    total = len(queue)

    state = {
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ppa": ppa,
        "sources": sources,
        "queue": queue,
        "schedule_set": False,
    }
    sched_mod.save_state(state)

    await update.message.reply_text(
        f"⚙️ *Pre-processing {total} unique variants...*\n\n"
        f"Videos: *{len(sources)}*\n"
        f"Accounts: *{len(accs)}*\n"
        f"Posts per account: *{ppa}*\n\n"
        f"I'll notify you as variants are processed.",
        
    )

    chat_id = update.effective_chat.id
    asyncio.create_task(_run_variant_generation(context.application, chat_id))


async def _run_variant_generation(app, chat_id):
    """Generate variants in background, posting progress to chat_id."""
    state = sched_mod.load_state() or {}
    if not state.get("queue"):
        return

    queue = state["queue"]
    sources = state["sources"]
    total = len(queue)
    done = 0
    failures = 0
    last_progress_at = 0

    for idx, item in enumerate(queue):
        if item["status"] != "pending":
            done += 1
            continue
        try:
            src = sources[item["video_idx"]]
            url, key = await asyncio.to_thread(
                videos_mod.make_variant_and_upload, src,
                tempfile.gettempdir()
            )
            item["variant_url"] = url
            item["variant_r2_key"] = key
            item["status"] = "variant_ready"
            done += 1
        except Exception as e:
            log.exception("Variant generation failed")
            item["status"] = "failed"
            item["error"] = f"variant: {e}"
            failures += 1

        # Persist state every iteration (resume-safe)
        sched_mod.save_state(state)

        # Progress message every 10 done OR at completion
        if done - last_progress_at >= 10 or (done + failures) == total:
            try:
                await app.bot.send_message(
                    chat_id,
                    f"Processed *{done}/{total}* variants" +
                    (f" • {failures} failed" if failures else "")
                    , 
                )
            except Exception:
                pass
            last_progress_at = done

    # Final message + auto-schedule branch (even-24h if N was set)
    _n = app.bot_data.pop("igcampaign_n", None)
    try:
        if failures == 0:
            if _n:
                await app.bot.send_message(
                    chat_id,
                    f"✅ All {total} variants ready. Auto-scheduling ({_n} posts/account across 24h)..."
                )
            else:
                await app.bot.send_message(
                    chat_id,
                    f"✅ All {total} variants ready.\n\nRun /igschedule to set times."
                )
        else:
            if _n:
                await app.bot.send_message(
                    chat_id,
                    f"⚠️ Done: {done} ready, {failures} failed. Auto-scheduling the ready items ({_n} posts/account across 24h)..."
                )
            else:
                await app.bot.send_message(
                    chat_id,
                    f"⚠️ Done: {done} ready, {failures} failed.\n"
                    f"Run /igschedule to schedule the {done} ready items."
                )
    except Exception:
        pass

    if _n:
        try:
            await _auto_schedule_even24(app, chat_id, _n)
            # NEW: start the auto-loop after auto-schedule succeeds.
            # This is the entry point for automode — once started, the loop
            # triggers the next iteration automatically when posts finish.
            try:
                _loop_state = loop_mod.get_state()
                if not _loop_state.get("active") or _loop_state.get("mode") != "even24" or _loop_state.get("N") != _n:
                    loop_mod.start(N=_n, chat_id=chat_id)
                    loop_mod.record_iteration_start()
                    await app.bot.send_message(
                        chat_id,
                        f"🔁 Auto-loop started (even24, N={_n}).\n"
                        f"Next campaign auto-starts when this one finishes posting.\n"
                        f"Commands: /stoploop /setn M /loopstatus",
                        parse_mode="Markdown",
                    )
                elif _loop_state.get("N") != _n:
                    loop_mod.set_n(_n)
                    await app.bot.send_message(
                        chat_id,
                        f"🔁 Loop N updated: {_loop_state['N']} → {_n}.",
                    )
            except Exception as _le:
                log.exception("loop start failed (non-fatal, campaign still ran)")
                try:
                    await app.bot.send_message(
                        chat_id,
                        f"⚠️ Campaign scheduled, but auto-loop start failed: {_le}\n"
                        f"Use /loopstatus to check, or restart with /igcampaign N."
                    )
                except Exception:
                    pass
        except Exception as e:
            log.exception("Auto-schedule failed")
            try:
                await app.bot.send_message(chat_id, f"❌ Auto-schedule error: {e}\n\nFall back to /igschedule.")
            except Exception:
                pass


# ---------------- schedule ----------------

@admin_only
async def cmd_igschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for posting times."""
    state = sched_mod.load_state() or {}
    if not state.get("queue"):
        await update.message.reply_text(
            "❌ No campaign queue. Run /igcampaign + /igcampaigngo first."
        )
        return

    if state.get("schedule_set"):
        await update.message.reply_text(
            "⚠️ Schedule already set for today's queue.\n"
            "Use /igqueueclear to wipe and restart."
        )
        return

    bot_cfg = context.application.bot_data.get("bot_config", {})
    ppa = state.get("ppa", len(bot_cfg.get("windows", [])))
    default_tz = bot_cfg.get("timezone", "UTC")
    default_times = " ".join(
        f"{w['start']}-{w['end']}" for w in bot_cfg.get("windows", [])[:ppa]
    )

    # NEW: even-24h auto-schedule if N was set on /igcampaign
    _n = context.user_data.pop("igcampaign_n", None)
    context.application.bot_data.pop("igcampaign_n", None)  # keep both stores in sync
    if _n:
        await _auto_schedule_even24(context.application, update.effective_chat.id, _n)
        return
    context.user_data["awaiting_schedule"] = True
    await update.message.reply_text(
        f"📅 Send times and timezone:\n\n"
        f"*Example: {default_times} {default_tz.split('/')[-1] if '/' in default_tz else default_tz}*\n\n"
        f"Need *{ppa}* time(s) (one per posting window).\n"
        f"Timezones: EST, CST, MST, PST, GMT, UTC, EAT, CET, IST, JST\n\n"
        f"/cancel to abort.",
        
    )


async def _auto_schedule_even24(app, chat_id, num_posts: int):
    """
    Auto-schedule variants across 24h from now, N posts per account,
    per-account random 0-60min offset. Skips the VA time prompt entirely.
    Callable from background tasks (only needs app + chat_id, not update/context).
    """
    import datetime as _dt

    state = sched_mod.load_state() or {}
    if not state.get("queue"):
        await app.bot.send_message(chat_id, "❌ Queue disappeared before scheduling.")
        return

    bot_cfg = app.bot_data.get("bot_config", {})
    tz = sched_mod.resolve_timezone(bot_cfg.get("timezone", "UTC"))
    start_at = _dt.datetime.now(tz)

    accounts_list = accounts_mod.list_accounts()
    times_by_post = sched_mod.assign_post_times_even(
        accounts_list, num_posts=num_posts, tz=tz, start_at=start_at,
    )

    scheduler_inst = app.bot_data["sched"]
    earliest = None
    latest = None
    scheduled_count = 0
    for queue_idx, item in enumerate(state["queue"]):
        if item["status"] not in ("variant_ready", "scheduled"):
            continue
        pn = item.get("post_num")
        acc = item.get("account")
        if pn not in times_by_post or acc not in times_by_post[pn]:
            continue
        run_at = times_by_post[pn][acc]
        state["queue"][queue_idx]["scheduled_at"] = run_at.isoformat()
        state["queue"][queue_idx]["status"] = "scheduled"
        scheduler_inst.schedule_item(queue_idx, run_at)
        scheduled_count += 1
        if earliest is None or run_at < earliest:
            earliest = run_at
        if latest is None or run_at > latest:
            latest = run_at

    state["schedule_set"] = True
    state["schedule_mode"] = "even24"
    sched_mod.save_state(state)

    msg = f"✅ *Even-24h scheduled!*\n"
    msg += f"\nPosts per account: {num_posts}"
    msg += f"\nSpacing: {24/num_posts:.2f}h per account"
    msg += f"\nQueue items scheduled: {scheduled_count}"
    if earliest and latest:
        msg += f"\n\nFirst post: {earliest.strftime('%H:%M %Z')}"
        msg += f"\nLast post: {latest.strftime('%H:%M %Z')}"
    try:
        await app.bot.send_message(chat_id, msg, parse_mode="Markdown")
    except Exception:
        await app.bot.send_message(chat_id, msg)



async def cmd_igforceschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """RESCUE: force even-24h scheduling on current queue.
    Usage: /igforceschedule [N]  — defaults to state's ppa if omitted."""
    # inline admin check (no decorator, keep simple)
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("⛔ Not authorized.")
        return
    state = sched_mod.load_state() or {}
    if not state.get("queue"):
        await update.message.reply_text("❌ No queue to schedule.")
        return
    if state.get("schedule_set"):
        await update.message.reply_text("⚠️ Schedule already set. /igqueueclear first.")
        return
    _arg = update.message.text.partition(" ")[2].strip()
    if _arg:
        try:
            n = int(_arg)
        except ValueError:
            await update.message.reply_text(f"❌ Bad N: {_arg!r}. Use /igforceschedule or /igforceschedule <int>.")
            return
    else:
        n = state.get("ppa")
        if not n:
            await update.message.reply_text("❌ No N in state. Pass explicitly: /igforceschedule <int>.")
            return
    if n < 1 or n > 500:
        await update.message.reply_text(f"❌ N must be 1..500, got {n}")
        return
    await _auto_schedule_even24(context.application, update.effective_chat.id, n)



async def cmd_addlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download videos from a Dropbox or Google Drive shared link (file or folder)."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    url = update.message.text.partition(" ")[2].strip()
    if not url:
        await update.message.reply_text(
            "Usage: /addlink <url>\n\n"
            "Supports:\n"
            "- Dropbox file or folder (public share link)\n"
            "- Google Drive file or folder (public share link)"
        )
        return
    dl = context.application.bot_data.get("link_downloader")
    if dl is None:
        await update.message.reply_text("Link downloader not initialized.")
        return
    try:
        provider, kind, _ = dl.detect(url)
    except ValueError as e:
        await update.message.reply_text(f"URL error: {e}")
        return
    await update.message.reply_text(f"Downloading from {provider} ({kind})...")
    tmp_dir = tempfile.mkdtemp(prefix="addlink_")
    added = 0
    skipped = 0
    errors = 0
    error_msgs = []
    def _do_download():
        return list(dl.download(url, tmp_dir))
    try:
        items = await asyncio.to_thread(_do_download)
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        return
    for item in items:
        if item["status"] == "skipped":
            skipped += 1
        elif item["status"] == "error":
            errors += 1
            error_msgs.append(f"{item.get('name','?')}: {item.get('error','?')}")
        elif item["status"] == "ok":
            try:
                await asyncio.to_thread(videos_mod.add_source_video, item["path"])
                dl.mark_used(item["source_id"], item["name"])
                added += 1
                try:
                    os.remove(item["path"])
                except Exception:
                    pass
            except Exception as e:
                errors += 1
                error_msgs.append(f"{item['name']}: add_source: {e}")
    try:
        os.rmdir(tmp_dir)
    except Exception:
        pass
    msg = f"Added: {added} video(s) to source pool."
    if skipped:
        msg += f"\nSkipped (already used): {skipped}"
    if errors:
        msg += f"\nErrors: {errors}"
        if error_msgs:
            msg += "\n\n" + "\n".join(error_msgs[:5])
    total_pool = len(videos_mod.list_sources())
    msg += f"\n\nPool total: {total_pool}"
    await update.message.reply_text(msg)




async def cmd_postnowigtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """TEST: Post a video via IG Login using a direct public MP4 URL.
    Usage: /postnowigtest <video_url> [caption]
    Posts to the first connected IG Login account."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    ig = context.application.bot_data.get("iglogin")
    if ig is None:
        await update.message.reply_text("IG Login OAuth not configured.")
        return
    tokens = ig.list_tokens()
    if not tokens:
        await update.message.reply_text("No IG Login accounts. Use /igconnect first.")
        return
    args_str = update.message.text.partition(" ")[2].strip()
    if not args_str:
        await update.message.reply_text(
            "Usage: /postnowigtest <video_url> [caption]\n\n"
            "URL must be a public MP4 (Instagram fetches it)."
        )
        return
    parts = args_str.split(" ", 1)
    video_url = parts[0]
    caption = parts[1] if len(parts) > 1 else ""
    target = tokens[0]
    user_id = target["user_id"]
    username = target.get("username", user_id)
    await update.message.reply_text(
        f"Posting via IG Login to @{username}...\n\n"
        f"Video: {video_url}\n"
        f"Caption: {caption if caption else '(none)'}\n\n"
        f"This takes 30-60s (Instagram processes the video)."
    )
    def _do_post():
        return ig.publish_reel(user_id, video_url, caption=caption)
    try:
        media_id = await asyncio.to_thread(_do_post)
    except Exception as e:
        await update.message.reply_text(f"Post failed: {e}")
        return
    await update.message.reply_text(
        f"POSTED!\n\nMedia ID: {media_id}\nView: https://instagram.com/{username}"
    )



async def cmd_igrefresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger IG Login token refresh (any token within 15 days of expiry)."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    ig = context.application.bot_data.get("iglogin")
    if ig is None:
        await update.message.reply_text("IG Login not configured.")
        return
    await update.message.reply_text("Checking tokens for refresh (within 15 days of expiry)...")
    try:
        results = await asyncio.to_thread(ig.refresh_all)
    except Exception as e:
        await update.message.reply_text(f"Refresh error: {e}")
        return
    if not results:
        await update.message.reply_text("No tokens need refresh right now.")
        return
    ok = sum(1 for r in results if r.get("status") == "ok")
    err = sum(1 for r in results if r.get("status") == "error")
    lines = [f"Refresh complete: {ok} ok, {err} errors\n"]
    for r in results:
        icon = "OK" if r.get("status") == "ok" else "FAIL"
        line = f"[{icon}] @{r.get('username', '?')}"
        if r.get("status") == "error":
            line += f" - {r.get('error', '?')}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


async def cmd_igconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate an Instagram OAuth URL. Owner taps link, authorizes, gets a code."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    ig = context.application.bot_data.get("iglogin")
    if ig is None:
        await update.message.reply_text(
            "IG Login OAuth not configured for this bot.\n\n"
            "Set INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET, INSTAGRAM_REDIRECT_URI in .env."
        )
        return
    # Optional state param to identify this connect attempt (not required)
    state = update.message.text.partition(" ")[2].strip()
    url = ig.build_authorize_url(state=state)
    msg = (
        "*Instagram OAuth link*\n\n"
        "Send this link to the IG account owner. They must:\n"
        "1. Tap the link\n"
        "2. Log in to the IG account (in their browser)\n"
        "3. Tap *Allow*\n"
        "4. Copy the code shown on the next page\n"
        "5. Send back to you\n\n"
        "Then run: `/igauth <code>` in this bot to save the token.\n\n"
        f"[Authorize on Instagram]({url})\n\n"
        f"Raw URL:\n`{url}`"
    )
    # Start (or extend) the CF OAuth poller for 15 min if configured
    _poll_task = context.application.bot_data.get("_cf_oauth_poll_task")
    if _poll_task is not None and hasattr(context.application, "job_queue") and context.application.job_queue is not None:
        import time as _time
        _duration_sec = 15 * 60
        context.application.bot_data["cf_poller_stop_at"] = _time.time() + _duration_sec
        if context.application.bot_data.get("cf_poller_job") is None:
            _job = context.application.job_queue.run_repeating(
                _poll_task,
                interval=20,
                first=5,
                name="cf_oauth_poll_active",
            )
            context.application.bot_data["cf_poller_job"] = _job
            msg += f"\n\nCF poller: STARTED for 15 min (polls every 20s, auto-stops after)."
        else:
            msg += f"\n\nCF poller: extended for another 15 min."
    await update.message.reply_text(msg, disable_web_page_preview=True)


async def cmd_igconnectstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually stop the CF OAuth poller (saves KV usage)."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    job = context.application.bot_data.pop("cf_poller_job", None)
    context.application.bot_data.pop("cf_poller_stop_at", None)
    if job:
        try:
            job.schedule_removal()
        except Exception:
            pass
        await update.message.reply_text("CF OAuth poller stopped.")
    else:
        await update.message.reply_text("CF OAuth poller was not running.")


async def cmd_igconnectstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show CF OAuth poller status."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    poll_task = context.application.bot_data.get("_cf_oauth_poll_task")
    if not poll_task:
        await update.message.reply_text("CF OAuth polling not configured for this bot.")
        return
    job = context.application.bot_data.get("cf_poller_job")
    if not job:
        await update.message.reply_text("CF OAuth poller: NOT running. Use /igconnect to start.")
        return
    import time as _time
    stop_at = context.application.bot_data.get("cf_poller_stop_at", 0)
    remaining = max(0, int(stop_at - _time.time()))
    mins = remaining // 60
    secs = remaining % 60
    await update.message.reply_text(f"CF OAuth poller: RUNNING\nTime remaining: {mins}m {secs}s")


async def cmd_igauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exchange an authorization code for a long-lived token, store per account."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    ig = context.application.bot_data.get("iglogin")
    if ig is None:
        await update.message.reply_text("IG Login OAuth not configured for this bot.")
        return
    code = update.message.text.partition(" ")[2].strip()
    if not code:
        await update.message.reply_text("Usage: /igauth <code>\n\nGet the code by running /igconnect first.")
        return
    await update.message.reply_text("Exchanging code for token...")
    try:
        record = await asyncio.to_thread(ig.exchange_code, code)
    except Exception as e:
        await update.message.reply_text(f"Exchange failed: {e}")
        return
    msg = (
        "*IG Login connected*\n\n"
        f"Username: @{record.get('username', '?')}\n"
        f"User ID: `{record.get('user_id', '?')}`\n"
        f"Expires: {record.get('expires_at', '?')[:10]}\n"
        f"Token: `{record.get('access_token', '')[:20]}...` (truncated)"
    )
    await update.message.reply_text(msg)


async def cmd_iglogintokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all stored IG Login tokens with expiry."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    ig = context.application.bot_data.get("iglogin")
    if ig is None:
        await update.message.reply_text("IG Login OAuth not configured for this bot.")
        return
    tokens = ig.list_tokens()
    if not tokens:
        await update.message.reply_text("No IG Login tokens stored yet. Use /igconnect to add one.")
        return
    lines = [f"*IG Login tokens ({len(tokens)})*\n"]
    for t in tokens:
        lines.append(f"- @{t.get('username','?')} (uid `{t.get('user_id','?')}`), exp {t.get('expires_at','?')[:10]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")



async def cmd_igsetproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a proxy for a specific IG account. Usage: /igsetproxy <username> <proxy_url>"""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    args_str = update.message.text.partition(" ")[2].strip()
    parts = args_str.split(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text(
            "Usage: /igsetproxy <username> <proxy_url>\n\n"
            "Format: http://user:pass@host:port\n"
            "Or: http://host:port (no auth)"
        )
        return
    username = parts[0].lstrip("@")
    proxy_url = parts[1].strip()
    # Locate account file
    accounts_dir = os.path.join(os.environ.get("REELSHIFT_DATA_DIR", "/data"), "configs", "accounts")
    path = os.path.join(accounts_dir, f"{username}.json")
    if not os.path.exists(path):
        await update.message.reply_text(f"Account file not found: {username}.json")
        return
    try:
        with open(path) as f:
            cfg = json.load(f)
        cfg["proxy_url"] = proxy_url
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, path)
        # Redacted echo (hide user:pass)
        import re as _re
        redacted = _re.sub(r'://[^@]+@', '://***:***@', proxy_url)
        await update.message.reply_text(f"Proxy set for @{username}: {redacted}")
    except Exception as e:
        await update.message.reply_text(f"Failed to set proxy: {e}")


async def cmd_igclearproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear proxy for an IG account. Usage: /igclearproxy <username>"""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    username = update.message.text.partition(" ")[2].strip().lstrip("@")
    if not username:
        await update.message.reply_text("Usage: /igclearproxy <username>")
        return
    accounts_dir = os.path.join(os.environ.get("REELSHIFT_DATA_DIR", "/data"), "configs", "accounts")
    path = os.path.join(accounts_dir, f"{username}.json")
    if not os.path.exists(path):
        await update.message.reply_text(f"Account file not found: {username}.json")
        return
    try:
        with open(path) as f:
            cfg = json.load(f)
        had = "proxy_url" in cfg
        cfg.pop("proxy_url", None)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, path)
        await update.message.reply_text(f"Proxy {'cleared' if had else 'was not set'} for @{username}")
    except Exception as e:
        await update.message.reply_text(f"Failed to clear proxy: {e}")


async def cmd_igproxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all accounts with their proxy status."""
    allowed = context.application.bot_data.get("allowed_user_ids", [])
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("Not authorized.")
        return
    accs = accounts_mod.list_accounts()
    if not accs:
        await update.message.reply_text("No accounts registered.")
        return
    import re as _re
    lines = [f"Account proxies ({len(accs)} accounts):\n"]
    for a in accs:
        p = a.get("proxy_url")
        if p:
            redacted = _re.sub(r'://[^@]+@', '://***:***@', p)
            lines.append(f"  @{a['model_name']} -> {redacted}")
        else:
            lines.append(f"  @{a['model_name']} -> (none)")
    msg = "\n".join(lines)
    # Chunk if too long
    if len(msg) > 3800:
        chunks = [msg[i:i+3800] for i in range(0, len(msg), 3800)]
        for c in chunks:
            await update.message.reply_text(c)
    else:
        await update.message.reply_text(msg)


async def handle_schedule_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_schedule"):
        return

    bot_cfg = context.application.bot_data.get("bot_config", {})
    default_tz = bot_cfg.get("timezone", "UTC")

    try:
        times, tz = sched_mod.parse_schedule_input(update.message.text, default_tz)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    state = sched_mod.load_state() or {}
    if not state.get("queue"):
        context.user_data.pop("awaiting_schedule", None)
        await update.message.reply_text("❌ Queue disappeared.")
        return

    ppa = state.get("ppa", len(bot_cfg.get("windows", [])))
    if len(times) != ppa:
        await update.message.reply_text(
            f"❌ Expected *{ppa}* time(s), got *{len(times)}*.",
            
        )
        return

    context.user_data.pop("awaiting_schedule", None)

    # Group queue items by post_num
    accs = accounts_mod.list_accounts()
    by_post_num = {}
    for i, item in enumerate(state["queue"]):
        if item["status"] not in ("variant_ready", "scheduled"):
            continue
        by_post_num.setdefault(item["post_num"], []).append(i)

    # Generate randomized times per post_num
    schedule_times = sched_mod.assign_post_times(
        max((len(v) for v in by_post_num.values()), default=0),
        times, tz, jitter_minutes=20,
    )

    scheduler: sched_mod.CampaignScheduler = context.application.bot_data["sched"]

    earliest = None
    latest = None

    for post_num, item_indices in by_post_num.items():
        for slot, queue_idx in enumerate(item_indices):
            run_at = schedule_times[post_num][slot]
            state["queue"][queue_idx]["scheduled_at"] = run_at.isoformat()
            state["queue"][queue_idx]["status"] = "scheduled"
            scheduler.schedule_item(queue_idx, run_at)
            if earliest is None or run_at < earliest:
                earliest = run_at
            if latest is None or run_at > latest:
                latest = run_at

    state["schedule_set"] = True
    sched_mod.save_state(state)

    msg = "✅ *Schedule set!*\n"
    if earliest and latest:
        msg += (
            f"\nFirst post: {earliest.strftime('%H:%M %Z')}"
            f"\nLast post: {latest.strftime('%H:%M %Z')}"
        )
    await update.message.reply_text(msg, )


# ---------------- queue / today commands ----------------

@admin_only
async def cmd_igqueuestatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = sched_mod.load_state() or {}
    s = sched_mod.queue_summary(state)
    if not s.get("total"):
        await update.message.reply_text("Queue empty.")
        return
    parts = [f"*Total:* {s['total']}"]
    for k in ("pending", "variant_ready", "scheduled", "posted", "failed"):
        if k in s:
            parts.append(f"*{k}:* {s[k]}")
    await update.message.reply_text("\n".join(parts), )


@admin_only
async def cmd_igqueueclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wipe queue, delete variants from R2, clear sources."""
    state = sched_mod.load_state() or {}
    scheduler: sched_mod.CampaignScheduler = context.application.bot_data["sched"]

    # Cancel all jobs
    scheduler.clear_all_jobs()

    # Delete variants from R2
    deleted = 0
    for item in state.get("queue", []):
        key = item.get("variant_r2_key")
        if key:
            try:
                delete_file(key)
                deleted += 1
            except Exception:
                pass

    # Clear local sources
    videos_mod.clear_sources()

    # Clear state
    sched_mod.clear_state()

    context.application.bot_data["collecting"] = False
    await update.message.reply_text(
        f"🗑️ Queue cleared.\nVariants removed from R2: {deleted}"
    )


@admin_only
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = sched_mod.load_state() or {}
    s = sched_mod.queue_summary(state)
    verbose = bool(context.args) and context.args[0].lower() == "verbose"

    if not s.get("total"):
        await update.message.reply_text("Today's queue is empty.")
        return

    if not verbose:
        parts = [f"*Today:* {s['total']} items"]
        for k in ("posted", "scheduled", "variant_ready", "pending", "failed"):
            if k in s:
                parts.append(f"  • {k}: {s[k]}")
        await update.message.reply_text("\n".join(parts), )
        return

    # Verbose: list each item
    lines = []
    bot_cfg = context.application.bot_data.get("bot_config", {})
    tz = sched_mod.resolve_timezone(bot_cfg.get("timezone", "UTC"))
    for item in state["queue"]:
        time_str = ""
        if item.get("scheduled_at"):
            t = dt.datetime.fromisoformat(item["scheduled_at"])
            time_str = t.astimezone(tz).strftime("%H:%M")
        emoji = {
            "pending": "⏳", "variant_ready": "📦", "scheduled": "🕒",
            "posted": "✅", "failed": "❌",
        }.get(item["status"], "❓")
        lines.append(
            f"{emoji} `{time_str:>5}` p{item['post_num']} @{item['account']}"
        )
    await _send_chunked(update.message, lines)


# ---------------- post execution ----------------

async def post_queue_item(app, queue_idx):
    """Called by scheduler when an item's time arrives."""
    state = sched_mod.load_state() or {}
    if not state.get("queue") or queue_idx >= len(state["queue"]):
        return
    item = state["queue"][queue_idx]
    if item["status"] != "scheduled":
        return  # already posted, failed, or canceled

    log.info(f"Firing post for item {queue_idx}: @{item['account']} p{item['post_num']}")

    # Pick a caption
    try:
        cap = captions.pick_captions(1)[0]
    except ValueError:
        sched_mod.update_item(queue_idx, {"status": "failed", "error": "Empty caption pool"})
        return

    item["caption"] = cap
    sched_mod.update_item(queue_idx, {"caption": cap})

    # Post the Reel using the cached variant URL.
    # If this account has an IG Login token, route via graph.instagram.com + that token.
    # Otherwise use FB Login: graph.facebook.com + per-account or global token.
    _ig_login = app.bot_data.get("iglogin")
    _iglogin_rec = _ig_login.get_token(item["ig_user_id"]) if _ig_login else None
    # Look up per-account proxy from account config
    _proxy_url = None
    try:
        for _a in accounts_mod.list_accounts():
            if _a.get("model_name") == item.get("account"):
                _proxy_url = _a.get("proxy_url")
                break
    except Exception:
        pass
    # Pick a cover from the pool and upload to R2 (best-effort — post continues without cover on failure)
    _cover_url, _cover_key = None, None
    try:
        _cover_path = covers.pick_cover()
        if _cover_path:
            from r2 import upload_file as _r2_upload
            _cover_url, _cover_key = await asyncio.to_thread(_r2_upload, _cover_path, "image/jpeg")
            log.info(f"post_queue_item[{queue_idx}]: cover {_cover_path.split('/')[-1]} -> {_cover_key}")
    except Exception as _e:
        log.warning(f"post_queue_item[{queue_idx}]: cover selection failed, posting without: {_e}")

    if _iglogin_rec:
        _token = _iglogin_rec["access_token"]
        _base = "https://graph.instagram.com/v21.0"
        log.info(f"post_queue_item[{queue_idx}]: routing via IG Login (@{_iglogin_rec.get('username','?')})" + (f" [proxy set]" if _proxy_url else ""))
        try:
            media_id = await asyncio.to_thread(
                lambda: _ig_login.publish_reel(
                    item["ig_user_id"], item["variant_url"], cap,
                    True, 10, 60, _proxy_url, _cover_url,
                )
            )
            _iglogin_used = True
        except Exception as e:
            log.exception(f"Post raised for item {queue_idx} (IG Login)")
            sched_mod.update_item(queue_idx, {"status": "failed", "error": f"Exception: {e}"})
            return
        # Skip the FB Login path below by exiting the try block via 'else'
    else:
        _token = item.get("ig_access_token")
        _base = None
        _iglogin_used = False
    if not _iglogin_used:
        try:
            media_id = await asyncio.to_thread(
                _post_reel_sync,
                item["ig_user_id"], item["variant_url"], cap, True,
                _token, _base, _proxy_url,
            )
        except Exception as e:
            log.exception(f"Post raised for item {queue_idx}")
            sched_mod.update_item(queue_idx, {"status": "failed", "error": f"Exception: {e}"})
            return

    if media_id:
        state = sched_mod.update_item(queue_idx, {"status": "posted", "media_id": media_id, "error": None})
    else:
        state = sched_mod.update_item(queue_idx, {"status": "failed", "error": "Posting failed"})

    # Cleanup cover from R2 (if we uploaded one)
    if _cover_key:
        try:
            await asyncio.to_thread(delete_file, _cover_key)
        except Exception:
            pass

    # Cleanup variant from R2
    if item.get("variant_r2_key"):
        try:
            await asyncio.to_thread(delete_file, item["variant_r2_key"])
        except Exception:
            pass

    # Notify admin if all items done
    summary = sched_mod.queue_summary(state)
    pending_left = sum(summary.get(k, 0) for k in ("pending", "variant_ready", "scheduled"))
    if pending_left == 0:
        admin_id = app.bot_data.get("allowed_user_ids", [None])[0]
        if admin_id:
            try:
                # Build failed accounts list for the Day complete message
                _failed_items = [it for it in state.get("queue", []) if it.get("status") == "failed"]
                _failed_by_acc = {}
                for _it in _failed_items:
                    _acc = _it.get("account", "?")
                    _err = (_it.get("error") or "unknown")[:120]
                    if _acc not in _failed_by_acc:
                        _failed_by_acc[_acc] = _err
                _failed_lines = ""
                if _failed_by_acc:
                    _failed_lines = "\n\n❌ Failed accounts:\n"
                    for _a, _e in sorted(_failed_by_acc.items()):
                        _failed_lines += f"@{_a}: {_e}\n"
                    if len(_failed_lines) > 3000:
                        _failed_lines = _failed_lines[:3000] + "\n... (truncated)"
                await app.bot.send_message(
                    admin_id,
                    f"🏁 Day complete!\n"
                    f"Posted: {summary.get('posted', 0)}\n"
                    f"Failed: {summary.get('failed', 0)}"
                    f"{_failed_lines}"
                )
            except Exception:
                pass


def _post_reel_sync(ig_id, video_url, caption, share_to_feed, access_token=None, base_url=None, proxy_url=None):
    token = access_token or TOKEN
    _base = base_url or BASE_URL
    _proxies_ps = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    """Synchronous Reel posting with retry on transient Meta errors."""
    MAX_ATTEMPTS = 3
    RETRY_DELAY = 60
    RETRIABLE_ERRORS = ("2207076", "2207082", "2207020", "1363045", "2207085", "2207077")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            create = requests.post(
                f"{_base}/{ig_id}/media",
                params={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption,
                    "share_to_feed": "true" if share_to_feed else "false",
                    "access_token": token,
                },
                timeout=30, proxies=_proxies_ps,
            ).json()
        except Exception as e:
            log.error(f"[{ig_id}] Container create exception (attempt {attempt}): {e}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY)
                continue
            return None

        if "id" not in create:
            log.error(f"[{ig_id}] Container creation failed (attempt {attempt}): {create}")
            return None

        container_id = create["id"]
        elapsed = 0
        final_status = None
        final_status_msg = ""
        while elapsed < 600:
            try:
                r = requests.get(
                    f"{_base}/{container_id}",
                    params={"fields": "status_code,status", "access_token": token},
                    timeout=30, proxies=_proxies_ps,
                ).json()
            except Exception as e:
                log.warning(f"[{ig_id}] Status check exception, retrying: {e}")
                time.sleep(5)
                elapsed += 5
                continue
            final_status = r.get("status_code")
            final_status_msg = r.get("status", "") or ""
            if final_status == "FINISHED":
                break
            if final_status in ("ERROR", "EXPIRED"):
                log.error(f"[{ig_id}] Container {final_status} (attempt {attempt}): {final_status_msg}")
                break
            time.sleep(5)
            elapsed += 5
        else:
            final_status = "TIMEOUT"
            log.error(f"[{ig_id}] Container timed out after {elapsed}s (attempt {attempt})")

        if final_status == "FINISHED":
            try:
                publish = requests.post(
                    f"{_base}/{ig_id}/media_publish",
                    params={"creation_id": container_id, "access_token": token},
                    timeout=30, proxies=_proxies_ps,
                ).json()
            except Exception as e:
                log.error(f"[{ig_id}] Publish exception: {e}")
                return None
            media_id = publish.get("id")
            if media_id:
                return media_id

            # Publish didn't return an id. Verify via GET - Meta sometimes processes
            # the post but returns a malformed publish response.
            log.error(f"[{ig_id}] Publish returned no id: {publish}")
            log.info(f"[{ig_id}] Verifying via GET /media...")
            try:
                time.sleep(5)
                verify = requests.get(
                    f"{_base}/{ig_id}/media",
                    params={"fields": "id,timestamp", "limit": 3, "access_token": token},
                    timeout=30, proxies=_proxies_ps,
                ).json()
                items = verify.get("data") or []
                if items:
                    recent_id = items[0].get("id")
                    ts_str = items[0].get("timestamp", "")
                    if ts_str:
                        from datetime import datetime, timezone, timedelta
                        post_time = datetime.fromisoformat(ts_str.replace("+0000", "+00:00"))
                        now = datetime.now(timezone.utc)
                        if (now - post_time) < timedelta(minutes=3):
                            log.info(f"[{ig_id}] Verified via GET, media_id={recent_id}")
                            return recent_id
                log.warning(f"[{ig_id}] No recent post matched on verify")
            except Exception as e:
                log.error(f"[{ig_id}] Verification check failed: {e}")
            return None

        # Failed - decide whether to retry
        is_retriable = (
            final_status in ("EXPIRED", "TIMEOUT")
            or any(code in final_status_msg for code in RETRIABLE_ERRORS)
        )
        if is_retriable and attempt < MAX_ATTEMPTS:
            log.info(f"[{ig_id}] Retriable failure, waiting {RETRY_DELAY}s before retry {attempt + 1}/{MAX_ATTEMPTS}")
            time.sleep(RETRY_DELAY)
            continue

        return None

    return None


# ---------------- video router ----------------

@admin_only
async def handle_video_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes incoming videos based on mode:
    - If collection mode active → add to source pool
    - If /postnow mode active → use manual single-post flow
    - Otherwise → tell VA to start a campaign first
    """
    msg = update.message
    media = msg.video or (
        msg.document if (msg.document and msg.document.mime_type
                         and msg.document.mime_type.startswith("video/")) else None
    )
    if not media:
        await msg.reply_text("Please send a video.")
        return

    if context.user_data.get("postnow_active"):
        await _handle_postnow_video(update, context, media)
        return

    if context.application.bot_data.get("collecting"):
        await handle_campaign_video(update, context, media)
        return

    await msg.reply_text(
        "ℹ️ Use /igcampaign to start a campaign, or /postnow for a one-off post."
    )


# ---------------- /postnow manual flow ----------------

@admin_only
async def cmd_postnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accs = accounts_mod.list_accounts()
    if not accs:
        await update.message.reply_text("No accounts configured.")
        return
    if captions.pool_status()["total"] == 0:
        await update.message.reply_text("Caption pool empty. Add captions first.")
        return

    context.user_data["postnow_active"] = True
    await update.message.reply_text(
        "📤 Manual post mode. Send a video — you'll select target accounts."
    )


async def _handle_postnow_video(update, context, media):
    msg = update.message
    status = await msg.reply_text("📥 Downloading...")
    tmpdir = tempfile.mkdtemp(prefix="reelshift_postnow_")
    raw_path = os.path.join(tmpdir, "input.mp4")

    try:
        tg_file = await media.get_file()
        await tg_file.download_to_drive(raw_path)
    except Exception as e:
        await status.edit_text(f"❌ Download failed: {e}")
        return

    context.user_data["postnow_pending"] = {
        "raw_path": raw_path, "tmpdir": tmpdir, "selected": set(),
    }
    context.user_data.pop("postnow_active", None)

    size = os.path.getsize(raw_path)
    await status.edit_text(
        f"✅ Video ready ({size // 1024} KB).\nSelect account(s):",
        reply_markup=_postnow_keyboard(set()),
    )


def _postnow_keyboard(selected):
    accs = accounts_mod.list_accounts()
    rows = []
    for a in accs:
        mark = "✅" if a["model_name"] in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} @{a['model_name']}",
            callback_data=f"pn_toggle:{a['model_name']}"
        )])
    rows.append([
        InlineKeyboardButton("🌐 All", callback_data="pn_all"),
        InlineKeyboardButton("❌ Cancel", callback_data="pn_cancel"),
    ])
    rows.append([InlineKeyboardButton("🚀 Post", callback_data="pn_post")])
    return InlineKeyboardMarkup(rows)


@admin_only
async def handle_postnow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    pending = context.user_data.get("postnow_pending")
    if not pending:
        await query.edit_message_text("⚠️ No pending video.")
        return

    if data == "pn_cancel":
        _cleanup_postnow(context)
        await query.edit_message_text("❌ Cancelled.")
        return

    if data == "pn_all":
        accs = accounts_mod.list_accounts()
        pending["selected"] = {a["model_name"] for a in accs}
        await query.edit_message_reply_markup(_postnow_keyboard(pending["selected"]))
        return

    if data.startswith("pn_toggle:"):
        name = data.split(":", 1)[1]
        if name in pending["selected"]:
            pending["selected"].discard(name)
        else:
            pending["selected"].add(name)
        await query.edit_message_reply_markup(_postnow_keyboard(pending["selected"]))
        return

    if data == "pn_post":
        if not pending["selected"]:
            await query.answer("Select at least one account", show_alert=True)
            return

        selected = sorted(pending["selected"])
        await query.edit_message_text(
            f"⚙️ Processing for {len(selected)} account(s)..."
        )
        result = await asyncio.to_thread(
            _postnow_process_and_post, pending["raw_path"], selected
        )
        lines = [f"✅ {result['ok_count']}/{len(selected)} posted:"]
        for r in result["results"]:
            if r["ok"]:
                lines.append(f"• @{r['name']} — “{r['caption']}”")
            else:
                lines.append(f"• @{r['name']} ❌ {r['error']}")
        await query.edit_message_text("\n".join(lines))
        _cleanup_postnow(context)


def _cleanup_postnow(context):
    pending = context.user_data.pop("postnow_pending", None)
    if pending:
        try:
            for f in os.listdir(pending["tmpdir"]):
                os.remove(os.path.join(pending["tmpdir"], f))
            os.rmdir(pending["tmpdir"])
        except Exception:
            pass


def _postnow_process_and_post(raw_path, selected_names):
    """Same single-video flow as before, kept for emergency manual posting."""
    results = []
    processed_path = None
    r2_key = None
    ok_count = 0

    try:
        processed_path = process_video(raw_path, output_dir=os.path.dirname(raw_path))
        url, r2_key = upload_file(processed_path, content_type="video/mp4")
        chosen = captions.pick_captions(len(selected_names))
        accs_by_name = {a["model_name"]: a for a in accounts_mod.list_accounts()}

        for name, cap in zip(selected_names, chosen):
            cfg = accs_by_name.get(name)
            if not cfg:
                results.append({"name": name, "ok": False, "error": "Not found"})
                continue
            mid = _post_reel_sync(cfg["ig_user_id"], url, cap, True, cfg.get("ig_access_token"))
            if mid:
                results.append({"name": name, "ok": True, "caption": cap, "media_id": mid})
                ok_count += 1
            else:
                results.append({"name": name, "ok": False, "error": "Post failed", "caption": cap})

        time.sleep(15)
        delete_file(r2_key)
        r2_key = None
        return {"ok_count": ok_count, "results": results}
    except Exception as e:
        return {
            "ok_count": ok_count,
            "results": results + [
                {"name": n, "ok": False, "error": str(e)}
                for n in selected_names if not any(r["name"] == n for r in results)
            ],
        }
    finally:
        if processed_path and os.path.exists(processed_path):
            try: os.remove(processed_path)
            except: pass
        if r2_key:
            try: delete_file(r2_key)
            except: pass


# ---------------- bulk text router ----------------

@admin_only
async def handle_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Plain-text messages (no command) route to:
    - schedule input handler if awaiting_schedule
    - bulk caption handler if awaiting_captions
    """
    if context.user_data.get("awaiting_schedule"):
        await handle_schedule_input(update, context)
        return
    if context.user_data.get("awaiting_captions"):
        await handle_bulk_captions(update, context)
        return


async def handle_bulk_captions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_captions", None)
    added, failed = [], []
    for line in update.message.text.split("\n"):
        cleaned = _strip_numbering(line)
        if not cleaned:
            continue
        try:
            idx = captions.add_caption(cleaned)
            added.append((idx, cleaned))
        except Exception as e:
            failed.append((cleaned, str(e)))

    if not added and not failed:
        await update.message.reply_text("⚠️ No captions found.")
        return
    msg = f"✅ Added {len(added)} caption(s)"
    if failed:
        msg += f"\n❌ {len(failed)} failed"
    await update.message.reply_text(msg)



# ---------------- covers pool ----------------

@admin_only
async def cmd_addcover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_covers"] = True
    s = covers.pool_status()
    await update.message.reply_text(
        f"🖼 Send cover images (JPG/PNG/WEBP), one or many.\n"
        f"Currently: {s['total']} in pool.\n"
        f"Send /donecovers or /cancel to stop."
    )


@admin_only
async def cmd_donecovers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.pop("awaiting_covers", None):
        s = covers.pool_status()
        await update.message.reply_text(f"✅ Done. Cover pool: {s['total']} total.")
    else:
        await update.message.reply_text("Not in cover-upload mode.")


@admin_only
async def cmd_covers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = covers.list_covers()
    if not items:
        await update.message.reply_text("Cover pool empty. /addcover")
        return
    lines = [f"[{'✓' if used else ' '}] `{name}`" for name, used in items]
    await _send_chunked(update.message, lines)


@admin_only
async def cmd_removecover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removecover <filename>")
        return
    try:
        removed = covers.remove_cover(context.args[0])
        await update.message.reply_text(f"✅ Removed: {removed}")
    except (FileNotFoundError, ValueError) as e:
        await update.message.reply_text(f"❌ {e}")


@admin_only
async def cmd_coverstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = covers.pool_status()
    await update.message.reply_text(
        f"🖼 Covers: {s['used']}/{s['total']} used, {s['remaining']} remaining"
    )


@admin_only
async def cmd_resetcovers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    covers.reset_used()
    await update.message.reply_text("🔄 Cover pool used-tracker reset")


@admin_only
async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When /addcover is active, incoming photos/images are saved to the cover pool."""
    if not context.user_data.get("awaiting_covers"):
        return
    msg = update.message
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        ext = ".jpg"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        tg_file = await msg.document.get_file()
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
            msg.document.mime_type, ".jpg"
        )
    else:
        return

    tmpdir = tempfile.mkdtemp(prefix="reelshift_cover_")
    tmp_path = os.path.join(tmpdir, f"in{ext}")
    try:
        await tg_file.download_to_drive(tmp_path)
        name = covers.add_cover(tmp_path, original_ext=ext)
        s = covers.pool_status()
        await msg.reply_text(f"✅ Added cover {name} ({s['total']} total)")
    except Exception as e:
        await msg.reply_text(f"❌ {e}")
    finally:
        try:
            os.remove(tmp_path)
            os.rmdir(tmpdir)
        except Exception:
            pass



# ---------------- vault / scrape targets ----------------

@admin_only
async def cmd_addhashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addhashtag <name>")
        return
    try:
        added = scrape_targets.add_hashtag(context.args[0])
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    d = scrape_targets.list_all()
    if added:
        await update.message.reply_text(f"✅ Added #{context.args[0].lstrip('#').lower()} ({len(d['hashtags'])} hashtags)")
    else:
        await update.message.reply_text(f"Already tracking that hashtag ({len(d['hashtags'])} hashtags)")


@admin_only
async def cmd_addcreator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addcreator <handle>")
        return
    try:
        added = scrape_targets.add_creator(context.args[0])
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    d = scrape_targets.list_all()
    if added:
        await update.message.reply_text(f"✅ Added @{context.args[0].lstrip('@').lower()} ({len(d['creators'])} creators)")
    else:
        await update.message.reply_text(f"Already tracking that creator ({len(d['creators'])} creators)")


@admin_only
async def cmd_listtargets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = scrape_targets.list_all()
    lines = []
    if d["hashtags"]:
        lines.append("*Hashtags:*")
        lines += [f"  #{h}" for h in d["hashtags"]]
    if d["creators"]:
        if lines: lines.append("")
        lines.append("*Creators:*")
        lines += [f"  @{c}" for c in d["creators"]]
    if not lines:
        lines = ["No scrape targets. Use /addhashtag <name> or /addcreator <handle>."]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def cmd_removetarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removetarget <name>")
        return
    kind = scrape_targets.remove(context.args[0])
    if kind:
        await update.message.reply_text(f"✅ Removed {kind}: {context.args[0]}")
    else:
        await update.message.reply_text(f"Not found in targets: {context.args[0]}")


@admin_only
async def cmd_vaultstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = vault.status_counts()
    await update.message.reply_text(
        f"🗄 Vault:\n"
        f"  Pending:  {s['pending']}\n"
        f"  Approved: {s['approved']}\n"
        f"  Rejected: {s['rejected']}\n"
        f"  Used:     {s['used']}"
    )


@admin_only
async def cmd_vaultlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show approved candidates (next-up for /igcampaign)."""
    approved = vault.list_by_status("approved", limit=20)
    if not approved:
        await update.message.reply_text("No approved candidates. Use /scrape to fetch some.")
        return
    lines = [f"*Approved ({len(approved)}):*"]
    for r in approved:
        cap = (r["caption"] or "")[:40].replace("\n", " ")
        lines.append(f"`{r['id']:>4}` {r['source_ref']} — {cap}")
    await _send_chunked(update.message, lines)



# ---------------- /scrape + approval flow ----------------

async def _send_candidate_preview(bot, chat_id, cand_row):
    """Send a candidate to the admin with approve/reject buttons.
    cand_row is a dict from vault (must include id, source_ref, video_url, cover_url, caption, like_count, play_count).
    """
    cid = cand_row["id"]
    caption = (cand_row.get("caption") or "").strip()
    if len(caption) > 250:
        caption = caption[:247] + "..."
    stats_bits = []
    if cand_row.get("play_count"):
        stats_bits.append(f"📺 {cand_row['play_count']:,}")
    if cand_row.get("like_count"):
        stats_bits.append(f"❤ {cand_row['like_count']:,}")
    if cand_row.get("comment_count"):
        stats_bits.append(f"💬 {cand_row['comment_count']:,}")
    stats = " · ".join(stats_bits)

    text = (
        f"*Vault candidate `#{cid}`*\n"
        f"From: {cand_row['source_ref']}\n"
        + (f"{stats}\n" if stats else "")
        + (f"\n_{caption}_" if caption else "")
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"vlt_a_{cid}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"vlt_r_{cid}"),
    ]])
    # Try to send as video (Telegram fetches the URL); fall back to text-only if it fails
    try:
        await bot.send_video(
            chat_id, video=cand_row["video_url"],
            caption=text, reply_markup=kb, parse_mode="Markdown",
            supports_streaming=True,
        )
    except Exception:
        try:
            await bot.send_photo(
                chat_id, photo=cand_row.get("cover_url") or cand_row["video_url"],
                caption=text, reply_markup=kb, parse_mode="Markdown",
            )
        except Exception:
            await bot.send_message(
                chat_id,
                text + f"\n\n[video]({cand_row['video_url']})",
                reply_markup=kb, parse_mode="Markdown",
            )


@admin_only
@admin_only
async def cmd_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fire the actual scrape work in a background task so buttons stay responsive."""
    asyncio.create_task(_run_scrape_job(update, context))


async def _run_scrape_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scrape [N] — run Apify across all configured targets. N = max items per target (default 3)."""
    n_arg = update.message.text.partition(" ")[2].strip()
    try:
        per_target = int(n_arg) if n_arg else 3
    except ValueError:
        await update.message.reply_text(f"❌ Bad N: {n_arg!r}")
        return
    if per_target < 1 or per_target > 100:
        await update.message.reply_text("N must be 1..100")
        return

    targets = scrape_targets.list_all()
    total_targets = len(targets["hashtags"]) + len(targets["creators"])
    if total_targets == 0:
        await update.message.reply_text("No scrape targets. Use /addhashtag or /addcreator first.")
        return

    status = await update.message.reply_text(
        f"🔍 Scraping {total_targets} target(s), {per_target} each..."
    )

    added_count = 0
    dup_count = 0
    err_count = 0
    new_candidates = []

    def _run_hashtag(h): return instagram_direct.scrape_hashtag(h, max_items=per_target)
    def _run_creator(c): return apify_client.scrape_creator(c, max_items=per_target)

    for h in targets["hashtags"]:
        try:
            items = await asyncio.to_thread(_run_hashtag, h)
            for item in items:
                new_id = vault.add_candidate(**item)
                if new_id:
                    added_count += 1
                    stored = vault.get(new_id)
                    if stored:
                        new_candidates.append(stored)
                else:
                    dup_count += 1
        except Exception as e:
            log.exception(f"Scrape failed for #{h}")
            err_count += 1
            try:
                await update.message.reply_text(f"❌ #{h}: {e}")
            except Exception:
                pass

    for c in targets["creators"]:
        try:
            items = await asyncio.to_thread(_run_creator, c)
            for item in items:
                new_id = vault.add_candidate(**item)
                if new_id:
                    added_count += 1
                    stored = vault.get(new_id)
                    if stored:
                        new_candidates.append(stored)
                else:
                    dup_count += 1
        except Exception as e:
            log.exception(f"Scrape failed for @{c}")
            err_count += 1
            try:
                await update.message.reply_text(f"❌ @{c}: {e}")
            except Exception:
                pass

    await status.edit_text(
        f"✅ Scrape done: {added_count} new, {dup_count} duplicates, {err_count} errors.\n"
        f"Sending {len(new_candidates)} for approval..."
    )

    for cand in new_candidates:
        try:
            await _send_candidate_preview(context.bot, update.effective_chat.id, cand)
        except Exception as e:
            log.warning(f"preview send failed for vault {cand['id']}: {e}")


@admin_only
async def handle_vault_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve / reject buttons on scraped candidates."""
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    # Pattern: vlt_a_<id> or vlt_r_<id>
    parts = data.split("_", 2)
    if len(parts) != 3:
        return
    _, action, cid_str = parts
    try:
        cid = int(cid_str)
    except ValueError:
        return

    if action == "a":
        ok = vault.approve(cid)
        label = "✅ APPROVED" if ok else "(already handled)"
    elif action == "r":
        ok = vault.reject(cid)
        label = "❌ REJECTED" if ok else "(already handled)"
    else:
        return

    # Strip buttons and prepend the outcome to the caption
    try:
        # For video/photo messages the "text" is actually the caption
        original_caption = q.message.caption or q.message.text or ""
        new_body = f"*{label}*  #{cid}\n\n{original_caption}"
        if q.message.caption is not None:
            await q.edit_message_caption(caption=new_body, parse_mode="Markdown", reply_markup=None)
        else:
            await q.edit_message_text(text=new_body, parse_mode="Markdown", reply_markup=None)
    except Exception as e:
        log.warning(f"edit callback message failed: {e}")




# ---------------- library sync (RocketAPI-backed, multi-creator) ----------------

@admin_only
async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full-catalog sync of all SCRAPE_CREATORS into the local library."""
    creators_env = (os.environ.get("SCRAPE_CREATORS") or os.environ.get("SCRAPE_MAIN_CREATOR") or "").strip()
    creators = [c.strip().lstrip("@") for c in creators_env.split(",") if c.strip()]
    if not creators:
        await update.message.reply_text("SCRAPE_CREATORS not set in .env")
        return
    await update.message.reply_text(
        f"🔄 Syncing {len(creators)} creator(s) to library: "
        + ", ".join(f"@{c}" for c in creators)
    )
    for creator in creators:
        asyncio.create_task(_run_sync_job(update, context, creator))


async def _run_sync_job(update, context, main_creator):
    import requests as _rq
    import shutil as _sh
    manifest = library.load_manifest()
    known = set(manifest.keys())

    try:
        reels = await asyncio.to_thread(rocketapi_client.scrape_profile_reels, main_creator, 500)
    except Exception as e:
        await update.effective_chat.send_message(f"❌ @{main_creator} reels scrape failed: {e}")
        reels = []
    try:
        pics = await asyncio.to_thread(rocketapi_client.scrape_profile_pics, main_creator, 500)
    except Exception as e:
        await update.effective_chat.send_message(f"❌ @{main_creator} pics scrape failed: {e}")
        pics = []

    new_reels = [c for c in reels if c.get("ig_shortcode") and c["ig_shortcode"] not in known]
    new_pics  = [c for c in pics  if c.get("ig_shortcode") and c["ig_shortcode"] not in known]

    await update.effective_chat.send_message(
        f"@{main_creator}: found {len(reels)} reels ({len(new_reels)} new), {len(pics)} pics ({len(new_pics)} new). Downloading..."
    )

    r_ok = r_fail = p_ok = p_fail = 0
    for cand in new_reels:
        tmpdir = tempfile.mkdtemp(prefix="lib_")
        tmp = os.path.join(tmpdir, "v.mp4")
        try:
            r = await asyncio.to_thread(_rq.get, cand["video_url"], timeout=60, stream=True)
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1024*256):
                    if chunk: f.write(chunk)
            await asyncio.to_thread(library.add_reel_from_local_path, cand, tmp)
            r_ok += 1
        except Exception as e:
            log.warning(f"sync reel {cand.get('ig_shortcode')} failed: {e}")
            r_fail += 1
        finally:
            try: _sh.rmtree(tmpdir)
            except Exception: pass

    for cand in new_pics:
        tmpdir = tempfile.mkdtemp(prefix="lib_")
        tmp = os.path.join(tmpdir, "p.jpg")
        try:
            r = await asyncio.to_thread(_rq.get, cand["image_url"], timeout=60, stream=True)
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1024*256):
                    if chunk: f.write(chunk)
            await asyncio.to_thread(library.add_pic_from_local_path, cand, tmp)
            p_ok += 1
        except Exception as e:
            log.warning(f"sync pic {cand.get('ig_shortcode')} failed: {e}")
            p_fail += 1
        finally:
            try: _sh.rmtree(tmpdir)
            except Exception: pass

    s = library.stats()
    await update.effective_chat.send_message(
        f"✅ Sync done for @{main_creator}.\n"
        f"Reels: +{r_ok} added, {r_fail} failed (library total: {s['reels']})\n"
        f"Pics:  +{p_ok} added, {p_fail} failed (library total: {s['pics']})\n"
        f"Disk: {s['size_mb']} MB"
    )


@admin_only
async def cmd_librarystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = library.stats()
    await update.message.reply_text(
        f"📚 Library:\n"
        f"  Reels: {s['reels']} total\n"
        f"  Pics:  {s['pics']} total\n"
        f"  Disk:  {s['size_mb']} MB"
    )


# ---------------- application builder ----------------


@admin_only
async def cmd_igtoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set or clear the session active token used for subsequent /igadd commands."""
    args = update.message.text.partition(" ")[2].strip()
    if not args:
        active = context.application.bot_data.get("active_token")
        if active:
            await update.message.reply_text(
                f"Active token: {active[:15]}...{active[-5:]}\n"
                f"Subsequent /igadd without inline ig_access_token will use this."
            )
        else:
            await update.message.reply_text(
                "No active token set. /igadd will use the default token from .env."
            )
        return
    if args.lower() in ("clear", "reset", "default", "none"):
        context.application.bot_data.pop("active_token", None)
        await update.message.reply_text(
            "✅ Cleared active token. /igadd will use default token from .env."
        )
        return
    context.application.bot_data["active_token"] = args
    await update.message.reply_text(
        f"✅ Active token set: {args[:15]}...{args[-5:]}\n"
        f"All subsequent /igadd will use this token until changed (or /igtoken clear)."
    )



# ---------- INSIGHTS COMMANDS (read-only, additive) ----------

@admin_only
async def cmd_insightsrefresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually refresh the insights cache from Meta Graph API."""
    import asyncio
    await update.message.reply_text("\u26a1 Refreshing insights from Graph API...")
    try:
        accounts = accounts_mod.list_accounts()
    except Exception as e:
        await update.message.reply_text(f"\u274c Failed to list accounts: {e}")
        return

    def _do_refresh():
        return insights_mod.refresh_all(accounts, TOKEN)

    try:
        total = await asyncio.to_thread(_do_refresh)
        await update.message.reply_text(
            f"\u2705 Refresh complete. {total} posts upserted across {len(accounts)} accounts."
        )
    except Exception as e:
        log.exception("Insights refresh failed")
        await update.message.reply_text(f"\u274c Refresh failed: {e}")


def _fmt_post_line(p):
    from datetime import datetime
    ts = p.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(ts.replace("+0000", "+00:00"))
        ts_str = dt.strftime("%m-%d %H:%M")
    except Exception:
        ts_str = ts[:16]
    likes = p.get("like_count") or 0
    comments = p.get("comments_count") or 0
    views = p.get("views")
    views_str = f" \U0001f440 {views}" if views else ""
    return f"  {ts_str} @{p['username']}{views_str} \u2764\ufe0f {likes} \U0001f4ac {comments}"


@admin_only
async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Summary across all accounts for last 7 days, OR per-account if @username given."""
    args = update.message.text.partition(" ")[2].strip().split()
    if args and args[0].startswith("@"):
        username = args[0][1:]
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
        posts = insights_mod.query_account(username, limit=limit)
        if not posts:
            await update.message.reply_text(f"No cached posts for @{username}. Try /insightsrefresh first.")
            return
        lines = [f"\U0001f4ca @{username} \u2014 last {len(posts)} posts"]
        for p in posts:
            lines.append(_fmt_post_line(p))
            if p.get("permalink"):
                lines.append(f"    {p['permalink']}")
        await update.message.reply_text("\n".join(lines))
        return

    days = int(args[0]) if args and args[0].isdigit() else 7
    s = insights_mod.query_summary(days=days)
    last = insights_mod.get_last_polled()
    msg = (
        f"\U0001f4ca Last {days} days summary\n"
        f"{s.get('post_count',0)} posts across {s.get('account_count',0)} accounts\n"
        f"\u2764\ufe0f {s.get('total_likes',0)} likes  \U0001f4ac {s.get('total_comments',0)} comments"
    )
    if s.get("posts_with_views", 0) > 0:
        msg += f"\n\U0001f440 {s.get('total_views',0)} views ({s.get('posts_with_views',0)} posts)"
    if last:
        msg += f"\n\nCache last updated: {last[:16]}"
    else:
        msg += "\n\n\u26a0\ufe0f Cache empty \u2014 run /insightsrefresh"
    await update.message.reply_text(msg)


@admin_only
async def cmd_engagement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Today's posts with engagement."""
    posts = insights_mod.query_today()
    if not posts:
        await update.message.reply_text("No cached posts today. Try /insightsrefresh.")
        return
    total_likes = sum((p.get("like_count") or 0) for p in posts)
    total_comments = sum((p.get("comments_count") or 0) for p in posts)
    total_views = sum((p.get("views") or 0) for p in posts)
    has_views = any(p.get("views") for p in posts)
    lines = [
        f"\U0001f4ca Today \u2014 {len(posts)} posts",
        f"\u2764\ufe0f {total_likes}  \U0001f4ac {total_comments}"
        + (f"  \U0001f440 {total_views}" if has_views else ""),
        "",
    ]
    for p in posts[:30]:
        lines.append(_fmt_post_line(p))
    await update.message.reply_text("\n".join(lines))


@admin_only
async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top 5 posts by views (or likes) in last N days."""
    args = update.message.text.partition(" ")[2].strip().split()
    days = int(args[0]) if args and args[0].isdigit() else 7
    posts = insights_mod.query_top(days=days, limit=5)
    if not posts:
        await update.message.reply_text(f"No cached posts in last {days} days.")
        return
    lines = [f"\U0001f3c6 Top 5 \u2014 last {days} days"]
    medals = ["\U0001f947", "\U0001f948", "\U0001f949", "\u2b50", "\u2b50"]
    for i, p in enumerate(posts):
        lines.append(f"{medals[i]} {_fmt_post_line(p).strip()}")
        if p.get("permalink"):
            lines.append(f"    {p['permalink']}")
    await update.message.reply_text("\n".join(lines))


# ---------- END INSIGHTS COMMANDS ----------



# ---------- DASHBOARD BOT (read-only, password-auth) ----------

def _dashboard_auth_file():
    return os.path.join(os.environ.get("REELSHIFT_DATA_DIR", "."), "state", "dashboard_authorized.json")


def load_dashboard_authorized():
    p = _dashboard_auth_file()
    if not os.path.exists(p):
        return set()
    try:
        with open(p) as f:
            return set(json.load(f).get("authorized_user_ids", []))
    except Exception as e:
        log.warning(f"Failed to load dashboard auth file: {e}")
        return set()


def save_dashboard_authorized(user_ids):
    p = _dashboard_auth_file()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        with open(p, "w") as f:
            json.dump({"authorized_user_ids": sorted(list(user_ids))}, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save dashboard auth file: {e}")


def dashboard_auth(func):
    """Auth check for dashboard bot. Silent ignore if user not authorized."""
    @wraps(func)
    async def wrapper(update, context):
        user_id = update.effective_user.id
        authorized = context.application.bot_data.get("dashboard_authorized_users", set())
        if user_id not in authorized:
            return
        return await func(update, context)
    return wrapper


async def cmd_dashboard_start(update, context):
    user_id = update.effective_user.id
    authorized = context.application.bot_data.get("dashboard_authorized_users", set())
    if user_id in authorized:
        await update.message.reply_text(
            "\U0001f44b Welcome back!\n\nUse /help to see available commands."
        )
    else:
        await update.message.reply_text(
            "\U0001f44b Welcome to the agency dashboard.\n\n"
            "Please authenticate:\n/login <password>"
        )


async def cmd_dashboard_login(update, context):
    args = update.message.text.partition(" ")[2].strip()
    password = context.application.bot_data.get("dashboard_password", "")
    user_id = update.effective_user.id

    if not args:
        await update.message.reply_text("Usage: /login <password>")
        return

    if not password or args != password:
        await update.message.reply_text("\u274c Invalid password.")
        return

    authorized = context.application.bot_data.get("dashboard_authorized_users", set())
    authorized = set(authorized)
    authorized.add(user_id)
    context.application.bot_data["dashboard_authorized_users"] = authorized
    save_dashboard_authorized(authorized)

    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
    except Exception:
        pass

    await update.message.reply_text(
        "\u2705 Authenticated.\n\nUse /help to see available commands."
    )


@dashboard_auth
async def cmd_dashboard_logout(update, context):
    user_id = update.effective_user.id
    authorized = set(context.application.bot_data.get("dashboard_authorized_users", set()))
    authorized.discard(user_id)
    context.application.bot_data["dashboard_authorized_users"] = authorized
    save_dashboard_authorized(authorized)
    await update.message.reply_text("\U0001f44b Logged out.")


async def cmd_dashboard_help(update, context):
    user_id = update.effective_user.id
    authorized = context.application.bot_data.get("dashboard_authorized_users", set())
    if user_id not in authorized:
        await update.message.reply_text("Please /login <password> first.")
        return
    await update.message.reply_text(
        "\U0001f4ca Commands:\n\n"
        "/insights \u2014 7-day summary\n"
        "/insights @username [N] \u2014 per-account\n"
        "/today \u2014 campaign status (✅ posted / 🕒 scheduled)\n"
        "/engagement \u2014 last 24h posts with likes/comments\n"
        "/top [days] \u2014 top performers\n"
        "/accounts \u2014 list accounts\n"
        "/logout \u2014 sign out"
    )


@dashboard_auth
async def cmd_dashboard_accounts(update, context):
    accounts = accounts_mod.list_accounts()
    lines = [f"\U0001f4cb {len(accounts)} accounts:"]
    for acc in sorted(accounts, key=lambda a: a["model_name"]):
        lines.append(f"  @{acc['model_name']}")
    await update.message.reply_text("\n".join(lines))


def build_dashboard_application(token: str, password: str):
    """Build a separate read-only dashboard bot Application."""
    from telegram.ext import Application, CommandHandler

    builder = Application.builder().token(token)
    base_url = os.environ.get("TELEGRAM_API_BASE_URL")
    if base_url:
        builder = builder.base_url(f"{base_url}/bot")
        builder = builder.local_mode(True)

    app = builder.concurrent_updates(True).build()

    app.bot_data["dashboard_password"] = password
    app.bot_data["dashboard_authorized_users"] = load_dashboard_authorized()

    # Public commands
    app.add_handler(CommandHandler("start", cmd_dashboard_start))
    app.add_handler(CommandHandler("login", cmd_dashboard_login))
    app.add_handler(CommandHandler("help", cmd_dashboard_help))

    # Auth-required dashboard commands
    app.add_handler(CommandHandler("logout", cmd_dashboard_logout))
    app.add_handler(CommandHandler("accounts", cmd_dashboard_accounts))

    # Reuse existing read-only handlers, but rewrap with dashboard auth
    app.add_handler(CommandHandler("insights", dashboard_auth(cmd_insights.__wrapped__)))
    app.add_handler(CommandHandler("today",    dashboard_auth(cmd_today.__wrapped__)))
    app.add_handler(CommandHandler("engagement", dashboard_auth(cmd_engagement.__wrapped__)))
    app.add_handler(CommandHandler("top",      dashboard_auth(cmd_top.__wrapped__)))

    return app


# ---------- END DASHBOARD BOT ----------

@admin_only
async def cmd_igremovebulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/igremovebulk @user1 @user2 ... — remove multiple IG accounts at once."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /igremovebulk @user1 @user2 @user3\n"
            "Removes multiple IG account configs from this bot."
        )
        return

    _accounts_dir = os.path.join(
        os.environ.get("REELSHIFT_DATA_DIR", "/data"),
        "configs", "accounts"
    )
    removed = []
    not_found = []
    for arg in context.args:
        username = arg.lstrip("@").strip()
        if not username:
            continue
        config_path = os.path.join(_accounts_dir, f"{username}.json")
        if os.path.exists(config_path):
            try:
                os.remove(config_path)
                removed.append(username)
            except Exception as e:
                await update.message.reply_text(f"⚠️ Error removing @{username}: {e}")
        else:
            not_found.append(username)

    msg = f"✅ Removed {len(removed)} account(s):\n"
    for u in removed:
        msg += f"  @{u}\n"
    if not_found:
        msg += f"\n⚠️ Not found ({len(not_found)}):\n"
        for u in not_found:
            msg += f"  @{u}\n"
    msg += f"\nTotal accounts remaining: {len([f for f in os.listdir(_accounts_dir) if f.endswith('.json')])}"
    await update.message.reply_text(msg)


def build_application(bot_config):
    builder = (
        Application.builder()
        .token(bot_config["telegram_bot_token"])
        .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(10)
        .get_updates_connect_timeout(30).get_updates_read_timeout(30)
    )
    api_base = os.environ.get("TELEGRAM_API_BASE_URL")
    if api_base:
        builder = builder.base_url(f"{api_base}/bot").base_file_url(f"{api_base}/file/bot").local_mode(True)
        print(f"Using local Telegram Bot API: {api_base}")
    app = builder.build()
    app.bot_data["allowed_user_ids"] = bot_config.get("allowed_telegram_user_ids", [])
    app.bot_data["bot_config"] = bot_config
    app.bot_data["collecting"] = False

    # Link downloader for /addlink (Dropbox + Google Drive)
    _gkey = os.environ.get("GOOGLE_DRIVE_API_KEY", "").strip() or None
    _used_db = os.path.join(os.environ.get("REELSHIFT_DATA_DIR", "/data"), "state", "used_links.db")
    app.bot_data["link_downloader"] = link_dl_mod.LinkDownloader(google_api_key=_gkey, used_db_path=_used_db)

    # Instagram Login OAuth client (only initialized if env vars are set)
    _ig_app_id = os.environ.get("INSTAGRAM_APP_ID", "").strip()
    _ig_app_secret = os.environ.get("INSTAGRAM_APP_SECRET", "").strip()
    _ig_redirect_uri = os.environ.get("INSTAGRAM_REDIRECT_URI", "").strip()
    if _ig_app_id and _ig_app_secret and _ig_redirect_uri:
        _iglogin_path = os.path.join(os.environ.get("REELSHIFT_DATA_DIR", "/data"), "state", "iglogin_tokens.json")
        app.bot_data["iglogin"] = iglogin_mod.IGLoginClient(
            app_id=_ig_app_id,
            app_secret=_ig_app_secret,
            redirect_uri=_ig_redirect_uri,
            tokens_path=_iglogin_path,
        )
        print(f"IGLogin OAuth enabled: app_id={_ig_app_id}, redirect_uri={_ig_redirect_uri}")

        # Schedule daily background refresh of IG Login tokens
        async def _iglogin_refresh_task(context=None):
            _ig = app.bot_data.get("iglogin")
            if _ig is None:
                return
            try:
                # Build proxy map from account configs
                _proxy_map = {}
                try:
                    for _a in accounts_mod.list_accounts():
                        if _a.get("proxy_url"):
                            _proxy_map[_a["ig_user_id"]] = _a["proxy_url"]
                except Exception:
                    pass
                results = await asyncio.to_thread(_ig.refresh_all, 15, _proxy_map)
                if results:
                    ok = sum(1 for _r in results if _r.get("status") == "ok")
                    err = sum(1 for _r in results if _r.get("status") == "error")
                    print(f"IGLogin daily refresh: {ok} ok, {err} errors")
                    if err > 0:
                        for _r in results:
                            if _r.get("status") == "error":
                                print(f"  REFRESH ERROR @{_r.get('username','?')}: {_r.get('error','?')}")
            except Exception as _e:
                import traceback; traceback.print_exc()
                print(f"IGLogin refresh task exception: {_e}")

        app.bot_data["_iglogin_refresh_task"] = _iglogin_refresh_task

        # If job_queue is available, schedule 24h repeating refresh
        if hasattr(app, "job_queue") and app.job_queue is not None:
            app.job_queue.run_repeating(
                _iglogin_refresh_task,
                interval=24 * 3600,  # daily
                first=3600,          # first run 1h after startup
                name="iglogin_refresh",
            )
            print("IGLogin refresh scheduled: every 24h via job_queue")
        else:
            print("IGLogin refresh: job_queue unavailable; use /igrefresh manually")

        # Cloudflare OAuth code poller - auto-fetches codes from Worker KV
        _cf_worker_url = os.environ.get("CF_OAUTH_WORKER_URL", "").strip().rstrip("/")
        _cf_poll_secret = os.environ.get("CF_OAUTH_POLL_SECRET", "").strip()
        if _cf_worker_url and _cf_poll_secret:
            async def _cf_oauth_poll_task(context=None):
                import requests as _req
                import json as _json
                import time as _time
                # Self-expiry check: if past stop_at, unschedule this job
                _stop_at = app.bot_data.get("cf_poller_stop_at", 0)
                if _stop_at > 0 and _time.time() >= _stop_at:
                    _job = app.bot_data.pop("cf_poller_job", None)
                    if _job:
                        try:
                            _job.schedule_removal()
                        except Exception:
                            pass
                    app.bot_data.pop("cf_poller_stop_at", None)
                    print("CF OAuth poller: auto-stopped (15 min timer expired)")
                    return
                _ig = app.bot_data.get("iglogin")
                if _ig is None:
                    return
                try:
                    _r = _req.get(f"{_cf_worker_url}/pending",
                                  params={"secret": _cf_poll_secret}, timeout=15)
                    if _r.status_code != 200:
                        return
                    pending = _r.json()
                except Exception as _e:
                    print(f"CF poller fetch error: {_e}")
                    return
                if not pending:
                    return
                for entry in pending:
                    _id = entry.get("id")
                    _payload = entry.get("code", "")
                    if "::" in _payload:
                        _code, _state = _payload.split("::", 1)
                    else:
                        _code, _state = _payload, ""
                    _consume = True
                    _notify = True
                    _msg_lines = []
                    try:
                        record = await asyncio.to_thread(_ig.exchange_code, _code)
                        username = record.get("username", "?")
                        user_id = record.get("user_id", "?")
                        expires = record.get("expires_at", "?")[:10]
                        _msg_lines.append(f"IG Login auto-connected: @{username}")
                        _msg_lines.append(f"User ID: {user_id}")
                        _msg_lines.append(f"Expires: {expires}")
                        try:
                            _cfg = {"model_name": username, "ig_user_id": user_id, "share_to_feed": True}
                            accounts_mod.add_account(_json.dumps(_cfg))
                            _msg_lines.append("Auto-added to bot accounts.")
                        except Exception as _e:
                            _msg_lines.append(f"Account add note: {_e}")
                        print(f"CF poller: connected @{username} ({user_id})")
                    except Exception as _e:
                        _err_str = str(_e)
                        _msg_lines.append(f"OAuth exchange failed: {_err_str}")
                        print(f"CF poller exchange failed for id={_id}: {_err_str}")
                        if "already been used" in _err_str.lower() or "authorization code has been used" in _err_str.lower():
                            _notify = False
                    if _notify:
                     for _aid in app.bot_data.get("allowed_user_ids", []):
                        try:
                            await app.bot.send_message(_aid, "\n".join(_msg_lines))
                        except Exception:
                            pass
                    if _consume:
                        try:
                            _req.get(f"{_cf_worker_url}/consume",
                                     params={"secret": _cf_poll_secret, "id": _id}, timeout=10)
                        except Exception:
                            pass

            app.bot_data["_cf_oauth_poll_task"] = _cf_oauth_poll_task
            # On-demand mode: poller starts only via /igconnect, auto-stops after 15 min
            print(f"CF OAuth poller: on-demand mode ({_cf_worker_url}/pending, triggered by /igconnect)")
        else:
            print("CF OAuth poller: CF_OAUTH_WORKER_URL or CF_OAUTH_POLL_SECRET not set, skipping")
    else:
        app.bot_data["iglogin"] = None
        app.bot_data["_iglogin_refresh_task"] = None

    # Set up scheduler with the post callback
    async def post_cb(queue_idx):
        await post_queue_item(app, queue_idx)

    # Wrap post_cb so we can detect "campaign done" and trigger the next
    # iteration automatically. Without this wrapper, the bot would post all
    # items in the queue and then sit idle forever (no new content pulled).
    # The trigger is non-fatal: if it fails, the loop continues silently.
    async def _post_cb_v3(_qidx):
        try:
            await post_cb(_qidx)
        finally:
            try:
                _s = app.bot_data.get("sched")
                # Filter to count only post jobs (IDs start with "post_").
                # The insights_poller is an interval job and is always in the
                # scheduler — without this filter, the check `len(jobs) == 0`
                # would never be True and the post-completion trigger would
                # never fire. (Bug discovered 2026-09-20: agency_a finished
                # 540 posts but didn't trigger iter 3 because of this.)
                _post_jobs = [j for j in _s.scheduler.get_jobs()
                              if (j.id or "").startswith("post_")]
                if _s and len(_post_jobs) == 0 and loop_mod.is_active():
                    # Give the just-fired post's status write a moment to land, then re-check.
                    import asyncio as _aio
                    await _aio.sleep(2)
                    _st = sched_mod.load_state() or {}
                    _pending_left = sum(1 for it in _st.get("queue", []) if it.get("status") not in ("posted","failed","cleared"))
                    if _pending_left > 0:
                        pass
                    else:
                        await _trigger_loop_iteration(app)
            except Exception:
                log.exception("loop check after post failed")

    # Store for main.py to use on restart (resume variant generation)
    app.bot_data["_run_variant_generation"] = _run_variant_generation
    sched = sched_mod.CampaignScheduler(_post_cb_v3)
    app.bot_data["sched"] = sched

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myig", cmd_myig))
    app.add_handler(CommandHandler("igadd", cmd_igadd))
    app.add_handler(CommandHandler("igtoken", cmd_igtoken))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("insightsrefresh", cmd_insightsrefresh))
    app.add_handler(CommandHandler("igremove", cmd_igremove))
    app.add_handler(CommandHandler("igremovebulk", cmd_igremovebulk))
    app.add_handler(CommandHandler("igcaption", cmd_igcaption))
    app.add_handler(CommandHandler("captions", cmd_captions))
    app.add_handler(CommandHandler("removecaption", cmd_removecaption))
    app.add_handler(CommandHandler("poolstatus", cmd_poolstatus))
    app.add_handler(CommandHandler("resetpool", cmd_resetpool))
    app.add_handler(CommandHandler("addcover", cmd_addcover))
    app.add_handler(CommandHandler("donecovers", cmd_donecovers))
    app.add_handler(CommandHandler("covers", cmd_covers))
    app.add_handler(CommandHandler("removecover", cmd_removecover))
    app.add_handler(CommandHandler("coverstatus", cmd_coverstatus))
    app.add_handler(CommandHandler("resetcovers", cmd_resetcovers))
    app.add_handler(CommandHandler("addhashtag", cmd_addhashtag))
    app.add_handler(CommandHandler("addcreator", cmd_addcreator))
    app.add_handler(CommandHandler("listtargets", cmd_listtargets))
    app.add_handler(CommandHandler("removetarget", cmd_removetarget))
    app.add_handler(CommandHandler("vaultstatus", cmd_vaultstatus))
    app.add_handler(CommandHandler("vaultlist", cmd_vaultlist))
    app.add_handler(CommandHandler("scrape", cmd_scrape))
    app.add_handler(CallbackQueryHandler(handle_vault_callback, pattern="^vlt_"))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("igcampaign", cmd_igcampaign))
    app.add_handler(CommandHandler("stoploop", cmd_stoploop))
    app.add_handler(CommandHandler("setn", cmd_setn))
    app.add_handler(CommandHandler("loopstatus", cmd_loopstatus))
    app.add_handler(CommandHandler("looptrigger", cmd_looptrigger))
    app.add_handler(CommandHandler("addlink", cmd_addlink))
    app.add_handler(CommandHandler("igconnect", cmd_igconnect))
    app.add_handler(CommandHandler("igauth", cmd_igauth))
    app.add_handler(CommandHandler("iglogintokens", cmd_iglogintokens))
    app.add_handler(CommandHandler("igconnectstop", cmd_igconnectstop))
    app.add_handler(CommandHandler("igconnectstatus", cmd_igconnectstatus))
    app.add_handler(CommandHandler("igsetproxy", cmd_igsetproxy))
    app.add_handler(CommandHandler("igclearproxy", cmd_igclearproxy))
    app.add_handler(CommandHandler("igproxies", cmd_igproxies))
    app.add_handler(CommandHandler("postnowigtest", cmd_postnowigtest))
    app.add_handler(CommandHandler("igrefresh", cmd_igrefresh))
    app.add_handler(CommandHandler("igcampaigngo", cmd_igcampaigngo))
    app.add_handler(CommandHandler("igschedule", cmd_igschedule))
    app.add_handler(CommandHandler("igforceschedule", cmd_igforceschedule))
    app.add_handler(CommandHandler("igqueuestatus", cmd_igqueuestatus))
    app.add_handler(CommandHandler("igqueueclear", cmd_igqueueclear))
    app.add_handler(CommandHandler("engagement", cmd_engagement))
    app.add_handler(CommandHandler("postnow", cmd_postnow))

    # Video router
    video_filter = filters.VIDEO | (
        filters.Document.MimeType("video/mp4") | filters.Document.MimeType("video/quicktime")
    )
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("librarystats", cmd_librarystats))
    app.add_handler(MessageHandler(video_filter, handle_video_message))

    # Photo router (covers upload)
    photo_filter = filters.PHOTO | (
        filters.Document.MimeType("image/jpeg") |
        filters.Document.MimeType("image/png") |
        filters.Document.MimeType("image/webp")
    )
    app.add_handler(MessageHandler(photo_filter, handle_photo_message))

    # Inline keyboard for /postnow
    app.add_handler(CallbackQueryHandler(handle_postnow_callback, pattern="^pn_"))

    # Plain text router (for awaiting_schedule, awaiting_captions)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_router))

    return app