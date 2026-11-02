"""Preview-first publishing to the existing personal calendars."""
from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
import secrets
from urllib.parse import urlencode

from core.personal_calendars import (WorkbookDuties, event_changes, holiday_day_types,
                                    make_event, read_workbook, render_calendar, calendar_filename, POLICY)

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / '.calendar-pilot' / 'state.json'
OWNER = 'shamir.neuroshift@gmail.com'
TABLE_FIELDS = ['Date', 'Shift', 'Name', 'SourceCell', 'Start', 'End', 'DayType']
NAMING_VERSION = 2
HANDLER = '''function doGet(e) {
  var key = e && e.parameter && e.parameter.key;
  if (typeof key !== 'string' || !Object.prototype.hasOwnProperty.call(CALENDAR_FEEDS, key)) {
    return ContentService.createTextOutput('Calendar unavailable.');
  }
  var output = ContentService.createTextOutput(CALENDAR_FEEDS[key]).setMimeType(ContentService.MimeType.ICAL);
  var filename = typeof CALENDAR_FILENAMES !== 'undefined' && CALENDAR_FILENAMES[key];
  if (typeof filename === 'string') {
    try { return output.downloadAsFile(filename); } catch (error) { return output; }
  }
  return output;
}
'''


def state_token(state: dict) -> str:
    fields = ('records', 'calendar_months', 'worker_keys', 'deployment_id', 'version', 'sequence', 'test_mode', 'timing_policy', 'holiday_snapshot', 'calendar_naming_version')
    return hashlib.sha256(json.dumps({k: state.get(k) for k in fields},
                          ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def published_months(state: dict) -> dict[str, dict]:
    if 'calendar_months' in state:
        return json.loads(json.dumps(state['calendar_months']))
    # Read the existing pilot without replacing its IDs, keys or November data.
    months = {}
    types = state.get('holiday_snapshot', {}).get('day_types', {})
    for record in state.get('records', []):
        month = record['Date'][:7]
        entry = months.setdefault(month, {'events': [], 'source_path': state.get('source_path', '')})
        entry['events'].append(make_event(record, types, state.get('timing_policy')))
    return months


class StateStore:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding='utf-8'))

    def write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    @contextmanager
    def publishing_lock(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a+b') as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError('פרסום אחר עדיין פועל. יש להמתין לסיומו.') from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def load_holiday_rows() -> list[list[str]]:
    """Read only holiday rules; never load or execute the scheduling engine."""
    import gspread
    from google.oauth2.service_account import Credentials
    from core import config
    credentials = Credentials.from_service_account_file(config.CRED_FILE,
                   scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    client = gspread.authorize(credentials)
    client.set_timeout(15)
    return client.open_by_key(config.SHEET_ID).worksheet('חגים').get_all_values()


@dataclass(frozen=True)
class CalendarPreview:
    workbook: WorkbookDuties
    events: tuple[dict, ...]
    changes: tuple[dict, ...]
    counts: dict[str, int]
    day_types: dict[str, str]
    baseline_token: str

    @property
    def month(self) -> str:
        return self.workbook.month


@dataclass(frozen=True)
class PublishResult:
    month: str
    changed: bool
    verified: bool
    worker_count: int
    message: str


class GoogleCalendarCloud:
    """The account is checked on every publish; no credentials are displayed."""
    def __init__(self):
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        path = Path.home() / '.clasprc.json'
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))['tokens']['default']
            credentials = Credentials.from_authorized_user_info(saved)
            credentials.refresh(Request())
        except Exception as exc:
            raise RuntimeError('לא ניתן להתחבר לחשבון NeuroShift. יש לחדש את החיבור ל-shamir.neuroshift@gmail.com.') from exc
        self.client = AuthorizedSession(credentials)
        who = self._json('GET', 'https://www.googleapis.com/drive/v3/about',
                         params={'fields': 'user(emailAddress)'})
        if who['user']['emailAddress'] != OWNER:
            raise RuntimeError('החשבון המחובר אינו shamir.neuroshift@gmail.com. הפרסום נעצר.')

    def _json(self, method: str, url: str, **kwargs):
        response = self.client.request(method, url, timeout=30, **kwargs)
        if not response.ok:
            try:
                error = response.json().get('error', {})
                text = error.get('message', '') if isinstance(error, dict) else str(error)
            except ValueError:
                text = 'לא התקבלה תשובה תקינה.'
            raise RuntimeError(f'Google ({response.status_code}): {text.split("Help Token:")[0]}')
        return response.json()

    def check_resources(self, state: dict) -> None:
        meta = self._json('GET', 'https://www.googleapis.com/drive/v3/files/' + state['sheet_id'],
               params={'fields': 'name,mimeType,owners(emailAddress),permissions(type,role,emailAddress)'})
        if (not meta.get('name', '').startswith('NeuroShift Calendar TEST')
                or meta.get('mimeType') != 'application/vnd.google-apps.spreadsheet'
                or [o.get('emailAddress') for o in meta.get('owners', [])] != [OWNER]
                or any(p.get('type') != 'user' or p.get('emailAddress') != OWNER
                       for p in meta.get('permissions', []))):
            raise RuntimeError('גיליון היומנים אינו הגיליון הפרטי הצפוי. הפרסום נעצר.')

    def upload_snapshot(self, state: dict, events: list[dict]) -> None:
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=TABLE_FIELDS, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(events)
        boundary = 'neuroshift_' + secrets.token_hex(12)
        body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'
                + json.dumps({'mimeType': 'application/vnd.google-apps.spreadsheet'})
                + f'\r\n--{boundary}\r\nContent-Type: text/csv; charset=UTF-8\r\n\r\n'
                + output.getvalue() + f'\r\n--{boundary}--\r\n').encode('utf-8')
        self._json('PATCH', 'https://www.googleapis.com/upload/drive/v3/files/' + state['sheet_id'],
                   params={'uploadType': 'multipart', 'fields': 'id'},
                   headers={'Content-Type': 'multipart/related; boundary=' + boundary}, data=body)
        response = self.client.get('https://www.googleapis.com/drive/v3/files/' + state['sheet_id'] + '/export',
                                   params={'mimeType': 'text/csv'}, timeout=30)
        if not response.ok:
            raise RuntimeError('לא ניתן לבדוק את השיבוצים לאחר העלאתם. היומנים לא פורסמו.')
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
        if rows != events:
            raise RuntimeError('השיבוצים שנקראו מ-Google אינם זהים לתצוגה המקדימה. היומנים לא פורסמו.')

    def deploy(self, state: dict, payload: dict[str, str]) -> tuple[int, str]:
        source = HANDLER + '\nvar CALENDAR_FEEDS = ' + json.dumps(payload, ensure_ascii=False) + ';\n'
        filenames = {key: calendar_filename(name) for name, key in state['worker_keys'].items()}
        source += 'var CALENDAR_FILENAMES = ' + json.dumps(filenames, ensure_ascii=False) + ';\n'
        manifest = {'timeZone': 'Asia/Jerusalem', 'runtimeVersion': 'V8', 'oauthScopes': [],
                    'exceptionLogging': 'STACKDRIVER',
                    'webapp': {'access': 'ANYONE_ANONYMOUS', 'executeAs': 'USER_DEPLOYING'}}
        base = 'https://script.googleapis.com/v1/projects/' + state['script_id']
        self._json('PUT', base + '/content', json={'files': [
            {'name': 'appsscript', 'type': 'JSON', 'source': json.dumps(manifest)},
            {'name': 'Code', 'type': 'SERVER_JS', 'source': source}]})
        version = self._json('POST', base + '/versions',
                            json={'description': 'Personal calendars published from finalized workbook preview'})['versionNumber']
        deployment = self._json('PUT', base + '/deployments/' + state['deployment_id'],
                     json={'deploymentConfig': {'versionNumber': version, 'manifestFileName': 'appsscript',
                           'description': 'NeuroShift personal calendar preview and publishing'}})
        entry = next(e['webApp'] for e in deployment.get('entryPoints', []) if 'webApp' in e)
        if entry['entryPointConfig'] != {'access': 'ANYONE_ANONYMOUS', 'executeAs': 'USER_DEPLOYING'}:
            raise RuntimeError('תצורת פרסום היומנים אינה תואמת לפרויקט היומנים.')
        if state.get('feed_base_url') and entry['url'] != state['feed_base_url']:
            raise RuntimeError('כתובת היומנים השתנתה באופן לא צפוי.')
        return version, entry['url']

    def verify_feeds(self, feeds: dict[str, str], payload: dict[str, str], keys: dict[str, str]) -> bool:
        import requests
        def check(item):
            name, url = item
            try:
                response = requests.get(url, timeout=30)
                return (response.status_code == 200
                        and 'text/calendar' in response.headers.get('Content-Type', '').lower()
                        and response.content == payload[keys[name]].encode('utf-8'))
            except requests.RequestException:
                return False
        with ThreadPoolExecutor(max_workers=3) as pool:
            return all(list(pool.map(check, feeds.items())))


class CalendarPublisher:
    def __init__(self, store: StateStore | None = None, *, holiday_loader=load_holiday_rows,
                 cloud_factory=GoogleCalendarCloud):
        self.store = store or StateStore()
        self.holiday_loader = holiday_loader
        self.cloud_factory = cloud_factory

    def preview(self, path: str | Path) -> CalendarPreview:
        workbook = read_workbook(path)
        state = self.store.read()
        try:
            types = holiday_day_types(workbook.month, self.holiday_loader())
        except Exception as exc:
            raise RuntimeError('לא ניתן לקרוא את כללי החגים לצורך חישוב שעות. יש לבדוק את החיבור ולנסות שוב.\n' + str(exc)) from exc
        events = [make_event(r, types) for r in workbook.records]
        before = published_months(state).get(workbook.month, {}).get('events', [])
        changes, counts = event_changes(before, events)
        return CalendarPreview(workbook, tuple(events), tuple(changes), counts, types, state_token(state))

    def publish(self, preview: CalendarPreview, progress=lambda text: None) -> PublishResult:
        with self.store.publishing_lock():
            state = self.store.read()
            if state_token(state) != preview.baseline_token:
                raise RuntimeError('בוצע פרסום נוסף מאז התצוגה המקדימה. יש להכין תצוגה חדשה.')
            if hashlib.sha256(Path(preview.workbook.source_path).read_bytes()).hexdigest() != preview.workbook.source_sha256:
                raise RuntimeError('קובץ ה-Excel השתנה מאז התצוגה המקדימה. יש להכין תצוגה חדשה.')
            names = {e['Name'] for e in preview.events}
            if (not any(preview.counts[k] for k in ('added', 'removed', 'changed'))
                    and state.get('calendar_naming_version') == NAMING_VERSION):
                return PublishResult(preview.month, False, True, len(names), 'אין שינויים לפרסום. היומנים כבר תואמים לסידור שנבחר.')
            months = published_months(state)
            months[preview.month] = {'events': list(preview.events), 'source_path': preview.workbook.source_path,
                                     'source_sha256': preview.workbook.source_sha256, 'day_types': preview.day_types}
            state.update(source_path=preview.workbook.source_path, source_sha256=preview.workbook.source_sha256)
            message = f'פורסם חודש {preview.month}: {len(preview.events)} שיבוצים עבור {len(names)} עובדים. הקישורים הקיימים נשמרו.'
            return self._publish_months(state, months, month=preview.month,
                    worker_count=len(names), message=message,
                    test_mode=state.get('test_mode', False), progress=progress)

    def release_calendars(self, progress=lambda text: None) -> PublishResult:
        """Remove test labels from already published duties without reading a roster."""
        with self.store.publishing_lock():
            state = self.store.read()
            months = published_months(state)
            if not months:
                raise RuntimeError('לא נמצאו שיבוצים שפורסמו ביומנים.')
            count = len(state.get('worker_keys', {}))
            if state.get('test_mode', True) is False:
                return PublishResult('all', False, state.get('last_publish_verified', False), count,
                                     'היומנים כבר מפורסמים ללא תוויות בדיקה.')
            return self._publish_months(state, months, month='all', worker_count=count,
                    message='היומנים פורסמו ללא תוויות בדיקה. כל קישורי המינוי הקיימים נשמרו.',
                    test_mode=False, progress=progress)

    def refresh_calendar_names(self, progress=lambda text: None) -> PublishResult:
        """Republish calendar metadata without reopening or changing any duties."""
        with self.store.publishing_lock():
            state = self.store.read()
            months = published_months(state)
            if not months:
                raise RuntimeError('לא נמצאו יומנים שפורסמו.')
            count = len(state.get('worker_keys', {}))
            if state.get('calendar_naming_version') == NAMING_VERSION:
                return PublishResult('all', False, state.get('last_publish_verified', False), count,
                                     'שמות היומנים כבר מעודכנים.')
            return self._publish_months(state, months, month='all', worker_count=count,
                    message='שמות היומנים האישיים עודכנו. קישורי המינוי והשיבוצים נשמרו.',
                    test_mode=state.get('test_mode', False), progress=progress)

    def refresh_published_settings(self, progress=lambda text: None) -> PublishResult:
        """Apply confirmed naming/hour rules to saved duties, never reschedule."""
        with self.store.publishing_lock():
            state = self.store.read()
            months = published_months(state)
            if not months:
                raise RuntimeError('לא נמצאו יומנים שפורסמו.')
            changed_hours = 0
            for entry in months.values():
                old = entry['events']
                types = {e['Date']: e.get('DayType', 'weekday') for e in old}
                types.update(entry.get('day_types', {}))
                events = [make_event({k: e[k] for k in ('Date', 'Shift', 'Name', 'SourceCell')}, types)
                          for e in old]
                _, counts = event_changes(old, events)
                changed_hours += counts['changed']
                entry['events'] = events
            count = len(state.get('worker_keys', {}))
            if not changed_hours and state.get('calendar_naming_version') == NAMING_VERSION:
                return PublishResult('all', False, state.get('last_publish_verified', False), count,
                                     'שמות היומנים ושעות התורנויות כבר מעודכנים.')
            return self._publish_months(state, months, month='all', worker_count=count,
                    message=f'שמות היומנים עודכנו. שעות עודכנו ב-{changed_hours} שיבוצים, ללא שינוי בתאריכים או בעובדים.',
                    test_mode=state.get('test_mode', False), progress=progress)

    def _publish_months(self, state: dict, months: dict, *, month: str, worker_count: int,
                        message: str, test_mode: bool, progress) -> PublishResult:
        if not all(state.get(k) for k in ('sheet_id', 'script_id', 'deployment_id')):
            raise RuntimeError('לא נמצא חיבור לפרויקט היומנים. יש להשלים את הגדרת חשבון NeuroShift.')
        progress('בודק את חשבון היומנים')
        cloud = self.cloud_factory()
        cloud.check_resources(state)
        events = [e for month_key in sorted(months) for e in months[month_key]['events']]
        keys = dict(state.get('worker_keys', {}))
        for name in {e['Name'] for e in events}:
            keys.setdefault(name, secrets.token_urlsafe(32))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        sequence = int(state.get('sequence', 0)) + 1
        payload = {key: render_calendar(name, events, stamp, sequence, test_mode=test_mode)
                   for name, key in keys.items()}
        progress('מעלה ובודק את השיבוצים')
        cloud.upload_snapshot(state, events)
        progress('מפרסם את היומנים האישיים')
        version, base_url = cloud.deploy({**state, 'worker_keys': keys}, payload)
        state.update(calendar_months=months, worker_keys=keys, version=version,
                     feed_base_url=base_url, sequence=sequence, published_stamp=stamp,
                     records=[{k: e[k] for k in ('Date', 'Shift', 'Name', 'SourceCell')} for e in events],
                     counts=dict(Counter(e['Name'] for e in events)), test_mode=test_mode, schema_version=2,
                     feeds={name: base_url + '?' + urlencode({'key': key}) for name, key in keys.items()},
                     last_publish_verified=False, calendar_naming_version=NAMING_VERSION,
                     timing_policy=dict(POLICY))
        # Persist the actual live deployment before optional anonymous checks.
        self.store.write(state)
        progress('בודק את קישורי היומנים')
        try:
            verified = cloud.verify_feeds(state['feeds'], payload, keys)
        except Exception:
            verified = False
        state['last_publish_verified'] = verified
        self.store.write(state)
        if not verified:
            message += '\nהפרסום הסתיים, אך בדיקת הקישורים לא הושלמה. אין צורך לפרסם שוב.'
        message += '\nהיומנים באפליקציות יתעדכנו לפי זמני הרענון שלהן.'
        return PublishResult(month, True, verified, worker_count, message)
