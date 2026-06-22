"""
core/assign2.py
==============
Auto-assign neuro roster for a given month and return the filled roster
External contracts
------------------
• core.constants
      WEEKDAY_BONUS, BONUS_SHIFT_TYPES, PRIORITY_BUCKETS
• core.data
      backend_tables()  -> dict of DataFrames
      # save_roster(df, month)         # currently disabled
• core.eligibility2
      get_eligible_workers(...)
• core.roster
      template_for_month('YYYY-MM')
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Callable, Dict, Set

import hashlib
import pandas as pd
import logging
import sys
import os
import re

from core import constants
from core.constants import PRIORITY_BUCKETS, NIGHT_DUTY_SHIFTS, DUAL_OK
from core.clinic_calendar import build_clinic_needs, build_clinic_owners
from core.data import backend_tables, _sh, _backend_tables_cached, _sh_by_id, _gc, _creds         # , save_roster
from core.eligibility2 import get_eligible_workers, eligibility_reason          # public API
from core.elig_utils   import (                             # helper look-ups
    workers_df,
    fixed_clinic_lut,
    can_do,         
    is_senior,     
    unavail_lookup,
    _tables as _elig_tables,
    ISO2HEB,
    BLOCKS_ALL,
)

from core.roster      import template_for_month
from core import elig_utils as _el
from core.assign_utils import (
    fairness_score,
    filter_fixed_by_availability,
    fixed_lookup,
    bump_extra_day_off,
    write_unassigned_ledger,
    enforce_epilepsy_eeg_coupling,
)
from core.availability_simple_parser import preferred_night_dates_from_simple

if len(sys.argv) > 1:
    constants.CURRENT_TARGET_MONTH = sys.argv[1]
else:
    constants.CURRENT_TARGET_MONTH = None

# ───────────────────────────────────────────────
# Configure logging (console + file)
# ───────────────────────────────────────────────
LOG_DIR = r"C:\Users\shlom\Google Drive\Neurology\Projects\Neuro Shift\neuroshift-py\logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "neuroshift.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),  # overwrite each run
        # logging.StreamHandler(sys.stdout),                        # also show in console
    ]
)

# Suppress noisy third-party libraries
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("googleapiclient.discovery").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
ATTENDING_SHIFT = "\u05d0\u05d8\u05e0\u05d3\u05d9\u05e0\u05d2"
KONEN_MION_SHIFT = "\u05db\u05d5\u05e0\u05df \u05de\u05d9\u05d5\u05df"
YOEATZIM_SHIFT = "\u05d9\u05d9\u05e2\u05d5\u05e6\u05d9\u05dd \u05de\u05d5\u05d1\u05d9\u05dc\u05d9\u05dd"
RESIDENT_NIGHT_SHIFTS = {"ת.מיון", "ת.מיון 2"}
FRIDAY_NIGHT_MORNING_SHIFT = {"ת.מיון": "מיון", "ת.מיון 2": "מחלקה"}
FRIDAY_TOTAL_SHIFTS = {
    ATTENDING_SHIFT,
    "מחלקה",
    "מיון",
    YOEATZIM_SHIFT,
    "ת.מיון",
    "ת.מיון 2",
    KONEN_MION_SHIFT,
}
FRIDAY_DAY_BALANCE_SHIFTS = {
    ATTENDING_SHIFT,
    "מחלקה",
    "מיון",
    YOEATZIM_SHIFT,
    "EEG",
    "EEG ילדים",
}
GLINSKAYA_NAME = "\u05d2\u05dc\u05d9\u05e0\u05e1\u05e7\u05d9\u05d4"
ESLEY_NAME = "\u05e2\u05e1\u05dc\u05d9"
SHIMON_NAME = "\u05e9\u05de\u05e2\u05d5\u05df"
BARTAL_NAME = "\u05d1\u05e8\u05d8\u05dc"
KINAN_NAME = "\u05e7\u05d9\u05e0\u05df"
SHIMON_KONEN_TARGET = 2
EEG_SOFT_CAPS = {
    KINAN_NAME: 2,
    BARTAL_NAME: 6,
}
RESIDENT_NIGHT_EXTRA_CAPACITY = {
    "\u05d7\u05d3\u05d9\u05d2'\u05d4": 1,
}

# ───────────────────────────────────────────────
# Focused debug target (you can change these)
# ───────────────────────────────────────────────
DEBUG_CLINIC_SHIFT = "EMG"
DEBUG_CLINIC_DATE  = date(2025, 12, 2)   # 2025-12-02


def _is_debug_clinic(shift_type: str, shift_date: date) -> bool:
    return shift_type == DEBUG_CLINIC_SHIFT and shift_date == DEBUG_CLINIC_DATE


def _has_shift(daily_assignments: Dict[date, Dict[str, Set[str]]], d: date, name: str, shift: str) -> bool:
    return shift in daily_assignments.get(d, {}).get(name, set())


def _has_any_shift(daily_assignments: Dict[date, Dict[str, Set[str]]], d: date, name: str) -> bool:
    return bool(daily_assignments.get(d, {}).get(name, set()))


def _parse_history_date(value) -> date | None:
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, format="mixed", dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _parse_int_cell(value, default: int = 0) -> int:
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(float(text))
    except Exception:
        return default


def _parse_optional_int_cell(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except Exception:
        return None


def _norm_rule_text(value) -> str:
    return str(value or "").replace("\u200f", "").replace("\u200e", "").replace("\xa0", " ").strip()


def _rule_weekday(value) -> int | None:
    text = _norm_rule_text(value)
    if not text:
        return None
    mapping = {
        "ראשון": 6,
        "יום א": 6,
        "א": 6,
        "שני": 0,
        "יום ב": 0,
        "ב": 0,
        "שלישי": 1,
        "יום ג": 1,
        "ג": 1,
        "רביעי": 2,
        "יום ד": 2,
        "ד": 2,
        "חמישי": 3,
        "יום ה": 3,
        "ה": 3,
        "שישי": 4,
        "יום ו": 4,
        "ו": 4,
        "שבת": 5,
        "ש": 5,
    }
    return mapping.get(text)


def _parse_personal_rules(frame: pd.DataFrame) -> list[dict[str, object]]:
    if frame is None or frame.empty:
        return []
    normalized = frame.rename(columns={c: _norm_rule_text(c) for c in frame.columns})
    rules: list[dict[str, object]] = []
    for _, row in normalized.iterrows():
        name = _norm_rule_text(row.get("שם"))
        shift = _norm_rule_text(row.get("שיבוץ"))
        if not name or not shift:
            continue
        rules.append({
            "name": name,
            "shift": shift,
            "staffing": _norm_rule_text(row.get("איוש")) or "משמרת",
            "max": _parse_optional_int_cell(row.get("מכסה מקסימלית בחודש")),
            "min": _parse_int_cell(row.get("מכסה מינימלית בחודש"), 0),
            "condition": _norm_rule_text(row.get("תנאי")),
            "weekday": _rule_weekday(row.get("יום")),
        })
    return rules


def _previous_month_duty_summary(
    summary_df: pd.DataFrame,
    prev_month: str,
) -> tuple[Counter, Counter, bool, bool]:
    """
    Read the imported history_summary tab when available. It is already grouped
    by month/person and is less ambiguous than reconstructing totals from rows.
    """
    night_counts = Counter()
    weekend_counts = Counter()
    shimon_friday = False
    if summary_df is None or summary_df.empty:
        return night_counts, weekend_counts, shimon_friday, False

    df = summary_df.rename(columns={c: str(c).strip() for c in summary_df.columns})
    name_col = "Name" if "Name" in df.columns else "שם" if "שם" in df.columns else None
    month_col = "Month" if "Month" in df.columns else "חודש" if "חודש" in df.columns else None
    if not name_col or not month_col:
        return night_counts, weekend_counts, shimon_friday, False

    rows = df[df[month_col].astype(str).str.strip() == prev_month]
    if rows.empty:
        return night_counts, weekend_counts, shimon_friday, False

    for _, row in rows.iterrows():
        name = str(row.get(name_col, "")).strip()
        if not name:
            continue
        resident_nights = _parse_int_cell(row.get("ResidentNights", 0))
        weekend_shifts = _parse_int_cell(row.get("WeekendShifts", 0))
        night_counts[name] = resident_nights
        weekend_counts[name] = weekend_shifts
        if name == SHIMON_NAME and _parse_int_cell(row.get("FridayShifts", 0)) > 0:
            shimon_friday = True

    return night_counts, weekend_counts, shimon_friday, True


def _friday_night_morning_penalty(
    name: str,
    shift_type: str,
    shift_date: date,
    daily_assignments: Dict[date, Dict[str, Set[str]]],
) -> tuple[int, int]:
    """
    Prefer Friday ת.מיון/ת.מיון 2 candidates who can also cover the matching
    Friday morning: ת.מיון→מיון, ת.מיון 2→מחלקה.
    """
    if shift_date.weekday() != 4 or shift_type not in FRIDAY_NIGHT_MORNING_SHIFT:
        return (0, 0)

    desired = FRIDAY_NIGHT_MORNING_SHIFT[shift_type]
    if _has_shift(daily_assignments, shift_date, name, desired):
        return (0, 0)
    if eligibility_reason(name, shift_date.isoformat(), desired) is None:
        return (1, 0)
    return (2, 0)


def _alternate_risk_penalty(
    name: str,
    shift_type: str,
    shift_date: date,
    daily_assignments: Dict[date, Dict[str, Set[str]]],
) -> int:
    """
    Saturday resident nights create alternate-day needs only if paired with a
    Friday morning shift or Thursday resident night. Prefer candidates without
    those pairings when all hard rules still allow it.
    """
    if shift_date.weekday() != 5 or shift_type not in RESIDENT_NIGHT_SHIFTS:
        return 0

    friday = shift_date - timedelta(days=1)
    thursday = shift_date - timedelta(days=2)
    friday_morning = [
        s for s in daily_assignments.get(friday, {}).get(name, set())
        if s not in NIGHT_DUTY_SHIFTS and s not in {"חלופי", "חופש", "אחרי תורנות"}
    ]
    thursday_resident_night = bool(
        daily_assignments.get(thursday, {}).get(name, set()).intersection(RESIDENT_NIGHT_SHIFTS)
    )
    return int(bool(friday_morning)) + int(thursday_resident_night)


def _resident_night_spacing_penalty(
    name: str,
    shift_date: date,
    last_night: Dict[str, date],
) -> int:
    prev = last_night.get(name)
    if not prev:
        return 0
    gap = (shift_date - prev).days
    if gap == 2:
        return 100
    if gap == 3:
        return 1
    return 0


def _resident_sandwich_penalty(
    name: str,
    shift_date: date,
    daily_assignments: Dict[date, Dict[str, Set[str]]],
) -> int:
    def has_resident_night(d: date) -> bool:
        return bool(daily_assignments.get(d, {}).get(name, set()).intersection(RESIDENT_NIGHT_SHIFTS))

    penalty = 0
    if has_resident_night(shift_date - timedelta(days=2)) and not has_resident_night(shift_date - timedelta(days=1)):
        penalty += 100
    if has_resident_night(shift_date + timedelta(days=2)) and not has_resident_night(shift_date + timedelta(days=1)):
        penalty += 100
    return penalty


def _resident_adjacent_night_penalty(
    name: str,
    shift_date: date,
    daily_assignments: Dict[date, Dict[str, Set[str]]],
) -> int:
    for d in (shift_date - timedelta(days=1), shift_date + timedelta(days=1)):
        if daily_assignments.get(d, {}).get(name, set()).intersection(RESIDENT_NIGHT_SHIFTS):
            return 100
    return 0


def _name_list(cell: object) -> list[str]:
    out: list[str] = []
    for raw in str(cell or "").split(","):
        name = raw.replace("\u200f", "").replace("\u200e", "").replace("\xa0", " ").strip()
        if not name or name == "-":
            continue
        if name.startswith("\u26a0"):
            stripped = re.sub(r"^\u26a0\ufe0f?\s*\d+\s*/\s*\d+\s*", "", name).strip(" ,")
            if not stripped or stripped == name:
                continue
            name = stripped
        if "לבחור" in name and "חלופי" in name:
            continue
        if name.lower().startswith("needs manual pick"):
            continue
        if re.fullmatch(r"\d+\s*/\s*\d+", name):
            continue
        out.append(name)
    return out


def _write_name_list(names: list[str]) -> str:
    return ", ".join(names) if names else ""


# ──────────────────────────────────────────────────────────────
#  Main engine
# ──────────────────────────────────────────────────────────────
def _clear_sheet_caches() -> None:
    _backend_tables_cached.cache_clear()
    _sh.cache_clear()
    _sh_by_id.cache_clear()
    _gc.cache_clear()
    _creds.cache_clear()

    for fn in (
        workers_df,
        can_do,
        unavail_lookup,
        fixed_clinic_lut,
        _elig_tables,
    ):
        fn.cache_clear()


def auto_assign(
    month: str,
    dry_run: bool = False,
    progress_callback: Callable[[int, str], None] | None = None,
) -> pd.DataFrame:
    def report_progress(percent: int, label: str) -> None:
        if progress_callback:
            progress_callback(max(0, min(100, percent)), label)

    def assignment_progress_label(shift_type: str) -> str:
        if shift_type in RESIDENT_NIGHT_SHIFTS:
            return "משבץ תורנויות לילה"
        if shift_type in {KONEN_MION_SHIFT, ATTENDING_SHIFT}:
            return "משבץ כוננויות ואטנדינג"
        if shift_type == YOEATZIM_SHIFT:
            return "משבץ ייעוצים"
        if shift_type in {"מיון", "מחלקה", "EEG", "EEG ילדים"} or shift_type in _el.CLINIC_SHIFTS:
            return "משבץ מיון, מחלקה ומרפאות"
        return "משבץ שיבוצים יומיים"

    constants.CURRENT_TARGET_MONTH = month
    # ───── Phase 0: sheet caches ─────
    report_progress(2, "מרענן נתונים")
    print(f"[auto-assign] Reloading sheets for {month} …")
    _clear_sheet_caches()
    print("[auto-assign] Caches cleared")
    tbl = backend_tables()
    print("[auto-assign] Sheets loaded")
    report_progress(10, "טוען אילוצים")
    personal_rules = _parse_personal_rules(tbl.get("personal_rules", pd.DataFrame()))

    # 1. clinic needs from hospital calendar -----------------------------
    try:
        clinic_needs = build_clinic_needs(month)
        clinic_owners = build_clinic_owners(month)
        print(f"[auto-assign] Loaded clinic calendar: {len(clinic_needs)} (date,shift) entries")
    except Exception as e:
        print(f"[auto-assign] WARNING: failed to load clinic calendar for {month}: {type(e).__name__}: {e!r}")
        clinic_needs = {}
        clinic_owners = {}

    # DEBUG: what does the clinic calendar say for our debug EMG?
    debug_key = (DEBUG_CLINIC_DATE, DEBUG_CLINIC_SHIFT)
    logger.debug(
        "[EMG DEBUG] clinic_needs[%s,%s] = %r",
        DEBUG_CLINIC_DATE.isoformat(),
        DEBUG_CLINIC_SHIFT,
        clinic_needs.get(debug_key),
    )

    # 2. blank roster ----------------------------------------------------
    roster = template_for_month(month, clinic_needs=clinic_needs or None)
    print("[auto-assign] Blank template created")

    # DEBUG: roster row for debug clinic right after template creation
    debug_mask = (
        (roster["Shift"] == DEBUG_CLINIC_SHIFT)
        & (roster["Date"] == DEBUG_CLINIC_DATE.isoformat())
    )
    if debug_mask.any():
        r0 = roster.loc[debug_mask].iloc[0]
        logger.debug(
            "[EMG DEBUG] roster row pre-fixed: Date=%s Shift=%s Needed=%s SoftCap=%s Assigned=%r",
            r0["Date"], r0["Shift"], r0["Needed"], r0["SoftCap"], r0["Assigned"],
        )

    # 2. worker caps -----------------------------------------------------
    #   -deleted-

    # 3. historical counters --------------------------------------------
    # Tracks the first reason we failed to place somebody on a given day
    blocked_reasons: dict[tuple[date, str, str] | tuple[date, str], str] = {}

    hist_df = tbl.get("history", pd.DataFrame())
    if not hist_df.empty and "Name" not in hist_df.columns and "שם" in hist_df.columns:
        hist_df = hist_df.rename(columns={"שם": "Name"})

    try:
        preferred_night_requests = preferred_night_dates_from_simple(
            tbl.get("requests", pd.DataFrame()),
            target_month=month,
        )
    except Exception as e:
        logger.warning("Failed to parse preferred night-duty dates: %s: %r", type(e).__name__, e)
        preferred_night_requests = {}

    # Per-shift fairness starts fresh each generated month. Imported history is
    # used for night-duty recency and month-boundary blocking, not lifetime shift
    # counts such as "worked מיון many times last month".
    history: Dict[str, Counter] = defaultdict(Counter)

    last_night: Dict[str, date] = {}
    if not hist_df.empty:
        recent_night_rows = hist_df[hist_df["Shift"].isin(NIGHT_DUTY_SHIFTS)]
        if not recent_night_rows.empty:
            grp = recent_night_rows.groupby("Name")["Date"].max()
            last_night = {name: date.fromisoformat(d_iso) for name, d_iso in grp.items()}
# ------------------------------------------------------------------
    
    # 4. state trackers --------------------------------------------------
    blocked_next_day: Dict[str, set[date]] = defaultdict(set)
    extra_day_off: set[str] = set()
    daily_assignments: Dict[date, Dict[str, Set[str]]] = defaultdict(dict)

    yr, mon = map(int, month.split("-"))
    first_day = date(yr, mon, 1)
    prev_day = first_day - timedelta(days=1)
    prev_month_first = prev_day.replace(day=1)
    prev_month_str = prev_month_first.strftime("%Y-%m")
    previous_resident_night_counts = Counter()
    previous_resident_weekend_counts = Counter()
    previous_shimon_friday = False
    (
        summary_night_counts,
        summary_weekend_counts,
        summary_shimon_friday,
        loaded_previous_summary,
    ) = _previous_month_duty_summary(
        tbl.get("history_summary", pd.DataFrame()),
        prev_month_str,
    )
    if loaded_previous_summary:
        previous_resident_night_counts.update(summary_night_counts)
        previous_resident_weekend_counts.update(summary_weekend_counts)
        previous_shimon_friday = summary_shimon_friday
    if not hist_df.empty and {"Date", "Name", "Shift"}.issubset(hist_df.columns):
        for _, r in hist_df.iterrows():
            hist_date = _parse_history_date(r["Date"])
            if hist_date is None or not (prev_month_first <= hist_date <= prev_day):
                continue
            name = str(r["Name"]).strip()
            shift = str(r["Shift"]).strip()
            if name == SHIMON_NAME and hist_date.weekday() == 4:
                previous_shimon_friday = True
            if loaded_previous_summary:
                continue
            if not name or shift not in RESIDENT_NIGHT_SHIFTS:
                continue
            previous_resident_night_counts[name] += 1
            if hist_date.weekday() in (4, 5):
                previous_resident_weekend_counts[name] += 1

    previous_after_duty_names: list[str] = []
    if not hist_df.empty and {"Date", "Name", "Shift"}.issubset(hist_df.columns):
        for _, r in hist_df.iterrows():
            hist_date = _parse_history_date(r["Date"])
            if hist_date is None:
                continue
            if hist_date != prev_day:
                continue
            name = str(r["Name"]).strip()
            shift = str(r["Shift"]).strip()
            if shift in ("ת.מיון", "ת.מיון 2"):
                if name and name not in previous_after_duty_names:
                    previous_after_duty_names.append(name)
                blocked_next_day[name].add(first_day)
                daily_assignments[hist_date].setdefault(name, set()).add(shift)
                bump_extra_day_off(name, shift, hist_date, extra_day_off)
            if shift in NIGHT_DUTY_SHIFTS:
                last_night[name] = hist_date

    # 5. fixed assignments + mute clinics -------------------------------
    def _to_int(x, default=0):
        try:
            return int(x)
        except Exception:
            return default

    fixed_raw = fixed_lookup(month, tbl)                     # (date, shift) → [names]
    fixed_required_counts = {
        key: len([n for n in names if str(n).strip()])
        for key, names in fixed_raw.items()
        if any(str(n).strip() for n in names)
    }
    for key, owners in clinic_owners.items():
        if not owners:
            continue
        clinic_date, clinic_shift = key
        mask = (roster["Date"] == clinic_date.isoformat()) & (roster["Shift"] == clinic_shift)
        if not mask.any():
            continue
        idx = roster.index[mask][0]
        slot_count = _to_int(roster.at[idx, "Needed"], 0)
        if slot_count <= 0:
            continue
        fixed_raw.setdefault(key, [])
        for owner in sorted(owners)[:slot_count]:
            if owner not in fixed_raw[key]:
                fixed_raw[key].append(owner)
    fixed     = filter_fixed_by_availability(fixed_raw)      # honour day-off requests
    print(f"[auto-assign] Injected {sum(map(len, fixed.values()))} fixed rows")
    report_progress(25, "משבץ קבועים")

    roster["Assigned"] = ""
    if previous_after_duty_names:
        mask = (roster["Date"] == first_day.isoformat()) & (roster["Shift"] == "אחרי תורנות")
        if mask.any():
            roster.loc[mask, "Assigned"] = ", ".join(previous_after_duty_names)

    # Named clinic rows from יומן מרפאות are authoritative for the date/owner
    # preference, but כמות נדרשת controls capacity.
    for (clinic_date, clinic_shift), owners in clinic_owners.items():
        if not owners:
            continue
        mask = (roster["Date"] == clinic_date.isoformat()) & (roster["Shift"] == clinic_shift)
        if mask.any():
            idx = roster.index[mask][0]
            target_count = _to_int(roster.at[idx, "Needed"], 0)
            roster.loc[mask, ["Needed", "SoftCap"]] = [target_count, target_count]

    # A fixed row represents a real slot even if the named fixed worker is later
    # filtered out for day-off/rest. Keep the slot so auto-assignment can fill it.
    for (fixed_date, fixed_shift), required_count in fixed_required_counts.items():
        mask = (roster["Date"] == fixed_date.isoformat()) & (roster["Shift"] == fixed_shift)
        if not mask.any():
            continue
        idx = roster.index[mask][0]
        roster.at[idx, "Needed"] = max(_to_int(roster.at[idx, "Needed"], 0), required_count)
        roster.at[idx, "SoftCap"] = max(_to_int(roster.at[idx, "SoftCap"], 0), required_count)

    def _required_clinic_row(d: date, shift: str) -> bool:
        if _to_int(clinic_needs.get((d, shift), 0), 0) > 0:
            return True
        mask = (roster["Date"] == d.isoformat()) & (roster["Shift"] == shift)
        if not mask.any():
            return False
        idx = roster.index[mask][0]
        return _to_int(roster.at[idx, "Needed"], 0) > 0

    # ─── mute clinics that clash with a fixed Attending ───
    clinic_lut = fixed_clinic_lut()          # (name, heb_day) → {clinic_shift, …}
    for (d, shift_type), names in fixed.items():
        if shift_type != ATTENDING_SHIFT:
            continue

        heb = ISO2HEB[d.isoweekday() - 1]
        for doc in names:
            for cl in clinic_lut.get((doc, heb), set()):
                if _required_clinic_row(d, cl):
                    continue
                mask = (roster["Date"] == d.isoformat()) & (roster["Shift"] == cl)
                if mask.any():
                    roster.loc[mask, ["Needed", "Assigned"]] = [0, "-"]

    # ─── write the fixed staff into the roster ───
    fixed_assignment_keys: set[tuple[date, str, str]] = set()
    shimon_fixed_friday_used = False
    for idx, row in roster.iterrows():
        key      = (date.fromisoformat(row["Date"]), row["Shift"])
        fx_names = fixed.get(key, [])
        if not fx_names:
            continue

        # -------------------------------------------------------------
        #  Enforce rest-day & night-cool-down rules on fixed rows
        # -------------------------------------------------------------
        pruned = []
        for doc in fx_names:
            # 1) mandatory rest-day after ת.מיון
            if key[0] in blocked_next_day.get(doc, set()):
                logger.warning(
                    "Fixed %s %s – %s skipped (next-day rest rule)",
                    row.Date, row.Shift, doc
                )
                continue

            # 2) ≥48 h between any two night duties
            if row["Shift"] in NIGHT_DUTY_SHIFTS and \
               (key[0] - last_night.get(doc, date.min)).days < 2:
                logger.warning(
                    "Fixed %s %s – %s skipped (night cool-down <2 days)",
                    row.Date, row.Shift, doc
                )
                continue

            if doc == SHIMON_NAME and key[0].weekday() == 4:
                if previous_shimon_friday or shimon_fixed_friday_used:
                    logger.warning(
                        "Fixed %s %s – %s skipped (Shimon Friday cap)",
                        row.Date, row.Shift, doc
                    )
                    continue
                shimon_fixed_friday_used = True
            if row["Shift"] == YOEATZIM_SHIFT and doc == SHIMON_NAME and key[0].weekday() != 4:
                logger.warning(
                    "Fixed %s %s – %s skipped (Shimon consult restriction)",
                    row.Date, row.Shift, doc
                )
                continue

            pruned.append(doc)

        fx_names = pruned
        if not fx_names:
            continue  # every name got pruned → leave slot for auto-assign

        # --- clinics: allow a fixed "joiner" only if the clinic owner is available ---
        def _to_int(x, default=0):
            try:
                return int(x)
            except Exception:
                return default

        count = len(fx_names)
        soft  = _to_int(row.SoftCap, 0)
        need  = _to_int(row.Needed, 0)

        if row["Shift"] in _el.CLINIC_SHIFTS:
            calendar_owned_clinic = bool(clinic_owners.get((key[0], row["Shift"])))
            # Identify clinic "owner": the only eligible name in עובדים for this clinic
            eligible_names = [nm for (nm, sh), ok in _el.can_do().items() if sh == row["Shift"] and ok]
            eligible_count = len(eligible_names)

            # Owner available (or multiple eligibles exist):
            # ensure capacity fits fixed joiners AND (if sole owner case) the owner
            target_cap = count
            if count >= 1 and eligible_count == 1 and not calendar_owned_clinic:
                target_cap = max(target_cap, 2)  # owner + one joiner

            if soft < target_cap:
                roster.at[idx, "SoftCap"] = target_cap
            if need < target_cap:
                roster.at[idx, "Needed"]  = target_cap
            # keep all fx_names (no trimming)

        else:
            # Fixed rows are authoritative: expand capacity rather than dropping names.
            cap = soft or need
            if count > cap:
                logger.warning(
                    "Fixed assignments over-filled %s %s: expanding capacity from %d to %d (%s)",
                    row.Date, row.Shift, cap, count, ", ".join(fx_names),
                )
                roster.at[idx, "SoftCap"] = max(soft, count)
                roster.at[idx, "Needed"] = max(need, count)

        roster.at[idx, "Assigned"] = ", ".join(fx_names)

        # Update counters and state
        d = key[0]
        for n in fx_names:
            fixed_assignment_keys.add((d, row["Shift"], n))
            history[n][row["Shift"]] += 1
            daily_assignments[d].setdefault(n, set()).add(row["Shift"])

            if row["Shift"] in ("ת.מיון", "ת.מיון 2"):
                blocked_next_day[n].add(d + timedelta(days=1))
                bump_extra_day_off(n, row["Shift"], d, extra_day_off)
            if row["Shift"] in NIGHT_DUTY_SHIFTS:
                last_night[n] = d

    # DEBUG: roster row for debug clinic after fixed-injection
    debug_mask = (
        (roster["Shift"] == DEBUG_CLINIC_SHIFT)
        & (roster["Date"] == DEBUG_CLINIC_DATE.isoformat())
    )
    if debug_mask.any():
        r0 = roster.loc[debug_mask].iloc[0]
        logger.debug(
            "[EMG DEBUG] roster row post-fixed: Date=%s Shift=%s Needed=%s SoftCap=%s Assigned=%r",
            r0["Date"], r0["Shift"], r0["Needed"], r0["SoftCap"], r0["Assigned"],
        )

    # Botox is a real clinic-calendar row, but כמות נדרשת controls its capacity.
    # Do not inflate it to all Botox-capable doctors; fixed same-day duties such
    # as אטנדינג must be allowed to keep one of the capable doctors out.
    botox_shift = "מרפאת בוטוקס"
    for idx, row in roster[roster["Shift"] == botox_shift].iterrows():
        if _to_int(row.Needed, 0) <= 0 and not _required_clinic_row(date.fromisoformat(str(row.Date)), botox_shift):
            continue
        current_count = len(_name_list(roster.at[idx, "Assigned"]))
        needed_count = _to_int(row.Needed, 0)
        target_count = max(current_count, needed_count)
        roster.at[idx, "Needed"] = target_count
        roster.at[idx, "SoftCap"] = target_count

    base_blocked_next_day = {
        name: set(days)
        for name, days in blocked_next_day.items()
    }
    base_last_night = dict(last_night)

    # 6. assignment loop -------------------------------------------------
    month_counts = Counter()          # doctor → ת.מיון 2 + ת.מיון count in this run

    # ---------------------------------------------------------------------
    # Night-duty load already on the roster (fixed rows + earlier passes)
    # ---------------------------------------------------------------------
    def _names(s: str):
        """split a cell to individual, cleaned names (skip blanks or warnings)"""
        for n in s.split(","):
            n = n.replace("\u200f", "").replace("\u200e", "").replace("\xa0", " ").strip()
            if not n or n == "-":
                continue
            if n.startswith("\u26a0"):
                stripped = re.sub(r"^\u26a0\ufe0f?\s*\d+\s*/\s*\d+\s*", "", n).strip(" ,")
                if not stripped or stripped == n:
                    continue
                n = stripped
            if "לבחור" in n and "חלופי" in n:
                continue
            if n.lower().startswith("needs manual pick"):
                continue
            if re.fullmatch(r"\d+\s*/\s*\d+", n):
                continue
            yield n

    total_slots   = int(roster["Needed"].sum())
    filled_so_far = sum(1 for value in roster["Assigned"] for _ in _names(str(value or "")))
    print(f"[auto-assign] Filling shifts…")
    report_progress(30, "משבץ תורנויות")

    month_counts = Counter(
        name
        for _, row in roster[roster["Shift"].isin(["ת.מיון", "ת.מיון 2"])].iterrows()
        for name in _names(row.Assigned)
    )
    weekend_night_counts = Counter(
        name
        for _, row in roster[
            roster["Shift"].isin(["ת.מיון", "ת.מיון 2"])
            & roster["Date"].map(lambda x: date.fromisoformat(str(x)).weekday() in (4, 5))
        ].iterrows()
        for name in _names(row.Assigned)
    )
    saturday_night_counts = Counter(
        name
        for _, row in roster[
            roster["Shift"].isin(["ת.מיון", "ת.מיון 2"])
            & roster["Date"].map(lambda x: date.fromisoformat(str(x)).weekday() == 5)
        ].iterrows()
        for name in _names(row.Assigned)
    )
    thursday_night_counts = Counter(
        name
        for _, row in roster[
            roster["Shift"].isin(["ת.מיון", "ת.מיון 2"])
            & roster["Date"].map(lambda x: date.fromisoformat(str(x)).weekday() == 3)
        ].iterrows()
        for name in _names(row.Assigned)
    )
    resident_night_shift_counts: dict[str, Counter] = {
        "ת.מיון": Counter(
            name
            for _, row in roster[roster["Shift"] == "ת.מיון"].iterrows()
            for name in _names(row.Assigned)
        ),
        "ת.מיון 2": Counter(
            name
            for _, row in roster[roster["Shift"] == "ת.מיון 2"].iterrows()
            for name in _names(row.Assigned)
        ),
    }
    konen_month_counts = Counter(
        name
        for _, row in roster[roster["Shift"] == KONEN_MION_SHIFT].iterrows()
        for name in _names(row.Assigned)
    )
    konen_friday_counts = Counter(
        name
        for _, row in roster[
            (roster["Shift"] == KONEN_MION_SHIFT)
            & roster["Date"].map(lambda x: date.fromisoformat(str(x)).weekday() == 4)
        ].iterrows()
        for name in _names(row.Assigned)
    )
    yoeatzim_counts = Counter(
        name
        for _, row in roster[roster["Shift"] == YOEATZIM_SHIFT].iterrows()
        for name in _names(row.Assigned)
    )
    yoeatzim_weekday_counts = Counter(
        name
        for _, row in roster[roster["Shift"] == YOEATZIM_SHIFT].iterrows()
        if date.fromisoformat(str(row["Date"])).weekday() not in (4, 5)
        for name in _names(row.Assigned)
    )
    attending_counts = Counter(
        name
        for _, row in roster[roster["Shift"] == ATTENDING_SHIFT].iterrows()
        for name in _names(row.Assigned)
    )
    eeg_counts = Counter(
        name
        for _, row in roster[roster["Shift"] == "EEG"].iterrows()
        for name in _names(row.Assigned)
    )
    personal_assignment_counts = Counter(
        (name, str(row.Shift))
        for _, row in roster.iterrows()
        for name in _names(row.Assigned)
    )
    worker_shift_lut = can_do()
    month_dates: list[date] = []
    cur = first_day
    while cur.month == mon:
        month_dates.append(cur)
        cur += timedelta(days=1)
    active_resident_night_names = {
        name
        for (name, shift), ok in worker_shift_lut.items()
        if ok
        and shift in RESIDENT_NIGHT_SHIFTS
        and any(
            worker_shift_lut.get((name, candidate_shift), False)
            and eligibility_reason(name, d.isoformat(), candidate_shift) is None
            for d in month_dates
            for candidate_shift in RESIDENT_NIGHT_SHIFTS
        )
    }
    sun_thu_month_dates = {d for d in month_dates if d.weekday() not in (4, 5)}
    rotation_dates_by_name: dict[str, set[date]] = defaultdict(set)
    for _, row in roster[roster["Shift"] == "רוטציה"].iterrows():
        d = date.fromisoformat(str(row["Date"]))
        if d.weekday() in (4, 5):
            continue
        for name in _names(row.Assigned):
            rotation_dates_by_name[name].add(d)
    full_month_rotation_names = {
        name
        for name, days in rotation_dates_by_name.items()
        if sun_thu_month_dates and days >= sun_thu_month_dates
    }
    rotation_override_blocks = {"חופש", "אחרי תורנות"}
    for idx, row in roster[roster["Shift"] == "רוטציה"].iterrows():
        d = date.fromisoformat(str(row["Date"]))
        current = _name_list(row["Assigned"])
        if not current:
            continue
        keep: list[str] = []
        removed: list[str] = []
        for name in current:
            today = daily_assignments.get(d, {}).get(name, set())
            if name in full_month_rotation_names and today.intersection(rotation_override_blocks):
                removed.append(name)
                if "רוטציה" in today:
                    today.discard("רוטציה")
                fixed_assignment_keys.discard((d, "רוטציה", name))
                continue
            keep.append(name)
        if removed:
            roster.at[idx, "Assigned"] = _write_name_list(keep)
            roster.at[idx, "Needed"] = len(keep)
            roster.at[idx, "SoftCap"] = len(keep)
            logger.info(
                "Removed full-month rotation overridden by rest/vacation: %s %s",
                d.isoformat(), ", ".join(removed),
            )
    all_worker_names = workers_df()["שם"].tolist()
    worker_names_set = set(all_worker_names)
    senior_names = {
        name
        for name in all_worker_names
        if is_senior(name, worker_shift_lut)
    }
    friday_dates_by_name: dict[str, set[date]] = defaultdict(set)
    for _, row in roster[roster["Shift"].isin(FRIDAY_TOTAL_SHIFTS)].iterrows():
        d = date.fromisoformat(str(row["Date"]))
        if d.weekday() != 4:
            continue
        for name in _names(row.Assigned):
            friday_dates_by_name[name].add(d)
    friday_day_counts = Counter({
        name: len(days)
        for name, days in friday_dates_by_name.items()
    })
    preferred_night_assignment_keys: set[tuple[date, str, str]] = set()
    preferred_night_hits = Counter()
    important_preferred_night_hits = Counter()
    preferred_night_requesters_by_date: dict[date, set[str]] = defaultdict(set)
    for pref_name, pref_date in preferred_night_requests:
        preferred_night_requesters_by_date[pref_date].add(pref_name)

    def _rule_matches_date(rule: dict[str, object], d: date) -> bool:
        weekday = rule.get("weekday")
        return weekday is None or weekday == d.weekday()

    def _matching_personal_rules(
        name: str,
        shift_type: str,
        shift_date: date,
        staffing: str = "משמרת",
    ) -> list[dict[str, object]]:
        return [
            rule for rule in personal_rules
            if rule.get("name") == name
            and rule.get("shift") == shift_type
            and rule.get("staffing") == staffing
            and _rule_matches_date(rule, shift_date)
        ]

    def _personal_shift_max(name: str, shift_type: str, shift_date: date) -> int | None:
        caps = [
            int(rule["max"])
            for rule in _matching_personal_rules(name, shift_type, shift_date)
            if rule.get("max") is not None
        ]
        if caps:
            return min(caps)
        if shift_type == "EEG":
            return EEG_SOFT_CAPS.get(name)
        return None

    def _personal_under_max(name: str, shift_type: str, shift_date: date) -> bool:
        cap = _personal_shift_max(name, shift_type, shift_date)
        return cap is None or personal_assignment_counts[(name, shift_type)] < cap

    def _personal_rule_key(name: str, shift_type: str, shift_date: date) -> tuple[int, int, int]:
        rules = _matching_personal_rules(name, shift_type, shift_date)
        if not rules:
            return (2, 0, personal_assignment_counts[(name, shift_type)])

        best = (2, 0, personal_assignment_counts[(name, shift_type)])
        for rule in rules:
            condition = str(rule.get("condition") or "")
            minimum = int(rule.get("min") or 0)
            maximum = rule.get("max")
            count = personal_assignment_counts[(name, shift_type)]
            if condition == "חובה":
                rank = 0 if count < minimum else 2
            elif condition == "אם אפשר":
                rank = 1
            elif condition == "אם צריך":
                rank = 4
            else:
                rank = 2
            remaining = 999 if maximum is None else max(0, int(maximum) - count)
            best = min(best, (rank, -remaining, count))
        return best

    def _is_senior_name(name: str) -> bool:
        return name in senior_names

    def _resident_night_capable_shifts(name: str) -> set[str]:
        return {
            shift
            for shift in RESIDENT_NIGHT_SHIFTS
            if worker_shift_lut.get((name, shift), False)
        }

    def _friday_adds_day(name: str, d: date) -> bool:
        return d.weekday() == 4 and d not in friday_dates_by_name.get(name, set())

    def _friday_konen_pair_rank(name: str, d: date) -> int:
        if d.weekday() != 4 or not _is_senior_name(name):
            return 2
        if _has_shift(daily_assignments, d, name, KONEN_MION_SHIFT):
            return 0
        if _has_shift(daily_assignments, d + timedelta(days=1), name, KONEN_MION_SHIFT):
            return 1
        return 2

    def _friday_work_key(name: str, shift_type: str, d: date) -> tuple[int, ...]:
        if d.weekday() != 4 or shift_type not in FRIDAY_TOTAL_SHIFTS:
            return (0, 0, 0, 0)

        projected = friday_day_counts[name] + int(_friday_adds_day(name, d))
        if _is_senior_name(name):
            # Seniors should ideally have exactly one Friday total. If all
            # eligible seniors are already capped, residents can be preferred
            # for shifts they are allowed to cover.
            return (0 if projected <= 1 else 2, _friday_konen_pair_rank(name, d), projected, 0)

        # Residents fill overflow Friday work and should be balanced too.
        return (1, 2, friday_day_counts[name], projected)

    def _record_friday_assignment(name: str, shift_type: str, d: date) -> None:
        if d.weekday() != 4 or shift_type not in FRIDAY_TOTAL_SHIFTS:
            return
        if d not in friday_dates_by_name[name]:
            friday_dates_by_name[name].add(d)
            friday_day_counts[name] += 1

    row_date_by_idx: dict[int, date] = {}
    row_weekday_by_idx: dict[int, int] = {}
    row_index_by_date_shift: dict[tuple[date, str], int] = {}
    rows_by_shift: dict[str, list[int]] = defaultdict(list)
    assigned_names_cache: dict[int, tuple[str, tuple[str, ...]]] = {}

    for idx, row in roster.iterrows():
        row_idx = int(idx)
        row_date = date.fromisoformat(str(row["Date"]))
        row_shift = str(row["Shift"])
        row_date_by_idx[row_idx] = row_date
        row_weekday_by_idx[row_idx] = row_date.weekday()
        row_index_by_date_shift[(row_date, row_shift)] = row_idx
        rows_by_shift[row_shift].append(row_idx)

    resident_night_row_indexes = [
        idx
        for shift in RESIDENT_NIGHT_SHIFTS
        for idx in rows_by_shift.get(shift, [])
    ]
    resident_night_row_indexes.sort(key=lambda idx: (row_date_by_idx[idx], str(roster.at[idx, "Shift"])))

    def _row_date(idx: int) -> date:
        return row_date_by_idx[int(idx)]

    def _row_weekday(idx: int) -> int:
        return row_weekday_by_idx[int(idx)]

    def _assigned_names(idx: int) -> list[str]:
        row_idx = int(idx)
        cell = str(roster.at[row_idx, "Assigned"] or "")
        cached = assigned_names_cache.get(row_idx)
        if cached is not None and cached[0] == cell:
            return list(cached[1])
        names = tuple(_name_list(cell))
        assigned_names_cache[row_idx] = (cell, names)
        return list(names)

    def _resident_night_signature() -> tuple[tuple[int, str], ...]:
        return tuple(
            (idx, str(roster.at[idx, "Assigned"] or ""))
            for idx in resident_night_row_indexes
        )

    resident_repair_noop_signatures: dict[str, tuple[tuple[int, str], ...]] = {}

    def _repair_noop_cached(label: str) -> bool:
        return resident_repair_noop_signatures.get(label) == _resident_night_signature()

    def _remember_repair_noop(label: str) -> None:
        resident_repair_noop_signatures[label] = _resident_night_signature()

    def _forget_repair_noops() -> None:
        resident_repair_noop_signatures.clear()

    def _can_worker_take_shift(
        name: str,
        shift_type: str,
        shift_date: date,
        *,
        last_night_map: Dict[str, date] | None = None,
    ) -> bool:
        if name not in worker_names_set:
            return False
        if eligibility_reason(name, shift_date.isoformat(), shift_type) is not None:
            return False

        last_night_map = last_night_map or {}
        if shift_type in RESIDENT_NIGHT_SHIFTS:
            tomorrow_iso = (shift_date + timedelta(days=1)).isoformat()
            tomorrow_clinics = clinic_lut.get((name, _el.weekday_letter(tomorrow_iso)), set())
            if tomorrow_clinics:
                return False

            tomorrow = daily_assignments.get(shift_date + timedelta(days=1), {}).get(name, set())
            if any(s in _el.CLINIC_SHIFTS or s == "מיון" for s in tomorrow):
                return False

        if shift_date in blocked_next_day.get(name, set()):
            return False

        if shift_type in RESIDENT_NIGHT_SHIFTS and (shift_date - last_night_map.get(name, date.min)).days < 2:
            return False

        today_set = daily_assignments.get(shift_date, {}).get(name, set())
        senior = _is_senior_name(name)

        if name == SHIMON_NAME and shift_type == YOEATZIM_SHIFT and shift_date.weekday() != 4:
            return False

        if "רוטציה" in today_set and shift_type in _el.DAY_SHIFTS and shift_type != "רוטציה":
            return False
        if shift_type == "רוטציה" and today_set.intersection(_el.DAY_SHIFTS - {"רוטציה"}):
            return False
        if shift_type in _el.DAY_SHIFTS:
            incompatible_day = [
                existing for existing in today_set.intersection(_el.DAY_SHIFTS)
                if (existing, shift_type) not in DUAL_OK
            ]
            if incompatible_day:
                return False

        if not today_set:
            return True

        total_after = len(today_set) + 1
        if len(today_set) == 1:
            existing = next(iter(today_set))
            if (existing, shift_type) not in DUAL_OK:
                return False
            return senior or total_after <= 2

        if not senior:
            return False
        if total_after > 3:
            return False
        special = {KONEN_MION_SHIFT, "בכיר מיון"}
        return total_after <= 2 or shift_type in special or any(s in special for s in today_set)

    def _rebuild_night_state_from_roster() -> None:
        blocked_next_day.clear()
        for name, days in base_blocked_next_day.items():
            blocked_next_day[name].update(days)

        last_night.clear()
        last_night.update(base_last_night)

        for idx, row in roster.sort_values("Date").iterrows():
            d = _row_date(idx)
            shift = str(row["Shift"])
            for name in _assigned_names(idx):
                if shift in RESIDENT_NIGHT_SHIFTS:
                    blocked_next_day[name].add(d + timedelta(days=1))
                if shift in NIGHT_DUTY_SHIFTS:
                    last_night[name] = max(last_night.get(name, date.min), d)

    def _rebuild_live_counters_from_roster() -> None:
        month_counts.clear()
        weekend_night_counts.clear()
        saturday_night_counts.clear()
        thursday_night_counts.clear()
        for counter in resident_night_shift_counts.values():
            counter.clear()
        konen_month_counts.clear()
        konen_friday_counts.clear()
        yoeatzim_counts.clear()
        yoeatzim_weekday_counts.clear()
        attending_counts.clear()
        eeg_counts.clear()
        personal_assignment_counts.clear()
        friday_dates_by_name.clear()
        friday_day_counts.clear()
        preferred_night_assignment_keys.clear()
        preferred_night_hits.clear()
        important_preferred_night_hits.clear()

        for idx, row in roster.iterrows():
            d = _row_date(idx)
            shift = str(row["Shift"])
            for name in _assigned_names(idx):
                personal_assignment_counts[(name, shift)] += 1
                if shift in ("ת.מיון", "ת.מיון 2"):
                    month_counts[name] += 1
                    resident_night_shift_counts[shift][name] += 1
                    if d.weekday() in (4, 5):
                        weekend_night_counts[name] += 1
                    if d.weekday() == 5:
                        saturday_night_counts[name] += 1
                    if d.weekday() == 3:
                        thursday_night_counts[name] += 1
                elif shift == KONEN_MION_SHIFT:
                    konen_month_counts[name] += 1
                    if d.weekday() == 4:
                        konen_friday_counts[name] += 1
                elif shift == YOEATZIM_SHIFT:
                    yoeatzim_counts[name] += 1
                    if d.weekday() not in (4, 5):
                        yoeatzim_weekday_counts[name] += 1
                elif shift == ATTENDING_SHIFT:
                    attending_counts[name] += 1
                elif shift == "EEG":
                    eeg_counts[name] += 1

                if d.weekday() == 4 and shift in FRIDAY_TOTAL_SHIFTS:
                    friday_dates_by_name[name].add(d)
                if _is_preferred_night_assignment(name, shift, d):
                    preferred_night_assignment_keys.add((d, shift, name))
                    preferred_night_hits[name] += 1
                    if _preferred_night_strength(name, d) >= 2:
                        important_preferred_night_hits[name] += 1

        for name, days in friday_dates_by_name.items():
            friday_day_counts[name] = len(days)

    def _senior_yoeatzim_preferred_cap(name: str) -> int:
        return 1 if attending_counts[name] >= 10 else 2

    def _yoeatzim_assignment_tier(name: str, shift_date: date) -> int:
        if shift_date.weekday() in (4, 5) or not _is_senior_name(name):
            return 1
        projected_weekday = yoeatzim_weekday_counts[name] + 1
        preferred_cap = _senior_yoeatzim_preferred_cap(name)
        if projected_weekday <= preferred_cap:
            return 0
        if projected_weekday <= 2:
            return 2
        return 5

    def _yoeatzim_weekday_cap_objective() -> tuple[int, int, int]:
        hard_over = 0
        preferred_over = 0
        senior_square = 0
        for name in _senior_yoeatzim_pool():
            count = yoeatzim_weekday_counts[name]
            hard_over += max(0, count - 2)
            preferred_over += max(0, count - _senior_yoeatzim_preferred_cap(name))
            senior_square += count * count
        return (hard_over, preferred_over, senior_square)

    def _eeg_under_cap(name: str, shift_date: date) -> bool:
        return _personal_under_max(name, "EEG", shift_date)

    def _eeg_key(name: str, shift_date: date) -> tuple[int, int, float]:
        return (
            *_personal_rule_key(name, "EEG", shift_date),
            1 if name in EEG_SOFT_CAPS else 0,
            eeg_counts[name],
            fairness_score(name, "EEG", shift_date, history, _last_night_before(shift_date)),
        )

    def _yoeatzim_key(name: str, shift_date: date) -> tuple[int, int, int, int, int, int, float]:
        if not _yoeatzim_allowed(name, shift_date):
            return (999, 999, 999, 999, 999, 999, 999.0)
        is_senior_candidate = _is_senior_name(name)
        weekday_add = int(shift_date.weekday() not in (4, 5))
        projected_weekday = yoeatzim_weekday_counts[name] + weekday_add
        attending_load = attending_counts[name] if is_senior_candidate else 0
        return (
            *_personal_rule_key(name, YOEATZIM_SHIFT, shift_date),
            _yoeatzim_assignment_tier(name, shift_date),
            yoeatzim_counts[name] + (attending_load // 3 if is_senior_candidate else 0),
            projected_weekday,
            attending_load,
            month_counts[name] if not is_senior_candidate else 0,
            0 if is_senior_candidate else 1,
            fairness_score(name, YOEATZIM_SHIFT, shift_date, history, last_night),
        )

    def _record_yoeatzim_assignment(name: str, shift_date: date) -> None:
        yoeatzim_counts[name] += 1
        if shift_date.weekday() not in (4, 5):
            yoeatzim_weekday_counts[name] += 1

    def _preferred_night_strength(name: str, shift_date: date) -> int:
        return int(preferred_night_requests.get((name, shift_date), 0))

    def _preferred_night_shifts_for(name: str) -> set[str]:
        resident_shifts = _resident_night_capable_shifts(name)
        if resident_shifts:
            return resident_shifts
        if _is_senior_name(name):
            return {KONEN_MION_SHIFT}
        return set()

    for (pref_name, pref_date), strength in sorted(
        preferred_night_requests.items(),
        key=lambda item: (item[0][0], item[0][1]),
    ):
        logger.info(
            "preferred night request parsed: %s %s strength=%d shifts=%s",
            pref_date.isoformat(), pref_name, strength,
            "/".join(sorted(_preferred_night_shifts_for(pref_name))) or "none",
        )

    def _is_preferred_night_assignment(name: str, shift_type: str, shift_date: date) -> bool:
        return bool(
            _preferred_night_strength(name, shift_date)
            and shift_type in _preferred_night_shifts_for(name)
        )

    def _preferred_night_key(name: str, shift_type: str, shift_date: date) -> tuple[float, int, int]:
        strength = _preferred_night_strength(name, shift_date)
        if not strength or shift_type not in _preferred_night_shifts_for(name):
            return (0.0, 0, 0)

        weekend_bonus = 4.0 if shift_date.weekday() in (4, 5) else 0.0
        if strength >= 2:
            reward = -6.0 - weekend_bonus + min(5.5, important_preferred_night_hits[name] * 2.0 + preferred_night_hits[name] * 0.5)
        else:
            reward = -3.0 - (weekend_bonus / 2.0) + min(2.5, preferred_night_hits[name] * 1.0)

        if len(preferred_night_requesters_by_date.get(shift_date, set())) == 1:
            reward -= 1.0
        return (reward, -strength, preferred_night_hits[name])

    def _preferred_night_miss_objective() -> tuple[int, int, int, int]:
        missed_important = 0
        missed_weekend = 0
        missed_regular = 0
        for (name, pref_date), strength in preferred_night_requests.items():
            assigned = bool(
                daily_assignments.get(pref_date, {}).get(name, set()).intersection(
                    _preferred_night_shifts_for(name)
                )
            )
            if assigned:
                continue
            if pref_date.weekday() in (4, 5):
                missed_weekend += 1
            if strength >= 2:
                missed_important += 1
            else:
                missed_regular += 1
        weighted = missed_important * 8 + missed_weekend * 4 + missed_regular * 2
        return (missed_important, missed_weekend, missed_regular, weighted)

    def _record_preferred_night_assignment(name: str, shift_type: str, shift_date: date) -> None:
        if not _is_preferred_night_assignment(name, shift_type, shift_date):
            return
        key = (shift_date, shift_type, name)
        if key in preferred_night_assignment_keys:
            return
        preferred_night_assignment_keys.add(key)
        preferred_night_hits[name] += 1
        if _preferred_night_strength(name, shift_date) >= 2:
            important_preferred_night_hits[name] += 1

    def _preferred_night_removal_penalty(name: str, shift_type: str, shift_date: date) -> int:
        strength = _preferred_night_strength(name, shift_date)
        if not strength or shift_type not in _preferred_night_shifts_for(name):
            return 0
        return 100 if strength >= 2 else 30

    def _record_konen_mion_assignment(name: str, shift_date: date) -> None:
        konen_month_counts[name] += 1
        if shift_date.weekday() == 4:
            konen_friday_counts[name] += 1

    def _shimon_friday_due() -> bool:
        return not previous_shimon_friday

    def _shimon_friday_available(shift_date: date) -> bool:
        return (
            shift_date.weekday() == 4
            and _shimon_friday_due()
            and friday_day_counts[SHIMON_NAME] == 0
        )

    def _yoeatzim_allowed(name: str, shift_date: date) -> bool:
        if name != SHIMON_NAME:
            return True
        return _shimon_friday_available(shift_date)

    def _konen_mion_key(name: str, shift_date: date) -> tuple[float, ...]:
        if name == SHIMON_NAME:
            projected = konen_month_counts[name] + 1
            target_rank = 0 if projected <= SHIMON_KONEN_TARGET else 2
            friday_due = _shimon_friday_available(shift_date)
            is_friday = shift_date.weekday() == 4
            if is_friday and friday_due:
                friday_penalty = -2
            elif is_friday and not friday_due:
                friday_penalty = 4
            elif friday_due and projected >= SHIMON_KONEN_TARGET:
                friday_penalty = 2
            else:
                friday_penalty = 0
            return (
                target_rank,
                max(0, SHIMON_KONEN_TARGET - projected),
                *_preferred_night_key(name, KONEN_MION_SHIFT, shift_date),
                friday_penalty,
                konen_month_counts[name],
                _friday_work_key(name, KONEN_MION_SHIFT, shift_date)[1],
                0,
                fairness_score(name, KONEN_MION_SHIFT, shift_date, history, _last_night_before(shift_date)),
            )
        return (
            1,
            0,
            *_preferred_night_key(name, KONEN_MION_SHIFT, shift_date),
            0,
            konen_month_counts[name],
            _friday_work_key(name, KONEN_MION_SHIFT, shift_date)[1],
            1,
            fairness_score(name, KONEN_MION_SHIFT, shift_date, history, _last_night_before(shift_date)),
        )

    def _resident_night_extra_capacity(name: str) -> int:
        return RESIDENT_NIGHT_EXTRA_CAPACITY.get(name, 0)

    def _current_resident_night_count(name: str) -> int:
        return month_counts[name]

    def _rolling_resident_night_count(name: str) -> int:
        return max(0, previous_resident_night_counts[name] + month_counts[name] - _resident_night_extra_capacity(name))

    def _rolling_resident_weekend_count(name: str) -> int:
        return previous_resident_weekend_counts[name] + weekend_night_counts[name]

    def _previous_resident_night_baseline() -> int:
        pool = active_resident_night_names or set(previous_resident_night_counts)
        if not pool:
            return 0
        return min(previous_resident_night_counts[name] for name in pool)

    def _resident_type_compensation_key(name: str, shift_type: str) -> int:
        """
        Residents who carried a heavier previous month should, when otherwise
        fair, be compensated with fewer ת.מיון and relatively more ת.מיון 2.
        Lower is better.
        """
        previous_overload = max(
            0,
            previous_resident_night_counts[name] - _previous_resident_night_baseline(),
        )
        if not previous_overload or shift_type not in RESIDENT_NIGHT_SHIFTS:
            return 0
        current_delta = (
            resident_night_shift_counts["ת.מיון"][name]
            - resident_night_shift_counts["ת.מיון 2"][name]
        )
        projected_delta = current_delta + (1 if shift_type == "ת.מיון" else -1)
        return previous_overload * projected_delta

    def _resident_personal_night_penalty(name: str, shift_type: str, shift_date: date) -> tuple[int, int]:
        if name == ESLEY_NAME and shift_date.weekday() == 1:
            return (1, 0 if shift_type == "ת.מיון 2" else 1)
        return (0, 0)

    def _glinskaya_weekend_preference(name: str, shift_date: date) -> int:
        if name != GLINSKAYA_NAME:
            return 0
        current_weekend = weekend_night_counts[name]
        current_saturdays = saturday_night_counts[name]
        current_fridays = max(0, current_weekend - current_saturdays)
        if shift_date.weekday() == 5 and current_saturdays < 2 and current_weekend < 2:
            return -1
        if shift_date.weekday() == 4 and current_fridays < 1 and current_weekend < 2:
            return -1
        return 0

    def _rolling_resident_total_counts(pool: set[str]) -> Counter:
        return Counter({name: _rolling_resident_night_count(name) for name in pool})

    def _current_resident_total_counts(pool: set[str]) -> Counter:
        return Counter({name: _current_resident_night_count(name) for name in pool})

    def _rolling_resident_weekend_counts(pool: set[str]) -> Counter:
        return Counter({name: _rolling_resident_weekend_count(name) for name in pool})

    def _weekend_resident_night_key(name: str, shift_date: date) -> tuple[int, ...]:
        if shift_date.weekday() not in (4, 5):
            return (
                0,
                month_counts[name],
                -_resident_night_extra_capacity(name),
                _rolling_resident_night_count(name),
            )
        return (
            weekend_night_counts[name],
            _rolling_resident_weekend_count(name),
            _rolling_resident_night_count(name),
            _glinskaya_weekend_preference(name, shift_date),
        )

    def _resident_night_balance_key(name: str, shift_type: str, shift_date: date) -> tuple[int, ...]:
        if shift_type not in resident_night_shift_counts:
            return (0, 0, 0, 0)
        jitter_key = f"{month}|{shift_date.isoformat()}|{shift_type}|{name}".encode("utf-8")
        jitter = int.from_bytes(hashlib.blake2s(jitter_key, digest_size=2).digest(), "big")
        if shift_date.weekday() in (4, 5):
            return (
                _current_resident_night_count(name),
                -_resident_night_extra_capacity(name),
                weekend_night_counts[name],
                *_preferred_night_key(name, shift_type, shift_date),
                _rolling_resident_night_count(name),
                _rolling_resident_weekend_count(name),
                resident_night_shift_counts[shift_type][name],
                _resident_type_compensation_key(name, shift_type),
                _glinskaya_weekend_preference(name, shift_date),
                *_resident_personal_night_penalty(name, shift_type, shift_date),
                jitter,
            )
        return (
            _current_resident_night_count(name),
            -_resident_night_extra_capacity(name),
            *_preferred_night_key(name, shift_type, shift_date),
            _rolling_resident_night_count(name),
            resident_night_shift_counts[shift_type][name],
            weekend_night_counts[name],
            _rolling_resident_weekend_count(name),
            _resident_type_compensation_key(name, shift_type),
            *_resident_personal_night_penalty(name, shift_type, shift_date),
            jitter,
        )

    def _resident_night_names(d: date) -> set[str]:
        out: set[str] = set()
        for name, shifts in daily_assignments.get(d, {}).items():
            if shifts.intersection({"ת.מיון", "ת.מיון 2"}):
                out.add(name)
        return out

    def _rebuild_daily_assignments_from_roster() -> None:
        daily_assignments.clear()
        for idx, r in roster.iterrows():
            d = _row_date(idx)
            shift = str(r["Shift"])
            for name in _assigned_names(idx):
                daily_assignments[d].setdefault(name, set()).add(shift)

    def _last_night_before(target_date: date) -> Dict[str, date]:
        out: Dict[str, date] = {
            name: d
            for name, d in last_night.items()
            if d < target_date
        }
        for d, by_name in daily_assignments.items():
            if d >= target_date:
                continue
            for name, shifts in by_name.items():
                if shifts.intersection(NIGHT_DUTY_SHIFTS):
                    out[name] = max(out.get(name, date.min), d)
        return out

    def _is_day_shift(shift: str) -> bool:
        return shift in _el.DAY_SHIFTS or shift in {"רוטציה", "EEG ילדים"}

    def _shift_keep_rank(d: date, name: str, shift: str) -> tuple[int, int]:
        fixed_rank = 0 if (d, shift, name) in fixed_assignment_keys else 1
        if shift == "רוטציה":
            return (fixed_rank, 0)
        if name in clinic_owners.get((d, shift), set()):
            return (fixed_rank, 10)
        if shift == "אטנדינג":
            return (fixed_rank, 20)
        if shift == "מיון":
            return (fixed_rank, 30)
        if shift in _el.CLINIC_SHIFTS:
            return (fixed_rank, 40)
        if shift == "EEG ילדים":
            return (fixed_rank, 45)
        if shift == "כונן מיון":
            return (fixed_rank, 50)
        if shift == "ייעוצים מובילים":
            return (fixed_rank, 60)
        return (fixed_rank, 55)

    def _is_fixed_assignment(d: date, name: str, shift: str) -> bool:
        return (d, shift, name) in fixed_assignment_keys

    def _pair_allowed_for_cleanup(d: date, name: str, existing: str, shift: str) -> bool:
        if (existing, shift) in DUAL_OK:
            return True
        return _is_fixed_assignment(d, name, existing) and _is_fixed_assignment(d, name, shift)

    def _remove_from_roster(d: date, shift: str, name: str) -> None:
        mask = (roster["Date"] == d.isoformat()) & (roster["Shift"] == shift)
        if not mask.any():
            return
        idx = roster.index[mask][0]
        names = [n for n in _name_list(roster.at[idx, "Assigned"]) if n != name]
        roster.at[idx, "Assigned"] = _write_name_list(names)

    def _row_index(d: date, shift: str) -> int | None:
        return row_index_by_date_shift.get((d, shift))

    def _rotation_counts() -> Counter:
        return Counter(
            name
            for _, row in roster[roster["Shift"] == "רוטציה"].iterrows()
            if date.fromisoformat(str(row["Date"])).weekday() not in (4, 5)
            for name in _name_list(row["Assigned"])
        )

    def _rotation_pull_candidates(shift_type: str, shift_date: date, current: list[str]) -> list[str]:
        if shift_date.weekday() in (4, 5):
            return []
        if shift_type == "רוטציה" or not _is_day_shift(shift_type):
            return []

        rotation_idx = _row_index(shift_date, "רוטציה")
        if rotation_idx is None:
            return []
        rotated_today = set(_name_list(roster.at[rotation_idx, "Assigned"]))
        if not rotated_today:
            return []

        out: list[str] = []
        for name in sorted(full_month_rotation_names & rotated_today):
            if name in current:
                continue
            if not worker_shift_lut.get((name, shift_type), False):
                continue

            original_rotation = _name_list(roster.at[rotation_idx, "Assigned"])
            roster.at[rotation_idx, "Assigned"] = _write_name_list(
                [n for n in original_rotation if n != name]
            )
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            eligible = get_eligible_workers(
                shift_type=shift_type,
                shift_date=shift_date,
                blocked_next_day=blocked_next_day,
                extra_day_off=extra_day_off,
                daily_assignments=daily_assignments,
                blocked_reasons=None,
                last_night=_last_night_before(shift_date),
            )

            roster.at[rotation_idx, "Assigned"] = _write_name_list(original_rotation)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            if name in eligible:
                out.append(name)

        rotation_counts = _rotation_counts()
        return sorted(out, key=lambda name: (-rotation_counts[name], month_counts[name], name))

    def _pull_from_full_month_rotation(shift_type: str, shift_date: date, name: str) -> None:
        if name not in full_month_rotation_names:
            return
        _remove_from_roster(shift_date, "רוטציה", name)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()

    def _resolve_same_day_conflicts() -> int:
        _rebuild_daily_assignments_from_roster()
        removed = 0
        for d, by_name in list(daily_assignments.items()):
            for name, shifts in list(by_name.items()):
                shift_list = sorted(shifts, key=lambda s: _shift_keep_rank(d, name, s))
                has_rotation = "רוטציה" in shifts
                keep: list[str] = []
                for shift in shift_list:
                    if shift in keep:
                        continue
                    if has_rotation and shift != "רוטציה" and _is_day_shift(shift):
                        if name in full_month_rotation_names:
                            _remove_from_roster(d, "רוטציה", name)
                            if "רוטציה" in keep:
                                keep = [kept for kept in keep if kept != "רוטציה"]
                            keep.append(shift)
                            removed += 1
                            logger.info(
                                "Pulled full-month rotation for day shift: %s %s from %s",
                                d.isoformat(), shift, name,
                            )
                            continue
                        if _is_fixed_assignment(d, name, "רוטציה") and _is_fixed_assignment(d, name, shift):
                            keep.append(shift)
                            continue
                        _remove_from_roster(d, shift, name)
                        removed += 1
                        logger.warning(
                            "Removed day shift blocked by rotation: %s %s from %s",
                            d.isoformat(), shift, name,
                        )
                        continue
                    if not keep or all(_pair_allowed_for_cleanup(d, name, existing, shift) for existing in keep):
                        keep.append(shift)
                    else:
                        _remove_from_roster(d, shift, name)
                        removed += 1
                        logger.warning(
                            "Removed illegal same-day conflict: %s %s from %s (kept %s)",
                            d.isoformat(), shift, name, ", ".join(keep),
                        )
        if removed:
            _rebuild_daily_assignments_from_roster()
        return removed

    def _resolve_after_duty_conflicts() -> int:
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        removed = 0
        rest_only = {"אחרי תורנות", "חלופי", "חופש"}
        for name, rest_dates in list(blocked_next_day.items()):
            for d in sorted(rest_dates):
                for shift in sorted(daily_assignments.get(d, {}).get(name, set())):
                    if shift in rest_only:
                        continue
                    if (d, shift, name) in fixed_assignment_keys:
                        logger.warning(
                            "Fixed assignment conflicts with after-duty rest: %s %s %s",
                            d.isoformat(), shift, name,
                        )
                        continue
                    _remove_from_roster(d, shift, name)
                    removed += 1
                    logger.warning(
                        "Removed after-duty conflict: %s %s from %s",
                        d.isoformat(), shift, name,
                    )
        if removed:
            _rebuild_daily_assignments_from_roster()
            _rebuild_live_counters_from_roster()
        return removed

    def _hard_refill_shifts() -> list[str]:
        return ["מיון", "מחלקה", ATTENDING_SHIFT, "EEG", "EEG ילדים", YOEATZIM_SHIFT, KONEN_MION_SHIFT]

    def _try_free_worker_for_hard_row(
        target_idx: int,
        shift_type: str,
        shift_date: date,
        current: list[str],
    ) -> bool:
        target_rank = _shift_keep_rank(shift_date, "", shift_type)
        lower_priority_shifts = [
            shift
            for shift in _hard_refill_shifts()
            if shift != shift_type
            and _shift_keep_rank(shift_date, "", shift) > target_rank
        ]
        if not lower_priority_shifts:
            return False

        lower_rows = roster[
            (roster["Date"] == shift_date.isoformat())
            & (roster["Shift"].isin(lower_priority_shifts))
        ]

        for lower_idx, lower_row in lower_rows.iterrows():
            lower_shift = str(lower_row.Shift)
            lower_names = _name_list(lower_row.Assigned)
            if not lower_names:
                continue
            for candidate in sorted(lower_names, key=lambda n: fairness_score(n, shift_type, shift_date, history, _last_night_before(shift_date))):
                if (shift_date, lower_shift, candidate) in fixed_assignment_keys:
                    continue

                remaining_lower = [name for name in lower_names if name != candidate]
                original_lower = _name_list(roster.at[lower_idx, "Assigned"])
                original_target = _name_list(roster.at[target_idx, "Assigned"])

                roster.at[lower_idx, "Assigned"] = _write_name_list(remaining_lower)
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()

                target_eligible = get_eligible_workers(
                    shift_type=shift_type,
                    shift_date=shift_date,
                    blocked_next_day=blocked_next_day,
                    extra_day_off=extra_day_off,
                    daily_assignments=daily_assignments,
                    blocked_reasons=None,
                    last_night=_last_night_before(shift_date),
                )
                if shift_type == YOEATZIM_SHIFT:
                    target_eligible = [
                        name for name in target_eligible
                        if _yoeatzim_allowed(name, shift_date)
                    ]
                elif shift_type == "EEG":
                    target_eligible = [
                        name for name in target_eligible
                        if _eeg_under_cap(name, shift_date)
                    ]
                if candidate not in target_eligible or candidate in current:
                    roster.at[lower_idx, "Assigned"] = _write_name_list(original_lower)
                    roster.at[target_idx, "Assigned"] = _write_name_list(original_target)
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    continue

                lower_eligible = get_eligible_workers(
                    shift_type=lower_shift,
                    shift_date=shift_date,
                    blocked_next_day=blocked_next_day,
                    extra_day_off=extra_day_off,
                    daily_assignments=daily_assignments,
                    blocked_reasons=None,
                    last_night=_last_night_before(shift_date),
                )
                if lower_shift == YOEATZIM_SHIFT:
                    lower_eligible = [
                        name for name in lower_eligible
                        if _yoeatzim_allowed(name, shift_date)
                    ]
                elif lower_shift == "EEG":
                    lower_eligible = [
                        name for name in lower_eligible
                        if _eeg_under_cap(name, shift_date)
                    ]
                replacements = [
                    name for name in lower_eligible
                    if name != candidate
                    and name not in remaining_lower
                    and name not in current
                ]
                if not replacements:
                    roster.at[lower_idx, "Assigned"] = _write_name_list(original_lower)
                    roster.at[target_idx, "Assigned"] = _write_name_list(original_target)
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    continue

                replacement = min(
                    replacements,
                    key=lambda name: (
                        _shift_keep_rank(shift_date, name, lower_shift),
                        _personal_rule_key(name, lower_shift, shift_date),
                        _friday_work_key(name, lower_shift, shift_date),
                        _yoeatzim_key(name, shift_date) if lower_shift == YOEATZIM_SHIFT else (),
                        _eeg_key(name, shift_date) if lower_shift == "EEG" else (),
                        fairness_score(name, lower_shift, shift_date, history, _last_night_before(shift_date)),
                    ),
                )
                roster.at[lower_idx, "Assigned"] = _write_name_list(remaining_lower + [replacement])
                roster.at[target_idx, "Assigned"] = _write_name_list(current + [candidate])
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                logger.info(
                    "hard-row refill displaced lower-priority shift: %s %s <- %s; %s %s -> %s",
                    shift_date.isoformat(), shift_type, candidate,
                    lower_shift, candidate, replacement,
                )
                return True

        return False

    def _refill_hard_rows_after_cleanup() -> int:
        filled = 0
        refill_shifts = _hard_refill_shifts()
        for idx, r in roster[roster["Shift"].isin(refill_shifts)].sort_values(["Date", "Shift"]).iterrows():
            needed = int(r.Needed)
            current = _name_list(roster.at[idx, "Assigned"])
            missing = max(needed - len(current), 0)
            if missing <= 0:
                continue

            shift_type = str(r.Shift)
            shift_date = date.fromisoformat(str(r.Date))
            for _ in range(missing):
                elig = get_eligible_workers(
                    shift_type=shift_type,
                    shift_date=shift_date,
                    blocked_next_day=blocked_next_day,
                    extra_day_off=extra_day_off,
                    daily_assignments=daily_assignments,
                    blocked_reasons=blocked_reasons,
                    last_night=_last_night_before(shift_date),
                )
                elig = [w for w in elig if w not in current]
                rotation_elig = [
                    w for w in _rotation_pull_candidates(shift_type, shift_date, current)
                    if w not in elig
                ]
                if rotation_elig:
                    logger.info(
                        "full-month rotation candidates for hard row: %s %s -> %s",
                        shift_date.isoformat(), shift_type, ", ".join(rotation_elig),
                    )
                    elig.extend(rotation_elig)
                elig = [
                    w for w in elig
                    if _personal_under_max(w, shift_type, shift_date)
                ]
                if not elig:
                    if _try_free_worker_for_hard_row(idx, shift_type, shift_date, current):
                        filled += 1
                        current = _name_list(roster.at[idx, "Assigned"])
                        continue
                    break
                if shift_type == YOEATZIM_SHIFT:
                    elig = [w for w in elig if _yoeatzim_allowed(w, shift_date)]
                    if not elig:
                        break
                    key_fn = lambda w: (
                        _friday_work_key(w, shift_type, shift_date),
                        _yoeatzim_key(w, shift_date),
                    )
                elif shift_type == "EEG":
                    elig = [w for w in elig if _eeg_under_cap(w, shift_date)]
                    if not elig:
                        logger.info(
                            "EEG refill left empty on %s: all eligible workers are at their monthly cap",
                            shift_date.isoformat(),
                        )
                        break
                    key_fn = lambda w: (
                        0 if w == "גנדלמן" and _has_shift(daily_assignments, shift_date, w, "EEG ילדים") else 1,
                        _eeg_key(w, shift_date),
                    )
                elif shift_type in RESIDENT_NIGHT_SHIFTS:
                    key_fn = lambda w: (
                        _resident_night_balance_key(w, shift_type, shift_date),
                        _weekend_resident_night_key(w, shift_date),
                        _friday_work_key(w, shift_type, shift_date),
                        _alternate_risk_penalty(w, shift_type, shift_date, daily_assignments),
                        _friday_night_morning_penalty(w, shift_type, shift_date, daily_assignments),
                        _resident_adjacent_night_penalty(w, shift_date, daily_assignments),
                        _resident_sandwich_penalty(w, shift_date, daily_assignments),
                        _resident_night_spacing_penalty(w, shift_date, _last_night_before(shift_date)),
                        fairness_score(w, shift_type, shift_date, history, _last_night_before(shift_date)),
                    )
                elif shift_type == KONEN_MION_SHIFT:
                    if shift_date.weekday() == 4:
                        elig = [
                            w for w in elig
                            if w != SHIMON_NAME or _shimon_friday_available(shift_date)
                        ]
                        if not elig:
                            break
                    key_fn = lambda w: (
                        _konen_mion_key(w, shift_date),
                        _friday_work_key(w, shift_type, shift_date),
                    )
                elif shift_type == ATTENDING_SHIFT:
                    key_fn = lambda w: (
                        0 if shift_date.weekday() == 4 and _has_shift(daily_assignments, shift_date, w, KONEN_MION_SHIFT) else 1,
                        _personal_rule_key(w, shift_type, shift_date),
                        _friday_work_key(w, shift_type, shift_date),
                        fairness_score(w, shift_type, shift_date, history, _last_night_before(shift_date)),
                    )
                else:
                    key_fn = lambda w: (
                        0 if shift_type == "EEG" and w == "גנדלמן" and _has_shift(daily_assignments, shift_date, w, "EEG ילדים") else 1,
                        _personal_rule_key(w, shift_type, shift_date),
                        _friday_work_key(w, shift_type, shift_date),
                        fairness_score(w, shift_type, shift_date, history, _last_night_before(shift_date)),
                    )
                pick = min(
                    elig,
                    key=key_fn,
                )
                current.append(pick)
                _pull_from_full_month_rotation(shift_type, shift_date, pick)
                daily_assignments[shift_date].setdefault(pick, set()).add(shift_type)
                _record_friday_assignment(pick, shift_type, shift_date)
                if shift_type == YOEATZIM_SHIFT:
                    _record_yoeatzim_assignment(pick, shift_date)
                elif shift_type == ATTENDING_SHIFT:
                    attending_counts[pick] += 1
                elif shift_type == KONEN_MION_SHIFT:
                    _record_konen_mion_assignment(pick, shift_date)
                elif shift_type == "EEG":
                    eeg_counts[pick] += 1
                if shift_type in ("ת.מיון", "ת.מיון 2"):
                    month_counts[pick] += 1
                    resident_night_shift_counts[shift_type][pick] += 1
                    if shift_date.weekday() in (4, 5):
                        weekend_night_counts[pick] += 1
                    if shift_date.weekday() == 5:
                        saturday_night_counts[pick] += 1
                if shift_type in NIGHT_DUTY_SHIFTS:
                    _record_preferred_night_assignment(pick, shift_type, shift_date)
                history[pick][shift_type] += 1
                personal_assignment_counts[(pick, shift_type)] += 1
                filled += 1
            if len(current) < needed:
                warn = f"⚠️, {len(current)}/{needed}"
                roster.at[idx, "Assigned"] = (
                    f"{warn}, " + ", ".join(current) if current
                    else f"{warn}, Needs manual pick"
                )
            else:
                roster.at[idx, "Assigned"] = _write_name_list(current)
        return filled

    def _personal_rule_rows(rule: dict[str, object]) -> pd.DataFrame:
        shift = str(rule.get("shift") or "")
        rows = roster[roster["Shift"] == shift].copy()
        if rows.empty:
            return rows
        weekday = rule.get("weekday")
        if weekday is not None:
            rows = rows[
                rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday() == weekday)
            ]
        return rows.sort_values("Date")

    def _mark_personal_rule_missing(rule: dict[str, object]) -> bool:
        rows = _personal_rule_rows(rule)
        if rows.empty:
            return False
        idx = int(rows.index[0])
        name = str(rule.get("name") or "")
        marker = "כלל אישי חסר"
        assigned = str(roster.at[idx, "Assigned"] or "").strip()
        if marker in assigned:
            return True
        roster.at[idx, "Assigned"] = f"{assigned}, {marker}" if assigned and assigned != "-" else marker
        logger.warning(
            "personal mandatory rule missing: %s %s",
            str(rule.get("shift") or ""),
            name,
        )
        return True

    def _try_place_personal_shift_rule(rule: dict[str, object]) -> bool:
        name = str(rule.get("name") or "")
        shift = str(rule.get("shift") or "")
        if not name or not shift:
            return False
        if not _personal_under_max(name, shift, first_day):
            return False

        for idx, row in _personal_rule_rows(rule).iterrows():
            shift_date = date.fromisoformat(str(row.Date))
            if not _personal_under_max(name, shift, shift_date):
                return False
            current = _name_list(roster.at[idx, "Assigned"])
            if name in current:
                return True

            needed = _to_int(roster.at[idx, "Needed"], 0)
            soft = _to_int(roster.at[idx, "SoftCap"], needed)
            original = _name_list(roster.at[idx, "Assigned"])
            candidate_slots: list[list[str]] = []
            if len(current) < max(needed, soft):
                candidate_slots.append(current)
            for victim in current:
                if (shift_date, shift, victim) in fixed_assignment_keys:
                    continue
                candidate_slots.append([worker for worker in current if worker != victim])

            for base_current in candidate_slots:
                roster.at[idx, "Assigned"] = _write_name_list(base_current)
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                eligible = get_eligible_workers(
                    shift_type=shift,
                    shift_date=shift_date,
                    blocked_next_day=blocked_next_day,
                    extra_day_off=extra_day_off,
                    daily_assignments=daily_assignments,
                    blocked_reasons=None,
                    last_night=_last_night_before(shift_date),
                )
                if (
                    name in eligible
                    and name not in base_current
                    and _personal_under_max(name, shift, shift_date)
                ):
                    roster.at[idx, "Assigned"] = _write_name_list(base_current + [name])
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    logger.info(
                        "personal mandatory rule placed: %s %s on %s",
                        shift, name, shift_date.isoformat(),
                    )
                    return True

                roster.at[idx, "Assigned"] = _write_name_list(original)
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
        return False

    def _apply_mandatory_personal_rules() -> int:
        changed = 0
        for rule in personal_rules:
            if rule.get("staffing") != "משמרת" or rule.get("condition") != "חובה":
                continue
            name = str(rule.get("name") or "")
            shift = str(rule.get("shift") or "")
            minimum = int(rule.get("min") or 0)
            while minimum > 0 and personal_assignment_counts[(name, shift)] < minimum:
                before = personal_assignment_counts[(name, shift)]
                if not _try_place_personal_shift_rule(rule):
                    _mark_personal_rule_missing(rule)
                    break
                if personal_assignment_counts[(name, shift)] <= before:
                    break
                changed += 1
        return changed

    def _apply_companion_personal_rules() -> int:
        changed = 0
        for rule in personal_rules:
            if rule.get("staffing") != "נלווה":
                continue
            name = str(rule.get("name") or "")
            shift = str(rule.get("shift") or "")
            maximum = rule.get("max")
            minimum = int(rule.get("min") or 0)
            target = int(maximum) if maximum is not None else minimum
            if target <= 0:
                continue
            for idx, row in _personal_rule_rows(rule).iterrows():
                if personal_assignment_counts[(name, shift)] >= target:
                    break
                shift_date = date.fromisoformat(str(row.Date))
                current = _name_list(roster.at[idx, "Assigned"])
                if not current or name in current:
                    continue
                if not _personal_under_max(name, shift, shift_date):
                    break
                eligible = get_eligible_workers(
                    shift_type=shift,
                    shift_date=shift_date,
                    blocked_next_day=blocked_next_day,
                    extra_day_off=extra_day_off,
                    daily_assignments=daily_assignments,
                    blocked_reasons=None,
                    last_night=_last_night_before(shift_date),
                )
                if name not in eligible:
                    continue
                roster.at[idx, "Assigned"] = _write_name_list(current + [name])
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                logger.info(
                    "personal companion rule added: %s %s on %s",
                    shift, name, shift_date.isoformat(),
                )
                changed += 1
            if rule.get("condition") == "חובה" and personal_assignment_counts[(name, shift)] < minimum:
                _mark_personal_rule_missing(rule)
        return changed

    def _find_resident_sandwiches() -> list[tuple[str, date, date]]:
        by_name: dict[str, set[date]] = defaultdict(set)
        for d, by_worker in daily_assignments.items():
            for name, shifts in by_worker.items():
                if shifts.intersection(RESIDENT_NIGHT_SHIFTS):
                    by_name[name].add(d)

        sandwiches: list[tuple[str, date, date]] = []
        for name, dates in by_name.items():
            for d in sorted(dates):
                later = d + timedelta(days=2)
                middle = d + timedelta(days=1)
                if later in dates and middle not in dates:
                    sandwiches.append((name, d, later))
        return sandwiches

    def _resident_night_row_index(d: date, name: str) -> int | None:
        for idx in resident_night_row_indexes:
            if _row_date(idx) != d:
                continue
            if name in _assigned_names(idx):
                return idx
        return None

    def _try_replace_resident_night(
        idx: int,
        old_name: str,
        preferred_names: set[str] | None = None,
        reason: str = "resident night repair",
        allow_sandwich: bool = False,
        protect_total_objective: tuple[int, int] | None = None,
        protect_weekend_objective: tuple[int, int] | None = None,
        protect_weekend_history: int | None = None,
    ) -> bool:
        row = roster.loc[idx]
        shift_type = str(row.Shift)
        shift_date = date.fromisoformat(str(row.Date))
        if (shift_date, shift_type, old_name) in fixed_assignment_keys:
            return False

        original = _name_list(row.Assigned)
        current = [name for name in original if name != old_name]
        roster.at[idx, "Assigned"] = _write_name_list(current)
        _rebuild_daily_assignments_from_roster()
        _rebuild_live_counters_from_roster()
        effective_last_night = _last_night_before(shift_date)

        elig = [
            w for w in all_worker_names
            if _can_worker_take_shift(
                w,
                shift_type,
                shift_date,
                last_night_map=effective_last_night,
            )
            and w not in current
            and w != old_name
        ]
        non_sandwich = [
            w for w in elig
            if _resident_adjacent_night_penalty(w, shift_date, daily_assignments) == 0
            and _resident_night_spacing_penalty(w, shift_date, effective_last_night) < 100
            and (allow_sandwich or _resident_sandwich_penalty(w, shift_date, daily_assignments) == 0)
        ]

        if preferred_names:
            preferred = [w for w in non_sandwich if w in preferred_names]
            if preferred:
                non_sandwich = preferred

        if not non_sandwich:
            roster.at[idx, "Assigned"] = _write_name_list(original)
            _rebuild_daily_assignments_from_roster()
            _rebuild_live_counters_from_roster()
            return False

        pick = min(
            non_sandwich,
            key=lambda w: (
                0 if preferred_names and w in preferred_names else 1,
                _resident_night_balance_key(w, shift_type, shift_date),
                _weekend_resident_night_key(w, shift_date),
                _friday_work_key(w, shift_type, shift_date),
                _alternate_risk_penalty(w, shift_type, shift_date, daily_assignments),
                _friday_night_morning_penalty(w, shift_type, shift_date, daily_assignments),
                _resident_adjacent_night_penalty(w, shift_date, daily_assignments),
                fairness_score(w, shift_type, shift_date, history, effective_last_night),
            ),
        )
        current.append(pick)
        roster.at[idx, "Assigned"] = _write_name_list(current)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()
        if (
            protect_total_objective is not None
            and _resident_night_total_objective() > protect_total_objective
        ):
            roster.at[idx, "Assigned"] = _write_name_list(original)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            return False
        if (
            protect_weekend_objective is not None
            and _resident_weekend_objective() > protect_weekend_objective
        ):
            roster.at[idx, "Assigned"] = _write_name_list(original)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            return False
        if (
            protect_weekend_history is not None
            and _resident_weekend_history_load() > protect_weekend_history
        ):
            roster.at[idx, "Assigned"] = _write_name_list(original)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            return False
        logger.info(
            "%s: %s %s %s -> %s",
            reason, shift_date.isoformat(), shift_type, old_name, pick,
        )
        return True

    def _repair_resident_sandwiches() -> int:
        label = "resident_sandwiches"
        if _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(10):
            sandwiches = _find_resident_sandwiches()
            if not sandwiches:
                _remember_repair_noop(label)
                break

            changed = False
            for name, first, second in sandwiches:
                for candidate_date in (second, first):
                    idx = _resident_night_row_index(candidate_date, name)
                    if idx is not None and _try_replace_resident_night(
                        idx,
                        name,
                        reason="resident night sandwich repair",
                        protect_total_objective=_resident_night_total_objective(),
                        protect_weekend_objective=_resident_weekend_objective(),
                        protect_weekend_history=_resident_weekend_history_load(),
                    ):
                        repaired += 1
                        changed = True
                        _forget_repair_noops()
                        break
                if changed:
                    break

            if not changed:
                _remember_repair_noop(label)
                break
        return repaired

    def _resident_night_pool() -> set[str]:
        assigned_names = set(month_counts) | set(weekend_night_counts)
        for counter in resident_night_shift_counts.values():
            assigned_names.update(counter)
        return active_resident_night_names | assigned_names

    def _resident_night_total_counts() -> Counter:
        pool = _resident_night_pool()
        return _current_resident_total_counts(pool)

    def _resident_night_total_objective() -> tuple[int, int]:
        pool = _resident_night_pool()
        return _count_spread_and_square(_current_resident_total_counts(pool), pool)

    def _resident_weekend_objective() -> tuple[int, int]:
        pool = _resident_night_pool()
        return _count_spread_and_square(
            Counter({name: weekend_night_counts[name] for name in pool}),
            pool,
        )

    def _resident_weekend_history_load(pool: set[str] | None = None) -> int:
        active_pool = pool or _resident_night_pool()
        return sum(
            weekend_night_counts[name]
            * (
                previous_resident_weekend_counts[name] * 3
                + previous_resident_night_counts[name]
            )
            for name in active_pool
        )

    def _resident_rolling_total_objective() -> tuple[int, int]:
        pool = _resident_night_pool()
        return _count_spread_and_square(_rolling_resident_total_counts(pool), pool)

    def _count_spread_and_square(counts: Counter, pool: set[str]) -> tuple[int, int]:
        if not pool:
            return (0, 0)
        values = [counts[name] for name in pool]
        return (max(values) - min(values), sum(v * v for v in values))

    def _resident_night_shift_balance_key(pool: set[str]) -> tuple[int, int, int, int, int, int]:
        shift_counters = list(resident_night_shift_counts.values())
        if not pool or len(shift_counters) < 2:
            return (0, 0, 0, 0, 0, 0)
        t1, t2 = shift_counters[:2]
        diffs = [abs(t1[name] - t2[name]) for name in pool]
        t1_spread, t1_square = _count_spread_and_square(t1, pool)
        t2_spread, t2_square = _count_spread_and_square(t2, pool)
        return (
            max(diffs),
            sum(diffs),
            t1_spread,
            t2_spread,
            t1_square,
            t2_square,
        )

    def _resident_night_personal_preference_total() -> int:
        penalty = 0
        for _, row in roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].iterrows():
            shift_date = date.fromisoformat(str(row["Date"]))
            shift_type = str(row["Shift"])
            for name in _name_list(row["Assigned"]):
                if name == ESLEY_NAME and shift_date.weekday() == 1:
                    penalty += 1 if shift_type == "ת.מיון 2" else 2
        return penalty

    def _resident_type_compensation_total() -> int:
        baseline = _previous_resident_night_baseline()
        penalty = 0
        for name in _resident_night_pool():
            previous_overload = max(0, previous_resident_night_counts[name] - baseline)
            if not previous_overload:
                continue
            delta = (
                resident_night_shift_counts["ת.מיון"][name]
                - resident_night_shift_counts["ת.מיון 2"][name]
            )
            penalty += previous_overload * delta
        return penalty

    def _resident_night_objective() -> tuple[int, ...]:
        _rebuild_daily_assignments_from_roster()
        _rebuild_live_counters_from_roster()
        pool = _resident_night_pool()
        current_total_counts = _current_resident_total_counts(pool)
        current_weekend_counts = Counter({name: weekend_night_counts[name] for name in pool})
        rolling_total_counts = _rolling_resident_total_counts(pool)
        rolling_weekend_counts = _rolling_resident_weekend_counts(pool)
        total_spread, total_square = _count_spread_and_square(current_total_counts, pool)
        weekend_spread, weekend_square = _count_spread_and_square(current_weekend_counts, pool)
        rolling_total_spread, rolling_total_square = _count_spread_and_square(rolling_total_counts, pool)
        rolling_weekend_spread, rolling_weekend_square = _count_spread_and_square(rolling_weekend_counts, pool)
        shift_balance = _resident_night_shift_balance_key(pool)
        thursday_balance = _count_spread_and_square(thursday_night_counts, pool)
        return (
            total_spread,
            total_square,
            weekend_spread,
            weekend_square,
            len(_find_resident_sandwiches()),
            *_preferred_night_miss_objective(),
            *shift_balance,
            rolling_total_spread,
            rolling_total_square,
            rolling_weekend_spread,
            rolling_weekend_square,
            _resident_type_compensation_total(),
            _resident_night_personal_preference_total(),
            *thursday_balance,
        )

    def _resident_hard_objective(objective: tuple[int, ...]) -> tuple[int, ...]:
        return objective[:5]

    def _resident_preference_objective(objective: tuple[int, ...]) -> tuple[int, ...]:
        return objective[5:9]

    def _resident_shift_type_objective(objective: tuple[int, ...]) -> tuple[int, ...]:
        return objective[9:15]

    def _mark_missing_required_rows() -> int:
        marked = 0
        for idx, row in roster.iterrows():
            needed = _to_int(row.Needed, 0)
            if needed <= 0:
                continue
            current = _name_list(roster.at[idx, "Assigned"])
            if len(current) >= needed:
                continue
            warn = f"⚠️, {len(current)}/{needed}"
            roster.at[idx, "Assigned"] = (
                f"{warn}, {', '.join(current)}" if current
                else f"{warn}, Needs manual pick"
            )
            marked += 1
        return marked

    def _legal_resident_replacement_candidates(
        idx: int,
        old_name: str,
    ) -> list[str]:
        row = roster.loc[idx]
        shift_type = str(row.Shift)
        shift_date = date.fromisoformat(str(row.Date))
        original = _name_list(row.Assigned)
        current = [name for name in original if name != old_name]

        roster.at[idx, "Assigned"] = _write_name_list(current)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()

        effective_last_night = _last_night_before(shift_date)
        candidates = [
            w for w in all_worker_names
            if _can_worker_take_shift(
                w,
                shift_type,
                shift_date,
                last_night_map=effective_last_night,
            )
            if w not in current
            and w != old_name
            and _resident_adjacent_night_penalty(w, shift_date, daily_assignments) == 0
            and _resident_night_spacing_penalty(w, shift_date, effective_last_night) < 100
        ]

        roster.at[idx, "Assigned"] = _write_name_list(original)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()
        return candidates

    def _try_best_resident_night_improvement() -> bool:
        current_objective = _resident_night_objective()
        pool = _resident_night_pool()
        if not pool:
            return False

        rolling_total_counts = _rolling_resident_total_counts(pool)
        rolling_weekend_counts = _rolling_resident_weekend_counts(pool)
        current_total_counts = _current_resident_total_counts(pool)
        total_values = [current_total_counts[name] for name in pool]
        weekend_values = [weekend_night_counts[name] for name in pool]
        max_total, min_total = max(total_values), min(total_values)
        max_weekend, min_weekend = max(weekend_values), min(weekend_values)

        if max_total - min_total > 1:
            old_pool = {name for name in pool if current_total_counts[name] == max_total}
            candidate_pool = {name for name in pool if current_total_counts[name] <= max_total - 2}
            weekend_only = False
            run_direct_replacement = True
        elif max_weekend - min_weekend > 1:
            old_pool = {name for name in pool if weekend_night_counts[name] == max_weekend}
            candidate_pool = {name for name in pool if weekend_night_counts[name] <= max_weekend - 2}
            weekend_only = True
            run_direct_replacement = False
        else:
            sandwich_names = {name for name, _, _ in _find_resident_sandwiches()}
            if sandwich_names:
                old_pool = sandwich_names
                candidate_pool = pool - sandwich_names
                run_direct_replacement = True
            else:
                old_pool = pool
                candidate_pool = pool
                run_direct_replacement = False
            weekend_only = False

        evaluated = 0
        class _SearchLimitReached(Exception):
            pass

        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        rows["_weekday"] = rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
        rows["_weekend"] = rows["_weekday"].map(lambda wd: 1 if wd in (4, 5) else 0)

        if run_direct_replacement:
            try:
                for idx, row in rows.sort_values(["_weekend", "_weekday", "Date"], ascending=[False, False, False]).iterrows():
                    shift_date = date.fromisoformat(str(row.Date))
                    shift_type = str(row.Shift)
                    old_names = sorted(
                        _name_list(row.Assigned),
                        key=lambda name: (
                            -current_total_counts[name],
                            -month_counts[name],
                            -weekend_night_counts[name],
                            -rolling_total_counts[name],
                            -rolling_weekend_counts[name],
                            -resident_night_shift_counts[shift_type][name],
                            _preferred_night_removal_penalty(name, shift_type, shift_date),
                            name,
                        ),
                    )
                    for old_name in old_names:
                        if old_name not in old_pool:
                            continue
                        if (shift_date, shift_type, old_name) in fixed_assignment_keys:
                            continue
                        original = _name_list(roster.at[idx, "Assigned"])
                        current = [name for name in original if name != old_name]
                        candidates = sorted(
                            [
                                name for name in _legal_resident_replacement_candidates(idx, old_name)
                                if name in candidate_pool
                            ],
                            key=lambda name: (
                                current_total_counts[name],
                                month_counts[name],
                                weekend_night_counts[name],
                                rolling_total_counts[name],
                                rolling_weekend_counts[name],
                                resident_night_shift_counts[shift_type][name],
                                _resident_type_compensation_key(name, shift_type),
                                _resident_personal_night_penalty(name, shift_type, shift_date),
                                name,
                            ),
                        )
                        for candidate in candidates:
                            evaluated += 1
                            if evaluated > 300:
                                raise _SearchLimitReached
                            roster.at[idx, "Assigned"] = _write_name_list(current + [candidate])
                            candidate_objective = _resident_night_objective()
                            if candidate_objective < current_objective:
                                _rebuild_daily_assignments_from_roster()
                                _rebuild_night_state_from_roster()
                                _rebuild_live_counters_from_roster()
                                logger.info(
                                    "resident night objective repair: %s %s %s -> %s objective %s -> %s",
                                    row.Date, row.Shift, old_name, candidate, current_objective, candidate_objective,
                                )
                                return True
                            roster.at[idx, "Assigned"] = _write_name_list(original)
                            _rebuild_daily_assignments_from_roster()
                            _rebuild_night_state_from_roster()
                            _rebuild_live_counters_from_roster()
            except _SearchLimitReached:
                logger.debug("resident night direct replacement search reached evaluation limit")

        if max_weekend - min_weekend > 1:
            weekend_rows = rows[rows["_weekend"] == 1]
            weekday_rows = rows[rows["_weekend"] == 0]
            for a_idx, a_row in weekend_rows.sort_values(["Date"], ascending=[False]).iterrows():
                a_date = date.fromisoformat(str(a_row.Date))
                a_shift = str(a_row.Shift)
                for high_name in _name_list(a_row.Assigned):
                    if high_name not in old_pool:
                        continue
                    if (a_date, a_shift, high_name) in fixed_assignment_keys:
                        continue

                    for b_idx, b_row in weekday_rows.sort_values(["Date"]).iterrows():
                        b_date = date.fromisoformat(str(b_row.Date))
                        b_shift = str(b_row.Shift)
                        for low_name in _name_list(b_row.Assigned):
                            if low_name not in candidate_pool:
                                continue
                            if (b_date, b_shift, low_name) in fixed_assignment_keys:
                                continue

                            original_a = _name_list(roster.at[a_idx, "Assigned"])
                            original_b = _name_list(roster.at[b_idx, "Assigned"])
                            current_a = [name for name in original_a if name != high_name]
                            current_b = [name for name in original_b if name != low_name]

                            roster.at[a_idx, "Assigned"] = _write_name_list(current_a)
                            roster.at[b_idx, "Assigned"] = _write_name_list(current_b)
                            _rebuild_daily_assignments_from_roster()
                            _rebuild_night_state_from_roster()
                            _rebuild_live_counters_from_roster()

                            a_last_night = _last_night_before(a_date)
                            b_last_night = _last_night_before(b_date)
                            low_can_take_weekend = (
                                _can_worker_take_shift(
                                    low_name,
                                    a_shift,
                                    a_date,
                                    last_night_map=a_last_night,
                                )
                                and _resident_adjacent_night_penalty(low_name, a_date, daily_assignments) == 0
                            )
                            high_can_take_weekday = (
                                _can_worker_take_shift(
                                    high_name,
                                    b_shift,
                                    b_date,
                                    last_night_map=b_last_night,
                                )
                                and _resident_adjacent_night_penalty(high_name, b_date, daily_assignments) == 0
                            )

                            if low_can_take_weekend and high_can_take_weekday:
                                roster.at[a_idx, "Assigned"] = _write_name_list(current_a + [low_name])
                                roster.at[b_idx, "Assigned"] = _write_name_list(current_b + [high_name])
                                candidate_objective = _resident_night_objective()
                                if candidate_objective < current_objective:
                                    _rebuild_daily_assignments_from_roster()
                                    _rebuild_night_state_from_roster()
                                    _rebuild_live_counters_from_roster()
                                    logger.info(
                                        "resident night weekend swap: %s %s %s <-> %s %s %s objective %s -> %s",
                                        a_row.Date, a_shift, high_name,
                                        b_row.Date, b_shift, low_name,
                                        current_objective, candidate_objective,
                                    )
                                    return True

                            roster.at[a_idx, "Assigned"] = _write_name_list(original_a)
                            roster.at[b_idx, "Assigned"] = _write_name_list(original_b)
                            _rebuild_daily_assignments_from_roster()
                            _rebuild_night_state_from_roster()
                            _rebuild_live_counters_from_roster()

        return False

    def _try_resident_night_type_swap() -> bool:
        current_objective = _resident_night_objective()
        current_weekend_history = _resident_weekend_history_load()
        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        if rows.empty:
            return False

        rows_by_date_shift: dict[tuple[str, str], int] = {}
        for idx, row in rows.iterrows():
            rows_by_date_shift[(str(row.Date), str(row.Shift))] = idx

        swap_candidates: list[tuple[int, int, str, int, int, str, str]] = []
        for d_iso in sorted({str(row.Date) for _, row in rows.iterrows()}, reverse=True):
            idx_a = rows_by_date_shift.get((d_iso, "ת.מיון"))
            idx_b = rows_by_date_shift.get((d_iso, "ת.מיון 2"))
            if idx_a is None or idx_b is None:
                continue

            shift_date = date.fromisoformat(d_iso)
            original_a = _name_list(roster.at[idx_a, "Assigned"])
            original_b = _name_list(roster.at[idx_b, "Assigned"])
            for name_a in sorted(
                original_a,
                key=lambda n: (
                    _resident_type_compensation_key(n, "ת.מיון 2"),
                    -(resident_night_shift_counts["ת.מיון"][n] - resident_night_shift_counts["ת.מיון 2"][n]),
                    n,
                ),
            ):
                if (shift_date, "ת.מיון", name_a) in fixed_assignment_keys:
                    continue
                if resident_night_shift_counts["ת.מיון"][name_a] <= resident_night_shift_counts["ת.מיון 2"][name_a]:
                    continue

                for name_b in sorted(
                    original_b,
                    key=lambda n: (
                        _resident_type_compensation_key(n, "ת.מיון"),
                        -(resident_night_shift_counts["ת.מיון 2"][n] - resident_night_shift_counts["ת.מיון"][n]),
                        n,
                    ),
                ):
                    if name_a == name_b:
                        continue
                    if (shift_date, "ת.מיון 2", name_b) in fixed_assignment_keys:
                        continue
                    if resident_night_shift_counts["ת.מיון 2"][name_b] <= resident_night_shift_counts["ת.מיון"][name_b]:
                        continue
                    before = (
                        abs(resident_night_shift_counts["ת.מיון"][name_a] - resident_night_shift_counts["ת.מיון 2"][name_a])
                        + abs(resident_night_shift_counts["ת.מיון"][name_b] - resident_night_shift_counts["ת.מיון 2"][name_b])
                        + _resident_type_compensation_key(name_a, "ת.מיון")
                        + _resident_type_compensation_key(name_b, "ת.מיון 2")
                    )
                    after = (
                        abs((resident_night_shift_counts["ת.מיון"][name_a] - 1) - (resident_night_shift_counts["ת.מיון 2"][name_a] + 1))
                        + abs((resident_night_shift_counts["ת.מיון"][name_b] + 1) - (resident_night_shift_counts["ת.מיון 2"][name_b] - 1))
                        + _resident_type_compensation_key(name_a, "ת.מיון 2")
                        + _resident_type_compensation_key(name_b, "ת.מיון")
                    )
                    improvement = before - after
                    if improvement <= 0:
                        continue
                    swap_candidates.append((improvement, before, d_iso, idx_a, idx_b, name_a, name_b))

        for _, _, d_iso, idx_a, idx_b, name_a, name_b in sorted(
            swap_candidates,
            key=lambda item: (-item[0], -item[1], item[2]),
        ):
            shift_date = date.fromisoformat(d_iso)
            original_a = _name_list(roster.at[idx_a, "Assigned"])
            original_b = _name_list(roster.at[idx_b, "Assigned"])
            current_a = [name for name in original_a if name != name_a]
            current_b = [name for name in original_b if name != name_b]
            roster.at[idx_a, "Assigned"] = _write_name_list(current_a)
            roster.at[idx_b, "Assigned"] = _write_name_list(current_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            effective_last_night = _last_night_before(shift_date)
            a_can_take_b = (
                _can_worker_take_shift(
                    name_a,
                    "ת.מיון 2",
                    shift_date,
                    last_night_map=effective_last_night,
                )
                and _resident_adjacent_night_penalty(name_a, shift_date, daily_assignments) == 0
                and _resident_night_spacing_penalty(name_a, shift_date, effective_last_night) < 100
            )
            b_can_take_a = (
                _can_worker_take_shift(
                    name_b,
                    "ת.מיון",
                    shift_date,
                    last_night_map=effective_last_night,
                )
                and _resident_adjacent_night_penalty(name_b, shift_date, daily_assignments) == 0
                and _resident_night_spacing_penalty(name_b, shift_date, effective_last_night) < 100
            )

            if a_can_take_b and b_can_take_a:
                roster.at[idx_a, "Assigned"] = _write_name_list(current_a + [name_b])
                roster.at[idx_b, "Assigned"] = _write_name_list(current_b + [name_a])
                candidate_objective = _resident_night_objective()
                if (
                    _resident_hard_objective(candidate_objective) <= _resident_hard_objective(current_objective)
                    and _resident_preference_objective(candidate_objective) <= _resident_preference_objective(current_objective)
                    and _resident_shift_type_objective(candidate_objective) < _resident_shift_type_objective(current_objective)
                    and _resident_weekend_history_load() <= current_weekend_history
                ):
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    logger.info(
                        "resident night type swap: %s ת.מיון %s <-> ת.מיון 2 %s objective %s -> %s",
                        d_iso, name_a, name_b, current_objective, candidate_objective,
                    )
                    return True

            roster.at[idx_a, "Assigned"] = _write_name_list(original_a)
            roster.at[idx_b, "Assigned"] = _write_name_list(original_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

        return False

    def _try_resident_weekend_swap() -> bool:
        pool = {
            name
            for name in _resident_night_pool()
            if month_counts[name] > 0
        }
        if not pool:
            return False
        current_objective = _resident_night_objective()
        current_total = _resident_night_total_objective()
        current_sandwiches = len(_find_resident_sandwiches())
        rolling_weekend_counts = _rolling_resident_weekend_counts(pool)
        current_weekend_counts = Counter({name: weekend_night_counts[name] for name in pool})
        current_total_counts = _current_resident_total_counts(pool)
        current_weekend = _count_spread_and_square(current_weekend_counts, pool)
        rolling_weekend = _count_spread_and_square(rolling_weekend_counts, pool)
        current_weekend_history = _resident_weekend_history_load(pool)
        weekend_values = [weekend_night_counts[name] for name in pool]
        min_weekend = min(weekend_values)
        current_weekend_gap = max(weekend_values) - min_weekend
        rolling_weekend_values = [rolling_weekend_counts[name] for name in pool]
        min_rolling_weekend = min(rolling_weekend_values)
        rolling_weekend_gap = max(rolling_weekend_values) - min_rolling_weekend
        if current_weekend_gap == 0:
            return False

        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        rows["_weekday"] = rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
        weekend_rows = rows[rows["_weekday"].isin([4, 5])]
        weekday_rows = rows[~rows["_weekday"].isin([4, 5])]
        direct_candidates: list[tuple[int, int, int, int, str, int, str, str]] = []
        candidates: list[tuple[int, int, int, int, int, str, int, int, str, str]] = []

        for a_idx, a_row in weekend_rows.iterrows():
            a_date = date.fromisoformat(str(a_row.Date))
            a_shift = str(a_row.Shift)
            for high_name in _name_list(a_row.Assigned):
                if current_weekend_gap > 1 and weekend_night_counts[high_name] <= min_weekend + 1:
                    continue
                if current_weekend_gap <= 1 and weekend_night_counts[high_name] <= min_weekend:
                    continue
                if (a_date, a_shift, high_name) in fixed_assignment_keys:
                    continue
                for low_name in sorted(pool):
                    if low_name == high_name:
                        continue
                    if weekend_night_counts[low_name] != min_weekend:
                        continue
                    if low_name in _name_list(a_row.Assigned):
                        continue
                    if current_total_counts[high_name] < current_total_counts[low_name]:
                        continue
                    if eligibility_reason(low_name, a_date.isoformat(), a_shift) is not None:
                        continue
                    direct_candidates.append((
                        weekend_night_counts[high_name] - weekend_night_counts[low_name],
                        _preferred_night_removal_penalty(high_name, a_shift, a_date),
                        current_total_counts[high_name] - current_total_counts[low_name],
                        _rolling_resident_night_count(low_name),
                        a_date.isoformat(),
                        a_idx,
                        high_name,
                        low_name,
                    ))
                for b_idx, b_row in weekday_rows.iterrows():
                    b_date = date.fromisoformat(str(b_row.Date))
                    b_shift = str(b_row.Shift)
                    for low_name in _name_list(b_row.Assigned):
                        if low_name == high_name:
                            continue
                        if weekend_night_counts[low_name] != min_weekend:
                            continue
                        if current_weekend_gap > 1 and weekend_night_counts[low_name] >= weekend_night_counts[high_name] - 1:
                            continue
                        if current_weekend_gap <= 1:
                            history_advantage = (
                                previous_resident_weekend_counts[high_name]
                                - previous_resident_weekend_counts[low_name]
                            ) * 3 + (
                                previous_resident_night_counts[high_name]
                                - previous_resident_night_counts[low_name]
                            )
                            if history_advantage <= 0:
                                continue
                        if (b_date, b_shift, low_name) in fixed_assignment_keys:
                            continue
                        if eligibility_reason(low_name, a_date.isoformat(), a_shift) is not None:
                            continue
                        if eligibility_reason(high_name, b_date.isoformat(), b_shift) is not None:
                            continue
                        improvement = weekend_night_counts[high_name] - weekend_night_counts[low_name]
                        candidates.append((
                            improvement,
                            _preferred_night_removal_penalty(high_name, a_shift, a_date),
                            rolling_weekend_counts[low_name],
                            -rolling_weekend_counts[high_name],
                            _rolling_resident_night_count(low_name),
                            a_date.isoformat(),
                            a_idx,
                            b_idx,
                            high_name,
                            low_name,
                        ))

        for _, _, _, _, _, a_idx, high_name, low_name in sorted(
            direct_candidates,
            key=lambda item: (item[1], -item[0], -item[2], item[3], item[4]),
        ):
            a_row = roster.loc[a_idx]
            a_date = date.fromisoformat(str(a_row.Date))
            a_shift = str(a_row.Shift)
            original_a = _name_list(roster.at[a_idx, "Assigned"])
            if high_name not in original_a or low_name in original_a:
                continue
            current_a = [name for name in original_a if name != high_name]

            roster.at[a_idx, "Assigned"] = _write_name_list(current_a)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            a_last_night = _last_night_before(a_date)
            low_can_take_weekend = (
                _can_worker_take_shift(
                    low_name,
                    a_shift,
                    a_date,
                    last_night_map=a_last_night,
                )
                and _resident_adjacent_night_penalty(low_name, a_date, daily_assignments) == 0
            )
            if low_can_take_weekend:
                roster.at[a_idx, "Assigned"] = _write_name_list(current_a + [low_name])
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                new_objective = _resident_night_objective()
                new_weekend = _count_spread_and_square(
                    Counter({name: weekend_night_counts[name] for name in pool}),
                    pool,
                )
                new_rolling_weekend = _count_spread_and_square(_rolling_resident_weekend_counts(pool), pool)
                new_sandwiches = len(_find_resident_sandwiches())
                sandwich_cost_ok = (
                    new_sandwiches <= current_sandwiches
                    or (
                        current_weekend[0] >= 2
                        and new_weekend[0] < current_weekend[0]
                        and new_sandwiches <= current_sandwiches + 1
                    )
                )
                if (
                    (
                        new_weekend < current_weekend
                        or new_rolling_weekend < rolling_weekend
                    )
                    and _resident_night_total_objective() <= current_total
                    and sandwich_cost_ok
                ):
                    logger.info(
                        "resident direct weekend balance transfer: %s %s %s -> %s objective %s -> %s",
                        a_date.isoformat(), a_shift, high_name, low_name,
                        current_objective, new_objective,
                    )
                    return True

            roster.at[a_idx, "Assigned"] = _write_name_list(original_a)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

        for _, _, _, _, _, _, a_idx, b_idx, high_name, low_name in sorted(
            candidates,
            key=lambda item: (item[1], -item[0], item[2], item[3], item[4], item[5]),
        ):
            a_row = roster.loc[a_idx]
            b_row = roster.loc[b_idx]
            a_date = date.fromisoformat(str(a_row.Date))
            b_date = date.fromisoformat(str(b_row.Date))
            a_shift = str(a_row.Shift)
            b_shift = str(b_row.Shift)
            original_a = _name_list(roster.at[a_idx, "Assigned"])
            original_b = _name_list(roster.at[b_idx, "Assigned"])
            current_a = [name for name in original_a if name != high_name]
            current_b = [name for name in original_b if name != low_name]

            roster.at[a_idx, "Assigned"] = _write_name_list(current_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(current_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            a_last_night = _last_night_before(a_date)
            b_last_night = _last_night_before(b_date)
            low_can_take_weekend = (
                _can_worker_take_shift(
                    low_name,
                    a_shift,
                    a_date,
                    last_night_map=a_last_night,
                )
                and _resident_adjacent_night_penalty(low_name, a_date, daily_assignments) == 0
            )
            high_can_take_weekday = (
                _can_worker_take_shift(
                    high_name,
                    b_shift,
                    b_date,
                    last_night_map=b_last_night,
                )
                and _resident_adjacent_night_penalty(high_name, b_date, daily_assignments) == 0
            )

            if low_can_take_weekend and high_can_take_weekday:
                roster.at[a_idx, "Assigned"] = _write_name_list(current_a + [low_name])
                roster.at[b_idx, "Assigned"] = _write_name_list(current_b + [high_name])
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                new_objective = _resident_night_objective()
                new_weekend = _count_spread_and_square(
                    Counter({name: weekend_night_counts[name] for name in pool}),
                    pool,
                )
                new_rolling_weekend = _count_spread_and_square(_rolling_resident_weekend_counts(pool), pool)
                new_weekend_history = _resident_weekend_history_load(pool)
                new_sandwiches = len(_find_resident_sandwiches())
                sandwich_cost_ok = (
                    new_sandwiches <= current_sandwiches
                    or (
                        current_weekend[0] >= 2
                        and new_weekend[0] < current_weekend[0]
                        and new_sandwiches <= current_sandwiches + 1
                    )
                )
                if (
                    (
                        new_weekend < current_weekend
                        or new_rolling_weekend < rolling_weekend
                        or (
                            current_weekend_gap <= 1
                            and new_weekend <= current_weekend
                            and new_weekend_history < current_weekend_history
                        )
                    )
                    and _resident_night_total_objective() <= current_total
                    and sandwich_cost_ok
                ):
                    logger.info(
                        "resident weekend balance swap: %s %s %s <-> %s %s %s objective %s -> %s",
                        a_date.isoformat(), a_shift, high_name,
                        b_date.isoformat(), b_shift, low_name,
                        current_objective, new_objective,
                    )
                    return True

            roster.at[a_idx, "Assigned"] = _write_name_list(original_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(original_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

        return False

    def _try_cross_date_resident_night_type_swap() -> bool:
        current_objective = _resident_night_objective()
        current_weekend_history = _resident_weekend_history_load()
        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        if rows.empty:
            return False

        rows["_date"] = rows["Date"].map(lambda x: date.fromisoformat(str(x)))
        candidates: list[tuple[int, int, str, int, int, str, str]] = []
        for a_idx, a_row in rows.iterrows():
            a_date = a_row["_date"]
            a_shift = str(a_row.Shift)
            b_shift = "ת.מיון 2" if a_shift == "ת.מיון" else "ת.מיון"
            for name_a in _name_list(a_row.Assigned):
                if (a_date, a_shift, name_a) in fixed_assignment_keys:
                    continue
                if resident_night_shift_counts[a_shift][name_a] <= resident_night_shift_counts[b_shift][name_a]:
                    continue

                for b_idx, b_row in rows[rows["Shift"] == b_shift].iterrows():
                    b_date = b_row["_date"]
                    for name_b in _name_list(b_row.Assigned):
                        if name_a == name_b:
                            continue
                        if (b_date, b_shift, name_b) in fixed_assignment_keys:
                            continue
                        if resident_night_shift_counts[b_shift][name_b] <= resident_night_shift_counts[a_shift][name_b]:
                            continue

                        before = (
                            abs(resident_night_shift_counts[a_shift][name_a] - resident_night_shift_counts[b_shift][name_a])
                            + abs(resident_night_shift_counts[a_shift][name_b] - resident_night_shift_counts[b_shift][name_b])
                            + _resident_type_compensation_key(name_a, a_shift)
                            + _resident_type_compensation_key(name_b, b_shift)
                        )
                        after = (
                            abs((resident_night_shift_counts[a_shift][name_a] - 1) - (resident_night_shift_counts[b_shift][name_a] + 1))
                            + abs((resident_night_shift_counts[a_shift][name_b] + 1) - (resident_night_shift_counts[b_shift][name_b] - 1))
                            + _resident_type_compensation_key(name_a, b_shift)
                            + _resident_type_compensation_key(name_b, a_shift)
                        )
                        improvement = before - after
                        if improvement <= 0:
                            continue
                        candidates.append((improvement, before, a_date.isoformat(), a_idx, b_idx, name_a, name_b))

        for _, _, _, a_idx, b_idx, name_a, name_b in sorted(
            candidates,
            key=lambda item: (-item[0], -item[1], item[2]),
        ):
            a_row = roster.loc[a_idx]
            b_row = roster.loc[b_idx]
            a_date = date.fromisoformat(str(a_row.Date))
            b_date = date.fromisoformat(str(b_row.Date))
            a_shift = str(a_row.Shift)
            b_shift = str(b_row.Shift)
            original_a = _name_list(a_row.Assigned)
            original_b = _name_list(b_row.Assigned)
            if name_a not in original_a or name_b not in original_b:
                continue

            current_a = [name for name in original_a if name != name_a]
            current_b = [name for name in original_b if name != name_b]
            roster.at[a_idx, "Assigned"] = _write_name_list(current_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(current_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            a_last_night = _last_night_before(a_date)
            b_last_night = _last_night_before(b_date)
            b_can_take_a = (
                _can_worker_take_shift(
                    name_b,
                    a_shift,
                    a_date,
                    last_night_map=a_last_night,
                )
                and _resident_adjacent_night_penalty(name_b, a_date, daily_assignments) == 0
                and _resident_night_spacing_penalty(name_b, a_date, a_last_night) < 100
            )
            a_can_take_b = (
                _can_worker_take_shift(
                    name_a,
                    b_shift,
                    b_date,
                    last_night_map=b_last_night,
                )
                and _resident_adjacent_night_penalty(name_a, b_date, daily_assignments) == 0
                and _resident_night_spacing_penalty(name_a, b_date, b_last_night) < 100
            )

            if b_can_take_a and a_can_take_b:
                roster.at[a_idx, "Assigned"] = _write_name_list(current_a + [name_b])
                roster.at[b_idx, "Assigned"] = _write_name_list(current_b + [name_a])
                candidate_objective = _resident_night_objective()
                if (
                    _resident_hard_objective(candidate_objective) <= _resident_hard_objective(current_objective)
                    and _resident_preference_objective(candidate_objective) <= _resident_preference_objective(current_objective)
                    and _resident_shift_type_objective(candidate_objective) < _resident_shift_type_objective(current_objective)
                    and _resident_weekend_history_load() <= current_weekend_history
                ):
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    logger.info(
                        "cross-date resident night type swap: %s %s %s <-> %s %s %s objective %s -> %s",
                        a_date.isoformat(), a_shift, name_a,
                        b_date.isoformat(), b_shift, name_b,
                        current_objective, candidate_objective,
                    )
                    return True

            roster.at[a_idx, "Assigned"] = _write_name_list(original_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(original_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

        return False

    def _try_preferred_resident_night_swap() -> bool:
        current_objective = _resident_night_objective()
        current_weekend_history = _resident_weekend_history_load()
        requests = sorted(
            preferred_night_requests.items(),
            key=lambda item: (-item[1], 0 if item[0][1].weekday() in (4, 5) else 1, item[0][1], item[0][0]),
        )
        resident_rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        if resident_rows.empty:
            return False

        for (name, pref_date), _strength in requests:
            if not _preferred_night_shifts_for(name).intersection(RESIDENT_NIGHT_SHIFTS):
                continue
            if daily_assignments.get(pref_date, {}).get(name, set()).intersection(RESIDENT_NIGHT_SHIFTS):
                continue

            for pref_shift in sorted(_preferred_night_shifts_for(name) & RESIDENT_NIGHT_SHIFTS):
                pref_mask = (roster["Date"] == pref_date.isoformat()) & (roster["Shift"] == pref_shift)
                if not pref_mask.any():
                    continue
                pref_idx = int(roster.index[pref_mask][0])
                needed = _to_int(roster.at[pref_idx, "Needed"], 0)
                current_pref = _name_list(roster.at[pref_idx, "Assigned"])
                if needed <= 0 or len(current_pref) < needed:
                    continue

                for displaced in sorted(
                    current_pref,
                    key=lambda n: (
                        _preferred_night_removal_penalty(n, pref_shift, pref_date),
                        resident_night_shift_counts[pref_shift][n],
                        n,
                    ),
                ):
                    if displaced == name or (pref_date, pref_shift, displaced) in fixed_assignment_keys:
                        continue

                    donor_rows = resident_rows[
                        resident_rows["Assigned"].astype(str).map(lambda cell: name in _name_list(cell))
                    ]
                    for donor_idx, donor_row in donor_rows.iterrows():
                        donor_date = date.fromisoformat(str(donor_row.Date))
                        donor_shift = str(donor_row.Shift)
                        if donor_idx == pref_idx:
                            continue
                        if (donor_date, donor_shift, name) in fixed_assignment_keys:
                            continue
                        if not worker_shift_lut.get((displaced, donor_shift), False):
                            continue

                        original_pref = _name_list(roster.at[pref_idx, "Assigned"])
                        original_donor = _name_list(roster.at[donor_idx, "Assigned"])
                        if displaced not in original_pref or name not in original_donor:
                            continue

                        roster.at[pref_idx, "Assigned"] = _write_name_list(
                            [n for n in original_pref if n != displaced]
                        )
                        roster.at[donor_idx, "Assigned"] = _write_name_list(
                            [n for n in original_donor if n != name]
                        )
                        _rebuild_daily_assignments_from_roster()
                        _rebuild_night_state_from_roster()
                        _rebuild_live_counters_from_roster()

                        pref_last_night = _last_night_before(pref_date)
                        donor_last_night = _last_night_before(donor_date)
                        name_can_take_preferred = (
                            _can_worker_take_shift(
                                name,
                                pref_shift,
                                pref_date,
                                last_night_map=pref_last_night,
                            )
                            and _resident_adjacent_night_penalty(name, pref_date, daily_assignments) == 0
                            and _resident_night_spacing_penalty(name, pref_date, pref_last_night) < 100
                        )
                        displaced_can_take_donor = (
                            _can_worker_take_shift(
                                displaced,
                                donor_shift,
                                donor_date,
                                last_night_map=donor_last_night,
                            )
                            and _resident_adjacent_night_penalty(displaced, donor_date, daily_assignments) == 0
                            and _resident_night_spacing_penalty(displaced, donor_date, donor_last_night) < 100
                        )

                        if name_can_take_preferred and displaced_can_take_donor:
                            roster.at[pref_idx, "Assigned"] = _write_name_list(
                                [n for n in original_pref if n != displaced] + [name]
                            )
                            roster.at[donor_idx, "Assigned"] = _write_name_list(
                                [n for n in original_donor if n != name] + [displaced]
                            )
                            candidate_objective = _resident_night_objective()
                            if (
                                _resident_hard_objective(candidate_objective) <= _resident_hard_objective(current_objective)
                                and _resident_preference_objective(candidate_objective) < _resident_preference_objective(current_objective)
                                and _resident_weekend_history_load() <= current_weekend_history
                            ):
                                _rebuild_daily_assignments_from_roster()
                                _rebuild_night_state_from_roster()
                                _rebuild_live_counters_from_roster()
                                logger.info(
                                    "preferred resident night swap: %s %s %s <= %s; %s %s %s <= %s objective %s -> %s",
                                    pref_date.isoformat(), pref_shift, name, displaced,
                                    donor_date.isoformat(), donor_shift, displaced, name,
                                    current_objective, candidate_objective,
                                )
                                return True

                        roster.at[pref_idx, "Assigned"] = _write_name_list(original_pref)
                        roster.at[donor_idx, "Assigned"] = _write_name_list(original_donor)
                        _rebuild_daily_assignments_from_roster()
                        _rebuild_night_state_from_roster()
                        _rebuild_live_counters_from_roster()

        return False

    def _optimize_resident_night_assignments(max_steps: int = 8) -> int:
        label = "resident_night_assignment_optimization"
        if _repair_noop_cached(label):
            return 0
        improved = 0
        for _ in range(max_steps):
            if (
                _try_preferred_resident_night_swap()
                or _try_resident_night_type_swap()
                or _try_cross_date_resident_night_type_swap()
            ):
                improved += 1
                _forget_repair_noops()
            else:
                _remember_repair_noop(label)
                break
        return improved

    def _repair_resident_weekend_balance(max_steps: int = 24) -> int:
        label = "resident_weekend_balance"
        if _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(max_steps):
            if not _try_resident_weekend_swap():
                _remember_repair_noop(label)
                break
            repaired += 1
            _forget_repair_noops()
        return repaired

    def _try_resident_rolling_total_swap() -> bool:
        pool = {
            name
            for name in _resident_night_pool()
            if month_counts[name] > 0
        }
        if not pool:
            return False

        current_counts = _current_resident_total_counts(pool)
        rolling_counts = _rolling_resident_total_counts(pool)
        current_values = [current_counts[name] for name in pool]
        if max(current_values) - min(current_values) > 1:
            return False

        current_total = _resident_night_total_objective()
        current_rolling = _resident_rolling_total_objective()
        current_weekend = _resident_weekend_objective()
        current_sandwiches = len(_find_resident_sandwiches())
        max_current = max(current_values)
        min_current = min(current_values)
        high_pool = {
            name for name in pool
            if current_counts[name] == max_current
            and rolling_counts[name] > min(rolling_counts.values())
        }
        low_pool = {
            name for name in pool
            if current_counts[name] == min_current
        }
        if not high_pool or not low_pool:
            return False

        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        rows["_date"] = rows["Date"].map(lambda x: date.fromisoformat(str(x)))
        rows["_weekend"] = rows["_date"].map(lambda d: 1 if d.weekday() in (4, 5) else 0)

        for idx, row in rows.sort_values(["_weekend", "_date"], ascending=[True, False]).iterrows():
            shift_date = row["_date"]
            shift_type = str(row.Shift)
            for high_name in sorted(
                [name for name in _name_list(row.Assigned) if name in high_pool],
                key=lambda n: (-rolling_counts[n], -previous_resident_night_counts[n], n),
            ):
                if (shift_date, shift_type, high_name) in fixed_assignment_keys:
                    continue
                eligible_lows = {
                    name for name in low_pool
                    if rolling_counts[name] < rolling_counts[high_name]
                }
                if not eligible_lows:
                    continue

                assigned_snapshot = roster["Assigned"].copy()
                if _try_replace_resident_night(
                    idx,
                    high_name,
                    preferred_names=eligible_lows,
                        reason="resident rolling history balance repair",
                        allow_sandwich=True,
                        protect_weekend_objective=current_weekend,
                        protect_weekend_history=_resident_weekend_history_load(),
                    ):
                    if (
                        _resident_night_total_objective() <= current_total
                        and _resident_rolling_total_objective() < current_rolling
                        and len(_find_resident_sandwiches()) <= current_sandwiches
                    ):
                        return True
                    roster["Assigned"] = assigned_snapshot
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()

        return False

    def _repair_resident_rolling_total_balance(max_steps: int = 20) -> int:
        label = "resident_rolling_total_balance"
        if _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(max_steps):
            if not _try_resident_rolling_total_swap():
                _remember_repair_noop(label)
                break
            repaired += 1
            _forget_repair_noops()
        return repaired

    def _try_resident_thursday_swap() -> bool:
        pool = {
            name
            for name in _resident_night_pool()
            if month_counts[name] > 0
        }
        if not pool:
            return False
        values = [thursday_night_counts[name] for name in pool]
        min_thursday = min(values)
        if max(values) - min_thursday <= 1:
            return False

        current_objective = _resident_night_objective()
        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        rows["_weekday"] = rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
        thursday_rows = rows[rows["_weekday"] == 3]
        swap_rows = rows[~rows["_weekday"].isin([3, 4, 5])]
        candidates: list[tuple[int, int, str, int, int, str, str]] = []

        for a_idx, a_row in thursday_rows.iterrows():
            a_date = date.fromisoformat(str(a_row.Date))
            a_shift = str(a_row.Shift)
            for high_name in _name_list(a_row.Assigned):
                if thursday_night_counts[high_name] <= min_thursday + 1:
                    continue
                if (a_date, a_shift, high_name) in fixed_assignment_keys:
                    continue
                for b_idx, b_row in swap_rows.iterrows():
                    b_date = date.fromisoformat(str(b_row.Date))
                    b_shift = str(b_row.Shift)
                    for low_name in _name_list(b_row.Assigned):
                        if low_name == high_name:
                            continue
                        if thursday_night_counts[low_name] != min_thursday:
                            continue
                        if (b_date, b_shift, low_name) in fixed_assignment_keys:
                            continue
                        candidates.append((
                            thursday_night_counts[high_name] - thursday_night_counts[low_name],
                            month_counts[low_name],
                            a_date.isoformat(),
                            a_idx,
                            b_idx,
                            high_name,
                            low_name,
                        ))

        for _, _, _, a_idx, b_idx, high_name, low_name in sorted(
            candidates,
            key=lambda item: (-item[0], item[1], item[2]),
        ):
            a_row = roster.loc[a_idx]
            b_row = roster.loc[b_idx]
            a_date = date.fromisoformat(str(a_row.Date))
            b_date = date.fromisoformat(str(b_row.Date))
            a_shift = str(a_row.Shift)
            b_shift = str(b_row.Shift)
            original_a = _name_list(roster.at[a_idx, "Assigned"])
            original_b = _name_list(roster.at[b_idx, "Assigned"])
            current_a = [name for name in original_a if name != high_name]
            current_b = [name for name in original_b if name != low_name]

            roster.at[a_idx, "Assigned"] = _write_name_list(current_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(current_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            a_last_night = _last_night_before(a_date)
            b_last_night = _last_night_before(b_date)
            low_can_take_thursday = (
                _can_worker_take_shift(
                    low_name,
                    a_shift,
                    a_date,
                    last_night_map=a_last_night,
                )
                and _resident_adjacent_night_penalty(low_name, a_date, daily_assignments) == 0
            )
            high_can_take_other = (
                _can_worker_take_shift(
                    high_name,
                    b_shift,
                    b_date,
                    last_night_map=b_last_night,
                )
                and _resident_adjacent_night_penalty(high_name, b_date, daily_assignments) == 0
            )

            if low_can_take_thursday and high_can_take_other:
                roster.at[a_idx, "Assigned"] = _write_name_list(current_a + [low_name])
                roster.at[b_idx, "Assigned"] = _write_name_list(current_b + [high_name])
                if _resident_night_objective() < current_objective:
                    logger.info(
                        "resident Thursday balance swap: %s %s %s <-> %s %s %s",
                        a_date.isoformat(), a_shift, high_name,
                        b_date.isoformat(), b_shift, low_name,
                    )
                    return True

            roster.at[a_idx, "Assigned"] = _write_name_list(original_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(original_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

        return False

    def _repair_resident_thursday_balance(max_steps: int = 3) -> int:
        label = "resident_thursday_balance"
        if _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(max_steps):
            if not _try_resident_thursday_swap():
                _remember_repair_noop(label)
                break
            repaired += 1
            _forget_repair_noops()
        return repaired

    def _repair_resident_night_fairness(
        *,
        rounds: int = 3,
        weekend_steps: int = 24,
        type_steps: int = 2,
        thursday_steps: int = 3,
    ) -> int:
        _forget_repair_noops()
        repaired = 0
        for _ in range(rounds):
            before = repaired
            repaired += _repair_resident_night_balance()
            repaired += _repair_resident_rolling_total_balance()
            repaired += _repair_resident_weekend_balance(max_steps=weekend_steps)
            repaired += _repair_resident_sandwiches()
            repaired += _repair_resident_weekend_balance(max_steps=weekend_steps)
            repaired += _optimize_resident_night_assignments(max_steps=type_steps)
            repaired += _repair_resident_weekend_balance(max_steps=weekend_steps)
            repaired += _repair_resident_rolling_total_balance()
            repaired += _repair_resident_thursday_balance(max_steps=thursday_steps)
            if repaired == before:
                break
        if _resident_night_total_objective()[0] > 1:
            repaired += _repair_resident_night_balance()
        return repaired

    def _senior_other_friday_day_dates(name: str, d: date) -> set[date]:
        days: set[date] = set()
        for shift in FRIDAY_DAY_BALANCE_SHIFTS:
            idx = _row_index(d, shift)
            if idx is not None and name in _name_list(roster.at[idx, "Assigned"]):
                days.add(d)
        for other_date, by_name in daily_assignments.items():
            if other_date.weekday() != 4 or other_date == d:
                continue
            if by_name.get(name, set()).intersection(FRIDAY_DAY_BALANCE_SHIFTS):
                days.add(other_date)
        return days - {d}

    def _is_protected_friday_pair(d: date, name: str, shift: str) -> bool:
        if d.weekday() != 4:
            return False
        if shift == ATTENDING_SHIFT and _has_shift(daily_assignments, d, name, KONEN_MION_SHIFT):
            return True
        if shift == "מיון" and _has_shift(daily_assignments, d, name, "ת.מיון"):
            return True
        if shift == "מחלקה" and _has_shift(daily_assignments, d, name, "ת.מיון 2"):
            return True
        return False

    def _can_place_on_shift_after_removal(
        d: date,
        shift: str,
        name: str,
        idx: int,
        trial_names: list[str],
    ) -> bool:
        original = _name_list(roster.at[idx, "Assigned"])
        roster.at[idx, "Assigned"] = _write_name_list(trial_names)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()
        elig = get_eligible_workers(
            shift_type=shift,
            shift_date=d,
            blocked_next_day=blocked_next_day,
            extra_day_off=extra_day_off,
            daily_assignments=daily_assignments,
            blocked_reasons=None,
            last_night=_last_night_before(d),
        )
        roster.at[idx, "Assigned"] = _write_name_list(original)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()
        return name in elig

    def _place_on_friday_shift(
        d: date,
        shift: str,
        name: str,
        *,
        protect_senior_cap: bool = False,
    ) -> bool:
        idx = _row_index(d, shift)
        if idx is None:
            return False
        needed = int(roster.at[idx, "Needed"])
        soft = int(roster.at[idx, "SoftCap"])
        if needed <= 0 or soft <= 0:
            return False

        current = _name_list(roster.at[idx, "Assigned"])
        if name in current:
            return False
        if protect_senior_cap and _is_senior_name(name) and _senior_other_friday_day_dates(name, d):
            return False

        victim_options: list[str | None] = []
        if len(current) < soft:
            victim_options.append(None)
        removable = [
            victim for victim in current
            if (d, shift, victim) not in fixed_assignment_keys
            and not _is_protected_friday_pair(d, victim, shift)
        ]
        victim_options.extend(
            sorted(
                removable,
                key=lambda victim: (
                    _is_senior_name(victim),
                    -friday_day_counts[victim],
                    fairness_score(victim, shift, d, history, _last_night_before(d)),
                ),
            )
        )

        for victim in victim_options:
            trial = [existing for existing in current if existing != victim]
            if not _can_place_on_shift_after_removal(d, shift, name, idx, trial):
                continue
            roster.at[idx, "Assigned"] = _write_name_list(trial + [name])
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            logger.info(
                "Friday pairing repair: %s %s added %s%s",
                d.isoformat(),
                shift,
                name,
                f" replacing {victim}" if victim else "",
            )
            return True
        return False

    def _repair_friday_pairings() -> int:
        repaired = 0
        friday_dates = sorted(
            date.fromisoformat(str(value))
            for value in roster["Date"].unique()
            if date.fromisoformat(str(value)).weekday() == 4
        )
        for d in friday_dates:
            for night_shift, day_shift in FRIDAY_NIGHT_MORNING_SHIFT.items():
                idx = _row_index(d, night_shift)
                if idx is None:
                    continue
                for name in _name_list(roster.at[idx, "Assigned"]):
                    if _place_on_friday_shift(d, day_shift, name):
                        repaired += 1

            konen_idx = _row_index(d, KONEN_MION_SHIFT)
            if konen_idx is None:
                continue
            for name in _name_list(roster.at[konen_idx, "Assigned"]):
                if _place_on_friday_shift(
                    d,
                    ATTENDING_SHIFT,
                    name,
                    protect_senior_cap=True,
                ):
                    repaired += 1
        return repaired

    def _friday_day_pool() -> set[str]:
        assigned = set(friday_day_counts)
        capable = {
            name
            for (name, shift), ok in worker_shift_lut.items()
            if ok and shift in FRIDAY_DAY_BALANCE_SHIFTS
        }
        return assigned | capable

    def _friday_day_objective() -> tuple[int, int]:
        pool = _friday_day_pool()
        if not pool:
            return (0, 0)
        return _count_spread_and_square(friday_day_counts, pool)

    def _try_friday_day_swap() -> bool:
        pool = _friday_day_pool()
        if not pool:
            return False
        values = [friday_day_counts[name] for name in pool]
        min_fridays = min(values)
        if max(values) - min_fridays <= 1:
            return False

        current_objective = _friday_day_objective()
        rows = roster[roster["Shift"].isin(FRIDAY_DAY_BALANCE_SHIFTS)].copy()
        rows["_weekday"] = rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
        friday_rows = rows[rows["_weekday"] == 4]
        other_rows = rows[rows["_weekday"] != 4]
        candidates: list[tuple[int, int, str, int, int, str, str]] = []

        for a_idx, a_row in friday_rows.iterrows():
            a_date = date.fromisoformat(str(a_row.Date))
            a_shift = str(a_row.Shift)
            for high_name in _name_list(a_row.Assigned):
                if _is_protected_friday_pair(a_date, high_name, a_shift):
                    continue
                if friday_day_counts[high_name] <= min_fridays + 1:
                    continue
                if (a_date, a_shift, high_name) in fixed_assignment_keys:
                    continue
                for b_idx, b_row in other_rows[other_rows["Shift"] == a_shift].iterrows():
                    b_date = date.fromisoformat(str(b_row.Date))
                    b_shift = str(b_row.Shift)
                    for low_name in _name_list(b_row.Assigned):
                        if low_name == high_name:
                            continue
                        if friday_day_counts[low_name] >= friday_day_counts[high_name] - 1:
                            continue
                        if (b_date, b_shift, low_name) in fixed_assignment_keys:
                            continue
                        candidates.append((
                            friday_day_counts[high_name] - friday_day_counts[low_name],
                            friday_day_counts[low_name],
                            a_date.isoformat(),
                            a_idx,
                            b_idx,
                            high_name,
                            low_name,
                        ))

        for _, _, _, a_idx, b_idx, high_name, low_name in sorted(
            candidates,
            key=lambda item: (-item[0], item[1], item[2]),
        ):
            a_row = roster.loc[a_idx]
            b_row = roster.loc[b_idx]
            a_date = date.fromisoformat(str(a_row.Date))
            b_date = date.fromisoformat(str(b_row.Date))
            a_shift = str(a_row.Shift)
            b_shift = str(b_row.Shift)
            original_a = _name_list(roster.at[a_idx, "Assigned"])
            original_b = _name_list(roster.at[b_idx, "Assigned"])
            current_a = [name for name in original_a if name != high_name]
            current_b = [name for name in original_b if name != low_name]

            roster.at[a_idx, "Assigned"] = _write_name_list(current_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(current_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            low_can_take_friday = _can_worker_take_shift(
                low_name,
                a_shift,
                a_date,
                last_night_map=_last_night_before(a_date),
            )
            high_can_take_other = _can_worker_take_shift(
                high_name,
                b_shift,
                b_date,
                last_night_map=_last_night_before(b_date),
            )

            if low_can_take_friday and high_can_take_other:
                roster.at[a_idx, "Assigned"] = _write_name_list(current_a + [low_name])
                roster.at[b_idx, "Assigned"] = _write_name_list(current_b + [high_name])
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                if _friday_day_objective() < current_objective:
                    logger.info(
                        "Friday day balance swap: %s %s %s <-> %s %s %s",
                        a_date.isoformat(), a_shift, high_name,
                        b_date.isoformat(), b_shift, low_name,
                    )
                    return True

            roster.at[a_idx, "Assigned"] = _write_name_list(original_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(original_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

        return False

    def _repair_friday_day_balance(max_steps: int = 8) -> int:
        repaired = 0
        for _ in range(max_steps):
            if not _try_friday_day_swap():
                break
            repaired += 1
        return repaired

    def _repair_resident_night_balance() -> int:
        label = "resident_night_balance"
        if _repair_noop_cached(label):
            return 0
        repaired = 0
        seen_states: set[tuple[tuple[str, int], ...]] = set()
        for _ in range(20):
            counts = _resident_night_total_counts()
            if not counts:
                _remember_repair_noop(label)
                break
            state = tuple(sorted(counts.items()))
            if state in seen_states:
                _remember_repair_noop(label)
                break
            seen_states.add(state)
            max_count = max(counts.values())
            min_count = min(counts.values())
            if max_count - min_count <= 1:
                _remember_repair_noop(label)
                break

            high_names = [name for name, count in counts.items() if count == max_count]
            low_names = {name for name, count in counts.items() if count == min_count}
            before_objective = _resident_night_total_objective()
            changed = False

            for high_name in sorted(high_names, key=lambda n: (-counts[n], -month_counts[n], n)):
                high_rows = roster[
                    (roster["Shift"].isin(["ת.מיון", "ת.מיון 2"]))
                    & (roster["Assigned"].astype(str).map(lambda cell: high_name in _name_list(cell)))
                ].copy()
                high_rows["_weekday"] = high_rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
                for idx, _ in high_rows.sort_values(["_weekday", "Date"], ascending=[False, False]).iterrows():
                    assigned_snapshot = roster["Assigned"].copy()
                    if _try_replace_resident_night(
                        idx,
                        high_name,
                        preferred_names=low_names,
                        reason="resident night balance repair",
                        allow_sandwich=True,
                        protect_weekend_objective=_resident_weekend_objective(),
                        protect_weekend_history=_resident_weekend_history_load(),
                    ):
                        after_objective = _resident_night_total_objective()
                        if after_objective < before_objective:
                            repaired += 1
                            changed = True
                            _forget_repair_noops()
                            break
                        roster["Assigned"] = assigned_snapshot
                        _rebuild_daily_assignments_from_roster()
                        _rebuild_night_state_from_roster()
                        _rebuild_live_counters_from_roster()
                if changed:
                    break

            if not changed:
                if _try_best_resident_night_improvement():
                    repaired += 1
                    _forget_repair_noops()
                    continue
                _remember_repair_noop(label)
                break
        return repaired

    def _senior_on_call_pool() -> set[str]:
        return {
            name
            for name in senior_names
            if worker_shift_lut.get((name, KONEN_MION_SHIFT), False)
        }

    def _try_replace_konen_mion(
        idx: int,
        old_name: str,
        preferred_names: set[str],
    ) -> bool:
        row = roster.loc[idx]
        shift_date = date.fromisoformat(str(row.Date))
        shift_type = str(row.Shift)
        if (shift_date, shift_type, old_name) in fixed_assignment_keys:
            return False

        original = _name_list(row.Assigned)
        current = [name for name in original if name != old_name]
        roster.at[idx, "Assigned"] = _write_name_list(current)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()

        eligible = get_eligible_workers(
            shift_type=KONEN_MION_SHIFT,
            shift_date=shift_date,
            blocked_next_day=blocked_next_day,
            extra_day_off=extra_day_off,
            daily_assignments=daily_assignments,
            blocked_reasons=None,
            last_night=_last_night_before(shift_date),
        )
        candidates = [
            name for name in eligible
            if name in preferred_names
            and name not in current
            and name != old_name
            and (
                name != SHIMON_NAME
                or shift_date.weekday() != 4
                or _shimon_friday_available(shift_date)
            )
        ]
        if not candidates:
            roster.at[idx, "Assigned"] = _write_name_list(original)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            return False

        pick = min(
            candidates,
            key=lambda name: (
                _konen_mion_key(name, shift_date),
                fairness_score(name, KONEN_MION_SHIFT, shift_date, history, _last_night_before(shift_date)),
            ),
        )
        roster.at[idx, "Assigned"] = _write_name_list(current + [pick])
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()
        logger.info(
            "senior on-call balance repair: %s %s -> %s",
            shift_date.isoformat(), old_name, pick,
        )
        return True

    def _repair_konen_mion_balance() -> int:
        repaired = 0
        pool = _senior_on_call_pool()
        for _ in range(80):
            if not pool:
                break
            regular_pool = pool - {SHIMON_NAME}
            shimon_count = konen_month_counts[SHIMON_NAME] if SHIMON_NAME in pool else SHIMON_KONEN_TARGET
            regular_values = [konen_month_counts[name] for name in regular_pool]
            regular_spread_ok = not regular_values or max(regular_values) - min(regular_values) <= 1
            shimon_ok = SHIMON_NAME not in pool or shimon_count == SHIMON_KONEN_TARGET
            if regular_spread_ok and shimon_ok:
                break

            if SHIMON_NAME in pool and shimon_count < SHIMON_KONEN_TARGET and regular_values:
                max_count = max(regular_values)
                high_names = [name for name in regular_pool if konen_month_counts[name] == max_count]
                low_names = {SHIMON_NAME}
            elif SHIMON_NAME in pool and shimon_count > SHIMON_KONEN_TARGET and regular_values:
                min_count = min(regular_values)
                high_names = [SHIMON_NAME]
                low_names = {name for name in regular_pool if konen_month_counts[name] == min_count}
            elif regular_values:
                max_count = max(regular_values)
                min_count = min(regular_values)
                high_names = [name for name in regular_pool if konen_month_counts[name] == max_count]
                low_names = {name for name in regular_pool if konen_month_counts[name] == min_count}
            else:
                break

            changed = False
            for high_name in sorted(high_names, key=lambda n: (-konen_month_counts[n], n)):
                high_rows = roster[
                    (roster["Shift"] == KONEN_MION_SHIFT)
                    & (roster["Assigned"].astype(str).map(lambda cell: high_name in _name_list(cell)))
                ].copy()
                high_rows["_weekday"] = high_rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
                for idx, _ in high_rows.sort_values(["_weekday", "Date"], ascending=[False, False]).iterrows():
                    if _try_replace_konen_mion(idx, high_name, low_names):
                        repaired += 1
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                break
        return repaired

    def _senior_yoeatzim_pool() -> set[str]:
        return {
            name
            for (name, shift), ok in worker_shift_lut.items()
            if ok and shift == YOEATZIM_SHIFT and name != SHIMON_NAME
        }

    def _yoeatzim_balance_objective(pool: set[str]) -> tuple[int, int, int, int]:
        if not pool:
            return (0, 0, 0, 0)
        effective_counts = [
            yoeatzim_counts[name] + (attending_counts[name] // 3)
            for name in pool
        ]
        raw_counts = [yoeatzim_counts[name] for name in pool]
        spread = max(raw_counts) - min(raw_counts)
        effective_spread = max(effective_counts) - min(effective_counts)
        squared_load = sum(count * count for count in effective_counts)
        attending_load = sum(
            yoeatzim_counts[name] * (attending_counts[name] if _is_senior_name(name) else 0)
            for name in pool
        )
        resident_night_load = sum(
            yoeatzim_counts[name] * (month_counts[name] if not _is_senior_name(name) else 0)
            for name in pool
        )
        weekday_counts = [yoeatzim_weekday_counts[name] for name in pool]
        weekday_spread = max(weekday_counts) - min(weekday_counts)
        return (effective_spread, spread, squared_load, attending_load + resident_night_load + weekday_spread)

    def _try_replace_yoeatzim(
        idx: int,
        old_name: str,
        preferred_names: set[str],
        *,
        require_improved_objective: bool = False,
    ) -> bool:
        row = roster.loc[idx]
        shift_date = date.fromisoformat(str(row.Date))
        shift_type = str(row.Shift)
        if (shift_date, shift_type, old_name) in fixed_assignment_keys:
            return False

        pool = _senior_yoeatzim_pool()
        old_objective = _yoeatzim_balance_objective(pool)
        original = _name_list(row.Assigned)
        current = [name for name in original if name != old_name]
        roster.at[idx, "Assigned"] = _write_name_list(current)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()

        eligible = get_eligible_workers(
            shift_type=YOEATZIM_SHIFT,
            shift_date=shift_date,
            blocked_next_day=blocked_next_day,
            extra_day_off=extra_day_off,
            daily_assignments=daily_assignments,
            blocked_reasons=None,
            last_night=_last_night_before(shift_date),
        )
        candidates = [
            name for name in eligible
            if name in preferred_names
            and name not in current
            and name != old_name
            and _yoeatzim_allowed(name, shift_date)
            and _personal_under_max(name, shift_type, shift_date)
        ]
        if not candidates:
            roster.at[idx, "Assigned"] = _write_name_list(original)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            return False

        pick = min(
            candidates,
            key=lambda name: (
                _yoeatzim_key(name, shift_date),
                fairness_score(name, YOEATZIM_SHIFT, shift_date, history, _last_night_before(shift_date)),
            ),
        )

        roster.at[idx, "Assigned"] = _write_name_list(current + [pick])
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()

        if require_improved_objective and _yoeatzim_balance_objective(pool) >= old_objective:
            roster.at[idx, "Assigned"] = _write_name_list(original)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            return False

        logger.info(
            "senior consult balance repair: %s %s -> %s",
            shift_date.isoformat(), old_name, pick,
        )
        return True

    def _repair_yoeatzim_balance() -> int:
        repaired = 0
        pool = _senior_yoeatzim_pool()
        for _ in range(20):
            before_cap_objective = _yoeatzim_weekday_cap_objective()
            if before_cap_objective[:2] == (0, 0):
                break
            changed = False
            assigned_rows = roster[roster["Shift"] == YOEATZIM_SHIFT].copy()
            assigned_rows["_weekday"] = assigned_rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
            assigned_rows = assigned_rows[~assigned_rows["_weekday"].isin([4, 5])]
            for idx, row in assigned_rows.sort_values(["Date"], ascending=[False]).iterrows():
                shift_date = date.fromisoformat(str(row.Date))
                for old_name in sorted(
                    [
                        name for name in _name_list(row.Assigned)
                        if _is_senior_name(name)
                        and yoeatzim_weekday_counts[name] > _senior_yoeatzim_preferred_cap(name)
                    ],
                    key=lambda n: (
                        -(yoeatzim_weekday_counts[n] - _senior_yoeatzim_preferred_cap(n)),
                        -yoeatzim_weekday_counts[n],
                        -attending_counts[n],
                        n,
                    ),
                ):
                    target_names = {
                        name
                        for (name, shift), ok in worker_shift_lut.items()
                        if ok
                        and shift == YOEATZIM_SHIFT
                        and name != old_name
                        and _yoeatzim_allowed(name, shift_date)
                        and (
                            not _is_senior_name(name)
                            or yoeatzim_weekday_counts[name] < _senior_yoeatzim_preferred_cap(name)
                        )
                    }
                    if not target_names:
                        continue
                    assigned_snapshot = roster["Assigned"].copy()
                    if _try_replace_yoeatzim(idx, old_name, target_names):
                        if _yoeatzim_weekday_cap_objective() < before_cap_objective:
                            repaired += 1
                            changed = True
                            break
                        roster["Assigned"] = assigned_snapshot
                        _rebuild_daily_assignments_from_roster()
                        _rebuild_night_state_from_roster()
                        _rebuild_live_counters_from_roster()
                if changed:
                    break
            if not changed:
                break

        for _ in range(6):
            zero_names = [name for name in pool if yoeatzim_counts[name] == 0]
            if not zero_names:
                break
            changed = False
            for low_name in sorted(zero_names, key=lambda n: (0 if not _is_senior_name(n) else 1, month_counts[n], n)):
                high_names = [
                    name for name in pool
                    if yoeatzim_counts[name] >= 2 and name != low_name
                ]
                for high_name in sorted(high_names, key=lambda n: (-yoeatzim_counts[n], -attending_counts[n], n)):
                    high_rows = roster[
                        (roster["Shift"] == YOEATZIM_SHIFT)
                        & (roster["Assigned"].astype(str).map(lambda cell: high_name in _name_list(cell)))
                    ].copy()
                    high_rows["_weekday"] = high_rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
                    for idx, _ in high_rows.sort_values(["_weekday", "Date"], ascending=[False, False]).iterrows():
                        if _try_replace_yoeatzim(idx, high_name, {low_name}, require_improved_objective=True):
                            repaired += 1
                            changed = True
                            break
                    if changed:
                        break
                if changed:
                    break
            if not changed:
                break

        for _ in range(12):
            if not pool:
                break
            values = [yoeatzim_counts[name] for name in pool]
            max_count = max(values)
            min_count = min(values)
            if max_count - min_count <= 1:
                break

            high_names = [name for name in pool if yoeatzim_counts[name] > min_count]
            changed = False
            for high_name in sorted(high_names, key=lambda n: (-yoeatzim_counts[n], -attending_counts[n], n)):
                lower_names = {name for name in pool if yoeatzim_counts[name] < yoeatzim_counts[high_name]}
                high_rows = roster[
                    (roster["Shift"] == YOEATZIM_SHIFT)
                    & (roster["Assigned"].astype(str).map(lambda cell: high_name in _name_list(cell)))
                ].copy()
                high_rows["_weekday"] = high_rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
                for idx, _ in high_rows.sort_values(["_weekday", "Date"], ascending=[False, False]).iterrows():
                    if _try_replace_yoeatzim(idx, high_name, lower_names, require_improved_objective=True):
                        repaired += 1
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                break

        for _ in range(8):
            old_objective = _yoeatzim_balance_objective(pool)
            changed = False
            assigned_rows = roster[roster["Shift"] == YOEATZIM_SHIFT].copy()
            assigned_rows["_weekday"] = assigned_rows["Date"].map(lambda x: date.fromisoformat(str(x)).weekday())
            for idx, row in assigned_rows.sort_values(["_weekday", "Date"], ascending=[False, False]).iterrows():
                for old_name in sorted(
                    [name for name in _name_list(row.Assigned) if name in pool],
                    key=lambda n: (-attending_counts[n], -yoeatzim_counts[n], n),
                ):
                    target_names = {
                        name for name in pool
                        if name != old_name and attending_counts[name] < attending_counts[old_name]
                    }
                    if _try_replace_yoeatzim(
                        idx,
                        old_name,
                        target_names,
                        require_improved_objective=True,
                    ):
                        repaired += 1
                        changed = True
                        break
                if changed:
                    break
            if not changed or _yoeatzim_balance_objective(pool) >= old_objective:
                break
        return repaired

    alternate_credits: list[tuple[str, date, str]] = []
    marked_alternate_credits: set[tuple[str, str, str]] = set()

    def _alternate_credit_key(name: str, earned_date: date, reason: str) -> tuple[str, str, str]:
        return (name, earned_date.isoformat(), reason)

    def _alternate_row_index(d_iso: str) -> int | None:
        mask = (roster["Date"] == d_iso) & (roster["Shift"] == "חלופי")
        if not mask.any():
            return None
        return roster.index[mask][0]

    def _mark_manual_alternate(name: str, earned_date: date, reason: str) -> bool:
        key = _alternate_credit_key(name, earned_date, reason)
        if key in marked_alternate_credits:
            return True

        idx = _alternate_row_index(earned_date.isoformat())
        if idx is None:
            return False

        marker_prefix = "⚠️, לבחור חלופי: "
        existing = str(roster.at[idx, "Assigned"] or "").strip()
        existing_names = list(_names(existing))
        marker_names: list[str] = []

        old_marker_prefix = "⚠️ לבחור חלופי: "
        active_marker_prefix = marker_prefix if marker_prefix in existing else old_marker_prefix
        if active_marker_prefix in existing:
            marker_part = existing.split(active_marker_prefix, 1)[1]
            marker_part = marker_part.split(",", 1)[0]
            marker_names = [x.strip() for x in marker_part.split(" / ") if x.strip()]
            existing = existing.split(active_marker_prefix, 1)[0].rstrip(" ,")

        if name not in marker_names:
            marker_names.append(name)

        marker = marker_prefix + " / ".join(marker_names)
        if existing_names:
            roster.at[idx, "Assigned"] = f"{', '.join(existing_names)}, {marker}"
        else:
            roster.at[idx, "Assigned"] = marker

        logger.info(
            "חלופי manual marker: %s for %s (%s)",
            name, earned_date.isoformat(), reason,
        )
        marked_alternate_credits.add(key)
        return True

    def _build_alternate_credits() -> list[tuple[str, date, str]]:
        credits: list[tuple[str, date, str]] = []
        yr, mon = map(int, month.split("-"))
        first = date(yr, mon, 1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

        cur = first
        while cur <= last:
            if cur.weekday() == 4:
                for name in sorted(_resident_night_names(cur)):
                    credits.append((name, cur, "Friday resident night"))

            elif cur.weekday() == 5:
                sat_names = _resident_night_names(cur)
                friday = cur - timedelta(days=1)
                thursday = cur - timedelta(days=2)
                for name in sorted(sat_names):
                    friday_morning = [
                        s for s in daily_assignments.get(friday, {}).get(name, set())
                        if s not in NIGHT_DUTY_SHIFTS and s not in {"חלופי", "חופש", "אחרי תורנות"}
                    ]
                    thursday_night = bool(
                        daily_assignments.get(thursday, {}).get(name, set()).intersection(RESIDENT_NIGHT_SHIFTS)
                    )
                    if friday_morning:
                        credits.append((name, cur, "Friday morning + Saturday resident night"))
                    if thursday_night:
                        credits.append((name, cur, "Thursday + Saturday resident nights"))

            cur += timedelta(days=1)

        return credits

    def _mark_all_manual_alternates() -> int:
        marked = 0
        for name, earned_date, reason in alternate_credits:
            if _mark_manual_alternate(name, earned_date, reason):
                marked += 1
        return marked

    def _log_unmet_preferred_night_requests() -> None:
        _rebuild_daily_assignments_from_roster()
        for (name, pref_date), strength in sorted(
            preferred_night_requests.items(),
            key=lambda item: (-item[1], 0 if item[0][1].weekday() in (4, 5) else 1, item[0][1], item[0][0]),
        ):
            allowed_shifts = _preferred_night_shifts_for(name)
            assigned_allowed = daily_assignments.get(pref_date, {}).get(name, set()).intersection(allowed_shifts)
            if assigned_allowed:
                continue

            row_notes: list[str] = []
            for shift_type in sorted(allowed_shifts):
                mask = (roster["Date"] == pref_date.isoformat()) & (roster["Shift"] == shift_type)
                if not mask.any():
                    row_notes.append(f"{shift_type}: no row")
                    continue
                idx = int(roster.index[mask][0])
                needed = _to_int(roster.at[idx, "Needed"], 0)
                current = _name_list(roster.at[idx, "Assigned"])
                if needed <= 0:
                    row_notes.append(f"{shift_type}: not required")
                    continue
                if not worker_shift_lut.get((name, shift_type), False):
                    row_notes.append(f"{shift_type}: not capable")
                    continue
                reason = eligibility_reason(name, pref_date.isoformat(), shift_type)
                if reason:
                    row_notes.append(f"{shift_type}: {reason}")
                    continue
                if len(current) >= needed:
                    row_notes.append(f"{shift_type}: full {len(current)}/{needed}")
                else:
                    row_notes.append(f"{shift_type}: open but blocked by live state/swap constraints")

            logger.info(
                "unmet preferred night request: %s %s strength=%d (%s)",
                pref_date.isoformat(), name, strength, "; ".join(row_notes) or "no eligible requested shift",
            )

    def _seed_preferred_night_requests() -> int:
        nonlocal filled_so_far
        seeded = 0
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()

        requests = sorted(
            preferred_night_requests.items(),
            key=lambda item: (-item[1], 0 if item[0][1].weekday() in (4, 5) else 1, item[0][1], item[0][0]),
        )
        for (name, pref_date), strength in requests:
            if daily_assignments.get(pref_date, {}).get(name, set()).intersection(_preferred_night_shifts_for(name)):
                continue

            possible_shifts = [
                shift
                for shift in ("ת.מיון", "ת.מיון 2", KONEN_MION_SHIFT)
                if shift in _preferred_night_shifts_for(name)
                and worker_shift_lut.get((name, shift), False)
            ]
            candidates: list[tuple[tuple, int, str, list[str]]] = []
            for shift_type in possible_shifts:
                mask = (roster["Date"] == pref_date.isoformat()) & (roster["Shift"] == shift_type)
                if not mask.any():
                    continue
                idx = roster.index[mask][0]
                needed = _to_int(roster.at[idx, "Needed"], 0)
                if needed <= 0:
                    continue
                current = _name_list(roster.at[idx, "Assigned"])
                if name in current or len(current) >= needed:
                    continue

                effective_last_night = _last_night_before(pref_date)
                eligible = get_eligible_workers(
                    shift_type=shift_type,
                    shift_date=pref_date,
                    blocked_next_day=blocked_next_day,
                    extra_day_off=extra_day_off,
                    daily_assignments=daily_assignments,
                    blocked_reasons=None,
                    last_night=effective_last_night,
                )
                if name not in eligible:
                    continue
                if shift_type in RESIDENT_NIGHT_SHIFTS:
                    if _resident_adjacent_night_penalty(name, pref_date, daily_assignments) > 0:
                        continue
                    if _resident_night_spacing_penalty(name, pref_date, effective_last_night) >= 100:
                        continue
                    score = (
                        _resident_sandwich_penalty(name, pref_date, daily_assignments),
                        _resident_night_balance_key(name, shift_type, pref_date),
                        _weekend_resident_night_key(name, pref_date),
                        resident_night_shift_counts[shift_type][name],
                    )
                else:
                    score = (
                        _konen_mion_key(name, pref_date),
                        konen_month_counts[name],
                    )
                candidates.append((score, idx, shift_type, current))

            if not candidates:
                continue

            _, idx, shift_type, current = min(candidates, key=lambda item: item[0])
            current.append(name)
            roster.at[idx, "Assigned"] = _write_name_list(current)

            history[name][shift_type] += 1
            daily_assignments[pref_date].setdefault(name, set()).add(shift_type)
            _record_friday_assignment(name, shift_type, pref_date)
            if shift_type in RESIDENT_NIGHT_SHIFTS:
                month_counts[name] += 1
                resident_night_shift_counts[shift_type][name] += 1
                if pref_date.weekday() in (4, 5):
                    weekend_night_counts[name] += 1
                if pref_date.weekday() == 5:
                    saturday_night_counts[name] += 1
                blocked_next_day[name].add(pref_date + timedelta(days=1))
                bump_extra_day_off(name, shift_type, pref_date, extra_day_off)
            elif shift_type == KONEN_MION_SHIFT:
                _record_konen_mion_assignment(name, pref_date)
            if shift_type in NIGHT_DUTY_SHIFTS:
                last_night[name] = pref_date
                _record_preferred_night_assignment(name, shift_type, pref_date)

            filled_so_far += 1
            seeded += 1
            logger.info(
                "preferred night seed: %s %s %s strength=%d",
                pref_date.isoformat(), shift_type, name, strength,
            )

        if seeded:
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
        return seeded

    seeded_preferred_nights = _seed_preferred_night_requests()
    if seeded_preferred_nights:
        logger.info("Seeded %d preferred night-duty assignments", seeded_preferred_nights)

    handled_night_duties = False
    night_shift_order = {"ת.מיון": 0, "ת.מיון 2": 1, KONEN_MION_SHIFT: 2}

    for bucket in PRIORITY_BUCKETS:
        if bucket in NIGHT_DUTY_SHIFTS:
            if handled_night_duties:
                continue
            handled_night_duties = True
            bucket_rows = roster[roster["Shift"].isin(NIGHT_DUTY_SHIFTS)].copy()
            bucket_rows["_shift_order"] = bucket_rows["Shift"].map(night_shift_order)
            row_iter = bucket_rows.sort_values(["Date", "_shift_order"]).iterrows()
        else:
            # (optional) guard against out-of-order rows inside the bucket
            row_iter = roster[roster["Shift"] == bucket].sort_values("Date").iterrows()

        for idx, row in row_iter:
            needed = int(row.Needed)
            soft   = int(row.SoftCap)

            if needed == 0:
                roster.at[idx, "Assigned"] = "-"
                continue

            current     = [x.strip() for x in row.Assigned.split(",") if x.strip()]
            current_set = set(current)

            remaining_hard = max(needed - len(current), 0)
            extra_soft     = max(soft   - needed, 0)      # for מחלקה only
            if remaining_hard == 0 and extra_soft == 0:
                continue

            shift_type = row.Shift
            shift_date = date.fromisoformat(row.Date)

            # DEBUG: entry into this EMG row
            if _is_debug_clinic(shift_type, shift_date):
                logger.debug(
                    "[EMG DEBUG] start assignment loop: Needed=%d SoftCap=%d current=%r",
                    needed, soft, current,
                )

            # Fill hard requirements first. Optional soft-cap extras must not
            # consume eligible people before mandatory shifts like מיון/ייעוצים.
            attempts = remaining_hard
            for attempt_idx in range(attempts):
                effective_last_night = _last_night_before(shift_date)
                elig = get_eligible_workers(
                    shift_type        = shift_type,
                    shift_date        = shift_date,
                    blocked_next_day  = blocked_next_day,
                    extra_day_off     = extra_day_off,     # currently passive
                    daily_assignments = daily_assignments,
                    blocked_reasons   = blocked_reasons,
                    last_night        = effective_last_night,
                )

                # drop already-assigned workers
                elig = [w for w in elig if w not in current_set]
                rotation_elig = [
                    w for w in _rotation_pull_candidates(shift_type, shift_date, current)
                    if w not in elig
                ]
                if rotation_elig:
                    logger.info(
                        "full-month rotation candidates for assignment: %s %s -> %s",
                        shift_date.isoformat(), shift_type, ", ".join(rotation_elig),
                    )
                    elig.extend(rotation_elig)
                elig = [
                    w for w in elig
                    if _personal_under_max(w, shift_type, shift_date)
                ]

                # DEBUG: show elig list per attempt
                if _is_debug_clinic(shift_type, shift_date):
                    logger.debug(
                        "[EMG DEBUG] attempt %d: current=%r elig=%r",
                        attempt_idx + 1, current, elig,
                    )

                if not elig:                               # nothing left to pick
                    logger.debug(
                        "No eligible workers for %s on %s "
                        "(current %d, need %d, soft %d)",
                        shift_type, shift_date.isoformat(),
                        len(current), needed, soft
                    )
                    break

                # ─── choose the pick ────────────────────────────────────────
                if shift_type in ("ת.מיון", "ת.מיון 2"):
                    if shift_date.weekday() == 4:
                        friday_pairable = [
                            w for w in elig
                            if _friday_night_morning_penalty(
                                w, shift_type, shift_date, daily_assignments
                            )[0] < 2
                        ]
                        if friday_pairable:
                            elig = friday_pairable

                    non_sandwich = [
                        w for w in elig
                        if _resident_night_spacing_penalty(w, shift_date, effective_last_night) < 100
                        and _resident_adjacent_night_penalty(w, shift_date, daily_assignments) == 0
                        and _resident_sandwich_penalty(w, shift_date, daily_assignments) == 0
                    ]
                    if non_sandwich:
                        elig = non_sandwich

                    # primary key: how many night duties this month
                    # secondary key: original fairness score (lifetime + recency)
                    pick = min(
                        elig,
                        key=lambda w: (
                            _resident_night_balance_key(w, shift_type, shift_date),
                            _weekend_resident_night_key(w, shift_date),
                            _friday_work_key(w, shift_type, shift_date),
                            _alternate_risk_penalty(w, shift_type, shift_date, daily_assignments),
                            _friday_night_morning_penalty(w, shift_type, shift_date, daily_assignments),
                            _resident_adjacent_night_penalty(w, shift_date, daily_assignments),
                            _resident_sandwich_penalty(w, shift_date, daily_assignments),
                            _resident_night_spacing_penalty(w, shift_date, effective_last_night),
                            fairness_score(w, shift_type, shift_date,
                                        history, effective_last_night),
                        ),
                    )
                elif shift_type == KONEN_MION_SHIFT:
                    if shift_date.weekday() == 4:
                        elig = [
                            w for w in elig
                            if w != SHIMON_NAME or _shimon_friday_available(shift_date)
                        ]
                        if not elig:
                            break
                    friday_attending = {
                        w for w in elig
                        if shift_date.weekday() == 4
                        and _has_shift(daily_assignments, shift_date, w, ATTENDING_SHIFT)
                    }
                    pick = min(
                        elig,
                        key=lambda w: (
                            _konen_mion_key(w, shift_date),
                            0 if friday_attending and w in friday_attending else 1,
                            _friday_work_key(w, shift_type, shift_date),
                            fairness_score(w, shift_type, shift_date,
                                           history, effective_last_night),
                        ),
                    )
                elif shift_type == YOEATZIM_SHIFT:
                    elig = [w for w in elig if _yoeatzim_allowed(w, shift_date)]
                    if not elig:
                        break
                    pick = min(
                        elig,
                        key=lambda w: (
                            _friday_work_key(w, shift_type, shift_date),
                            _yoeatzim_key(w, shift_date),
                        ),
                    )
                elif shift_type == "EEG":
                    elig = [w for w in elig if _eeg_under_cap(w, shift_date)]
                    if not elig:
                        logger.info(
                            "EEG left empty on %s: all eligible workers are at their monthly cap",
                            shift_date.isoformat(),
                        )
                        break
                    pick = min(
                        elig,
                        key=lambda w: (
                            0 if w == "גנדלמן" and _has_shift(daily_assignments, shift_date, w, "EEG ילדים") else 1,
                            _eeg_key(w, shift_date),
                        ),
                    )
                else:
                    preferred_friday_worker = None
                    if shift_date.weekday() == 4 and shift_type in {"מיון", "מחלקה"}:
                        preferred_night = "ת.מיון" if shift_type == "מיון" else "ת.מיון 2"
                        preferred = [
                            w for w in elig
                            if _has_shift(daily_assignments, shift_date, w, preferred_night)
                        ]
                        if preferred:
                            preferred_friday_worker = set(preferred)

                    pick = min(
                        elig,
                        key=lambda w: (
                            0 if shift_date.weekday() == 4 and shift_type == ATTENDING_SHIFT and _has_shift(daily_assignments, shift_date, w, KONEN_MION_SHIFT) else 1,
                            0 if shift_type == "EEG" and w == "גנדלמן" and _has_shift(daily_assignments, shift_date, w, "EEG ילדים") else 1,
                            _personal_rule_key(w, shift_type, shift_date),
                            0 if preferred_friday_worker and w in preferred_friday_worker else 1,
                            _friday_work_key(w, shift_type, shift_date),
                            fairness_score(w, shift_type, shift_date, history, effective_last_night),
                        ),
                    )

                # DEBUG: chosen pick for our clinic
                if _is_debug_clinic(shift_type, shift_date):
                    logger.debug("[EMG DEBUG] pick -> %s", pick)

                # ─── accept the pick ───────────────────────────────────────
                current.append(pick)
                current_set.add(pick)
                _pull_from_full_month_rotation(shift_type, shift_date, pick)
                filled_so_far += 1
                if total_slots:
                    assignment_pct = 30 + int(min(filled_so_far, total_slots) * 25 / total_slots)
                    report_progress(assignment_pct, assignment_progress_label(shift_type))

                history[pick][shift_type] += 1
                personal_assignment_counts[(pick, shift_type)] += 1
                if shift_type in ("ת.מיון", "ת.מיון 2"):
                    month_counts[pick] += 1          # track month-so-far load
                    resident_night_shift_counts[shift_type][pick] += 1
                    if shift_date.weekday() in (4, 5):
                        weekend_night_counts[pick] += 1
                    if shift_date.weekday() == 5:
                        saturday_night_counts[pick] += 1
                    if shift_date.weekday() == 3:
                        thursday_night_counts[pick] += 1
                elif shift_type == KONEN_MION_SHIFT:
                    _record_konen_mion_assignment(pick, shift_date)    # track month-so-far senior on-call load
                elif shift_type == YOEATZIM_SHIFT:
                    _record_yoeatzim_assignment(pick, shift_date)
                elif shift_type == ATTENDING_SHIFT:
                    attending_counts[pick] += 1
                elif shift_type == "EEG":
                    eeg_counts[pick] += 1
                if shift_type in NIGHT_DUTY_SHIFTS:
                    _record_preferred_night_assignment(pick, shift_type, shift_date)

                daily_assignments[shift_date].setdefault(pick, set()).add(shift_type)
                _record_friday_assignment(pick, shift_type, shift_date)
                if shift_type in NIGHT_DUTY_SHIFTS:
                    last_night[pick] = shift_date

                if shift_type in ("ת.מיון", "ת.מיון 2"):
                    blocked_next_day[pick].add(shift_date + timedelta(days=1))
                    bump_extra_day_off(pick, shift_type, shift_date, extra_day_off)

                if len(current) >= soft:             # reached soft cap → stop
                    break

            # -------- guard: never exceed soft cap ----------
            if len(current) > soft:                  # <-- shouldn’t happen
                logger.error("%s %s over-filled (%d>%d)",
                            row.Date, row.Shift, len(current), soft)
                current = current[:soft]             # hard trim

            # DEBUG: final state of this clinic row
            if _is_debug_clinic(shift_type, shift_date):
                logger.debug(
                    "[EMG DEBUG] final current=%r len=%d Needed=%d SoftCap=%d",
                    current, len(current), needed, soft,
                )

            # write back the final list
            if len(current) < needed:
                warn = f"⚠️, {len(current)}/{needed}"
                roster.at[idx, "Assigned"] = (
                    f"{warn}, " + ", ".join(current) if current
                    else f"{warn}, Needs manual pick"
                )
            elif current:
                roster.at[idx, "Assigned"] = ", ".join(current)

    report_progress(56, "מאזן תורנויות")
    resident_fairness_repairs = _repair_resident_night_fairness(
        rounds=4,
        weekend_steps=32,
        type_steps=4,
        thursday_steps=3,
    )
    if resident_fairness_repairs:
        logger.info("Resident night fairness repair changed %d assignments", resident_fairness_repairs)
    report_progress(76, "מצמיד שישי לתורנויות")
    repaired_friday_pairings = _repair_friday_pairings()
    if repaired_friday_pairings:
        logger.info("Friday duty/day pairing repair changed %d assignments", repaired_friday_pairings)
    report_progress(78, "מאזן ימי שישי")
    repaired_fridays = _repair_friday_day_balance()
    if repaired_fridays:
        logger.info("Friday day balance repair changed %d assignments", repaired_fridays)
    final_resident_fairness_repairs = _repair_resident_night_fairness(
        rounds=2,
        weekend_steps=24,
        type_steps=2,
        thursday_steps=2,
    )
    if final_resident_fairness_repairs:
        logger.info("Final resident night fairness repair changed %d assignments", final_resident_fairness_repairs)
    after_duty_removed = _resolve_after_duty_conflicts()
    if after_duty_removed:
        logger.info("After-duty cleanup removed %d assignments", after_duty_removed)
    report_progress(82, "משלים שיבוצים חסרים")
    refilled_hard_rows = _refill_hard_rows_after_cleanup()
    if refilled_hard_rows:
        logger.info("Final hard-row refill filled %d assignments", refilled_hard_rows)
    repaired_konen = _repair_konen_mion_balance()
    if repaired_konen:
        logger.info("Senior on-call balance repair changed %d assignments", repaired_konen)

    print(f"    -> bucket done ({filled_so_far}/{total_slots} shifts filled)")
    report_progress(84, "בודק מרפאות מול אטנדינג")

    # ───── final clinic-mute pass (dynamic attendings) ─────
    for idx, row in roster[roster["Shift"] == ATTENDING_SHIFT].iterrows():
        docs = [n.strip() for n in row.Assigned.split(",") if n.strip()]
        if not docs:
            continue

        d   = date.fromisoformat(row.Date)
        heb = ISO2HEB[d.isoweekday() - 1]

        for doc in docs:
            for cl in fixed_clinic_lut().get((doc, heb), set()):
                if _required_clinic_row(d, cl):
                    continue
                mask = (roster["Date"] == row.Date) & (roster["Shift"] == cl)
                if mask.any():
                    clinic_idx = roster.index[mask][0]
                    clinic_names = _name_list(roster.at[clinic_idx, "Assigned"])
                    if any((d, cl, name) in fixed_assignment_keys for name in clinic_names):
                        continue
                    roster.loc[mask, ["Needed", "SoftCap", "Assigned"]] = [0, 0, "-"]

    # ───── mute a fixed clinic when its doctor is fully "לא זמין" ─────
    
    unavail = unavail_lookup()                      # {(name, date): [(block, src), …]}
    yr, mon = map(int, month.split("-"))
    first   = date(yr, mon, 1)
    last    = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    for (doc, heb_day), clinics in fixed_clinic_lut().items():
        for d in (first + timedelta(days=i) for i in range((last - first).days + 1)):
            if ISO2HEB[d.isoweekday() - 1] != heb_day:
                continue

            # doctor marked fully unavailable (source ≠ "מרפאה קבועה")
            blocks = unavail.get((doc, d.isoformat()), [])
            if any(bt in BLOCKS_ALL and src != "מרפאה קבועה" for bt, src in blocks):
                for cl in clinics:
                    if _required_clinic_row(d, cl):
                        continue
                    mask = (roster["Date"] == d.isoformat()) & (roster["Shift"] == cl)
                    if mask.any():
                        roster.loc[mask, ["Needed", "SoftCap", "Assigned"]] = [0, 0, "-"]

    report_progress(86, "מנקה התנגשויות")
    for cleanup_round in range(3):
        removed_conflicts = _resolve_same_day_conflicts()
        if not removed_conflicts:
            break
        logger.info(
            "same-day conflict cleanup round %d removed %d assignments",
            cleanup_round + 1,
            removed_conflicts,
        )
        _rebuild_live_counters_from_roster()
        refilled = _refill_hard_rows_after_cleanup()
        logger.info(
            "same-day conflict cleanup round %d refilled %d hard slots",
            cleanup_round + 1,
            refilled,
        )
        if not refilled:
            break

    # ---- enforce גנדלמן -> EEG ילדים same day after cleanup so it cannot be removed by it.
    report_progress(87, "מתקן EEG ואפילפסיה")
    roster = enforce_epilepsy_eeg_coupling(roster, daily_assignments=daily_assignments)
    for cleanup_round in range(2):
        removed_conflicts = _resolve_same_day_conflicts()
        if not removed_conflicts:
            break
        logger.info(
            "post-coupling same-day conflict cleanup round %d removed %d assignments",
            cleanup_round + 1,
            removed_conflicts,
        )
        _rebuild_live_counters_from_roster()
        refilled = _refill_hard_rows_after_cleanup()
        logger.info(
            "post-coupling same-day conflict cleanup round %d refilled %d hard slots",
            cleanup_round + 1,
            refilled,
        )
        roster = enforce_epilepsy_eeg_coupling(roster, daily_assignments=daily_assignments)
    _resolve_same_day_conflicts()
    report_progress(88, "משלים אחרי ניקוי")
    final_refilled = _refill_hard_rows_after_cleanup()
    if final_refilled:
        logger.info("Post-cleanup hard-row refill filled %d assignments", final_refilled)

    # Cleanup/refill/coupling can move the roster away from the best resident-night
    # balance. Run the resident repairs again on the final shape so obvious legal
    # weekend swaps are not lost late in the pipeline.
    report_progress(89, "מאזן תורנויות אחרי ניקוי (כולל חמישי)")
    post_cleanup_resident_fairness = _repair_resident_night_fairness(
        rounds=3,
        weekend_steps=32,
        type_steps=3,
        thursday_steps=3,
    )
    if post_cleanup_resident_fairness:
        logger.info("Post-cleanup resident night fairness repair changed %d assignments", post_cleanup_resident_fairness)
    report_progress(92, "מצמיד ומאזן ימי שישי")
    final_friday_pairings = _repair_friday_pairings()
    if final_friday_pairings:
        logger.info("Post-cleanup Friday duty/day pairing repair changed %d assignments", final_friday_pairings)
    final_friday_balance = _repair_friday_day_balance(max_steps=8)
    if final_friday_balance:
        logger.info("Post-cleanup Friday day balance repair changed %d assignments", final_friday_balance)
    final_after_duty_removed = _resolve_after_duty_conflicts()
    if final_after_duty_removed:
        logger.info("Post-cleanup after-duty cleanup removed %d assignments", final_after_duty_removed)
    final_refilled_after_duty = _refill_hard_rows_after_cleanup()
    if final_refilled_after_duty:
        logger.info("Post-cleanup after-duty refill filled %d assignments", final_refilled_after_duty)

    report_progress(93, "מאזן כוננויות")
    final_repaired_konen = _repair_konen_mion_balance()
    if final_repaired_konen:
        logger.info("Post-cleanup senior on-call balance repair changed %d assignments", final_repaired_konen)
    final_konen_friday_pairings = _repair_friday_pairings()
    if final_konen_friday_pairings:
        logger.info("Post-konen Friday duty/day pairing repair changed %d assignments", final_konen_friday_pairings)
    report_progress(94, "מאזן ייעוצים")
    final_repaired_yoeatzim = _repair_yoeatzim_balance()
    if final_repaired_yoeatzim:
        logger.info("Post-cleanup senior consult balance repair changed %d assignments", final_repaired_yoeatzim)
    report_progress(95, "משלים אחרי איזון")
    final_refilled_after_yoeatzim = _refill_hard_rows_after_cleanup()
    if final_refilled_after_yoeatzim:
        logger.info("Post-consult hard-row refill filled %d assignments", final_refilled_after_yoeatzim)
    final_post_refill_yoeatzim = _repair_yoeatzim_balance()
    if final_post_refill_yoeatzim:
        logger.info("Post-refill senior consult balance repair changed %d assignments", final_post_refill_yoeatzim)
    final_post_consult_friday_pairings = _repair_friday_pairings()
    if final_post_consult_friday_pairings:
        logger.info("Post-consult Friday duty/day pairing repair changed %d assignments", final_post_consult_friday_pairings)
    report_progress(96, "מאזן תורנויות סופי (כולל חמישי)")
    final_last_resident_fairness = _repair_resident_night_fairness(
        rounds=3,
        weekend_steps=32,
        type_steps=2,
        thursday_steps=2,
    )
    if final_last_resident_fairness:
        logger.info("Final resident night fairness repair changed %d assignments", final_last_resident_fairness)
    final_mandatory_personal = _apply_mandatory_personal_rules()
    final_companion_personal = _apply_companion_personal_rules()
    if final_mandatory_personal:
        logger.info("Final mandatory personal rules placed %d assignments", final_mandatory_personal)
    if final_companion_personal:
        logger.info("Final companion personal rules added %d assignments", final_companion_personal)
    report_progress(96, "מסמן שיבוצים חסרים")
    final_missing_rows = _mark_missing_required_rows()
    if final_missing_rows:
        logger.info("Marked %d underfilled required rows", final_missing_rows)

    alternate_credits = _build_alternate_credits()
    manual_alternates = _mark_all_manual_alternates()
    logger.info(
        "חלופי manual markers complete: %d/%d earned days marked",
        manual_alternates, len(alternate_credits),
    )
    _log_unmet_preferred_night_requests()

    # DEBUG: blocked reasons for our clinic date (if any)
    for key, reason in blocked_reasons.items():
        # key may be (date, shift, name) or (date, name)
        if len(key) == 3:
            d, sh, name = key
            if d == DEBUG_CLINIC_DATE and sh == DEBUG_CLINIC_SHIFT:
                logger.debug("[EMG DEBUG] blocked_reasons: name=%s reason=%s", name, reason)
        elif len(key) == 2:
            d, name = key
            if d == DEBUG_CLINIC_DATE:
                logger.debug("[EMG DEBUG] blocked_reasons (no-shift): name=%s reason=%s", name, reason)

    # ------------------------------------------------------------------
    # 7.  WRITE “unassigned” ledger  (idle workers per day, reasons)
    #      – Residents first (A-Z), then seniors/others (A-Z)
    # ------------------------------------------------------------------
    report_progress(97, "מסכם עובדים פנויים")
    print("[auto-assign] Writing unassigned ledger …")
    write_unassigned_ledger(
         month             = month,
         blocked_reasons   = blocked_reasons,
         daily_assignments = daily_assignments,
         workers           = workers_df()["שם"].tolist(),
         is_senior_fn      = lambda w: is_senior(w, can_do()),
         log_dir           = LOG_DIR,
     )
 
    report_progress(98, "סידור מוכן לייצוא")
    return roster

# ──────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("__main__ in assign2.py")
    import argparse
    from core.export import export_month_to_xlsx, export_week_to_xlsx

    p = argparse.ArgumentParser(description="Auto-assign roster and export XLSX")
    p.add_argument("month", help="YYYY-MM  (e.g. 2025-08)")
    p.add_argument("--week-start",
                   help="ISO date for a single-week export. "
                        "If omitted, the entire month is exported.")
    p.add_argument("--out-dir",
                   default=r"C:\Users\shlom\Google Drive\Neurology\Projects\Neuro Shift\neuroshift-py\output_roster",
                   help="Destination folder for XLSX (default: output_roster)")
    p.add_argument("--dry-run", action="store_true",
                   help="Do NOT push anything to Google Sheets")
    ns = p.parse_args()

    roster = auto_assign(ns.month, dry_run=ns.dry_run)

    if ns.week_start:
        path = export_week_to_xlsx(roster,
                                   week_start=ns.week_start,
                                   out_dir=ns.out_dir)
    else:
        path = export_month_to_xlsx(roster,
                                    month=ns.month,
                                    out_dir=ns.out_dir)

    print("[v] Roster exported to", path)
    print("Done")
