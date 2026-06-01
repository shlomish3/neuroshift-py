"""
core.assign_utils
─────────────────
Stateless helpers used by auto-assignment.

Exposed symbols
---------------
_clean(text)                             – strip hidden RTL/LTR marks
fairness_score(worker, shift, date, history)
fixed_lookup(month, tables)              – expand “שיבוצים קבועים”
bump_extra_day_off(name, shift_type, d, pool)
write_unassigned_ledger(month, blocked_reasons, daily_assignments, workers, is_senior_fn, log_dir)
"""

from __future__ import annotations
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Dict, List, Tuple, Set, Iterable
import re

import pandas as pd

from core.constants import( 
    WEEKDAY_BONUS, BONUS_SHIFT_TYPES, NIGHT_DUTY_SHIFTS, 
    RECENCY_WINDOW_DAYS, RECENCY_PENALTY_MAX, PENALTY_REDUCER,
    )
from core.eligibility2 import unavail_lookup, BLOCKS_ALL
import logging
from pathlib import Path
from itertools import chain
from datetime import date, timedelta
from collections import defaultdict

# ───────────────────────────────────────────────
# small utils
# ───────────────────────────────────────────────
def _clean(text: str) -> str:
    return str(text).replace("\u200f", "").replace("\u200e", "").strip()


# ───────────────────────────────────────────────
#  Fairness score
# ───────────────────────────────────────────────
def _recency_penalty(last_date: date | None, today: date) -> float:
    """
    Graduated linear fade:  Δ=1 → P_MAX,  Δ=W → P_MAX/W,  Δ>W → 0
    Penalty is multiplied by PENALTY_REDUCER if the *up-coming* duty
    is on a “desirable” weekday (e.g. Wednesday halves the pain).
    """
    if not last_date:
        return 0.0

    delta = (today - last_date).days
    if 1 <= delta <= RECENCY_WINDOW_DAYS:
        base = RECENCY_PENALTY_MAX * (RECENCY_WINDOW_DAYS - delta + 1) / RECENCY_WINDOW_DAYS
        reducer = PENALTY_REDUCER.get(today.strftime("%a"), 1.0)
        return base * reducer
    return 0.0

def fairness_score(
    worker: str,
    shift_type: str,
    shift_date: date,
    history: Dict[str, Counter],
    last_night: Dict[str, date],
) -> float:
    """
    Lower score ⇒ higher likelihood of being chosen.

    score = lifetime_count
          + recency_penalty              (night duties only)
          + same_weekday_bump            (night duties only)
          + desirability(today)          (all duties)
          + 0.25 × desirability(prev)    (night duties only)

    Components
    ----------
    1. **Base count** – how many times *worker* has already covered *shift_type*.
    2. **Recency penalty** – larger when the previous night duty is closer,
       up to `RECENCY_PENALTY_MAX` (applies to all NIGHT_DUTY_SHIFTS).
    3. **Same-weekday bump** – +1.0 if the worker’s last night duty
       was on the same weekday; encourages rotation within the week.
    4. **Week-day bias** – small bonus/penalty for shifts in `WEEKDAY_BONUS`.
    """
    # 1) lifetime count component
    score = history[worker][shift_type]

    # 2) add desirability of the *target* weekday  (all duties)
    score += WEEKDAY_BONUS.get(shift_date.strftime("%a"), 0.0)

    # 3) night-duty extras
    if shift_type in NIGHT_DUTY_SHIFTS:
        # 3-a  recency
        score += _recency_penalty(last_night.get(worker), shift_date)

        prev_dt = last_night.get(worker)

        # 3-b  discourage giving the same weekday twice in a row
        if prev_dt and prev_dt.weekday() == shift_date.weekday():
            score += 2.0        # tweak magnitude if stronger/weaker push desired

        # 3-c  quarter-weight of previous duty’s weekday bias
        if prev_dt:
            score += 0.25 * WEEKDAY_BONUS.get(prev_dt.strftime("%a"), 0.0)

    return score


# recognise every variant: "סבב", "לפני מבחן", "סבב / לפני מבחן", "סבב חיצוני" …
_EXEMPT_PAT = re.compile(r"(סבב|לפני[ _]מבחן)")

def _is_exempt_night_source(src: str) -> bool:
    return bool(_EXEMPT_PAT.search(src))

# -------------------------------------------------------------------------
def filter_fixed_by_availability(
    fixed: dict[tuple[date, str], list[str]]
) -> dict[tuple[date, str], list[str]]:
    """
    Keep fixed rows unless a *real* universal block exists.
    Night shifts (ת.מיון / ת.מיון 2 / כונן מיון) ignore blocks
    whose source contains “סבב” or “לפני מבחן”.
    """
    unavail = unavail_lookup()
    out: dict[tuple[date, str], list[str]] = {}

    for (d, shift_raw), names in fixed.items():
        shift = _clean(shift_raw)          # ← NEW: strip spaces & LTR/RTL marks
        keep: list[str] = []

        for n in names:
            blocks = unavail.get((n, d.isoformat()), [])

            def _counts_as_universal(bt: str, src: str) -> bool:
                if bt not in BLOCKS_ALL or src == "מרפאה קבועה":
                    return False
                if shift in NIGHT_DUTY_SHIFTS and _is_exempt_night_source(src):
                    return False
                return True

            # --- DEBUG hook -------------------------------------------------
            for bt, src in blocks:
                logging.getLogger(__name__).debug(
                    "⇢ block check  %s  %s  %s  –  (%s | %s)",
                    d, shift, n, bt, src)

            # ----------------------------------------------------------------
            if any(_counts_as_universal(bt, src) for bt, src in blocks):
                logging.getLogger(__name__).warning(
                    "Fixed assignment skipped: %s %s – %s (universal block)",
                    d, shift, n
                )
            else:
                keep.append(n)

        if keep:
            out[(d, shift)] = keep

    return out

# ──────────────────────────────────────────────────────────────
#  Fixed-assignment helper – שיבוצים קבועים
# ──────────────────────────────────────────────────────────────
def fixed_lookup(month: str, tables: dict) -> Dict[Tuple[date, str], List[str]]:
    """
    Build a {(date, shift_type): [names…]} map for *month*.

    • **Night shifts** (ת.מיון / ת.מיון 2 / כונן מיון) are kept even on
      Fridays, Saturdays and days tagged *חופש*.
    • All other shift types are still limited to Sun-Thu | non-holiday days.
    """
    fx = tables["fixed_assign"].copy()
    if fx.empty:
        return {}

    COL_SHIFT, COL_START, COL_END, COL_NAME = "סוג משמרת", "התחלה", "סיום", "שם"
    missing = {COL_SHIFT, COL_START, COL_END, COL_NAME} - set(fx.columns)
    if missing:
        raise ValueError(f"שיבוצים קבועים missing: {', '.join(sorted(missing))}")

    # ── normalise ─────────────────────────────────────────────
    fx[COL_SHIFT] = fx[COL_SHIFT].astype(str).map(_clean)
    fx[COL_NAME]  = fx[COL_NAME].astype(str).str.strip()
    fx[COL_START] = pd.to_datetime(fx[COL_START], format="mixed",
                                   dayfirst=True, errors="coerce").dt.date
    fx[COL_END]   = pd.to_datetime(fx[COL_END],   format="mixed",
                                   dayfirst=True, errors="coerce").dt.date
    fx = fx.dropna(subset=[COL_START, COL_END])

    # ── month boundaries ──────────────────────────────────────
    yr, mon = map(int, month.split("-"))
    first_day = date(yr, mon, 1)
    last_day  = (first_day.replace(day=28) + timedelta(days=4)).replace(day=1) \
                - timedelta(days=1)

    # ── holidays tagged 'חופש' ────────────────────────────────
    hol_df = tables["holidays"]
    rest_days = set(
        pd.to_datetime(
            hol_df.loc[hol_df["סוג"] == "חופש", "תאריך"],
            format="mixed", dayfirst=True, errors="coerce"
        ).dropna().dt.date
    )

    # ── expand spans → dict ───────────────────────────────────
    out: Dict[Tuple[date, str], List[str]] = {}
    for _, row in fx.iterrows():
        span_start = max(row[COL_START], first_day)
        span_end   = min(row[COL_END],   last_day)
        if span_start > span_end:
            continue

        is_night_shift = row[COL_SHIFT] in NIGHT_DUTY_SHIFTS
        d = span_start
        while d <= span_end:
            #  allow all dates for night duties;
            #  for other shifts keep Sun-Thu & non-holiday only
            if is_night_shift or (d.weekday() not in (4, 5) and d not in rest_days):
                key = (d, row[COL_SHIFT])
                out.setdefault(key, []).append(row[COL_NAME])
            d += timedelta(days=1)

    return out


# ──────────────────────────────────────────────────────────────
#  Extra-day-off pool  (silent; no 😴 marking)
# ──────────────────────────────────────────────────────────────
def bump_extra_day_off(name: str, shift_type: str, d: date, pool: set[str]) -> None:
    if shift_type not in ("ת.מיון", "ת.מיון 2"):
        return
    wd = d.weekday()        # Mon=0 … Sun=6

    if wd == 4:             # Fri night
        pool.add(name)

    if wd == 5:             # Sat
        prev = d - timedelta(days=1)
        if prev.weekday() in (3, 4):   # Thu or Fri
            pool.add(name)

# ───────────────────────────────────────────────
#  Ledger reason prioritiser
# ───────────────────────────────────────────────

_LEDGER_RULES = (
    ("availability:universal-block",  lambda rs: "availability:universal-block" in rs),
    ("availability:other",            lambda rs: any(r.startswith("availability:") for r in rs)),
    ("next-day rest rule",            lambda rs: "next-day rest rule" in rs),
    ("capability",                    lambda rs: rs == {"capability"}),
    ("all shifts were full",          lambda rs: True),                      # fallback
)

def _strongest_tag(reasons: Set[str]) -> str:
    """Return the highest-priority tag from *_LEDGER_RULES*."""
    for tag, pred in _LEDGER_RULES:
        if pred(reasons):
            return tag
    return "all shifts were full"      # safety-net


def month_days(year_month: str) -> Iterable[date]:
    """Yield every calendar day for *YYYY-MM*."""
    yr, mon = map(int, year_month.split("-"))
    first = date(yr, mon, 1)
    last  = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return (first + timedelta(days=i) for i in range((last - first).days + 1))

# ──────────────────────────────────────────────────────────────
#  Epilepsy↔EEG coupling: if גנדלמן has מרפאת אפילפסיה → also EEG that day
# ──────────────────────────────────────────────────────────────
def enforce_epilepsy_eeg_coupling(roster: pd.DataFrame,
                                  daily_assignments: Dict[date, Dict[str, Set[str]]] | None = None
                                  ) -> pd.DataFrame:
    """
    Rule: On any day where 'מרפאת אפילפסיה' includes גנדלמן,
    EEG that same day must be גנדלמן (exclusively).
    If she isn't in מרפאת אפילפסיה that day, EEG is untouched.
    """
    target_name   = _clean("גנדלמן")
    epi_shift     = "מרפאת אפילפסיה גנדלמן"
    eeg_shift     = "EEG"

    def _split_names(cell: str) -> list[str]:
        if not isinstance(cell, str):
            return []
        out = []
        for n in cell.split(","):
            n = _clean(n)
            if n and not n.startswith("⚠️") and n != "-":
                out.append(n)
        return out

    out = roster.copy()

    # iterate only days where אפילפסיה has גנדלמן
    mask_epi = out["Shift"] == epi_shift
    for idx, row in out[mask_epi].iterrows():
        names = set(_split_names(row["Assigned"]))
        if target_name not in names:
            continue  # only enforce on days she actually does אפילפסיה

        d_iso = row["Date"]
        eeg_mask = (out["Date"] == d_iso) & (out["Shift"] == eeg_shift)
        if not eeg_mask.any():
            continue  # no EEG row that day

        eeg_idx = out.index[eeg_mask][0]
        # Ensure caps allow exactly one (her)
        def _to_int(x, default=0):
            try:
                return int(x)
            except Exception:
                return default

        cur_need = _to_int(out.at[eeg_idx, "Needed"], 0)
        cur_soft = _to_int(out.at[eeg_idx, "SoftCap"], 0)
        if cur_need < 1:
            out.at[eeg_idx, "Needed"] = 1
        if cur_soft < 1:
            out.at[eeg_idx, "SoftCap"] = 1

        # Set EEG assignment to גנדלמן only (replace whoever was there)
        out.at[eeg_idx, "Assigned"] = target_name

        # Optional: keep daily_assignments in sync for the ledger
        if daily_assignments is not None:
            d = date.fromisoformat(d_iso)
            # remove EEG from anyone else that day
            todays = daily_assignments.get(d, {})
            to_remove = []
            for worker, shifts in todays.items():
                if eeg_shift in shifts and worker != target_name:
                    shifts.discard(eeg_shift)
                    if not shifts:
                        to_remove.append(worker)
            for w in to_remove:
                todays.pop(w, None)
            # add EEG to גנדלמן
            todays.setdefault(target_name, set()).add(eeg_shift)

    return out



def write_unassigned_ledger(
    *,
    month: str,
    blocked_reasons: Dict[tuple[date, str, str] | tuple[date, str], str],
    daily_assignments: Dict[date, Dict[str, Set[str]]],
    workers: List[str],
    is_senior_fn,
    log_dir: str,
) -> None:
    """
    Build `unassigned_YYYY-MM.txt` and log its creation.

    • One line per idle worker-day.
    • Residents listed before seniors every day.
    """
    # ── 1. collapse to (date, worker) → strongest tag ──────────────────
    reasons_per_dw: Dict[tuple[date, str], Set[str]] = defaultdict(set)
    for key, reason in blocked_reasons.items():
        d, w = key[0], key[-1]           # key may be (d, w) or (d, shift, w)
        reasons_per_dw[(d, w)].add(reason)

    collapsed = {dw: _strongest_tag(rs) for dw, rs in reasons_per_dw.items()}

    # ── 2. assemble ledger lines, residents first ──────────────────────
    lines: List[str] = []
    for d in month_days(month):
        todays_assigned = daily_assignments.get(d, {})
        idle = set(workers) - set(todays_assigned.keys())

        res, sen = [], []
        for w in idle:
            (res if not is_senior_fn(w) else sen).append(w)

        for w in chain(sorted(res), sorted(sen)):
            lines.append(f"{d.isoformat()} – {w} >> {collapsed.get((d, w), 'all shifts were full')}")

    # ── 3. write to disk ───────────────────────────────────────────────
    if lines:
        out = Path(log_dir) / f"unassigned_{month}.txt"
        out.write_text("\n".join(lines), encoding="utf-8")
        logging.getLogger(__name__).info("Unassigned ledger written to %s  (%d lines)", out, len(lines))
