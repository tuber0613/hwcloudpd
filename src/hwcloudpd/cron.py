"""Standard 5-field cron expression parser and next-run calculator (pure stdlib)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_DOW_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


def _parse_atom(token: str, min_val: int, max_val: int, names: dict[str, int] | None, is_dow: bool) -> int:
    low = token.strip().lower()
    if names and low in names:
        val = names[low]
    else:
        try:
            val = int(low)
        except ValueError as exc:
            raise ValueError(f"Invalid cron value '{token}'") from exc
    if is_dow and val == 7:
        val = 0
    if val < min_val or val > max_val:
        raise ValueError(f"Cron value {val} out of range [{min_val}, {max_val}]")
    return val


def _parse_field(
    field_str: str,
    min_val: int,
    max_val: int,
    names: dict[str, int] | None = None,
    is_dow: bool = False,
) -> tuple[frozenset[int], bool]:
    """Parse one cron field into (allowed_values, is_wildcard)."""
    raw = field_str.strip()
    if not raw:
        raise ValueError("Empty cron field")
    if raw == "*":
        return frozenset(range(min_val, max_val + 1)), True

    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Empty element in cron field '{field_str}'")

        if "/" in part:
            range_part, step_part = part.split("/", 1)
            try:
                step = int(step_part)
            except ValueError as exc:
                raise ValueError(f"Invalid cron step '{step_part}'") from exc
            if step <= 0:
                raise ValueError(f"Cron step must be positive, got {step}")
        else:
            range_part = part
            step = 1

        if range_part == "*":
            start, end = min_val, max_val
        elif "-" in range_part:
            left, right = range_part.split("-", 1)
            range_max = 7 if is_dow else max_val
            start = _parse_atom(left, min_val, range_max, names, is_dow=False)
            end = _parse_atom(right, min_val, range_max, names, is_dow=False)
            if is_dow and end == 7 and start <= 7:
                # Allow ranges like 1-7 or 5-7 where 7 maps to Sunday (0).
                for v in range(start, end + 1, step):
                    values.add(0 if v == 7 else v)
                continue
            if start > end:
                raise ValueError(f"Invalid cron range '{range_part}': start > end")
        else:
            single = _parse_atom(range_part, min_val, max_val, names, is_dow=is_dow)
            if "/" in part:
                start, end = single, max_val
            else:
                values.add(single)
                continue

        for v in range(start, end + 1, step):
            values.add(0 if (is_dow and v == 7) else v)

    if not values:
        raise ValueError(f"Cron field '{field_str}' matched no values")
    return frozenset(values), False


@dataclass(frozen=True)
class CronSchedule:
    """Compiled 5-field cron schedule supporting timezone-aware next-run lookup."""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]  # 0 = Sunday .. 6 = Saturday
    dom_wildcard: bool
    dow_wildcard: bool

    @classmethod
    def parse(cls, expr: str) -> CronSchedule:
        """Compile a standard 5-field cron expression or @macro."""
        cleaned = " ".join(expr.strip().split())
        if not cleaned:
            raise ValueError("Cron expression cannot be empty")
        expanded = _MACROS.get(cleaned.lower(), cleaned)
        parts = expanded.split(" ")
        if len(parts) != 5:
            raise ValueError(
                f"Expected 5-field cron expression (got {len(parts)} fields): '{expr}'"
            )
        minutes, _ = _parse_field(parts[0], 0, 59)
        hours, _ = _parse_field(parts[1], 0, 23)
        doms, dom_wild = _parse_field(parts[2], 1, 31)
        months, _ = _parse_field(parts[3], 1, 12, names=_MONTH_NAMES)
        dows, dow_wild = _parse_field(parts[4], 0, 6, names=_DOW_NAMES, is_dow=True)
        return cls(
            expression=cleaned,
            minutes=minutes,
            hours=hours,
            days_of_month=doms,
            months=months,
            days_of_week=dows,
            dom_wildcard=dom_wild,
            dow_wildcard=dow_wild,
        )

    def _day_matches(self, dt: datetime) -> bool:
        # Python weekday(): Monday=0 .. Sunday=6 -> convert to cron Sunday=0 .. Saturday=6
        cron_dow = (dt.weekday() + 1) % 7
        dom_match = dt.day in self.days_of_month
        dow_match = cron_dow in self.days_of_week
        if self.dom_wildcard and self.dow_wildcard:
            return True
        if self.dom_wildcard:
            return dow_match
        if self.dow_wildcard:
            return dom_match
        # Standard Vixie cron: if both DOM and DOW are restricted, either matches.
        return dom_match or dow_match

    def matches(self, dt: datetime) -> bool:
        """Return True if the given datetime (truncated to minute) matches this schedule."""
        return (
            dt.month in self.months
            and self._day_matches(dt)
            and dt.hour in self.hours
            and dt.minute in self.minutes
        )

    @staticmethod
    def _is_real_local_time(dt: datetime, tz) -> bool:
        """Return False if `dt` falls in a DST spring-forward gap (nonexistent wall time)."""
        rt = dt.astimezone(timezone.utc).astimezone(tz)
        return (rt.year, rt.month, rt.day, rt.hour, rt.minute) == (
            dt.year,
            dt.month,
            dt.day,
            dt.hour,
            dt.minute,
        )

    def next_after(self, after: datetime, tz_name: str = "UTC") -> datetime:
        """Return the earliest real timezone-aware datetime strictly after `after` matching this schedule."""
        tz = ZoneInfo(tz_name) if after.tzinfo is None else after.tzinfo
        base = after.astimezone(tz) if after.tzinfo is not None else after.replace(tzinfo=tz)
        # Advance by 1 real minute in UTC so DST fall-back / spring-forward boundaries are exact.
        base_utc = base.astimezone(timezone.utc)
        candidate = (base_utc.replace(second=0, microsecond=0) + timedelta(minutes=1)).astimezone(tz)

        # Bound search to 4 years (+ 2 days for leap years) so invalid dates like Feb 31 fail fast.
        limit_utc = base_utc + timedelta(days=366 * 4 + 2)
        while candidate.astimezone(timezone.utc) < limit_utc:
            if not self._is_real_local_time(candidate, tz):
                # Wall-clock jump landed inside a spring-forward gap: normalize to real local time.
                candidate = candidate.astimezone(timezone.utc).astimezone(tz)
                continue

            if candidate.month not in self.months:
                if candidate.month == 12:
                    candidate = candidate.replace(
                        year=candidate.year + 1, month=1, day=1, hour=0, minute=0, fold=0
                    )
                else:
                    candidate = candidate.replace(
                        month=candidate.month + 1, day=1, hour=0, minute=0, fold=0
                    )
                continue

            if not self._day_matches(candidate):
                candidate = (candidate.replace(hour=0, minute=0, fold=0) + timedelta(days=1)).replace(
                    hour=0, minute=0, fold=0
                )
                continue

            if candidate.hour not in self.hours:
                # Step to the next real hour boundary via UTC so fall-back folds aren't skipped
                # and spring-forward gaps are bridged cleanly.
                mins_to_next_hour = 60 - candidate.minute
                candidate = (
                    candidate.astimezone(timezone.utc) + timedelta(minutes=mins_to_next_hour)
                ).astimezone(tz)
                continue

            if candidate.minute in self.minutes:
                return candidate

            candidate = (candidate.astimezone(timezone.utc) + timedelta(minutes=1)).astimezone(tz)

        raise ValueError(f"Cron expression '{self.expression}' has no matching date within 4 years")
