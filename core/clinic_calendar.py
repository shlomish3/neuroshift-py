# core/clinic_calendar.py

from __future__ import annotations
from datetime import date as _date
from typing import Dict, Tuple, Set

import pandas as pd

from core.data import _df_from, backend_tables
from core import config

# column names in מפת מרפאות
COL_PATTERN = "Pattern (in תא.יומן)"
COL_SHIFT   = "ShiftType"
COL_OWNER   = "Owner"

# column names in the raw calendar
CAL_COL_DATE   = "תאריך זימון-תכנון"
CAL_COL_CELL   = "תא.יומן"


def _clean(s: str) -> str:
    return str(s).replace("\u200f", "").replace("\u200e", "").strip()


def clinic_map_df() -> pd.DataFrame:
    """Return the מפת מרפאות table."""
    df = backend_tables()["clinic_map"].copy()
    df[COL_PATTERN] = df[COL_PATTERN].astype(str).map(_clean)
    df[COL_SHIFT]   = df[COL_SHIFT].astype(str).map(_clean)
    df[COL_OWNER]   = df[COL_OWNER].fillna("").astype(str).map(_clean)
    return df


def raw_calendar_for_month(month: str) -> pd.DataFrame:
    """
    Load the raw calendar for *month* (YYYY-MM) from the dedicated workbook.
    Assumes the tab name == month (e.g. '2025-12').
    """
    print(f"[clinic-calendar] loading workbook {config.CLINIC_CAL_SHEET_ID}, tab '{month}'")
    df = _df_from(config.CLINIC_CAL_SHEET_ID, month)
    print(f"[clinic-calendar] loaded tab '{month}', rows={len(df)}")
    return df


def build_clinic_needs(month: str) -> Dict[Tuple[_date, str], int]:
    """
    Return {(date, shift_type): needed_count} for all clinics in *month* (YYYY-MM),
    based on the hospital calendar + מפת מרפאות.

    Logic
    -----
    • We read the raw monthly calendar from the dedicated clinics workbook
      (tab name == *month*, e.g. '2025-12').
    • We map each תא.יומן text via מפת מרפאות (Pattern → ShiftType, Owner).
    • For each (date, ShiftType) we collect the set of clinic "owners".
      - If there are named owners (e.g. קינן, טולצ'ינסקי) → needed_count = #owners.
      - If there is no Owner for that pattern (e.g. EEG, פוסט אשפוז) → needed_count = 1.
    • Clinics whose תא.יומן pattern is NOT in מפת מרפאות are ignored as irrelevant
      (e.g. פסיכולוגים, אנשי שיקום וכו').
    """
    # Raw hospital calendar for that month
    raw = raw_calendar_for_month(month).copy()

    # מפת מרפאות – pattern → (ShiftType, Owner)
    cmap = clinic_map_df()
    pattern_lut: Dict[str, Tuple[str, str]] = {
        row[COL_PATTERN]: (row[COL_SHIFT], row[COL_OWNER])
        for _, row in cmap.iterrows()
        if row[COL_SHIFT]  # ignore rows with empty ShiftType (= not mapped to a shift)
    }

    # Normalise calendar fields
    raw[CAL_COL_DATE] = pd.to_datetime(
        raw[CAL_COL_DATE],
        format="mixed",
        dayfirst=False,
        errors="coerce",
    ).dt.date
    raw[CAL_COL_CELL] = raw[CAL_COL_CELL].astype(str).map(_clean)

    # Accumulate owners per (date, ShiftType)
    owners_by_key: Dict[Tuple[_date, str], Set[str]] = {}

    for _, r in raw.iterrows():
        d = r[CAL_COL_DATE]
        if not isinstance(d, _date):
            # bad / empty date → skip row
            continue

        cell = r[CAL_COL_CELL]
        if cell not in pattern_lut:
            # clinic not mapped in מפת מרפאות → irrelevant (e.g. פסיכולוגים, ססיות EMG)
            continue

        shift_type, owner = pattern_lut[cell]
        key = (d, shift_type)

        owners = owners_by_key.setdefault(key, set())
        if owner:
            # named clinic → track real owner(s)
            owners.add(owner)
        else:
            # unnamed clinic (EEG, פוסט אשפוז וכו') – ensure at least one slot
            # We use a sentinel so that multiple rows without Owner still count as 1.
            if not owners:
                owners.add("__ANON__")

    # Convert owner-sets → needed counts
    needs: Dict[Tuple[_date, str], int] = {}
    for key, owners in owners_by_key.items():
        real_owners = {o for o in owners if o != "__ANON__"}
        if real_owners:
            # e.g. EMG on 2025-12-02: {קינן, טולצ'ינסקי} → need 2 EMG slots
            needs[key] = len(real_owners)
        else:
            # fully anonymous clinics (e.g. EEG with no Owner in the map)
            needs[key] = 1

        # debug_snippets.py
    from datetime import date
    from core.clinic_calendar import build_clinic_needs
    from core.roster import template_for_month

    def debug_emg_day(month: str = "2025-12", target_iso: str = "2025-12-02"):
        d = date.fromisoformat(target_iso)

        print("=== clinic_needs ===")
        needs = build_clinic_needs(month)
        print("clinic_needs.get((date, 'EMG')):", needs.get((d, "EMG")))

        print("\n=== roster row ===")
        roster = template_for_month(month, clinic_needs=needs)
        emg_row = roster[(roster["Date"] == target_iso) & (roster["Shift"] == "EMG")]
        print(emg_row[["Date", "Shift", "Needed", "SoftCap"]])
    
    return needs


