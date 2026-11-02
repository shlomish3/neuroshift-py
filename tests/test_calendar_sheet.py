"""Readback and preservation checks for the private calendar workbook backend."""
from datetime import datetime
import io
import unittest

from openpyxl import Workbook, load_workbook

from core.calendar_sheet import (TABLE_FIELDS, LINKS_TAB, LINK_HEADERS, read_snapshot,
                                read_subscription_rows, update_workbook, subscription_rows)


def sample_workbook():
    workbook = Workbook()
    data = workbook.active
    data.title = 'נתוני שיבוצים'
    data.append(TABLE_FIELDS)
    data.append(['2026-11-03', 'ת.מיון', 'שמואל', 'D6', '20261103T140000Z', '20261104T080000Z', 'weekday'])
    extra = workbook.create_sheet('הערות')
    extra['A1'] = 'תוכן לשימור'
    extra['B2'] = '=1+2'
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class CalendarSheetTests(unittest.TestCase):
    def setUp(self):
        self.original = sample_workbook()
        self.feeds = {'שמואל': 'https://example.test/exec?key=one',
                      'פרץ': 'https://example.test/exec?key=two'}

    def test_links_tab_has_copyable_names_and_original_urls(self):
        data = update_workbook(self.original, feeds=self.feeds)
        self.assertEqual(read_subscription_rows(data), subscription_rows(self.feeds))
        workbook = load_workbook(io.BytesIO(data))
        sheet = workbook[LINKS_TAB]
        self.assertEqual([sheet.cell(1, c).value for c in range(1, 4)], LINK_HEADERS)
        self.assertEqual(sheet.freeze_panes, 'A2')
        self.assertEqual(sheet.auto_filter.ref, 'A1:C3')
        self.assertTrue(sheet.sheet_view.rightToLeft)
        self.assertEqual(sheet['C2'].hyperlink.target, sheet['C2'].value)
        self.assertEqual(sheet['C2'].number_format, '@')

    def test_links_only_update_preserves_every_existing_tab_cell_and_formula(self):
        data = update_workbook(self.original, feeds=self.feeds)
        before = load_workbook(io.BytesIO(self.original))
        after = load_workbook(io.BytesIO(data))
        for sheet in before:
            for row in sheet:
                for cell in row:
                    self.assertEqual(after[sheet.title][cell.coordinate].value, cell.value)
        self.assertEqual(read_snapshot(data), read_snapshot(self.original))

    def test_future_snapshot_publish_keeps_link_tab_and_extra_sheets(self):
        linked = update_workbook(self.original, feeds=self.feeds)
        events = read_snapshot(linked)
        events[0]['End'] = '20261104T070000Z'
        published = update_workbook(linked, events=events)
        self.assertEqual(read_snapshot(published), events)
        self.assertEqual(read_subscription_rows(published), subscription_rows(self.feeds))
        workbook = load_workbook(io.BytesIO(published))
        self.assertEqual(workbook['הערות']['B2'].value, '=1+2')

    def test_repeated_link_sync_does_not_duplicate_rows_or_tabs(self):
        data = update_workbook(self.original, feeds=self.feeds)
        data = update_workbook(data, feeds={'שמואל': self.feeds['שמואל']})
        self.assertEqual(read_subscription_rows(data), subscription_rows({'שמואל': self.feeds['שמואל']}))
        self.assertEqual(load_workbook(io.BytesIO(data)).sheetnames.count(LINKS_TAB), 1)

    def test_tab_reordering_does_not_confuse_links_with_duty_data(self):
        data = update_workbook(self.original, feeds=self.feeds)
        workbook = load_workbook(io.BytesIO(data))
        workbook.move_sheet(workbook[LINKS_TAB], offset=-2)
        buffer = io.BytesIO()
        workbook.save(buffer)
        self.assertEqual(read_snapshot(buffer.getvalue()), read_snapshot(self.original))

    def test_native_excel_date_is_normalized_for_readback(self):
        workbook = load_workbook(io.BytesIO(self.original))
        workbook['נתוני שיבוצים']['A2'] = datetime(2026, 11, 3)
        buffer = io.BytesIO()
        workbook.save(buffer)
        self.assertEqual(read_snapshot(buffer.getvalue())[0]['Date'], '2026-11-03')

    def test_links_refresh_preserves_notes_outside_managed_columns(self):
        data = update_workbook(self.original, feeds=self.feeds)
        workbook = load_workbook(io.BytesIO(data))
        workbook[LINKS_TAB]['E4'] = 'הערה פרטית'
        buffer = io.BytesIO()
        workbook.save(buffer)
        updated = update_workbook(buffer.getvalue(), feeds=self.feeds)
        self.assertEqual(load_workbook(io.BytesIO(updated))[LINKS_TAB]['E4'].value, 'הערה פרטית')


if __name__ == '__main__':
    unittest.main()
