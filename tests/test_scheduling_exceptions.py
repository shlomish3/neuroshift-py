from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from core.assign_utils import filter_fixed_by_availability, fixed_lookup

from core.scheduling_exceptions import (
    derive_fixed_resident_consecutive_night_exceptions,
    deserialize_resident_consecutive_night_exceptions,
    resident_consecutive_night_allowed,
    serialize_resident_consecutive_night_exceptions,
)


class FixedResidentConsecutiveNightExceptionTests(unittest.TestCase):
    @patch("core.assign_utils.unavail_lookup", return_value={})
    def test_live_style_fixed_rows_survive_loading_and_activate_exception(
        self,
        _mock: object,
    ) -> None:
        tables = {
            "fixed_assign": pd.DataFrame([
                {
                    "סוג משמרת": "ת.מיון 2",
                    "התחלה": "2026-09-20",
                    "סיום": "2026-09-20",
                    "שם": "סעוב",
                },
                {
                    "סוג משמרת": "ת.מיון 2",
                    "התחלה": "2026-09-21",
                    "סיום": "2026-09-21",
                    "שם": "סעוב",
                },
            ]),
            "holidays": pd.DataFrame([
                {"תאריך": "20/09/2026", "חג": "ערב יום כיפור", "סוג": "מידע"},
                {"תאריך": "21/09/2026", "חג": "יום כיפור", "סוג": "חופש"},
            ]),
        }

        fixed = filter_fixed_by_availability(fixed_lookup("2026-09", tables))

        self.assertEqual(
            derive_fixed_resident_consecutive_night_exceptions(fixed),
            {("סעוב", date(2026, 9, 20), date(2026, 9, 21))},
        )

    def test_exception_activates_only_for_same_resident_fixed_on_both_dates(self) -> None:
        fixed = {
            (date(2026, 9, 20), "ת.מיון"): ["Resident A"],
            (date(2026, 9, 21), "ת.מיון 2"): ["Resident A"],
        }

        exceptions = derive_fixed_resident_consecutive_night_exceptions(fixed)

        self.assertEqual(
            exceptions,
            {("Resident A", date(2026, 9, 20), date(2026, 9, 21))},
        )
        self.assertTrue(
            resident_consecutive_night_allowed(
                exceptions,
                "Resident A",
                date(2026, 9, 20),
                date(2026, 9, 21),
            )
        )

    def test_different_workers_or_later_month_do_not_activate_exception(self) -> None:
        different_workers = {
            (date(2026, 9, 20), "ת.מיון"): ["Resident A"],
            (date(2026, 9, 21), "ת.מיון 2"): ["Resident B"],
        }
        october_pair = {
            (date(2026, 10, 20), "ת.מיון"): ["Resident A"],
            (date(2026, 10, 21), "ת.מיון 2"): ["Resident A"],
        }

        self.assertEqual(
            derive_fixed_resident_consecutive_night_exceptions(different_workers),
            set(),
        )
        self.assertEqual(
            derive_fixed_resident_consecutive_night_exceptions(october_pair),
            set(),
        )

    def test_serialized_exception_is_date_bounded(self) -> None:
        exceptions = {
            ("Resident A", date(2026, 9, 20), date(2026, 9, 21)),
        }
        serialized = serialize_resident_consecutive_night_exceptions(exceptions)

        self.assertEqual(
            deserialize_resident_consecutive_night_exceptions(serialized),
            exceptions,
        )
        serialized[0]["first_date"] = "2026-10-20"
        serialized[0]["second_date"] = "2026-10-21"
        self.assertEqual(
            deserialize_resident_consecutive_night_exceptions(serialized),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
