"""
core.eligibility2.py
High-level eligibility functions consumed by assign2.py."""

from __future__ import annotations
from datetime import date, timedelta
from typing import Dict, List, Set

import logging
import pandas as pd
from core.constants import DUAL_OK
from core.elig_utils import (
    BLOCKS_ALL, BLOCKS_DUTY, DUTY_SHIFTS,
    CLINIC_SHIFTS, DAY_SHIFTS,
    can_do, workers_df, fixed_clinic_lut, unavail_lookup,
    weekday_letter, is_senior
)

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# public helpers
# ──────────────────────────────────────────────
# List of “לא זמין” sources that do **not** block ת.מיון
EXEMPT_DUTY_SOURCES = {"סבב / לפני מבחן"}


def eligibility_reason(name: str, date_iso: str, shift: str) -> str | None:
    """
    Return a reason-tag that blocks *shift* for *name* on *date_iso*,
    or None if the worker is eligible.
    """
    if not can_do().get((name, shift), True):
        return "capability"

    blocks = unavail_lookup().get((name, date_iso), [])

    # universal blocks  ── allow bypass for ת.מיון + exempt sources
    if any(
        bt in BLOCKS_ALL
        and src != "מרפאה קבועה"
        and not (shift == "ת.מיון" and src in EXEMPT_DUTY_SOURCES)
        for bt, src in blocks
    ):
        return "availability:universal-block"

    # duty-only blocks
    if shift in DUTY_SHIFTS and any(bt in BLOCKS_DUTY for bt, _ in blocks):
        return "availability:duty-only-block"

    # clinic-rule conflicts
    todays_clinics = fixed_clinic_lut().get((name, weekday_letter(date_iso)), set())
    if shift in CLINIC_SHIFTS and todays_clinics and shift not in todays_clinics:
        return "availability:clinic-rule"

    # “מרפאה קבועה” nuance: blocks day/duty shifts outside the clinic itself
    for bt, src in blocks:
        if (
            bt == "לא זמין"
            and src == "מרפאה קבועה"
            and shift in (DAY_SHIFTS | DUTY_SHIFTS)
            and shift not in todays_clinics
        ):
            return "availability:clinic-rule"

    return None  # eligible



def is_eligible(name: str, date_iso: str, shift: str) -> bool:
    return eligibility_reason(name, date_iso, shift) is None


def apply(roster_df: pd.DataFrame) -> pd.DataFrame:
    """Add a “זכאים” column to *roster_df* and return a copy."""
    names = workers_df()["שם"].tolist()
    build = lambda row: ", ".join(
        n for n in names if is_eligible(n, row["Date"], row["Shift"])
    )
    out = roster_df.copy()
    out["זכאים"] = out.apply(build, axis=1)
    return out


# ──────────────────────────────────────────────
#  assign.py helper
# ──────────────────────────────────────────────
def get_eligible_workers(
    *,
    shift_type: str,
    shift_date: date,
    blocked_next_day: Dict[str, Set[date]],
    extra_day_off: Set[str],
    daily_assignments: Dict[date, Dict[str, Set[str]]] | None = None,
    blocked_reasons: Dict[tuple[date, str], str] | None = None,
    last_night: Dict[str, date] | None = None,
) -> list[str]:
    """Return the list of workers who may staff (*shift_date*, *shift_type*)."""
    daily_assignments = daily_assignments or {}
    last_night        = last_night or {}
    lut       = can_do()
    all_names = workers_df()["שם"].tolist()
    elig: list[str] = []

    def note(name: str, reason: str) -> None:
        key = (shift_date, shift_type, name)
        if blocked_reasons is not None and key not in blocked_reasons:
            blocked_reasons[key] = reason
            log.debug("%s %s – %s blocked (%s)",
                      shift_date, shift_type, name, reason)

    for n in all_names:
        # ── capability / personal availability ─────────────────────────
        reason = eligibility_reason(n, shift_date.isoformat(), shift_type)
        if reason:
            note(n, reason);  continue

        # Clinics and ER tomorrow block tonight's resident night duty.
        # These rows are assigned before nights, so the night pass must avoid
        # creating an אחרי תורנות conflict after the fact.
        if shift_type in ("ת.מיון", "ת.מיון 2"):
            tomorrow = daily_assignments.get(shift_date + timedelta(days=1), {}).get(n, set())
            if any(s in CLINIC_SHIFTS or s == "מיון" for s in tomorrow):
                note(n, "tomorrow morning blocks night");  continue

        # ── mandatory post-ת.מיון rest ────────────────────────────
        if shift_date in blocked_next_day.get(n, set()):
            note(n, "next-day rest rule");  continue
        
        # forbid night-duty two evenings in a row
        if shift_type in ("ת.מיון", "ת.מיון 2") and \
            (shift_date - last_night.get(n, date.min)).days < 2:
            note(n, "night-cooldown (<2 days)");  continue

        today_set = daily_assignments.get(shift_date, {}).get(n, set())
        senior    = is_senior(n, lut)

        # ── first shift today – always allowed ────────────────────────
        if not today_set:
            elig.append(n);  continue

        # ── second shift today – check pair legality ──────────────────
        total_after = len(today_set) + 1     # how many if we add this one?
        if len(today_set) == 1:
            existing = next(iter(today_set))
            if (existing, shift_type) in DUAL_OK:
                # residents may not exceed 2/day via DUAL_OK
                if not senior and total_after > 2:
                    note(n, "resident >2 via dual-ok");  continue
                elig.append(n);  continue
            else:
                note(n, "illegal pair");  continue

        # ── third (or more) shift today – senior-only logic ───────────
        if not senior:
            note(n, "resident >2/day");  continue    # residents capped at 2

        # senior:
        if total_after > 3:
            note(n, "senior >3/day");  continue
        special = {"כונן מיון", "בכיר מיון"}
        if total_after > 2 and shift_type not in special \
           and not any(s in special for s in today_set):
            note(n, "third shift lacks special");  continue

        # ── survived all tests ────────────────────────────────────────
        elig.append(n)

    # ── safety-net: ensure every (date, name) has some tagged reason ───
    if blocked_reasons is not None:
        for n in all_names:
            blocked_reasons.setdefault((shift_date, n), "no eligible shifts")

    return elig

