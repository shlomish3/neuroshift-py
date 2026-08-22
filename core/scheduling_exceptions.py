"""Narrow, date-bounded scheduling exceptions shared across scheduler stages."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence


RESIDENT_NIGHT_SHIFTS = frozenset({"ת.מיון", "ת.מיון 2"})

# One-time Yom Kippur exception.  A date pair becomes active only for a
# resident who is explicitly present in שיבוצים קבועים on both nights.
FIXED_RESIDENT_CONSECUTIVE_NIGHT_DATE_PAIRS = frozenset({
    (date(2026, 9, 20), date(2026, 9, 21)),
})

ResidentConsecutiveNightException = tuple[str, date, date]


def derive_fixed_resident_consecutive_night_exceptions(
    fixed_assignments: Mapping[tuple[date, str], Sequence[str]],
) -> set[ResidentConsecutiveNightException]:
    """Activate configured pairs only for a resident fixed on both nights."""

    resident_names_by_date: dict[date, set[str]] = {}
    for (shift_date, shift), names in fixed_assignments.items():
        if shift not in RESIDENT_NIGHT_SHIFTS:
            continue
        resident_names_by_date.setdefault(shift_date, set()).update(
            str(name).strip() for name in names if str(name).strip()
        )

    return {
        (name, first, second)
        for first, second in FIXED_RESIDENT_CONSECUTIVE_NIGHT_DATE_PAIRS
        for name in (
            resident_names_by_date.get(first, set())
            & resident_names_by_date.get(second, set())
        )
    }


def resident_consecutive_night_allowed(
    exceptions: Iterable[ResidentConsecutiveNightException] | None,
    name: str,
    first: date,
    second: date,
) -> bool:
    """Return whether this exact resident/date pair has the one-time allowance."""

    if second - first != timedelta(days=1):
        return False
    return (name, first, second) in (exceptions or ())


def serialize_resident_consecutive_night_exceptions(
    exceptions: Iterable[ResidentConsecutiveNightException],
) -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "first_date": first.isoformat(),
            "second_date": second.isoformat(),
        }
        for name, first, second in sorted(
            exceptions,
            key=lambda item: (item[1], item[2], item[0]),
        )
    ]


def deserialize_resident_consecutive_night_exceptions(
    raw: object,
) -> set[ResidentConsecutiveNightException]:
    """Read the DataFrame-attribute representation used between pipeline stages."""

    if isinstance(raw, Mapping):
        raw = raw.values()
    if not isinstance(raw, (list, tuple, set)):
        return set()

    out: set[ResidentConsecutiveNightException] = set()
    for item in raw:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            first_raw = item.get("first_date")
            second_raw = item.get("second_date")
        elif isinstance(item, (list, tuple)) and len(item) == 3:
            name, first_raw, second_raw = item
            name = str(name).strip()
        else:
            continue

        try:
            first = first_raw if isinstance(first_raw, date) else date.fromisoformat(str(first_raw))
            second = second_raw if isinstance(second_raw, date) else date.fromisoformat(str(second_raw))
        except (TypeError, ValueError):
            continue
        if name and (first, second) in FIXED_RESIDENT_CONSECUTIVE_NIGHT_DATE_PAIRS:
            out.add((name, first, second))
    return out
