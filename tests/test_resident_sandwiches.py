from __future__ import annotations

from datetime import date
import unittest

from core.assign2 import _resident_sandwich_pairs_from_dates


class ResidentSandwichPairsTests(unittest.TestCase):
    def test_cross_month_pair_is_movable_from_current_endpoint(self) -> None:
        july_31 = date(2026, 7, 31)
        august_2 = date(2026, 8, 2)

        result = _resident_sandwich_pairs_from_dates(
            {"פריאנטה": {july_31, august_2}},
            movable_dates={date(2026, 8, day) for day in range(1, 32)},
        )

        self.assertEqual(result, [("פריאנטה", july_31, august_2)])

    def test_previous_month_only_pair_is_not_offered_for_repair(self) -> None:
        result = _resident_sandwich_pairs_from_dates(
            {"פריאנטה": {date(2026, 7, 4), date(2026, 7, 6)}},
            movable_dates={date(2026, 8, day) for day in range(1, 32)},
        )

        self.assertEqual(result, [])

    def test_middle_night_prevents_sandwich(self) -> None:
        result = _resident_sandwich_pairs_from_dates(
            {
                "פריאנטה": {
                    date(2026, 7, 31),
                    date(2026, 8, 1),
                    date(2026, 8, 2),
                }
            },
            movable_dates={date(2026, 8, day) for day in range(1, 32)},
        )

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
