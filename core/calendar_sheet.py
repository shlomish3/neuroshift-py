"""Maintain the app-owned calendar tables without discarding other Sheet tabs."""
from __future__ import annotations

from datetime import date, datetime
import io

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from core.personal_calendars import calendar_name

XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
TABLE_FIELDS = ['Date', 'Shift', 'Name', 'SourceCell', 'Start', 'End', 'DayType']
LINKS_TAB = 'קישורי יומנים'
LINK_HEADERS = ['שם העובד', 'שם היומן', 'קישור להרשמה']


def subscription_rows(feeds: dict[str, str]) -> list[list[str]]:
    return [[name, calendar_name(name), feeds[name]] for name in sorted(feeds)]


def _data_sheet(workbook):
    for sheet in workbook:
        if [sheet.cell(1, col).value for col in range(1, 5)] == TABLE_FIELDS[:4]:
            return sheet
    raise ValueError('לא נמצא גיליון נתוני היומנים הצפוי. לא בוצע עדכון.')


def _text(value):
    if isinstance(value, (date, datetime)):
        return value.strftime('%Y-%m-%d')
    return '' if value is None else str(value)


def read_snapshot(data: bytes) -> list[dict[str, str]]:
    workbook = load_workbook(io.BytesIO(data), data_only=False)
    sheet = _data_sheet(workbook)
    if [sheet.cell(1, col).value for col in range(1, 8)] != TABLE_FIELDS:
        raise ValueError('כותרות גיליון נתוני היומנים אינן תואמות לנתונים שפורסמו.')
    rows = []
    for values in sheet.iter_rows(min_row=2, max_col=7, values_only=True):
        if any(value is not None for value in values):
            rows.append(dict(zip(TABLE_FIELDS, map(_text, values))))
    return rows


def read_subscription_rows(data: bytes) -> list[list[str]]:
    workbook = load_workbook(io.BytesIO(data), data_only=False)
    if LINKS_TAB not in workbook.sheetnames:
        raise ValueError('לא נמצא גיליון קישורי יומנים לאחר העדכון.')
    sheet = workbook[LINKS_TAB]
    if [sheet.cell(1, col).value for col in range(1, 4)] != LINK_HEADERS:
        raise ValueError('כותרות גיליון הקישורים אינן תואמות לעדכון.')
    return [[_text(value) for value in row] for row in
            sheet.iter_rows(min_row=2, max_col=3, values_only=True)
            if any(value is not None for value in row)]


def update_workbook(data: bytes, *, events: list[dict] | None = None,
                    feeds: dict[str, str] | None = None) -> bytes:
    """Edit only the app's data/link ranges; retain other sheets and cells."""
    workbook = load_workbook(io.BytesIO(data), data_only=False)
    if events is not None:
        sheet = _data_sheet(workbook)
        for row in sheet.iter_rows(min_row=1, max_row=max(sheet.max_row, len(events) + 1), max_col=7):
            for cell in row:
                cell.value = None
        for number, values in enumerate([TABLE_FIELDS, *[[e[key] for key in TABLE_FIELDS] for e in events]], 1):
            for column, value in enumerate(values, 1):
                cell = sheet.cell(number, column, value)
                cell.number_format = '@'
    if feeds is not None:
        sheet = workbook[LINKS_TAB] if LINKS_TAB in workbook.sheetnames else workbook.create_sheet(LINKS_TAB)
        rows = subscription_rows(feeds)
        for row in sheet.iter_rows(min_row=1, max_row=max(sheet.max_row, len(rows) + 1), max_col=3):
            for cell in row:
                cell.value = None
                cell.hyperlink = None
        for number, values in enumerate([LINK_HEADERS, *rows], 1):
            for column, value in enumerate(values, 1):
                cell = sheet.cell(number, column, value)
                cell.number_format = '@'
                cell.font = Font(name='Arial', size=11, bold=number == 1,
                                 color='FFFFFF' if number == 1 else '0563C1' if column == 3 else '17324D',
                                 underline='single' if column == 3 and number > 1 else None)
                cell.fill = PatternFill('solid', fgColor='1F6F8B' if number == 1 else 'F2F6FA' if number % 2 == 0 else 'FFFFFF')
                cell.alignment = Alignment(horizontal='left' if column == 3 else 'right',
                                           vertical='center', wrap_text=True)
                if column == 3 and number > 1:
                    cell.hyperlink = value
            sheet.row_dimensions[number].height = 30 if number == 1 else 34
        sheet.column_dimensions['A'].width = 20
        sheet.column_dimensions['B'].width = 32
        sheet.column_dimensions['C'].width = 110
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = f'A1:C{len(rows) + 1}'
        sheet.sheet_view.rightToLeft = True
        workbook.active = workbook.index(sheet)
    result = io.BytesIO()
    workbook.save(result)
    return result.getvalue()
