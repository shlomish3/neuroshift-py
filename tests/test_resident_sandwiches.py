from __future__ import annotations

from datetime import date
import unittest

from core.assign2 import (
    _resident_sandwich_pairs_from_dates,
    _resident_type_compensation_distance,
    _resident_type_excess_gap,
)


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


class ResidentShiftTypeFairnessTests(unittest.TestCase):
    def test_even_total_requires_equal_shift_types(self) -> None:
        self.assertEqual(_resident_type_excess_gap(6, 3, 3), 0)
        self.assertEqual(_resident_type_excess_gap(6, 4, 2), 2)

    def test_odd_total_allows_exactly_one_extra_shift_type(self) -> None:
        self.assertEqual(_resident_type_excess_gap(7, 3, 4), 0)
        self.assertEqual(_resident_type_excess_gap(7, 5, 2), 2)

    def test_harder_month_prefers_extra_tmion2(self) -> None:
        self.assertEqual(_resident_type_compensation_distance(7, 3, 4, 2), 0)
        self.assertGreater(_resident_type_compensation_distance(7, 4, 3, 2), 0)
        self.assertEqual(_resident_type_compensation_distance(6, 3, 3, 2), 0)

if __name__ == "__main__":
    unittest.main()
