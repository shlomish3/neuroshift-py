"""
core/roster.py
==============

• Builds the blank roster (one row per calendar-day × shift-type).
• Injects “Needed”  (hard minimum) and “SoftCap” (upper bound).
• Clinic dates come from יומן מרפאות; כמות נדרשת controls staffing count
  only on dates where that clinic appears in the clinic calendar.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List, Set, Dict

import pandas as pd

from core.data import backend_tables
from core.holiday_utils import effective_weekday_letter, holiday_eve_names_from_tables, holiday_names_from_tables


# ──────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────
def _clean(text: str) -> str:
    """
    Strip stray LTR/RTL marks (U+200E / U+200F) and surrounding spaces.
    """
    return str(text).replace("\u200f", "").replace("\u200e", "").strip()


# ──────────────────────────────────────────────────────────────
#  Lazy-loaded backend sheets
# ──────────────────────────────────────────────────────────────
_tables: dict | None = None
NEEDED_DF: pd.DataFrame | None = None
SHIFT_TYPES: List[str] = []
POST_DATES: Set[date] = set()
HOLIDAY_NAMES: Dict[date, str] = {}
HOLIDAY_EVE_NAMES: Dict[date, str] = {}

HEB_WEEKDAYS: List[str] = ["ב", "ג", "ד", "ה", "ו", "ש", "א"]   # Mon … Sun


def _is_calendar_clinic_shift(shift: str) -> bool:
    return (
        shift in {"EMG", "נוירולוגיה כללית"}
        or shift.startswith("מרפאת ")
    )


def _ensure_roster_tables_loaded() -> None:
    """
    Load Google-Sheets-backed roster tables lazily.

    This avoids contacting Google during module import.
    """
    global _tables, NEEDED_DF, SHIFT_TYPES, POST_DATES, HOLIDAY_NAMES, HOLIDAY_EVE_NAMES

    if _tables is not None:
        return

    _tables = backend_tables()

    NEEDED_DF = _tables["required"].set_index("סוג משמרת")
    SHIFT_TYPES = [_clean(s) for s in NEEDED_DF.index]

    POST_DATES = set(
        pd.to_datetime(
            _tables["post_admission"]["תאריך"],
            format="mixed",
            errors="coerce",
        ).dropna().dt.date
    )
    HOLIDAY_NAMES = holiday_names_from_tables(_tables)
    HOLIDAY_EVE_NAMES = holiday_eve_names_from_tables(_tables)


# ──────────────────────────────────────────────────────────────
#  Builder
# ──────────────────────────────────────────────────────────────
def template_for_month(
    month: str,
    clinic_needs: Dict[tuple[date, str], int] | None = None,
) -> pd.DataFrame:
    """
    Build an empty roster grid for *month* (YYYY-MM).

    Columns
    -------
    Date       : ISO yyyy-mm-dd
    DayHeb     : Hebrew weekday letter (ב, ג, …)
    Shift      : cleaned shift type
    Needed     : hard minimum
    SoftCap    : upper bound
    Assigned   : names will be filled later

    If *clinic_needs* is provided, it overrides the base 'Needed'
    for clinic shifts on specific dates, and SoftCap is bumped to
    be at least that value.
    """
    _ensure_roster_tables_loaded()

    assert NEEDED_DF is not None  # for type checkers

    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    rows = []
    for d in (first + timedelta(days=i) for i in range((last - first).days + 1)):
        display_wd = HEB_WEEKDAYS[d.isoweekday() - 1]  # actual Hebrew weekday letter
        wd = effective_weekday_letter(d, HOLIDAY_NAMES, HOLIDAY_EVE_NAMES)

        for shift in SHIFT_TYPES:
            # ── 1. base 'Needed' from the required-sheet ───────────────
            base_needed = int(NEEDED_DF.loc[shift, wd])

            # ── 2. base SoftCap ────────────────────────────────────────
            if shift == "מחלקה":
                # Sun–Thu → allow one extra; Fri/Sat → no extras
                if wd in {"ו", "ש"}:
                    base_soft_cap = base_needed
                else:
                    base_soft_cap = base_needed + 1
            else:
                base_soft_cap = base_needed

            needed = base_needed
            soft_cap = base_soft_cap

            # ── 3. calendar clinic handling ───────────────────────────
            if clinic_needs is not None and _is_calendar_clinic_shift(shift):
                calendar_needed = int(clinic_needs.get((d, shift), 0))
                if calendar_needed > 0:
                    # יומן מרפאות decides the date. כמות נדרשת decides how
                    # many people are needed on that date. Fall back to the
                    # calendar count only if כמות נדרשת has no value.
                    needed = base_needed if base_needed > 0 else calendar_needed
                else:
                    needed = 0
                soft_cap = needed

            rows.append(
                {
                    "Date": d.isoformat(),
                    "DayHeb": display_wd,
                    "Shift": shift,
                    "Needed": needed,
                    "SoftCap": soft_cap,
                    "Assigned": "",
                }
            )

    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────
#  CLI smoke-test
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    df = template_for_month("2026-06")
    print(df.head(8)[["Date", "Shift", "Needed", "SoftCap"]])
