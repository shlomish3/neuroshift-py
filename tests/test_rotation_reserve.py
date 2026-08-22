from __future__ import annotations

import unittest

from core.assign2 import _rotation_reserve_pick_prefix


class RotationReservePriorityTests(unittest.TestCase):
    def test_ordinary_candidate_outranks_balance_improving_rotation_pull(self) -> None:
        ordinary = _rotation_reserve_pick_prefix(False)
        balance_improving_rotation = _rotation_reserve_pick_prefix(
            True,
            (0, 50, 0, -6, 2, "פריאנטה"),
        )

        self.assertLess(ordinary, balance_improving_rotation)

    def test_rotation_balance_still_selects_between_reserves_when_needed(self) -> None:
        priante = _rotation_reserve_pick_prefix(
            True,
            (0, 50, 0, -6, 2, "פריאנטה"),
        )
        shmuel = _rotation_reserve_pick_prefix(
            True,
            (1, 61, 1, -5, 1, "שמואל"),
        )

        self.assertLess(priante, shmuel)
        selected = min(
            {"פריאנטה": priante, "שמואל": shmuel}.items(),
            key=lambda item: item[1],
        )[0]
        self.assertEqual(selected, "פריאנטה")

    def test_rotation_candidate_requires_balance_context(self) -> None:
        with self.assertRaises(ValueError):
            _rotation_reserve_pick_prefix(True)


if __name__ == "__main__":
    unittest.main()
