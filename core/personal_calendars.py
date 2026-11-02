"""Read finalized duties and prepare personal calendar events. No scheduling."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import io
from pathlib import Path
import posixpath
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Jerusalem')
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RID = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
HEADERS = {'ת.מיון': 'ת.מיון', 'ת.מיון 1': 'ת.מיון', 'ת.מיון 2': 'ת.מיון 2',
           'כ.מיון': 'כונן מיון', 'כונן מיון': 'כונן מיון',
           'כ.שבץ': 'כונן שבץ', 'כונן שבץ': 'כונן שבץ'}
BASE_SHIFTS = {'ת.מיון', 'ת.מיון 2', 'כונן מיון'}
POLICY = {'resident_weekday_start': 16, 'resident_friday_eve_start': 13,
          'resident_next_day_end': 10, 'senior_start': 15, 'senior_next_day_end': 8,
          'senior_friday_start': 13, 'senior_friday_next_day_end': 9,
          'saturday_holiday_start': 9, 'saturday_holiday_next_day_end': 8}
EMPTY_NAMES = {'', '-', '—', 'חסר', 'לא שובץ', 'לא שובצה', 'פנוי', 'nan', 'None'}


def clean(value: object) -> str:
    return re.sub('[\u200e\u200f\u202a-\u202e\u2066-\u2069]', '', str(value or '')).strip()


def utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def local_string(value: str) -> str:
    return datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).astimezone(ZONE).strftime('%d/%m/%Y %H:%M')


def event_uid(row: dict) -> str:
    raw = row['Date'] + '|' + row['Shift'] + '|' + row['Name']
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32] + '@calendar.neuroshift'


@dataclass(frozen=True)
class WorkbookDuties:
    month: str
    records: tuple[dict, ...]
    warnings: tuple[str, ...]
    source_path: str
    source_sha256: str


def read_workbook(path: str | Path) -> WorkbookDuties:
    path = Path(path).resolve()
    if path.suffix.lower() not in {'.xlsx', '.xlsm'}:
        raise ValueError('יש לבחור קובץ Excel מסוג xlsx או xlsm.')
    data = path.read_bytes()  # One consistent read; never save/recalculate the workbook.
    with ZipFile(io.BytesIO(data)) as archive:
        wb = ET.fromstring(archive.read('xl/workbook.xml'))
        sheets = wb.find('s:sheets', NS)
        sheet = next((s for s in sheets if clean(s.get('name')) == 'תורנויות'), None)
        if sheet is None:
            raise ValueError('לא נמצא גיליון תורנויות בקובץ שנבחר.')
        rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        target = rels[sheet.get(RID)]
        target = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(t.text or '' for t in si.findall('.//s:t', NS))
                       for si in ET.fromstring(archive.read('xl/sharedStrings.xml'))]

        def value(cell):
            if cell is None:
                return ''
            if cell.find('s:f', NS) is not None:
                raise ValueError(f'תא {cell.get("r")} מכיל נוסחה. נדרש ערך שמור מפורש לתאריך ולשיבוץ.')
            v = cell.find('s:v', NS)
            raw = v.text if v is not None else ''
            if cell.get('t') == 's':
                return strings[int(raw)]
            if cell.get('t') == 'inlineStr':
                return ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
            if cell.get('t') == 'e':
                raise ValueError(f'תא {cell.get("r")} מכיל שגיאת Excel: {raw}')
            return raw

        rows = ET.fromstring(archive.read(target)).findall('.//s:sheetData/s:row', NS)
        columns = None
        date_col = None
        header_number = 0
        for row in rows[:20]:
            labels = {re.sub(r'\d', '', c.get('r')): clean(value(c))
                      for c in row if c.find('s:f', NS) is None}
            candidate = {col: HEADERS[label] for col, label in labels.items() if label in HEADERS}
            date_cols = [col for col, label in labels.items() if label == 'תאריך']
            if BASE_SHIFTS.issubset(candidate.values()) and date_cols:
                if len(set(candidate.values())) != len(candidate) or len(date_cols) != 1:
                    raise ValueError('נמצאו כותרות תורנויות או תאריך כפולות.')
                columns, date_col, header_number = candidate, date_cols[0], int(row.get('r'))
                break
        if columns is None:
            raise ValueError('לא נמצאו כותרות תאריך, ת.מיון, ת.מיון 2 וכ.מיון בגיליון תורנויות.')
        prop = wb.find('s:workbookPr', NS)
        epoch = date(1904, 1, 1) if prop is not None and prop.get('date1904') in {'1', 'true'} else date(1899, 12, 30)

        def parse_day(raw):
            raw = clean(raw)
            for fmt in ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d', '%d.%m.%Y'):
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    pass
            if re.fullmatch(r'\d+(?:\.0+)?', raw) and 20000 <= float(raw) <= 100000:
                return epoch + timedelta(days=int(float(raw)))
            return None

        records, warnings, days = [], [], []
        for row in rows:
            if int(row.get('r')) <= header_number:
                continue
            cells = {re.sub(r'\d', '', c.get('r')): c for c in row}
            day = parse_day(value(cells.get(date_col)))
            if day is None:
                continue
            days.append(day)
            for col, shift in columns.items():
                cell = cells.get(col)
                name = clean(value(cell))
                if name in EMPTY_NAMES:
                    if shift in BASE_SHIFTS:
                        warnings.append(f'{day:%d/%m/%Y} — {shift}: ללא שם, לא יפורסם אירוע.')
                    continue
                if any(c in name for c in '\n\r\t,;/=') or name.startswith('#'):
                    raise ValueError(f'שם לא חד-משמעי בתא {col}{row.get("r")}: {name}')
                records.append(dict(Date=day.isoformat(), Shift=shift, Name=name,
                                    SourceCell=cell.get('r')))
    months = {d.strftime('%Y-%m') for d in days}
    if len(months) != 1:
        raise ValueError('גיליון תורנויות חייב להכיל חודש אחד בלבד.')
    month = next(iter(months))
    year, number = map(int, month.split('-'))
    expected = [date(year, number, d) for d in range(1, monthrange(year, number)[1] + 1)]
    if sorted(days) != expected:
        raise ValueError('חסרים תאריכים או יש תאריכים כפולים בגיליון תורנויות. נדרש חודש מלא.')
    if not records:
        raise ValueError('לא נמצאו שיבוצים לפרסום בקובץ שנבחר.')
    return WorkbookDuties(month, tuple(records), tuple(warnings), str(path), hashlib.sha256(data).hexdigest())


def holiday_day_types(month: str, rows: list[list[str]]) -> dict[str, str]:
    import pandas as pd
    from core.holiday_utils import (holiday_names_from_tables, holiday_eve_names_from_tables,
                                   holiday_no_eve_dates_from_tables, is_holiday_eve_date)
    if not rows or not {'תאריך', 'סוג'}.issubset({clean(c) for c in rows[0]}):
        raise ValueError('טבלת החגים אינה כוללת את עמודות תאריך וסוג.')
    width = len(rows[0])
    frame = pd.DataFrame([(r + [''] * width)[:width] for r in rows[1:]],
                         columns=[clean(c) for c in rows[0]])
    tables = {'holidays': frame}
    holidays = holiday_names_from_tables(tables)
    eves = holiday_eve_names_from_tables(tables)
    no_eves = holiday_no_eve_dates_from_tables(tables)
    year, number = map(int, month.split('-'))
    result = {}
    for n in range(1, monthrange(year, number)[1] + 1):
        day = date(year, number, n)
        if day in holidays:
            result[day.isoformat()] = 'holiday'
        elif day.weekday() == 5:
            result[day.isoformat()] = 'saturday'
        elif is_holiday_eve_date(day, holidays, eves, no_eves):
            result[day.isoformat()] = 'eve'
        elif day.weekday() == 4:
            result[day.isoformat()] = 'friday'
    return result


def make_event(record: dict, day_types: dict[str, str], policy: dict | None = None) -> dict:
    policy = {**POLICY, **(policy or {})}
    day = date.fromisoformat(record['Date'])
    kind = day_types.get(record['Date'], 'weekday')
    if day.weekday() == 5 or kind == 'holiday':
        hour, end_hour = policy['saturday_holiday_start'], policy['saturday_holiday_next_day_end']
    elif record['Shift'] in {'כונן מיון', 'כונן שבץ'}:
        if day.weekday() == 4:
            hour, end_hour = policy['senior_friday_start'], policy['senior_friday_next_day_end']
        else:
            hour, end_hour = policy['senior_start'], policy['senior_next_day_end']
    elif record['Shift'] in {'ת.מיון', 'ת.מיון 2'}:
        hour = policy['resident_friday_eve_start'] if day.weekday() == 4 or kind == 'eve' else policy['resident_weekday_start']
        end_hour = policy['resident_next_day_end']
    else:
        raise ValueError('סוג תורנות לא מוכר: ' + record['Shift'])
    return {**record, 'Start': utc_string(datetime.combine(day, time(hour), ZONE)),
            'End': utc_string(datetime.combine(day + timedelta(days=1), time(end_hour), ZONE)),
            'DayType': kind}


def event_changes(before: list[dict], after: list[dict]) -> tuple[list[dict], dict[str, int]]:
    old, new = {event_uid(e): e for e in before}, {event_uid(e): e for e in after}
    if len(old) != len(before) or len(new) != len(after):
        raise ValueError('נמצאו שיבוצים כפולים לאותו אדם, תאריך ותפקיד.')
    changes, counts = [], dict(added=0, removed=0, changed=0, unchanged=0)
    for uid in sorted(old.keys() | new.keys(), key=lambda u: (new.get(u, old.get(u))['Date'], u)):
        a, b = old.get(uid), new.get(uid)
        kind = 'added' if a is None else 'removed' if b is None else \
               'changed' if (a['Start'], a['End']) != (b['Start'], b['End']) else 'unchanged'
        counts[kind] += 1
        changes.append(dict(kind=kind, before=a, after=b))
    return changes, counts


def escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def fold(line: str) -> str:
    parts, part = [], ''
    for char in line:
        if len((part + char).encode('utf-8')) > 75:
            parts.append(part)
            part = ' '
        part += char
    return '\r\n'.join(parts + [part])


def calendar_name(name: str, *, test_mode: bool = False) -> str:
    return ('[בדיקה] ' if test_mode else '') + f'Neuroshift - {name}'


def calendar_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', calendar_name(name)).strip(' .') + '.ics'


def render_calendar(name: str, events: list[dict], stamp: str, sequence: int, *, test_mode: bool = False) -> str:
    title = calendar_name(name, test_mode=test_mode)
    description = f'תורנויות וכוננויות של {name}, המחלקה הנוירולוגית, המרכז הרפואי שמיר.'
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//NeuroShift//Personal Calendar//HE',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-TIMEZONE:Asia/Jerusalem',
             'NAME:' + escape(title), 'X-WR-CALNAME:' + escape(title),
             'DESCRIPTION:' + escape(description), 'X-WR-CALDESC:' + escape(description)]
    for row in sorted(events, key=lambda r: (r['Date'], r['Shift'], r['Name'])):
        if row['Name'] != name:
            continue
        lines += ['BEGIN:VEVENT', 'UID:' + event_uid(row), 'DTSTAMP:' + stamp,
                  'LAST-MODIFIED:' + stamp, f'SEQUENCE:{sequence}',
                  'DTSTART:' + row['Start'], 'DTEND:' + row['End'],
                  'SUMMARY:' + escape(('[בדיקה] ' if test_mode else '') + row['Shift'] + ' - ' + name),
                  'DESCRIPTION:' + escape('לפי הסידור הסופי שפורסם ב-NeuroShift.'),
                  'STATUS:CONFIRMED', 'TRANSP:TRANSPARENT', 'END:VEVENT']
    return '\r\n'.join(fold(line) for line in lines + ['END:VCALENDAR']) + '\r\n'
