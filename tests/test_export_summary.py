from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from openpyxl import Workbook
import pandas as pd

from core.export.excel import (
    TORANUT_RESIDENTS,
    _auto_nights_off,
    _build_month_summary_table,
    _build_sheet_ovdim,
    _build_sheet_toranut,
    _build_toranut_explanation_sheet,
    _build_toranut_summary_table,
    _resident_night_candidate_reason,
)
from core.export.origexcel import _build_sheet_ovdim as _build_legacy_sheet_ovdim


class MonthSummaryTests(unittest.TestCase):
    def test_shir_is_in_all_export_name_tables(self) -> None:
        self.assertIn("שיר", TORANUT_RESIDENTS)

        month_ws = Workbook().active
        _build_month_summary_table(
            month_ws,
            month="2026-07",
            refs_by_metric={},
            friday_ref_groups=[],
        )
        self.assertIn("שיר", [month_ws.cell(row, 10).value for row in range(4, month_ws.max_row + 1)])

        duty_ws = Workbook().active
        _build_toranut_summary_table(
            duty_ws,
            first_day_row=4,
            last_day_row=4,
        )
        self.assertIn("שיר", [duty_ws.cell(row, 9).value for row in range(5, duty_ws.max_row + 1)])
        self.assertIsNone(duty_ws["S4"].value)
        self.assertIsNone(duty_ws["AC5"].value)

        workers_ws = Workbook().active
        _build_sheet_ovdim(workers_ws)
        self.assertIn("שיר", [workers_ws.cell(row, 1).value for row in range(1, workers_ws.max_row + 1)])

        legacy_workers_ws = Workbook().active
        _build_legacy_sheet_ovdim(legacy_workers_ws)
        self.assertIn(
            "שיר",
            [legacy_workers_ws.cell(row, 1).value for row in range(1, legacy_workers_ws.max_row + 1)],
        )

    def test_intubation_and_day_hospital_are_appended_with_dynamic_refs(self) -> None:
        ws = Workbook().active
        refs = {
            "אינטובציה": ["'2026-07'!E$10", "'2026-07'!D$45"],
            "אשפוז יום": ["'2026-07'!B$6", "'2026-07'!C$41"],
        }

        _build_month_summary_table(
            ws,
            month="2026-07",
            refs_by_metric=refs,
            friday_ref_groups=[],
        )

        self.assertEqual(ws["T3"].value, "אינטובציה")
        self.assertEqual(ws["U3"].value, "אשפוז יום")
        self.assertIn("E$10", ws["T4"].value.text)
        self.assertIn("D$45", ws["T4"].value.text)
        self.assertIn("B$6", ws["U4"].value.text)
        self.assertIn("C$41", ws["U4"].value.text)
        self.assertEqual(ws.column_dimensions["T"].width, 14)
        self.assertEqual(ws.column_dimensions["U"].width, 14)


class ResidentNightExportTests(unittest.TestCase):
    def test_nights_off_uses_all_scheduler_availability_blocks(self) -> None:
        d_iso = "2026-08-20"
        lookup = {
            ("גלינסקיה", d_iso): [("לא זמין", "חופש")],
            ("ברג", d_iso): [("לא זמין לתורנות", "בקשה")],
            ("ברטל", d_iso): [("לא זמין", "חופש")],
            ("לקן", d_iso): [("לא זמין", "סבב / לפני מבחן")],
            ("שיר", d_iso): [("לא זמין", "חופש")],
        }
        capability = {
            ("גלינסקיה", "ת.מיון"): True,
            ("גלינסקיה", "ת.מיון 2"): True,
            ("ברג", "ת.מיון"): True,
            ("ברג", "ת.מיון 2"): True,
            ("ברטל", "ת.מיון"): False,
            ("ברטל", "ת.מיון 2"): False,
            ("ברטל", "כונן מיון"): True,
            ("לקן", "ת.מיון"): True,
            ("לקן", "ת.מיון 2"): True,
            ("שיר", "ת.מיון"): False,
            ("שיר", "ת.מיון 2"): False,
            ("שיר", "כונן מיון"): False,
        }

        def reason(name: str, _date_iso: str, shift: str) -> str | None:
            if name == "לקן" and shift == "ת.מיון":
                return None
            if name == "ברג":
                return "availability:duty-only-block"
            if name in {"גלינסקיה", "ברטל", "לקן"}:
                return "availability:universal-block"
            return "capability"

        with (
            patch("core.export.excel.unavail_lookup", return_value=lookup),
            patch("core.export.excel.can_do", return_value=capability),
            patch("core.export.excel.eligibility_reason", side_effect=reason),
        ):
            nights_off = _auto_nights_off()

        self.assertEqual(nights_off[d_iso], ["ברג", "ברטל", "גלינסקיה"])

    def test_candidate_audit_matches_scheduler_constraints(self) -> None:
        d = date(2026, 8, 20)
        assignments = {
            ("2026-08-19", "לקן"): {"ת.מיון"},
            ("2026-08-20", "שמואל"): {"ת.מיון"},
            ("2026-08-20", "הסר"): {"מחלקה", "מחקר"},
            ("2026-08-20", "דקל"): {"אחרי תורנות"},
            ("2026-08-21", "ברג"): {"מיון"},
        }

        def reason(name: str, _date_iso: str, _shift: str) -> str | None:
            if name == "שיר":
                return "capability"
            if name == "גלינסקיה":
                return "availability:universal-block"
            return None

        with (
            patch("core.export.excel.eligibility_reason", side_effect=reason),
            patch("core.export.excel.fixed_clinic_lut", return_value={}),
        ):
            self.assertEqual(
                _resident_night_candidate_reason("שיר", d, "ת.מיון 2", assignments),
                "capability",
            )
            self.assertEqual(
                _resident_night_candidate_reason("גלינסקיה", d, "ת.מיון 2", assignments),
                "availability:universal-block",
            )
            self.assertEqual(
                _resident_night_candidate_reason("לקן", d, "ת.מיון 2", assignments),
                "adjacent-resident-night",
            )
            self.assertEqual(
                _resident_night_candidate_reason("שמואל", d, "ת.מיון 2", assignments),
                "same-day-resident-night",
            )
            self.assertEqual(
                _resident_night_candidate_reason("הסר", d, "ת.מיון 2", assignments),
                "resident-daily-limit",
            )
            self.assertEqual(
                _resident_night_candidate_reason("דקל", d, "ת.מיון 2", assignments),
                "illegal-same-day-pair",
            )
            self.assertEqual(
                _resident_night_candidate_reason("ברג", d, "ת.מיון 2", assignments),
                "tomorrow-morning",
            )

    def test_toranut_available_residents_are_static_and_qualified(self) -> None:
        roster = pd.DataFrame(
            [
                {"Date": "2026-08-20", "Shift": "ת.מיון", "Assigned": "שמואל"},
                {"Date": "2026-08-20", "Shift": "ת.מיון 2", "Assigned": "-"},
                {"Date": "2026-08-20", "Shift": "כונן מיון", "Assigned": "ברטל"},
            ]
        )

        def reason(name: str, date_iso: str, _shift: str) -> str | None:
            if name == "שיר":
                return "capability"
            if name == "גלינסקיה" and date_iso == "2026-08-20":
                return "availability:universal-block"
            return None

        ws = Workbook().active
        with (
            patch("core.export.excel.eligibility_reason", side_effect=reason),
            patch("core.export.excel.fixed_clinic_lut", return_value={}),
        ):
            _build_sheet_toranut(ws, 2026, 8, roster)

        available = ws["H23"].value
        self.assertIsInstance(available, str)
        available_names = {name.strip() for name in available.split(",")}
        self.assertIn("ברג", available_names)
        self.assertNotIn("גלינסקיה", available_names)
        self.assertNotIn("שמואל", available_names)
        self.assertNotIn("שיר", available_names)

    def test_missing_night_explanation_reports_no_eligible_resident(self) -> None:
        roster = pd.DataFrame(
            [
                {"Date": "2026-08-19", "Shift": "ת.מיון", "Assigned": "לקן"},
                {"Date": "2026-08-20", "Shift": "ת.מיון", "Assigned": "שמואל"},
                {"Date": "2026-08-20", "Shift": "ת.מיון 2", "Assigned": "-"},
            ]
        )

        def reason(name: str, date_iso: str, shift: str) -> str | None:
            if name == "גלינסקיה" and date_iso == "2026-08-20":
                return "availability:universal-block"
            if name in {"לקן", "שמואל"}:
                return None
            if shift in {"ת.מיון", "ת.מיון 2"}:
                return "capability"
            return None

        ws = Workbook().active
        with (
            patch("core.export.excel.eligibility_reason", side_effect=reason),
            patch("core.export.excel.fixed_clinic_lut", return_value={}),
        ):
            _build_toranut_explanation_sheet(
                ws,
                roster,
                2026,
                8,
                history_df=pd.DataFrame(),
            )

        missing_row = next(
            row
            for row in ws.iter_rows(min_row=4, values_only=True)
            if row[0] == "20/08/2026" and row[2] == "ת.מיון 2"
        )
        self.assertEqual(missing_row[6], "")
        self.assertEqual(missing_row[7], "אין מתמחה כשיר וזמין לפי כללי התורנויות")
        self.assertIn("גלינסקיה", missing_row[14])
        self.assertIn("לא זמין", missing_row[14])
        self.assertIn("שיר", missing_row[14])
        self.assertIn("לא מוסמך למשמרת", missing_row[14])


if __name__ == "__main__":
    unittest.main()
