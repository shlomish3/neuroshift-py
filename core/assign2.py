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
from typing import Dict, Set

import pandas as pd
import logging
import sys
import os

from core import constants
from core.constants import PRIORITY_BUCKETS, NIGHT_DUTY_SHIFTS
from core.clinic_calendar import build_clinic_needs
from core.data import backend_tables, _sh, _backend_tables_cached, _sh_by_id, _gc, _creds         # , save_roster
from core.eligibility2 import get_eligible_workers          # public API
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

# ───────────────────────────────────────────────
# Focused debug target (you can change these)
# ───────────────────────────────────────────────
DEBUG_CLINIC_SHIFT = "EMG"
DEBUG_CLINIC_DATE  = date(2025, 12, 2)   # 2025-12-02


def _is_debug_clinic(shift_type: str, shift_date: date) -> bool:
    return shift_type == DEBUG_CLINIC_SHIFT and shift_date == DEBUG_CLINIC_DATE


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


def auto_assign(month: str, dry_run: bool = False) -> pd.DataFrame:
    constants.CURRENT_TARGET_MONTH = month
    # ───── Phase 0: sheet caches ─────
    print(f"[auto-assign] Reloading sheets for {month} …")
    _clear_sheet_caches()
    print("[auto-assign] Caches cleared")
    tbl = backend_tables()
    print("[auto-assign] Sheets loaded")

    # 1. clinic needs from hospital calendar -----------------------------
    try:
        clinic_needs = build_clinic_needs(month)
        print(f"[auto-assign] Loaded clinic calendar: {len(clinic_needs)} (date,shift) entries")
    except Exception as e:
        print(f"[auto-assign] WARNING: failed to load clinic calendar for {month}: {type(e).__name__}: {e!r}")
        clinic_needs = {}

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

    history: Dict[str, Counter] = defaultdict(Counter)
    for _, r in hist_df.iterrows():
        history[r["Name"]][r["Shift"]] += 1

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

    # 5. fixed assignments + mute clinics -------------------------------
    fixed_raw = fixed_lookup(month, tbl)                     # (date, shift) → [names]
    fixed     = filter_fixed_by_availability(fixed_raw)      # honour day-off requests
    print(f"[auto-assign] Injected {sum(map(len, fixed.values()))} fixed rows")

    roster["Assigned"] = ""

    # ─── mute clinics that clash with a fixed Attending ───
    clinic_lut = fixed_clinic_lut()          # (name, heb_day) → {clinic_shift, …}
    for (d, shift_type), names in fixed.items():
        if shift_type != "אטנדינג":
            continue

        heb = ISO2HEB[d.isoweekday() - 1]
        for doc in names:
            for cl in clinic_lut.get((doc, heb), set()):
                mask = (roster["Date"] == d.isoformat()) & (roster["Shift"] == cl)
                if mask.any():
                    roster.loc[mask, ["Needed", "Assigned"]] = [0, "-"]

    # ─── write the fixed staff into the roster ───
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
            # Identify clinic "owner": the only eligible name in עובדים for this clinic
            eligible_names = [nm for (nm, sh), ok in _el.can_do().items() if sh == row["Shift"] and ok]
            eligible_count = len(eligible_names)

            if eligible_count == 1:
                owner = eligible_names[0]
                d = key[0]  # current date (datetime.date)

                # If owner isn't eligible today for this clinic (for ANY reason) → clinic OFF
                owner_is_eligible = owner in get_eligible_workers(
                    shift_type        = row["Shift"],
                    shift_date        = d,
                    blocked_next_day  = blocked_next_day,
                    extra_day_off     = extra_day_off,
                    daily_assignments = daily_assignments,
                    blocked_reasons   = None,     # don't log here
                    last_night        = last_night,
                )

                if not owner_is_eligible:
                    logger.info("Clinic off: %s %s – owner %s unavailable", row.Date, row["Shift"], owner)
                    roster.loc[idx, ["Needed", "SoftCap", "Assigned"]] = [0, 0, "-"]
                    continue  # skip placing any fixed joiners

            # Owner available (or multiple eligibles exist):
            # ensure capacity fits fixed joiners AND (if sole owner case) the owner
            target_cap = count
            if count >= 1 and eligible_count == 1:
                target_cap = max(target_cap, 2)  # owner + one joiner

            if soft < target_cap:
                roster.at[idx, "SoftCap"] = target_cap
            if need < target_cap:
                roster.at[idx, "Needed"]  = target_cap
            # keep all fx_names (no trimming)

        else:
            # Non-clinics: trim fixed names to the cap like before
            cap = soft or need
            if count > cap:
                logger.warning(
                    "Fixed assignments over-filled %s %s: keeping first %d (%s); dropped %s",
                    row.Date, row.Shift, cap,
                    ", ".join(fx_names[:cap]),
                    ", ".join(fx_names[cap:]),
                )
                fx_names = fx_names[:cap]

        roster.at[idx, "Assigned"] = ", ".join(fx_names)

        # Update counters and state
        d = key[0]
        for n in fx_names:
            history[n][row["Shift"]] += 1
            daily_assignments[d].setdefault(n, set()).add(row["Shift"])

            if row["Shift"] in ("ת.מיון", "ת.מיון 2"):
                blocked_next_day[n].add(d + timedelta(days=1))
                bump_extra_day_off(n, row["Shift"], d, extra_day_off)

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

    # 6. assignment loop -------------------------------------------------
    month_counts = Counter()          # doctor → ת.מיון 2 + ת.מיון count in this run
    total_slots   = int(roster["Needed"].sum())
    filled_so_far = 0
    print(f"[auto-assign] Filling shifts…")

    # ---------------------------------------------------------------------
    # Night-duty load already on the roster (fixed rows + earlier passes)
    # ---------------------------------------------------------------------
    def _names(s: str):
        """split a cell to individual, cleaned names (skip blanks or warnings)"""
        for n in s.split(","):
            n = n.strip()
            if not n or n.startswith("⚠️") or n == "-":
                continue
            yield n

    month_counts = Counter(
        name
        for _, row in roster[roster["Shift"].isin(["ת.מיון", "ת.מיון 2"])].iterrows()
        for name in _names(row.Assigned)
    )


    for bucket in PRIORITY_BUCKETS:
        # (optional) guard against out-of-order rows inside the bucket
        for idx, row in roster[roster["Shift"] == bucket].sort_values("Date").iterrows():
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

            attempts = remaining_hard + extra_soft
            for attempt_idx in range(attempts):
                elig = get_eligible_workers(
                    shift_type        = shift_type,
                    shift_date        = shift_date,
                    blocked_next_day  = blocked_next_day,
                    extra_day_off     = extra_day_off,     # currently passive
                    daily_assignments = daily_assignments,
                    blocked_reasons   = blocked_reasons,
                    last_night        = last_night,
                )

                # drop already-assigned workers
                elig = [w for w in elig if w not in current_set]

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
                    # primary key: how many night duties this month
                    # secondary key: original fairness score (lifetime + recency)
                    pick = min(
                        elig,
                        key=lambda w: (
                            month_counts[w],
                            fairness_score(w, shift_type, shift_date,
                                        history, last_night),
                        ),
                    )
                else:
                    pick = min(
                        elig,
                        key=lambda w: fairness_score(w, shift_type, shift_date,
                                                    history, last_night),
                    )

                # DEBUG: chosen pick for our clinic
                if _is_debug_clinic(shift_type, shift_date):
                    logger.debug("[EMG DEBUG] pick -> %s", pick)

                # ─── accept the pick ───────────────────────────────────────
                current.append(pick)
                current_set.add(pick)

                history[pick][shift_type] += 1
                if shift_type in ("ת.מיון", "ת.מיון 2"):
                    month_counts[pick] += 1          # track month-so-far load

                daily_assignments[shift_date].setdefault(pick, set()).add(shift_type)
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
                warn = f"⚠️ {len(current)}/{needed}"
                roster.at[idx, "Assigned"] = (
                    f"{warn} " + ", ".join(current) if current
                    else f"{warn} Needs manual pick"
                )
            elif current:
                roster.at[idx, "Assigned"] = ", ".join(current)
            filled_so_far += len(current)
    print(f"    -> bucket done ({filled_so_far}/{total_slots} shifts filled)")

    # ───── final clinic-mute pass (dynamic attendings) ─────
    for idx, row in roster[roster["Shift"] == "אטנדינג"].iterrows():
        docs = [n.strip() for n in row.Assigned.split(",") if n.strip()]
        if not docs:
            continue

        d   = date.fromisoformat(row.Date)
        heb = ISO2HEB[d.isoweekday() - 1]

        for doc in docs:
            for cl in fixed_clinic_lut().get((doc, heb), set()):
                mask = (roster["Date"] == row.Date) & (roster["Shift"] == cl)
                if mask.any():
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
                    mask = (roster["Date"] == d.isoformat()) & (roster["Shift"] == cl)
                    if mask.any():
                        roster.loc[mask, ["Needed", "SoftCap", "Assigned"]] = [0, 0, "-"]

    # ---- enforce coupling: גנדלמן אפילפסיה ⇒ EEG same day
    roster = enforce_epilepsy_eeg_coupling(roster)

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
    print("[auto-assign] Writing unassigned ledger …")
    write_unassigned_ledger(
         month             = month,
         blocked_reasons   = blocked_reasons,
         daily_assignments = daily_assignments,
         workers           = workers_df()["שם"].tolist(),
         is_senior_fn      = lambda w: is_senior(w, can_do()),
         log_dir           = LOG_DIR,
     )
 
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