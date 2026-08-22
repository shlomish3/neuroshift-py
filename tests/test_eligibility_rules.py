from __future__ import annotations

import unittest
from collections import defaultdict
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from core.constants import PRIORITY_BUCKETS
from core.eligibility2 import get_eligible_workers
from core.optimizer import (
    _repair_resident_night_balance,
    _resident_night_violations,
    _resolve_after_duty_conflicts,
)
from core.scheduling_exceptions import (
    serialize_resident_consecutive_night_exceptions,
)


class PreviousNightBlockingTests(unittest.TestCase):
    def test_clinics_are_scheduled_before_nights_and_other_morning_work_after(self) -> None:
        first_night = PRIORITY_BUCKETS.index("\u05ea.\u05de\u05d9\u05d5\u05df")
        self.assertLess(PRIORITY_BUCKETS.index("EMG"), first_night)
        self.assertLess(PRIORITY_BUCKETS.index("EEG"), first_night)
        self.assertGreater(PRIORITY_BUCKETS.index("\u05de\u05d9\u05d5\u05df"), first_night)
        self.assertGreater(PRIORITY_BUCKETS.index("\u05de\u05d7\u05dc\u05e7\u05d4"), first_night)

    def _eligible_with_tomorrow_shift(self, tomorrow_shift: str) -> list[str]:
        worker = "Resident A"
        night = date(2026, 8, 20)
        daily_assignments = defaultdict(dict)
        daily_assignments[night + timedelta(days=1)] = {
            worker: {tomorrow_shift},
        }
        with (
            patch("core.eligibility2.workers_df", return_value=pd.DataFrame([{"\u05e9\u05dd": worker}])),
            patch("core.eligibility2.can_do", return_value={(worker, "\u05ea.\u05de\u05d9\u05d5\u05df"): True}),
            patch("core.eligibility2.eligibility_reason", return_value=None),
            patch("core.eligibility2.fixed_clinic_lut", return_value={}),
            patch("core.eligibility2.is_senior", return_value=False),
        ):
            return get_eligible_workers(
                shift_type="\u05ea.\u05de\u05d9\u05d5\u05df",
                shift_date=night,
                blocked_next_day={},
                extra_day_off=set(),
                daily_assignments=daily_assignments,
                last_night={},
            )

    def test_nonclinic_morning_work_does_not_block_previous_night(self) -> None:
        for shift in ("\u05de\u05d9\u05d5\u05df", "\u05de\u05d7\u05dc\u05e7\u05d4", "\u05de\u05d7\u05e7\u05e8", "\u05e8\u05d5\u05d8\u05e6\u05d9\u05d4"):
            with self.subTest(shift=shift):
                self.assertEqual(self._eligible_with_tomorrow_shift(shift), ["Resident A"])

    def test_clinic_work_blocks_previous_night(self) -> None:
        for shift in ("EMG", "EEG", "\u05de\u05e8\u05e4\u05d0\u05ea \u05ea\u05e0\u05d5\u05e2\u05d4", "\u05e0\u05d5\u05d9\u05e8\u05d5\u05dc\u05d5\u05d2\u05d9\u05d4 \u05db\u05dc\u05dc\u05d9\u05ea"):
            with self.subTest(shift=shift):
                self.assertEqual(self._eligible_with_tomorrow_shift(shift), [])

    def test_legacy_fixed_clinic_metadata_does_not_create_a_hard_assignment(self) -> None:
        worker = "\u05d4\u05e1\u05e8"
        night = date(2026, 8, 20)
        with (
            patch("core.eligibility2.workers_df", return_value=pd.DataFrame([{"\u05e9\u05dd": worker}])),
            patch("core.eligibility2.can_do", return_value={(worker, "\u05ea.\u05de\u05d9\u05d5\u05df"): True}),
            patch("core.eligibility2.eligibility_reason", return_value=None),
            patch(
                "core.eligibility2.fixed_clinic_lut",
                return_value={(worker, "\u05d5"): {"\u05de\u05e8\u05e4\u05d0\u05ea \u05d6\u05d9\u05db\u05e8\u05d5\u05df"}},
            ),
            patch("core.eligibility2.is_senior", return_value=False),
        ):
            eligible = get_eligible_workers(
                shift_type="\u05ea.\u05de\u05d9\u05d5\u05df",
                shift_date=night,
                blocked_next_day={},
                extra_day_off=set(),
                daily_assignments={},
                last_night={},
            )
        self.assertEqual(eligible, [worker])

    def test_yom_kippur_exception_allows_only_the_second_resident_night(self) -> None:
        worker = "Resident A"
        first = date(2026, 9, 20)
        second = date(2026, 9, 21)
        exceptions = {(worker, first, second)}
        capabilities = {
            (worker, "ת.מיון 2"): True,
            (worker, "מיון"): True,
        }
        common = {
            "shift_date": second,
            "blocked_next_day": {worker: {second}},
            "extra_day_off": set(),
            "daily_assignments": {},
            "last_night": {worker: first},
            "allowed_consecutive_resident_nights": exceptions,
        }

        with (
            patch("core.eligibility2.workers_df", return_value=pd.DataFrame([{"שם": worker}])),
            patch("core.eligibility2.can_do", return_value=capabilities),
            patch("core.eligibility2.eligibility_reason", return_value=None),
            patch("core.eligibility2.is_senior", return_value=False),
        ):
            self.assertEqual(
                get_eligible_workers(shift_type="ת.מיון 2", **common),
                [worker],
            )
            self.assertEqual(
                get_eligible_workers(shift_type="מיון", **common),
                [],
            )
            self.assertEqual(
                get_eligible_workers(
                    shift_type="ת.מיון",
                    shift_date=date(2026, 9, 22),
                    blocked_next_day={worker: {date(2026, 9, 22)}},
                    extra_day_off=set(),
                    daily_assignments={},
                    last_night={worker: second},
                    allowed_consecutive_resident_nights=exceptions,
                ),
                [],
            )


class AfterDutyCleanupTests(unittest.TestCase):
    @staticmethod
    def _roster(tomorrow_shift: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "Date": "2026-08-20",
                    "Shift": "\u05ea.\u05de\u05d9\u05d5\u05df",
                    "Assigned": "Resident A",
                    "Needed": 1,
                    "SoftCap": 1,
                },
                {
                    "Date": "2026-08-21",
                    "Shift": tomorrow_shift,
                    "Assigned": "Resident A",
                    "Needed": 1,
                    "SoftCap": 1,
                },
            ]
        )

    def test_night_wins_over_nonclinic_morning_work(self) -> None:
        for shift in ("\u05de\u05d9\u05d5\u05df", "\u05de\u05d7\u05dc\u05e7\u05d4", "\u05de\u05d7\u05e7\u05e8", "\u05e8\u05d5\u05d8\u05e6\u05d9\u05d4"):
            with self.subTest(shift=shift):
                cleaned, removed = _resolve_after_duty_conflicts(self._roster(shift))
                self.assertEqual(cleaned.iloc[0]["Assigned"], "Resident A")
                self.assertEqual(cleaned.iloc[1]["Assigned"], "-")
                self.assertEqual(removed, 1)

    def test_clinic_wins_over_previous_night(self) -> None:
        cleaned, removed = _resolve_after_duty_conflicts(self._roster("EMG"))
        self.assertEqual(cleaned.iloc[0]["Assigned"], "-")
        self.assertEqual(cleaned.iloc[1]["Assigned"], "Resident A")
        self.assertEqual(removed, 1)

    @staticmethod
    def _yom_kippur_roster(day_shift: str | None = None) -> pd.DataFrame:
        rows = [
            {
                "Date": "2026-09-20",
                "Shift": "ת.מיון",
                "Assigned": "Resident A",
                "Needed": 1,
                "SoftCap": 1,
            },
            {
                "Date": "2026-09-21",
                "Shift": "ת.מיון 2",
                "Assigned": "Resident A",
                "Needed": 1,
                "SoftCap": 1,
            },
        ]
        if day_shift:
            rows.append({
                "Date": "2026-09-21",
                "Shift": day_shift,
                "Assigned": "Resident A",
                "Needed": 1,
                "SoftCap": 1,
            })
        roster = pd.DataFrame(rows)
        roster.attrs["resident_consecutive_night_exceptions"] = (
            serialize_resident_consecutive_night_exceptions({
                ("Resident A", date(2026, 9, 20), date(2026, 9, 21)),
            })
        )
        roster.attrs["fixed_assignment_keys"] = [
            {"date": "2026-09-20", "shift": "ת.מיון", "name": "Resident A"},
            {"date": "2026-09-21", "shift": "ת.מיון 2", "name": "Resident A"},
        ]
        return roster

    def test_yom_kippur_pair_survives_while_morning_work_is_removed(self) -> None:
        cleaned, removed = _resolve_after_duty_conflicts(
            self._yom_kippur_roster("מיון")
        )

        self.assertEqual(cleaned.iloc[0]["Assigned"], "Resident A")
        self.assertEqual(cleaned.iloc[1]["Assigned"], "Resident A")
        self.assertEqual(cleaned.iloc[2]["Assigned"], "-")
        self.assertEqual(removed, 1)

    def test_yom_kippur_clinic_still_defeats_the_previous_night(self) -> None:
        cleaned, removed = _resolve_after_duty_conflicts(
            self._yom_kippur_roster("EMG")
        )

        self.assertEqual(cleaned.iloc[0]["Assigned"], "-")
        self.assertEqual(cleaned.iloc[1]["Assigned"], "Resident A")
        self.assertEqual(cleaned.iloc[2]["Assigned"], "Resident A")
        self.assertEqual(removed, 1)
        self.assertEqual(
            cleaned.attrs.get("resident_consecutive_night_exceptions"),
            [],
        )

    def test_consecutive_pair_in_later_month_is_still_removed(self) -> None:
        roster = pd.DataFrame([
            {
                "Date": "2026-10-20",
                "Shift": "ת.מיון",
                "Assigned": "Resident A",
                "Needed": 1,
                "SoftCap": 1,
            },
            {
                "Date": "2026-10-21",
                "Shift": "ת.מיון 2",
                "Assigned": "Resident A",
                "Needed": 1,
                "SoftCap": 1,
            },
        ])

        cleaned, removed = _resolve_after_duty_conflicts(roster)

        self.assertEqual(cleaned.iloc[0]["Assigned"], "Resident A")
        self.assertEqual(cleaned.iloc[1]["Assigned"], "-")
        self.assertEqual(removed, 1)

    def test_optimizer_preserves_both_fixed_exception_assignments(self) -> None:
        roster = self._yom_kippur_roster()
        capabilities = {
            (name, shift): True
            for name in {"Resident A", "Resident B"}
            for shift in {"ת.מיון", "ת.מיון 2"}
        }

        with patch("core.optimizer.can_do", return_value=capabilities):
            repaired, count = _repair_resident_night_balance(roster)

        self.assertEqual(count, 0)
        self.assertEqual(repaired.iloc[0]["Assigned"], "Resident A")
        self.assertEqual(repaired.iloc[1]["Assigned"], "Resident A")
        self.assertEqual(_resident_night_violations(repaired), [])


if __name__ == "__main__":
    unittest.main()
