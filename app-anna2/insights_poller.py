"""Background poller for refreshing insights cache.

Called by APScheduler every 6 hours. Runs in a worker thread.
Failures here never affect posting.
"""
import logging

import accounts as accounts_mod
import insights as insights_mod

log = logging.getLogger("insights_poller")


def run_refresh(default_token: str):
    """Refresh insights for all accounts. Safe to call from APScheduler."""
    log.info("Background insights refresh: starting")
    try:
        accounts = accounts_mod.list_accounts()
    except Exception as e:
        log.error(f"Background refresh: failed to list accounts: {e}")
        return
    if not accounts:
        log.info("Background refresh: no accounts, skipping")
        return
    try:
        total = insights_mod.refresh_all(accounts, default_token)
        log.info(f"Background refresh complete: {total} posts across {len(accounts)} accounts")
    except Exception as e:
        log.exception(f"Background refresh failed: {e}")
