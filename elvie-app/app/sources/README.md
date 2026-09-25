# `app.sources` — Google Drive + Dropbox ingestion

Downloads source reels from remote folders into `INBOX_DIR/<ig_username>/`
so `/igcampaign` can hand them to the ffmpeg variation stage unchanged.

---

## 1. Install deps

```bash
pip install google-api-python-client google-auth dropbox
```

Add to `requirements.txt`:
```
google-api-python-client>=2.140
google-auth>=2.35
dropbox>=12.0
```

Rebuild the image (touches `requirements.txt`):
```bash
cd /opt/reelshift-graph && docker compose build && docker compose up -d
```

---

## 2. Env vars

Add to `/data/.env` (or per-deployment env_file for anna):
```
INBOX_DIR=/data/inbox
SOURCES_DB=/data/state/sources.db
ACCOUNTS_DIR=/app/configs/accounts

# Google Drive (one service account, shared across agencies)
GDRIVE_SA_JSON=/data/creds/gdrive-sa.json

# Dropbox (single shared app; per-account overrides possible later)
DROPBOX_APP_KEY=...
DROPBOX_APP_SECRET=...
DROPBOX_REFRESH_TOKEN=...
```

Bind-mount `/data/creds/` and `/data/inbox/` if not already mounted.

---

## 3. Google Drive service account

1. GCP Console → IAM → Service Accounts → Create.
   No roles required — Drive access is granted per-folder by sharing.
2. Keys tab → Add Key → JSON → save as `/data/creds/gdrive-sa.json`
   on the VPS, `chmod 600`.
3. Note the service account email (e.g. `reelshift@your-proj.iam.gserviceaccount.com`).
4. Send that email to each agency: they share their per-account source
   folders with it as **Editor** (Editor is required — we move consumed
   files into `_posted/`).
5. For each account, capture the folder ID from the URL
   (`drive.google.com/drive/folders/<ID>`) and put it in the account
   config (below).

---

## 4. Dropbox app + refresh token

Do this once per Meta app / agency grouping. One shared app is fine
unless an agency insists on their own.

1. dropbox.com/developers/apps → Create app
   - API: Scoped access
   - Type: **App folder** (simpler) or **Full Dropbox** (if reusing existing folders)
   - Permissions tab, enable:
     - `files.metadata.read`
     - `files.content.read`
     - `files.content.write` (needed for move-to-`_posted`)
2. In the app's Settings tab, note **App key** and **App secret**.
3. Get a refresh token — run this once from your laptop:

```python
# get_dropbox_refresh_token.py
import dropbox

APP_KEY = "..."
APP_SECRET = "..."

flow = dropbox.DropboxOAuth2FlowNoRedirect(
    APP_KEY, APP_SECRET, token_access_type="offline"
)
print("Open:", flow.start())
code = input("Auth code: ").strip()
result = flow.finish(code)
print("REFRESH TOKEN:", result.refresh_token)
```

Put the refresh token into `DROPBOX_REFRESH_TOKEN`. It doesn't expire
unless revoked.

---

## 5. Per-account config

`configs/accounts/<username>.json` — add a `sources` block. Either
provider is optional; both is fine.

```json
{
    "ig_username": "j4d3n_d1az",
    "ig_user_id": "17841...",
    "sources": {
        "gdrive":  { "folder_id":   "1AbcXyZ..." },
        "dropbox": { "folder_path": "/reels/j4d3n_d1az" }
    }
}
```

When both are configured, `pull_next` drains **gdrive first, then dropbox**.

Accounts without a `sources` block just skip the ingestion step (backward
compatible — existing manual-upload flows keep working).

---

## 6. Bot integration

### `/igcampaign` (single) and `/igcampaign N` (even 24h distribution)

Before the variation step, replace whatever currently loads the source
video with a call to `pull_next`:

```python
# in _igcampaign_process (or wherever the variation stage starts)
from app.sources import pull_next, mark_posted

async def _run_for_account(username: str, n: int, ...):
    items = pull_next(username, count=n)
    if not items:
        await update.message.reply_text(f"⚠️ No new source content for {username}")
        return
    if len(items) < n:
        await update.message.reply_text(
            f"⚠️ Only {len(items)}/{n} source clips available for {username}"
        )

    for item in items:
        # existing variation + upload pipeline, but use item.local_path
        variant_path = build_variant(item.local_path, ...)
        media_id = await _post_reel_sync(variant_path, base_url=..., ...)
        if media_id:
            mark_posted(item.id, ig_media_id=media_id)
```

Two things to note:

- **Call `mark_posted` from inside `_post_reel_sync`'s completion path
  as well as `/postnow`.** Anywhere a reel actually publishes, the item
  transitions to `posted`. Otherwise `/igqueueclear` never picks it up.
- For `/igcampaign N`, `pull_next(username, count=N)` returns N items
  eagerly. Scheduling then hands them out to APScheduler jobs; each
  job's `on-success` callback fires `mark_posted` for its own item.

### `/igqueueclear`

New command handler:

```python
# somewhere near other command handlers in bot_worker.py
from app.sources import clear_posted

async def igqueueclear(update, context):
    # optional arg: /igqueueclear <username> restricts to one account
    account = context.args[0] if context.args else None
    moved, failed = clear_posted(account=account)
    scope = account or "all accounts"
    await update.message.reply_text(
        f"🧹 {scope}: moved {moved} to _posted/, {failed} failed"
    )

# register:
application.add_handler(CommandHandler("igqueueclear", igqueueclear))
```

Suggested cadence: run it manually after big batches, or wire a
`job_queue.run_daily(clear_posted, ...)` at, say, 04:00 UTC.

---

## 7. State DB layout

`/data/state/sources.db` — one SQLite file per deployment, matches the
existing `state/*.json` convention.

```
source_items (id, provider, account, remote_id, remote_name,
              local_path, downloaded_at, status, posted_at,
              cleared_at, ig_media_id)
```

Status lifecycle:

```
             download           publish            /igqueueclear
    (none) ─────────► pending ─────────► posted ────────────────► cleared
                                  (mark_posted)     (clear_posted)
```

`known_remote_ids()` returns everything ever seen for
(provider, account), so a file that's been downloaded but not yet
published won't be re-fetched, and a posted-but-not-cleared file
also won't be re-fetched (it's still in the source folder until
`/igqueueclear`).

---

## 8. Failure modes worth knowing

- **Partial download** — local file is unlinked, no DB row inserted;
  next `pull_next` retries cleanly.
- **`_posted` folder missing on remote** — created lazily on first
  successful move.
- **Source file manually deleted before `/igqueueclear`** — move step
  swallows 404, still marks `cleared` locally.
- **Dropbox short-lived token expired** — SDK refreshes automatically
  from the refresh token; no code path needed.
- **Broken provider** (bad creds, revoked share) — logged and skipped;
  other providers keep working. `pull_next` never raises upward.
