"""GUI action checks with a fake publisher/cloud; no generation or Google access."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from core.calendar_publisher import CalendarPublisher, StateStore
from gui.app import NeuroShiftApp
from gui.calendar_window import CalendarWindow
from core.personal_calendars import calendar_name
from test_calendar_publisher import FakeCloud, write_fixture


class CalendarWindowTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.calendar-pilot'
        base.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'final.xlsx'
        write_fixture(self.path)
        self.store = StateStore(Path(self.folder.name) / 'state.json')
        self.store.write(dict(source_path=str(self.path), records=[], version=3, sequence=3,
            sheet_id='test-sheet', script_id='test-script', deployment_id='test-deployment',
            feed_base_url='https://example.test/exec', worker_keys={'אור': 'permanent'},
            feeds={'אור': 'https://example.test/exec?key=permanent'}))
        self.cloud = FakeCloud()
        self.publisher = CalendarPublisher(self.store, holiday_loader=lambda: [['תאריך', 'חג', 'סוג']],
                                           cloud_factory=lambda: self.cloud)
        self.logo_patch = patch.object(NeuroShiftApp, '_load_logo_async')
        self.logo_patch.start()
        self.addCleanup(self.logo_patch.stop)
        self.errors = []
        self.message_patch = patch('gui.calendar_window.messagebox.showerror',
                                    side_effect=lambda *args, **kwargs: self.errors.append(args))
        self.message_patch.start()
        self.addCleanup(self.message_patch.stop)
        self.app = NeuroShiftApp()
        self.app.withdraw()
        self.window = CalendarWindow(self.app, self.publisher)
        self.window.withdraw()
        self.app._calendar_window = self.window
        self.addCleanup(self.close)

    def close(self):
        self.app.update_idletasks()
        self.window.destroy()
        self.app.update_idletasks()
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()

    def wait(self):
        deadline = time.monotonic() + 5
        while self.window._busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.app.update()
        self.assertFalse(self.window._busy)

    def test_existing_links_visible_before_preview_and_singleton_window(self):
        self.assertFalse(hasattr(self.app, 'optimize_file_var'))
        self.assertFalse(hasattr(self.app, '_optimize_roster'))
        self.assertFalse(hasattr(self.app, '_browse_optimize_file'))
        self.assertIsNone(self.window._preview)
        self.assertEqual(len(self.window.links_tree.get_children()), 1)
        self.window.links_tree.selection_set('0')
        self.window._select_link()
        self.assertEqual(self.window.url_var.get(), 'https://example.test/exec?key=permanent')
        self.assertEqual(self.window.calendar_name_var.get(), calendar_name('אור'))
        with patch.object(self.window, 'clipboard_clear'), patch.object(self.window, 'clipboard_append') as copy:
            self.window._copy_calendar_name()
            copy.assert_called_once_with(calendar_name('אור'))
        self.app._open_calendars()
        self.assertIs(self.app._calendar_window, self.window)
        self.assertEqual(self.cloud.calls, [])

    def test_links_sheet_button_syncs_without_preview_or_publication(self):
        before = self.store.read()
        self.window.sync_links_button.invoke()
        self.wait()
        self.assertFalse(self.errors)
        self.assertEqual(self.cloud.calls, ['resources', 'links'])
        self.assertEqual(self.cloud.synced_feeds, before['feeds'])
        self.assertEqual(self.store.read()['version'], before['version'])
        self.assertTrue(self.store.read()['links_sheet_synced'])
        self.assertIsNone(self.window._preview)
        self.assertIn('גיליון קישורי יומנים עודכן', self.window.status_var.get())

    def test_select_preview_filter_and_publish(self):
        self.assertIn('disabled', self.window.publish_button.state())
        self.window._preview_file()
        self.wait()
        self.assertFalse(self.errors)
        self.assertEqual(len(self.window.tree.get_children()), 93)
        self.assertNotIn('disabled', self.window.publish_button.state())
        self.window.worker_var.set('אור')
        self.window._render_preview()
        self.assertEqual(len(self.window.tree.get_children()), 31)
        self.window._publish()
        self.wait()
        self.assertFalse(self.errors)
        self.assertIn('deploy', self.cloud.calls)
        self.assertIn('disabled', self.window.publish_button.state())
        self.assertEqual(len(self.window.links_tree.get_children()), 3)
        self.assertIn('פורסם חודש 2026-12', self.window.status_var.get())

    def test_file_change_invalidates_preview_and_stale_source_requires_new_preview(self):
        self.window._preview_file()
        self.wait()
        self.window.file_var.set(str(self.path))
        self.assertIsNone(self.window._preview)
        self.assertIn('disabled', self.window.publish_button.state())
        self.window._preview_file()
        self.wait()
        with self.path.open('ab') as handle:
            handle.write(b'external resave')
        self.window._publish()
        self.wait()
        self.assertEqual(len(self.errors), 1)
        self.assertIn('disabled', self.window.publish_button.state())
        self.assertEqual(self.cloud.calls, [])


if __name__ == '__main__':
    unittest.main()
