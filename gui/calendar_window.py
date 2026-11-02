"""Workbook selection, review and explicit personal-calendar publishing."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from core.calendar_publisher import CalendarPublisher, CalendarPreview, OWNER, published_months
from core.personal_calendars import local_string, calendar_name

CHANGE_LABELS = {'added': 'חדש', 'removed': 'הוסר', 'changed': 'שעות עודכנו', 'unchanged': 'ללא שינוי'}


class CalendarWindow(tk.Toplevel):
    def __init__(self, master, publisher: CalendarPublisher | None = None):
        super().__init__(master)
        self.publisher = publisher or CalendarPublisher()
        self.title('NeuroShift — יומנים אישיים')
        self.geometry('1080x740')
        self.minsize(940, 660)
        self.configure(background='#F5F7FA')
        self._queue = queue.Queue()
        self._busy = False
        self._preview: CalendarPreview | None = None
        self._links = {}
        self._after_id = None
        try:
            state = self.publisher.store.read()
        except Exception:
            state = {}
        self.file_var = tk.StringVar(value=state.get('source_path', ''))
        self.status_var = tk.StringVar(value='בחרו סידור סופי והכינו תצוגה מקדימה.')
        self.summary_var = tk.StringVar(value='השעות והשיבוצים יוצגו כאן לפני הפרסום.')
        self.warning_var = tk.StringVar()
        self.worker_var = tk.StringVar(value='כל העובדים')
        self.change_var = tk.StringVar(value='כל השיבוצים')
        self.url_var = tk.StringVar()
        self.calendar_name_var = tk.StringVar()
        self._build(state)
        self.file_var.trace_add('write', self._invalidate_preview)
        self.protocol('WM_DELETE_WINDOW', self._close)
        self._refresh_links()
        self._after_id = self.after(100, self._drain)

    def _build(self, state):
        root = ttk.Frame(self, padding=16, style='Root.TFrame')
        root.pack(fill='both', expand=True)
        heading = ttk.Frame(root, padding=12, style='Header.TFrame')
        heading.pack(fill='x')
        ttk.Label(heading, text='יומנים אישיים', style='Title.TLabel', anchor='e').pack(side='right')
        ttk.Label(root, text=f'חשבון הפרסום: {OWNER}    |    יומנים אישיים לעובדים',
                  anchor='e').pack(fill='x', pady=(8, 10))

        selection = ttk.LabelFrame(root, text='בחירת הסידור הסופי', padding=12, labelanchor='ne')
        selection.pack(fill='x')
        selection.columnconfigure(0, weight=1)
        self.file_entry = ttk.Entry(selection, textvariable=self.file_var)
        self.file_entry.grid(row=0, column=0, sticky='ew', padx=(0, 8))
        self.browse_button = ttk.Button(selection, text='בחירת קובץ', command=self._browse)
        self.browse_button.grid(row=0, column=1, padx=(0, 8))
        self.preview_button = ttk.Button(selection, text='תצוגה מקדימה', command=self._preview_file, style='Accent.TButton')
        self.preview_button.grid(row=0, column=2)

        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill='both', expand=True, pady=(12, 8))
        preview_tab = ttk.Frame(self.tabs, padding=10)
        links_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(preview_tab, text='תצוגה מקדימה')
        self.tabs.add(links_tab, text='יומנים וקישורים')
        ttk.Label(preview_tab, textvariable=self.summary_var, anchor='e').pack(fill='x', pady=(0, 6))
        ttk.Label(preview_tab, textvariable=self.warning_var, anchor='e', foreground='#945900', wraplength=960).pack(fill='x')
        filters = ttk.Frame(preview_tab)
        filters.pack(fill='x', pady=8)
        self.worker_filter = ttk.Combobox(filters, textvariable=self.worker_var, state='readonly', width=22,
                                          values=['כל העובדים'])
        self.worker_filter.pack(side='right', padx=(8, 0))
        self.change_filter = ttk.Combobox(filters, textvariable=self.change_var, state='readonly', width=22,
                                          values=['כל השיבוצים', 'שינויים בלבד'])
        self.change_filter.pack(side='right')
        self.worker_filter.bind('<<ComboboxSelected>>', lambda _: self._render_preview())
        self.change_filter.bind('<<ComboboxSelected>>', lambda _: self._render_preview())
        self.tree = self._table(preview_tab, [
            ('end', 'סיום', 150), ('start', 'התחלה', 150), ('worker', 'עובד/ת', 110),
            ('shift', 'תפקיד', 120), ('date', 'תאריך', 95), ('change', 'שינוי', 110)])
        self.tree.bind('<<TreeviewSelect>>', self._show_previous)
        self.previous_var = tk.StringVar(value='בחירת שורה תציג את השעות הקודמות, אם היו.')
        ttk.Label(preview_tab, textvariable=self.previous_var, anchor='e').pack(fill='x', pady=(8, 0))

        ttk.Label(links_tab, text='הקישורים הקיימים זמינים גם לפני בחירת קובץ. כל עובד מצטרף ליומן שלו פעם אחת.',
                  anchor='e', wraplength=960).pack(fill='x', pady=(0, 10))
        resources = ttk.Frame(links_tab)
        resources.pack(fill='x', pady=(0, 10))
        ttk.Button(resources, text='פתח את גיליון היומנים', command=lambda: self._open_resource('sheet')).pack(side='right', padx=(8, 0))
        ttk.Button(resources, text='פתח את פרויקט היומנים', command=lambda: self._open_resource('script')).pack(side='right', padx=(8, 0))
        ttk.Button(resources, text='הוראות הצטרפות', command=self._instructions).pack(side='right')
        self.sync_links_button = ttk.Button(resources, text='עדכון גיליון הקישורים',
            command=lambda: self._start('links_synced', self.publisher.sync_links_sheet, 'מעדכן את גיליון הקישורים…'))
        self.sync_links_button.pack(side='right', padx=(8, 0))
        self.links_tree = self._table(links_tab, [('count', 'שיבוצים שפורסמו', 160), ('worker', 'עובד/ת', 280)])
        self.links_tree.bind('<<TreeviewSelect>>', self._select_link)
        link_row = ttk.Frame(links_tab)
        link_row.pack(fill='x', pady=(10, 0))
        self.link_entry = ttk.Entry(link_row, textvariable=self.url_var, state='readonly')
        self.link_entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
        ttk.Button(link_row, text='העתקת קישור', command=self._copy_link).pack(side='right')
        name_row = ttk.Frame(links_tab)
        name_row.pack(fill='x', pady=(6, 0))
        ttk.Entry(name_row, textvariable=self.calendar_name_var, state='readonly', justify='right').pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        ttk.Button(name_row, text='העתקת שם היומן', command=self._copy_calendar_name).pack(side='right')

        footer = ttk.Frame(root)
        footer.pack(fill='x')
        self.publish_button = ttk.Button(footer, text='פרסום היומנים', state='disabled',
                                         command=self._publish, style='Accent.TButton')
        self.publish_button.pack(side='right', padx=(12, 0))
        ttk.Label(footer, text='הפרסום מעדכן את החודש שנבחר ושומר חודשים אחרים. העדכון באפליקציות אינו מיידי.',
                  anchor='e', wraplength=750).pack(side='right', fill='x', expand=True)
        ttk.Label(root, textvariable=self.status_var, anchor='e', wraplength=1000).pack(fill='x', pady=(8, 0))

    def _table(self, parent, columns):
        box = ttk.Frame(parent)
        box.pack(fill='both', expand=True)
        tree = ttk.Treeview(box, columns=[c[0] for c in columns], show='headings', selectmode='browse', height=10)
        for key, label, width in columns:
            tree.heading(key, text=label, anchor='e')
            tree.column(key, width=width, minwidth=70, anchor='e')
        vertical = ttk.Scrollbar(box, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(box, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        return tree

    def _browse(self):
        current = Path(self.file_var.get()).parent if self.file_var.get() else Path.home()
        path = filedialog.askopenfilename(parent=self, title='בחירת סידור סופי לפרסום ביומנים',
                   initialdir=str(current), filetypes=[('קבצי Excel', '*.xlsx *.xlsm')])
        if path:
            self.file_var.set(path)

    def _invalidate_preview(self, *_):
        self._preview = None
        self.publish_button.configure(state='disabled')
        self.tree.delete(*self.tree.get_children())
        self.summary_var.set('יש להכין תצוגה מקדימה לקובץ שנבחר.')
        self.warning_var.set('')

    def _preview_file(self):
        path = self.file_var.get().strip()
        if not path:
            messagebox.showerror('יומנים אישיים', 'יש לבחור קובץ סידור סופי.', parent=self)
            return
        self._preview = None
        self.tree.delete(*self.tree.get_children())
        self.summary_var.set('קורא את הסידור ואת כללי החגים…')
        self.warning_var.set('')
        self._start('preview', lambda: self.publisher.preview(path), 'קורא את הסידור ואת כללי החגים…')

    def _start(self, kind, task, message):
        if self._busy:
            return
        self._busy = True
        self._set_controls()
        self.status_var.set(message)
        def worker():
            try:
                self._queue.put((kind, task()))
            except Exception as exc:
                self._queue.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _set_controls(self):
        for widget in (self.browse_button, self.preview_button, self.file_entry, self.sync_links_button):
            widget.configure(state='disabled' if self._busy else 'normal')
        self.publish_button.configure(state='normal' if self._preview and not self._busy else 'disabled')

    def _publish(self):
        if self._preview is None:
            return
        preview = self._preview
        self._start('published', lambda: self.publisher.publish(preview,
                    progress=lambda text: self._queue.put(('progress', text))), 'מפרסם את התצוגה שאושרה…')

    def _drain(self):
        try:
            while True:
                kind, value = self._queue.get_nowait()
                if kind == 'progress':
                    self.status_var.set(value)
                    continue
                self._busy = False
                if kind == 'preview':
                    self._preview = value
                    counts = value.counts
                    self.summary_var.set(f'חודש {value.month}   |   {len(value.events)} שיבוצים   |   '
                         f'{len({e["Name"] for e in value.events})} עובדים   |   '
                         f'חדשים: {counts["added"]}   הוסרו: {counts["removed"]}   שינוי שעות: {counts["changed"]}')
                    warnings = value.workbook.warnings
                    self.warning_var.set('\n'.join(warnings[:2]) + (f'\nועוד {len(warnings)-2} שיבוצים חסרים.' if len(warnings) > 2 else ''))
                    names = sorted({(r['after'] or r['before'])['Name'] for r in value.changes})
                    self.worker_filter.configure(values=['כל העובדים', *names])
                    self.worker_var.set('כל העובדים')
                    self._render_preview()
                    self.tabs.select(0)
                    self.status_var.set('התצוגה מוכנה. אפשר לסנן לפי עובד ולבדוק שינויים לפני הפרסום.')
                elif kind == 'published':
                    self.status_var.set(value.message)
                    self._preview = None
                    self._refresh_links()
                elif kind == 'links_synced':
                    self.status_var.set('גיליון קישורי יומנים עודכן. אפשר לפתוח את גיליון היומנים ולהעתיק ממנו קישורים.')
                    self._refresh_links()
                else:
                    self._preview = None
                    self.status_var.set(value)
                    messagebox.showerror('יומנים אישיים', value, parent=self)
                self._set_controls()
        except queue.Empty:
            pass
        self._after_id = self.after(100, self._drain)

    def _render_preview(self):
        self.tree.delete(*self.tree.get_children())
        if not self._preview:
            return
        for index, change in enumerate(self._preview.changes):
            event = change['after'] or change['before']
            if self.worker_var.get() != 'כל העובדים' and event['Name'] != self.worker_var.get():
                continue
            if self.change_var.get() == 'שינויים בלבד' and change['kind'] == 'unchanged':
                continue
            self.tree.insert('', 'end', iid=str(index), values=(local_string(event['End']),
                local_string(event['Start']), event['Name'], event['Shift'],
                event['Date'][8:10] + '/' + event['Date'][5:7] + '/' + event['Date'][:4], CHANGE_LABELS[change['kind']]))

    def _show_previous(self, _=None):
        selected = self.tree.selection()
        if not selected or not self._preview:
            return
        before = self._preview.changes[int(selected[0])]['before']
        self.previous_var.set('לפני הפרסום: ' + local_string(before['Start']) + ' עד ' + local_string(before['End'])
                              if before else 'שיבוץ חדש שלא היה בפרסום הקודם.')

    def _refresh_links(self):
        try:
            state = self.publisher.store.read()
            self._links = state.get('feeds', {})
            events = [e for data in published_months(state).values() for e in data['events']]
            counts = Counter(e['Name'] for e in events)
            self.links_tree.delete(*self.links_tree.get_children())
            for index, name in enumerate(sorted(self._links)):
                self.links_tree.insert('', 'end', iid=str(index), values=(counts[name], name))
            self.url_var.set('')
            self.calendar_name_var.set('')
        except Exception as exc:
            self.status_var.set('לא ניתן לטעון קישורים: ' + str(exc))

    def _select_link(self, _=None):
        selected = self.links_tree.selection()
        if selected:
            name = self.links_tree.item(selected[0], 'values')[1]
            self.url_var.set(self._links.get(name, ''))
            self.calendar_name_var.set(calendar_name(name))

    def _copy_calendar_name(self):
        if self.calendar_name_var.get():
            self.clipboard_clear()
            self.clipboard_append(self.calendar_name_var.get())
            self.status_var.set('שם היומן הועתק. אם Google מציג כתובת במקום שם, אפשר להדביק אותו בשדה שם היומן בהגדרות.')

    def _copy_link(self):
        if self.url_var.get():
            self.clipboard_clear()
            self.clipboard_append(self.url_var.get())
            self.status_var.set('הקישור האישי הועתק. מי שמחזיק בקישור יכול לראות את שיבוצי העובד ביומן.')

    def _open_resource(self, kind):
        state = self.publisher.store.read()
        resource = state.get('sheet_id' if kind == 'sheet' else 'script_id')
        if resource:
            webbrowser.open(('https://docs.google.com/spreadsheets/d/' + resource + '/edit') if kind == 'sheet'
                            else ('https://script.google.com/home/projects/' + resource + '/edit'))

    def _instructions(self):
        messagebox.showinfo('הצטרפות ליומן אישי',
            'בחרו עובד והעתיקו את הקישור האישי.\n\n'
            'Google Calendar: במחשב, ליד Other calendars לחצו + ואז From URL, הדביקו את הקישור והוסיפו את היומן.\n\n'
            'אם Google מציג את הכתובת במקום שם היומן: פתחו את הגדרות היומן והדביקו את השם מכפתור העתקת שם היומן.\n\n'
            'Apple Calendar: הוספת יומן > הוספת יומן במינוי, והדבקת הקישור.\n\n'
            'Outlook בדפדפן: Add calendar > Subscribe from web, והדבקת הקישור.\n\n'
            'Google Calendar נבדק בהצטרפות הראשונית. Apple ו-Outlook טרם נבדקו בפיילוט.\n'
            'עדכונים אינם מיידיים; אין צורך להצטרף מחדש בכל חודש.', parent=self)

    def _close(self):
        if self._busy:
            self.status_var.set('הפעולה עדיין פועלת. אפשר לסגור את החלון לאחר סיומה.')
            return
        self.destroy()

    def destroy(self):
        if self._after_id:
            self.after_cancel(self._after_id)
            self._after_id = None
        super().destroy()
