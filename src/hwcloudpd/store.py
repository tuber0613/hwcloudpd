"""SQLite state store tracking synced Huawei Cloud Gallery media items."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable

from hwcloudpd.models import MediaItem

SCHEMA_VERSION = 3

# Local-side status of a record.
STATUS_DOWNLOADED = "downloaded"
STATUS_DELETED_LOCAL = "deleted_local"

# Cloud-side state of a record (independent of the local status).
CLOUD_PRESENT = "present"
CLOUD_DELETE_PENDING = "delete_pending"  # we asked Huawei to recycle it; outcome not yet confirmed
CLOUD_DELETED = "deleted"  # confirmed moved to the Recycle Bin by hwcloudpd
CLOUD_DELETE_FAILED = "delete_failed"  # still in the cloud; a move-mode deletion must be retried
CLOUD_MISSING = "missing"  # no longer visible anywhere in the cloud (purged by user / expiry)

_V2_COLUMNS: dict[str, str] = {
    "owner_id": "TEXT NOT NULL DEFAULT ''",
    "album_version": "TEXT NOT NULL DEFAULT ''",
    "is_shared": "INTEGER NOT NULL DEFAULT 0",
    "cloud_state": f"TEXT NOT NULL DEFAULT '{CLOUD_PRESENT}'",
    "extra_paths": "TEXT NOT NULL DEFAULT '[]'",
}

_V3_SYNCED_COLUMNS: dict[str, str] = {
    "is_fav": "INTEGER NOT NULL DEFAULT 0",
    "cloud_delete_attempts": "INTEGER NOT NULL DEFAULT 0",
}

_V3_FAILURE_COLUMNS: dict[str, str] = {
    "stub_json": "TEXT NOT NULL DEFAULT ''",
}


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

    # ------------------------------------------------------------------ schema

    def _columns(self, table: str) -> set[str]:
        return {str(r["name"]) for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=10000;")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])

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
                """
                CREATE TABLE IF NOT EXISTS download_failures (
                    unique_id TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_attempt_at INTEGER NOT NULL DEFAULT 0,
                    next_retry_at INTEGER NOT NULL DEFAULT 0,
                    stub_json TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS burst_cache (
                    cover_uid TEXT PRIMARY KEY,
                    members_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

            if version < 2:
                self._conn.execute("BEGIN")
                try:
                    existing = self._columns("synced_media")
                    for col, ddl in _V2_COLUMNS.items():
                        if col not in existing:
                            self._conn.execute(f"ALTER TABLE synced_media ADD COLUMN {col} {ddl}")
                    # v1 overloaded `status` with the cloud side; split it into cloud_state.
                    self._conn.execute(
                        "UPDATE synced_media SET status = ?, cloud_state = ? WHERE status = 'deleted_cloud'",
                        (STATUS_DOWNLOADED, CLOUD_DELETED),
                    )
                    self._conn.execute("PRAGMA user_version = 2")
                    self._conn.execute("COMMIT")
                    version = 2
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

            if version < 3:
                self._conn.execute("BEGIN")
                try:
                    existing_synced = self._columns("synced_media")
                    for col, ddl in _V3_SYNCED_COLUMNS.items():
                        if col not in existing_synced:
                            self._conn.execute(f"ALTER TABLE synced_media ADD COLUMN {col} {ddl}")
                    existing_fail = self._columns("download_failures")
                    for col, ddl in _V3_FAILURE_COLUMNS.items():
                        if col not in existing_fail:
                            self._conn.execute(
                                f"ALTER TABLE download_failures ADD COLUMN {col} {ddl}"
                            )
                    self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_synced_media_rel_path ON synced_media(local_rel_path)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_synced_media_status ON synced_media(status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_synced_media_album ON synced_media(album_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_synced_media_cloud_state ON synced_media(cloud_state)"
            )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ records

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
                "SELECT * FROM synced_media WHERE local_rel_path = ? AND status = ? LIMIT 1",
                (rel_path, STATUS_DOWNLOADED),
            ).fetchone()
            return dict(row) if row is not None else None

    def is_synced_and_present(
        self,
        unique_id: str,
        root_dir: Path,
        size_variant: str,
    ) -> bool:
        """Return True if unique_id was already synced with matching variant and file exists on disk.

        The cloud-side state is deliberately ignored: an item we moved to the Recycle Bin and the
        user later restored is still backed up locally and must not be downloaded twice.
        """
        rec = self.get_record(unique_id)
        if rec is None:
            return False
        if rec["status"] != STATUS_DOWNLOADED:
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
        extra_paths: Iterable[str] | None = None,
    ) -> None:
        """Insert or update a downloaded item record (cloud_state resets to present).

        When `extra_paths` is None, any existing sidecar paths recorded for this `unique_id`
        are preserved on conflict instead of being wiped to `[]`.
        """
        now_sec = int(time.time())
        extras_val = (
            json.dumps(sorted(set(extra_paths)), ensure_ascii=False)
            if extra_paths is not None
            else None
        )
        extras_update = (
            "extra_paths = excluded.extra_paths"
            if extras_val is not None
            else "extra_paths = synced_media.extra_paths"
        )
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    f"""
                    INSERT INTO synced_media (
                        unique_id, album_id, album_name, file_name, file_type,
                        file_size, checksum, local_sha256, create_time_ms,
                        local_rel_path, size_variant, status, synced_at,
                        owner_id, album_version, is_shared, cloud_state, extra_paths,
                        is_fav, cloud_delete_attempts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
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
                        status = excluded.status,
                        synced_at = excluded.synced_at,
                        owner_id = excluded.owner_id,
                        album_version = excluded.album_version,
                        is_shared = excluded.is_shared,
                        cloud_state = excluded.cloud_state,
                        {extras_update},
                        is_fav = excluded.is_fav,
                        cloud_delete_attempts = 0
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
                        STATUS_DOWNLOADED,
                        now_sec,
                        item.owner_id or "",
                        item.album_version or "",
                        1 if item.is_shared else 0,
                        CLOUD_PRESENT,
                        extras_val if extras_val is not None else "[]",
                        1 if item.is_fav else 0,
                    ),
                )
                self._conn.execute(
                    "DELETE FROM download_failures WHERE unique_id = ?", (item.unique_id,)
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def add_extra_paths(self, unique_id: str, rel_paths: Iterable[str]) -> None:
        """Record sidecar / extracted files (XMP, Motion Photo MP4) belonging to an item."""
        new = [p for p in rel_paths if p]
        if not new:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT extra_paths FROM synced_media WHERE unique_id = ?", (unique_id,)
            ).fetchone()
            if row is None:
                return
            try:
                current = set(json.loads(row["extra_paths"] or "[]"))
            except (TypeError, ValueError):
                current = set()
            current.update(new)
            self._conn.execute(
                "UPDATE synced_media SET extra_paths = ? WHERE unique_id = ?",
                (json.dumps(sorted(current), ensure_ascii=False), unique_id),
            )

    @staticmethod
    def extra_paths_of(record: dict[str, Any]) -> list[str]:
        try:
            value = json.loads(record.get("extra_paths") or "[]")
        except (TypeError, ValueError):
            return []
        return [str(v) for v in value if v]

    def mark_deleted_local(self, unique_id: str) -> None:
        """Mark an item as deleted locally (e.g., via --auto-delete)."""
        with self._lock:
            self._conn.execute(
                "UPDATE synced_media SET status = ?, synced_at = ? WHERE unique_id = ?",
                (STATUS_DELETED_LOCAL, int(time.time()), unique_id),
            )

    def update_is_fav(self, unique_id: str, is_fav: int) -> None:
        """Update cached favorite flag for an existing synced record."""
        with self._lock:
            self._conn.execute(
                "UPDATE synced_media SET is_fav = ? WHERE unique_id = ?",
                (1 if is_fav else 0, unique_id),
            )

    def set_cloud_state(self, unique_ids: Iterable[str], state: str) -> None:
        """Set cloud_state for many records atomically."""
        ids = list(unique_ids)
        if not ids:
            return
        reset_attempts = state in (CLOUD_PRESENT, CLOUD_DELETED, CLOUD_MISSING)
        sql = (
            "UPDATE synced_media SET cloud_state = ?, cloud_delete_attempts = 0 WHERE unique_id = ?"
            if reset_attempts
            else "UPDATE synced_media SET cloud_state = ? WHERE unique_id = ?"
        )
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, [(state, uid) for uid in ids])
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def increment_cloud_delete_attempts(self, unique_ids: Iterable[str]) -> None:
        """Increment retry counter for items whose cloud deletion failed."""
        ids = list(unique_ids)
        if not ids:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(
                    "UPDATE synced_media SET cloud_delete_attempts = cloud_delete_attempts + 1 "
                    "WHERE unique_id = ?",
                    [(uid,) for uid in ids],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def mark_deleted_cloud(self, unique_id: str) -> None:
        """Mark an item as confirmed moved to the cloud Recycle Bin by hwcloudpd."""
        self.set_cloud_state([unique_id], CLOUD_DELETED)

    def iter_records(
        self,
        *,
        status: str | None = STATUS_DOWNLOADED,
        cloud_states: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return records filtered by local status and/or cloud states."""
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        states = list(cloud_states or [])
        if states:
            clauses.append(f"cloud_state IN ({','.join('?' for _ in states)})")
            params.extend(states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM synced_media {where} ORDER BY create_time_ms DESC", params
            ).fetchall()
            return [dict(r) for r in rows]

    def iter_downloaded_records(self) -> list[dict[str, Any]]:
        """Return all records currently marked as downloaded."""
        return self.iter_records(status=STATUS_DOWNLOADED)

    def count_by_status(self) -> dict[str, int]:
        """Return counts grouped by status."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM synced_media GROUP BY status"
            ).fetchall()
            return {str(r["status"]): int(r["cnt"]) for r in rows}

    def total_records(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM synced_media").fetchone()[0])

    # ------------------------------------------------------------------ failures

    def record_failure(
        self,
        unique_id: str,
        error: str,
        *,
        stub: dict[str, Any] | None = None,
        base_delay_sec: int = 300,
        max_delay_sec: int = 86400,
    ) -> int:
        """Record a download failure and schedule an exponential-backoff retry. Return attempts."""
        now = int(time.time())
        stub_str = json.dumps(stub, ensure_ascii=False) if stub is not None else ""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts, stub_json FROM download_failures WHERE unique_id = ?",
                (unique_id,),
            ).fetchone()
            attempts = (int(row["attempts"]) if row else 0) + 1
            if not stub_str and row is not None:
                stub_str = str(row["stub_json"] or "")
            delay = min(max_delay_sec, base_delay_sec * (2 ** (attempts - 1)))
            self._conn.execute(
                """
                INSERT INTO download_failures (
                    unique_id, attempts, last_error, last_attempt_at, next_retry_at, stub_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(unique_id) DO UPDATE SET
                    attempts = excluded.attempts,
                    last_error = excluded.last_error,
                    last_attempt_at = excluded.last_attempt_at,
                    next_retry_at = excluded.next_retry_at,
                    stub_json = excluded.stub_json
                """,
                (unique_id, attempts, str(error)[:500], now, now + delay, stub_str),
            )
            return attempts

    def get_due_failures(self, now: float | None = None) -> list[dict[str, Any]]:
        """Return previously failed downloads whose backoff window has elapsed."""
        ts = int(now if now is not None else time.time())
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM download_failures WHERE next_retry_at <= ? ORDER BY last_attempt_at ASC",
                (ts,),
            ).fetchall()
            return [dict(r) for r in rows]

    def is_failure_deferred(self, unique_id: str, now: float | None = None) -> bool:
        """True if a previous failure's backoff window has not elapsed yet."""
        ts = int(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT next_retry_at FROM download_failures WHERE unique_id = ?", (unique_id,)
            ).fetchone()
            return bool(row) and int(row["next_retry_at"]) > ts

    def clear_failure(self, unique_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM download_failures WHERE unique_id = ?", (unique_id,))

    def failure_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM download_failures").fetchone()[0])

    # ------------------------------------------------------------------ bursts

    def get_burst_members(self, cover_uid: str) -> list[dict[str, Any]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT members_json FROM burst_cache WHERE cover_uid = ?", (cover_uid,)
            ).fetchone()
        if row is None:
            return None
        try:
            members = json.loads(row["members_json"])
        except (TypeError, ValueError):
            return None
        return members if isinstance(members, list) else None

    def set_burst_members(self, cover_uid: str, members: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO burst_cache (cover_uid, members_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(cover_uid) DO UPDATE SET
                    members_json = excluded.members_json, updated_at = excluded.updated_at
                """,
                (cover_uid, json.dumps(members, ensure_ascii=False), int(time.time())),
            )

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
