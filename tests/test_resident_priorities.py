from __future__ import annotations

import unittest

import pandas as pd

from core.assign2 import ResidentNightMetrics, _resident_stage_improves
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
