from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from core.assign_utils import (
    KONEN_MION_SHIFT,
    canonical_shift_name,
    filter_fixed_by_availability,
    fixed_lookup,
)


class FixedAssignmentTests(unittest.TestCase):
    def test_konen_mion_abbreviation_is_canonicalized(self) -> None:
        self.assertEqual(canonical_shift_name("\u05db.\u05de\u05d9\u05d5\u05df"), KONEN_MION_SHIFT)
        self.assertEqual(canonical_shift_name("\u05db. \u05de\u05d9\u05d5\u05df"), KONEN_MION_SHIFT)

    def test_abbreviated_fixed_konen_is_kept_on_saturday(self) -> None:
        fixed = pd.DataFrame(
            [
                {
                    "\u05e1\u05d5\u05d2 \u05de\u05e9\u05de\u05e8\u05ea": "\u05db.\u05de\u05d9\u05d5\u05df",
                    "\u05d4\u05ea\u05d7\u05dc\u05d4": "01/08/2026",
                    "\u05e1\u05d9\u05d5\u05dd": "01/08/2026",
                    "\u05e9\u05dd": "Senior A",
                }
            ]
        )
        result = fixed_lookup(
            "2026-08",
            {"fixed_assign": fixed, "holidays": pd.DataFrame()},
        )
        self.assertEqual(result[(date(2026, 8, 1), KONEN_MION_SHIFT)], ["Senior A"])

    @patch("core.assign_utils.unavail_lookup", return_value={})
    def test_availability_filter_returns_canonical_fixed_key(self, _mock: object) -> None:
        result = filter_fixed_by_availability(
            {(date(2026, 8, 1), "\u05db.\u05de\u05d9\u05d5\u05df"): ["Senior A"]}
        )
        self.assertEqual(result, {(date(2026, 8, 1), KONEN_MION_SHIFT): ["Senior A"]})


if __name__ == "__main__":
    unittest.main()
