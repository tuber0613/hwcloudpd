"""Small filesystem helpers: cross-process file locks and streaming file hashes."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import time
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


class LockTimeoutError(TimeoutError):
    """Raised when an exclusive file lock could not be acquired in time."""


@contextmanager
def exclusive_file_lock(lock_path: Path, timeout_sec: float = 30.0) -> Iterator[None]:
    """Hold an exclusive advisory `flock` on `lock_path` (no-op where fcntl is unavailable)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            deadline = time.monotonic() + timeout_sec
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError(f"Timed out waiting for lock {lock_path}")
                    time.sleep(0.05)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def hash_file(path: Path) -> tuple[str, str, int]:
    """Return (sha256_hex, md5_hex, size) of a file, streaming in 1 MiB chunks."""
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
            md5.update(chunk)
            total += len(chunk)
    return sha.hexdigest(), md5.hexdigest(), total


def checksum_matches(checksum: str, sha256_hex: str, md5_hex: str) -> bool | None:
    """Compare a cloud checksum (sha256 or md5) to local digests; None when not comparable."""
    c = (checksum or "").strip().lower()
    if len(c) == 64:
        return c == sha256_hex
    if len(c) == 32:
        return c == md5_hex
    return None
