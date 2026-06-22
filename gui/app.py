from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import urllib.request


DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "output_roster"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
LOGO_PATH = ASSETS_DIR / "shamir_logo.png"
LOGO_URL = "https://shamirmedicalcenter.org/wp-content/uploads/2025/10/heb_eng.cleaned-scaled-e1760995935901-1200x341.png"
TEMPLATE_REFRESH_SCRIPT = PROJECT_ROOT / "tools" / "update_template_vba.ps1"
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "neuroshift_template.xlsm"
VBA_MODULE_PATH = PROJECT_ROOT / "Module2.bas"
RTL = "\u200f"
RTL_EMBED = "\u202b"
POP_DIRECTIONAL = "\u202c"


def rtl(text: str) -> str:
    return f"{RTL_EMBED}{text}{POP_DIRECTIONAL}"


def _refresh_excel_template_if_needed() -> None:
    if not TEMPLATE_REFRESH_SCRIPT.exists() or not TEMPLATE_PATH.exists() or not VBA_MODULE_PATH.exists():
        return

    source_stamp = max(
        TEMPLATE_REFRESH_SCRIPT.stat().st_mtime,
        VBA_MODULE_PATH.stat().st_mtime,
    )
    if TEMPLATE_PATH.stat().st_mtime >= source_stamp:
        return

    import subprocess

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(TEMPLATE_REFRESH_SCRIPT),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stdout + "\n" + result.stderr).strip()
        raise RuntimeError(f"לא ניתן לעדכן את תבנית Excel.\n{details}")


def _history_notice(month: str) -> str:
    from core.data import backend_tables
    import pandas as pd

    try:
        yr, mon = map(int, month.split("-"))
        first = date(yr, mon, 1)
        prev_month_day = first - timedelta(days=1)
        hist = backend_tables().get("history")
        if hist is None or hist.empty:
            return "לא נמצאו נתוני היסטוריה. הסידור יווצר ללא נתוני חודש קודם."
        if not {"Date", "Name", "Shift"}.issubset(hist.columns):
            return "טבלת ההיסטוריה נמצאה, אבל חסרות בה עמודות Date/Name/Shift."

        dates = pd.to_datetime(hist["Date"], format="mixed", dayfirst=True, errors="coerce").dt.date
        prev_month_rows = hist[
            dates.map(lambda d: isinstance(d, date) and d.year == prev_month_day.year and d.month == prev_month_day.month)
        ]
        if prev_month_rows.empty:
            return f"לא נמצאה היסטוריה לחודש הקודם ({prev_month_day:%Y-%m})."

        last_day_rows = hist[
            (dates == prev_month_day)
            & (hist["Shift"].astype(str).str.strip().isin(["ת.מיון", "ת.מיון 2"]))
        ]
        if last_day_rows.empty:
            return f"לא נמצאו תורנויות ת.מיון/ת.מיון 2 ביום האחרון של החודש הקודם ({prev_month_day:%d/%m/%Y})."
    except Exception:
        return "לא ניתן היה לבדוק את נתוני ההיסטוריה."
    return ""


class NeuroShiftApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Neuro Shift")
        self.geometry("860x620")
        self.minsize(820, 580)
        self.configure(bg="#F5F7FA")

        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy = False
        self._logo_image: tk.PhotoImage | None = None
        self._spinner_frames = ["|", "/", "-", "\\"]
        self._spinner_index = 0
        self._progress_label = ""
        self._progress_percent = 0

        today = date.today()
        default_year = today.year + (1 if today.month == 12 else 0)
        default_month = 1 if today.month == 12 else today.month + 1
        self.year_values = [str(y) for y in range(today.year - 1, today.year + 4)]
        self.month_values = [f"{m:02d}" for m in range(1, 13)]

        self.year_var = tk.StringVar(value=str(default_year))
        self.month_var = tk.StringVar(value=f"{default_month:02d}")
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_OUT_DIR))
        self.finalized_file_var = tk.StringVar()
        self.optimize_file_var = tk.StringVar()
        self.state_var = tk.StringVar(value=rtl("ממתין לפעולה"))
        self.progress_var = tk.DoubleVar(value=0)

        self._build()
        self._load_logo_async()
        self.after(100, self._drain_queue)

    def _build(self) -> None:
        self._configure_style()

        root = ttk.Frame(self, padding=18, style="Root.TFrame")
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root, style="Header.TFrame", padding=(16, 14))
        header.pack(fill=tk.X)
        header.columnconfigure(1, weight=1)

        title = ttk.Label(header, text="Neuro Shift", style="Title.TLabel")
        title.grid(row=0, column=0, sticky="w")

        self.logo_label = ttk.Label(header, text="שמיר", style="LogoPlaceholder.TLabel")
        self.logo_label.grid(row=0, column=2, sticky="e")

        import_box = ttk.LabelFrame(
            root,
            text=rtl("(אופציונלי) העלאת סידור סופי של חודש קודם"),
            padding=14,
            style="Card.TLabelframe",
            labelanchor="ne",
        )
        import_box.pack(fill=tk.X, pady=(16, 0))

        ttk.Label(import_box, text="קובץ סידור סופי").grid(row=0, column=0, sticky="e")
        ttk.Entry(import_box, textvariable=self.finalized_file_var).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(import_box, text="בחירה", command=self._browse_finalized_file).grid(row=1, column=1, sticky="e")
        ttk.Button(import_box, text="שמור להיסטוריה", command=self._import_history, style="Accent.TButton").grid(row=1, column=2, sticky="e", padx=(12, 0))
        import_box.columnconfigure(0, weight=1)

        month_box = ttk.LabelFrame(
            root,
            text=rtl("יצירת סידור"),
            padding=14,
            style="Card.TLabelframe",
            labelanchor="ne",
        )
        month_box.pack(fill=tk.X, pady=(14, 0))
        month_box.configure(style="Card.TLabelframe")

        ttk.Label(month_box, text="שנה").grid(row=0, column=0, sticky="e")
        ttk.Combobox(
            month_box,
            textvariable=self.year_var,
            values=self.year_values,
            width=10,
            state="normal",
        ).grid(row=1, column=0, sticky="w", padx=(0, 12))

        ttk.Label(month_box, text="חודש").grid(row=0, column=1, sticky="e")
        ttk.Combobox(
            month_box,
            textvariable=self.month_var,
            values=self.month_values,
            width=10,
            state="normal",
        ).grid(row=1, column=1, sticky="w", padx=(0, 12))

        ttk.Label(month_box, text="תיקיית פלט").grid(row=0, column=2, sticky="e")
        ttk.Entry(month_box, textvariable=self.output_dir_var).grid(row=1, column=2, sticky="ew", padx=(0, 8))
        ttk.Button(month_box, text="בחירה", command=self._browse_output_dir).grid(row=1, column=3, sticky="e")
        ttk.Button(month_box, text="צור סידור", command=self._generate_roster, style="Accent.TButton").grid(row=1, column=4, sticky="e", padx=(12, 0))
        month_box.columnconfigure(2, weight=1)

        optimize_box = ttk.LabelFrame(
            root,
            text=rtl("שיפור סידור קיים"),
            padding=14,
            style="Card.TLabelframe",
            labelanchor="ne",
        )
        optimize_box.pack(fill=tk.X, pady=(14, 0))

        ttk.Label(optimize_box, text="קובץ סידור לניתוח").grid(row=0, column=0, sticky="e")
        ttk.Entry(optimize_box, textvariable=self.optimize_file_var).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(optimize_box, text="בחירה", command=self._browse_optimize_file).grid(row=1, column=1, sticky="e")
        ttk.Button(optimize_box, text="שפר סידור", command=self._optimize_roster, style="Accent.TButton").grid(row=1, column=2, sticky="e", padx=(12, 0))
        optimize_box.columnconfigure(0, weight=1)

        status_box = ttk.LabelFrame(
            root,
            text=rtl("מצב"),
            padding=12,
            style="Card.TLabelframe",
            labelanchor="ne",
        )
        status_box.pack(fill=tk.BOTH, expand=True, pady=(14, 0))

        indicator = ttk.Frame(status_box, style="Card.TFrame")
        indicator.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            indicator,
            textvariable=self.state_var,
            style="State.TLabel",
            anchor="e",
            justify=tk.RIGHT,
        ).pack(side=tk.RIGHT)
        self.progress = ttk.Progressbar(
            indicator,
            mode="determinate",
            maximum=100,
            variable=self.progress_var,
            length=180,
        )
        self.progress.pack(side=tk.LEFT)

        self.status = tk.Text(status_box, height=14, wrap="word", state="disabled")
        self.status.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(status_box, orient=tk.VERTICAL, command=self.status.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.status.configure(yscrollcommand=scroll.set)
        self.status.tag_configure("rtl", justify="right", rmargin=6, lmargin1=6, lmargin2=6)

        self._log("מוכן.")

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Root.TFrame", background="#F5F7FA")
        style.configure("Header.TFrame", background="#EAF2F8")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("Title.TLabel", background="#EAF2F8", foreground="#17324D", font=("Segoe UI", 18, "bold"))
        style.configure("LogoPlaceholder.TLabel", background="#EAF2F8", foreground="#486177", font=("Segoe UI", 11, "bold"))
        style.configure("Card.TLabelframe", background="#FFFFFF", foreground="#17324D")
        style.configure("Card.TLabelframe.Label", background="#F5F7FA", foreground="#17324D", font=("Segoe UI", 10, "bold"))
        style.configure("State.TLabel", background="#FFFFFF", foreground="#17324D", font=("Segoe UI", 9, "bold"))
        style.configure("TLabel", background="#FFFFFF", foreground="#263238", font=("Segoe UI", 9))
        style.configure("TEntry", padding=4)
        style.configure("TCombobox", padding=4)
        style.configure("TButton", padding=(10, 5), font=("Segoe UI", 9))
        style.configure("Accent.TButton", background="#1F6F8B", foreground="#FFFFFF", padding=(12, 6), font=("Segoe UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#185A73")], foreground=[("active", "#FFFFFF")])

    def _load_logo_async(self) -> None:
        def worker() -> None:
            try:
                ASSETS_DIR.mkdir(parents=True, exist_ok=True)
                if not LOGO_PATH.exists():
                    with urllib.request.urlopen(LOGO_URL, timeout=8) as response:
                        LOGO_PATH.write_bytes(response.read())
                self._queue.put(("logo", str(LOGO_PATH)))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _set_logo(self, path: str) -> None:
        try:
            try:
                from PIL import Image, ImageTk

                src = Image.open(path)
                target_width = 260
                ratio = target_width / max(src.width, 1)
                target_height = max(1, int(src.height * ratio))
                src = src.resize((target_width, target_height), Image.Resampling.LANCZOS)
                image = ImageTk.PhotoImage(src)
            except Exception:
                image = tk.PhotoImage(file=path)
                width = max(image.width(), 1)
                target_width = 260
                factor = max(1, round(width / target_width))
                if factor > 1:
                    image = image.subsample(factor, factor)
            self._logo_image = image
            self.logo_label.configure(image=image, text="")
        except Exception:
            pass

    def _target_month(self) -> str:
        year = self.year_var.get().strip()
        month = self.month_var.get().strip()
        if not year or not month:
            raise ValueError("חובה לבחור שנה וחודש.")
        if len(month) == 1:
            month = f"0{month}"
        if len(year) != 4 or not year.isdigit():
            raise ValueError("יש להקליד שנה בפורמט 2026.")
        if len(month) != 2 or not month.isdigit() or not (1 <= int(month) <= 12):
            raise ValueError("יש להקליד חודש בפורמט 07.")
        return f"{year}-{month}"

    def _browse_output_dir(self) -> None:
        picked = filedialog.askdirectory(
            title="בחירת תיקיית פלט",
            initialdir=self.output_dir_var.get() or str(DEFAULT_OUT_DIR),
        )
        if picked:
            self.output_dir_var.set(picked)

    def _browse_finalized_file(self) -> None:
        picked = filedialog.askopenfilename(
            title="בחירת סידור סופי",
            filetypes=[("קבצי Excel", "*.xlsx *.xlsm"), ("כל הקבצים", "*.*")],
            initialdir=str(DEFAULT_OUT_DIR),
        )
        if picked:
            self.finalized_file_var.set(picked)

    def _browse_optimize_file(self) -> None:
        picked = filedialog.askopenfilename(
            title="בחירת סידור לשיפור",
            filetypes=[("קבצי Excel", "*.xlsx *.xlsm"), ("כל הקבצים", "*.*")],
            initialdir=str(DEFAULT_OUT_DIR),
        )
        if picked:
            self.optimize_file_var.set(picked)

    def _run_background(self, label: str, fn) -> None:
        if self._busy:
            messagebox.showinfo("Neuro Shift", "פעולה אחרת עדיין רצה.")
            return
        self._busy = True
        self._set_progress(0, "מתחיל")
        self._log(label)

        def worker() -> None:
            try:
                result = fn()
                self._queue.put(("ok", result or "Done."))
            except Exception as exc:
                self._queue.put(("error", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _generate_roster(self) -> None:
        try:
            month = self._target_month()
            out_dir = Path(self.output_dir_var.get().strip() or DEFAULT_OUT_DIR)
        except Exception as exc:
            messagebox.showerror("Neuro Shift", str(exc))
            return

        def task() -> str:
            from core.assign2 import auto_assign
            from core.export import export_month_to_xlsx

            def progress(percent: int, label: str) -> None:
                self._queue.put(("progress", (percent, label)))

            roster = auto_assign(month, progress_callback=progress)
            notice = _history_notice(month)
            if notice:
                self._queue.put(("log", notice))
            progress(98, "מעדכן תבנית Excel")
            _refresh_excel_template_if_needed()
            progress(99, "מייצא קובץ Excel")
            path = export_month_to_xlsx(roster, month=month, out_dir=out_dir)
            self._queue.put(("optimize_file", str(path)))
            progress(100, "הסתיים")
            return f"הסידור נוצר: {path}"

        self._run_background(f"…יוצר סידור עבור {month}", task)

    def _import_history(self) -> None:
        path = self.finalized_file_var.get().strip()
        if not path:
            messagebox.showerror("Neuro Shift", "יש לבחור קובץ סידור סופי.")
            return

        def task() -> str:
            from core.history_importer import import_finalized_roster

            def progress(percent: int, label: str) -> None:
                self._queue.put(("progress", (percent, label)))

            month, history_rows, summary_rows = import_finalized_roster(
                path,
                progress_callback=progress,
            )
            return (
                f"נשמר {month}: {history_rows} שורות היסטוריה, "
                f"{summary_rows} שורות סיכום."
            )

        self._run_background("…שומר נתוני סידור בהיסטוריה", task)

    def _optimize_roster(self) -> None:
        path = self.optimize_file_var.get().strip()
        if not path:
            messagebox.showerror("Neuro Shift", "יש לבחור קובץ סידור לשיפור.")
            return

        def task() -> str:
            from core.optimizer import optimize_exported_roster

            def progress(percent: int, label: str) -> None:
                self._queue.put(("progress", (percent, label)))

            out_path, summary = optimize_exported_roster(path, progress_callback=progress)
            self._queue.put(("optimize_file", str(out_path)))
            warning_count = len(summary.get("warnings", []))
            return (
                f"נוצר סידור משופר: {out_path}\n"
                f"הושלמו {summary.get('hard_filled', 0)} שיבוצי מיון/ייעוצים חסרים. "
                f"אזהרות רצף תורנויות: {warning_count}."
            )

        self._run_background("…משפר סידור קיים", task)

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, message = self._queue.get_nowait()
                if kind == "logo":
                    self._set_logo(str(message))
                    continue
                if kind == "optimize_file":
                    self.optimize_file_var.set(str(message))
                    continue
                if kind == "progress":
                    percent, label = message
                    self._set_progress(int(percent), str(label))
                    continue
                if kind == "log":
                    self._log(str(message))
                    continue
                self._busy = False
                self._set_waiting()
                if kind == "error":
                    self._log(f"שגיאה: {message}")
                    messagebox.showerror("Neuro Shift", message)
                else:
                    self._log(message)
        except queue.Empty:
            pass
        self._tick_spinner()
        self.after(100, self._drain_queue)

    def _set_progress(self, percent: int, label: str) -> None:
        percent = max(0, min(100, percent))
        self._progress_percent = percent
        self._progress_label = label
        self.progress_var.set(percent)
        self._render_progress_state()

    def _set_waiting(self) -> None:
        self.progress_var.set(0)
        self._progress_percent = 0
        self._progress_label = ""
        self.state_var.set(rtl("ממתין לפעולה"))

    def _tick_spinner(self) -> None:
        if not self._busy or not self._progress_label:
            return
        self._spinner_index = (self._spinner_index + 1) % len(self._spinner_frames)
        self._render_progress_state()

    def _render_progress_state(self) -> None:
        spinner = self._spinner_frames[self._spinner_index] if self._busy else ""
        suffix = "..." if self._busy else ""
        self.state_var.set(rtl(f"{self._progress_label} {self._progress_percent}% {spinner}{suffix}"))

    def _log(self, message: str) -> None:
        self.status.configure(state="normal")
        self.status.insert(tk.END, rtl(message) + "\n", "rtl")
        self.status.see(tk.END)
        self.status.configure(state="disabled")


def main() -> None:
    app = NeuroShiftApp()
    app.mainloop()


if __name__ == "__main__":
    main()
