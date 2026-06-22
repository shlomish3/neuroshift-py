"""
core/availability_simple_parser
-------------------------------

Parses the simplified availability Google Form:
  חותמת זמן | כתובת אימייל | שם | לילות בהם אינכם יכולים לעשות תורנות/כוננות |
  בחירת ימי חופש - כולל ימי שישי | התייחסות חופשית + תאריכים מועדפים

Output matches the canonical parsed_requests style:
  {(name, ISO-date): [(block_type, source), ...]}
"""

from __future__ import annotations
import re
from datetime import date
from typing import Dict, List, Tuple
import pandas as pd
from core.constants import EMAIL_TO_NAME

# detect DD/MM[/YYYY] patterns anywhere
_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\b")

def _clean(s: str) -> str:
    """Strip Unicode direction marks and spaces."""
    return str(s).replace("\u200f", "").replace("\u200e", "").replace("\xa0", " ").strip()

EMAIL_TO_NAME_NORMALIZED = {
    _clean(email).lower(): name
    for email, name in EMAIL_TO_NAME.items()
}

def _parse_blob_dates(blob: str, default_year: int) -> list[date]:
    """Extract all valid dates from a free-text blob."""
    out: list[date] = []
    if not isinstance(blob, str):
        return out
    blob = _clean(blob)
    for m in _DATE_RE.finditer(blob):
        d, mth, yr = int(m.group(1)), int(m.group(2)), int(m.group(3) or default_year)
        try:
            out.append(date(yr, mth, d))
        except ValueError:
            continue
    return out


def _latest_rows(df: pd.DataFrame, *, name_col: str, email_col: str, ts_col: str) -> pd.DataFrame:
    key = email_col if email_col in df.columns else name_col
    if ts_col and ts_col in df.columns:
        df["_ts"] = pd.to_datetime(df[ts_col], errors="coerce")
        df.sort_values("_ts", inplace=True)
        return df.drop_duplicates(subset=[key], keep="last")
    return df.drop_duplicates(subset=[key], keep="last")


def _parse_preferred_blob_dates(blob: str, default_year: int) -> list[tuple[date, int]]:
    """
    Extract preferred night-duty dates.

    Returns (date, strength), where strength 2 means the text between this date
    and the next date contains "חשוב"; otherwise strength is 1.
    """
    if not isinstance(blob, str):
        return []
    blob = _clean(blob)
    matches = list(_DATE_RE.finditer(blob))
    out: list[tuple[date, int]] = []
    for i, match in enumerate(matches):
        d, mth, yr = int(match.group(1)), int(match.group(2)), int(match.group(3) or default_year)
        try:
            parsed = date(yr, mth, d)
        except ValueError:
            continue
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(blob)
        note = blob[match.end():next_start]
        strength = 2 if "חשוב" in note else 1
        out.append((parsed, strength))
    return out


def preferred_night_dates_from_simple(
    df: pd.DataFrame,
    *,
    target_month: str,
    name_col: str = "שם",
    email_col: str = "כתובת אימייל",
    preferred_col: str = "תאריכים מועדפים",
    ts_col: str = "חותמת זמן",
) -> Dict[Tuple[str, date], int]:
    """
    Return {(name, date): strength} for requested night-duty dates.

    strength 1 = regular preference, strength 2 = important preference.
    Uses the latest submission per email/name, like unavail_from_simple.
    """
    if df.empty:
        return {}
    df = df.copy()

    cols_norm = {_clean(c): c for c in df.columns}

    def pick_optional(*wanted: str) -> str | None:
        for key in wanted:
            col = cols_norm.get(_clean(key))
            if col:
                return col
        return None

    name_col = (
        pick_optional(name_col, "בחר את שמך")
        or name_col
    )
    email_col = pick_optional(email_col) or email_col
    preferred_col = (
        pick_optional(preferred_col)
        or next(
            (
                col
                for label, col in cols_norm.items()
                if "תאריכים מועדפים" in label
                and "התייחסות חופשית" not in label
            ),
            "",
        )
        or pick_optional("התייחסות חופשית + תאריכים מועדפים")
        or ""
    )
    ts_col_opt = pick_optional(ts_col) or ""
    if not preferred_col or preferred_col not in df.columns:
        return {}
    if name_col not in df.columns and email_col not in df.columns:
        return {}

    df = _latest_rows(df, name_col=name_col, email_col=email_col, ts_col=ts_col_opt)

    year, month_num = map(int, target_month.split("-"))
    out: Dict[Tuple[str, date], int] = {}
    for _, row in df.iterrows():
        raw_name = _clean(row.get(name_col, ""))
        email = _clean(row.get(email_col, "")).lower()
        name = EMAIL_TO_NAME_NORMALIZED.get(email, raw_name)
        if not name:
            continue

        for dte, strength in _parse_preferred_blob_dates(row.get(preferred_col, ""), default_year=year):
            if dte.year != year or dte.month != month_num:
                continue
            key = (name, dte)
            out[key] = max(out.get(key, 0), strength)
    return out


def unavail_from_simple(
    df: pd.DataFrame,
    *,
    target_month: str,  # "YYYY-MM"
    name_col: str = "שם",
    email_col: str = "כתובת אימייל",
    nights_col: str = "לילות בהם אינכם יכולים לעשות תורנות/כוננות",
    days_off_col: str = "בחירת ימי חופש - כולל ימי שישי",
    ts_col: str = "חותמת זמן",
    source_label: str = "טופס זמינות",
) -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
    """
    Return unified availability mapping like the legacy parser.

    - Uses the latest submission per email.
    - Converts English names to Hebrew via EMAIL_TO_NAME.
    - Outputs a dict, not a DataFrame.
    """
    df = df.copy()

    # Normalize header variations
    cols_norm = { _clean(c): c for c in df.columns }
    def pick(wanted: str, *alts: str) -> str:
        for k in (wanted, *alts):
            ck = _clean(k)
            if ck in cols_norm:
                return cols_norm[ck]
        raise KeyError(f"Missing column: {wanted}. Available: {list(df.columns)}")

    name_col   = cols_norm.get(_clean(name_col)) or cols_norm.get(_clean("בחר את שמך")) or pick(name_col)
    email_col  = pick(email_col)
    nights_col = cols_norm.get(_clean(nights_col), nights_col)
    days_off_col = cols_norm.get(_clean(days_off_col), days_off_col)
    ts_col_opt = cols_norm.get(_clean(ts_col))

    for c in (nights_col, days_off_col):
        if c not in df.columns:
            df[c] = ""

    df = _latest_rows(df, name_col=name_col, email_col=email_col, ts_col=ts_col_opt or "")

    year = int(target_month.split("-")[0])
    simple_parsed: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}

    # Iterate each respondent
    for _, row in df.iterrows():
        raw_name = _clean(row.get(name_col, ""))
        email = _clean(row.get(email_col, "")).lower()
        name = EMAIL_TO_NAME_NORMALIZED.get(email, raw_name)
        if not name:
            continue

        # Days off → “לא זמין”
        for dte in _parse_blob_dates(row.get(days_off_col, ""), default_year=year):
            key = (name, dte.isoformat())
            lst = simple_parsed.setdefault(key, [])
            tag = ("לא זמין", source_label)
            if tag not in lst:
                lst.append(tag)

        # Nights → “לא זמין לתורנות”
        for dte in _parse_blob_dates(row.get(nights_col, ""), default_year=year):
            key = (name, dte.isoformat())
            lst = simple_parsed.setdefault(key, [])
            tag = ("לא זמין לתורנות", source_label)
            if tag not in lst:
                lst.append(tag)

    return simple_parsed
