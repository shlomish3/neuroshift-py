"""
core.elig_utils
---------------
Pure helpers / cached look-ups for *core.eligibility*.
Nothing here logs, mutates the roster, or touches business rules.
"""

from __future__ import annotations
from functools import lru_cache
from datetime import datetime
from typing import Dict, Set, Tuple, List, Final

import pandas as pd
from core.data import backend_tables
import core.roster as roster
from core import constants
from core.constants import USE_SIMPLE_FORM
from core.availability_simple_parser import unavail_from_simple


# ──  constants ────────────────────────────────────────────────
BLOCKS_ALL:  Set[str] = {"לא זמין"}
BLOCKS_DUTY: Set[str] = {"לא זמין לתורנות"}
DUTY_SHIFTS: Set[str] = {"ת.מיון", "ת.מיון 2", "כונן מיון"}

CLINIC_SHIFTS: Set[str] = {
    "EMG", "EEG",
    "מרפאת עצב שריר", "מרפאת תנועה", "מרפאת אפילפסיה גנדלמן", "מרפאת אפילפסיה הרש",
    "מרפאת CVA", "מרפאת קרוטיס", "מרפאת זיכרון",
    "מרפאת בוטוקס", "מרפאת נוירואימונולוגיה", "מרפאת כאבי ראש",
    "מרפאת פוסט אשפוז", "מרפאת שבץ מוחי", "מרפאת נוירואונקולוגיה", "מרפאת שבץ מוחי", "נוירולוגיה כללית",
}

DAY_SHIFTS: Set[str] = CLINIC_SHIFTS | {
    "אטנדינג", "מחלקה", "מיון",
    "ייעוצים מובילים", "מחקר",
}

ISO2HEB = ["ב", "ג", "ד", "ה", "ו", "ש", "א"]  # 1-Mon → ב …

REQ_NAME:  Final = "שם"
REQ_DATE:  Final = "תאריך"
REQ_BLOCK: Final = "סוג חסימה"

_NAME_ALIAS  = {REQ_NAME, "שם עובד", "Name"}
_DATE_ALIAS  = {REQ_DATE, "Date", "תאריך חסימה"}
_BLOCK_ALIAS = {REQ_BLOCK, "סוג חסימה/זמינות", "חסימה", "זמינות"}

_TRUTHY = {"1", "true", "TRUE", "yes", "YES", "y", "Y", "✔", "✓", "✅"}


# ──  cached raw sheets ────────────────────────────────────────
@lru_cache(maxsize=1)
def _tables():
    return backend_tables()


@lru_cache(maxsize=1)
def workers_df() -> pd.DataFrame:
    df = _tables()["workers"].copy()
    if REQ_NAME not in df.columns:
        df = df.rename(columns={df.columns[0]: REQ_NAME})
    for col in df.columns.difference([REQ_NAME]):
        df[col] = (
            df[col].fillna("")
            .astype(str).str.strip()
            .apply(lambda v: v in _TRUTHY)
        )
    return df


@lru_cache(maxsize=1)
def can_do() -> Dict[Tuple[str, str], bool]:
    roster._ensure_roster_tables_loaded()
    df = workers_df()
    return {
        (row[REQ_NAME], s): bool(row.get(s, True))
        for _, row in df.iterrows()
        for s in roster.SHIFT_TYPES
    }

@lru_cache(maxsize=1)
def fixed_clinic_lut() -> Dict[Tuple[str, str], Set[str]]:
    fc = _tables()["fixed_clinics"].copy()
    for col in ("שם", "יום", "מרפאה"):
        if col not in fc.columns:
            raise ValueError("מרפאות קבועות missing column %s" % col)
    fc["יום"]   = fc["יום"].astype(str).str.strip()
    fc["מרפאה"] = fc["מרפאה"].astype(str).str.strip()
    lut: Dict[Tuple[str, str], Set[str]] = {}
    for _, r in fc.iterrows():
        lut.setdefault((r["שם"], r["יום"]), set()).add(r["מרפאה"])
    return lut


# ──  unified availability lookup ──────────────────────────────
@lru_cache(maxsize=1)
def unavail_lookup() -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
    """
    Unified availability lookup.
    If USE_SIMPLE_FORM=1, use the simplified Google Form ("requests" tab);
    otherwise use the legacy parsed_requests tab.
    """
    tables = _tables()

    # New simplified form
    if USE_SIMPLE_FORM:
        target = constants.CURRENT_TARGET_MONTH
        if not target:
            # Fallback to current month if none was set by assign2.auto_assign()
            from datetime import date as _date
            target = _date.today().strftime("%Y-%m")

        df = tables["requests"].copy()
        return unavail_from_simple(df, target_month=target)

    # Legacy parsed_requests path
    raw = tables["parsed_requests"].copy()
    rename = {}
    for c in raw.columns:
        t = c.strip()
        if t in _NAME_ALIAS:
            rename[c] = REQ_NAME
        elif t in _DATE_ALIAS:
            rename[c] = REQ_DATE
        elif t in _BLOCK_ALIAS:
            rename[c] = REQ_BLOCK
        elif t == "מקור":
            rename[c] = "מקור"

    df = raw.rename(columns=rename)
    for col in (REQ_NAME, REQ_DATE, REQ_BLOCK, "מקור"):
        if col not in df.columns:
            raise ValueError("Availability sheet missing %s" % col)

    df[REQ_DATE]  = pd.to_datetime(df[REQ_DATE]).dt.date.astype(str)
    df[REQ_BLOCK] = df[REQ_BLOCK].astype(str).str.strip()
    df["מקור"]   = df["מקור"].fillna("").astype(str).str.strip()

    grouped = (
        df.groupby([REQ_NAME, REQ_DATE], group_keys=False)[[REQ_BLOCK, "מקור"]]
        .apply(lambda g: list(g.itertuples(index=False, name=None)))
        .to_dict()
    )
    return grouped


# ──  tiny helpers ─────────────────────────────────────────────
def weekday_letter(date_iso: str) -> str:
    """2025-07-01 → 'ג' (Tue)"""
    return ISO2HEB[datetime.fromisoformat(date_iso).isoweekday() - 1]


def is_senior(name: str, lut: Dict[Tuple[str, str], bool]) -> bool:
    """Return True if the worker is a senior (כונן מיון or בכיר מיון)."""
    return lut.get((name, "כונן מיון"), False) or lut.get((name, "בכיר מיון"), False)
