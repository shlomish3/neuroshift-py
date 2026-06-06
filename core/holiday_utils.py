from __future__ import annotations

from datetime import date, timedelta
from typing import Dict

import pandas as pd


def _clean(text: str) -> str:
    return str(text).replace("\u200f", "").replace("\u200e", "").strip()


def holiday_names_from_tables(tables: dict) -> Dict[date, str]:
    """
    Return actual rest-day holidays from the חגים table.
    Only rows whose סוג is חופש are treated as holidays for assignment/export.
    """
    hol_df = tables.get("holidays", pd.DataFrame()).copy()
    if hol_df.empty or "תאריך" not in hol_df.columns or "סוג" not in hol_df.columns:
        return {}

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

    out: Dict[date, str] = {}
    for idx, d in dates.dropna().items():
        if _clean(hol_df.at[idx, "סוג"]) != "חופש":
            continue

        if name_col:
            name = _clean(hol_df.at[idx, name_col])
        else:
            name = ""

        out[d] = name or "חג"

    return out


def effective_weekday_letter(d: date, holiday_names: Dict[date, str]) -> str:
    """
    Required-count weekday for scheduling:
    - actual holiday/rest day -> שבת
    - day before holiday -> שישי
    - otherwise the real weekday
    """
    if d in holiday_names:
        return "ש"
    if d + timedelta(days=1) in holiday_names:
        return "ו"
    return ["ב", "ג", "ד", "ה", "ו", "ש", "א"][d.isoweekday() - 1]
