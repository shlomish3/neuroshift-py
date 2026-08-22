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
from typing import Callable, Dict, Mapping, NamedTuple, Set

import hashlib
import pandas as pd
import logging
import sys
import os
import re
import time

from core import constants
from core.constants import PRIORITY_BUCKETS, NIGHT_DUTY_SHIFTS, DUAL_OK
from core.clinic_calendar import build_clinic_needs, build_clinic_owners
from core.data import backend_tables, _sh, _backend_tables_cached, _sh_by_id, _gc, _creds         # , save_roster
from core.eligibility2 import get_eligible_workers as _get_eligible_workers, eligibility_reason, has_clinic_shift  # public API
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
from core.availability_simple_parser import (
    preferred_night_dates_from_simple,
    submitted_names_from_simple,
)
from core.holiday_utils import (
    effective_weekday_letter,
    holiday_eve_names_from_tables,
    holiday_names_from_tables,
)
from core.scheduling_exceptions import (
    ResidentConsecutiveNightException,
    derive_fixed_resident_consecutive_night_exceptions,
    resident_consecutive_night_allowed,
    serialize_resident_consecutive_night_exceptions,
)

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


class ResidentNightMetrics(NamedTuple):
    """Resident-duty objectives in their protected project priority order."""

    missing: int
    total: tuple[int, int]
    weekend_friday: tuple[int, int, int, int]
    saturday: tuple[int, int]
    sandwich_total: int
    sandwich_distribution: tuple[int, int]
    shift_type: tuple[int, int, int, int, int, int]


class ResidentNightObjective(NamedTuple):
    core: ResidentNightMetrics
    preferred: tuple[int, ...]
    personal: int
    thursday: tuple[int, int]
    history: tuple[int, int, int, int, int, int]


RESIDENT_PRIORITY_STAGES = (
    "missing",
    "total",
    "weekend_friday",
    "saturday",
    "sandwich_total",
    "sandwich_distribution",
    "shift_type",
)

RESIDENT_PRIORITY_LABELS_HE = {
    "missing": "מילוי תורנויות חסרות",
    "total": "איזון סך התורנויות",
    "weekend_friday": "איזון סופי שבוע וימי שישי",
    "saturday": "איזון שבתות",
    "sandwich_total": "צמצום מספר הסנדוויצ'ים",
    "sandwich_distribution": "חלוקת הסנדוויצ'ים",
    "shift_type": "איזון ת.מיון מול ת.מיון 2",
}

RotationPullBalanceKey = tuple[int, int, int, int, int, str]
RotationReservePickPrefix = tuple[int, int, int, int, int, int, str]


def _rotation_reserve_pick_prefix(
    is_rotation_reserve: bool,
    balance_key: RotationPullBalanceKey | None = None,
) -> RotationReservePickPrefix:
    """Keep fixed rotation workers behind every ordinary legal candidate.

    Rotation balancing is meaningful only after the ordinary candidate pool is
    exhausted.  The leading flag guarantees that even a balance-improving pull
    from ``רוטציה`` cannot outrank a non-rotation worker.
    """

    if not is_rotation_reserve:
        return (0, 0, 0, 0, 0, 0, "")
    if balance_key is None:
        raise ValueError("Rotation reserve candidates require a balance key")
    return (1, *balance_key)


def _preferred_request_removal_penalty(strength: int, weekday: int) -> int:
    """Rank equally valid request removals without protecting them from the core.

    Starred requests remain stronger than regular requests.  Within the same
    strength, Friday/Saturday requests are more costly to remove than weekday
    requests.  Callers use this only to order alternatives; protected resident
    priorities still decide whether a repair is accepted.
    """

    if strength <= 0:
        return 0
    base = 100 if strength >= 2 else 30
    weekend_bonus = 40 if weekday in (4, 5) else 0
    return base + weekend_bonus


def _request_approval_percentage_basis_points(fulfilled: int, total: int) -> int:
    """Return an integer request-approval percentage for stable comparisons."""

    if total <= 0:
        return 0
    return max(0, fulfilled) * 10_000 // total


def _preferred_request_removal_order_cost(
    base_penalty: int,
    other_approval_percentage: int,
) -> int:
    """Order equal-class removals by the worker's other approved requests.

    Request strength and weekend status remain the dominant request guidance.
    Within that class, an assignment belonging to a worker who already has a
    higher percentage of their other requests fulfilled is cheaper to remove.
    """

    if base_penalty <= 0:
        return 0
    return base_penalty * 10_001 - max(0, other_approval_percentage)


def _preferred_request_competition_key(
    request_priority: tuple[object, ...],
    projected_core: tuple[object, ...],
    other_approval_percentage: int,
    history_key: tuple[object, ...],
    jitter: int,
    name: str,
) -> tuple[object, ...]:
    """Tie-break competing requests after the protected resident objectives."""

    return (
        request_priority,
        projected_core,
        other_approval_percentage,
        history_key,
        jitter,
        name,
    )


def _preferred_seed_block_cause(blocks: list[str]) -> dict[str, str]:
    """Choose the causal seed failure without overstating fixed capacity.

    A fixed assignment is the cause only when no compatible resident-night
    row was instead taken by an earlier preferred request.  In a mixed
    two-slot date, the earlier request is the counterfactual cause of losing
    the remaining non-fixed slot.
    """
    if not blocks:
        return {
            "reason_code": "diagnostic",
            "block": "no-seed-candidate",
        }
    if all(block.startswith("availability:") for block in blocks):
        return {
            "reason_code": "unavailable",
            "block": blocks[0],
        }

    causal_order = (
        "capability",
        "tomorrow-fixed-clinic",
        "tomorrow-fixed-night",
        "tomorrow-clinic",
        "adjacent-fixed-night",
        "adjacent-history-night",
        "same-day-resident-night",
        "resident-daily-limit",
        "illegal-same-day-pair",
        "adjacent-earlier-preference",
        "earlier-preference-filled-slot",
        "fixed-slot-full",
        "slot-full",
        "no-row",
        "not-required",
        "live-state",
    )
    chosen = next(
        (block for block in causal_order if block in blocks),
        blocks[0],
    )
    hard_blocks = {
        "capability",
        "tomorrow-fixed-clinic",
        "tomorrow-fixed-night",
        "tomorrow-clinic",
        "adjacent-fixed-night",
        "adjacent-history-night",
        "same-day-resident-night",
        "resident-daily-limit",
        "illegal-same-day-pair",
        "fixed-slot-full",
    }
    if chosen in {"adjacent-earlier-preference", "earlier-preference-filled-slot"}:
        reason_code = "request_competition"
    elif chosen in hard_blocks:
        reason_code = "hard_rule"
    else:
        reason_code = "diagnostic"
    return {
        "reason_code": reason_code,
        "block": chosen,
    }


def _resident_balance_scope(
    stage: str,
    before: tuple[int, int, int, int, int, int],
    after: tuple[int, int, int, int, int, int],
) -> str:
    """Say whether a protected repair reduced this resident's own burden."""

    stage_indexes = {
        "total": (0,),
        "weekend_friday": (1, 2),
        "saturday": (3,),
        "sandwich_total": (4,),
        "sandwich_distribution": (4,),
        "shift_type": (5,),
    }
    indexes = stage_indexes.get(stage, ())
    return "self" if any(after[index] < before[index] for index in indexes) else "others"


def _adjacent_fixed_night_resolution(
    *,
    previous_is_fixed: bool,
    current_is_fixed: bool,
    exception_allowed: bool,
) -> str:
    """Resolve an adjacent-night conflict without sacrificing a later fixed duty."""

    if exception_allowed:
        return "keep_both"
    if current_is_fixed and not previous_is_fixed:
        return "remove_previous"
    return "remove_current"


def _resident_core_key(metrics: ResidentNightMetrics) -> tuple[object, ...]:
    return tuple(getattr(metrics, field) for field in RESIDENT_PRIORITY_STAGES)


def _resident_stage_improves(
    before: ResidentNightMetrics,
    after: ResidentNightMetrics,
    stage: str,
) -> bool:
    """Return true when ``stage`` improves without worsening an earlier stage."""

    try:
        stage_index = RESIDENT_PRIORITY_STAGES.index(stage)
    except ValueError as exc:
        raise ValueError(f"Unknown resident priority stage: {stage}") from exc

    for protected_stage in RESIDENT_PRIORITY_STAGES[:stage_index]:
        if getattr(after, protected_stage) > getattr(before, protected_stage):
            return False
    return getattr(after, stage) < getattr(before, stage)


def _resident_core_equal_through_stage(
    before: ResidentNightMetrics,
    after: ResidentNightMetrics,
    stage: str,
) -> bool:
    """Compare the protected core only through ``stage``.

    Preferred-request recovery runs immediately after each balancing stage.
    At that point lower stages have not earned protection yet, so they may
    change, but the completed stage and every higher stage must stay exactly
    equal.
    """

    try:
        stage_index = RESIDENT_PRIORITY_STAGES.index(stage)
    except ValueError as exc:
        raise ValueError(f"Unknown resident priority stage: {stage}") from exc

    protected = RESIDENT_PRIORITY_STAGES[: stage_index + 1]
    return all(
        getattr(after, protected_stage) == getattr(before, protected_stage)
        for protected_stage in protected
    )


def _resident_core_preserved_for_request_recovery(
    before: ResidentNightMetrics,
    after: ResidentNightMetrics,
    protect_through_stage: str | None,
) -> bool:
    """Enforce either a completed-stage prefix or the complete final core."""

    if protect_through_stage is None:
        return _resident_core_key(after) == _resident_core_key(before)
    return _resident_core_equal_through_stage(
        before,
        after,
        protect_through_stage,
    )


def _resident_history_tiebreak_improves(
    before: ResidentNightObjective,
    after: ResidentNightObjective,
) -> bool:
    """Allow history only after the core and request outcome both tie."""

    return (
        after.core == before.core
        and after.preferred == before.preferred
        and after.history < before.history
    )


def _resident_flexible_comparison_pool(
    active_names: Set[str],
    assignment_keys: Set[tuple[date, str, str]],
    protected_keys: Set[tuple[date, str, str]],
    receivable_names: Set[str],
) -> Set[str]:
    """Return residents whose monthly duty load can still be changed legally.

    A resident with only protected assignments and no legal capacity for any
    movable slot is deliberately outside fairness and history comparisons. The
    protected assignments remain in the roster; they simply cannot distort the
    optimization target for residents whose loads are actually adjustable.
    """

    movable_names = {
        name
        for assignment_date, shift, name in assignment_keys
        if (assignment_date, shift, name) not in protected_keys
    }
    return set(active_names).intersection(movable_names | set(receivable_names))


def _first_improved_resident_stage(
    before: ResidentNightMetrics,
    after: ResidentNightMetrics,
) -> str | None:
    """Return the first protected priority that improved in lexicographic order."""

    for stage in RESIDENT_PRIORITY_STAGES:
        before_value = getattr(before, stage)
        after_value = getattr(after, stage)
        if after_value == before_value:
            continue
        return stage if after_value < before_value else None
    return None


def _resident_saturday_swap_gain(
    a_weekday: int,
    a_name: str,
    b_weekday: int,
    b_name: str,
    saturday_counts: Mapping[str, int],
) -> tuple[str, str, int] | None:
    """Return (Saturday assignee, other assignee, potential gain) for a cross-class swap."""

    a_is_saturday = a_weekday == 5
    b_is_saturday = b_weekday == 5
    if a_is_saturday == b_is_saturday:
        return None
    saturday_name = a_name if a_is_saturday else b_name
    other_name = b_name if a_is_saturday else a_name
    gain = int(saturday_counts.get(saturday_name, 0)) - int(
        saturday_counts.get(other_name, 0)
    )
    return saturday_name, other_name, gain


def _weekend_konen_target_names_for_row(
    original: list[str],
    fixed_names: Set[str],
    candidate: str,
    *,
    needed: int,
    soft_cap: int,
) -> list[str] | None:
    """Plan one weekend on-call row without removing fixed staff or exceeding capacity."""

    if needed <= 0 or soft_cap <= 0:
        return None
    target = [name for name in original if name in fixed_names]
    target = list(dict.fromkeys(target))
    if candidate not in target:
        target.append(candidate)
    if len(target) > soft_cap:
        return None

    target_count = min(soft_cap, max(needed, len(target)))
    for name in original:
        if name not in target and len(target) < target_count:
            target.append(name)
    return target


def _senior_friday_assignment_priority(
    projected_fridays: int,
    pairing_rank: int,
) -> tuple[int, int, int]:
    """Keep a senior at one Friday before rewarding same-day pairings."""

    return (
        0 if projected_fridays <= 1 else 2,
        projected_fridays,
        pairing_rank,
    )


def _friday_count_objective(
    counts: Mapping[str, int],
    pool: Set[str],
) -> tuple[int, int]:
    """Return the Friday spread and square load for a selected worker pool."""

    if not pool:
        return (0, 0)
    values = [int(counts.get(name, 0)) for name in pool]
    return (max(values) - min(values), sum(value * value for value in values))

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
    preferred_night_requests: Mapping[tuple[str, date], int] | None = None,
) -> int:
    prev = last_night.get(name)
    if not prev:
        return 0
    gap = (shift_date - prev).days
    if gap == 2:
        if _is_requested_resident_sandwich(
            name,
            prev,
            shift_date,
            preferred_night_requests,
        ):
            return 0
        return 100
    if gap == 3:
        return 1
    return 0


def _is_requested_resident_sandwich(
    name: str,
    first: date,
    second: date,
    preferred_night_requests: Mapping[tuple[str, date], int] | None,
) -> bool:
    """Return whether both endpoints of a D/D+2 pair were requested.

    Such a pair remains subject to all hard rules and protected priorities
    above sandwich balancing.  It is merely not treated as an avoidable
    sandwich by the lower sandwich objectives.
    """
    if second - first != timedelta(days=2) or not preferred_night_requests:
        return False
    return bool(
        preferred_night_requests.get((name, first), 0)
        and preferred_night_requests.get((name, second), 0)
    )


def _resident_sandwich_penalty(
    name: str,
    shift_date: date,
    daily_assignments: Dict[date, Dict[str, Set[str]]],
    historical_resident_nights: Dict[str, Set[date]] | None = None,
    preferred_night_requests: Mapping[tuple[str, date], int] | None = None,
) -> int:
    def has_resident_night(d: date) -> bool:
        return (
            bool(daily_assignments.get(d, {}).get(name, set()).intersection(RESIDENT_NIGHT_SHIFTS))
            or d in (historical_resident_nights or {}).get(name, set())
        )

    penalty = 0
    previous_endpoint = shift_date - timedelta(days=2)
    if (
        has_resident_night(previous_endpoint)
        and not has_resident_night(shift_date - timedelta(days=1))
        and not _is_requested_resident_sandwich(
            name,
            previous_endpoint,
            shift_date,
            preferred_night_requests,
        )
    ):
        penalty += 100
    next_endpoint = shift_date + timedelta(days=2)
    if (
        has_resident_night(next_endpoint)
        and not has_resident_night(shift_date + timedelta(days=1))
        and not _is_requested_resident_sandwich(
            name,
            shift_date,
            next_endpoint,
            preferred_night_requests,
        )
    ):
        penalty += 100
    return penalty


def _resident_sandwich_pairs_from_dates(
    by_name: Dict[str, Set[date]],
    *,
    movable_dates: Set[date] | None = None,
) -> list[tuple[str, date, date]]:
    """Return resident-night pairs separated by one night off.

    If ``movable_dates`` is supplied, previous-month-only pairs stay out of
    repair searches while cross-month pairs remain visible.
    """
    sandwiches: list[tuple[str, date, date]] = []
    for name, night_dates in by_name.items():
        for first in sorted(night_dates):
            second = first + timedelta(days=2)
            middle = first + timedelta(days=1)
            if second not in night_dates or middle in night_dates:
                continue
            if movable_dates is not None and first not in movable_dates and second not in movable_dates:
                continue
            sandwiches.append((name, first, second))
    return sandwiches


def _resident_actionable_sandwich_pairs_from_dates(
    by_name: Dict[str, Set[date]],
    *,
    movable_dates: Set[date] | None = None,
    preferred_night_requests: Mapping[tuple[str, date], int] | None = None,
) -> list[tuple[str, date, date]]:
    """Return sandwich pairs that the sandwich stages may try to remove."""
    return [
        (name, first, second)
        for name, first, second in _resident_sandwich_pairs_from_dates(
            by_name,
            movable_dates=movable_dates,
        )
        if not _is_requested_resident_sandwich(
            name,
            first,
            second,
            preferred_night_requests,
        )
    ]


def _resident_requested_sandwich_endpoints_from_dates(
    by_name: Dict[str, Set[date]],
    preferred_night_requests: Mapping[tuple[str, date], int] | None,
) -> Set[tuple[str, date]]:
    """Return assigned endpoints belonging to an acceptable requested pair."""
    return {
        (name, endpoint)
        for name, first, second in _resident_sandwich_pairs_from_dates(by_name)
        if _is_requested_resident_sandwich(
            name,
            first,
            second,
            preferred_night_requests,
        )
        for endpoint in (first, second)
    }


def _resident_type_excess_gap(total: int, tmion: int, tmion2: int) -> int:
    """Gap beyond the mathematically unavoidable parity difference."""
    return max(0, abs(tmion - tmion2) - (total % 2))


def _resident_type_compensation_distance(
    total: int,
    tmion: int,
    tmion2: int,
    burden: int,
) -> int:
    """Distance from a balanced split, leaning odd totals to ת.מיון 2."""
    if burden <= 0:
        return 0
    desired_delta = -1 if total % 2 else 0
    return burden * abs((tmion - tmion2) - desired_delta)


def _resident_adjacent_night_penalty_base(
    name: str,
    shift_date: date,
    daily_assignments: Dict[date, Dict[str, Set[str]]],
    allowed_consecutive_resident_nights: Set[ResidentConsecutiveNightException] | None = None,
) -> int:
    for d in (shift_date - timedelta(days=1), shift_date + timedelta(days=1)):
        if daily_assignments.get(d, {}).get(name, set()).intersection(RESIDENT_NIGHT_SHIFTS):
            first, second = sorted((shift_date, d))
            if resident_consecutive_night_allowed(
                allowed_consecutive_resident_nights,
                name,
                first,
                second,
            ):
                continue
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
    last_reported_percent = 0

    def report_progress(percent: int, label: str) -> None:
        nonlocal last_reported_percent
        percent = max(last_reported_percent, min(100, max(0, percent)))
        last_reported_percent = percent
        if progress_callback:
            progress_callback(percent, label)

    def timed_repair(label: str, fn):
        started = time.perf_counter()
        result = fn()
        logger.info("Timing %s: %.1fs result=%s", label, time.perf_counter() - started, result)
        return result

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
    report_progress(1, "מרענן נתונים")
    print(f"[auto-assign] Reloading sheets for {month} …")
    _clear_sheet_caches()
    print("[auto-assign] Caches cleared")
    tbl = backend_tables()
    print("[auto-assign] Sheets loaded")
    report_progress(3, "טוען אילוצים")
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
    try:
        availability_submitters = submitted_names_from_simple(
            tbl.get("requests", pd.DataFrame())
        )
    except Exception as e:
        logger.warning("Failed to identify availability-form submitters: %s: %r", type(e).__name__, e)
        availability_submitters = set()

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
    previous_senior_night_counts = Counter()
    previous_senior_weekend_counts = Counter()
    previous_resident_night_dates: Dict[str, Set[date]] = defaultdict(set)
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
    raw_resident_night_counts = Counter()
    raw_resident_weekend_counts = Counter()
    raw_senior_night_counts = Counter()
    raw_senior_weekend_counts = Counter()
    if not hist_df.empty and {"Date", "Name", "Shift"}.issubset(hist_df.columns):
        for _, r in hist_df.iterrows():
            hist_date = _parse_history_date(r["Date"])
            if hist_date is None or not (prev_month_first <= hist_date <= prev_day):
                continue
            name = str(r["Name"]).strip()
            shift = str(r["Shift"]).strip()
            if name == SHIMON_NAME and hist_date.weekday() == 4:
                previous_shimon_friday = True
            if name and shift in RESIDENT_NIGHT_SHIFTS:
                previous_resident_night_dates[name].add(hist_date)
                raw_resident_night_counts[name] += 1
                if hist_date.weekday() in (4, 5):
                    raw_resident_weekend_counts[name] += 1
            elif name and shift == KONEN_MION_SHIFT:
                raw_senior_night_counts[name] += 1
                if hist_date.weekday() in (4, 5):
                    raw_senior_weekend_counts[name] += 1

    if raw_resident_night_counts:
        previous_resident_night_counts.clear()
        previous_resident_night_counts.update(raw_resident_night_counts)
        previous_resident_weekend_counts.clear()
        previous_resident_weekend_counts.update(raw_resident_weekend_counts)
    previous_senior_night_counts.update(raw_senior_night_counts)
    previous_senior_weekend_counts.update(raw_senior_weekend_counts)

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
                previous_resident_night_dates[name].add(hist_date)
                if name and name not in previous_after_duty_names:
                    previous_after_duty_names.append(name)
                blocked_next_day[name].add(first_day)
                daily_assignments[hist_date].setdefault(name, set()).add(shift)
                bump_extra_day_off(name, shift, hist_date, extra_day_off)
            if shift in NIGHT_DUTY_SHIFTS:
                last_night[name] = hist_date

    previous_resident_sandwich_counts = Counter()
    for name, night_dates in previous_resident_night_dates.items():
        for first_night in night_dates:
            second_night = first_night + timedelta(days=2)
            middle = first_night + timedelta(days=1)
            if second_night in night_dates and middle not in night_dates:
                previous_resident_sandwich_counts[name] += 1
    if previous_resident_sandwich_counts:
        logger.info(
            "Previous-month resident sandwich counts loaded: %s",
            dict(sorted(previous_resident_sandwich_counts.items())),
        )

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
    allowed_consecutive_resident_nights = (
        derive_fixed_resident_consecutive_night_exceptions(fixed)
    )
    fixed_resident_night_dates_by_name: dict[str, set[date]] = defaultdict(set)
    for (fixed_date, fixed_shift), fixed_names in fixed.items():
        if fixed_shift not in RESIDENT_NIGHT_SHIFTS:
            continue
        for fixed_name in fixed_names:
            fixed_resident_night_dates_by_name[fixed_name].add(fixed_date)
    if allowed_consecutive_resident_nights:
        logger.info(
            "Activated fixed resident consecutive-night exceptions: %s",
            serialize_resident_consecutive_night_exceptions(
                allowed_consecutive_resident_nights
            ),
        )

    def _blocked_by_tomorrow_fixed_resident_night(name: str, shift_date: date) -> bool:
        tomorrow = shift_date + timedelta(days=1)
        if tomorrow not in fixed_resident_night_dates_by_name.get(name, set()):
            return False
        return not resident_consecutive_night_allowed(
            allowed_consecutive_resident_nights,
            name,
            shift_date,
            tomorrow,
        )

    def get_eligible_workers(**kwargs: object) -> list[str]:
        """Apply this run's fixed-pair exceptions to every eligibility pass."""

        eligible = _get_eligible_workers(
            **kwargs,
            allowed_consecutive_resident_nights=allowed_consecutive_resident_nights,
        )
        shift_type = str(kwargs.get("shift_type") or "")
        shift_date = kwargs.get("shift_date")
        if shift_type in RESIDENT_NIGHT_SHIFTS and isinstance(shift_date, date):
            eligible = [
                name
                for name in eligible
                if not _blocked_by_tomorrow_fixed_resident_night(name, shift_date)
            ]
        return eligible

    def _resident_adjacent_night_penalty(
        name: str,
        shift_date: date,
        assignments: Dict[date, Dict[str, Set[str]]],
    ) -> int:
        return _resident_adjacent_night_penalty_base(
            name,
            shift_date,
            assignments,
            allowed_consecutive_resident_nights,
        )

    fixed_clinic_dates_by_name = {
        (clinic_date, name)
        for (clinic_date, clinic_shift), names in fixed.items()
        if clinic_shift in _el.CLINIC_SHIFTS
        for name in names
    }
    print(f"[auto-assign] Injected {sum(map(len, fixed.values()))} fixed rows")
    report_progress(5, "משבץ קבועים")

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
    mandatory_personal_assignment_keys: set[tuple[date, str, str]] = set()
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
            previous_night = last_night.get(doc, date.min)
            fixed_resident_consecutive_night = bool(
                row["Shift"] in RESIDENT_NIGHT_SHIFTS
                and resident_consecutive_night_allowed(
                    allowed_consecutive_resident_nights,
                    doc,
                    previous_night,
                    key[0],
                )
            )

            # Clinics alone veto the preceding resident night, including when
            # both assignments came from the fixed-assignment sheet.
            if (
                row["Shift"] in RESIDENT_NIGHT_SHIFTS
                and (key[0] + timedelta(days=1), doc) in fixed_clinic_dates_by_name
            ):
                logger.warning(
                    "Fixed %s %s – %s skipped (clinic next day)",
                    row.Date, row.Shift, doc,
                )
                continue

            # 1) mandatory rest-day after ת.מיון
            if (
                key[0] in blocked_next_day.get(doc, set())
                and not fixed_resident_consecutive_night
            ):
                logger.warning(
                    "Fixed %s %s – %s skipped (next-day rest rule)",
                    row.Date, row.Shift, doc
                )
                continue

            # 2) ≥48 h between any two night duties
            fixed_weekend_konen_pair = bool(
                row["Shift"] == KONEN_MION_SHIFT
                and key[0].weekday() == 5
                and previous_night == key[0] - timedelta(days=1)
                and doc in fixed.get(
                    (key[0] - timedelta(days=1), KONEN_MION_SHIFT),
                    [],
                )
            )
            if (
                row["Shift"] in NIGHT_DUTY_SHIFTS
                and (key[0] - previous_night).days < 2
                and not fixed_weekend_konen_pair
                and not fixed_resident_consecutive_night
            ):
                logger.warning(
                    "Fixed %s %s – %s skipped (night cool-down <2 days)",
                    row.Date, row.Shift, doc
                )
                continue

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

    def _resident_assignment_is_protected(d: date, shift: str, name: str) -> bool:
        key = (d, shift, name)
        return key in fixed_assignment_keys or key in mandatory_personal_assignment_keys

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
    report_progress(6, "משבץ תורנויות")

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
    resident_night_dates_by_name: dict[str, set[date]] = defaultdict(set)
    resident_sandwich_counts = Counter()
    resident_sandwich_cache_state = {"ready": False}
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
    konen_weekend_counts = Counter(
        name
        for _, row in roster[
            (roster["Shift"] == KONEN_MION_SHIFT)
            & roster["Date"].map(lambda x: date.fromisoformat(str(x)).weekday() in (4, 5))
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
        and (not availability_submitters or name in availability_submitters)
        and any(
            worker_shift_lut.get((name, candidate_shift), False)
            and eligibility_reason(name, d.isoformat(), candidate_shift) is None
            for d in month_dates
            for candidate_shift in RESIDENT_NIGHT_SHIFTS
        )
    }
    resident_fairness_pool = set(active_resident_night_names)
    resident_fairness_pool_excluded: set[str] = set()
    # A "whole-month" rotation is defined by the fixed-assignment sheet as
    # covering the 1st through the 28th. Later dates vary by month length.
    rotation_holiday_names = holiday_names_from_tables(tbl)
    rotation_holiday_eve_names = holiday_eve_names_from_tables(tbl)
    rotation_month_anchor_dates = {
        d for d in month_dates
        if d.day <= 28
        and effective_weekday_letter(d, rotation_holiday_names, rotation_holiday_eve_names)
        not in {"ו", "ש"}
    }
    fixed_rotation_dates_by_name: dict[str, set[date]] = defaultdict(set)
    for (d, shift_type), names in fixed_raw.items():
        if shift_type != "רוטציה" or d.weekday() in (4, 5):
            continue
        for name in names:
            name = str(name).strip()
            if name:
                fixed_rotation_dates_by_name[name].add(d)
    full_month_rotation_names = {
        name
        for name, days in fixed_rotation_dates_by_name.items()
        if rotation_month_anchor_dates and days >= rotation_month_anchor_dates
    }
    if full_month_rotation_names:
        logger.info(
            "Full-month rotation workers from fixed assignments: %s",
            ", ".join(sorted(full_month_rotation_names)),
        )
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
    senior_friday_day_dates_by_name: dict[str, set[date]] = defaultdict(set)
    for _, row in roster[roster["Shift"].isin(FRIDAY_DAY_BALANCE_SHIFTS)].iterrows():
        d = date.fromisoformat(str(row["Date"]))
        if d.weekday() != 4:
            continue
        for name in _names(row.Assigned):
            if name in senior_names:
                senior_friday_day_dates_by_name[name].add(d)
    senior_friday_day_counts = Counter({
        name: len(days)
        for name, days in senior_friday_day_dates_by_name.items()
    })
    preferred_night_assignment_keys: set[tuple[date, str, str]] = set()
    preferred_night_hits = Counter()
    important_preferred_night_hits = Counter()
    preferred_night_seeded_requests: set[tuple[str, date]] = set()
    preferred_night_seed_blocks: dict[tuple[str, date], dict[str, object]] = {}
    preferred_night_loss_stage: dict[tuple[str, date], str] = {}
    preferred_night_loss_detail: dict[tuple[str, date], dict[str, str]] = {}
    preferred_night_requesters_by_date: dict[date, set[str]] = defaultdict(set)
    preferred_night_request_dates_by_name: dict[str, set[date]] = defaultdict(set)
    for pref_name, pref_date in preferred_night_requests:
        preferred_night_requesters_by_date[pref_date].add(pref_name)
        preferred_night_request_dates_by_name[pref_name].add(pref_date)

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

    def _senior_friday_adds_day(name: str, shift_type: str, d: date) -> bool:
        return (
            d.weekday() == 4
            and shift_type in FRIDAY_DAY_BALANCE_SHIFTS
            and d not in senior_friday_day_dates_by_name.get(name, set())
        )

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

        if _is_senior_name(name):
            if shift_type == KONEN_MION_SHIFT:
                return (0, 0, _friday_konen_pair_rank(name, d), 0)
            projected = senior_friday_day_counts[name] + int(
                _senior_friday_adds_day(name, shift_type, d)
            )
            # The one-Friday target is stronger than the soft preference to
            # make Friday כונן מיון and אטנדינג the same person.
            return (*_senior_friday_assignment_priority(
                projected,
                _friday_konen_pair_rank(name, d),
            ), 0)

        # Residents fill overflow Friday work and should be balanced too.
        projected = friday_day_counts[name] + int(_friday_adds_day(name, d))
        return (1, 2, friday_day_counts[name], projected)

    def _record_friday_assignment(name: str, shift_type: str, d: date) -> None:
        if d.weekday() != 4 or shift_type not in FRIDAY_TOTAL_SHIFTS:
            return
        if d not in friday_dates_by_name[name]:
            friday_dates_by_name[name].add(d)
            friday_day_counts[name] += 1
        if (
            _is_senior_name(name)
            and shift_type in FRIDAY_DAY_BALANCE_SHIFTS
            and d not in senior_friday_day_dates_by_name[name]
        ):
            senior_friday_day_dates_by_name[name].add(d)
            senior_friday_day_counts[name] += 1

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
        if (
            shift_type in RESIDENT_NIGHT_SHIFTS
            and _blocked_by_tomorrow_fixed_resident_night(name, shift_date)
        ):
            return False

        last_night_map = last_night_map or {}
        if shift_type in RESIDENT_NIGHT_SHIFTS:
            tomorrow = daily_assignments.get(shift_date + timedelta(days=1), {}).get(name, set())
            if has_clinic_shift(tomorrow):
                return False

        previous_night = last_night_map.get(name, date.min)
        allowed_consecutive_night = bool(
            shift_type in RESIDENT_NIGHT_SHIFTS
            and resident_consecutive_night_allowed(
                allowed_consecutive_resident_nights,
                name,
                previous_night,
                shift_date,
            )
        )

        if (
            shift_date in blocked_next_day.get(name, set())
            and not allowed_consecutive_night
        ):
            return False

        if (
            shift_type in RESIDENT_NIGHT_SHIFTS
            and (shift_date - previous_night).days < 2
            and not allowed_consecutive_night
        ):
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
        konen_weekend_counts.clear()
        yoeatzim_counts.clear()
        yoeatzim_weekday_counts.clear()
        attending_counts.clear()
        eeg_counts.clear()
        resident_night_dates_by_name.clear()
        resident_sandwich_counts.clear()
        resident_sandwich_cache_state["ready"] = False
        personal_assignment_counts.clear()
        friday_dates_by_name.clear()
        friday_day_counts.clear()
        senior_friday_day_dates_by_name.clear()
        senior_friday_day_counts.clear()
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
                    resident_night_dates_by_name[name].add(d)
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
                    if d.weekday() in (4, 5):
                        konen_weekend_counts[name] += 1
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
                if (
                    d.weekday() == 4
                    and shift in FRIDAY_DAY_BALANCE_SHIFTS
                    and _is_senior_name(name)
                ):
                    senior_friday_day_dates_by_name[name].add(d)
                if _is_preferred_night_assignment(name, shift, d):
                    preferred_night_assignment_keys.add((d, shift, name))
                    preferred_night_hits[name] += 1
                    if _preferred_night_strength(name, d) >= 2:
                        important_preferred_night_hits[name] += 1

        for name, days in friday_dates_by_name.items():
            friday_day_counts[name] = len(days)
        for name, days in senior_friday_day_dates_by_name.items():
            senior_friday_day_counts[name] = len(days)

        combined_resident_night_dates: dict[str, set[date]] = defaultdict(set)
        for name, dates_for_name in previous_resident_night_dates.items():
            combined_resident_night_dates[name].update(dates_for_name)
        for name, dates_for_name in resident_night_dates_by_name.items():
            combined_resident_night_dates[name].update(dates_for_name)
        resident_sandwich_counts.update(_sandwich_counts_from_dates(combined_resident_night_dates))
        resident_sandwich_cache_state["ready"] = True

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

    def _preferred_night_request_is_fulfilled(name: str, request_date: date) -> bool:
        return bool(
            daily_assignments.get(request_date, {}).get(name, set()).intersection(
                _preferred_night_shifts_for(name)
            )
        )

    def _preferred_other_request_approval_percentage(
        name: str,
        excluded_date: date,
    ) -> int:
        other_dates = preferred_night_request_dates_by_name.get(name, set()) - {excluded_date}
        fulfilled = sum(
            _preferred_night_request_is_fulfilled(name, request_date)
            for request_date in other_dates
        )
        return _request_approval_percentage_basis_points(fulfilled, len(other_dates))

    def _preferred_night_key(name: str, shift_type: str, shift_date: date) -> tuple[float, int, int, int]:
        strength = _preferred_night_strength(name, shift_date)
        if not strength or shift_type not in _preferred_night_shifts_for(name):
            return (0.0, 0, 0, 0)

        weekend_bonus = 4.0 if shift_date.weekday() in (4, 5) else 0.0
        if strength >= 2:
            reward = -6.0 - weekend_bonus
        else:
            reward = -3.0 - (weekend_bonus / 2.0)

        if len(preferred_night_requesters_by_date.get(shift_date, set())) == 1:
            reward -= 1.0
        return (
            reward,
            -strength,
            _preferred_other_request_approval_percentage(name, shift_date),
            preferred_night_hits[name],
        )

    def _preferred_night_miss_objective() -> tuple[int, ...]:
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

        # Among rosters with the same request classes fulfilled, spread the
        # approvals.  Each fulfilled request is measured against the worker's
        # *other* requests, matching the head-to-head assignment tie-break.
        # A resident's first approval therefore has a zero distribution cost;
        # repeated approvals become progressively more expensive.
        approval_percentages: list[int] = []
        for name, request_dates in preferred_night_request_dates_by_name.items():
            if not _preferred_night_shifts_for(name).intersection(RESIDENT_NIGHT_SHIFTS):
                continue
            fulfilled_count = sum(
                _preferred_night_request_is_fulfilled(name, request_date)
                for request_date in request_dates
            )
            if fulfilled_count <= 0:
                continue
            percentage = _request_approval_percentage_basis_points(
                fulfilled_count - 1,
                len(request_dates) - 1,
            )
            approval_percentages.extend([percentage] * fulfilled_count)

        return (
            missed_important,
            missed_weekend,
            missed_regular,
            weighted,
            max(approval_percentages, default=0),
            sum(value * value for value in approval_percentages),
            sum(approval_percentages),
        )

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
        return _preferred_request_removal_penalty(strength, shift_date.weekday())

    def _preferred_night_removal_order_cost(
        name: str,
        shift_type: str,
        shift_date: date,
    ) -> int:
        return _preferred_request_removal_order_cost(
            _preferred_night_removal_penalty(name, shift_type, shift_date),
            _preferred_other_request_approval_percentage(name, shift_date),
        )

    def _record_konen_mion_assignment(name: str, shift_date: date) -> None:
        konen_month_counts[name] += 1
        if shift_date.weekday() == 4:
            konen_friday_counts[name] += 1
        if shift_date.weekday() in (4, 5):
            konen_weekend_counts[name] += 1

    def _shimon_friday_due() -> bool:
        return not previous_shimon_friday

    def _shimon_friday_available(shift_date: date) -> bool:
        return (
            shift_date.weekday() == 4
            and _shimon_friday_due()
            and (
                friday_day_counts[SHIMON_NAME] == 0
                or shift_date in friday_dates_by_name.get(SHIMON_NAME, set())
            )
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
        pool = resident_fairness_pool or active_resident_night_names or set(previous_resident_night_counts)
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
        projected_tmion = resident_night_shift_counts["ת.מיון"][name] + int(shift_type == "ת.מיון")
        projected_tmion2 = resident_night_shift_counts["ת.מיון 2"][name] + int(shift_type == "ת.מיון 2")
        return _resident_type_compensation_distance(
            projected_tmion + projected_tmion2,
            projected_tmion,
            projected_tmion2,
            previous_overload,
        )

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

    def _resident_sandwich_penalty_for(name: str, shift_date: date) -> int:
        return _resident_sandwich_penalty(
            name,
            shift_date,
            daily_assignments,
            historical_resident_nights=previous_resident_night_dates,
            preferred_night_requests=preferred_night_requests,
        )

    def _resident_night_spacing_penalty_for(
        name: str,
        shift_date: date,
        last_night_map: Dict[str, date],
    ) -> int:
        return _resident_night_spacing_penalty(
            name,
            shift_date,
            last_night_map,
            preferred_night_requests=preferred_night_requests,
        )

    def _resident_sandwich_count_by_name() -> Counter:
        if not resident_sandwich_cache_state["ready"]:
            _refresh_resident_sandwich_cache()
        return resident_sandwich_counts

    def _resident_actionable_sandwich_counts() -> Counter:
        """Count current-month and cross-month pairs, excluding past-only pairs."""
        by_name: dict[str, set[date]] = defaultdict(set)
        for name, dates_for_name in previous_resident_night_dates.items():
            by_name[name].update(dates_for_name)
        for d, by_worker in daily_assignments.items():
            if d not in month_dates:
                continue
            for name, shifts in by_worker.items():
                if shifts.intersection(RESIDENT_NIGHT_SHIFTS):
                    by_name[name].add(d)
        counts = Counter()
        for name, _, _ in _resident_actionable_sandwich_pairs_from_dates(
            by_name,
            movable_dates=set(month_dates),
            preferred_night_requests=preferred_night_requests,
        ):
            counts[name] += 1
        return counts

    def _resident_requested_sandwich_endpoints() -> set[tuple[str, date]]:
        """Return current assignments a sandwich-only repair must not remove."""
        by_name: dict[str, set[date]] = defaultdict(set)
        for name, dates_for_name in previous_resident_night_dates.items():
            by_name[name].update(dates_for_name)
        for d, by_worker in daily_assignments.items():
            for name, shifts in by_worker.items():
                if shifts.intersection(RESIDENT_NIGHT_SHIFTS):
                    by_name[name].add(d)
        return _resident_requested_sandwich_endpoints_from_dates(
            by_name,
            preferred_night_requests,
        )

    def _resident_assignment_is_requested_sandwich_endpoint(
        name: str,
        shift_date: date,
    ) -> bool:
        return (name, shift_date) in _resident_requested_sandwich_endpoints()

    def _resident_sandwich_distribution_objective(
        counts: Counter | None = None,
        pool: set[str] | None = None,
    ) -> tuple[int, int]:
        active_pool = pool or _resident_night_pool()
        if not active_pool:
            return (0, 0)
        active_counts = counts if counts is not None else _resident_actionable_sandwich_counts()
        values = [active_counts[name] for name in active_pool]
        return (max(values, default=0), sum(value * value for value in values))

    def _resident_sandwich_balance_key(name: str, shift_date: date) -> tuple[int, int, int]:
        current_counts = _resident_actionable_sandwich_counts()
        projected_delta = _resident_sandwich_penalty_for(name, shift_date) // 100
        projected_counts = Counter(current_counts)
        projected_counts[name] += projected_delta
        projected_distribution = _resident_sandwich_distribution_objective(
            projected_counts,
            _resident_night_pool() | {name},
        )
        return (
            projected_delta,
            *projected_distribution,
        )

    def _resident_actionable_sandwich_total(pool: set[str] | None = None) -> int:
        counts = _resident_actionable_sandwich_counts()
        active_pool = pool or _resident_night_pool()
        return sum(counts[name] for name in active_pool)

    def _resident_rolling_sandwich_total(pool: set[str] | None = None) -> int:
        counts = _resident_sandwich_count_by_name()
        if pool is None:
            pool = _resident_night_pool()
        return sum(counts[name] for name in pool)

    def _resident_projected_type_compensation_key(name: str, shift_type: str) -> int:
        if shift_type not in RESIDENT_NIGHT_SHIFTS:
            return 0
        previous_overload = max(
            0,
            previous_resident_night_counts[name] - _previous_resident_night_baseline(),
        )
        current_overload = max(
            0,
            month_counts[name] - min((month_counts[n] for n in _resident_night_pool()), default=0),
        )
        weekend_overload = max(
            0,
            weekend_night_counts[name] - min((weekend_night_counts[n] for n in _resident_night_pool()), default=0),
        )
        burden = previous_overload + current_overload + weekend_overload
        if burden <= 0:
            return 0

        projected_tmion = resident_night_shift_counts["ת.מיון"][name] + int(shift_type == "ת.מיון")
        projected_tmion2 = resident_night_shift_counts["ת.מיון 2"][name] + int(shift_type == "ת.מיון 2")
        return _resident_type_compensation_distance(
            projected_tmion + projected_tmion2,
            projected_tmion,
            projected_tmion2,
            burden,
        )

    def _projected_count_objective(
        counts: Counter,
        pool: set[str],
        name: str,
        delta: int,
    ) -> tuple[int, int]:
        projected = Counter({worker: counts[worker] for worker in pool})
        projected[name] += delta
        return _count_spread_and_square(projected, pool)

    def _resident_assignment_jitter(name: str, shift_type: str, shift_date: date) -> int:
        jitter_key = f"{month}|{shift_date.isoformat()}|{shift_type}|{name}".encode("utf-8")
        return int.from_bytes(hashlib.blake2s(jitter_key, digest_size=2).digest(), "big")

    def _resident_projected_fairness_key(name: str, shift_type: str, shift_date: date) -> tuple:
        if shift_type not in RESIDENT_NIGHT_SHIFTS:
            return (0,)

        weekend_add = int(shift_date.weekday() in (4, 5))
        saturday_add = int(shift_date.weekday() == 5)
        friday_add = int(shift_date.weekday() == 4)
        thursday_add = int(shift_date.weekday() == 3)
        projected_total = month_counts[name] + 1
        projected_weekend = weekend_night_counts[name] + weekend_add
        projected_saturday = saturday_night_counts[name] + saturday_add
        projected_rolling_total = max(
            0,
            previous_resident_night_counts[name]
            + projected_total
            - _resident_night_extra_capacity(name),
        )
        projected_rolling_weekend = previous_resident_weekend_counts[name] + projected_weekend
        projected_thursday = thursday_night_counts[name] + thursday_add
        projected_tmion = resident_night_shift_counts["ת.מיון"][name] + (1 if shift_type == "ת.מיון" else 0)
        projected_tmion2 = resident_night_shift_counts["ת.מיון 2"][name] + (1 if shift_type == "ת.מיון 2" else 0)
        projected_type_gap = _resident_type_excess_gap(
            projected_tmion + projected_tmion2,
            projected_tmion,
            projected_tmion2,
        )
        projected_shift_count = resident_night_shift_counts[shift_type][name] + 1
        sandwich_key = _resident_sandwich_balance_key(name, shift_date)
        pool = _resident_night_pool() | {name}
        total_objective = _projected_count_objective(month_counts, pool, name, 1)
        weekend_objective = _projected_count_objective(
            weekend_night_counts,
            pool,
            name,
            weekend_add,
        )
        friday_counts = Counter({
            worker: weekend_night_counts[worker] - saturday_night_counts[worker]
            for worker in pool
        })
        friday_objective = _projected_count_objective(
            friday_counts,
            pool,
            name,
            friday_add,
        )
        saturday_objective = _projected_count_objective(
            saturday_night_counts,
            pool,
            name,
            saturday_add,
        )
        dual_pool = {
            worker for worker in pool
            if all(worker_shift_lut.get((worker, candidate_shift), False) for candidate_shift in RESIDENT_NIGHT_SHIFTS)
        }
        if name in dual_pool:
            projected_t1 = Counter(resident_night_shift_counts["ת.מיון"])
            projected_t2 = Counter(resident_night_shift_counts["ת.מיון 2"])
            projected_t1[name] += int(shift_type == "ת.מיון")
            projected_t2[name] += int(shift_type == "ת.מיון 2")
            excess_gaps = [
                _resident_type_excess_gap(
                    projected_t1[worker] + projected_t2[worker],
                    projected_t1[worker],
                    projected_t2[worker],
                )
                for worker in dual_pool
            ]
            t1_spread, t1_square = _count_spread_and_square(projected_t1, dual_pool)
            t2_spread, t2_square = _count_spread_and_square(projected_t2, dual_pool)
            projected_type_objective = (
                max(excess_gaps, default=0),
                sum(excess_gaps),
                t1_spread,
                t2_spread,
                t1_square,
                t2_square,
            )
        else:
            projected_type_objective = (0, 0, 0, 0, 0, 0)

        return (
            total_objective,
            (*weekend_objective, *friday_objective),
            saturday_objective,
            sandwich_key[0],
            sandwich_key[1:],
            projected_type_objective,
            *_preferred_night_key(name, shift_type, shift_date),
            *_resident_personal_night_penalty(name, shift_type, shift_date),
            -_resident_night_extra_capacity(name),
            projected_thursday,
            _glinskaya_weekend_preference(name, shift_date),
            projected_rolling_total,
            projected_rolling_weekend,
            _resident_projected_type_compensation_key(name, shift_type),
            projected_type_gap,
            projected_shift_count,
        )

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
        return _resident_projected_fairness_key(name, shift_type, shift_date)

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

    def _sandwich_counts_from_dates(by_name: dict[str, set[date]]) -> Counter:
        counts = Counter()
        for name, _, _ in _resident_actionable_sandwich_pairs_from_dates(
            by_name,
            preferred_night_requests=preferred_night_requests,
        ):
            counts[name] += 1
        return counts

    def _refresh_resident_sandwich_cache() -> None:
        resident_night_dates_by_name.clear()
        for name, dates_for_name in previous_resident_night_dates.items():
            resident_night_dates_by_name[name].update(dates_for_name)
        for d, by_worker in daily_assignments.items():
            for name, shifts in by_worker.items():
                if shifts.intersection(RESIDENT_NIGHT_SHIFTS):
                    resident_night_dates_by_name[name].add(d)
        resident_sandwich_counts.clear()
        resident_sandwich_counts.update(_sandwich_counts_from_dates(resident_night_dates_by_name))
        resident_sandwich_cache_state["ready"] = True

    def _invalidate_resident_sandwich_cache(shift_type: str) -> None:
        if shift_type in RESIDENT_NIGHT_SHIFTS:
            resident_sandwich_cache_state["ready"] = False

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

    def _rotation_current_counts() -> Counter:
        return Counter(
            name
            for _, row in roster[roster["Shift"] == "רוטציה"].iterrows()
            for row_date in [date.fromisoformat(str(row["Date"]))]
            if row_date.weekday() not in (4, 5)
            for name in _name_list(row["Assigned"])
            if name in full_month_rotation_names
        )

    def _rotation_count_balance_key(rotation_counts: Counter | None = None) -> tuple[int, int]:
        if not full_month_rotation_names:
            return (0, 0)
        rotation_counts = rotation_counts or _rotation_current_counts()
        values = [rotation_counts[name] for name in full_month_rotation_names]
        if not values:
            return (0, 0)
        return (max(values) - min(values), sum(value * value for value in values))

    def _rotation_pull_balance_key(
        pulled_name: str,
        rotation_counts: Counter | None = None,
    ) -> RotationPullBalanceKey:
        rotation_counts = Counter(rotation_counts or _rotation_current_counts())
        before = _rotation_count_balance_key(rotation_counts)
        projected = Counter(rotation_counts)
        projected[pulled_name] = max(projected[pulled_name] - 1, 0)
        after = _rotation_count_balance_key(projected)
        return (
            *after,
            0 if after < before else 1,
            -rotation_counts[pulled_name],
            month_counts[pulled_name],
            pulled_name,
        )

    def _rotation_pick_prefix(
        name: str,
        rotation_elig_set: set[str],
        rotation_counts: Counter | None = None,
    ) -> RotationReservePickPrefix:
        is_rotation_reserve = name in rotation_elig_set
        balance_key = (
            _rotation_pull_balance_key(name, rotation_counts)
            if is_rotation_reserve
            else None
        )
        return _rotation_reserve_pick_prefix(
            is_rotation_reserve,
            balance_key,
        )

    def _rotation_pull_candidates(shift_type: str, shift_date: date, current: list[str]) -> list[str]:
        if shift_date.weekday() in (4, 5):
            return []
        if shift_type in NIGHT_DUTY_SHIFTS:
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

        rotation_counts = _rotation_current_counts()
        return sorted(out, key=lambda name: _rotation_pull_balance_key(name, rotation_counts))

    def _pull_from_full_month_rotation(shift_type: str, shift_date: date, name: str) -> None:
        if name not in full_month_rotation_names:
            return
        if shift_type in NIGHT_DUTY_SHIFTS:
            return
        rotation_idx = _row_index(shift_date, "רוטציה")
        if rotation_idx is None or name not in _name_list(roster.at[rotation_idx, "Assigned"]):
            return
        before_counts = _rotation_current_counts()
        before_balance = _rotation_count_balance_key(before_counts)
        _remove_from_roster(shift_date, "רוטציה", name)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()
        after_counts = _rotation_current_counts()
        logger.info(
            "Pulled full-month rotation for day shift: %s %s from %s rotation balance %s -> %s counts %s -> %s",
            shift_date.isoformat(), shift_type, name,
            before_balance, _rotation_count_balance_key(after_counts),
            dict(sorted(before_counts.items())), dict(sorted(after_counts.items())),
        )

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
                    if (
                        has_rotation
                        and shift != "רוטציה"
                        and shift not in NIGHT_DUTY_SHIFTS
                        and _is_day_shift(shift)
                    ):
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
                tomorrow_work = daily_assignments.get(d, {}).get(name, set())
                previous_date = d - timedelta(days=1)
                previous_nights = daily_assignments.get(previous_date, {}).get(name, set()).intersection(
                    RESIDENT_NIGHT_SHIFTS
                )
                if has_clinic_shift(tomorrow_work):
                    for night_shift in sorted(previous_nights):
                        _remove_from_roster(previous_date, night_shift, name)
                        fixed_assignment_keys.discard((previous_date, night_shift, name))
                        mandatory_personal_assignment_keys.discard((previous_date, night_shift, name))
                        removed += 1
                        logger.warning(
                            "Removed prior resident night blocked by next-day clinic: %s %s from %s",
                            previous_date.isoformat(), night_shift, name,
                        )
                    if previous_nights:
                        # The clinic wins this hard conflict. The removed night
                        # will be offered to another resident during refill.
                        continue

                fixed_current_resident_nights = {
                    shift
                    for shift in tomorrow_work.intersection(RESIDENT_NIGHT_SHIFTS)
                    if (d, shift, name) in fixed_assignment_keys
                }
                removed_previous_nights = 0
                if previous_nights and fixed_current_resident_nights:
                    exception_allowed = resident_consecutive_night_allowed(
                        allowed_consecutive_resident_nights,
                        name,
                        previous_date,
                        d,
                    )
                    for previous_shift in sorted(previous_nights):
                        resolution = _adjacent_fixed_night_resolution(
                            previous_is_fixed=(
                                previous_date,
                                previous_shift,
                                name,
                            ) in fixed_assignment_keys,
                            current_is_fixed=True,
                            exception_allowed=exception_allowed,
                        )
                        if resolution != "remove_previous":
                            continue
                        if _is_preferred_night_assignment(
                            name,
                            previous_shift,
                            previous_date,
                        ):
                            preferred_night_seed_blocks[(name, previous_date)] = {
                                "reason_code": "hard_rule",
                                "block": "tomorrow-fixed-night",
                            }
                        _remove_from_roster(previous_date, previous_shift, name)
                        mandatory_personal_assignment_keys.discard(
                            (previous_date, previous_shift, name)
                        )
                        removed += 1
                        removed_previous_nights += 1
                        logger.warning(
                            "Removed non-fixed prior resident night to preserve fixed next-night assignment: %s %s from %s; kept %s",
                            previous_date.isoformat(),
                            previous_shift,
                            name,
                            d.isoformat(),
                        )
                    if removed_previous_nights == len(previous_nights):
                        # With the preceding non-fixed night gone, this date is
                        # no longer a post-duty date. Keep its fixed duty and
                        # any otherwise legal daytime work.
                        continue

                for shift in sorted(daily_assignments.get(d, {}).get(name, set())):
                    if shift in rest_only:
                        continue
                    if (
                        shift in RESIDENT_NIGHT_SHIFTS
                        and previous_nights
                        and resident_consecutive_night_allowed(
                            allowed_consecutive_resident_nights,
                            name,
                            previous_date,
                            d,
                        )
                    ):
                        logger.info(
                            "Kept fixed Yom Kippur consecutive resident night: %s %s %s",
                            d.isoformat(), shift, name,
                        )
                        continue
                    if shift == "רוטציה" and name in full_month_rotation_names:
                        rotation_idx = _row_index(d, "רוטציה")
                        if rotation_idx is not None:
                            current = [
                                worker for worker in _name_list(roster.at[rotation_idx, "Assigned"])
                                if worker != name
                            ]
                            roster.at[rotation_idx, "Assigned"] = _write_name_list(current)
                            roster.at[rotation_idx, "Needed"] = len(current)
                            roster.at[rotation_idx, "SoftCap"] = len(current)
                        fixed_assignment_keys.discard((d, "רוטציה", name))
                        mandatory_personal_assignment_keys.discard((d, "רוטציה", name))
                        removed += 1
                        logger.info(
                            "Removed full-month rotation overridden by after-duty rest: %s %s",
                            d.isoformat(), name,
                        )
                        continue
                    if (d, shift, name) in fixed_assignment_keys:
                        logger.warning(
                            "Removing fixed assignment for hard after-duty rest: %s %s %s",
                            d.isoformat(), shift, name,
                        )
                        fixed_assignment_keys.discard((d, shift, name))
                    mandatory_personal_assignment_keys.discard((d, shift, name))
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
                rotation_elig_set = set(rotation_elig)
                rotation_counts_for_pick = _rotation_current_counts()

                def _rotation_pull_pick_prefix(w: str) -> tuple[int, int, int, int, int, str]:
                    return _rotation_pick_prefix(w, rotation_elig_set, rotation_counts_for_pick)

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
                        _resident_sandwich_balance_key(w, shift_date),
                        _resident_sandwich_penalty_for(w, shift_date),
                        _resident_night_spacing_penalty_for(w, shift_date, _last_night_before(shift_date)),
                        fairness_score(w, shift_type, shift_date, history, _last_night_before(shift_date)),
                        _resident_assignment_jitter(w, shift_type, shift_date),
                    )
                elif shift_type == KONEN_MION_SHIFT:
                    paired_date = None
                    if shift_date.weekday() == 4:
                        paired_date = shift_date + timedelta(days=1)
                    elif shift_date.weekday() == 5:
                        paired_date = shift_date - timedelta(days=1)
                    paired_idx = (
                        _row_index(paired_date, KONEN_MION_SHIFT)
                        if paired_date is not None
                        else None
                    )
                    paired_names = (
                        set(_name_list(roster.at[paired_idx, "Assigned"]))
                        if paired_idx is not None
                        else set()
                    )
                    paired_eligible = [name for name in elig if name in paired_names]
                    if paired_eligible:
                        elig = paired_eligible
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
                        0 if w in paired_names else 1,
                    )
                elif shift_type == ATTENDING_SHIFT:
                    key_fn = lambda w: (
                        _friday_work_key(w, shift_type, shift_date),
                        0 if shift_date.weekday() == 4 and _has_shift(daily_assignments, shift_date, w, KONEN_MION_SHIFT) else 1,
                        _personal_rule_key(w, shift_type, shift_date),
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
                    key=lambda w: (_rotation_pull_pick_prefix(w), key_fn(w)),
                )
                current.append(pick)
                _pull_from_full_month_rotation(shift_type, shift_date, pick)
                daily_assignments[shift_date].setdefault(pick, set()).add(shift_type)
                _invalidate_resident_sandwich_cache(shift_type)
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

    def _restore_full_month_rotation_reserves() -> int:
        """Return fixed rotation workers when an ordinary replacement is legal.

        A full-month fixed rotation may be used to cover a required daytime
        vacancy, but only as a reserve.  Night-duty repairs can later make a
        non-rotation worker available, so re-check every reserve pull after
        cleanup instead of leaving the fixed rotation displaced permanently.
        """

        restored = 0
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()

        day_rows = roster[
            roster["Shift"].map(
                lambda shift: _is_day_shift(str(shift)) and str(shift) != "רוטציה"
            )
        ].copy()
        day_rows["_date"] = day_rows["Date"].map(
            lambda value: date.fromisoformat(str(value))
        )

        for idx, row in day_rows.sort_values(["_date", "Shift"]).iterrows():
            shift_date = row["_date"]
            shift_type = str(row.Shift)
            current = _name_list(roster.at[idx, "Assigned"])
            reserve_names = [
                name
                for name in current
                if name in full_month_rotation_names
                and (shift_date, "רוטציה", name) in fixed_assignment_keys
                and (shift_date, shift_type, name) not in fixed_assignment_keys
                and (shift_date, shift_type, name) not in mandatory_personal_assignment_keys
            ]

            for reserve_name in reserve_names:
                eligible = get_eligible_workers(
                    shift_type=shift_type,
                    shift_date=shift_date,
                    blocked_next_day=blocked_next_day,
                    extra_day_off=extra_day_off,
                    daily_assignments=daily_assignments,
                    blocked_reasons=None,
                    last_night=_last_night_before(shift_date),
                )
                candidates = [
                    name
                    for name in eligible
                    if name not in current
                    and name not in full_month_rotation_names
                    and _personal_under_max(name, shift_type, shift_date)
                ]
                if shift_type == YOEATZIM_SHIFT:
                    candidates = [
                        name for name in candidates
                        if _yoeatzim_allowed(name, shift_date)
                    ]
                elif shift_type == "EEG":
                    candidates = [
                        name for name in candidates
                        if _eeg_under_cap(name, shift_date)
                    ]
                if not candidates:
                    continue

                replacement = min(
                    candidates,
                    key=lambda name: (
                        _personal_rule_key(name, shift_type, shift_date),
                        _friday_work_key(name, shift_type, shift_date),
                        fairness_score(
                            name,
                            shift_type,
                            shift_date,
                            history,
                            _last_night_before(shift_date),
                        ),
                        name,
                    ),
                )
                original_day_names = list(current)
                replacement_day_names = [
                    replacement if name == reserve_name else name
                    for name in current
                ]
                roster.at[idx, "Assigned"] = _write_name_list(replacement_day_names)
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()

                rotation_idx = _row_index(shift_date, "רוטציה")
                rotation_eligible = get_eligible_workers(
                    shift_type="רוטציה",
                    shift_date=shift_date,
                    blocked_next_day=blocked_next_day,
                    extra_day_off=extra_day_off,
                    daily_assignments=daily_assignments,
                    blocked_reasons=None,
                    last_night=_last_night_before(shift_date),
                )
                if rotation_idx is None or reserve_name not in rotation_eligible:
                    roster.at[idx, "Assigned"] = _write_name_list(original_day_names)
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    continue

                rotation_names = _name_list(roster.at[rotation_idx, "Assigned"])
                if reserve_name not in rotation_names:
                    rotation_names.append(reserve_name)
                    roster.at[rotation_idx, "Assigned"] = _write_name_list(rotation_names)

                history[reserve_name][shift_type] = max(
                    0,
                    history[reserve_name][shift_type] - 1,
                )
                history[replacement][shift_type] += 1
                personal_assignment_counts[(reserve_name, shift_type)] = max(
                    0,
                    personal_assignment_counts[(reserve_name, shift_type)] - 1,
                )
                personal_assignment_counts[(replacement, shift_type)] += 1
                current = replacement_day_names
                restored += 1
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                logger.info(
                    "Restored fixed full-month rotation from reserve: %s %s -> %s; %s returned to rotation",
                    shift_date.isoformat(), shift_type, replacement, reserve_name,
                )

        return restored

    def _resident_candidate_hard_legal(idx: int, name: str) -> bool:
        shift_date = _row_date(idx)
        shift_type = str(roster.at[idx, "Shift"])
        if shift_type not in RESIDENT_NIGHT_SHIFTS:
            return False
        if name not in active_resident_night_names or name in _assigned_names(idx):
            return False
        if not _can_worker_take_shift(
            name,
            shift_type,
            shift_date,
            last_night_map=_last_night_before(shift_date),
        ):
            return False
        if _resident_adjacent_night_penalty(name, shift_date, daily_assignments) > 0:
            return False
        tomorrow_shifts = daily_assignments.get(
            shift_date + timedelta(days=1),
            {},
        ).get(name, set())
        if has_clinic_shift(tomorrow_shifts):
            return False
        return True

    def _resident_vacancy_candidates(idx: int) -> list[str]:
        shift_date = _row_date(idx)
        shift_type = str(roster.at[idx, "Shift"])
        return sorted(
            [
                name for name in active_resident_night_names
                if _resident_candidate_hard_legal(idx, name)
            ],
            key=lambda name: (
                _resident_night_balance_key(name, shift_type, shift_date),
                _preferred_night_removal_order_cost(name, shift_type, shift_date),
                fairness_score(
                    name,
                    shift_type,
                    shift_date,
                    history,
                    _last_night_before(shift_date),
                ),
                _resident_assignment_jitter(name, shift_type, shift_date),
            ),
        )

    def _restore_resident_assignments(snapshot: pd.Series) -> None:
        roster["Assigned"] = snapshot.copy()
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()

    def _try_resident_vacancy_chain(target_idx: int, max_evaluations: int = 300) -> bool:
        """Fill one resident vacancy through a bounded one-displacement chain."""
        before_missing = _resident_missing_slot_count()
        target_date = _row_date(target_idx)
        target_shift = str(roster.at[target_idx, "Shift"])
        target_names = _assigned_names(target_idx)
        evaluations = 0

        donor_rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        donor_rows["_date"] = donor_rows["Date"].map(lambda value: date.fromisoformat(str(value)))
        for donor_idx, donor_row in donor_rows.sort_values("_date").iterrows():
            if int(donor_idx) == int(target_idx):
                continue
            donor_date = donor_row["_date"]
            donor_shift = str(donor_row.Shift)
            donor_original = _assigned_names(int(donor_idx))
            for donor_name in sorted(
                donor_original,
                key=lambda name: (
                    _preferred_night_removal_order_cost(name, donor_shift, donor_date),
                    _resident_assignment_jitter(name, donor_shift, donor_date),
                ),
            ):
                evaluations += 1
                if evaluations > max_evaluations:
                    return False
                if donor_name in target_names:
                    continue
                if (donor_date, donor_shift, donor_name) in fixed_assignment_keys:
                    continue

                snapshot = roster["Assigned"].copy()
                roster.at[donor_idx, "Assigned"] = _write_name_list(
                    [name for name in donor_original if name != donor_name]
                )
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()

                if not _resident_candidate_hard_legal(target_idx, donor_name):
                    _restore_resident_assignments(snapshot)
                    continue

                roster.at[target_idx, "Assigned"] = _write_name_list(target_names + [donor_name])
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()

                replacements = _resident_vacancy_candidates(int(donor_idx))
                for replacement in replacements:
                    roster.at[donor_idx, "Assigned"] = _write_name_list(
                        _assigned_names(int(donor_idx)) + [replacement]
                    )
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    if _resident_missing_slot_count() < before_missing:
                        logger.info(
                            "resident vacancy chain: %s %s <- %s; %s %s <- %s",
                            target_date.isoformat(),
                            target_shift,
                            donor_name,
                            donor_date.isoformat(),
                            donor_shift,
                            replacement,
                        )
                        _forget_repair_noops()
                        return True
                    roster.at[donor_idx, "Assigned"] = _write_name_list(
                        [name for name in _assigned_names(int(donor_idx)) if name != replacement]
                    )
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()

                _restore_resident_assignments(snapshot)
        return False

    def _repair_missing_resident_nights(max_steps: int = 20) -> int:
        repaired = 0
        for _ in range(max_steps):
            missing_rows = [
                idx for idx in resident_night_row_indexes
                if len(_assigned_names(idx)) < _to_int(roster.at[idx, "Needed"], 0)
            ]
            if not missing_rows:
                break
            constrained = sorted(
                missing_rows,
                key=lambda idx: (
                    len(_resident_vacancy_candidates(idx)),
                    _row_date(idx),
                    str(roster.at[idx, "Shift"]),
                ),
            )
            changed = False
            for idx in constrained:
                candidates = _resident_vacancy_candidates(idx)
                if candidates:
                    pick = candidates[0]
                    roster.at[idx, "Assigned"] = _write_name_list(_assigned_names(idx) + [pick])
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    repaired += 1
                    changed = True
                    logger.info(
                        "resident vacancy direct refill: %s %s <- %s",
                        _row_date(idx).isoformat(),
                        str(roster.at[idx, "Shift"]),
                        pick,
                    )
                    break
                if _try_resident_vacancy_chain(idx):
                    repaired += 1
                    changed = True
                    break
            if not changed:
                break
        return repaired

    def _refill_required_rows_after_cleanup() -> int:
        filled = _repair_missing_resident_nights() + _refill_hard_rows_after_cleanup()
        restored_rotations = _restore_full_month_rotation_reserves()
        if restored_rotations:
            logger.info(
                "Restored %d fixed full-month rotation assignments after refill",
                restored_rotations,
            )
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

            # A later fairness pass may improve Saturdays, but it must not undo
            # a mandatory personal placement that was just satisfied.
            for idx, row in _personal_rule_rows(rule).iterrows():
                shift_date = date.fromisoformat(str(row.Date))
                if name in _name_list(roster.at[idx, "Assigned"]):
                    mandatory_personal_assignment_keys.add((shift_date, shift, name))
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
            if rule.get("condition") == "חובה":
                for idx, row in _personal_rule_rows(rule).iterrows():
                    shift_date = date.fromisoformat(str(row.Date))
                    if name in _name_list(roster.at[idx, "Assigned"]):
                        mandatory_personal_assignment_keys.add((shift_date, shift, name))
        return changed

    def _find_resident_sandwiches(*, include_history: bool = False) -> list[tuple[str, date, date]]:
        by_name: dict[str, set[date]] = defaultdict(set)
        if include_history:
            for name, night_dates in previous_resident_night_dates.items():
                by_name[name].update(night_dates)
        for d, by_worker in daily_assignments.items():
            for name, shifts in by_worker.items():
                if shifts.intersection(RESIDENT_NIGHT_SHIFTS):
                    by_name[name].add(d)
        movable_dates = set(month_dates) if include_history else None
        return _resident_actionable_sandwich_pairs_from_dates(
            by_name,
            movable_dates=movable_dates,
            preferred_night_requests=preferred_night_requests,
        )

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
        protect_weekend_objective: tuple[int, int, int, int] | None = None,
        protect_saturday_objective: tuple[int, int] | None = None,
        protect_weekend_history: int | None = None,
        require_preferred_names: bool = False,
        required_stage: str | None = None,
    ) -> bool:
        row = roster.loc[idx]
        shift_type = str(row.Shift)
        shift_date = date.fromisoformat(str(row.Date))
        if _resident_assignment_is_protected(shift_date, shift_type, old_name):
            return False
        if (
            required_stage in {"sandwich_total", "sandwich_distribution"}
            and _resident_assignment_is_requested_sandwich_endpoint(
                old_name,
                shift_date,
            )
        ):
            return False

        stage_before = _resident_priority_metrics() if required_stage else None
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
            and (
                allow_sandwich
                or _resident_night_spacing_penalty_for(w, shift_date, effective_last_night) < 100
            )
            and (allow_sandwich or _resident_sandwich_penalty_for(w, shift_date) == 0)
        ]

        if preferred_names:
            preferred = [w for w in non_sandwich if w in preferred_names]
            if preferred:
                non_sandwich = preferred
            elif require_preferred_names:
                roster.at[idx, "Assigned"] = _write_name_list(original)
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                return False

        if not non_sandwich:
            roster.at[idx, "Assigned"] = _write_name_list(original)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            return False

        ordered_candidates = sorted(
            non_sandwich,
            key=lambda w: (
                0 if preferred_names and w in preferred_names else 1,
                _resident_night_balance_key(w, shift_type, shift_date),
                _weekend_resident_night_key(w, shift_date),
                _friday_work_key(w, shift_type, shift_date),
                _alternate_risk_penalty(w, shift_type, shift_date, daily_assignments),
                _friday_night_morning_penalty(w, shift_type, shift_date, daily_assignments),
                _resident_adjacent_night_penalty(w, shift_date, daily_assignments),
                _resident_sandwich_balance_key(w, shift_date),
                fairness_score(w, shift_type, shift_date, history, effective_last_night),
                _resident_assignment_jitter(w, shift_type, shift_date),
            ),
        )

        for pick in ordered_candidates:
            roster.at[idx, "Assigned"] = _write_name_list(current + [pick])
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            rejected = bool(
                (
                    protect_total_objective is not None
                    and _resident_night_total_objective() > protect_total_objective
                )
                or (
                    protect_weekend_objective is not None
                    and _resident_weekend_objective() > protect_weekend_objective
                )
                or (
                    protect_saturday_objective is not None
                    and _resident_saturday_objective() > protect_saturday_objective
                )
                or (
                    protect_weekend_history is not None
                    and _resident_weekend_history_load() > protect_weekend_history
                )
                or (
                    required_stage is not None
                    and stage_before is not None
                    and not _resident_stage_improves(
                        stage_before,
                        _resident_priority_metrics(),
                        required_stage,
                    )
                )
            )
            if not rejected:
                logger.info(
                    "%s: %s %s %s -> %s",
                    reason, shift_date.isoformat(), shift_type, old_name, pick,
                )
                return True

            roster.at[idx, "Assigned"] = _write_name_list(current)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

        roster.at[idx, "Assigned"] = _write_name_list(original)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()
        return False

    def _repair_resident_sandwiches() -> int:
        label = "resident_sandwiches"
        if _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(10):
            sandwiches = _find_resident_sandwiches(include_history=True)
            if not sandwiches:
                _remember_repair_noop(label)
                break

            changed = False
            for name, first, second in sandwiches:
                candidate_rows: list[tuple[int, str, int]] = []
                for candidate_date in (first, second):
                    idx = _resident_night_row_index(candidate_date, name)
                    if idx is None:
                        continue
                    shift_type = str(roster.at[idx, "Shift"])
                    candidate_rows.append((
                        _preferred_night_removal_order_cost(
                            name,
                            shift_type,
                            candidate_date,
                        ),
                        candidate_date.isoformat(),
                        idx,
                    ))

                # Both endpoints can break the same sandwich.  Try the less
                # valuable request first (normally a weekday before a weekend).
                for _, _, idx in sorted(candidate_rows):
                    if _try_replace_resident_night(
                        idx,
                        name,
                        reason="resident night sandwich repair",
                        allow_sandwich=True,
                        protect_total_objective=_resident_night_total_objective(),
                        protect_weekend_objective=_resident_weekend_objective(),
                        protect_saturday_objective=_resident_saturday_objective(),
                        required_stage="sandwich_total",
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

    def _try_resident_sandwich_swap(max_trials: int = 120) -> bool:
        """Swap two resident nights while preserving totals and higher priorities."""
        current_metrics = _resident_priority_metrics()

        sandwiches = _find_resident_sandwiches(include_history=True)
        if not sandwiches:
            return False

        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        if rows.empty:
            return False
        rows["_date"] = rows["Date"].map(lambda value: date.fromisoformat(str(value)))

        combined_dates: dict[str, set[date]] = defaultdict(set)
        for name, night_dates in previous_resident_night_dates.items():
            combined_dates[name].update(night_dates)
        for d, by_worker in daily_assignments.items():
            for name, shifts in by_worker.items():
                if shifts.intersection(RESIDENT_NIGHT_SHIFTS):
                    combined_dates[name].add(d)
        requested_sandwich_endpoints = (
            _resident_requested_sandwich_endpoints_from_dates(
                combined_dates,
                preferred_night_requests,
            )
        )

        def day_class(d: date) -> int:
            if d.weekday() == 5:
                return 2
            if d.weekday() == 4:
                return 1
            return 0

        def sandwich_count(name: str, dates: set[date]) -> int:
            return len(_resident_actionable_sandwich_pairs_from_dates(
                {name: dates},
                movable_dates=set(month_dates),
                preferred_night_requests=preferred_night_requests,
            ))

        candidates: list[tuple[int, int, int, int, str, int, int, str, str]] = []
        seen: set[tuple[int, str, int, str]] = set()
        for old_name, first, second in sandwiches:
            for old_date in (second, first):
                old_idx = _resident_night_row_index(old_date, old_name)
                if old_idx is None:
                    continue
                if (old_name, old_date) in requested_sandwich_endpoints:
                    continue
                old_shift = str(roster.at[old_idx, "Shift"])
                if (old_date, old_shift, old_name) in fixed_assignment_keys:
                    continue
                old_preference_cost = _preferred_night_removal_order_cost(old_name, old_shift, old_date)

                for other_idx, other_row in rows.iterrows():
                    other_date = other_row["_date"]
                    other_shift = str(other_row.Shift)
                    if other_idx == old_idx or other_date == old_date:
                        continue
                    if day_class(other_date) != day_class(old_date):
                        continue

                    for other_name in _name_list(other_row.Assigned):
                        if other_name == old_name:
                            continue
                        if (other_name, other_date) in requested_sandwich_endpoints:
                            continue
                        key = (old_idx, old_name, other_idx, other_name)
                        if key in seen:
                            continue
                        seen.add(key)
                        if (other_date, other_shift, other_name) in fixed_assignment_keys:
                            continue
                        other_preference_cost = _preferred_night_removal_order_cost(
                            other_name, other_shift, other_date,
                        )
                        if not worker_shift_lut.get((other_name, old_shift), False):
                            continue
                        if not worker_shift_lut.get((old_name, other_shift), False):
                            continue

                        old_dates = set(combined_dates[old_name])
                        other_dates = set(combined_dates[other_name])
                        if other_date in old_dates or old_date in other_dates:
                            continue
                        before_count = (
                            sandwich_count(old_name, old_dates)
                            + sandwich_count(other_name, other_dates)
                        )
                        old_dates.discard(old_date)
                        old_dates.add(other_date)
                        other_dates.discard(other_date)
                        other_dates.add(old_date)
                        after_count = (
                            sandwich_count(old_name, old_dates)
                            + sandwich_count(other_name, other_dates)
                        )
                        improvement = before_count - after_count
                        if improvement <= 0:
                            continue
                        candidates.append((
                            -improvement,
                            0 if old_shift == other_shift else 1,
                            old_preference_cost + other_preference_cost,
                            abs((other_date - old_date).days),
                            other_date.isoformat(),
                            old_idx,
                            other_idx,
                            old_name,
                            other_name,
                        ))

        for _, _, _, _, _, old_idx, other_idx, old_name, other_name in sorted(candidates)[:max_trials]:
            old_date = _row_date(old_idx)
            other_date = _row_date(other_idx)
            old_shift = str(roster.at[old_idx, "Shift"])
            other_shift = str(roster.at[other_idx, "Shift"])
            original_old = _name_list(roster.at[old_idx, "Assigned"])
            original_other = _name_list(roster.at[other_idx, "Assigned"])
            if old_name not in original_old or other_name not in original_other:
                continue

            current_old = [name for name in original_old if name != old_name]
            current_other = [name for name in original_other if name != other_name]
            roster.at[old_idx, "Assigned"] = _write_name_list(current_old)
            roster.at[other_idx, "Assigned"] = _write_name_list(current_other)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            other_can_take_old = _can_worker_take_shift(
                other_name,
                old_shift,
                old_date,
                last_night_map=_last_night_before(old_date),
            )
            old_can_take_other = _can_worker_take_shift(
                old_name,
                other_shift,
                other_date,
                last_night_map=_last_night_before(other_date),
            )
            if other_can_take_old and old_can_take_other:
                roster.at[old_idx, "Assigned"] = _write_name_list(current_old + [other_name])
                roster.at[other_idx, "Assigned"] = _write_name_list(current_other + [old_name])
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                locally_legal = (
                    _resident_adjacent_night_penalty(other_name, old_date, daily_assignments) == 0
                    and _resident_adjacent_night_penalty(old_name, other_date, daily_assignments) == 0
                )
                candidate_metrics = _resident_priority_metrics() if locally_legal else current_metrics
                if (
                    locally_legal
                    and _resident_stage_improves(
                        current_metrics,
                        candidate_metrics,
                        "sandwich_total",
                    )
                ):
                    logger.info(
                        "resident rolling sandwich total-preserving swap: "
                        "%s %s %s <-> %s %s %s sandwiches %d -> %d",
                        old_date.isoformat(), old_shift, old_name,
                        other_date.isoformat(), other_shift, other_name,
                        current_metrics.sandwich_total,
                        candidate_metrics.sandwich_total,
                    )
                    return True

            roster.at[old_idx, "Assigned"] = _write_name_list(original_old)
            roster.at[other_idx, "Assigned"] = _write_name_list(original_other)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

        return False

    def _repair_resident_sandwich_swaps(
        max_steps: int = 10,
        *,
        use_cache: bool = True,
    ) -> int:
        label = "resident_sandwich_swaps"
        if use_cache and _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(max_steps):
            if not _try_resident_sandwich_swap():
                if use_cache:
                    _remember_repair_noop(label)
                break
            repaired += 1
            if use_cache:
                _forget_repair_noops()
        return repaired

    def _resident_night_pool() -> set[str]:
        return set(resident_fairness_pool)

    def _refresh_resident_fairness_pool() -> set[str]:
        """Keep only residents whose monthly duty count is legally adjustable."""

        assignment_keys: set[tuple[date, str, str]] = set()
        protected_keys: set[tuple[date, str, str]] = set()
        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)]
        for idx, row in rows.iterrows():
            shift_date = _row_date(idx)
            shift_type = str(row.Shift)
            for name in _assigned_names(idx):
                if name not in active_resident_night_names:
                    continue
                key = (shift_date, shift_type, name)
                assignment_keys.add(key)
                if _resident_assignment_is_protected(*key):
                    protected_keys.add(key)

        initially_flexible = _resident_flexible_comparison_pool(
            active_resident_night_names,
            assignment_keys,
            protected_keys,
            set(),
        )
        receivable_names: set[str] = set()
        candidates = active_resident_night_names - initially_flexible
        if candidates:
            for idx, row in rows.iterrows():
                shift_date = _row_date(idx)
                shift_type = str(row.Shift)
                assigned_here = _assigned_names(idx)
                needed = _to_int(row.get("Needed", 0), 0)
                has_vacancy = len(assigned_here) < needed
                has_movable_assignment = any(
                    not _resident_assignment_is_protected(
                        shift_date,
                        shift_type,
                        assigned_name,
                    )
                    for assigned_name in assigned_here
                )
                if not has_vacancy and not has_movable_assignment:
                    continue
                for name in list(candidates - receivable_names):
                    if _resident_candidate_hard_legal(int(idx), name):
                        receivable_names.add(name)
                if candidates <= receivable_names:
                    break

        flexible = _resident_flexible_comparison_pool(
            active_resident_night_names,
            assignment_keys,
            protected_keys,
            receivable_names,
        )
        excluded = active_resident_night_names - flexible
        if flexible != resident_fairness_pool or excluded != resident_fairness_pool_excluded:
            logger.info(
                "Resident flexible fairness pool: active=%s excluded_fixed_or_capped=%s",
                ", ".join(sorted(flexible)) or "none",
                ", ".join(sorted(excluded)) or "none",
            )
        resident_fairness_pool.clear()
        resident_fairness_pool.update(flexible)
        resident_fairness_pool_excluded.clear()
        resident_fairness_pool_excluded.update(excluded)
        return set(resident_fairness_pool)

    def _resident_night_total_counts() -> Counter:
        pool = _resident_night_pool()
        return _current_resident_total_counts(pool)

    def _resident_night_total_objective() -> tuple[int, int]:
        pool = _resident_night_pool()
        return _count_spread_and_square(_current_resident_total_counts(pool), pool)

    def _resident_weekend_objective() -> tuple[int, int, int, int]:
        pool = _resident_night_pool()
        weekend_objective = _count_spread_and_square(
            Counter({name: weekend_night_counts[name] for name in pool}),
            pool,
        )
        friday_counts = Counter({
            name: weekend_night_counts[name] - saturday_night_counts[name]
            for name in pool
        })
        friday_objective = _count_spread_and_square(friday_counts, pool)
        return (*weekend_objective, *friday_objective)

    def _resident_saturday_pool() -> set[str]:
        """Residents who can structurally participate in Saturday balancing."""

        pool = _resident_night_pool()
        saturday_dates = [d for d in month_dates if d.weekday() == 5]
        reachable: set[str] = set()
        for name in pool:
            if saturday_night_counts[name] > 0:
                reachable.add(name)
                continue
            if any(
                worker_shift_lut.get((name, shift), False)
                and eligibility_reason(name, saturday_date.isoformat(), shift) is None
                for saturday_date in saturday_dates
                for shift in RESIDENT_NIGHT_SHIFTS
            ):
                reachable.add(name)
        return reachable

    def _resident_saturday_objective() -> tuple[int, int]:
        pool = _resident_saturday_pool()
        return _count_spread_and_square(
            Counter({name: saturday_night_counts[name] for name in pool}),
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
        pool = {
            name for name in pool
            if all(worker_shift_lut.get((name, shift), False) for shift in RESIDENT_NIGHT_SHIFTS)
        }
        shift_counters = list(resident_night_shift_counts.values())
        if not pool or len(shift_counters) < 2:
            return (0, 0, 0, 0, 0, 0)
        t1, t2 = shift_counters[:2]
        excess_gaps = [
            _resident_type_excess_gap(month_counts[name], t1[name], t2[name])
            for name in pool
        ]
        t1_spread, t1_square = _count_spread_and_square(t1, pool)
        t2_spread, t2_square = _count_spread_and_square(t2, pool)
        return (
            max(excess_gaps),
            sum(excess_gaps),
            t1_spread,
            t2_spread,
            t1_square,
            t2_square,
        )

    def _resident_weekend_stack_objective(pool: set[str]) -> tuple[int, int]:
        if not pool:
            return (0, 0)
        current_total_counts = _current_resident_total_counts(pool)
        rolling_total_counts = _rolling_resident_total_counts(pool)
        min_current = min(current_total_counts[name] for name in pool)
        min_rolling = min(rolling_total_counts[name] for name in pool)
        current_stack = sum(
            max(0, current_total_counts[name] - min_current) * weekend_night_counts[name]
            for name in pool
        )
        rolling_stack = sum(
            max(0, rolling_total_counts[name] - min_rolling) * (previous_resident_weekend_counts[name] + weekend_night_counts[name])
            for name in pool
        )
        return (current_stack, rolling_stack)

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
        pool = _resident_night_pool()
        current_baseline = min((month_counts[name] for name in pool), default=0)
        weekend_baseline = min((weekend_night_counts[name] for name in pool), default=0)
        penalty = 0
        for name in pool:
            burden = (
                max(0, previous_resident_night_counts[name] - baseline)
                + max(0, month_counts[name] - current_baseline)
                + max(0, weekend_night_counts[name] - weekend_baseline)
            )
            if not burden:
                continue
            penalty += _resident_type_compensation_distance(
                month_counts[name],
                resident_night_shift_counts["ת.מיון"][name],
                resident_night_shift_counts["ת.מיון 2"][name],
                burden,
            )
        return penalty

    def _resident_current_hardship(name: str, pool: set[str] | None = None) -> int:
        if pool is None:
            pool = _resident_night_pool()
        if not pool:
            return 0
        current_baseline = min((month_counts[n] for n in pool), default=0)
        weekend_baseline = min((weekend_night_counts[n] for n in pool), default=0)
        saturday_baseline = min((saturday_night_counts[n] for n in pool), default=0)
        thursday_baseline = min((thursday_night_counts[n] for n in pool), default=0)
        return (
            max(0, month_counts[name] - current_baseline) * 4
            + max(0, weekend_night_counts[name] - weekend_baseline) * 5
            + max(0, saturday_night_counts[name] - saturday_baseline) * 3
            + max(0, thursday_night_counts[name] - thursday_baseline) * 2
        )

    def _resident_current_hardship_type_compensation_total(pool: set[str] | None = None) -> int:
        if pool is None:
            pool = _resident_night_pool()
        penalty = 0
        for name in pool:
            burden = _resident_current_hardship(name, pool)
            if not burden:
                continue
            penalty += _resident_type_compensation_distance(
                month_counts[name],
                resident_night_shift_counts["ת.מיון"][name],
                resident_night_shift_counts["ת.מיון 2"][name],
                burden,
            )
        return penalty

    def _resident_missing_slot_count() -> int:
        return sum(
            max(_to_int(roster.at[idx, "Needed"], 0) - len(_assigned_names(idx)), 0)
            for idx in resident_night_row_indexes
        )

    def _resident_priority_metrics() -> ResidentNightMetrics:
        _rebuild_daily_assignments_from_roster()
        _rebuild_live_counters_from_roster()
        pool = _resident_night_pool()
        current_total_counts = _current_resident_total_counts(pool)
        total_spread, total_square = _count_spread_and_square(current_total_counts, pool)
        actionable_sandwiches = _resident_actionable_sandwich_counts()
        return ResidentNightMetrics(
            missing=_resident_missing_slot_count(),
            total=(total_spread, total_square),
            weekend_friday=_resident_weekend_objective(),
            saturday=_resident_saturday_objective(),
            sandwich_total=sum(actionable_sandwiches[name] for name in pool),
            sandwich_distribution=_resident_sandwich_distribution_objective(
                actionable_sandwiches,
                pool,
            ),
            shift_type=_resident_night_shift_balance_key(pool),
        )

    def _fulfilled_resident_preferred_nights() -> set[tuple[str, date]]:
        fulfilled: set[tuple[str, date]] = set()
        for idx in resident_night_row_indexes:
            shift_type = str(roster.at[idx, "Shift"])
            shift_date = _row_date(idx)
            for name in _assigned_names(idx):
                request_key = (name, shift_date)
                if (
                    request_key in preferred_night_requests
                    and shift_type in _preferred_night_shifts_for(name)
                ):
                    fulfilled.add(request_key)
        return fulfilled

    def _resident_personal_balance_snapshot() -> dict[str, tuple[int, int, int, int, int, int]]:
        actionable_sandwiches = _resident_actionable_sandwich_counts()
        names = (
            set(month_counts)
            | set(weekend_night_counts)
            | set(saturday_night_counts)
            | set(actionable_sandwiches)
        )
        return {
            name: (
                month_counts[name],
                weekend_night_counts[name],
                weekend_night_counts[name] - saturday_night_counts[name],
                saturday_night_counts[name],
                actionable_sandwiches[name],
                _resident_type_excess_gap(
                    month_counts[name],
                    resident_night_shift_counts["ת.מיון"][name],
                    resident_night_shift_counts["ת.מיון 2"][name],
                ),
            )
            for name in names
        }

    def _run_tracking_preferred_night_losses(
        fn: Callable[[], int],
        *,
        expected_stage: str | None = None,
    ) -> int:
        """Run a resident repair and remember which core priority displaced a request."""

        before_fulfilled = _fulfilled_resident_preferred_nights()
        before_personal_balance = _resident_personal_balance_snapshot()
        before_metrics = (
            _resident_priority_metrics()
            if expected_stage is None
            else None
        )
        result = fn()
        after_fulfilled = _fulfilled_resident_preferred_nights()
        after_personal_balance = _resident_personal_balance_snapshot()

        for request_key in after_fulfilled:
            preferred_night_loss_stage.pop(request_key, None)
            preferred_night_loss_detail.pop(request_key, None)

        lost = before_fulfilled - after_fulfilled
        if not lost:
            return result
        gained = after_fulfilled - before_fulfilled

        after_metrics = (
            _resident_priority_metrics()
            if before_metrics is not None
            else None
        )
        improved_stage = expected_stage
        if before_metrics is not None and after_metrics is not None:
            improved_stage = _first_improved_resident_stage(
                before_metrics,
                after_metrics,
            )

        remaining_gained_by_name: dict[str, list[date]] = defaultdict(list)
        for gained_name, gained_date in gained:
            remaining_gained_by_name[gained_name].append(gained_date)
        for gained_dates in remaining_gained_by_name.values():
            gained_dates.sort()

        for request_key in sorted(lost, key=lambda item: (item[0], item[1])):
            preferred_night_loss_stage[request_key] = improved_stage or "unprotected"
            if improved_stage in RESIDENT_PRIORITY_STAGES:
                zero_balance = (0, 0, 0, 0, 0, 0)
                preferred_night_loss_detail.setdefault(request_key, {})[
                    "balance_scope"
                ] = _resident_balance_scope(
                    improved_stage,
                    before_personal_balance.get(request_key[0], zero_balance),
                    after_personal_balance.get(request_key[0], zero_balance),
                )
            replacement_dates = remaining_gained_by_name.get(request_key[0], [])
            if replacement_dates:
                replacement_date = min(
                    replacement_dates,
                    key=lambda candidate: (
                        abs((candidate - request_key[1]).days),
                        candidate,
                    ),
                )
                replacement_dates.remove(replacement_date)
                preferred_night_loss_detail.setdefault(request_key, {})[
                    "replacement_date"
                ] = replacement_date.isoformat()
            logger.info(
                "preferred night displaced: %s %s stage=%s replacement=%s metrics %s -> %s",
                request_key[1].isoformat(),
                request_key[0],
                preferred_night_loss_stage[request_key],
                preferred_night_loss_detail.get(request_key, {}).get("replacement_date"),
                before_metrics if before_metrics is not None else expected_stage,
                after_metrics if after_metrics is not None else expected_stage,
            )
        return result

    def _resident_night_objective() -> ResidentNightObjective:
        core = _resident_priority_metrics()
        pool = _resident_night_pool()
        rolling_total = _resident_rolling_total_objective()
        rolling_weekend = _count_spread_and_square(_rolling_resident_weekend_counts(pool), pool)
        return ResidentNightObjective(
            core=core,
            preferred=_preferred_night_miss_objective(),
            personal=_resident_night_personal_preference_total(),
            thursday=_count_spread_and_square(thursday_night_counts, pool),
            history=(
                *rolling_total,
                *rolling_weekend,
                _resident_rolling_sandwich_total(pool),
                _resident_type_compensation_total(),
            ),
        )

    def _resident_hard_objective(objective: ResidentNightObjective) -> tuple[object, ...]:
        # Callers compare the complete protected core, including the final
        # ת.מיון/ת.מיון 2 balance stage. Preferred-request recovery itself
        # requires equality across this complete key.
        return _resident_core_key(objective.core)

    def _resident_preference_objective(objective: ResidentNightObjective) -> tuple[int, ...]:
        return objective.preferred

    def _resident_shift_type_objective(objective: ResidentNightObjective) -> tuple[int, int, int, int, int, int]:
        return objective.core.shift_type

    def _try_resident_priority_swap(stage: str, max_evaluations: int = 60) -> bool:
        """Try a legal two-resident swap that improves one protected stage."""
        before = _resident_priority_metrics()
        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        if rows.empty:
            return False
        rows["_date"] = rows["Date"].map(lambda value: date.fromisoformat(str(value)))

        assignments: list[tuple[int, date, str, str]] = []
        for idx, row in rows.iterrows():
            d = row["_date"]
            shift = str(row.Shift)
            for name in _name_list(row.Assigned):
                assignments.append((int(idx), d, shift, name))

        pool = _resident_night_pool()
        friday_counts = Counter({
            name: weekend_night_counts[name] - saturday_night_counts[name]
            for name in pool
        })
        sandwich_counts = _resident_actionable_sandwich_counts()
        sandwich_endpoints = {
            (name, endpoint)
            for name, first, second in _find_resident_sandwiches(include_history=True)
            for endpoint in (first, second)
        }
        requested_sandwich_endpoints = _resident_requested_sandwich_endpoints()

        candidates: list[tuple[int, int, int, int, str, int, int, str, str]] = []
        seen: set[tuple[int, str, int, str]] = set()
        for a_pos, (a_idx, a_date, a_shift, a_name) in enumerate(assignments):
            if _resident_assignment_is_protected(a_date, a_shift, a_name):
                continue
            for b_idx, b_date, b_shift, b_name in assignments[a_pos + 1:]:
                if a_idx == b_idx or a_name == b_name:
                    continue
                if _resident_assignment_is_protected(b_date, b_shift, b_name):
                    continue
                key = (a_idx, a_name, b_idx, b_name)
                if key in seen:
                    continue
                seen.add(key)

                a_weekday = a_date.weekday()
                b_weekday = b_date.weekday()
                potential_gain = 0
                if stage == "weekend_friday":
                    a_weekend = a_weekday in (4, 5)
                    b_weekend = b_weekday in (4, 5)
                    if a_weekend != b_weekend:
                        weekend_name = a_name if a_weekend else b_name
                        weekday_name = b_name if a_weekend else a_name
                        potential_gain = (
                            weekend_night_counts[weekend_name]
                            - weekend_night_counts[weekday_name]
                        )
                    elif {a_weekday, b_weekday} == {4, 5}:
                        friday_name = a_name if a_weekday == 4 else b_name
                        saturday_name = b_name if a_weekday == 4 else a_name
                        potential_gain = friday_counts[friday_name] - friday_counts[saturday_name]
                    else:
                        continue
                    if potential_gain <= 0:
                        continue
                elif stage == "saturday":
                    saturday_swap = _resident_saturday_swap_gain(
                        a_weekday,
                        a_name,
                        b_weekday,
                        b_name,
                        saturday_night_counts,
                    )
                    if saturday_swap is None:
                        continue
                    _, _, potential_gain = saturday_swap
                    if potential_gain <= 0:
                        continue
                elif stage in {"sandwich_total", "sandwich_distribution"}:
                    if a_date == b_date:
                        continue
                    if (
                        (a_name, a_date) in requested_sandwich_endpoints
                        or (b_name, b_date) in requested_sandwich_endpoints
                    ):
                        continue
                    if stage == "sandwich_total":
                        if (a_name, a_date) not in sandwich_endpoints and (b_name, b_date) not in sandwich_endpoints:
                            continue
                        potential_gain = sandwich_counts[a_name] + sandwich_counts[b_name]
                    else:
                        potential_gain = abs(sandwich_counts[a_name] - sandwich_counts[b_name])
                        if potential_gain < 2:
                            continue
                elif stage == "shift_type":
                    if a_shift == b_shift:
                        continue
                    a_t1 = resident_night_shift_counts["ת.מיון"][a_name]
                    a_t2 = resident_night_shift_counts["ת.מיון 2"][a_name]
                    b_t1 = resident_night_shift_counts["ת.מיון"][b_name]
                    b_t2 = resident_night_shift_counts["ת.מיון 2"][b_name]
                    if a_shift == "ת.מיון":
                        useful = a_t1 > a_t2 and b_t2 > b_t1
                    else:
                        useful = a_t2 > a_t1 and b_t1 > b_t2
                    if not useful:
                        continue
                    potential_gain = abs(a_t1 - a_t2) + abs(b_t1 - b_t2)
                else:
                    raise ValueError(f"Unsupported resident swap stage: {stage}")

                candidates.append((
                    -potential_gain,
                    _preferred_night_removal_order_cost(a_name, a_shift, a_date)
                    + _preferred_night_removal_order_cost(b_name, b_shift, b_date),
                    0 if a_shift == b_shift else 1,
                    abs((a_date - b_date).days),
                    min(a_date, b_date).isoformat(),
                    a_idx,
                    b_idx,
                    a_name,
                    b_name,
                ))

        evaluations = 0
        for _, _, _, _, _, a_idx, b_idx, a_name, b_name in sorted(candidates):
            evaluations += 1
            if evaluations > max_evaluations:
                break
            a_date = _row_date(a_idx)
            b_date = _row_date(b_idx)
            a_shift = str(roster.at[a_idx, "Shift"])
            b_shift = str(roster.at[b_idx, "Shift"])
            original_a = _assigned_names(a_idx)
            original_b = _assigned_names(b_idx)
            if a_name not in original_a or b_name not in original_b:
                continue

            snapshot = roster["Assigned"].copy()
            roster.at[a_idx, "Assigned"] = _write_name_list(
                [name for name in original_a if name != a_name]
            )
            roster.at[b_idx, "Assigned"] = _write_name_list(
                [name for name in original_b if name != b_name]
            )
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            if not (
                _resident_candidate_hard_legal(a_idx, b_name)
                and _resident_candidate_hard_legal(b_idx, a_name)
            ):
                _restore_resident_assignments(snapshot)
                continue

            roster.at[a_idx, "Assigned"] = _write_name_list(_assigned_names(a_idx) + [b_name])
            roster.at[b_idx, "Assigned"] = _write_name_list(_assigned_names(b_idx) + [a_name])
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            after = _resident_priority_metrics()
            distribution_total_unchanged = (
                stage != "sandwich_distribution"
                or after.sandwich_total == before.sandwich_total
            )
            if distribution_total_unchanged and _resident_stage_improves(before, after, stage):
                logger.info(
                    "resident %s swap: %s %s %s <-> %s %s %s metrics %s -> %s",
                    stage,
                    a_date.isoformat(),
                    a_shift,
                    a_name,
                    b_date.isoformat(),
                    b_shift,
                    b_name,
                    before,
                    after,
                )
                _forget_repair_noops()
                return True
            _restore_resident_assignments(snapshot)
        return False

    def _try_resident_saturday_replacement(max_evaluations: int = 48) -> bool:
        """Try a one-slot Saturday replacement that preserves every higher stage."""

        before = _resident_priority_metrics()
        pool = _resident_saturday_pool()
        if len(pool) < 2:
            return False

        candidates: list[tuple[int, int, str, int, str, str]] = []
        for idx in resident_night_row_indexes:
            shift_date = _row_date(idx)
            if shift_date.weekday() != 5:
                continue
            shift_type = str(roster.at[idx, "Shift"])
            for high_name in _assigned_names(idx):
                if high_name not in pool or _resident_assignment_is_protected(
                    shift_date,
                    shift_type,
                    high_name,
                ):
                    continue
                for low_name in pool:
                    gain = saturday_night_counts[high_name] - saturday_night_counts[low_name]
                    if low_name == high_name or gain <= 0:
                        continue
                    candidates.append((
                        -gain,
                        _preferred_night_removal_order_cost(high_name, shift_type, shift_date),
                        shift_date.isoformat(),
                        int(idx),
                        high_name,
                        low_name,
                    ))

        for _, _, _, idx, high_name, low_name in sorted(candidates)[:max_evaluations]:
            shift_date = _row_date(idx)
            shift_type = str(roster.at[idx, "Shift"])
            original = _assigned_names(idx)
            if high_name not in original or low_name in original:
                continue
            snapshot = roster["Assigned"].copy()
            roster.at[idx, "Assigned"] = _write_name_list(
                [name for name in original if name != high_name]
            )
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            if not _resident_candidate_hard_legal(idx, low_name):
                _restore_resident_assignments(snapshot)
                continue

            roster.at[idx, "Assigned"] = _write_name_list(_assigned_names(idx) + [low_name])
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            after = _resident_priority_metrics()
            if _resident_stage_improves(before, after, "saturday"):
                logger.info(
                    "resident saturday replacement: %s %s %s -> %s metrics %s -> %s",
                    shift_date.isoformat(),
                    shift_type,
                    high_name,
                    low_name,
                    before,
                    after,
                )
                _forget_repair_noops()
                return True
            _restore_resident_assignments(snapshot)
        return False

    def _try_resident_saturday_cycle(max_evaluations: int = 48) -> bool:
        """Try a bounded three-assignment cycle when direct Saturday repairs fail."""

        before = _resident_priority_metrics()
        pool = _resident_saturday_pool()
        assignments: list[tuple[int, date, str, str]] = []
        for idx in resident_night_row_indexes:
            d = _row_date(idx)
            shift = str(roster.at[idx, "Shift"])
            for name in _assigned_names(idx):
                if name in pool and not _resident_assignment_is_protected(d, shift, name):
                    assignments.append((int(idx), d, shift, name))

        saturday_assignments = [item for item in assignments if item[1].weekday() == 5]
        other_assignments = [item for item in assignments if item[1].weekday() != 5]
        candidates: list[
            tuple[int, int, int, str, int, int, int, str, str, str]
        ] = []
        for a_idx, a_date, a_shift, high_name in saturday_assignments:
            for b_idx, b_date, b_shift, low_name in other_assignments:
                if low_name == high_name:
                    continue
                gain = saturday_night_counts[high_name] - saturday_night_counts[low_name]
                if gain <= 0:
                    continue
                for c_idx, c_date, c_shift, third_name in other_assignments:
                    if (
                        len({a_idx, b_idx, c_idx}) < 3
                        or len({a_date, b_date, c_date}) < 3
                        or third_name in {high_name, low_name}
                    ):
                        continue
                    candidates.append((
                        -gain,
                        _preferred_night_removal_order_cost(high_name, a_shift, a_date)
                        + _preferred_night_removal_order_cost(low_name, b_shift, b_date)
                        + _preferred_night_removal_order_cost(third_name, c_shift, c_date),
                        max(
                            abs((a_date - b_date).days),
                            abs((a_date - c_date).days),
                            abs((b_date - c_date).days),
                        ),
                        a_date.isoformat(),
                        a_idx,
                        b_idx,
                        c_idx,
                        high_name,
                        low_name,
                        third_name,
                    ))

        evaluations = 0
        for _, _, _, _, a_idx, b_idx, c_idx, high_name, low_name, third_name in sorted(candidates):
            evaluations += 1
            if evaluations > max_evaluations:
                break
            a_date, b_date, c_date = _row_date(a_idx), _row_date(b_idx), _row_date(c_idx)
            a_shift = str(roster.at[a_idx, "Shift"])
            b_shift = str(roster.at[b_idx, "Shift"])
            c_shift = str(roster.at[c_idx, "Shift"])
            original_a = _assigned_names(a_idx)
            original_b = _assigned_names(b_idx)
            original_c = _assigned_names(c_idx)
            if (
                high_name not in original_a
                or low_name not in original_b
                or third_name not in original_c
                or _resident_assignment_is_protected(a_date, a_shift, high_name)
                or _resident_assignment_is_protected(b_date, b_shift, low_name)
                or _resident_assignment_is_protected(c_date, c_shift, third_name)
            ):
                continue

            snapshot = roster["Assigned"].copy()
            roster.at[a_idx, "Assigned"] = _write_name_list(
                [name for name in original_a if name != high_name]
            )
            roster.at[b_idx, "Assigned"] = _write_name_list(
                [name for name in original_b if name != low_name]
            )
            roster.at[c_idx, "Assigned"] = _write_name_list(
                [name for name in original_c if name != third_name]
            )
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            if not (
                _resident_candidate_hard_legal(a_idx, low_name)
                and _resident_candidate_hard_legal(b_idx, third_name)
                and _resident_candidate_hard_legal(c_idx, high_name)
            ):
                _restore_resident_assignments(snapshot)
                continue

            roster.at[a_idx, "Assigned"] = _write_name_list(_assigned_names(a_idx) + [low_name])
            roster.at[b_idx, "Assigned"] = _write_name_list(_assigned_names(b_idx) + [third_name])
            roster.at[c_idx, "Assigned"] = _write_name_list(_assigned_names(c_idx) + [high_name])
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            after = _resident_priority_metrics()
            if _resident_stage_improves(before, after, "saturday"):
                logger.info(
                    "resident saturday cycle: %s %s -> %s; %s %s -> %s; %s %s -> %s metrics %s -> %s",
                    a_date.isoformat(),
                    high_name,
                    low_name,
                    b_date.isoformat(),
                    low_name,
                    third_name,
                    c_date.isoformat(),
                    third_name,
                    high_name,
                    before,
                    after,
                )
                _forget_repair_noops()
                return True
            _restore_resident_assignments(snapshot)
        return False

    def _repair_resident_priority_swaps(
        stage: str,
        *,
        max_steps: int,
        max_evaluations: int = 60,
    ) -> int:
        label = f"resident_priority_{stage}"
        if _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(max_steps):
            if not _try_resident_priority_swap(stage, max_evaluations=max_evaluations):
                _remember_repair_noop(label)
                break
            repaired += 1
        return repaired

    def _repair_resident_saturday_balance(max_steps: int = 12) -> int:
        label = "resident_priority_saturday"
        if _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(max_steps):
            changed = (
                _try_resident_priority_swap("saturday", max_evaluations=120)
                or _try_resident_saturday_replacement()
                or _try_resident_saturday_cycle()
            )
            if not changed:
                _remember_repair_noop(label)
                break
            repaired += 1
            _forget_repair_noops()
        return repaired

    def _repair_resident_sandwich_distribution(max_steps: int = 10) -> int:
        return _repair_resident_priority_swaps(
            "sandwich_distribution",
            max_steps=max_steps,
        )

    def _run_resident_stage_repair(stage: str, repair: Callable[[], int]) -> int:
        """Rollback a legacy repair if its final result violates the priority stage."""
        before = _resident_priority_metrics()
        snapshot = roster["Assigned"].copy()
        changed = repair()
        if not changed:
            return 0
        after = _resident_priority_metrics()
        if _resident_stage_improves(before, after, stage):
            return changed
        _restore_resident_assignments(snapshot)
        logger.info(
            "Rolled back resident %s repair that did not preserve the priority order: %s -> %s",
            stage,
            before,
            after,
        )
        return 0

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
        *,
        allow_sandwich: bool = False,
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
            and (
                allow_sandwich
                or _resident_night_spacing_penalty_for(w, shift_date, effective_last_night) < 100
            )
        ]

        roster.at[idx, "Assigned"] = _write_name_list(original)
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()
        return candidates

    def _try_best_resident_night_improvement(max_evaluations: int = 300) -> bool:
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
            sandwich_names = {
                name for name, _, _ in _find_resident_sandwiches(include_history=True)
            }
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
                            _preferred_night_removal_order_cost(name, shift_type, shift_date),
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
                            if evaluated > max_evaluations:
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
        current_compensation = _resident_type_compensation_total()
        current_hardship_compensation = _resident_current_hardship_type_compensation_total()
        compensation_protected = _resident_type_gap_within(limit=2)
        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        if rows.empty:
            return False

        rows_by_date_shift: dict[tuple[str, str], int] = {}
        for idx, row in rows.iterrows():
            rows_by_date_shift[(str(row.Date), str(row.Shift))] = idx

        def _can_swap_same_date_resident_type(name: str, target_shift: str, shift_date: date) -> bool:
            if not worker_shift_lut.get((name, target_shift), False):
                return False
            if eligibility_reason(name, shift_date.isoformat(), target_shift) == "capability":
                return False
            return True

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
                    )
                    after = (
                        abs((resident_night_shift_counts["ת.מיון"][name_a] - 1) - (resident_night_shift_counts["ת.מיון 2"][name_a] + 1))
                        + abs((resident_night_shift_counts["ת.מיון"][name_b] + 1) - (resident_night_shift_counts["ת.מיון 2"][name_b] - 1))
                    )
                    improvement = before - after
                    if improvement < 0:
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

            a_can_take_b = (
                _can_swap_same_date_resident_type(name_a, "ת.מיון 2", shift_date)
            )
            b_can_take_a = (
                _can_swap_same_date_resident_type(name_b, "ת.מיון", shift_date)
            )

            if a_can_take_b and b_can_take_a:
                roster.at[idx_a, "Assigned"] = _write_name_list(current_a + [name_b])
                roster.at[idx_b, "Assigned"] = _write_name_list(current_b + [name_a])
                candidate_objective = _resident_night_objective()
                candidate_compensation = _resident_type_compensation_total()
                candidate_hardship_compensation = _resident_current_hardship_type_compensation_total()
                if (
                    _resident_hard_objective(candidate_objective) <= _resident_hard_objective(current_objective)
                    and _resident_shift_type_objective(candidate_objective) <= _resident_shift_type_objective(current_objective)
                    and (not compensation_protected or candidate_compensation <= current_compensation)
                    and (not compensation_protected or candidate_hardship_compensation <= current_hardship_compensation)
                    and candidate_objective < current_objective
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
        current_sandwiches = _resident_rolling_sandwich_total(pool)
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
                        _preferred_night_removal_order_cost(high_name, a_shift, a_date),
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
                            _preferred_night_removal_order_cost(high_name, a_shift, a_date),
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
                new_sandwiches = _resident_rolling_sandwich_total(pool)
                # Sandwiches are lower priority than weekend/Friday balance.
                # Candidate ordering and later sandwich passes minimize any cost.
                sandwich_cost_ok = True
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
                new_sandwiches = _resident_rolling_sandwich_total(pool)
                # Sandwiches are lower priority than weekend/Friday balance.
                sandwich_cost_ok = True
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
        current_compensation = _resident_type_compensation_total()
        current_hardship_compensation = _resident_current_hardship_type_compensation_total()
        compensation_protected = _resident_type_gap_within(limit=2)
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
                        )
                        after = (
                            abs((resident_night_shift_counts[a_shift][name_a] - 1) - (resident_night_shift_counts[b_shift][name_a] + 1))
                            + abs((resident_night_shift_counts[a_shift][name_b] + 1) - (resident_night_shift_counts[b_shift][name_b] - 1))
                        )
                        improvement = before - after
                        if improvement < 0:
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
                and _resident_night_spacing_penalty_for(name_b, a_date, a_last_night) < 100
            )
            a_can_take_b = (
                _can_worker_take_shift(
                    name_a,
                    b_shift,
                    b_date,
                    last_night_map=b_last_night,
                )
                and _resident_adjacent_night_penalty(name_a, b_date, daily_assignments) == 0
                and _resident_night_spacing_penalty_for(name_a, b_date, b_last_night) < 100
            )

            if b_can_take_a and a_can_take_b:
                roster.at[a_idx, "Assigned"] = _write_name_list(current_a + [name_b])
                roster.at[b_idx, "Assigned"] = _write_name_list(current_b + [name_a])
                candidate_objective = _resident_night_objective()
                candidate_compensation = _resident_type_compensation_total()
                candidate_hardship_compensation = _resident_current_hardship_type_compensation_total()
                if (
                    _resident_hard_objective(candidate_objective) <= _resident_hard_objective(current_objective)
                    and _resident_preference_objective(candidate_objective) <= _resident_preference_objective(current_objective)
                    and _resident_shift_type_objective(candidate_objective) <= _resident_shift_type_objective(current_objective)
                    and (not compensation_protected or candidate_compensation <= current_compensation)
                    and (not compensation_protected or candidate_hardship_compensation <= current_hardship_compensation)
                    and candidate_objective < current_objective
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

    def _resident_compensation_load(name: str) -> int:
        pool = _resident_night_pool()
        if not pool:
            return 0
        current_baseline = min(month_counts[n] for n in pool)
        weekend_baseline = min(weekend_night_counts[n] for n in pool)
        previous_baseline = _previous_resident_night_baseline()
        previous_weekend_baseline = min((previous_resident_weekend_counts[n] for n in pool), default=0)
        return (
            max(0, month_counts[name] - current_baseline) * 5
            + max(0, weekend_night_counts[name] - weekend_baseline) * 5
            + max(0, previous_resident_night_counts[name] - previous_baseline) * 2
            + max(0, previous_resident_weekend_counts[name] - previous_weekend_baseline) * 2
        )

    def _resident_type_gap_within(limit: int = 2) -> bool:
        for name in _resident_night_pool():
            gap = _resident_type_excess_gap(
                month_counts[name],
                resident_night_shift_counts["ת.מיון"][name],
                resident_night_shift_counts["ת.מיון 2"][name],
            )
            if gap > limit:
                return False
        return True

    def _try_resident_compensation_type_swap() -> bool:
        """
        Prefer ת.מיון 2 as compensation for residents carrying heavier total or
        weekend load without worsening anyone's parity-adjusted type balance.
        """
        current_objective = _resident_night_objective()
        current_hard = _resident_hard_objective(current_objective)
        current_preference = _resident_preference_objective(current_objective)
        current_shift_balance = _resident_night_shift_balance_key(_resident_night_pool())
        current_compensation = _resident_type_compensation_total()
        current_weekend_history = _resident_weekend_history_load()
        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        if rows.empty:
            return False

        rows["_date"] = rows["Date"].map(lambda x: date.fromisoformat(str(x)))
        tmion_rows = rows[rows["Shift"] == "ת.מיון"]
        tmion2_rows = rows[rows["Shift"] == "ת.מיון 2"]
        compensation_loads = {
            name: _resident_compensation_load(name)
            for name in _resident_night_pool()
        }

        candidates: list[tuple[int, int, str, int, int, str, str]] = []
        for a_idx, a_row in tmion_rows.iterrows():
            a_date = a_row["_date"]
            for heavy_name in _name_list(a_row.Assigned):
                if (a_date, "ת.מיון", heavy_name) in fixed_assignment_keys:
                    continue
                heavy_load = compensation_loads[heavy_name]
                if heavy_load <= 0:
                    continue

                for b_idx, b_row in tmion2_rows.iterrows():
                    b_date = b_row["_date"]
                    for lighter_name in _name_list(b_row.Assigned):
                        if heavy_name == lighter_name:
                            continue
                        if (b_date, "ת.מיון 2", lighter_name) in fixed_assignment_keys:
                            continue
                        lighter_load = compensation_loads[lighter_name]
                        if lighter_load >= heavy_load:
                            continue
                        candidates.append((
                            heavy_load - lighter_load,
                            heavy_load,
                            a_date.isoformat(),
                            a_idx,
                            b_idx,
                            heavy_name,
                            lighter_name,
                        ))

        # The full Cartesian product is large, and every trial recomputes the
        # resident-night objective. Check the strongest compensation candidates
        # first; weaker candidates cannot beat them unless legality blocks all
        # stronger options.
        for _, _, _, a_idx, b_idx, heavy_name, lighter_name in sorted(
            candidates,
            key=lambda item: (-item[0], -item[1], item[2]),
        )[:240]:
            a_row = roster.loc[a_idx]
            b_row = roster.loc[b_idx]
            a_date = date.fromisoformat(str(a_row.Date))
            b_date = date.fromisoformat(str(b_row.Date))
            original_a = _name_list(a_row.Assigned)
            original_b = _name_list(b_row.Assigned)
            if heavy_name not in original_a or lighter_name not in original_b:
                continue

            current_a = [name for name in original_a if name != heavy_name]
            current_b = [name for name in original_b if name != lighter_name]
            roster.at[a_idx, "Assigned"] = _write_name_list(current_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(current_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            a_last_night = _last_night_before(a_date)
            b_last_night = _last_night_before(b_date)
            light_can_take_tmion = (
                _can_worker_take_shift(
                    lighter_name,
                    "ת.מיון",
                    a_date,
                    last_night_map=a_last_night,
                )
                and _resident_adjacent_night_penalty(lighter_name, a_date, daily_assignments) == 0
                and _resident_night_spacing_penalty_for(lighter_name, a_date, a_last_night) < 100
            )
            heavy_can_take_tmion2 = (
                _can_worker_take_shift(
                    heavy_name,
                    "ת.מיון 2",
                    b_date,
                    last_night_map=b_last_night,
                )
                and _resident_adjacent_night_penalty(heavy_name, b_date, daily_assignments) == 0
                and _resident_night_spacing_penalty_for(heavy_name, b_date, b_last_night) < 100
            )

            if light_can_take_tmion and heavy_can_take_tmion2:
                roster.at[a_idx, "Assigned"] = _write_name_list(current_a + [lighter_name])
                roster.at[b_idx, "Assigned"] = _write_name_list(current_b + [heavy_name])
                candidate_objective = _resident_night_objective()
                if (
                    _resident_hard_objective(candidate_objective) <= current_hard
                    and _resident_preference_objective(candidate_objective) <= current_preference
                    and _resident_weekend_history_load() <= current_weekend_history
                    and _resident_night_shift_balance_key(_resident_night_pool()) <= current_shift_balance
                    and _resident_type_compensation_total() < current_compensation
                ):
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    logger.info(
                        "resident compensation type swap: %s ת.מיון %s -> %s; %s ת.מיון 2 %s -> %s objective %s -> %s",
                        a_date.isoformat(), heavy_name, lighter_name,
                        b_date.isoformat(), lighter_name, heavy_name,
                        current_objective, candidate_objective,
                    )
                    return True

            roster.at[a_idx, "Assigned"] = _write_name_list(original_a)
            roster.at[b_idx, "Assigned"] = _write_name_list(original_b)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

        return False

    def _try_preferred_resident_night_swap(
        *,
        protect_through_stage: str | None = None,
    ) -> bool:
        current_objective = _resident_night_objective()
        requests = sorted(
            preferred_night_requests.items(),
            key=lambda item: (
                -item[1],
                0 if item[0][1].weekday() in (4, 5) else 1,
                _preferred_other_request_approval_percentage(item[0][0], item[0][1]),
                item[0][1],
                item[0][0],
            ),
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
                        _preferred_night_removal_order_cost(n, pref_shift, pref_date),
                        resident_night_shift_counts[pref_shift][n],
                        n,
                    ),
                ):
                    if (
                        displaced == name
                        or (pref_date, pref_shift, displaced) in fixed_assignment_keys
                        or (pref_date, pref_shift, displaced) in mandatory_personal_assignment_keys
                    ):
                        continue

                    donor_rows = resident_rows[
                        resident_rows["Assigned"].astype(str).map(lambda cell: name in _name_list(cell))
                    ]
                    for donor_idx, donor_row in donor_rows.iterrows():
                        donor_date = date.fromisoformat(str(donor_row.Date))
                        donor_shift = str(donor_row.Shift)
                        if donor_idx == pref_idx:
                            continue
                        if (
                            (donor_date, donor_shift, name) in fixed_assignment_keys
                            or (donor_date, donor_shift, name) in mandatory_personal_assignment_keys
                        ):
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
                            and _resident_night_spacing_penalty_for(name, pref_date, pref_last_night) < 100
                        )
                        displaced_can_take_donor = (
                            _can_worker_take_shift(
                                displaced,
                                donor_shift,
                                donor_date,
                                last_night_map=donor_last_night,
                            )
                            and _resident_adjacent_night_penalty(displaced, donor_date, daily_assignments) == 0
                            and _resident_night_spacing_penalty_for(displaced, donor_date, donor_last_night) < 100
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
                                _resident_core_preserved_for_request_recovery(
                                    current_objective.core,
                                    candidate_objective.core,
                                    protect_through_stage,
                                )
                                and _resident_preference_objective(candidate_objective)
                                < _resident_preference_objective(current_objective)
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

    def _repair_preferred_resident_night_requests(
        max_steps: int = 8,
        *,
        protect_through_stage: str | None = None,
        use_cache: bool = True,
    ) -> int:
        """Improve request outcomes without changing the protected core in scope.

        During balancing, ``protect_through_stage`` preserves the completed
        prefix and deliberately leaves later stages free.  The final pass uses
        the default and therefore requires equality across the complete core.
        """

        label = (
            "preferred_resident_night_requests"
            if protect_through_stage is None
            else f"preferred_resident_night_requests_{protect_through_stage}"
        )
        if use_cache and _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(max_steps):
            if not _try_preferred_resident_night_swap(
                protect_through_stage=protect_through_stage,
            ):
                if use_cache:
                    _remember_repair_noop(label)
                break
            repaired += 1
            _forget_repair_noops()
        return repaired

    def _run_resident_balancing_stage_with_preference_preservation(
        stage: str,
        repair: Callable[[], int],
        *,
        stage_label: str,
    ) -> int:
        """Run one protected stage and recover requests across equal stage results."""

        before_preference = _preferred_night_miss_objective()
        changed = repair()
        after_preference = _preferred_night_miss_objective()
        if after_preference <= before_preference:
            return changed

        restored = _repair_preferred_resident_night_requests(
            max_steps=min(12, max(4, len(preferred_night_requests))),
            protect_through_stage=stage,
            use_cache=False,
        )
        if restored:
            logger.info(
                "Restored %d preferred resident-night assignment(s) after %s "
                "while preserving the protected core through %s",
                restored,
                stage_label,
                stage,
            )
        return changed + restored

    def _try_current_hardship_same_day_type_swap() -> bool:
        """
        Final cheap correction: if two residents are already assigned on the
        same date, give ת.מיון 2 to the one carrying the heavier current-month
        pattern. This does not move nights between dates, so totals/weekends/
        Thursdays/sandwiches remain protected.
        """
        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        if rows.empty:
            return False
        current_objective = _resident_night_objective()
        current_hard = _resident_hard_objective(current_objective)
        current_preference = _resident_preference_objective(current_objective)
        current_shift_balance = _resident_night_shift_balance_key(_resident_night_pool())
        current_compensation = _resident_current_hardship_type_compensation_total()

        rows_by_date_shift: dict[tuple[str, str], int] = {}
        for idx, row in rows.iterrows():
            rows_by_date_shift[(str(row.Date), str(row.Shift))] = idx

        candidates: list[tuple[int, int, str, int, int, str, str]] = []
        for d_iso in sorted({str(row.Date) for _, row in rows.iterrows()}):
            idx_tmion = rows_by_date_shift.get((d_iso, "ת.מיון"))
            idx_tmion2 = rows_by_date_shift.get((d_iso, "ת.מיון 2"))
            if idx_tmion is None or idx_tmion2 is None:
                continue
            shift_date = date.fromisoformat(d_iso)
            tmion_names = _name_list(roster.at[idx_tmion, "Assigned"])
            tmion2_names = _name_list(roster.at[idx_tmion2, "Assigned"])
            for heavy_name in tmion_names:
                if (shift_date, "ת.מיון", heavy_name) in fixed_assignment_keys:
                    continue
                heavy_load = _resident_current_hardship(heavy_name)
                if heavy_load <= 0:
                    continue
                for lighter_name in tmion2_names:
                    if heavy_name == lighter_name:
                        continue
                    if (shift_date, "ת.מיון 2", lighter_name) in fixed_assignment_keys:
                        continue
                    lighter_load = _resident_current_hardship(lighter_name)
                    if heavy_load <= lighter_load:
                        continue
                    candidates.append((
                        heavy_load - lighter_load,
                        heavy_load,
                        d_iso,
                        idx_tmion,
                        idx_tmion2,
                        heavy_name,
                        lighter_name,
                    ))

        for _, _, d_iso, idx_tmion, idx_tmion2, heavy_name, lighter_name in sorted(
            candidates,
            key=lambda item: (-item[0], -item[1], item[2]),
        ):
            original_tmion = _name_list(roster.at[idx_tmion, "Assigned"])
            original_tmion2 = _name_list(roster.at[idx_tmion2, "Assigned"])
            if heavy_name not in original_tmion or lighter_name not in original_tmion2:
                continue

            roster.at[idx_tmion, "Assigned"] = _write_name_list(
                [name for name in original_tmion if name != heavy_name] + [lighter_name]
            )
            roster.at[idx_tmion2, "Assigned"] = _write_name_list(
                [name for name in original_tmion2 if name != lighter_name] + [heavy_name]
            )
            candidate_objective = _resident_night_objective()
            candidate_shift_balance = _resident_night_shift_balance_key(_resident_night_pool())
            candidate_compensation = _resident_current_hardship_type_compensation_total()
            if (
                _resident_hard_objective(candidate_objective) <= current_hard
                and _resident_preference_objective(candidate_objective) <= current_preference
                and candidate_shift_balance <= current_shift_balance
                and candidate_compensation < current_compensation
            ):
                logger.info(
                    "current hardship type compensation swap: %s ת.מיון %s -> %s; ת.מיון 2 %s -> %s current_comp %s -> %s",
                    d_iso,
                    heavy_name,
                    lighter_name,
                    lighter_name,
                    heavy_name,
                    current_compensation,
                    candidate_compensation,
                )
                return True

            roster.at[idx_tmion, "Assigned"] = _write_name_list(original_tmion)
            roster.at[idx_tmion2, "Assigned"] = _write_name_list(original_tmion2)
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
                or _try_resident_compensation_type_swap()
            ):
                improved += 1
                _forget_repair_noops()
            else:
                _remember_repair_noop(label)
                break
        return improved

    def _repair_resident_shift_type_final(max_steps: int = 8, *, use_cache: bool = True) -> int:
        label = "resident_shift_type_final"
        if use_cache and _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(max_steps):
            if (
                _try_resident_night_type_swap()
                or _try_cross_date_resident_night_type_swap()
                or _try_resident_compensation_type_swap()
                or _try_current_hardship_same_day_type_swap()
            ):
                repaired += 1
                if use_cache:
                    _forget_repair_noops()
            else:
                if use_cache:
                    _remember_repair_noop(label)
                break
        return repaired

    def _repair_resident_weekend_balance(max_steps: int = 24, *, use_cache: bool = True) -> int:
        label = "resident_weekend_balance"
        if use_cache and _repair_noop_cached(label):
            return 0
        repaired = 0
        for _ in range(max_steps):
            if not _try_resident_weekend_swap():
                if use_cache:
                    _remember_repair_noop(label)
                break
            repaired += 1
            if use_cache:
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

        current_objective = _resident_night_objective()
        current_weekend = _resident_weekend_objective()
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
                if _resident_assignment_is_protected(shift_date, shift_type, high_name):
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
                    candidate_objective = _resident_night_objective()
                    if _resident_history_tiebreak_improves(
                        current_objective,
                        candidate_objective,
                    ):
                        logger.info(
                            "resident history tie-break: %s %s %s replaced; history %s -> %s",
                            shift_date.isoformat(),
                            shift_type,
                            high_name,
                            current_objective.history,
                            candidate_objective.history,
                        )
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

    def _resident_higher_priority_objective() -> tuple[object, ...]:
        return _resident_core_key(_resident_priority_metrics())

    def _try_resident_thursday_swap(*, protect_higher_priorities: bool = False) -> bool:
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
        current_higher = _resident_higher_priority_objective()
        current_thursday = _count_spread_and_square(thursday_night_counts, pool)
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
                candidate_objective = _resident_night_objective()
                candidate_higher = _resident_higher_priority_objective()
                candidate_thursday = _count_spread_and_square(thursday_night_counts, pool)
                accepted = (
                    candidate_higher <= current_higher
                    and candidate_thursday < current_thursday
                    and (
                        candidate_higher < current_higher
                        or _resident_preference_objective(candidate_objective)
                        <= _resident_preference_objective(current_objective)
                    )
                    if protect_higher_priorities
                    else candidate_objective < current_objective
                )
                if accepted:
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

    def _repair_resident_thursday_final(max_steps: int = 8) -> int:
        repaired = 0
        for _ in range(max_steps):
            if not _try_resident_thursday_swap(protect_higher_priorities=True):
                break
            repaired += 1
        return repaired

    def _repair_resident_night_fairness(
        *,
        rounds: int = 3,
        weekend_steps: int = 24,
        type_steps: int = 2,
        thursday_steps: int = 3,
        progress_start: int | None = None,
        progress_end: int | None = None,
        progress_context: str = "",
    ) -> int:
        stage_weights = {
            "missing": 1,
            "total": 3,
            "weekend": 5,
            "weekend_swaps": 4,
            "saturday": 2,
            "sandwich_total": 2,
            "sandwich_swaps": 4,
            "sandwich_priority": 3,
            "sandwich_distribution": 2,
            "shift_type": 3,
            "requests": 2,
            "thursday": 1,
        }
        round_weight = sum(stage_weights.values())
        final_total_weight = 2
        total_progress_weight = max(1, rounds * round_weight + final_total_weight)
        completed_progress_weight = 0

        def resident_progress_label(stage_label: str) -> str:
            if progress_context:
                return f"{progress_context}: {stage_label}"
            return stage_label

        def report_resident_progress(stage_label: str) -> None:
            if progress_start is None or progress_end is None:
                return
            span = max(0, progress_end - progress_start)
            percent = progress_start + int(
                span * min(completed_progress_weight, total_progress_weight)
                / total_progress_weight
            )
            report_progress(percent, resident_progress_label(stage_label))

        def run_resident_progress_stage(
            stage_label: str,
            weight: int,
            fn: Callable[[], int],
            *,
            priority_stage: str | None = None,
        ) -> int:
            nonlocal completed_progress_weight
            report_resident_progress(stage_label)
            tracked_fn = fn
            if priority_stage in RESIDENT_PRIORITY_STAGES:
                tracked_fn = lambda: _run_resident_balancing_stage_with_preference_preservation(
                    priority_stage,
                    fn,
                    stage_label=stage_label,
                )
            result = _run_tracking_preferred_night_losses(
                tracked_fn,
                expected_stage=priority_stage,
            )
            completed_progress_weight += weight
            report_resident_progress(stage_label)
            return result

        _forget_repair_noops()
        _refresh_resident_fairness_pool()
        repaired = 0
        for _ in range(rounds):
            _refresh_resident_fairness_pool()
            before = repaired
            repaired += run_resident_progress_stage(
                "משלים תורנויות חסרות למתמחים",
                stage_weights["missing"],
                _repair_missing_resident_nights,
                priority_stage="missing",
            )
            repaired += run_resident_progress_stage(
                "מאזן סך תורנויות למתמחים",
                stage_weights["total"],
                lambda: _run_resident_stage_repair(
                    "total",
                    _repair_resident_night_balance,
                ),
                priority_stage="total",
            )
            repaired += run_resident_progress_stage(
                "מאזן סופי שבוע וימי שישי למתמחים",
                stage_weights["weekend"],
                lambda: _run_resident_stage_repair(
                    "weekend_friday",
                    lambda: _repair_resident_weekend_balance(
                        max_steps=weekend_steps,
                        use_cache=False,
                    ),
                ),
                priority_stage="weekend_friday",
            )
            repaired += run_resident_progress_stage(
                "משפר חלוקת סופי שבוע וימי שישי",
                stage_weights["weekend_swaps"],
                lambda: _repair_resident_priority_swaps(
                    "weekend_friday",
                    max_steps=min(12, weekend_steps),
                ),
                priority_stage="weekend_friday",
            )
            repaired += run_resident_progress_stage(
                "מאזן שבתות למתמחים",
                stage_weights["saturday"],
                lambda: _repair_resident_saturday_balance(
                    max_steps=min(8, max(4, weekend_steps // 2)),
                ),
                priority_stage="saturday",
            )
            repaired += run_resident_progress_stage(
                "מצמצם סנדוויצ'ים למתמחים",
                stage_weights["sandwich_total"],
                lambda: _run_resident_stage_repair(
                    "sandwich_total",
                    _repair_resident_sandwiches,
                ),
                priority_stage="sandwich_total",
            )
            repaired += run_resident_progress_stage(
                "מצמצם סנדוויצ'ים למתמחים",
                stage_weights["sandwich_swaps"],
                lambda: _run_resident_stage_repair(
                    "sandwich_total",
                    lambda: _repair_resident_sandwich_swaps(
                        max_steps=10,
                        use_cache=False,
                    ),
                ),
                priority_stage="sandwich_total",
            )
            repaired += run_resident_progress_stage(
                "בודק חלופות לצמצום סנדוויצ'ים",
                stage_weights["sandwich_priority"],
                lambda: _repair_resident_priority_swaps(
                    "sandwich_total",
                    max_steps=8,
                    max_evaluations=180,
                ),
                priority_stage="sandwich_total",
            )
            repaired += run_resident_progress_stage(
                "מחלק סנדוויצ'ים בין מתמחים",
                stage_weights["sandwich_distribution"],
                lambda: _repair_resident_sandwich_distribution(max_steps=8),
                priority_stage="sandwich_distribution",
            )
            repaired += run_resident_progress_stage(
                "מאזן ת.מיון מול ת.מיון 2",
                stage_weights["shift_type"],
                lambda: _repair_resident_priority_swaps(
                    "shift_type",
                    max_steps=min(8, max(2, type_steps * 2)),
                ),
                priority_stage="shift_type",
            )
            repaired += run_resident_progress_stage(
                "מחלק בקשות מועדפות בין המתמחים",
                stage_weights["requests"],
                lambda: _repair_preferred_resident_night_requests(max_steps=8),
                priority_stage="request_fairness",
            )
            repaired += run_resident_progress_stage(
                "מבצע בדיקת איזון אחרונה",
                stage_weights["thursday"],
                lambda: _repair_resident_thursday_final(max_steps=thursday_steps),
                priority_stage="thursday",
            )
            if repaired == before:
                break
        if _resident_night_total_objective()[0] > 1:
            repaired += run_resident_progress_stage(
                "מוודא הוגנות בסך התורנויות",
                final_total_weight,
                lambda: _run_resident_stage_repair(
                    "total",
                    _repair_resident_night_balance,
                ),
                priority_stage="total",
            )
        if progress_start is not None and progress_end is not None:
            completed_progress_weight = total_progress_weight
            report_resident_progress("מסיים איזון תורנויות מתמחים")
        return repaired

    def _senior_other_friday_day_dates(name: str, d: date) -> set[date]:
        return set(senior_friday_day_dates_by_name.get(name, set())) - {d}

    def _is_protected_friday_pair(d: date, name: str, shift: str) -> bool:
        if d.weekday() != 4:
            return False
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
            and (d, shift, victim) not in mandatory_personal_assignment_keys
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
                    # Keep the identity when it does not create a second
                    # Friday. Fixed rows and hard eligibility remain absolute.
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

    def _senior_friday_day_pool() -> set[str]:
        assigned = {
            name for name in senior_friday_day_counts
            if _is_senior_name(name)
        }
        capable = {
            name
            for name in senior_names
            if any(
                worker_shift_lut.get((name, shift), False)
                for shift in FRIDAY_DAY_BALANCE_SHIFTS
            )
        }
        return assigned | capable

    def _senior_friday_day_objective() -> tuple[int, int]:
        return _friday_count_objective(
            senior_friday_day_counts,
            _senior_friday_day_pool(),
        )

    def _friday_day_objective() -> tuple[int, int, int, int]:
        return (
            *_senior_friday_day_objective(),
            *_friday_count_objective(friday_day_counts, _friday_day_pool()),
        )

    def _try_senior_friday_direct_replacement() -> bool:
        """Transfer a Friday role from a senior with 2+ Fridays to one with 0/1."""

        pool = _senior_friday_day_pool()
        if not pool:
            return False
        current_objective = _senior_friday_day_objective()
        counts = Counter({name: senior_friday_day_counts[name] for name in pool})
        candidates: list[tuple[int, int, int, str, int, str, str]] = []
        rows = roster[roster["Shift"].isin(FRIDAY_DAY_BALANCE_SHIFTS)].copy()
        rows = rows[
            rows["Date"].map(lambda value: date.fromisoformat(str(value)).weekday() == 4)
        ]

        for idx, row in rows.iterrows():
            d = _row_date(idx)
            shift = str(row.Shift)
            current = _name_list(row.Assigned)
            for high_name in current:
                if high_name not in pool or counts[high_name] <= 1:
                    continue
                if (
                    (d, shift, high_name) in fixed_assignment_keys
                    or (d, shift, high_name) in mandatory_personal_assignment_keys
                ):
                    continue
                for low_name in pool:
                    if low_name == high_name or low_name in current:
                        continue
                    if counts[high_name] - counts[low_name] <= 1:
                        continue
                    other_friday_work = (
                        daily_assignments.get(d, {}).get(high_name, set())
                        & FRIDAY_DAY_BALANCE_SHIFTS
                    ) - {shift}
                    candidates.append((
                        1 if other_friday_work else 0,
                        counts[high_name] - counts[low_name],
                        counts[low_name],
                        d.isoformat(),
                        idx,
                        high_name,
                        low_name,
                    ))

        for _, _, _, _, idx, high_name, low_name in sorted(
            candidates,
            key=lambda item: (item[0], -item[1], item[2], item[3], item[6]),
        ):
            d = _row_date(idx)
            shift = str(roster.at[idx, "Shift"])
            original = _name_list(roster.at[idx, "Assigned"])
            current = [name for name in original if name != high_name]
            roster.at[idx, "Assigned"] = _write_name_list(current)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            allowed = (
                _personal_under_max(low_name, shift, d)
                and _can_worker_take_shift(
                    low_name,
                    shift,
                    d,
                    last_night_map=_last_night_before(d),
                )
                and (shift != YOEATZIM_SHIFT or _yoeatzim_allowed(low_name, d))
                and (shift != "EEG" or _eeg_under_cap(low_name, d))
            )
            if allowed:
                roster.at[idx, "Assigned"] = _write_name_list(current + [low_name])
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                if _senior_friday_day_objective() < current_objective:
                    logger.info(
                        "Senior Friday balance replacement: %s %s %s -> %s",
                        d.isoformat(), shift, high_name, low_name,
                    )
                    return True

            roster.at[idx, "Assigned"] = _write_name_list(original)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
        return False

    def _try_friday_day_swap(
        balance_pool: set[str] | None = None,
        *,
        senior_only: bool = False,
    ) -> bool:
        pool = balance_pool if balance_pool is not None else _friday_day_pool()
        if not pool:
            return False
        balance_counts = senior_friday_day_counts if senior_only else friday_day_counts
        values = [balance_counts[name] for name in pool]
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
                if high_name not in pool:
                    continue
                if _is_protected_friday_pair(a_date, high_name, a_shift):
                    continue
                if balance_counts[high_name] <= min_fridays + 1:
                    continue
                if (
                    (a_date, a_shift, high_name) in fixed_assignment_keys
                    or (a_date, a_shift, high_name) in mandatory_personal_assignment_keys
                ):
                    continue
                for b_idx, b_row in other_rows[other_rows["Shift"] == a_shift].iterrows():
                    b_date = date.fromisoformat(str(b_row.Date))
                    b_shift = str(b_row.Shift)
                    for low_name in _name_list(b_row.Assigned):
                        if low_name not in pool:
                            continue
                        if low_name == high_name:
                            continue
                        if balance_counts[low_name] >= balance_counts[high_name] - 1:
                            continue
                        if (
                            (b_date, b_shift, low_name) in fixed_assignment_keys
                            or (b_date, b_shift, low_name) in mandatory_personal_assignment_keys
                        ):
                            continue
                        candidates.append((
                            balance_counts[high_name] - balance_counts[low_name],
                            balance_counts[low_name],
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
            if (
                not _try_senior_friday_direct_replacement()
                and not _try_friday_day_swap(
                    _senior_friday_day_pool(),
                    senior_only=True,
                )
                and not _try_friday_day_swap()
            ):
                break
            repaired += 1
        return repaired

    def _try_resident_night_balance_chain(max_evaluations: int = 300) -> bool:
        """
        Try a two-hop transfer when no direct high-to-low replacement is legal.

        A loses high_name to mid_name, while B loses mid_name to low_name.  The
        intermediate resident's total is unchanged; the high resident loses one
        night and the low resident gains one.  All final assignments are checked
        with the same eligibility and rest rules used by the normal scheduler.
        """
        pool = _resident_night_pool()
        counts = _resident_night_total_counts()
        if not pool or not counts:
            return False
        max_count = max(counts.values())
        min_count = min(counts.values())
        if max_count - min_count <= 1:
            return False

        high_names = {name for name in pool if counts[name] == max_count}
        low_names = {name for name in pool if counts[name] <= max_count - 2}
        if not high_names or not low_names:
            return False

        before_total = _resident_night_total_objective()
        before_weekend = _resident_weekend_objective()
        before_weekend_history = _resident_weekend_history_load()
        rows = roster[roster["Shift"].isin(RESIDENT_NIGHT_SHIFTS)].copy()
        candidates: list[tuple[int, int, str, str, int, int, str, str, str]] = []

        for a_idx, a_row in rows.iterrows():
            a_date = _row_date(a_idx)
            a_shift = str(a_row.Shift)
            a_names = _name_list(a_row.Assigned)
            for high_name in a_names:
                if high_name not in high_names:
                    continue
                if (
                    (a_date, a_shift, high_name) in fixed_assignment_keys
                    or (a_date, a_shift, high_name) in mandatory_personal_assignment_keys
                ):
                    continue
                high_removal_penalty = _preferred_night_removal_order_cost(
                    high_name, a_shift, a_date
                )

                for b_idx, b_row in rows.iterrows():
                    if b_idx == a_idx:
                        continue
                    b_date = _row_date(b_idx)
                    b_shift = str(b_row.Shift)
                    same_weekend_class = int(
                        (a_date.weekday() in (4, 5)) != (b_date.weekday() in (4, 5))
                    )
                    for mid_name in _name_list(b_row.Assigned):
                        if mid_name == high_name or mid_name in low_names or mid_name not in pool:
                            continue
                        if mid_name in a_names:
                            continue
                        if (b_date, b_shift, mid_name) in fixed_assignment_keys:
                            continue
                        mid_removal_penalty = _preferred_night_removal_order_cost(
                            mid_name, b_shift, b_date
                        )
                        if eligibility_reason(mid_name, a_date.isoformat(), a_shift) is not None:
                            continue

                        for low_name in low_names:
                            if low_name in a_names or low_name in _name_list(b_row.Assigned):
                                continue
                            if eligibility_reason(low_name, b_date.isoformat(), b_shift) is not None:
                                continue
                            candidates.append((
                                same_weekend_class,
                                high_removal_penalty + mid_removal_penalty,
                                a_date.isoformat(),
                                b_date.isoformat(),
                                a_idx,
                                b_idx,
                                high_name,
                                mid_name,
                                low_name,
                            ))

        candidates.sort()
        if not candidates:
            return False

        for protect_weekends in (True, False):
            evaluated = 0
            for (
                _, _, _, _, a_idx, b_idx, high_name, mid_name, low_name
            ) in candidates:
                evaluated += 1
                if evaluated > max_evaluations:
                    logger.debug(
                        "resident night two-hop search reached evaluation limit (%d)",
                        max_evaluations,
                    )
                    break

                a_date = _row_date(a_idx)
                b_date = _row_date(b_idx)
                a_shift = str(roster.at[a_idx, "Shift"])
                b_shift = str(roster.at[b_idx, "Shift"])
                original_a = _name_list(roster.at[a_idx, "Assigned"])
                original_b = _name_list(roster.at[b_idx, "Assigned"])
                current_a = [name for name in original_a if name != high_name]
                current_b = [name for name in original_b if name != mid_name]

                roster.at[a_idx, "Assigned"] = _write_name_list(current_a)
                roster.at[b_idx, "Assigned"] = _write_name_list(current_b)
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()

                mid_last_night = _last_night_before(a_date)
                low_last_night = _last_night_before(b_date)
                mid_can_take_a = (
                    _can_worker_take_shift(
                        mid_name,
                        a_shift,
                        a_date,
                        last_night_map=mid_last_night,
                    )
                    and _resident_adjacent_night_penalty(mid_name, a_date, daily_assignments) == 0
                    and _resident_night_spacing_penalty_for(mid_name, a_date, mid_last_night) < 100
                )
                low_can_take_b = (
                    _can_worker_take_shift(
                        low_name,
                        b_shift,
                        b_date,
                        last_night_map=low_last_night,
                    )
                    and _resident_adjacent_night_penalty(low_name, b_date, daily_assignments) == 0
                    and _resident_night_spacing_penalty_for(low_name, b_date, low_last_night) < 100
                )

                if mid_can_take_a and low_can_take_b:
                    roster.at[a_idx, "Assigned"] = _write_name_list(current_a + [mid_name])
                    roster.at[b_idx, "Assigned"] = _write_name_list(current_b + [low_name])
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    total_improved = _resident_night_total_objective() < before_total
                    weekends_protected = (
                        not protect_weekends
                        or (
                            _resident_weekend_objective() <= before_weekend
                            and _resident_weekend_history_load() <= before_weekend_history
                        )
                    )
                    if total_improved and weekends_protected:
                        logger.info(
                            "resident night two-hop balance: %s %s %s -> %s; "
                            "%s %s %s -> %s objective %s -> %s",
                            a_date.isoformat(), a_shift, high_name, mid_name,
                            b_date.isoformat(), b_shift, mid_name, low_name,
                            before_total, _resident_night_total_objective(),
                        )
                        _forget_repair_noops()
                        return True

                roster.at[a_idx, "Assigned"] = _write_name_list(original_a)
                roster.at[b_idx, "Assigned"] = _write_name_list(original_b)
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()

        return False

    def _repair_resident_night_balance(
        max_steps: int = 20,
        *,
        search_evaluations: int = 300,
    ) -> int:
        label = "resident_night_balance"
        if _repair_noop_cached(label):
            return 0
        repaired = 0
        seen_states: set[tuple[tuple[str, int], ...]] = set()
        for _ in range(max_steps):
            _refresh_resident_fairness_pool()
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
            low_levels = sorted({count for count in counts.values() if count <= max_count - 2})
            before_objective = _resident_night_total_objective()
            changed = False

            # Preserve weekends first. If no such transfer exists, total balance
            # still wins and the later weekend pass repairs the secondary metric.
            for protect_weekends in (True, False):
                for low_level in low_levels:
                    low_names = {name for name, count in counts.items() if count == low_level}
                    for high_name in sorted(high_names, key=lambda n: (-counts[n], -month_counts[n], n)):
                        high_rows = roster[
                            (roster["Shift"].isin(["ת.מיון", "ת.מיון 2"]))
                            & (roster["Assigned"].astype(str).map(lambda cell: high_name in _name_list(cell)))
                        ].copy()
                        high_rows["_weekday"] = high_rows["Date"].map(
                            lambda x: date.fromisoformat(str(x)).weekday()
                        )
                        for idx, _ in high_rows.sort_values(
                            ["_weekday", "Date"], ascending=[False, False]
                        ).iterrows():
                            shift_date = _row_date(idx)
                            shift_type = str(roster.at[idx, "Shift"])
                            assigned_snapshot = roster["Assigned"].copy()
                            if _try_replace_resident_night(
                                idx,
                                high_name,
                                preferred_names=low_names,
                                reason="resident night balance repair",
                                allow_sandwich=True,
                                protect_weekend_objective=(
                                    _resident_weekend_objective() if protect_weekends else None
                                ),
                                protect_weekend_history=(
                                    _resident_weekend_history_load() if protect_weekends else None
                                ),
                                require_preferred_names=True,
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
                    if changed:
                        break
                if changed:
                    break

            if not changed:
                if _try_best_resident_night_improvement(
                    max_evaluations=search_evaluations,
                ):
                    repaired += 1
                    _forget_repair_noops()
                    continue
                if _try_resident_night_balance_chain(
                    max_evaluations=search_evaluations,
                ):
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

    def _senior_on_call_weekend_objective(pool: set[str]) -> tuple[int, int, int, int]:
        if not pool:
            return (0, 0, 0, 0)
        current = Counter({name: konen_weekend_counts[name] for name in pool})
        rolling = Counter({
            name: previous_senior_weekend_counts[name] + konen_weekend_counts[name]
            for name in pool
        })
        current_spread, current_square = _count_spread_and_square(current, pool)
        rolling_spread, rolling_square = _count_spread_and_square(rolling, pool)
        return (current_spread, current_square, rolling_spread, rolling_square)

    def _try_senior_on_call_weekend_swap() -> bool:
        pool = {
            name for name in _senior_on_call_pool()
            if konen_month_counts[name] > 0
        }
        if not pool:
            return False
        current_objective = _senior_on_call_weekend_objective(pool)
        current_counts = Counter({name: konen_weekend_counts[name] for name in pool})
        rolling_counts = Counter({
            name: previous_senior_weekend_counts[name] + konen_weekend_counts[name]
            for name in pool
        })
        min_current = min(current_counts.values())
        max_current = max(current_counts.values())
        if max_current == min_current:
            return False

        rows = roster[roster["Shift"] == KONEN_MION_SHIFT].copy()
        rows["_date"] = rows["Date"].map(lambda value: date.fromisoformat(str(value)))
        weekend_rows = rows[rows["_date"].map(lambda d: d.weekday() in (4, 5))]
        weekday_rows = rows[~rows["_date"].map(lambda d: d.weekday() in (4, 5))]
        candidates: list[tuple[int, int, int, str, int, int, str, str]] = []

        for weekend_idx, weekend_row in weekend_rows.iterrows():
            weekend_date = weekend_row["_date"]
            for high_name in _name_list(weekend_row.Assigned):
                if current_counts[high_name] != max_current:
                    continue
                if (weekend_date, KONEN_MION_SHIFT, high_name) in fixed_assignment_keys:
                    continue
                if _preferred_night_removal_penalty(high_name, KONEN_MION_SHIFT, weekend_date) >= 100:
                    continue
                for weekday_idx, weekday_row in weekday_rows.iterrows():
                    weekday_date = weekday_row["_date"]
                    for low_name in _name_list(weekday_row.Assigned):
                        if low_name == high_name or current_counts[low_name] != min_current:
                            continue
                        if (weekday_date, KONEN_MION_SHIFT, low_name) in fixed_assignment_keys:
                            continue
                        if _preferred_night_removal_penalty(low_name, KONEN_MION_SHIFT, weekday_date) >= 100:
                            continue
                        candidates.append((
                            current_counts[high_name] - current_counts[low_name],
                            rolling_counts[high_name] - rolling_counts[low_name],
                            _preferred_night_removal_order_cost(high_name, KONEN_MION_SHIFT, weekend_date)
                            + _preferred_night_removal_order_cost(low_name, KONEN_MION_SHIFT, weekday_date),
                            weekend_date.isoformat(),
                            weekend_idx,
                            weekday_idx,
                            high_name,
                            low_name,
                        ))

        for _, _, _, _, weekend_idx, weekday_idx, high_name, low_name in sorted(
            candidates,
            key=lambda item: (-item[0], -item[1], item[2], item[3]),
        ):
            weekend_date = _row_date(weekend_idx)
            weekday_date = _row_date(weekday_idx)
            original_weekend = _name_list(roster.at[weekend_idx, "Assigned"])
            original_weekday = _name_list(roster.at[weekday_idx, "Assigned"])
            current_weekend = [name for name in original_weekend if name != high_name]
            current_weekday = [name for name in original_weekday if name != low_name]
            roster.at[weekend_idx, "Assigned"] = _write_name_list(current_weekend)
            roster.at[weekday_idx, "Assigned"] = _write_name_list(current_weekday)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()

            low_can_take_weekend = _can_worker_take_shift(
                low_name,
                KONEN_MION_SHIFT,
                weekend_date,
                last_night_map=_last_night_before(weekend_date),
            ) and (
                low_name != SHIMON_NAME
                or weekend_date.weekday() != 4
                or _shimon_friday_available(weekend_date)
            )
            high_can_take_weekday = _can_worker_take_shift(
                high_name,
                KONEN_MION_SHIFT,
                weekday_date,
                last_night_map=_last_night_before(weekday_date),
            )
            if low_can_take_weekend and high_can_take_weekday:
                roster.at[weekend_idx, "Assigned"] = _write_name_list(current_weekend + [low_name])
                roster.at[weekday_idx, "Assigned"] = _write_name_list(current_weekday + [high_name])
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                candidate_objective = _senior_on_call_weekend_objective(pool)
                if candidate_objective < current_objective:
                    logger.info(
                        "senior on-call weekend swap: %s %s <-> %s %s objective %s -> %s",
                        weekend_date.isoformat(), high_name,
                        weekday_date.isoformat(), low_name,
                        current_objective, candidate_objective,
                    )
                    return True

            roster.at[weekend_idx, "Assigned"] = _write_name_list(original_weekend)
            roster.at[weekday_idx, "Assigned"] = _write_name_list(original_weekday)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
        return False

    def _repair_senior_on_call_weekends(max_steps: int = 12) -> int:
        repaired = 0
        for _ in range(max_steps):
            if not _try_senior_on_call_weekend_swap():
                break
            repaired += 1
        return repaired

    def _senior_on_call_month_objective(pool: set[str]) -> tuple[int, int, int]:
        regular_pool = pool - {SHIMON_NAME}
        spread, square = _count_spread_and_square(konen_month_counts, regular_pool)
        shimon_gap = (
            abs(konen_month_counts[SHIMON_NAME] - SHIMON_KONEN_TARGET)
            if SHIMON_NAME in pool
            else 0
        )
        return (spread, square, shimon_gap)

    def _weekend_konen_target_names(
        idx: int,
        candidate: str,
        original: list[str],
    ) -> list[str] | None:
        shift_date = _row_date(idx)
        needed = _to_int(roster.at[idx, "Needed"], 0)
        soft = _to_int(roster.at[idx, "SoftCap"], 0)
        fixed_here = {
            name for name in original
            if (shift_date, KONEN_MION_SHIFT, name) in fixed_assignment_keys
        }
        return _weekend_konen_target_names_for_row(
            original,
            fixed_here,
            candidate,
            needed=needed,
            soft_cap=soft,
        )

    def _repair_weekend_konen_pairs() -> int:
        """Make Friday and Saturday כונן מיון the same senior when legally possible."""

        repaired = 0
        friday_dates = sorted(
            date.fromisoformat(str(value))
            for value in roster["Date"].unique()
            if date.fromisoformat(str(value)).weekday() == 4
        )
        for friday in friday_dates:
            saturday = friday + timedelta(days=1)
            friday_idx = _row_index(friday, KONEN_MION_SHIFT)
            saturday_idx = _row_index(saturday, KONEN_MION_SHIFT)
            if friday_idx is None or saturday_idx is None:
                continue

            original_friday = _name_list(roster.at[friday_idx, "Assigned"])
            original_saturday = _name_list(roster.at[saturday_idx, "Assigned"])
            if set(original_friday).intersection(original_saturday):
                continue

            fixed_friday = {
                name for name in original_friday
                if (friday, KONEN_MION_SHIFT, name) in fixed_assignment_keys
            }
            fixed_saturday = {
                name for name in original_saturday
                if (saturday, KONEN_MION_SHIFT, name) in fixed_assignment_keys
            }
            pool = _senior_on_call_pool()
            fixed_candidates = fixed_friday | fixed_saturday
            if fixed_candidates:
                candidate_names = fixed_candidates
            else:
                candidate_names = (
                    set(original_friday)
                    | set(original_saturday)
                    | pool
                )

            feasible: list[
                tuple[
                    tuple[object, ...],
                    str,
                    list[str],
                    list[str],
                ]
            ] = []
            for candidate in sorted(candidate_names):
                if not worker_shift_lut.get((candidate, KONEN_MION_SHIFT), False):
                    continue
                if (
                    candidate == SHIMON_NAME
                    and candidate not in original_friday
                    and (friday, KONEN_MION_SHIFT, candidate) not in fixed_assignment_keys
                    and not _shimon_friday_available(friday)
                ):
                    continue

                target_friday = _weekend_konen_target_names(
                    friday_idx,
                    candidate,
                    original_friday,
                )
                target_saturday = _weekend_konen_target_names(
                    saturday_idx,
                    candidate,
                    original_saturday,
                )
                if target_friday is None or target_saturday is None:
                    continue

                # Check both days after removing the two current on-call rows.
                friday_base = [
                    name for name in original_friday
                    if name != candidate
                    and (friday, KONEN_MION_SHIFT, name) in fixed_assignment_keys
                ]
                saturday_base = [
                    name for name in original_saturday
                    if name != candidate
                    and (saturday, KONEN_MION_SHIFT, name) in fixed_assignment_keys
                ]
                roster.at[friday_idx, "Assigned"] = _write_name_list(friday_base)
                roster.at[saturday_idx, "Assigned"] = _write_name_list(saturday_base)
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()
                can_take_pair = (
                    _can_worker_take_shift(
                        candidate,
                        KONEN_MION_SHIFT,
                        friday,
                        last_night_map=_last_night_before(friday),
                    )
                    and _can_worker_take_shift(
                        candidate,
                        KONEN_MION_SHIFT,
                        saturday,
                        last_night_map=_last_night_before(saturday),
                    )
                )
                if can_take_pair:
                    roster.at[friday_idx, "Assigned"] = _write_name_list(target_friday)
                    roster.at[saturday_idx, "Assigned"] = _write_name_list(target_saturday)
                    _rebuild_daily_assignments_from_roster()
                    _rebuild_night_state_from_roster()
                    _rebuild_live_counters_from_roster()
                    active_pool = pool | set(target_friday) | set(target_saturday)
                    removed_preference = sum(
                        _preferred_night_removal_order_cost(name, KONEN_MION_SHIFT, duty_date)
                        for duty_date, old_names, target_names in (
                            (friday, original_friday, target_friday),
                            (saturday, original_saturday, target_saturday),
                        )
                        for name in old_names
                        if name not in target_names
                    )
                    score = (
                        _senior_friday_day_objective(),
                        _senior_on_call_month_objective(active_pool),
                        _senior_on_call_weekend_objective(active_pool),
                        0 if candidate in (set(original_friday) | set(original_saturday)) else 1,
                        removed_preference,
                        0 if candidate in original_friday else 1,
                        candidate,
                    )
                    feasible.append((score, candidate, target_friday, target_saturday))

                roster.at[friday_idx, "Assigned"] = _write_name_list(original_friday)
                roster.at[saturday_idx, "Assigned"] = _write_name_list(original_saturday)
                _rebuild_daily_assignments_from_roster()
                _rebuild_night_state_from_roster()
                _rebuild_live_counters_from_roster()

            if not feasible:
                logger.warning(
                    "Weekend on-call pair unresolved: %s Friday=%s Saturday=%s fixed Friday=%s fixed Saturday=%s",
                    friday.isoformat(),
                    original_friday,
                    original_saturday,
                    sorted(fixed_friday),
                    sorted(fixed_saturday),
                )
                continue

            _, candidate, target_friday, target_saturday = min(
                feasible,
                key=lambda item: item[0],
            )
            roster.at[friday_idx, "Assigned"] = _write_name_list(target_friday)
            roster.at[saturday_idx, "Assigned"] = _write_name_list(target_saturday)
            _rebuild_daily_assignments_from_roster()
            _rebuild_night_state_from_roster()
            _rebuild_live_counters_from_roster()
            repaired += 1
            logger.info(
                "Weekend on-call pair repair: %s/%s -> %s",
                friday.isoformat(),
                saturday.isoformat(),
                candidate,
            )
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

    def _log_unmet_preferred_night_requests() -> dict[str, dict[str, str]]:
        _rebuild_daily_assignments_from_roster()
        audit: dict[str, dict[str, str]] = {}
        for (name, pref_date), strength in sorted(
            preferred_night_requests.items(),
            key=lambda item: (-item[1], 0 if item[0][1].weekday() in (4, 5) else 1, item[0][1], item[0][0]),
        ):
            allowed_shifts = _preferred_night_shifts_for(name)
            assigned_allowed = daily_assignments.get(pref_date, {}).get(name, set()).intersection(allowed_shifts)
            if assigned_allowed:
                continue

            request_key = (name, pref_date)
            stage = preferred_night_loss_stage.get(request_key)
            if stage in RESIDENT_PRIORITY_STAGES:
                cause = {
                    "reason_code": "higher_priority",
                    "priority_stage": stage,
                }
            elif stage in {"request_fairness", "thursday", "history"}:
                cause = {
                    "reason_code": "soft_priority",
                    "priority_stage": stage,
                }
            elif request_key in preferred_night_seed_blocks:
                cause = dict(preferred_night_seed_blocks[request_key])
            elif request_key in preferred_night_seeded_requests:
                cause = {
                    "reason_code": "diagnostic",
                    "block": "seeded-then-untracked-removal",
                }
            else:
                cause = {
                    "reason_code": "diagnostic",
                    "block": "no-causal-record",
                }
            cause.update(preferred_night_loss_detail.get(request_key, {}))

            audit[f"{name}|{pref_date.isoformat()}"] = cause

            logger.info(
                "unmet preferred night request: %s %s strength=%d cause=%s",
                pref_date.isoformat(), name, strength, cause,
            )
        return audit

    def _preferred_seed_candidate_block(
        name: str,
        pref_date: date,
        shift_type: str,
    ) -> str | None:
        """Return the causal block visible before general night assignment begins."""

        reason = eligibility_reason(name, pref_date.isoformat(), shift_type)
        if reason:
            return reason

        if shift_type in RESIDENT_NIGHT_SHIFTS:
            if _blocked_by_tomorrow_fixed_resident_night(name, pref_date):
                return "tomorrow-fixed-night"
            tomorrow = pref_date + timedelta(days=1)
            tomorrow_shifts = daily_assignments.get(tomorrow, {}).get(name, set())
            if has_clinic_shift(tomorrow_shifts):
                return "tomorrow-clinic"

            adjacent_assignments: list[tuple[date, str]] = []
            for adjacent_date in (pref_date - timedelta(days=1), pref_date + timedelta(days=1)):
                for adjacent_shift in daily_assignments.get(adjacent_date, {}).get(name, set()):
                    if adjacent_shift in RESIDENT_NIGHT_SHIFTS:
                        adjacent_assignments.append((adjacent_date, adjacent_shift))
            if adjacent_assignments or pref_date in blocked_next_day.get(name, set()):
                if any(
                    (adjacent_date, adjacent_shift, name) in fixed_assignment_keys
                    for adjacent_date, adjacent_shift in adjacent_assignments
                ):
                    return "adjacent-fixed-night"
                if any(
                    (name, adjacent_date) in preferred_night_seeded_requests
                    for adjacent_date, _ in adjacent_assignments
                ):
                    return "adjacent-earlier-preference"
                return "adjacent-history-night"

            effective_last_night = _last_night_before(pref_date)
            if _resident_night_spacing_penalty_for(name, pref_date, effective_last_night) >= 100:
                return "adjacent-history-night"

        today_set = daily_assignments.get(pref_date, {}).get(name, set())
        if today_set.intersection(RESIDENT_NIGHT_SHIFTS):
            return "same-day-resident-night"
        if len(today_set) >= 2:
            return "resident-daily-limit"
        if len(today_set) == 1:
            existing = next(iter(today_set))
            if (existing, shift_type) not in DUAL_OK:
                return "illegal-same-day-pair"
        return None

    def _remember_preferred_seed_block(
        name: str,
        pref_date: date,
        blocks: list[str],
        competing_names: set[str] | None = None,
    ) -> None:
        cause: dict[str, object] = dict(_preferred_seed_block_cause(blocks))
        if cause.get("reason_code") == "request_competition":
            cause["competing_names"] = ", ".join(sorted(competing_names or set()))
            cause["fixed_slot_present"] = "fixed-slot-full" in blocks
        preferred_night_seed_blocks[(name, pref_date)] = cause

    def _seed_preferred_night_requests() -> int:
        nonlocal filled_so_far
        seeded = 0
        _rebuild_daily_assignments_from_roster()
        _rebuild_night_state_from_roster()
        _rebuild_live_counters_from_roster()

        def request_sort_key(
            item: tuple[tuple[str, date], int],
        ) -> tuple[object, ...]:
            (name, pref_date), strength = item
            resident_shifts = sorted(
                _preferred_night_shifts_for(name).intersection(RESIDENT_NIGHT_SHIFTS)
            )
            if resident_shifts:
                projected_core = (
                    0,
                    *min(
                        _resident_projected_fairness_key(name, shift, pref_date)[:6]
                        for shift in resident_shifts
                    ),
                )
                history_key: tuple[object, ...] = min(
                    (
                        _rolling_resident_night_count(name),
                        _rolling_resident_weekend_count(name),
                        _resident_projected_type_compensation_key(name, shift),
                    )
                    for shift in resident_shifts
                )
                jitter = min(
                    _resident_assignment_jitter(name, shift, pref_date)
                    for shift in resident_shifts
                )
            else:
                # Senior requests use a different row and do not compete for a
                # resident slot; keep their existing on-call ordering isolated.
                projected_core = (1, _konen_mion_key(name, pref_date))
                history_key = (konen_month_counts[name], konen_weekend_counts[name])
                jitter = _resident_assignment_jitter(name, KONEN_MION_SHIFT, pref_date)

            return _preferred_request_competition_key(
                (
                    -strength,
                    0 if pref_date.weekday() in (4, 5) else 1,
                    pref_date,
                ),
                projected_core,
                _preferred_other_request_approval_percentage(name, pref_date),
                history_key,
                jitter,
                name,
            )

        # Re-select the next request after every successful seed.  This keeps
        # the live percentage of other approved requests current instead of
        # freezing it in one initial sort.
        requests = list(preferred_night_requests.items())
        while requests:
            next_index = min(range(len(requests)), key=lambda idx: request_sort_key(requests[idx]))
            (name, pref_date), strength = requests.pop(next_index)
            if daily_assignments.get(pref_date, {}).get(name, set()).intersection(_preferred_night_shifts_for(name)):
                continue

            possible_shifts = [
                shift
                for shift in ("ת.מיון", "ת.מיון 2", KONEN_MION_SHIFT)
                if shift in _preferred_night_shifts_for(name)
                and worker_shift_lut.get((name, shift), False)
            ]
            candidates: list[tuple[tuple, int, str, list[str]]] = []
            seed_blocks: list[str] = []
            competing_preferred_names: set[str] = set()
            if not possible_shifts:
                seed_blocks.append("capability")
            for shift_type in possible_shifts:
                mask = (roster["Date"] == pref_date.isoformat()) & (roster["Shift"] == shift_type)
                if not mask.any():
                    seed_blocks.append("no-row")
                    continue
                idx = roster.index[mask][0]
                needed = _to_int(roster.at[idx, "Needed"], 0)
                if needed <= 0:
                    seed_blocks.append("not-required")
                    continue
                current = _name_list(roster.at[idx, "Assigned"])
                if name in current:
                    continue
                if len(current) >= needed:
                    if any(
                        (pref_date, shift_type, current_name) in fixed_assignment_keys
                        for current_name in current
                    ):
                        seed_blocks.append("fixed-slot-full")
                    elif any(
                        (current_name, pref_date) in preferred_night_seeded_requests
                        for current_name in current
                    ):
                        seed_blocks.append("earlier-preference-filled-slot")
                        competing_preferred_names.update(
                            current_name
                            for current_name in current
                            if (current_name, pref_date) in preferred_night_seeded_requests
                        )
                    else:
                        seed_blocks.append("slot-full")
                    continue

                causal_block = _preferred_seed_candidate_block(name, pref_date, shift_type)
                if causal_block:
                    seed_blocks.append(causal_block)
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
                    seed_blocks.append("live-state")
                    continue
                if shift_type in RESIDENT_NIGHT_SHIFTS:
                    if _resident_adjacent_night_penalty(name, pref_date, daily_assignments) > 0:
                        seed_blocks.append("adjacent-history-night")
                        continue
                    if _resident_night_spacing_penalty_for(name, pref_date, effective_last_night) >= 100:
                        seed_blocks.append("adjacent-history-night")
                        continue
                    score = (
                        _resident_night_balance_key(name, shift_type, pref_date),
                        resident_night_shift_counts[shift_type][name],
                        _resident_assignment_jitter(name, shift_type, pref_date),
                    )
                else:
                    score = (
                        _konen_mion_key(name, pref_date),
                        konen_month_counts[name],
                    )
                candidates.append((score, idx, shift_type, current))

            if not candidates:
                _remember_preferred_seed_block(
                    name,
                    pref_date,
                    seed_blocks,
                    competing_preferred_names,
                )
                continue

            _, idx, shift_type, current = min(candidates, key=lambda item: item[0])
            current.append(name)
            roster.at[idx, "Assigned"] = _write_name_list(current)

            history[name][shift_type] += 1
            daily_assignments[pref_date].setdefault(name, set()).add(shift_type)
            _invalidate_resident_sandwich_cache(shift_type)
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
            preferred_night_seeded_requests.add((name, pref_date))
            preferred_night_seed_blocks.pop((name, pref_date), None)

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
                rotation_elig_set = set(rotation_elig)
                rotation_counts_for_pick = _rotation_current_counts()

                def _rotation_pull_pick_prefix(w: str) -> tuple[int, int, int, int, int, str]:
                    return _rotation_pick_prefix(w, rotation_elig_set, rotation_counts_for_pick)

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
                        if _resident_night_spacing_penalty_for(w, shift_date, effective_last_night) < 100
                        and _resident_adjacent_night_penalty(w, shift_date, daily_assignments) == 0
                        and _resident_sandwich_penalty_for(w, shift_date) == 0
                    ]
                    if non_sandwich:
                        elig = non_sandwich

                    # Protected resident priorities first; preference/history only
                    # guide otherwise equal current-month outcomes.
                    pick = min(
                        elig,
                        key=lambda w: (
                            _rotation_pull_pick_prefix(w),
                            _resident_night_balance_key(w, shift_type, shift_date),
                            _weekend_resident_night_key(w, shift_date),
                            _friday_work_key(w, shift_type, shift_date),
                            _alternate_risk_penalty(w, shift_type, shift_date, daily_assignments),
                            _friday_night_morning_penalty(w, shift_type, shift_date, daily_assignments),
                            _resident_adjacent_night_penalty(w, shift_date, daily_assignments),
                            _resident_sandwich_balance_key(w, shift_date),
                            _resident_sandwich_penalty_for(w, shift_date),
                            _resident_night_spacing_penalty_for(w, shift_date, effective_last_night),
                            fairness_score(w, shift_type, shift_date,
                                        history, effective_last_night),
                            _resident_assignment_jitter(w, shift_type, shift_date),
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
                            _rotation_pull_pick_prefix(w),
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
                            _rotation_pull_pick_prefix(w),
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
                            _rotation_pull_pick_prefix(w),
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
                            _rotation_pull_pick_prefix(w),
                            0 if shift_type == "EEG" and w == "גנדלמן" and _has_shift(daily_assignments, shift_date, w, "EEG ילדים") else 1,
                            _personal_rule_key(w, shift_type, shift_date),
                            0 if preferred_friday_worker and w in preferred_friday_worker else 1,
                            _friday_work_key(w, shift_type, shift_date),
                            0 if shift_date.weekday() == 4 and shift_type == ATTENDING_SHIFT and _has_shift(daily_assignments, shift_date, w, KONEN_MION_SHIFT) else 1,
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
                    assignment_pct = 6 + int(min(filled_so_far, total_slots) * 6 / total_slots)
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
                _invalidate_resident_sandwich_cache(shift_type)
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

    report_progress(13, "מתחיל השלמה ואיזון של תורנויות מתמחים")
    resident_fairness_repairs = timed_repair(
        "resident_fairness_initial",
        lambda: _repair_resident_night_fairness(
            rounds=4,
            weekend_steps=32,
            type_steps=4,
            thursday_steps=3,
            progress_start=13,
            progress_end=42,
            progress_context="איזון ראשוני",
        ),
    )
    if resident_fairness_repairs:
        logger.info("Resident night fairness repair changed %d assignments", resident_fairness_repairs)
    report_progress(43, "מצמיד שישי לתורנויות")
    repaired_friday_pairings = _repair_friday_pairings()
    if repaired_friday_pairings:
        logger.info("Friday duty/day pairing repair changed %d assignments", repaired_friday_pairings)
    report_progress(44, "מאזן עבודת שישי")
    repaired_fridays = _repair_friday_day_balance()
    if repaired_fridays:
        logger.info("Friday day balance repair changed %d assignments", repaired_fridays)
    final_resident_fairness_repairs = timed_repair(
        "resident_fairness_after_friday_pairing",
        lambda: _repair_resident_night_fairness(
            rounds=2,
            weekend_steps=24,
            type_steps=2,
            thursday_steps=2,
            progress_start=45,
            progress_end=52,
            progress_context="בדיקה אחרי איזון שישי",
        ),
    )
    if final_resident_fairness_repairs:
        logger.info("Final resident night fairness repair changed %d assignments", final_resident_fairness_repairs)
    report_progress(53, "בודק מנוחה אחרי תורנות")
    after_duty_removed = _resolve_after_duty_conflicts()
    if after_duty_removed:
        logger.info("After-duty cleanup removed %d assignments", after_duty_removed)
    report_progress(54, "משלים שיבוצים חסרים")
    refilled_hard_rows = _refill_required_rows_after_cleanup()
    if refilled_hard_rows:
        logger.info("Final hard-row refill filled %d assignments", refilled_hard_rows)
    report_progress(55, "מאזן כוננויות וסופי שבוע")
    repaired_konen = _repair_konen_mion_balance()
    if repaired_konen:
        logger.info("Senior on-call balance repair changed %d assignments", repaired_konen)
    repaired_konen_weekends = _repair_senior_on_call_weekends()
    if repaired_konen_weekends:
        logger.info("Senior on-call weekend balance changed %d assignment pairs", repaired_konen_weekends)
    repaired_weekend_konen_pairs = _repair_weekend_konen_pairs()
    if repaired_weekend_konen_pairs:
        logger.info(
            "Senior Friday/Saturday on-call pairing changed %d weekends",
            repaired_weekend_konen_pairs,
        )
    repaired_post_konen_friday_pairs = _repair_friday_pairings()
    if repaired_post_konen_friday_pairs:
        logger.info(
            "Post-konen Friday duty/day pairing changed %d assignments",
            repaired_post_konen_friday_pairs,
        )

    print(f"    -> bucket done ({filled_so_far}/{total_slots} shifts filled)")
    report_progress(58, "בודק מרפאות מול אטנדינג")

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

    report_progress(62, "מנקה התנגשויות")
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
        refilled = _refill_required_rows_after_cleanup()
        logger.info(
            "same-day conflict cleanup round %d refilled %d hard slots",
            cleanup_round + 1,
            refilled,
        )
        if not refilled:
            break

    # ---- enforce גנדלמן -> EEG ילדים same day after cleanup so it cannot be removed by it.
    report_progress(64, "מתקן EEG ואפילפסיה")
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
        refilled = _refill_required_rows_after_cleanup()
        logger.info(
            "post-coupling same-day conflict cleanup round %d refilled %d hard slots",
            cleanup_round + 1,
            refilled,
        )
        roster = enforce_epilepsy_eeg_coupling(roster, daily_assignments=daily_assignments)
    _resolve_same_day_conflicts()
    report_progress(66, "משלים אחרי ניקוי")
    final_refilled = _refill_required_rows_after_cleanup()
    if final_refilled:
        logger.info("Post-cleanup hard-row refill filled %d assignments", final_refilled)

    # Cleanup/refill/coupling can move the roster away from the best resident-night
    # balance. Run the resident repairs again on the final shape so obvious legal
    # weekend swaps are not lost late in the pipeline.
    report_progress(67, "מתחיל איזון תורנויות מתמחים אחרי ניקוי")
    post_cleanup_resident_fairness = timed_repair(
        "resident_fairness_post_cleanup",
        lambda: _repair_resident_night_fairness(
            rounds=3,
            weekend_steps=32,
            type_steps=3,
            thursday_steps=3,
            progress_start=67,
            progress_end=82,
            progress_context="איזון אחרי ניקוי",
        ),
    )
    if post_cleanup_resident_fairness:
        logger.info("Post-cleanup resident night fairness repair changed %d assignments", post_cleanup_resident_fairness)
    report_progress(83, "מצמיד ומאזן עבודת שישי")
    final_friday_pairings = _repair_friday_pairings()
    if final_friday_pairings:
        logger.info("Post-cleanup Friday duty/day pairing repair changed %d assignments", final_friday_pairings)
    final_friday_balance = _repair_friday_day_balance(max_steps=8)
    if final_friday_balance:
        logger.info("Post-cleanup Friday day balance repair changed %d assignments", final_friday_balance)
    final_after_duty_removed = _resolve_after_duty_conflicts()
    if final_after_duty_removed:
        logger.info("Post-cleanup after-duty cleanup removed %d assignments", final_after_duty_removed)
    final_refilled_after_duty = _refill_required_rows_after_cleanup()
    if final_refilled_after_duty:
        logger.info("Post-cleanup after-duty refill filled %d assignments", final_refilled_after_duty)

    report_progress(85, "מאזן כוננויות")
    final_repaired_konen = timed_repair("senior_on_call_balance", _repair_konen_mion_balance)
    if final_repaired_konen:
        logger.info("Post-cleanup senior on-call balance repair changed %d assignments", final_repaired_konen)
    final_repaired_konen_weekends = timed_repair(
        "senior_on_call_weekends",
        _repair_senior_on_call_weekends,
    )
    if final_repaired_konen_weekends:
        logger.info(
            "Post-cleanup senior on-call weekend balance changed %d assignment pairs",
            final_repaired_konen_weekends,
        )
    final_repaired_weekend_konen_pairs = timed_repair(
        "senior_weekend_on_call_pairs",
        _repair_weekend_konen_pairs,
    )
    if final_repaired_weekend_konen_pairs:
        logger.info(
            "Post-cleanup Friday/Saturday on-call pairing changed %d weekends",
            final_repaired_weekend_konen_pairs,
        )
    final_konen_friday_pairings = _repair_friday_pairings()
    if final_konen_friday_pairings:
        logger.info("Post-konen Friday duty/day pairing repair changed %d assignments", final_konen_friday_pairings)
    report_progress(87, "מאזן ייעוצים")
    final_repaired_yoeatzim = timed_repair("senior_consult_balance", _repair_yoeatzim_balance)
    if final_repaired_yoeatzim:
        logger.info("Post-cleanup senior consult balance repair changed %d assignments", final_repaired_yoeatzim)
    report_progress(89, "משלים אחרי איזון")
    final_refilled_after_yoeatzim = _refill_required_rows_after_cleanup()
    if final_refilled_after_yoeatzim:
        logger.info("Post-consult hard-row refill filled %d assignments", final_refilled_after_yoeatzim)
    final_post_refill_yoeatzim = _repair_yoeatzim_balance()
    if final_post_refill_yoeatzim:
        logger.info("Post-refill senior consult balance repair changed %d assignments", final_post_refill_yoeatzim)
    final_post_consult_friday_pairings = _repair_friday_pairings()
    if final_post_consult_friday_pairings:
        logger.info("Post-consult Friday duty/day pairing repair changed %d assignments", final_post_consult_friday_pairings)
    report_progress(90, "מתחיל איזון סופי של תורנויות מתמחים")
    final_last_resident_fairness = timed_repair(
        "resident_fairness_final",
        lambda: _repair_resident_night_fairness(
            rounds=3,
            weekend_steps=32,
            type_steps=2,
            thursday_steps=2,
            progress_start=90,
            progress_end=95,
            progress_context="איזון סופי",
        ),
    )
    if final_last_resident_fairness:
        logger.info("Final resident night fairness repair changed %d assignments", final_last_resident_fairness)
    report_progress(96, "מוודא את סדר העדיפויות הסופי")
    final_thursday_balance = _run_tracking_preferred_night_losses(
        lambda: _repair_resident_thursday_final(max_steps=8),
        expected_stage="thursday",
    )
    if final_thursday_balance:
        logger.info("Final resident Thursday-only repair changed %d assignments", final_thursday_balance)
    _refresh_resident_fairness_pool()
    if _resident_night_total_objective()[0] > 1:
        enforced_total_repairs = timed_repair(
            "resident_total_invariant",
            lambda: _run_tracking_preferred_night_losses(
                lambda: _run_resident_balancing_stage_with_preference_preservation(
                    "total",
                    lambda: _run_resident_stage_repair(
                        "total",
                        _repair_resident_night_balance,
                    ),
                    stage_label="final resident total invariant",
                ),
                expected_stage="total",
            ),
        )
        if enforced_total_repairs:
            logger.info("Final resident total invariant changed %d assignments", enforced_total_repairs)
            _repair_resident_night_fairness(
                rounds=2,
                weekend_steps=16,
                type_steps=3,
                thursday_steps=4,
                progress_start=96,
                progress_end=97,
                progress_context="בדיקת הוגנות סופית",
            )
    _refresh_resident_fairness_pool()
    final_total_objective = _resident_night_total_objective()
    if final_total_objective[0] > 1:
        logger.warning(
            "Resident total fairness still unresolved before final personal rules: "
            "objective=%s pool=%s excluded=%s counts=%s",
            final_total_objective,
            sorted(_resident_night_pool()),
            sorted(resident_fairness_pool_excluded),
            dict(sorted(_resident_night_total_counts().items())),
        )
    report_progress(97, "מחיל כללים אישיים סופיים")
    final_mandatory_personal = _apply_mandatory_personal_rules()
    final_companion_personal = _apply_companion_personal_rules()
    if final_mandatory_personal:
        logger.info("Final mandatory personal rules placed %d assignments", final_mandatory_personal)
    if final_companion_personal:
        logger.info("Final companion personal rules added %d assignments", final_companion_personal)
    final_refilled_after_personal = _refill_required_rows_after_cleanup()
    if final_refilled_after_personal:
        logger.info(
            "Post-personal-rule hard-row refill filled %d assignments",
            final_refilled_after_personal,
        )
    if final_mandatory_personal or final_companion_personal or final_refilled_after_personal:
        _forget_repair_noops()
        post_personal_saturday_repairs = _run_tracking_preferred_night_losses(
            lambda: _run_resident_balancing_stage_with_preference_preservation(
                "saturday",
                lambda: _repair_resident_saturday_balance(max_steps=4),
                stage_label="post-personal-rule Saturday balance",
            ),
            expected_stage="saturday",
        )
        if post_personal_saturday_repairs:
            logger.info(
                "Post-personal-rule Saturday balance changed %d assignments",
                post_personal_saturday_repairs,
            )
    _refresh_resident_fairness_pool()
    if _resident_night_total_objective()[0] > 1:
        # Cleanup/refill and mandatory personal placements can change totals
        # after the earlier invariant. Re-open the search here, with a broader
        # two-hop budget, before history is allowed to break equal-core ties.
        _forget_repair_noops()
        final_total_repairs = timed_repair(
            "resident_total_absolute_final",
            lambda: _run_tracking_preferred_night_losses(
                lambda: _run_resident_balancing_stage_with_preference_preservation(
                    "total",
                    lambda: _run_resident_stage_repair(
                        "total",
                        lambda: _repair_resident_night_balance(
                            max_steps=40,
                            search_evaluations=1200,
                        ),
                    ),
                    stage_label="absolute-final resident total fairness",
                ),
                expected_stage="total",
            ),
        )
        if final_total_repairs:
            logger.info(
                "Absolute-final resident total fairness changed %d assignments",
                final_total_repairs,
            )
            _repair_resident_night_fairness(
                rounds=1,
                weekend_steps=12,
                type_steps=2,
                thursday_steps=1,
            )
    _refresh_resident_fairness_pool()
    absolute_final_total = _resident_night_total_objective()
    if absolute_final_total[0] > 1:
        logger.error(
            "Resident total fairness invariant infeasible after exhaustive final search: "
            "objective=%s pool=%s excluded=%s counts=%s",
            absolute_final_total,
            sorted(_resident_night_pool()),
            sorted(resident_fairness_pool_excluded),
            dict(sorted(_resident_night_total_counts().items())),
        )
    _forget_repair_noops()
    final_request_fairness_repairs = _run_tracking_preferred_night_losses(
        lambda: _repair_preferred_resident_night_requests(max_steps=12),
        expected_stage="request_fairness",
    )
    if final_request_fairness_repairs:
        logger.info(
            "Final preferred-request distribution changed %d assignments across exact protected-core ties",
            final_request_fairness_repairs,
        )
    report_progress(97, "\u05de\u05db\u05e8\u05d9\u05e2 \u05dc\u05e4\u05d9 \u05d4\u05d9\u05e1\u05d8\u05d5\u05e8\u05d9\u05d4 \u05d1\u05de\u05e6\u05d1\u05d9 \u05e9\u05d5\u05d5\u05d9\u05d5\u05df")
    resident_history_repairs = timed_repair(
        "resident_history_tiebreak",
        lambda: _run_tracking_preferred_night_losses(
            lambda: _repair_resident_rolling_total_balance(max_steps=20),
            expected_stage="history",
        ),
    )
    if resident_history_repairs:
        logger.info(
            "Resident history tie-break changed %d assignments without changing the protected core",
            resident_history_repairs,
        )
    final_weekend_konen_pairs = _repair_weekend_konen_pairs()
    if final_weekend_konen_pairs:
        logger.info(
            "Final Friday/Saturday on-call pairing changed %d weekends",
            final_weekend_konen_pairs,
        )
    final_attending_konen_pairs = _repair_friday_pairings()
    if final_attending_konen_pairs:
        logger.info(
            "Final Friday on-call/attending pairing changed %d assignments",
            final_attending_konen_pairs,
        )
    final_senior_friday_balance = _repair_friday_day_balance(max_steps=12)
    if final_senior_friday_balance:
        logger.info(
            "Absolute-final senior Friday balance changed %d assignments",
            final_senior_friday_balance,
        )
    absolute_final_rotation_restores = _restore_full_month_rotation_reserves()
    if absolute_final_rotation_restores:
        logger.info(
            "Absolute-final rotation reserve restoration changed %d daytime assignments",
            absolute_final_rotation_restores,
        )
    report_progress(97, "מסמן שיבוצים חסרים")
    final_missing_rows = _mark_missing_required_rows()
    if final_missing_rows:
        logger.info("Marked %d underfilled required rows", final_missing_rows)

    alternate_credits = _build_alternate_credits()
    manual_alternates = _mark_all_manual_alternates()
    logger.info(
        "חלופי manual markers complete: %d/%d earned days marked",
        manual_alternates, len(alternate_credits),
    )
    roster.attrs["preferred_night_audit"] = _log_unmet_preferred_night_requests()
    roster.attrs["fixed_assignment_keys"] = [
        {"date": d.isoformat(), "shift": shift, "name": name}
        for d, shift, name in sorted(
            fixed_assignment_keys,
            key=lambda item: (item[0], item[1], item[2]),
        )
    ]
    roster.attrs["mandatory_personal_assignment_keys"] = [
        {"date": d.isoformat(), "shift": shift, "name": name}
        for d, shift, name in sorted(
            mandatory_personal_assignment_keys,
            key=lambda item: (item[0], item[1], item[2]),
        )
    ]
    active_consecutive_resident_nights = {
        (name, first, second)
        for name, first, second in allowed_consecutive_resident_nights
        if daily_assignments.get(first, {}).get(name, set()).intersection(
            RESIDENT_NIGHT_SHIFTS
        )
        and daily_assignments.get(second, {}).get(name, set()).intersection(
            RESIDENT_NIGHT_SHIFTS
        )
    }
    roster.attrs["resident_consecutive_night_exceptions"] = (
        serialize_resident_consecutive_night_exceptions(
            active_consecutive_resident_nights
        )
    )

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
