# core/clinic_calendar.py

from __future__ import annotations
from datetime import date as _date
from typing import Dict, Tuple, Set, NamedTuple
import re

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
_TRUTHY = {"1", "true", "TRUE", "yes", "YES", "y", "Y", "✔", "✓", "✅"}
_DATE_COL_ALIASES = (
    CAL_COL_DATE,
    "תאריך זימון",
    "תאריך",
    "Date",
)


def _clean(s: str) -> str:
    return str(s).replace("\u200f", "").replace("\u200e", "").strip()


def _pick_col(df: pd.DataFrame, wanted: str, aliases: tuple[str, ...]) -> str:
    by_clean = {_clean(col): col for col in df.columns}
    for alias in aliases:
        key = _clean(alias)
        if key in by_clean:
            return by_clean[key]

    wanted_key = _clean(wanted)
    for clean_col, original in by_clean.items():
        if wanted_key and wanted_key in clean_col:
            return original

    if wanted == CAL_COL_DATE:
        for clean_col, original in by_clean.items():
            if "תאריך" in clean_col:
                return original
        if len(df.columns) >= 1:
            return df.columns[0]

    if wanted == CAL_COL_CELL and len(df.columns) >= 3:
        return df.columns[2]

    raise KeyError(wanted)


def _match_text(s: str) -> str:
    text = _clean(s).lower()
    text = text.replace('ד"ר', "דר").replace("ד״ר", "דר")
    text = re.sub(r"\s*[-–—]\s*", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _split_owners(owner_text: str) -> list[str]:
    text = _clean(owner_text)
    if not text:
        return []
    owners: list[str] = []
    for part in re.split(r"[,;/\n]+", text):
        owner = _clean(part)
        if owner and owner not in owners:
            owners.append(owner)
    return owners


class _ClinicMapEntry(NamedTuple):
    pattern: str
    pattern_key: str
    shift_type: str
    owners: tuple[str, ...]


def _clinic_map_entries(cmap: pd.DataFrame) -> list[_ClinicMapEntry]:
    entries: list[_ClinicMapEntry] = []
    for _, row in cmap.iterrows():
        shift_type = row[COL_SHIFT]
        pattern = row[COL_PATTERN]
        if not shift_type or not pattern:
            continue
        entries.append(
            _ClinicMapEntry(
                pattern=pattern,
                pattern_key=_match_text(pattern),
                shift_type=shift_type,
                owners=tuple(_split_owners(row[COL_OWNER])),
            )
        )
    return entries


def _matching_entries(cell_text: str, entries: list[_ClinicMapEntry]) -> list[_ClinicMapEntry]:
    cell_key = _match_text(cell_text)
    if not cell_key:
        return []

    exact = [entry for entry in entries if cell_key == entry.pattern_key]
    if exact:
        return exact

    contained = [
        entry
        for entry in entries
        if entry.pattern_key and entry.pattern_key in cell_key
    ]
    contained.sort(key=lambda entry: len(entry.pattern_key), reverse=True)

    filtered: list[_ClinicMapEntry] = []
    for entry in contained:
        # Prefer the more specific row when both "כאב ראש" and
        # "כאב ראש-אסף ברג" match the same calendar text.
        if any(
            entry.shift_type == kept.shift_type
            and entry.pattern_key != kept.pattern_key
            and entry.pattern_key in kept.pattern_key
            for kept in filtered
        ):
            continue
        filtered.append(entry)
    return filtered


def _is_anon_owner(owner: str) -> bool:
    return owner.startswith("__ANON__")


def clinic_map_df() -> pd.DataFrame:
    """Return the מפת מרפאות table."""
    df = backend_tables()["clinic_map"].copy()
    df[COL_PATTERN] = df[COL_PATTERN].astype(str).map(_clean)
    df[COL_SHIFT]   = df[COL_SHIFT].astype(str).map(_clean)
    df[COL_OWNER]   = df[COL_OWNER].fillna("").astype(str).map(_clean)
    return df


def _worker_capabilities() -> Dict[Tuple[str, str], bool]:
    """Return {(name, ShiftType): can_do} from the עובדים sheet."""
    workers = backend_tables()["workers"].copy()
    if workers.empty:
        return {}
    name_col = "שם" if "שם" in workers.columns else workers.columns[0]
    out: Dict[Tuple[str, str], bool] = {}
    for _, row in workers.iterrows():
        name = _clean(row.get(name_col, ""))
        if not name:
            continue
        for shift in workers.columns:
            if shift == name_col:
                continue
            out[(name, _clean(shift))] = _clean(row.get(shift, "")) in _TRUTHY
    return out


def _capable_real_owners(
    key: Tuple[_date, str],
    owners: Set[str],
    capabilities: Dict[Tuple[str, str], bool],
) -> Set[str]:
    shift = key[1]
    return {
        owner
        for owner in owners
        if not _is_anon_owner(owner) and capabilities.get((owner, shift), False)
    }


def raw_calendar_for_month(month: str) -> pd.DataFrame:
    """
    Load the raw calendar for *month* (YYYY-MM) from the dedicated workbook.
    Assumes the tab name == month (e.g. '2025-12').
    """
    print(f"[clinic-calendar] loading workbook {config.CLINIC_CAL_SHEET_ID}, tab '{month}'")
    df = _df_from(config.CLINIC_CAL_SHEET_ID, month)
    clean_cols = {_clean(col) for col in df.columns}
    if CAL_COL_CELL not in clean_cols and not any(_clean(alias) in clean_cols for alias in _DATE_COL_ALIASES):
        df = pd.concat(
            [pd.DataFrame([list(df.columns)], columns=df.columns), df],
            ignore_index=True,
        )
    print(f"[clinic-calendar] loaded tab '{month}', rows={len(df)}")
    return df


def _clinic_owners_by_key(month: str) -> Dict[Tuple[_date, str], Set[str]]:
    # Raw hospital calendar for that month
    raw = raw_calendar_for_month(month).copy()

    # מפת מרפאות – each pattern may map into an existing ShiftType.
    cmap = clinic_map_df()
    map_entries = _clinic_map_entries(cmap)

    # Normalise calendar fields
    date_col = _pick_col(raw, CAL_COL_DATE, _DATE_COL_ALIASES)
    cell_col = _pick_col(raw, CAL_COL_CELL, (CAL_COL_CELL,))
    raw[CAL_COL_DATE] = pd.to_datetime(
        raw[date_col],
        format="mixed",
        dayfirst=True,
        errors="coerce",
    ).dt.date
    raw[CAL_COL_CELL] = raw[cell_col].astype(str).map(_clean)

    # Accumulate owners per (date, ShiftType)
    owners_by_key: Dict[Tuple[_date, str], Set[str]] = {}

    for _, r in raw.iterrows():
        d = r[CAL_COL_DATE]
        if not isinstance(d, _date):
            # bad / empty date → skip row
            continue

        cell = r[CAL_COL_CELL]
        matches = _matching_entries(cell, map_entries)
        if not matches:
            # clinic not mapped in מפת מרפאות → irrelevant (e.g. פסיכולוגים, ססיות EMG)
            continue

        for match_index, entry in enumerate(matches):
            key = (d, entry.shift_type)
            owners = owners_by_key.setdefault(key, set())
            if entry.owners:
                owners.update(entry.owners)
            else:
                owners.add(f"__ANON__:{r.name}:{match_index}:{entry.pattern}")

    return owners_by_key


def build_clinic_owners(month: str) -> Dict[Tuple[_date, str], Set[str]]:
    """
    Return real named clinic owners for each scheduled clinic row.
    Anonymous clinics such as EEG are omitted from the owner set.
    Owners marked incapable for that ShiftType in עובדים are ignored.
    """
    owners_by_key = _clinic_owners_by_key(month)
    capabilities = _worker_capabilities()
    return {
        key: capable
        for key, owners in owners_by_key.items()
        if (capable := _capable_real_owners(key, owners, capabilities))
    }


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
      - Anonymous rows for the same ShiftType/day collapse to one staffing slot
        (e.g. multiple EEG appointment rows still need one EEG doctor).
    • Clinics whose תא.יומן pattern is NOT in מפת מרפאות are ignored as irrelevant
      (e.g. פסיכולוגים, אנשי שיקום וכו').
    """
    owners_by_key = _clinic_owners_by_key(month)
    # Convert owner-sets → needed counts
    needs: Dict[Tuple[_date, str], int] = {}
    for key, owners in owners_by_key.items():
        real_owner_count = sum(1 for o in owners if not _is_anon_owner(o))
        anonymous_count = sum(1 for o in owners if _is_anon_owner(o))
        anonymous_slots = 1 if anonymous_count else 0
        if real_owner_count:
            # A clinic-calendar row is a real staffing requirement even when
            # the named owner is unavailable/incapable; assignment should then
            # show an underfilled marker instead of hiding the row.
            needs[key] = real_owner_count + anonymous_slots
        else:
            # fully anonymous clinics (e.g. EEG with no Owner in the map)
            needs[key] = anonymous_slots

    return needs


