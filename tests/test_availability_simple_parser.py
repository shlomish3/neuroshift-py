from __future__ import annotations

import unittest
from datetime import date

from core.availability_simple_parser import (
    _parse_blob_dates,
    _parse_preferred_blob_dates,
)


class LegacyRequestDateParsingTests(unittest.TestCase):
    def test_weekday_labels_do_not_change_unavailability_dates(self) -> None:
        text = "02/08/2026 יום א', 04/08/2026 יום ג'"

        self.assertEqual(
            _parse_blob_dates(text, default_year=2026),
            [date(2026, 8, 2), date(2026, 8, 4)],
        )

    def test_important_marker_applies_only_to_its_preceding_date(self) -> None:
        text = (
            "02/08/2026 יום א', "
            "03/08/2026 יום ב' (חשוב), "
            "04/08/2026 יום ג'"
        )

        self.assertEqual(
            _parse_preferred_blob_dates(text, default_year=2026),
            [
                (date(2026, 8, 2), 1),
                (date(2026, 8, 3), 2),
                (date(2026, 8, 4), 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()
