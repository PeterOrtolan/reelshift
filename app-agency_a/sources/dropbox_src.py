"""Dropbox provider — refresh-token OAuth (short-lived access tokens).

Cleanup strategy: READ-ONLY. We don't touch the agency's Dropbox folder.
Dedup relies on SQLite state (`known_remote_ids`) — any file we've ever
downloaded won't be picked up again, regardless of whether it's still
sitting in the source folder.

Supports two folder-reference styles:
  - `folder_path`: a path inside the token-owner's Dropbox
      e.g. "/reels/travis"
  - `shared_link`: a share URL to a folder in ANOTHER account's Dropbox
      e.g. "https://www.dropbox.com/scl/fo/xxxx/YYY?rlkey=ZZZ&dl=0"
Auto-detected by the leading "http" — no config key change required.

Shared-link mode caveats:
  - Dropbox does not populate FileMetadata.path_lower/path_display
    reliably for shared-link listings, so remote_id is constructed
    as "/<filename>" (the relative-to-shared-root path).
  - Nested content in shared links isn't supported (recursive=False).

IPv6 workaround:
  Contabo VPS containers have no IPv6 route, but Dropbox's
  content.dropboxapi.com returns AAAA records that Python's
  default resolver picks over available A records — causing all
  downloads to fail with `Network is unreachable`. We force
  urllib3 to prefer IPv4 at module load. Safe because everything
  else the bot talks to (Meta, Telegram, R2, Workers) is
  dual-stack and works fine over IPv4.

One-time app setup:
  Permissions: files.metadata.read + files.content.read + sharing.read
  (sharing.read is required to list/download via shared links).
  No write scopes needed anywhere.

Env (defaults; can be overridden per-account in configs/accounts/<user>.json):
  DROPBOX_APP_KEY
  DROPBOX_APP_SECRET
  DROPBOX_REFRESH_TOKEN
"""
from __future__ import annotations

# Force IPv4 for outbound HTTP — see IPv6 workaround note in docstring.
# Must come before importing `dropbox` (which imports urllib3).
import urllib3.util.connection as _u3_conn
_u3_conn.HAS_IPV6 = False

import logging
import os
from pathlib import Path
from typing import Iterable, Optional

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FileMetadata, SharedLink

from .base import RemoteFile, SourceProvider

log = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


class DropboxSource(SourceProvider):
    name = "dropbox"

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ):
        app_key = app_key or os.environ.get("DROPBOX_APP_KEY")
        app_secret = app_secret or os.environ.get("DROPBOX_APP_SECRET")
        refresh_token = refresh_token or os.environ.get("DROPBOX_REFRESH_TOKEN")
        if not (app_key and app_secret and refresh_token):
            raise RuntimeError(
                "Dropbox credentials missing "
                "(DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN)"
            )
        self._dbx = dropbox.Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
        )
        try:
            self._dbx.users_get_current_account()
        except AuthError as e:
            raise RuntimeError(f"Dropbox auth failed: {e}") from e

    # ---- interface ----

    def list_new(self, folder_ref: str, exclude_ids: Iterable[str]) -> list[RemoteFile]:
        if _is_shared_link(folder_ref):
            remotes = self._list_shared_link(folder_ref)
        else:
            remotes = self._list_owned(folder_ref)

        exclude = set(exclude_ids)
        remotes = [r for r in remotes if r.remote_id not in exclude]
        remotes.sort(key=lambda r: r.modified_at or 0)
        return remotes

    def download(self, folder_ref: str, remote_id: str, dest_path: Path) -> None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if _is_shared_link(folder_ref):
            # remote_id is "/<filename>" relative to the shared folder root.
            self._dbx.sharing_get_shared_link_file_to_file(
                download_path=str(dest_path),
                url=folder_ref,
                path=remote_id,
            )
        else:
            # remote_id is the full path in the owned Dropbox.
            self._dbx.files_download_to_file(str(dest_path), remote_id)

    def mark_consumed(self, folder_ref: str, remote_id: str, remote_name: str) -> None:
        """No-op — dedup relies on SQLite state, not remote moves."""
        return None

    # ---- helpers ----

    def _list_owned(self, folder_path: str) -> list[RemoteFile]:
        folder_path = _normalize_path(folder_path)
        try:
            res = self._dbx.files_list_folder(folder_path, recursive=False)
        except ApiError as e:
            log.warning("dropbox: list_folder %s failed: %s", folder_path, e)
            return []
        entries = list(res.entries)
        while res.has_more:
            res = self._dbx.files_list_folder_continue(res.cursor)
            entries.extend(res.entries)
        return [_entry_to_owned_remote(e) for e in entries if _is_video_file(e)]

    def _list_shared_link(self, url: str) -> list[RemoteFile]:
        try:
            res = self._dbx.files_list_folder(
                path="",
                recursive=False,
                shared_link=SharedLink(url=url),
            )
        except ApiError as e:
            log.warning("dropbox: list shared_link failed: %s", e)
            return []
        entries = list(res.entries)
        while res.has_more:
            res = self._dbx.files_list_folder_continue(res.cursor)
            entries.extend(res.entries)
        return [_entry_to_shared_remote(e) for e in entries if _is_video_file(e)]


def _is_video_file(entry) -> bool:
    return (
        isinstance(entry, FileMetadata)
        and any(entry.name.lower().endswith(ext) for ext in _VIDEO_EXTS)
    )


def _entry_to_owned_remote(e: FileMetadata) -> RemoteFile:
    return RemoteFile(
        remote_id=e.path_lower,
        name=e.name,
        size=e.size,
        modified_at=_epoch(e.server_modified),
    )


def _entry_to_shared_remote(e: FileMetadata) -> RemoteFile:
    # In shared-link mode, use "/" + name as the relative path — that's
    # what sharing_get_shared_link_file_to_file needs, and it's stable
    # across list calls unless the file is renamed.
    return RemoteFile(
        remote_id="/" + e.name,
        name=e.name,
        size=e.size,
        modified_at=_epoch(e.server_modified),
    )


def _epoch(dt) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def _is_shared_link(ref: str) -> bool:
    return ref.strip().lower().startswith("http")


def _normalize_path(p: str) -> str:
    p = p.strip()
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/")
