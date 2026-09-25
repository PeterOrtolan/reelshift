"""
link_downloader.py - pull videos from Dropbox and Google Drive shared links.

Supports single files and folders. Tracks used files in SQLite so folder
re-downloads auto-skip anything already posted.
"""
import os
import re
import io
import zipfile
import sqlite3
import logging
from pathlib import Path
from typing import Iterator, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests

log = logging.getLogger("link_downloader")

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
CHUNK = 1024 * 1024
TIMEOUT = 300


class LinkDownloader:
    def __init__(self, google_api_key: Optional[str], used_db_path: str):
        self.gkey = google_api_key
        self.db_path = used_db_path
        Path(used_db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS used_links (
                    source_id TEXT PRIMARY KEY,
                    name      TEXT,
                    used_at   TEXT DEFAULT (datetime('now'))
                )
            """)

    def is_used(self, source_id: str) -> bool:
        with sqlite3.connect(self.db_path) as c:
            row = c.execute("SELECT 1 FROM used_links WHERE source_id = ?", (source_id,)).fetchone()
        return row is not None

    def mark_used(self, source_id: str, name: str = ""):
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT OR IGNORE INTO used_links (source_id, name) VALUES (?, ?)", (source_id, name))

    def list_used(self, limit: int = 50):
        with sqlite3.connect(self.db_path) as c:
            return c.execute("SELECT source_id, name, used_at FROM used_links ORDER BY used_at DESC LIMIT ?", (limit,)).fetchall()

    def detect(self, url: str) -> Tuple[str, str, str]:
        u = url.strip()
        host = urlparse(u).netloc.lower()
        if "dropbox.com" in host:
            if "/scl/fo/" in u or "/sh/" in u:
                return ("dropbox", "folder", u)
            if "/scl/fi/" in u or "/s/" in u:
                return ("dropbox", "file", u)
            raise ValueError("Unrecognized Dropbox URL")
        if "drive.google.com" in host or "docs.google.com" in host:
            m = re.search(r"/folders/([a-zA-Z0-9_-]+)", u)
            if m:
                return ("drive", "folder", m.group(1))
            m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", u)
            if m:
                return ("drive", "file", m.group(1))
            qs = parse_qs(urlparse(u).query)
            if "id" in qs:
                return ("drive", "file", qs["id"][0])
            raise ValueError("Unrecognized Google Drive URL")
        raise ValueError(f"Unsupported URL host: {host}")

    def download(self, url: str, dest_dir: str, skip_used: bool = True) -> Iterator[dict]:
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        provider, kind, ref = self.detect(url)
        log.info(f"link_downloader: provider={provider} kind={kind}")
        if provider == "dropbox" and kind == "file":
            yield from self._dropbox_file(ref, dest_dir, skip_used)
        elif provider == "dropbox" and kind == "folder":
            yield from self._dropbox_folder(ref, dest_dir, skip_used)
        elif provider == "drive" and kind == "file":
            yield from self._drive_file(ref, dest_dir, skip_used)
        elif provider == "drive" and kind == "folder":
            yield from self._drive_folder(ref, dest_dir, skip_used)

    @staticmethod
    def _force_dropbox_dl(url: str) -> str:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        qs["dl"] = ["1"]
        new_q = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(parsed._replace(query=new_q))

    def _dropbox_file(self, url: str, dest_dir: str, skip_used: bool):
        source_id = f"dropbox:{urlparse(url).path}"
        name = Path(urlparse(url).path).name
        if skip_used and self.is_used(source_id):
            yield {"status": "skipped", "source_id": source_id, "name": name}
            return
        dl_url = self._force_dropbox_dl(url)
        try:
            with requests.get(dl_url, stream=True, timeout=TIMEOUT, allow_redirects=True) as r:
                r.raise_for_status()
                name = self._filename_from_response(r, fallback=name)
                out = Path(dest_dir) / name
                with open(out, "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        f.write(chunk)
            yield {"status": "ok", "path": str(out), "name": name, "source_id": source_id}
        except Exception as e:
            yield {"status": "error", "source_id": source_id, "name": name, "error": str(e)}

    def _dropbox_folder(self, url: str, dest_dir: str, skip_used: bool):
        dl_url = self._force_dropbox_dl(url)
        try:
            with requests.get(dl_url, stream=True, timeout=TIMEOUT, allow_redirects=True) as r:
                r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_content(CHUNK):
                    buf.write(chunk)
            buf.seek(0)
            with zipfile.ZipFile(buf) as zf:
                video_members = [
                    m for m in zf.namelist()
                    if Path(m).suffix.lower() in VIDEO_EXTS and not m.endswith("/")
                ]
                log.info(f"dropbox folder: {len(video_members)} videos found")
                for member in video_members:
                    source_id = f"dropbox:{urlparse(url).path}::{member}"
                    name = Path(member).name
                    if skip_used and self.is_used(source_id):
                        yield {"status": "skipped", "source_id": source_id, "name": name}
                        continue
                    out = Path(dest_dir) / name
                    with zf.open(member) as src, open(out, "wb") as dst:
                        while True:
                            chunk = src.read(CHUNK)
                            if not chunk:
                                break
                            dst.write(chunk)
                    yield {"status": "ok", "path": str(out), "name": name, "source_id": source_id}
        except Exception as e:
            yield {"status": "error", "source_id": f"dropbox:{url}", "name": "", "error": str(e)}

    def _drive_meta(self, file_id: str) -> dict:
        if not self.gkey:
            raise RuntimeError("google_drive_api_key not configured")
        r = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"key": self.gkey, "fields": "id,name,mimeType,size"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _drive_file(self, file_id: str, dest_dir: str, skip_used: bool):
        source_id = f"drive:{file_id}"
        try:
            meta = self._drive_meta(file_id)
            name = meta.get("name", f"{file_id}.mp4")
        except Exception as e:
            yield {"status": "error", "source_id": source_id, "name": "", "error": f"metadata: {e}"}
            return
        if skip_used and self.is_used(source_id):
            yield {"status": "skipped", "source_id": source_id, "name": name}
            return
        try:
            with requests.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"key": self.gkey, "alt": "media"},
                stream=True, timeout=TIMEOUT,
            ) as r:
                r.raise_for_status()
                out = Path(dest_dir) / name
                with open(out, "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        f.write(chunk)
            yield {"status": "ok", "path": str(out), "name": name, "source_id": source_id}
        except Exception as e:
            yield {"status": "error", "source_id": source_id, "name": name, "error": str(e)}

    def _drive_folder(self, folder_id: str, dest_dir: str, skip_used: bool):
        if not self.gkey:
            yield {"status": "error", "source_id": f"drive:{folder_id}", "name": "", "error": "google_drive_api_key not configured"}
            return
        page_token = None
        files = []
        try:
            while True:
                params = {
                    "key": self.gkey,
                    "q": f"'{folder_id}' in parents and mimeType contains 'video/' and trashed = false",
                    "fields": "nextPageToken, files(id,name,mimeType,size)",
                    "pageSize": 1000,
                }
                if page_token:
                    params["pageToken"] = page_token
                r = requests.get("https://www.googleapis.com/drive/v3/files", params=params, timeout=30)
                r.raise_for_status()
                data = r.json()
                files.extend(data.get("files", []))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            yield {"status": "error", "source_id": f"drive:{folder_id}", "name": "", "error": f"list: {e}"}
            return
        log.info(f"drive folder {folder_id}: {len(files)} videos found")
        for meta in files:
            yield from self._drive_file(meta["id"], dest_dir, skip_used)

    @staticmethod
    def _filename_from_response(r, fallback: str) -> str:
        cd = r.headers.get("content-disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
        if m:
            return os.path.basename(m.group(1))
        return fallback
