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

    # Deduplicate by latest timestamp per email
    key = email_col if email_col in df.columns else name_col
    if ts_col_opt and ts_col_opt in df.columns:
        df["_ts"] = pd.to_datetime(df[ts_col_opt], errors="coerce")
        df.sort_values("_ts", inplace=True)
        df = df.drop_duplicates(subset=[key], keep="last")
    else:
        df = df.drop_duplicates(subset=[key], keep="last")

    year = int(target_month.split("-")[0])
    simple_parsed: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}

    # Iterate each respondent
    for _, row in df.iterrows():
        raw_name = _clean(row.get(name_col, ""))
        email = _clean(row.get(email_col, "")).lower()
        name = EMAIL_TO_NAME.get(email, raw_name)
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
