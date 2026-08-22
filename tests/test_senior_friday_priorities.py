import unittest

from core.assign2 import (
    _friday_count_objective,
    _senior_friday_assignment_priority,
)


class SeniorFridayPriorityTests(unittest.TestCase):
    def test_first_friday_outranks_existing_pair_that_would_create_second(self) -> None:
        first_friday_unpaired = _senior_friday_assignment_priority(1, 2)
        second_friday_paired = _senior_friday_assignment_priority(2, 0)

        self.assertLess(first_friday_unpaired, second_friday_paired)

    def test_two_to_zero_transfer_improves_senior_friday_objective(self) -> None:
        pool = {"גנדלמן", "כהן", "קינן"}
        before = _friday_count_objective(
            {"גנדלמן": 2, "כהן": 0, "קינן": 1},
            pool,
        )
        after = _friday_count_objective(
            {"גנדלמן": 1, "כהן": 1, "קינן": 1},
            pool,
        )

        self.assertLess(after, before)


if __name__ == "__main__":
    unittest.main()
