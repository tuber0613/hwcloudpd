"""SQLite state store tracking synced Huawei Cloud Gallery media items."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from hwcloudpd.models import MediaItem


class StateStore:
    """Thread-safe SQLite index mapping cloud uniqueId to local archived files."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS synced_media (
                    unique_id TEXT PRIMARY KEY,
                    album_id TEXT NOT NULL,
                    album_name TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    checksum TEXT NOT NULL DEFAULT '',
                    local_sha256 TEXT NOT NULL DEFAULT '',
                    create_time_ms INTEGER NOT NULL DEFAULT 0,
                    local_rel_path TEXT NOT NULL,
                    size_variant TEXT NOT NULL DEFAULT 'original',
                    status TEXT NOT NULL,
                    synced_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_synced_media_rel_path ON synced_media(local_rel_path)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_synced_media_status ON synced_media(status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_synced_media_album ON synced_media(album_id)"
            )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get_record(self, unique_id: str) -> dict[str, Any] | None:
        """Return stored row for unique_id, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM synced_media WHERE unique_id = ?",
                (unique_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def find_by_rel_path(self, rel_path: str) -> dict[str, Any] | None:
        """Return active downloaded record occupying `rel_path`, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM synced_media WHERE local_rel_path = ? AND status = 'downloaded' LIMIT 1",
                (rel_path,),
            ).fetchone()
            return dict(row) if row is not None else None

    def is_synced_and_present(
        self,
        unique_id: str,
        root_dir: Path,
        size_variant: str,
    ) -> bool:
        """Return True if unique_id was already synced with matching variant and file exists on disk."""
        rec = self.get_record(unique_id)
        if rec is None:
            return False
        if rec["status"] != "downloaded":
            return False
        if rec["size_variant"] != size_variant:
            return False
        rel = rec["local_rel_path"]
        if not rel:
            return False
        return (root_dir / rel).is_file()

    def upsert_downloaded(
        self,
        item: MediaItem,
        *,
        local_rel_path: str,
        local_sha256: str,
        size_variant: str,
    ) -> None:
        """Insert or update a downloaded item record."""
        now_sec = int(time.time())
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    INSERT INTO synced_media (
                        unique_id, album_id, album_name, file_name, file_type,
                        file_size, checksum, local_sha256, create_time_ms,
                        local_rel_path, size_variant, status, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'downloaded', ?)
                    ON CONFLICT(unique_id) DO UPDATE SET
                        album_id = excluded.album_id,
                        album_name = excluded.album_name,
                        file_name = excluded.file_name,
                        file_type = excluded.file_type,
                        file_size = excluded.file_size,
                        checksum = excluded.checksum,
                        local_sha256 = excluded.local_sha256,
                        create_time_ms = excluded.create_time_ms,
                        local_rel_path = excluded.local_rel_path,
                        size_variant = excluded.size_variant,
                        status = 'downloaded',
                        synced_at = excluded.synced_at
                    """,
                    (
                        item.unique_id,
                        item.album_id,
                        item.album_name,
                        item.file_name,
                        item.file_type,
                        item.file_size,
                        item.checksum,
                        local_sha256,
                        item.create_time_ms,
                        local_rel_path,
                        size_variant,
                        now_sec,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def mark_deleted_local(self, unique_id: str) -> None:
        """Mark an item as deleted locally (e.g., via --auto-delete)."""
        with self._lock:
            self._conn.execute(
                "UPDATE synced_media SET status = 'deleted_local', synced_at = ? WHERE unique_id = ?",
                (int(time.time()), unique_id),
            )

    def mark_deleted_cloud(self, unique_id: str) -> None:
        """Mark an item as moved to cloud recycle bin."""
        with self._lock:
            self._conn.execute(
                "UPDATE synced_media SET status = 'deleted_cloud', synced_at = ? WHERE unique_id = ?",
                (int(time.time()), unique_id),
            )

    def iter_downloaded_records(self) -> list[dict[str, Any]]:
        """Return all records currently marked as downloaded."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM synced_media WHERE status = 'downloaded' ORDER BY create_time_ms DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        """Return counts grouped by status."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM synced_media GROUP BY status"
            ).fetchall()
            return {str(r["status"]): int(r["cnt"]) for r in rows}
