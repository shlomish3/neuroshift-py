from __future__ import annotations

from datetime import date
import unittest

from core.assign2 import (
    _resident_actionable_sandwich_pairs_from_dates,
    _resident_night_spacing_penalty,
    _resident_requested_sandwich_endpoints_from_dates,
    _resident_sandwich_penalty,
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

    def test_pair_requested_at_both_endpoints_is_not_actionable(self) -> None:
        first = date(2026, 9, 7)
        second = date(2026, 9, 9)
        requests = {
            ("הסר", first): 1,
            ("הסר", second): 1,
        }

        result = _resident_actionable_sandwich_pairs_from_dates(
            {"הסר": {first, second}},
            preferred_night_requests=requests,
        )

        self.assertEqual(result, [])

    def test_both_requested_endpoints_are_protected_from_sandwich_repairs(self) -> None:
        first = date(2026, 9, 7)
        shared = date(2026, 9, 9)
        third = date(2026, 9, 11)
        requests = {
            ("הסר", first): 1,
            ("הסר", shared): 1,
        }

        endpoints = _resident_requested_sandwich_endpoints_from_dates(
            {"הסר": {first, shared, third}},
            requests,
        )

        self.assertEqual(endpoints, {("הסר", first), ("הסר", shared)})

    def test_requesting_only_one_endpoint_does_not_exempt_pair(self) -> None:
        first = date(2026, 9, 7)
        second = date(2026, 9, 9)

        result = _resident_actionable_sandwich_pairs_from_dates(
            {"הסר": {first, second}},
            preferred_night_requests={("הסר", second): 1},
        )

        self.assertEqual(result, [("הסר", first, second)])

    def test_requested_pair_is_allowed_during_assignment(self) -> None:
        first = date(2026, 9, 7)
        second = date(2026, 9, 9)
        requests = {
            ("הסר", first): 1,
            ("הסר", second): 1,
        }
        daily_assignments = {
            first: {"הסר": {"ת.מיון"}},
        }

        self.assertEqual(
            _resident_night_spacing_penalty(
                "הסר",
                second,
                {"הסר": first},
                preferred_night_requests=requests,
            ),
            0,
        )
        self.assertEqual(
            _resident_sandwich_penalty(
                "הסר",
                second,
                daily_assignments,
                preferred_night_requests=requests,
            ),
            0,
        )

    def test_unrequested_pair_keeps_assignment_penalty(self) -> None:
        first = date(2026, 9, 7)
        second = date(2026, 9, 9)
        daily_assignments = {
            first: {"הסר": {"ת.מיון"}},
        }

        self.assertEqual(
            _resident_night_spacing_penalty(
                "הסר",
                second,
                {"הסר": first},
                preferred_night_requests={("הסר", second): 1},
            ),
            100,
        )
        self.assertEqual(
            _resident_sandwich_penalty(
                "הסר",
                second,
                daily_assignments,
                preferred_night_requests={("הסר", second): 1},
            ),
            100,
        )


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
