"""
core/data.py
============

Google-Sheets I/O layer with a smart cache.

• Default: keep each sheet bundle for up to TTL_SEC.
• Set NEUROSHIFT_NOCACHE=1 to force a cold reload.
• Caches the main workbook and external workbooks separately.
• Retries transient Google auth / network failures.
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Dict, Callable, TypeVar

import gspread
import pandas as pd
import requests
from google.auth.exceptions import TransportError
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound

from core import config
from core.constants import USE_SIMPLE_FORM


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TTL_SEC = 300
RETRYABLE_API_CODES = {429, 500, 502, 503, 504}
HISTORY_SHEET = getattr(config, "HISTORY_TAB", "history")
HISTORY_SUMMARY_SHEET = getattr(config, "HISTORY_SUMMARY_TAB", "history_summary")

T = TypeVar("T")


def _retry(
    op: Callable[[], T],
    *,
    attempts: int = 7,
    base_sleep: float = 2.0,
    max_sleep: float = 20.0,
) -> T:
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return op()
        except APIError as e:
            if e.code not in RETRYABLE_API_CODES:
                raise
            last_exc = e
            if i == attempts - 1:
                raise
            retry_after = e.response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else base_sleep * (2 ** i)
            except (TypeError, ValueError):
                delay = base_sleep * (2 ** i)
            time.sleep(min(max_sleep, max(0.0, delay)))
        except (TransportError, requests.exceptions.RequestException, TimeoutError) as e:
            last_exc = e
            if i == attempts - 1:
                raise
            time.sleep(min(max_sleep, base_sleep * (2 ** i)))
    assert last_exc is not None
    raise last_exc


@lru_cache(maxsize=1)
def _creds() -> Credentials:
    return Credentials.from_service_account_file(config.CRED_FILE, scopes=SCOPES)


@lru_cache(maxsize=1)
def _gc() -> gspread.Client:
    return _retry(lambda: gspread.authorize(_creds()))


@lru_cache(maxsize=1)
def _sh() -> gspread.Spreadsheet:
    return _retry(lambda: _gc().open_by_key(config.SHEET_ID))


@lru_cache(maxsize=8)
def _sh_by_id(sheet_id: str) -> gspread.Spreadsheet:
    return _retry(lambda: _gc().open_by_key(sheet_id))


def _open_by_key(sheet_id: str) -> gspread.Spreadsheet:
    return _sh_by_id(sheet_id)


@lru_cache(maxsize=1)
def _history_sh() -> gspread.Spreadsheet:
    history_sheet_id = getattr(config, "HISTORY_SHEET_ID", "")
    if history_sheet_id:
        return _sh_by_id(history_sheet_id)
    return _sh()


_last_pull = 0.0
def _should_refresh() -> bool:
    if os.getenv("NEUROSHIFT_NOCACHE") == "1":
        return True
    return time.time() - _last_pull > TTL_SEC


def df(sheet_title: str) -> pd.DataFrame:
    ws = _retry(lambda: _sh().worksheet(sheet_title))
    values = _retry(lambda: ws.get_all_values())
    if not values:
        return pd.DataFrame()
    return pd.DataFrame(values[1:], columns=values[0])


def optional_df(sheet_title: str, *, history: bool = False) -> pd.DataFrame:
    try:
        if history:
            ws = _retry(lambda: _history_sh().worksheet(sheet_title))
            values = _retry(lambda: ws.get_all_values())
            if not values:
                return pd.DataFrame()
            return pd.DataFrame(values[1:], columns=values[0])
        return df(sheet_title)
    except WorksheetNotFound:
        return pd.DataFrame()


def _df_from(sheet_id: str, sheet_title: str) -> pd.DataFrame:
    ws = _retry(lambda: _sh_by_id(sheet_id).worksheet(sheet_title))
    values = _retry(lambda: ws.get_all_values())
    if not values:
        return pd.DataFrame()
    return pd.DataFrame(values[1:], columns=values[0])


def _ensure_worksheet(sheet_title: str, *, rows: int = 1000, cols: int = 26, history: bool = False):
    sh = _history_sh() if history else _sh()
    try:
        return _retry(lambda: sh.worksheet(sheet_title))
    except WorksheetNotFound:
        return _retry(lambda: sh.add_worksheet(title=sheet_title, rows=rows, cols=cols))


def push(sheet_title: str, frame: pd.DataFrame, start: str = "A1", *, history: bool = False) -> None:
    frame = frame.drop_duplicates()
    ws = _ensure_worksheet(
        sheet_title,
        rows=max(len(frame) + 10, 1000),
        cols=max(len(frame.columns) + 5, 26),
        history=history,
    )
    _retry(lambda: ws.clear())
    sh = _history_sh() if history else _sh()
    _retry(lambda: sh.values_update(
        f"{sheet_title}!{start}",
        params={"valueInputOption": "USER_ENTERED"},
        body={"values": [frame.columns.tolist()] + frame.values.tolist()},
    ))


def replace_sheet_month(sheet_title: str, frame: pd.DataFrame, month: str) -> None:
    """
    Replace rows whose Date/Month belongs to month, then write the whole tab.
    Used for finalized roster imports so repeated imports do not duplicate data.
    """
    existing = optional_df(sheet_title, history=True)
    if existing.empty:
        merged = frame.copy()
    else:
        date_col = "Date" if "Date" in existing.columns else None
        month_col = "Month" if "Month" in existing.columns else None
        if date_col:
            keep = ~existing[date_col].astype(str).str.startswith(month)
        elif month_col:
            keep = existing[month_col].astype(str) != month
        else:
            keep = pd.Series([True] * len(existing), index=existing.index)
        merged = pd.concat([existing.loc[keep], frame], ignore_index=True)

    push(sheet_title, merged, history=True)


@lru_cache(maxsize=1)
def _backend_tables_cached() -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {
        "holidays":       df("חגים"),
        "attending":      df("אטנדינג"),
        "fixed_assign":   df("שיבוצים קבועים"),
        "workers":        df("עובדים"),
        "required":       df("כמות נדרשת"),
        "post_admission": df("פוסט אשפוז"),
        "fixed_clinics":  df("מרפאות קבועות"),
        "clinic_map":     df("מפת מרפאות"),
        "personal_rules": optional_df("כללים אישיים"),
        "history":        optional_df(HISTORY_SHEET, history=True),
        "history_summary": optional_df(HISTORY_SUMMARY_SHEET, history=True),
    }

    if USE_SIMPLE_FORM and getattr(config, "SIMPLE_FORM_SHEET_ID", None):
        tab_name = getattr(config, "SIMPLE_FORM_TAB", "טופס זמינות")
        out["requests"] = _df_from(config.SIMPLE_FORM_SHEET_ID, tab_name)
    else:
        out["requests"] = df("תגובות לטופס זמינות")

    try:
        out["parsed_requests"] = df("זמינות מפורקת")
    except Exception:
        out["parsed_requests"] = pd.DataFrame()

    return out


def backend_tables() -> Dict[str, pd.DataFrame]:
    global _last_pull

    if _should_refresh():
        _backend_tables_cached.cache_clear()
        _sh.cache_clear()
        _sh_by_id.cache_clear()
        _history_sh.cache_clear()
        _gc.cache_clear()
        _creds.cache_clear()

    out = _backend_tables_cached()
    _last_pull = time.time()
    return out
