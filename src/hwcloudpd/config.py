"""Configuration parsers and environment variable helpers."""

from __future__ import annotations

from datetime import datetime
import os
import re
from zoneinfo import ZoneInfo

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1000,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1000**4,
    "tib": 1024**4,
}


def parse_byte_size(value: str | int | None) -> int | None:
    """Parse human-readable size strings like '50MB', '1.5GiB', or '1024' into bytes."""
    if value is None:
        return None
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"Size must be non-negative, got {value}")
        return value
    raw = str(value).strip()
    if not raw:
        return None
    m = _SIZE_RE.match(raw)
    if not m:
        raise ValueError(f"Invalid size specification: '{value}'")
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit not in _SIZE_UNITS:
        raise ValueError(f"Unsupported size unit '{unit}' in '{value}'")
    return int(num * _SIZE_UNITS[unit])


def parse_date_bound(value: str | None, tz_name: str = "Asia/Shanghai", *, end_of_day: bool = False) -> int | None:
    """Parse a date/datetime/epoch string into epoch milliseconds."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    # Allow numeric epoch seconds or milliseconds directly.
    if re.fullmatch(r"\d+", raw):
        num = int(raw)
        return num if num >= 10**12 else num * 1000

    tz = ZoneInfo(tz_name)
    # Handle YYYY-MM-DD specifically so end_of_day can include the full calendar day.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=tz)
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
        return int(dt.timestamp() * 1000)

    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                dt = None  # type: ignore[assignment]
        if dt is None:
            raise ValueError(
                f"Invalid date format '{value}'. Expected YYYY-MM-DD, ISO-8601, or epoch."
            ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp() * 1000)


def env_str(name: str, default: str | None = None) -> str | None:
    """Read string environment variable, treating empty / whitespace-only values as unset.

    Docker `env_file` passes `KEY=` lines through as empty strings; those must fall back to
    the documented default rather than silently becoming "".
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def read_secret_file(path: str | None) -> str | None:
    """Read a secret (e.g. password) from a file such as a Docker secret, stripping newline."""
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        value = fh.read().rstrip("\r\n")
    return value or None


def env_bool(name: str, default: bool = False) -> bool:
    """Read boolean environment variable ('1', 'true', 'yes', 'on')."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "y", "on"}:
        return True
    if val in {"0", "false", "no", "n", "off"}:
        return False
    return default


def env_int(name: str, default: int | None = None) -> int | None:
    """Read optional integer environment variable."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"环境变量 {name}={raw!r} 不是合法整数") from exc


def env_float(name: str, default: float | None = None) -> float | None:
    """Read optional float environment variable."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"环境变量 {name}={raw!r} 不是合法数字") from exc


def env_list(name: str) -> list[str]:
    """Read comma-separated list from environment variable."""
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]
