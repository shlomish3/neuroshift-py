"""Finalized-workbook/calendar tests. No roster generation or live Google calls."""
from calendar import monthrange
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from core.calendar_publisher import CalendarPublisher, StateStore, published_months
from core.personal_calendars import (event_uid, holiday_day_types, local_string,
                                    make_event, read_workbook, render_calendar,
                                    calendar_name, calendar_filename, escape)


def write_fixture(path, month='2026-12', *, replace=None, stroke=False, numeric=False):
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    sheet = ET.Element('worksheet', xmlns=ns)
    data = ET.SubElement(sheet, 'sheetData')
    def row(number, values):
        node = ET.SubElement(data, 'row', r=str(number))
        for col, value in values.items():
            cell = ET.SubElement(node, 'c', r=col + str(number))
            if isinstance(value, int):
                ET.SubElement(cell, 'v').text = str(value)
            else:
                cell.set('t', 'inlineStr')
                ET.SubElement(ET.SubElement(cell, 'is'), 't').text = value
    row(3, {'B': 'תאריך', 'D': 'ת.מיון 1', 'E': 'ת.מיון 2', 'F': 'כ.מיון',
            'G': 'כ.שבץ', 'H': 'בכירים פנויים'})
    year, number = map(int, month.split('-'))
    for day in range(1, monthrange(year, number)[1] + 1):
        d = date(year, number, day)
        values = {'B': (d - date(1899, 12, 30)).days if numeric else d.strftime('%d/%m/%Y'),
                  'D': 'אור', 'E': 'גל', 'F': 'בכיר', 'G': 'בכיר' if stroke and day == 1 else '',
                  'H': 'טקסט עזר, שאינו שיבוץ'}
        if replace:
            values.update(replace.get(day, {}))
        row(day + 3, values)
    workbook = ('<workbook xmlns="' + ns + '" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="תורנויות" sheetId="1" r:id="rId1"/></sheets></workbook>')
    relationships = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
    with ZipFile(path, 'w') as archive:
        archive.writestr('xl/workbook.xml', workbook)
        archive.writestr('xl/_rels/workbook.xml.rels', relationships)
        archive.writestr('xl/worksheets/sheet1.xml', ET.tostring(sheet))


class FakeCloud:
    def __init__(self, *, fail=None, verified=True):
        self.calls = []
        self.fail = fail
        self.verified = verified
        self.payload = {}
        self.uploaded = []
        self.synced_feeds = {}

    def check_resources(self, state):
        self.calls.append('resources')

    def upload_snapshot(self, state, events):
        self.calls.append('upload')
        self.uploaded = list(events)
        if self.fail == 'upload':
            raise RuntimeError('Upload failed')

    def deploy(self, state, payload):
        self.calls.append('deploy')
        if self.fail == 'deploy':
            raise RuntimeError('Deploy failed')
        self.payload = payload
        return state.get('version', 0) + 1, state['feed_base_url']

    def verify_feeds(self, feeds, payload, keys):
        self.calls.append('verify')
        return self.verified

    def sync_subscription_links(self, state):
        self.calls.append('links')
        if self.fail == 'links':
            raise RuntimeError('Link table sync failed')
        self.synced_feeds = dict(state['feeds'])


class CalendarPublisherTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.calendar-pilot'
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / 'final.xlsx'
        write_fixture(self.path)
        self.store = StateStore(self.folder / 'state.json')
        self.cloud = FakeCloud()
        self.store.write(dict(owner='shamir.neuroshift@gmail.com', sheet_id='test-sheet', script_id='test-script',
            deployment_id='test-deployment', version=3, sequence=3, feed_base_url='https://example.test/exec',
            worker_keys={'אור': 'permanent-key'}, feeds={'אור': 'https://example.test/exec?key=permanent-key'},
            records=[], test_mode=True))
        self.publisher = CalendarPublisher(self.store, holiday_loader=lambda: [['תאריך', 'חג', 'סוג']],
                                           cloud_factory=lambda: self.cloud)

    def test_preview_is_read_only_and_not_locked_to_november(self):
        before = self.store.path.read_bytes()
        source = self.path.read_bytes()
        preview = self.publisher.preview(self.path)
        self.assertEqual(preview.month, '2026-12')
        self.assertEqual(len(preview.events), 93)
        self.assertEqual(preview.counts['added'], 93)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.path.read_bytes(), source)
        self.assertEqual(self.cloud.calls, [])

    def test_actual_excel_dates_and_stroke_column(self):
        write_fixture(self.path, stroke=True, numeric=True)
        preview = self.publisher.preview(self.path)
        strokes = [e for e in preview.events if e['Shift'] == 'כונן שבץ']
        self.assertEqual(len(strokes), 1)
        self.assertEqual(strokes[0]['Date'], '2026-12-01')
        self.assertEqual(local_string(strokes[0]['Start']), '01/12/2026 15:00')
        self.assertEqual(local_string(strokes[0]['End']), '02/12/2026 08:00')

    def test_canonical_holidays_eves_information_and_no_eve_exception(self):
        types = holiday_day_types('2026-09', [['תאריך', 'חג', 'סוג'],
            ['21/09/2026', 'יום כיפור', 'חופש'], ['20/09/2026', 'ערב יום כיפור', 'מידע'],
            ['09/09/2026', 'מידע בלבד', 'מידע'], ['17/09/2026', 'יום חופש מיוחד', 'חופש ללא ערב חג']])
        self.assertEqual(types['2026-09-21'], 'holiday')
        self.assertEqual(types['2026-09-20'], 'eve')
        self.assertNotIn('2026-09-09', types)
        self.assertNotIn('2026-09-16', types)
        row = dict(Date='2026-09-21', Shift='כונן שבץ', Name='בכיר', SourceCell='G24')
        event = make_event(row, types)
        self.assertEqual(local_string(event['Start']), '21/09/2026 09:00')
        self.assertEqual(local_string(event['End']), '22/09/2026 08:00')

    def test_reassignment_and_cancellation_show_additions_and_removals(self):
        original = self.publisher.preview(self.path)
        state = self.store.read()
        state['calendar_months'] = {original.month: {'events': list(original.events)}}
        self.store.write(state)
        write_fixture(self.path, replace={1: {'D': 'דנה'}, 2: {'E': ''}})
        preview = self.publisher.preview(self.path)
        self.assertEqual(preview.counts['added'], 1)
        self.assertEqual(preview.counts['removed'], 2)
        self.assertEqual(len(preview.workbook.warnings), 1)
        self.assertEqual(preview.counts['unchanged'], 91)

    def test_timing_change_is_a_change_not_a_duplicate(self):
        preview = self.publisher.preview(self.path)
        state = self.store.read()
        previous = [dict(e) for e in preview.events]
        previous[0]['End'] = '20261202T090000Z'
        state['calendar_months'] = {preview.month: {'events': previous}}
        self.store.write(state)
        fresh = self.publisher.preview(self.path)
        self.assertEqual(fresh.counts['changed'], 1)
        self.assertEqual(fresh.counts['added'], 0)
        change = next(c for c in fresh.changes if c['kind'] == 'changed')
        self.assertEqual(event_uid(change['before']), event_uid(change['after']))

    def test_month_publish_preserves_other_months_and_personal_links(self):
        november = make_event(dict(Date='2026-11-03', Shift='ת.מיון', Name='אור', SourceCell='D6'), {})
        state = self.store.read()
        state['calendar_months'] = {'2026-11': {'events': [november]}}
        self.store.write(state)
        preview = self.publisher.preview(self.path)
        result = self.publisher.publish(preview)
        final = self.store.read()
        self.assertTrue(result.verified)
        self.assertEqual(final['worker_keys']['אור'], 'permanent-key')
        self.assertEqual(final['feeds']['אור'], 'https://example.test/exec?key=permanent-key')
        self.assertEqual(final['calendar_months']['2026-11']['events'], [november])
        text = self.cloud.payload['permanent-key']
        self.assertIn('UID:' + event_uid(november), text)
        self.assertEqual(text.count('BEGIN:VEVENT'), 32)
        self.assertEqual(len(self.cloud.uploaded), 94)

    def test_republishing_same_month_is_idempotent(self):
        self.publisher.publish(self.publisher.preview(self.path))
        calls = list(self.cloud.calls)
        version = self.store.read()['version']
        result = self.publisher.publish(self.publisher.preview(self.path))
        self.assertFalse(result.changed)
        self.assertEqual(self.cloud.calls, calls)
        self.assertEqual(self.store.read()['version'], version)

    def test_links_tab_sync_does_not_publish_or_change_subscriptions(self):
        before = self.store.read()
        url = self.publisher.sync_links_sheet()
        after = self.store.read()
        self.assertEqual(url, 'https://docs.google.com/spreadsheets/d/test-sheet/edit')
        self.assertEqual(self.cloud.calls, ['resources', 'links'])
        self.assertEqual(after['feeds'], before['feeds'])
        self.assertEqual(after['worker_keys'], before['worker_keys'])
        self.assertEqual(after['version'], before['version'])
        self.assertTrue(after['links_sheet_synced'])

    def test_published_new_workers_appear_in_link_table(self):
        self.publisher.publish(self.publisher.preview(self.path))
        state = self.store.read()
        self.assertEqual(self.cloud.synced_feeds, state['feeds'])
        self.assertEqual(len(self.cloud.synced_feeds), 3)
        self.assertTrue(state['links_sheet_synced'])

    def test_link_table_failure_does_not_lose_successful_publication(self):
        self.cloud.fail = 'links'
        result = self.publisher.publish(self.publisher.preview(self.path))
        self.assertTrue(result.changed)
        self.assertTrue(result.verified)
        self.assertFalse(self.store.read()['links_sheet_synced'])
        self.assertIn('גיליון הקישורים לא עודכן', result.message)

    def test_changed_source_and_stale_preview_cannot_publish(self):
        preview = self.publisher.preview(self.path)
        with self.path.open('ab') as handle:
            handle.write(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'Excel'):
            self.publisher.publish(preview)
        write_fixture(self.path)
        fresh = self.publisher.preview(self.path)
        state = self.store.read()
        state['version'] += 1
        self.store.write(state)
        with self.assertRaisesRegex(RuntimeError, 'פרסום נוסף'):
            self.publisher.publish(fresh)
        self.assertEqual(self.cloud.calls, [])

    def test_deployment_failure_does_not_advance_published_state(self):
        self.cloud.fail = 'deploy'
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Deploy failed'):
            self.publisher.publish(self.publisher.preview(self.path))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_post_publish_check_failure_keeps_actual_live_state(self):
        self.cloud.verified = False
        result = self.publisher.publish(self.publisher.preview(self.path))
        self.assertTrue(result.changed)
        self.assertFalse(result.verified)
        self.assertIn('2026-12', self.store.read()['calendar_months'])
        self.assertFalse(self.publisher.publish(self.publisher.preview(self.path)).changed)

    def test_removed_worker_keeps_a_valid_empty_feed(self):
        state = self.store.read()
        state['worker_keys']['עזב'] = 'old-key'
        self.store.write(state)
        self.publisher.publish(self.publisher.preview(self.path))
        self.assertIn('old-key', self.cloud.payload)
        self.assertEqual(self.cloud.payload['old-key'].count('BEGIN:VEVENT'), 0)

    def test_legacy_pilot_is_migrated_without_changing_event_ids(self):
        state = self.store.read()
        record = dict(Date='2026-11-14', Shift='ת.מיון 2', Name='אור', SourceCell='E17')
        state['records'] = [record]
        months = published_months(state)
        event = months['2026-11']['events'][0]
        self.assertEqual(local_string(event['End']), '15/11/2026 08:00')
        self.assertEqual(event_uid(event), event_uid(record))

    def test_release_removes_labels_and_preserves_events_links_and_months(self):
        self.publisher.publish(self.publisher.preview(self.path))
        before = self.store.read()
        result = self.publisher.release_calendars()
        after = self.store.read()
        self.assertTrue(result.changed)
        self.assertTrue(result.verified)
        self.assertFalse(after['test_mode'])
        self.assertEqual(after['worker_keys'], before['worker_keys'])
        self.assertEqual(after['feeds'], before['feeds'])
        self.assertEqual(after['calendar_months'], before['calendar_months'])
        self.assertEqual(after['deployment_id'], before['deployment_id'])
        self.assertEqual(after['sequence'], before['sequence'] + 1)
        for text in self.cloud.payload.values():
            self.assertNotIn('בדיקה', text)
            self.assertNotIn('TEST', text)
            self.assertIn('X-WR-CALNAME:', text)
            self.assertIn('TRANSP:TRANSPARENT', text)
        calls = list(self.cloud.calls)
        self.assertFalse(self.publisher.release_calendars().changed)
        self.assertEqual(self.cloud.calls, calls)

    def test_release_failure_preserves_test_state_and_existing_links(self):
        self.publisher.publish(self.publisher.preview(self.path))
        before = self.store.path.read_bytes()
        self.cloud.fail = 'deploy'
        with self.assertRaisesRegex(RuntimeError, 'Deploy failed'):
            self.publisher.release_calendars()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_future_publications_stay_without_test_labels_after_release(self):
        self.publisher.publish(self.publisher.preview(self.path))
        self.publisher.release_calendars()
        write_fixture(self.path, stroke=True)
        self.publisher.publish(self.publisher.preview(self.path))
        self.assertFalse(self.store.read()['test_mode'])
        for text in self.cloud.payload.values():
            self.assertNotIn('בדיקה', text)
            self.assertNotIn('TEST', text)

    def test_hebrew_calendar_folding_and_no_duplicate_uids(self):
        preview = self.publisher.preview(self.path)
        text = render_calendar('אור', list(preview.events), '20261102T190000Z', 4)
        self.assertNotIn('\n', text.replace('\r\n', ''))
        for line in text.split('\r\n'):
            self.assertLessEqual(len(line.encode('utf-8')), 75)
        uids = [line for line in text.split('\r\n') if line.startswith('UID:')]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertEqual(len(uids), 31)

    def test_personal_name_in_standard_and_legacy_calendar_fields(self):
        name = 'שמואל'
        title = 'Neuroshift - שמואל'
        text = render_calendar(name, [], '20261102T190000Z', 4)
        unfolded = text.replace('\r\n ', '')
        self.assertEqual(calendar_name(name), title)
        self.assertIn('NAME:' + escape(title) + '\r\n', unfolded)
        self.assertIn('X-WR-CALNAME:' + escape(title) + '\r\n', unfolded)
        self.assertIn('X-WR-CALDESC:', unfolded)
        self.assertNotIn('http', unfolded)
        self.assertEqual(calendar_filename(name), title + '.ics')
        self.assertNotIn('/', calendar_filename('שם/עם:סימנים'))
        for line in text.split('\r\n'):
            self.assertLessEqual(len(line.encode('utf-8')), 75)

    def test_name_refresh_preserves_every_duty_and_subscription(self):
        self.publisher.publish(self.publisher.preview(self.path))
        state = self.store.read()
        state['calendar_naming_version'] = 0
        self.store.write(state)
        before = self.store.read()
        result = self.publisher.refresh_calendar_names()
        after = self.store.read()
        self.assertTrue(result.verified)
        self.assertEqual(after['feeds'], before['feeds'])
        self.assertEqual(after['worker_keys'], before['worker_keys'])
        self.assertEqual(after['calendar_months'], before['calendar_months'])
        for name, key in after['worker_keys'].items():
            text = self.cloud.payload[key].replace('\r\n ', '')
            self.assertIn('NAME:' + escape(calendar_name(name, test_mode=True)), text)
        calls = list(self.cloud.calls)
        self.assertFalse(self.publisher.refresh_calendar_names().changed)
        self.assertEqual(self.cloud.calls, calls)

    def test_friday_on_call_hours_and_full_holiday_precedence(self):
        for shift in ['כונן מיון', 'כונן שבץ']:
            record = dict(Date='2026-11-06', Shift=shift, Name='בכיר', SourceCell='F9')
            event = make_event(record, {})
            self.assertEqual(local_string(event['Start']), '06/11/2026 13:00')
            self.assertEqual(local_string(event['End']), '07/11/2026 09:00')
            holiday = make_event(record, {'2026-11-06': 'holiday'})
            self.assertEqual(local_string(holiday['Start']), '06/11/2026 09:00')
            self.assertEqual(local_string(holiday['End']), '07/11/2026 08:00')
            weekday_eve = make_event({**record, 'Date': '2026-11-05'}, {'2026-11-05': 'eve'})
            self.assertEqual(local_string(weekday_eve['Start']), '05/11/2026 15:00')
            self.assertEqual(local_string(weekday_eve['End']), '06/11/2026 08:00')
        resident = make_event(dict(Date='2026-11-06', Shift='ת.מיון', Name='אור', SourceCell='D9'), {})
        self.assertEqual(local_string(resident['End']), '07/11/2026 10:00')

    def test_settings_refresh_changes_only_on_call_friday_times(self):
        self.publisher.publish(self.publisher.preview(self.path))
        state = self.store.read()
        old_events = state['calendar_months']['2026-12']['events']
        for event in old_events:
            if event['Shift'] == 'כונן מיון' and date.fromisoformat(event['Date']).weekday() == 4:
                event.update(Start=event['Date'].replace('-', '')+'T130000Z',
                             End=(date.fromisoformat(event['Date'])+timedelta(days=1)).strftime('%Y%m%d')+'T060000Z')
        state['calendar_naming_version'] = 1
        self.store.write(state)
        before = self.store.read()
        result = self.publisher.refresh_published_settings()
        after = self.store.read()
        self.assertTrue(result.verified)
        self.assertEqual(after['feeds'], before['feeds'])
        self.assertEqual(after['records'], before['records'])
        old_by_uid = {event_uid(e): e for e in old_events}
        new_events = after['calendar_months']['2026-12']['events']
        self.assertEqual(set(old_by_uid), {event_uid(e) for e in new_events})
        for event in new_events:
            previous = old_by_uid[event_uid(event)]
            if (previous['Start'], previous['End']) != (event['Start'], event['End']):
                self.assertEqual(event['Shift'], 'כונן מיון')
                self.assertEqual(date.fromisoformat(event['Date']).weekday(), 4)
        self.assertFalse(self.publisher.refresh_published_settings().changed)


if __name__ == '__main__':
    unittest.main()
