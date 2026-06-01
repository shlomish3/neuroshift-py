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

from core import config
from core.constants import USE_SIMPLE_FORM


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TTL_SEC = 300
VERSION_CELL = ("Settings", "B2")

T = TypeVar("T")


def _retry(op: Callable[[], T], *, attempts: int = 3, base_sleep: float = 2.0) -> T:
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return op()
        except (TransportError, requests.exceptions.RequestException, TimeoutError) as e:
            last_exc = e
            if i == attempts - 1:
                raise
            time.sleep(base_sleep * (i + 1))
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


_last_pull = 0.0
_last_token = ""


def _should_refresh() -> bool:
    global _last_token

    if os.getenv("NEUROSHIFT_NOCACHE") == "1":
        return True

    if time.time() - _last_pull > TTL_SEC:
        return True

    try:
        sheet, cell = VERSION_CELL
        token = _sh().worksheet(sheet).acell(cell).value or ""
        if token != _last_token:
            _last_token = token
            return True
    except Exception:
        pass

    return False


def df(sheet_title: str) -> pd.DataFrame:
    ws = _retry(lambda: _sh().worksheet(sheet_title))
    values = _retry(lambda: ws.get_all_values())
    if not values:
        return pd.DataFrame()
    return pd.DataFrame(values[1:], columns=values[0])


def _df_from(sheet_id: str, sheet_title: str) -> pd.DataFrame:
    ws = _retry(lambda: _sh_by_id(sheet_id).worksheet(sheet_title))
    values = _retry(lambda: ws.get_all_values())
    if not values:
        return pd.DataFrame()
    return pd.DataFrame(values[1:], columns=values[0])


def push(sheet_title: str, frame: pd.DataFrame, start: str = "A1") -> None:
    frame = frame.drop_duplicates()
    _retry(lambda: _sh().values_update(
        f"{sheet_title}!{start}",
        params={"valueInputOption": "USER_ENTERED"},
        body={"values": [frame.columns.tolist()] + frame.values.tolist()},
    ))


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
        _gc.cache_clear()
        _creds.cache_clear()

    out = _backend_tables_cached()
    _last_pull = time.time()
    return out