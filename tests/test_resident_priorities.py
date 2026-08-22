from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from core.assign2 import (
    ResidentNightMetrics,
    ResidentNightObjective,
    _adjacent_fixed_night_resolution,
    _first_improved_resident_stage,
    _preferred_request_competition_key,
    _preferred_request_removal_penalty,
    _preferred_request_removal_order_cost,
    _preferred_seed_block_cause,
    _request_approval_percentage_basis_points,
    _resident_adjacent_night_penalty_base,
    _resident_flexible_comparison_pool,
    _resident_history_tiebreak_improves,
    _resident_balance_scope,
    _resident_core_equal_through_stage,
    _resident_core_preserved_for_request_recovery,
    _resident_saturday_swap_gain,
    _resident_stage_improves,
    _weekend_konen_target_names_for_row,
)
from core.availability_simple_parser import submitted_names_from_simple


def metrics(**overrides: object) -> ResidentNightMetrics:
    values: dict[str, object] = {
        "missing": 0,
        "total": (1, 20),
        "weekend_friday": (1, 8, 1, 4),
        "saturday": (1, 4),
        "sandwich_total": 2,
        "sandwich_distribution": (1, 2),
        "shift_type": (1, 2, 1, 1, 8, 8),
    }
    values.update(overrides)
    return ResidentNightMetrics(**values)  # type: ignore[arg-type]


class ResidentPriorityTests(unittest.TestCase):
    def test_later_fixed_resident_night_beats_nonfixed_previous_night(self) -> None:
        self.assertEqual(
            _adjacent_fixed_night_resolution(
                previous_is_fixed=False,
                current_is_fixed=True,
                exception_allowed=False,
            ),
            "remove_previous",
        )
        self.assertEqual(
            _adjacent_fixed_night_resolution(
                previous_is_fixed=True,
                current_is_fixed=True,
                exception_allowed=False,
            ),
            "remove_current",
        )
        self.assertEqual(
            _adjacent_fixed_night_resolution(
                previous_is_fixed=True,
                current_is_fixed=True,
                exception_allowed=True,
            ),
            "keep_both",
        )

    def test_balance_scope_distinguishes_self_from_group_benefit(self) -> None:
        before = (7, 2, 1, 1, 2, 1)
        self.assertEqual(
            _resident_balance_scope("sandwich_total", before, (7, 2, 1, 1, 1, 1)),
            "self",
        )
        self.assertEqual(
            _resident_balance_scope("weekend_friday", before, before),
            "others",
        )

    def test_mixed_fixed_and_preferred_capacity_names_request_competition(self) -> None:
        cause = _preferred_seed_block_cause([
            "fixed-slot-full",
            "earlier-preference-filled-slot",
        ])

        self.assertEqual(cause["reason_code"], "request_competition")
        self.assertEqual(cause["block"], "earlier-preference-filled-slot")

    def test_all_fixed_capacity_remains_a_hard_fixed_block(self) -> None:
        cause = _preferred_seed_block_cause([
            "fixed-slot-full",
            "fixed-slot-full",
        ])

        self.assertEqual(cause["reason_code"], "hard_rule")
        self.assertEqual(cause["block"], "fixed-slot-full")

    def test_tomorrow_fixed_night_is_a_named_hard_seed_block(self) -> None:
        cause = _preferred_seed_block_cause(["tomorrow-fixed-night"])

        self.assertEqual(cause["reason_code"], "hard_rule")
        self.assertEqual(cause["block"], "tomorrow-fixed-night")

    def test_adjacent_night_penalty_ignores_only_the_fixed_yom_kippur_pair(self) -> None:
        first = date(2026, 9, 20)
        second = date(2026, 9, 21)
        assignments = {
            first: {"Resident A": {"ת.מיון"}},
        }
        exceptions = {("Resident A", first, second)}

        self.assertEqual(
            _resident_adjacent_night_penalty_base(
                "Resident A",
                second,
                assignments,
                exceptions,
            ),
            0,
        )
        self.assertEqual(
            _resident_adjacent_night_penalty_base(
                "Resident B",
                second,
                {first: {"Resident B": {"ת.מיון"}}},
                exceptions,
            ),
            100,
        )

    def test_weekend_requests_are_removed_after_equal_weekday_requests(self) -> None:
        regular_weekday = _preferred_request_removal_penalty(1, 3)
        regular_weekend = _preferred_request_removal_penalty(1, 5)
        important_weekday = _preferred_request_removal_penalty(2, 3)
        important_weekend = _preferred_request_removal_penalty(2, 5)

        self.assertLess(regular_weekday, regular_weekend)
        self.assertLess(important_weekday, important_weekend)
        self.assertLess(regular_weekend, important_weekday)

    def test_request_approval_percentage_is_stable_and_handles_no_other_requests(self) -> None:
        self.assertEqual(_request_approval_percentage_basis_points(0, 0), 0)
        self.assertEqual(_request_approval_percentage_basis_points(1, 4), 2500)
        self.assertEqual(_request_approval_percentage_basis_points(2, 4), 5000)

    def test_equal_request_removals_prefer_the_more_satisfied_worker(self) -> None:
        less_satisfied = _preferred_request_removal_order_cost(70, 2500)
        more_satisfied = _preferred_request_removal_order_cost(70, 7500)
        stronger_class = _preferred_request_removal_order_cost(100, 10_000)

        self.assertLess(more_satisfied, less_satisfied)
        self.assertLess(less_satisfied, stronger_class)

    def test_request_competition_uses_core_then_percentage_then_history(self) -> None:
        common = {
            "request_priority": (-1, 0, date(2026, 9, 5)),
            "jitter": 10,
        }
        lower_percentage = _preferred_request_competition_key(
            **common,
            projected_core=((1, 8),),
            other_approval_percentage=2500,
            history_key=(8,),
            name="B",
        )
        better_history_but_higher_percentage = _preferred_request_competition_key(
            **common,
            projected_core=((1, 8),),
            other_approval_percentage=5000,
            history_key=(1,),
            name="A",
        )
        better_core = _preferred_request_competition_key(
            **common,
            projected_core=((0, 6),),
            other_approval_percentage=10_000,
            history_key=(9,),
            name="C",
        )

        self.assertLess(lower_percentage, better_history_but_higher_percentage)
        self.assertLess(better_core, lower_percentage)

    def test_history_only_breaks_an_exact_protected_core_tie(self) -> None:
        before = ResidentNightObjective(
            core=metrics(),
            preferred=(1, 1, 1, 1),
            personal=0,
            thursday=(1, 4),
            history=(2, 10, 1, 4, 1, 8),
        )
        better_history = before._replace(history=(1, 8, 1, 4, 1, 8))
        worse_core = better_history._replace(core=metrics(saturday=(2, 6)))
        worse_preference = better_history._replace(preferred=(2, 2, 2, 2))

        self.assertTrue(_resident_history_tiebreak_improves(before, better_history))
        self.assertFalse(_resident_history_tiebreak_improves(before, worse_core))
        # History is later than preferred-request coverage/distribution and
        # therefore cannot worsen it even when the protected core is equal.
        self.assertFalse(_resident_history_tiebreak_improves(before, worse_preference))

    def test_history_can_compensate_duty_type_only_after_the_core_ties(self) -> None:
        before = ResidentNightObjective(
            core=metrics(),
            preferred=(0, 0, 0, 0),
            personal=0,
            thursday=(1, 4),
            history=(1, 10, 1, 4, 2, 8),
        )
        compensated = before._replace(history=(1, 10, 1, 4, 2, 4))
        worse_core = compensated._replace(core=metrics(sandwich_total=3))

        self.assertTrue(_resident_history_tiebreak_improves(before, compensated))
        self.assertFalse(_resident_history_tiebreak_improves(before, worse_core))

    def test_stage_local_request_recovery_ignores_only_later_priorities(self) -> None:
        before = metrics()
        lower_stage_changes = metrics(
            saturday=(2, 8),
            sandwich_total=4,
            shift_type=(2, 6, 2, 4, 10, 10),
        )
        completed_stage_changes = metrics(
            weekend_friday=(2, 10, 2, 6),
        )

        self.assertTrue(
            _resident_core_equal_through_stage(
                before,
                lower_stage_changes,
                "weekend_friday",
            )
        )
        self.assertFalse(
            _resident_core_equal_through_stage(
                before,
                completed_stage_changes,
                "weekend_friday",
            )
        )

    def test_final_request_recovery_still_requires_the_complete_core(self) -> None:
        before = metrics()
        lower_stage_changes = metrics(sandwich_total=3)

        self.assertTrue(
            _resident_core_preserved_for_request_recovery(
                before,
                lower_stage_changes,
                "saturday",
            )
        )
        self.assertFalse(
            _resident_core_preserved_for_request_recovery(
                before,
                lower_stage_changes,
                None,
            )
        )

    def test_fixed_only_capped_resident_does_not_distort_flexible_pool(self) -> None:
        flexible_key = (date(2026, 8, 1), "ת.מיון", "Flexible")
        capped_key = (date(2026, 8, 2), "ת.מיון 2", "FixedOnly")
        pool = _resident_flexible_comparison_pool(
            {"Flexible", "FixedOnly", "Receiver", "Inactive"},
            {flexible_key, capped_key},
            {capped_key},
            {"Receiver"},
        )

        self.assertEqual(pool, {"Flexible", "Receiver"})

    def test_total_improvement_may_worsen_lower_metrics(self) -> None:
        before = metrics()
        after = metrics(
            total=(0, 18),
            weekend_friday=(2, 10, 2, 6),
            sandwich_total=4,
        )
        self.assertTrue(_resident_stage_improves(before, after, "total"))

    def test_weekend_improvement_may_add_sandwich(self) -> None:
        before = metrics()
        after = metrics(weekend_friday=(0, 6, 1, 4), sandwich_total=3)
        self.assertTrue(_resident_stage_improves(before, after, "weekend_friday"))

    def test_saturday_improvement_cannot_worsen_weekends(self) -> None:
        before = metrics()
        after = metrics(weekend_friday=(2, 10, 2, 6), saturday=(0, 2))
        self.assertFalse(_resident_stage_improves(before, after, "saturday"))

    def test_sandwich_improvement_cannot_worsen_saturday(self) -> None:
        before = metrics()
        after = metrics(saturday=(2, 6), sandwich_total=1)
        self.assertFalse(_resident_stage_improves(before, after, "sandwich_total"))

    def test_type_improvement_requires_every_higher_stage(self) -> None:
        before = metrics()
        good = metrics(shift_type=(0, 0, 1, 1, 8, 8))
        bad = metrics(sandwich_total=3, shift_type=(0, 0, 1, 1, 8, 8))
        self.assertTrue(_resident_stage_improves(before, good, "shift_type"))
        self.assertFalse(_resident_stage_improves(before, bad, "shift_type"))

    def test_causal_stage_names_the_first_protected_improvement(self) -> None:
        before = metrics()
        total_wins = metrics(
            total=(0, 18),
            saturday=(0, 2),
        )
        saturday_wins = metrics(saturday=(0, 2))
        self.assertEqual(
            _first_improved_resident_stage(before, total_wins),
            "total",
        )
        self.assertEqual(
            _first_improved_resident_stage(before, saturday_wins),
            "saturday",
        )

    def test_saturday_search_includes_weekdays_and_fridays(self) -> None:
        counts = {"High": 2, "Low": 0}
        self.assertEqual(
            _resident_saturday_swap_gain(5, "High", 2, "Low", counts),
            ("High", "Low", 2),
        )
        self.assertEqual(
            _resident_saturday_swap_gain(4, "Low", 5, "High", counts),
            ("High", "Low", 2),
        )
        self.assertIsNone(_resident_saturday_swap_gain(5, "High", 5, "Low", counts))
        self.assertIsNone(_resident_saturday_swap_gain(1, "High", 4, "Low", counts))

    def test_weekend_konen_pair_replaces_nonfixed_staff(self) -> None:
        self.assertEqual(
            _weekend_konen_target_names_for_row(
                ["קימיאגר"],
                set(),
                "פרץ",
                needed=1,
                soft_cap=1,
            ),
            ["פרץ"],
        )

    def test_weekend_konen_pair_preserves_fixed_staff_and_capacity(self) -> None:
        self.assertIsNone(
            _weekend_konen_target_names_for_row(
                ["כהן"],
                {"כהן"},
                "פרץ",
                needed=1,
                soft_cap=1,
            )
        )
        self.assertEqual(
            _weekend_konen_target_names_for_row(
                ["כהן"],
                {"כהן"},
                "פרץ",
                needed=1,
                soft_cap=2,
            ),
            ["כהן", "פרץ"],
        )


class AvailabilitySubmissionTests(unittest.TestCase):
    def test_latest_form_rows_identify_submitted_workers(self) -> None:
        frame = pd.DataFrame(
            [
                {"שם": "שיר", "כתובת אימייל": "", "חותמת זמן": "2026-07-01"},
                {"שם": "Resident A", "כתובת אימייל": "", "חותמת זמן": "2026-07-02"},
            ]
        )
        self.assertEqual(
            submitted_names_from_simple(frame),
            {"שיר", "Resident A"},
        )


if __name__ == "__main__":
    unittest.main()
