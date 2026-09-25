"""Google Drive provider — service account auth.

Cleanup strategy: STAR consumed files (per-user, invisible to the folder
owner). This means the SA only needs Viewer permission on the folder —
simpler ask for agency-owned folders.

Per-account setup on the agency side:
  1. Share the source folder with the SA email as Viewer (or Editor —
     both work; Viewer is the minimum).
  2. Grab the folder ID from the URL: drive.google.com/drive/folders/<ID>.
  3. Put the ID in configs/accounts/<user>.json under
     sources.gdrive.folder_id.

Env:
  GDRIVE_SA_JSON  — absolute path to service account credentials JSON
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Iterable, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from .base import RemoteFile, SourceProvider

log = logging.getLogger(__name__)

# drive.readonly can list + download, but starring is a write on the
# personal `starred` property — needs full drive scope for the SA identity.
_SCOPES = ["https://www.googleapis.com/auth/drive"]


class GDriveSource(SourceProvider):
    name = "gdrive"

    def __init__(self, sa_json_path: Optional[str] = None):
        sa_json_path = sa_json_path or os.environ.get("GDRIVE_SA_JSON")
        if not sa_json_path or not Path(sa_json_path).is_file():
            raise RuntimeError(
                f"GDRIVE_SA_JSON not set or not found: {sa_json_path!r}"
            )
        creds = service_account.Credentials.from_service_account_file(
            sa_json_path, scopes=_SCOPES
        )
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    # ---- interface ----

    def list_new(self, folder_ref: str, exclude_ids: Iterable[str]) -> list[RemoteFile]:
        exclude = set(exclude_ids)
        # Only videos, not starred (starred = already consumed by us),
        # not trashed.
        q = (
            f"'{folder_ref}' in parents "
            f"and trashed=false "
            f"and starred=false "
            f"and mimeType contains 'video/'"
        )
        out: list[RemoteFile] = []
        page_token: Optional[str] = None
        while True:
            resp = self._svc.files().list(
                q=q,
                fields="nextPageToken, files(id, name, size, modifiedTime)",
                orderBy="modifiedTime",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for f in resp.get("files", []):
                if f["id"] in exclude:
                    continue
                out.append(RemoteFile(
                    remote_id=f["id"],
                    name=f["name"],
                    size=int(f["size"]) if f.get("size") else None,
                    modified_at=_iso_to_epoch(f.get("modifiedTime")),
                ))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    def download(self, folder_ref: str, remote_id: str, dest_path: Path) -> None:
        # folder_ref unused for Drive — file_id is globally addressable.
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        req = self._svc.files().get_media(fileId=remote_id, supportsAllDrives=True)
        # 10MB chunks; keep memory bounded on large source reels
        with io.FileIO(str(dest_path), "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=10 * 1024 * 1024)
            done = False
            while not done:
                status, done = dl.next_chunk()
                if status:
                    log.debug("gdrive %s: %.0f%%", remote_id, status.progress() * 100)

    def mark_consumed(self, folder_ref: str, remote_id: str, remote_name: str) -> None:
        """Star the file so list_new skips it on the next pass.

        `folder_ref` is unused (star is a file-level property, not
        folder-scoped) but kept in the signature to match the provider ABC.
        Star is per-user — the agency's Drive view is unaffected.
        """
        try:
            self._svc.files().update(
                fileId=remote_id,
                body={"starred": True},
                fields="id, starred",
                supportsAllDrives=True,
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                log.info("gdrive: %s already gone, skipping star", remote_id)
                return
            log.warning("gdrive: star %s failed: %s", remote_name, e)
            raise


def _iso_to_epoch(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    from datetime import datetime, timezone
    try:
        # Drive returns e.g. "2025-09-14T12:34:56.789Z"
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.astimezone(timezone.utc).timestamp())
    except ValueError:
        return None
