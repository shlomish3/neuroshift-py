from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from core.holiday_utils import (
    effective_weekday_letter,
    holiday_eve_names_from_tables,
    holiday_names_from_tables,
)


class HolidayClassificationTests(unittest.TestCase):
    def _tables(self, rows: list[dict[str, str]]) -> dict[str, pd.DataFrame]:
        return {"holidays": pd.DataFrame(rows)}

    def test_named_eve_is_not_full_holiday_even_if_type_was_entered_as_rest(self) -> None:
        tables = self._tables([
            {"תאריך": "20/09/2026", "חג": "ערב יום כיפור", "סוג": "חופש"},
            {"תאריך": "21/09/2026", "חג": "יום כיפור", "סוג": "חופש"},
        ])

        holidays = holiday_names_from_tables(tables)
        eves = holiday_eve_names_from_tables(tables)

        self.assertNotIn(date(2026, 9, 20), holidays)
        self.assertEqual(holidays[date(2026, 9, 21)], "יום כיפור")
        self.assertEqual(eves[date(2026, 9, 20)], "ערב יום כיפור")
        self.assertEqual(
            effective_weekday_letter(date(2026, 9, 19), holidays, eves),
            "ש",
        )
        self.assertEqual(
            effective_weekday_letter(date(2026, 9, 20), holidays, eves),
            "ו",
        )

    def test_information_row_named_eve_is_explicitly_recognized(self) -> None:
        tables = self._tables([
            {"תאריך": "20/09/2026", "חג": "ערב יום כיפור", "סוג": "מידע"},
            {"תאריך": "21/09/2026", "חג": "יום כיפור", "סוג": "חופש"},
        ])

        self.assertEqual(
            holiday_eve_names_from_tables(tables),
            {date(2026, 9, 20): "ערב יום כיפור"},
        )


if __name__ == "__main__":
    unittest.main()
