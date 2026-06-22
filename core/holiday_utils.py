from __future__ import annotations

from datetime import date, timedelta
from typing import Dict

import pandas as pd


def _clean(text: str) -> str:
    return str(text).replace("\u200f", "").replace("\u200e", "").strip()


def _holiday_source(tables: dict) -> tuple[pd.Series, pd.DataFrame, str | None]:
    hol_df = tables.get("holidays", pd.DataFrame()).copy()
    if hol_df.empty or "תאריך" not in hol_df.columns:
        return pd.Series(dtype=object), hol_df, None

    name_col = next(
        (col for col in ("שם", "חג", "שם החג", "תיאור", "אירוע") if col in hol_df.columns),
        None,
    )
    dates = pd.to_datetime(
        hol_df["תאריך"],
        format="mixed",
        dayfirst=True,
        errors="coerce",
    ).dt.date
    return dates, hol_df, name_col


def _holiday_name(hol_df: pd.DataFrame, idx: int, name_col: str | None, fallback: str) -> str:
    if name_col:
        name = _clean(hol_df.at[idx, name_col])
        if name:
            return name
    return fallback


def _holiday_type(hol_df: pd.DataFrame, idx: int) -> str:
    if "סוג" not in hol_df.columns:
        return ""
    return _clean(hol_df.at[idx, "סוג"])


def _is_rest_type(kind: str) -> bool:
    return "חופש" in kind


def _is_erev_rest_type(kind: str) -> bool:
    return _is_rest_type(kind) and "ערב" in kind


def holiday_names_from_tables(tables: dict) -> Dict[date, str]:
    """
    Return full rest-day holidays from the חגים table.

    Rows marked ערב ... חופש are intentionally excluded here; they are
    Friday-equivalent eves, not full Saturday-equivalent holidays.
    """
    dates, hol_df, name_col = _holiday_source(tables)
    if hol_df.empty or "סוג" not in hol_df.columns:
        return {}

    out: Dict[date, str] = {}
    for idx, d in dates.dropna().items():
        kind = _holiday_type(hol_df, idx)
        if not _is_rest_type(kind) or _is_erev_rest_type(kind):
            continue
        out[d] = _holiday_name(hol_df, idx, name_col, "חג")

    return out


def holiday_eve_names_from_tables(tables: dict) -> Dict[date, str]:
    """
    Return explicit ערב חג (חופש) dates from the חגים table.
    These are Friday-equivalent days for scheduling/export coloring.
    """
    dates, hol_df, name_col = _holiday_source(tables)
    if hol_df.empty or "סוג" not in hol_df.columns:
        return {}

    out: Dict[date, str] = {}
    for idx, d in dates.dropna().items():
        kind = _holiday_type(hol_df, idx)
        if not _is_erev_rest_type(kind):
            continue
        out[d] = _holiday_name(hol_df, idx, name_col, "ערב חג")
    return out


def holiday_display_names_from_tables(tables: dict) -> Dict[date, str]:
    """
    Return all named holidays/informational dates from the חגים table.

    Unlike holiday_names_from_tables(), this includes non-חופש rows. Use it
    only for display labels; scheduling/rest logic must keep using
    holiday_names_from_tables().
    """
    dates, hol_df, name_col = _holiday_source(tables)
    if hol_df.empty:
        return {}

    if not name_col:
        return {}

    out: Dict[date, str] = {}
    for idx, d in dates.dropna().items():
        name = _clean(hol_df.at[idx, name_col])
        if name:
            out[d] = name
    return out


def effective_weekday_letter(
    d: date,
    holiday_names: Dict[date, str],
    holiday_eve_names: Dict[date, str] | None = None,
) -> str:
    """
    Required-count weekday for scheduling:
    - actual holiday/rest day -> שבת
    - day before holiday -> שישי
    - otherwise the real weekday
    """
    holiday_eve_names = holiday_eve_names or {}
    if d in holiday_names:
        return "ש"
    if d in holiday_eve_names or d + timedelta(days=1) in holiday_names:
        return "ו"
    return ["ב", "ג", "ד", "ה", "ו", "ש", "א"][d.isoweekday() - 1]
