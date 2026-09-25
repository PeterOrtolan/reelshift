"""Base types for content source providers (Google Drive, Dropbox, ...)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class RemoteFile:
    """A file listed on a remote provider, not yet downloaded."""
    remote_id: str         # gdrive file_id or dropbox path_lower
    name: str
    size: Optional[int]
    modified_at: Optional[int]   # unix seconds; providers may omit
    created_at: Optional[int] = None  # unix seconds; providers may omit


@dataclass
class SourceItem:
    """A downloaded source file, tracked in the state DB."""
    id: int                # sqlite rowid
    provider: str          # 'gdrive' | 'dropbox'
    account: str           # ig_username
    remote_id: str
    remote_name: str
    local_path: str        # absolute path under INBOX_DIR
    status: str            # 'pending' | 'posted' | 'cleared'
    ig_media_id: Optional[str] = None


class SourceProvider(ABC):
    """One provider per (agency deployment, remote service).

    A provider knows how to enumerate + fetch source videos for a given
    IG account from a remote folder, and how to move consumed files
    into a `_posted/` subfolder for `/igqueueclear`.
    """

    name: str  # 'gdrive' | 'dropbox' — set on subclass

    @abstractmethod
    def list_new(self, folder_ref: str, exclude_ids: Iterable[str]) -> list[RemoteFile]:
        """List video files in `folder_ref`, excluding remote_ids already tracked.

        `folder_ref` is the provider-native folder handle:
          - gdrive: folder_id
          - dropbox: a path (`/reels/acct`) OR a shared-link URL

        Results are ordered oldest-first so pull_next(N) picks the earliest
        un-posted content.
        """

    @abstractmethod
    def download(self, folder_ref: str, remote_id: str, dest_path: Path) -> None:
        """Download one file to `dest_path`. Creates parent dirs.

        `folder_ref` is included for providers that need it at download
        time (e.g. Dropbox shared links). Providers that don't need it
        can ignore it.

        Must be idempotent on partial failure: if raises, `dest_path` may
        exist but caller will delete it.
        """

    @abstractmethod
    def mark_consumed(self, folder_ref: str, remote_id: str, remote_name: str) -> None:
        """Mark a consumed file so `list_new` won't return it again.

        Called by `/igqueueclear`. Strategy is per-provider:
          - GDrive: star the file (per-user, invisible to folder owner)
          - Dropbox: move into a `_posted/` subfolder of `folder_ref`

        Safe to no-op if the file is already gone from the source.
        """
