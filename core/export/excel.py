"""
core.export.excel
=================
Excel export helpers for a *filled* roster DataFrame.
"""

from __future__ import annotations

from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Dict, Sequence, Iterable

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.workbook import Workbook
from openpyxl.worksheet.formula import ArrayFormula

from core.elig_utils import workers_df, unavail_lookup   # auto-build of unassigned lists
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ──────────────────────────────────────────────────────────────
#  Hebrew weekday letters  (Python: Monday=0 … Sunday=6)
# ──────────────────────────────────────────────────────────────
_WEEKDAY_LETTERS = ["ב", "ג", "ד", "ה", "ו", "ש", "א"]  # M-Sun → ב … א
# Full weekday names (Python: Monday=0 … Sunday=6)
_WEEKDAY_FULL = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
_HEB_MONTHS = ["ינואר","פברואר","מרץ","אפריל","מאי","יוני",
               "יולי","אוגוסט","ספטמבר","אוקטובר","נובמבר","דצמבר"]

def _heb_month_name(year: int, mon: int) -> str:
    return f"{_HEB_MONTHS[mon-1]} {str(year)[-2:]}"

#  Custom row order ­– exactly as requested
SHIFT_ORDER: List[str] = [
    "אטנדינג", "מחלקה", "מיון", "אשפוז יום", "ת.מיון", "ת.מיון 2", "כונן מיון",
    "אינטובציה", "ייעוצים מובילים", "מחקר",
    "מרפאת תנועה", "מרפאת אפילפסיה גנדלמן", "מרפאת אפילפסיה הרש", "מרפאת CVA", "מרפאת קרוטיס", "מרפאת זיכרון",
    "מרפאת בוטוקס", "מרפאת נוירואימונולוגיה", "מרפאת עצב שריר", "מרפאת כאבי ראש",
    "מרפאת פוסט אשפוז", "מרפאת שבץ מוחי", "מרפאת נוירואונקולוגיה", "נוירולוגיה כללית",
    "EMG", "EEG", "EEG ילדים", "אחרי תורנות", "חלופי", "חופש", "רוטציה"
]

FRIDAY_LINK_SHIFTS: List[str] = [
    "אטנדינג",
    "מחלקה",
    "מיון",
    "ת.מיון",
    "ת.מיון 2",
    "ייעוצים מובילים",
]

#  default output folder
_OUT_DIR = Path(
    r"C:\Users\shlom\Google Drive\Neurology\Projects\Neuro Shift\neuroshift-py\output_roster"
)
_OUT_DIR.mkdir(parents=True, exist_ok=True)
# macro-enabled template
_XLSM_TEMPLATE = _PROJECT_ROOT / "templates" / "neuroshift_template.xlsm"

_FIXED_TEMPLATE_SHEETS = {"תורנויות", "עובדים", "ימי שישי", "ייעוצים"}

#  fills
_ORANGE = PatternFill("solid", fgColor="FFD99B")  # Fri/Sat
_GREY   = PatternFill("solid", fgColor="E6E6E6")  # out-of-month
_BLUE   = PatternFill("solid", fgColor="CDEBF7")  # holidays / erev-chag (ייעוצים sheet)

# ──────────────────────────────────────────────────────────────
#  helpers
# ──────────────────────────────────────────────────────────────
def _sunday_of(d: date) -> date:
    """Return the Sunday on or *preceding* date *d*  (Sunday = weekday 6)."""
    delta = (d.weekday() + 1) % 7          # Sun→0, Mon→1, … Sat→6
    return d - timedelta(days=delta)

def _week_range(sunday: date) -> List[date]:
    """[Sunday, Monday, …, Saturday]  starting at *sunday* (inclusive)."""
    return [sunday + timedelta(days=i) for i in range(7)]

def _names(cell: str) -> Iterable[str]:
    """Split an 'Assigned' cell into clean names (skip warnings / ‘-’)."""
    for n in cell.split(","):
        n = n.strip()
        if n and not n.startswith("⚠️") and n != "-":
            yield n

def _auto_unassigned(roster: pd.DataFrame) -> Dict[str, List[str]]:
    """Compute un-assigned names per ISO-date (names only)."""
    all_workers = workers_df()["שם"].tolist()
    out: Dict[str, List[str]] = {}
    for d_iso in roster["Date"].unique():
        assigned = {
            n
            for cell in roster.loc[roster["Date"] == d_iso, "Assigned"]
            for n in _names(cell)
        }
        out[d_iso] = [w for w in all_workers if w not in assigned]
    return out

def _auto_days_off() -> Dict[str, List[str]]:
    """
    Return {date_iso: [names…]} for general day-off requests ("לא זמין").
    Based on unified availability lookup; merges all sources.
    """
    lookup = unavail_lookup()  # {(name, date_iso): [(block, src), ...]}
    by_date: Dict[str, List[str]] = {}
    for (name, d_iso), tags in lookup.items():
        if any(bt == "לא זמין" for bt, _ in tags):
            by_date.setdefault(d_iso, []).append(name)
    # de-duplicate and sort per date
    for k, v in by_date.items():
        by_date[k] = sorted(set(v))
    return by_date

def _auto_nights_off() -> Dict[str, List[str]]:
    """
    Return {date_iso: [names…]} for night-duty-only requests
    ("לא זמין לתורנות"), based on unified availability lookup.
    """
    lookup = unavail_lookup()  # {(name, date_iso): [(block, src), ...]}
    by_date: Dict[str, List[str]] = {}
    for (name, d_iso), tags in lookup.items():
        if any(bt == "לא זמין לתורנות" for bt, _ in tags):
            by_date.setdefault(d_iso, []).append(name)

    for k, v in by_date.items():
        by_date[k] = sorted(set(v))
    return by_date

def _merge_names(existing: str, extra: Sequence[str]) -> str:
    """Merge comma-separated names with a list of extra names, deduped."""
    cur = set(_names(existing))
    cur.update(n for n in extra if n.strip())
    return ", ".join(sorted(cur)) if cur else "-"

def _pivot_for_days(
    roster: pd.DataFrame,
    seven_iso: Sequence[str],
    month_filter: str | None = None,
    *,
    days_off_by_date: Dict[str, Sequence[str]] | None = None,
) -> pd.DataFrame:
    """
    Build a pivot whose
    • rows follow SHIFT_ORDER
    • columns follow *seven_iso* (Sun…Sat) – **always seven**
    • cells come only from *month_filter* (YYYY-MM) → outside days show “-”
    """
    subset = roster[roster["Date"].isin(seven_iso)]
    if month_filter is not None:
        subset = subset[subset["Date"].str.startswith(month_filter)]

    pvt = (
        subset
        .pivot(index="Shift", columns="Date", values="Assigned")
        .reindex(index=SHIFT_ORDER, fill_value="-")
        .reindex(columns=seven_iso, fill_value="-")
    )

    # overlay day-off requests into the 'חופש' row
    if days_off_by_date:
        for iso in seven_iso:
            if "חופש" in pvt.index:
                extra = days_off_by_date.get(iso, [])
                if extra:
                    current = pvt.at["חופש", iso]
                    pvt.at["חופש", iso] = _merge_names(current, extra)

    # rename columns to dd/mm/yyyy for display
    col_map = {iso: datetime.fromisoformat(iso).strftime("%d/%m/%Y")
               for iso in seven_iso}
    pvt.rename(columns=col_map, inplace=True)

    pvt.index.name = pvt.columns.name = None
    return pvt

def _fmt_unassigned(lst: Sequence[str | tuple[str, str]]) -> str:
    """Return newline-joined 'name (reason)' or just 'name'."""
    if not lst:
        return "-"
    if isinstance(lst[0], tuple):
        return "\n".join(f"{n} ({r})" for n, r in lst)       # type: ignore[arg-type]
    return "\n".join(lst)                                    # type: ignore[return-value]

def _build_calendar_df(
    pivot: pd.DataFrame,
    seven_dates: List[date],
    unassigned: Dict[str, Sequence[str | tuple[str, str]]] | None = None
) -> pd.DataFrame:
    """Return DF = 2-row header + body + optional ‘un-assigned’ row."""
    date_cols = [d.strftime("%d/%m/%Y") for d in seven_dates]
    cols      = ["תפקיד"] + date_cols

    # Weekday first (bolded top row) then date row
    hdr1 = [""] + [_WEEKDAY_LETTERS[d.weekday()] for d in seven_dates]
    hdr2 = ["תפקיד"] + date_cols

    header = pd.DataFrame([hdr1, hdr2], columns=cols)
    body   = pivot.reset_index().rename(columns={"index": "תפקיד"})

    frames = [header, body]

    return pd.concat(frames, ignore_index=True)[cols]   # keep order

# ----------  Excel-writing primitives  ------------------------
def _style_block(
    ws,
    top: int,
    left: int,
    n_rows: int,
    n_cols: int,
    highlight_cols: Sequence[int],
    grey_cols: Sequence[int] = ()
) -> None:
    """
    Apply:
      • wrap-text
      • header center (top row bold)
      • body right-align (RTL content)
      • thin borders on every cell
      • weekend orange fill
      • out-of-month gray fill (overrides weekend)
    """
    centre   = Alignment(horizontal="center", vertical="center",
                         wrap_text=True, readingOrder=2)
    rightaln = Alignment(horizontal="right",  vertical="top",
                         wrap_text=True, readingOrder=2)
    bold     = Font(bold=True)
    thin     = Side(style="thin", color="000000")
    border   = Border(left=thin, right=thin, top=thin, bottom=thin)

    # set column widths
    ws.column_dimensions[get_column_letter(left)].width = 18
    for c in range(left + 1, left + n_cols):
        ws.column_dimensions[get_column_letter(c)].width = 14

    for r in range(top, top + n_rows):
        for c in range(left, left + n_cols):
            cell = ws.cell(r, c)
            cell.border = border

            # header two rows → centre; top row bold
            if r in (top, top + 1):
                cell.alignment = centre
                if r == top:
                    cell.font = bold
            else:
                # body rows wrap & top-align, but right for Hebrew content
                cell.alignment = rightaln

            # fills (gray wins over orange)
            if c in grey_cols:
                cell.fill = _GREY
            elif c in highlight_cols:
                cell.fill = _ORANGE

def _write_block(
    ws,
    start_row: int,
    df_block: pd.DataFrame,
    seven_dates: List[date],
    grey_cols: Sequence[int] = ()
) -> int:
    """Write *df_block* (top-left at A<start_row>) – return next free row."""
    n_rows, n_cols = df_block.shape

    # write values
    for r_off, (_, row_vals) in enumerate(df_block.iterrows()):
        for c_off, val in enumerate(row_vals):
            ws.cell(start_row + r_off, 1 + c_off, val)

    # which visible columns are Fri/Sat? (skip “תפקיד” which is col 1)
    highlight = [
        1 + i
        for i, d in enumerate(seven_dates, start=1)
        if d.weekday() in (4, 5)  # Fri=4, Sat=5
    ]

    _style_block(ws, start_row, 1, n_rows, n_cols,
                 highlight_cols=highlight,
                 grey_cols=grey_cols)
    return start_row + n_rows

# ----------  Extra sheet builders  ----------------------------
def _new_sheet(wb: Workbook, title: str):
    ws = wb.create_sheet(title=title)
    ws.sheet_view.rightToLeft = True
    return ws

def _find_template_month_sheet(wb: Workbook):
    """
    Return the single non-fixed sheet in the template.
    This is the VBA-bearing main month sheet that must be reused, not deleted.
    """
    candidates = [ws for ws in wb.worksheets if ws.title not in _FIXED_TEMPLATE_SHEETS]
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one template month sheet, found {[ws.title for ws in candidates]}"
        )
    return candidates[0]

def _clear_range(
    ws,
    min_row: int,
    min_col: int,
    max_row: int | None = None,
    max_col: int | None = None,
    *,
    clear_merges: bool = True,
) -> None:
    """
    Clear cell values/comments/hyperlinks in a rectangular range.
    Keeps the worksheet object itself intact, which is critical for worksheet VBA code.
    """
    max_row = max_row or ws.max_row
    max_col = max_col or ws.max_column

    if clear_merges:
        for merged in list(ws.merged_cells.ranges):
            m_min_col, m_min_row, m_max_col, m_max_row = range_boundaries(str(merged))
            intersects = not (
                m_max_row < min_row or m_min_row > max_row or
                m_max_col < min_col or m_min_col > max_col
            )
            if intersects:
                ws.unmerge_cells(str(merged))

    for row in ws.iter_rows(
        min_row=min_row,
        max_row=max_row,
        min_col=min_col,
        max_col=max_col,
    ):
        for cell in row:
            cell.value = None
            cell.comment = None
            cell.hyperlink = None

def _prepare_month_sheet(ws) -> None:
    """
    Clear only the exported calendar area (A:H).
    Do NOT clear the whole sheet, because the template contains summary
    formulas/tables to the right (J and onward) that must be preserved.
    """
    _clear_range(ws, 1, 1, ws.max_row, 8, clear_merges=True)   # A:H only
    ws.sheet_view.rightToLeft = True
    ws.freeze_panes = None

def _prepare_toranut_sheet(ws) -> None:
    """
    Clear only the areas owned by the Python exporter:
    - main toranut table in B:F
    - helper night-unavailability column in R
    Preserve the counting/formula area in between.
    """
    _clear_range(ws, 1, 2, ws.max_row, 6, clear_merges=True)  # B:F
    _clear_range(ws, 1, 18, ws.max_row, 18, clear_merges=False)  # R:R
    ws.sheet_view.rightToLeft = True
    ws.freeze_panes = None

def _prepare_ovdim_sheet(ws) -> None:
    _clear_range(ws, 1, 1, ws.max_row, 3, clear_merges=True)  # A:C
    ws.sheet_view.rightToLeft = True
    ws.freeze_panes = None

def _prepare_fridays_sheet(ws) -> None:
    _clear_range(ws, 1, 1, ws.max_row, ws.max_column, clear_merges=True)
    ws.sheet_view.rightToLeft = True
    ws.freeze_panes = None

def _prepare_yoatzim_sheet(ws) -> None:
    _clear_range(ws, 1, 3, ws.max_row, 5, clear_merges=True)  # C:E
    ws.sheet_view.rightToLeft = True
    ws.freeze_panes = None

def _save_workbook_atomic(wb: Workbook, out_path: Path) -> None:
    """
    Save to a temporary file first, then replace the final file.
    This reduces the chance of ending up with a corrupted .xlsm if saving fails midway.
    """
    tmp_path = out_path.with_name(f"{out_path.stem}.__tmp__{out_path.suffix}")
    if tmp_path.exists():
        tmp_path.unlink()
    wb.save(tmp_path)
    tmp_path.replace(out_path)

def _ensure_xlsm_name(month: str, fname: str | None) -> str:
    if fname is None:
        return f"roster_{month}.xlsm"
    p = Path(fname)
    if p.suffix.lower() != ".xlsm":
        return f"{p.stem}.xlsm"
    return p.name

def _friday_unique_names_formula(source_refs: Sequence[str]) -> str:
    """
    Return a live Excel formula that pulls comma-separated names from the
    month sheet, splits them, removes blanks/dashes, de-duplicates, and joins
    them with line breaks.
    """
    if not source_refs:
        return '=""'
    joined_refs = ",".join(source_refs)
    return (
        f'=LET(src,TEXTJOIN(",",TRUE,{joined_refs}),'
        'names,TRIM(TEXTSPLIT(src,",")),'
        'clean,FILTER(names,(names<>"")*(names<>"-")),'
        'IFERROR(TEXTJOIN(CHAR(10),TRUE,UNIQUE(clean)),""))'
    )

def _set_main_month_lookup_formulas(
    ws,
    *,
    block_start: int,
    seven_dates: Sequence[date],
) -> None:
    """
    Rebuild the template-driven formulas on the main month sheet:
    - ת.מיון    <- תורנויות!E
    - ת.מיון 2   <- תורנויות!F
    - כונן מיון    <- תורנויות!G
    """
    date_row = block_start + 1  # second header row in each block

    row_toren_mion = block_start + 2 + SHIFT_ORDER.index("ת.מיון")
    row_toren_mach = block_start + 2 + SHIFT_ORDER.index("ת.מיון 2")
    row_konen_mion = block_start + 2 + SHIFT_ORDER.index("כונן מיון")

    for i, _ in enumerate(seven_dates):
        col = 2 + i  # B..H
        col_letter = get_column_letter(col)
        date_ref = f"{col_letter}${date_row}"

        ws.cell(row_toren_mion, col,
    f'=IFERROR(INDEX(תורנויות!$D:$D, MATCH({date_ref}, תורנויות!$B:$B, 0)), "")')

        ws.cell(row_toren_mach, col,
            f'=IFERROR(INDEX(תורנויות!$E:$E, MATCH({date_ref}, תורנויות!$B:$B, 0)), "")')

        ws.cell(row_konen_mion, col,
            f'=IFERROR(INDEX(תורנויות!$F:$F, MATCH({date_ref}, תורנויות!$B:$B, 0)), "")')

def _set_unassigned_formula_row(
    ws,
    *,
    block_start: int,
    seven_dates: Sequence[date],
    mon: int,
) -> None:
    """
    Write the Excel formula row for '⚠️ לא שובצו' under one weekly block.
    """
    row_num = block_start + 2 + len(SHIFT_ORDER)

    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Column A label
    label_cell = ws.cell(row_num, 1, "⚠️ לא שובצו")
    label_cell.font = Font(bold=True)
    label_cell.alignment = Alignment(
        horizontal="right",
        vertical="top",
        wrap_text=True,
        readingOrder=2,
    )
    label_cell.border = border

    # Same row bands as your template logic
    r1_start = block_start + 2
    r1_end   = block_start + 4
    r2_start = block_start + 10
    r2_end   = block_start + 32

    for i, d in enumerate(seven_dates):
        col = 2 + i   # B..H
        col_letter = get_column_letter(col)
        ref = f"{col_letter}{row_num}" # e.g., "B100"

        # The safely prefixed string
        formula = (
            f'=_xlfn.LET('
            f'_xlpm.all,_xlfn._xlws.FILTER(\'עובדים\'!$A$1:$A$28,\'עובדים\'!$A$1:$A$28<>""),'
            f'_xlpm.listed,IFERROR(TRIM(_xlfn.TEXTSPLIT(_xlfn.TEXTJOIN(",",TRUE,'
            f'{col_letter}{r1_start}:{col_letter}{r1_end},'
            f'{col_letter}{r2_start}:{col_letter}{r2_end}),",",,TRUE)),""),'
            f'_xlpm.exclude,_xlfn.UNIQUE(_xlfn.TOCOL(_xlpm.listed)),'
            f'_xlpm.keep,ISNA(MATCH(_xlpm.all,_xlpm.exclude,0)),'
            f'IFERROR(_xlfn.TEXTJOIN(", ",TRUE,_xlfn._xlws.FILTER(_xlpm.all,_xlpm.keep)),"")'
            f')'
        )

        # 1. Get the cell
        cell = ws.cell(row_num, col)
        
        # 2. Inject as an ArrayFormula to prevent the @ symbol bug
        cell.value = ArrayFormula(ref, formula)

        # 3. Apply your styling
        cell.alignment = Alignment(
            horizontal="right",
            vertical="top",
            wrap_text=True,
            readingOrder=2,
        )
        cell.border = border

        if d.month != mon:
            cell.fill = _GREY
        elif d.weekday() in (4, 5):
            cell.fill = _ORANGE

def _build_sheet_toranut(
    ws,
    year: int,
    mon: int,
    roster_df: pd.DataFrame,
    holidays: Sequence[date] = (),
    nights_off_by_date: Dict[str, Sequence[str]] | None = None,
):
    """
    Sheet 'תורנויות':
    - Row 1: merged D1:F1 title "תורנויות כוננויות <Month YY>"
    - Row 2: empty spacer row
    - Row 3: headers (B:תאריך, C:יום, D:ת.מיון, E:ת.מיון 2, F:כ.מיון)
    - Rows 4+: one row per day in months
    - E/F/G are populated from roster_df:
        E <- ת.מיון
        F <- ת.מיון 2
        G <- כונן מיון
    - Column R: names marked "לא זמין לתורנות" for that date
    """
    title = f"תורנויות כוננויות {_heb_month_name(year, mon)}"
    ws.merge_cells(start_row=1, start_column=4, end_row=1, end_column=6)
    title_cell = ws.cell(1, 4, title)
    title_cell.font = Font(bold=True, size=14)
    title_cell.alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)

    headers = ["תאריך", "יום", "ת.מיון", "ת.מיון 2", "כ.מיון"]
    start_col = 2  # B
    for i, h in enumerate(headers, start=start_col):
        cell = ws.cell(3, i, h)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)
        ws.column_dimensions[get_column_letter(i)].width = 18

    # Separate helper column in R
    cell = ws.cell(3, 18, "לא זמינים ללילה")
    cell.font = Font(bold=True)
    cell.alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)
    ws.column_dimensions["R"].width = 30

    # Column H: Available workers
    cell_h = ws.cell(3, 8, "פנויים לשיבוץ")
    cell_h.font = Font(bold=True)
    cell_h.alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)
    ws.column_dimensions["H"].width = 30

    # Build lookup from roster_df
    month_prefix = f"{year:04d}-{mon:02d}"
    subset = roster_df[roster_df["Date"].str.startswith(month_prefix)].copy()

    assigned_by_date_shift: Dict[tuple[str, str], str] = {}
    for _, row in subset.iterrows():
        shift = str(row["Shift"])
        if shift not in {"ת.מיון", "ת.מיון 2", "כונן מיון"}:
            continue
        d_iso = str(row["Date"])
        names = ", ".join(_names(str(row["Assigned"]))) or "-"
        assigned_by_date_shift[(d_iso, shift)] = names

    ORANGE = PatternFill("solid", fgColor="FFD99B")   # Fri / erev hag
    YELLOW = PatternFill("solid", fgColor="FFF299")   # Sat / holiday
    thin = Side(style="thin", color="000000")
    thick = Side(style="medium", color="000000")

    first = date(year, mon, 1)
    cur = first
    row_ptr = 4
    while cur.month == mon:
        d_iso = cur.isoformat()

        ws.cell(row_ptr, 2, cur.strftime("%d/%m/%Y"))                               # B תאריך
        ws.cell(row_ptr, 3, _WEEKDAY_LETTERS[cur.weekday()])                        # C יום
        ws.cell(row_ptr, 4, assigned_by_date_shift.get((d_iso, "ת.מיון"), ""))  # D
        ws.cell(row_ptr, 5, assigned_by_date_shift.get((d_iso, "ת.מיון 2"), "")) # E
        ws.cell(row_ptr, 6, assigned_by_date_shift.get((d_iso, "כונן מיון"), ""))  # F

        night_blocked = ", ".join(nights_off_by_date.get(d_iso, [])) if nights_off_by_date else ""
        ws.cell(row_ptr, 18, night_blocked)                                         # R

        # --- NEW LOGIC FOR COLUMN H ---
        worker_list = "$I$13:$I$23"
        
        if row_ptr == 4:
            search_target = f'D{row_ptr} & " " & E{row_ptr} & " " & R{row_ptr}'
        else:
            search_target = f'D{row_ptr} & " " & E{row_ptr} & " " & D{row_ptr-1} & " " & E{row_ptr-1} & " " & R{row_ptr}'

        # Using _xlfn. and ArrayFormula to prevent the openpyxl '@' bug
        formula = f'=_xlfn.TEXTJOIN(", ", TRUE, _xlfn._xlws.FILTER({worker_list}, ISERROR(SEARCH({worker_list}, {search_target})), ""))'
        
        h_cell = ws.cell(row_ptr, 8)
        h_cell.value = ArrayFormula(f"H{row_ptr}", formula)
        h_cell.alignment = Alignment(wrap_text=True, horizontal="right", vertical="top", readingOrder=2)
        # ------------------------------

        row_ptr += 1
        cur += timedelta(days=1)

    last_row = row_ptr - 1

    # Style main table B:F, and H
    for r in range(3, last_row + 1):
        for c in [2, 3, 4, 5, 6, 8]:  # Skip 7 (G)
            cell = ws.cell(r, c)

            try:
                d = datetime.strptime(str(ws.cell(r, 2).value), "%d/%m/%Y").date()
            except Exception:
                d = None

            if d:
                if d.weekday() == 4 or (d - timedelta(days=1)) in holidays:
                    cell.fill = ORANGE
                elif d.weekday() == 5 or d in holidays:
                    cell.fill = YELLOW

            left = thick if c in (start_col, 8) else thin
            right = thick if c in (start_col + 4, 8) else thin
            top = thick if r == 3 else thin
            bottom = thick if r == last_row else thin
            cell.border = Border(left=left, right=right, top=top, bottom=bottom)

            cell.alignment = Alignment(
                horizontal="center" if r == 3 else ("center" if c in (2, 3) else "right"),
                vertical="center" if r == 3 else "top",
                wrap_text=True,
                readingOrder=2,
            )

    for c in [2, 3, 4, 5, 6, 8]:
        ws.cell(3, c).border = Border(
            left=thick if c in (start_col, 8) else thin,
            right=thick if c in (start_col + 4, 8) else thin,
            top=thick,
            bottom=thin,
        )

    # Style helper column R
    for r in range(3, last_row + 1):
        cell = ws.cell(r, 18)

        try:
            d = datetime.strptime(str(ws.cell(r, 2).value), "%d/%m/%Y").date()
        except Exception:
            d = None

        if d:
            if d.weekday() == 4 or (d - timedelta(days=1)) in holidays:
                cell.fill = ORANGE
            elif d.weekday() == 5 or d in holidays:
                cell.fill = YELLOW

        cell.border = Border(
            left=thick if r == 3 else thin,
            right=thick,
            top=thick if r == 3 else thin,
            bottom=thick if r == last_row else thin,
        )

        cell.alignment = Alignment(
            horizontal="center" if r == 3 else "right",
            vertical="center" if r == 3 else "top",
            wrap_text=True,
            readingOrder=2,
        )

        if r == 3:
            cell.font = Font(bold=True)

    ws.freeze_panes = ws["C4"]

def _build_sheet_ovdim(ws):
    """
    Sheet 'עובדים':
    3 columns total — A: left list, B: empty spacer, C: header + right list.
    Header (C1): 'לא נכללים בבדיקה'
    """
    # Data (left column A, right column C)
    left_names = [
        "ברג", "ברטל", "גלינסקיה", "גנדלמן",
        "שמואל", "הסר", "כהן", "לקן", "דקל",
        "עסלי", "פריאנטה", "קימיאגר", "קינן", "סעוב", "ארדשירוב", "הרש", "שמעון"
    ]
    right_names = [
        "ערמון", "מיניוביץ'", "טולצ'ינסקי", "חדיג'ה",
        "פרץ", "אגאג'ני", "פרחובה",
    ]

    # Header in C1
    ws.cell(1, 3, "לא נכללים בבדיקה").font = Font(bold=True)
    ws.cell(1, 3).alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)

    # Column widths
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 4
    ws.column_dimensions["C"].width = 22

    # Write left names in A1..A12
    for i, name in enumerate(left_names, start=1):
        cell = ws.cell(i, 1, name)
        cell.alignment = Alignment(horizontal="right", vertical="center", readingOrder=2)

    # Write right names in C2.. downward
    for i, name in enumerate(right_names, start=2):
        cell = ws.cell(i, 3, name)
        cell.alignment = Alignment(horizontal="right", vertical="center", readingOrder=2)

    # Optional: thin borders around the used cells (A1:A12 and C1:C13), RTL sheet
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for r in range(1, max(len(left_names), len(right_names) + 1) + 1):  # +1 to include header row in C
        ws.cell(r, 1).border = border  # A
        ws.cell(r, 3).border = border  # C

    ws.sheet_view.rightToLeft = True

def _build_sheet_fridays(
    ws,
    year: int,
    mon: int,
    *,
    friday_refs_by_date: Dict[str, Sequence[str]] | None = None,
):
    """
    'ימי שישי' sheet:
      - Row 1: empty
      - Row 2: centered headline: 'ימי שישי <HebMonth YY>' (merged A2:E2)
      - Row 3: empty spacer
      - Row 4: header row with Friday dates (DD/MM/YYYY)
      - Row 5: live de-duplicated names from selected Friday rows in <YYYY-MM>
      - Rows 6–9: reserved assignment rows
      - Orange background for the whole table, RTL text, thin grid borders
    """
    friday_refs_by_date = friday_refs_by_date or {}
    ws.sheet_view.rightToLeft = True

    # Headline in row 2 (merged A2:E2 only)
    headline = f"ימי שישי {_heb_month_name(year, mon)}"
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=5)
    hcell = ws.cell(2, 1, headline)
    hcell.font = Font(bold=True, size=14)
    hcell.alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)

    # Find all Fridays in the month
    first = date(year, mon, 1)
    fridays = []
    cur = first
    while cur.month == mon:
        if cur.weekday() == 4:  # Friday
            fridays.append(cur)
        cur += timedelta(days=1)

    # Set column widths
    for j in range(1, len(fridays) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 18

    # Header row (row 4)
    for j, dte in enumerate(fridays, start=1):
        cell = ws.cell(4, j, dte.strftime("%d/%m/%Y"))
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)

    # Linked names row + reserved rows underneath
    for c, dte in enumerate(fridays, start=1):
        linked_cell = ws.cell(5, c)
        linked_cell.value = _friday_unique_names_formula(
            friday_refs_by_date.get(dte.isoformat(), [])
        )
        linked_cell.alignment = Alignment(
            horizontal="right", vertical="top", wrap_text=True, readingOrder=2
        )

    ws.row_dimensions[5].height = 95

    for r in range(6, 10):
        for c in range(1, len(fridays) + 1):
            ws.cell(r, c, "").alignment = Alignment(
                horizontal="right", vertical="top", wrap_text=True, readingOrder=2
            )

    # Orange fill and borders for table
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for r in range(4, 10):
        for c in range(1, len(fridays) + 1):
            cell = ws.cell(r, c)
            cell.fill = _ORANGE
            cell.border = border

# ================================
# CHANGED: added main_ref_by_date
# ================================
def _build_sheet_yoatzim(
    ws,
    roster_df: pd.DataFrame,
    month: str,
    holidays: Sequence[date] = (),                  # תאריכי חופש (חג)
    holidays_named: Dict[date, str] | None = None,  # אופציונלי: שם החג לכל תאריך
    main_ref_by_date: Dict[str, str] | None = None  # NEW: date_iso -> "'YYYY-MM'!C$10"
):
    """
    'ייעוצים' – כותרת בשורה 1, שורה 2 ריקה, טבלה מ-C3:
      C: תאריך (DD/MM/YYYY)
      D: יום (מלא; מוסיף "(ערב חג)" או "(<שם החג>)" אם רלוונטי)
      E: יועץ (מתוך Shift == 'ייעוצים מובילים')
    צבעים:
      - שישי/שבת → כתום
      - ערב חג/חג (חופש) → כחול
      עדיפות צבע כחול על כתום.

    If main_ref_by_date is provided, column E writes Excel formulas that reference the
    corresponding cell in the main month calendar sheet, e.g.:
      E4 = ='2025-12'!C$10
    """
    yr, mon = map(int, month.split("-"))
    ws.sheet_view.rightToLeft = True

    # כותרת (שורה 1)
    title = f"ייעוצים {_heb_month_name(yr, mon)}"
    ws.merge_cells(start_row=1, start_column=3, end_row=1, end_column=5)  # C..E
    tcell = ws.cell(1, 3, title)
    tcell.font = Font(bold=True, size=14)
    tcell.alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)

    # כותרות הטבלה (שורה 3)
    headers = ["תאריך", "יום", "יועץ"]
    for i, h in enumerate(headers, start=3):  # C,D,E
        cell = ws.cell(3, i, h)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)
        ws.column_dimensions[get_column_letter(i)].width = 20 if i != 4 else 14  # יום צר יותר

    # שליפת הנתונים לאותו חודש
    subset = roster_df[
        (roster_df["Date"].str.startswith(month)) &
        (roster_df["Shift"] == "ייעוצים מובילים")
    ].copy()
    subset.sort_values("Date", inplace=True)

    # בניית השורות החל מ-C4
    r = 4
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    holidays_set = set(holidays or [])
    name_map = holidays_named or {}

    for _, row in subset.iterrows():
        d_iso = str(row["Date"])  # "YYYY-MM-DD"
        d = datetime.fromisoformat(d_iso).date()
        weekday_full = _WEEKDAY_FULL[d.weekday()]

        # תיוג ערב חג/חג בטור "יום"
        label = ""
        if d in holidays_set:
            label = f" ({name_map.get(d, 'חג')})"
        elif (d - timedelta(days=1)) in holidays_set:
            label = " (ערב חג)"
        day_text = f"{weekday_full}{label}"

        ws.cell(r, 3, d.strftime("%d/%m/%Y"))  # תאריך
        ws.cell(r, 4, day_text)               # יום

        ref = main_ref_by_date.get(d_iso) if main_ref_by_date else None
        if ref:
            ws.cell(r, 5, f"={ref}")          # e.g. "='2025-12'!C$10"
        else:
            names = ", ".join(_names(str(row["Assigned"]))) or "-"
            ws.cell(r, 5, names)

        for c in range(3, 6):
            cell = ws.cell(r, c)
            cell.border = border
            cell.alignment = Alignment(
                horizontal="right" if c != 3 else "center",
                vertical="center",
                wrap_text=True,
                readingOrder=2
            )

        # צבעים: כחול גובר על כתום
        if d in holidays_set or (d - timedelta(days=1)) in holidays_set:
            fill = _BLUE
        elif d.weekday() in (4, 5):  # Fri/Sat
            fill = _ORANGE
        else:
            fill = None

        if fill:
            for c in range(3, 6):
                ws.cell(r, c).fill = fill

        r += 1

    for c in range(3, 6):
        ws.cell(3, c).border = border
        ws.cell(3, c).alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)

    ws.freeze_panes = ws["C4"]

# ──────────────────────────────────────────────────────────────
#  public helpers
# ──────────────────────────────────────────────────────────────
def export_week_to_xlsx(
    roster_df: pd.DataFrame,
    week_start: str,
    *,
    out_dir: str | Path = _OUT_DIR,
    fname: str | None = None,
    unassigned_by_date: Dict[str, Sequence[str | tuple[str, str]]] | None = None,
    days_off_by_date: Dict[str, Sequence[str]] | None = None
) -> Path:
    """
    Export one calendar week (Sun…Sat) to a standalone XLSX file.
    """
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)

    if unassigned_by_date is None:
        unassigned_by_date = _auto_unassigned(roster_df)
    if days_off_by_date is None:
        days_off_by_date = _auto_days_off()

    sunday    = _sunday_of(datetime.fromisoformat(week_start).date())
    days      = _week_range(sunday)
    pivot     = _pivot_for_days(
        roster_df,
        [d.isoformat() for d in days],
        days_off_by_date=days_off_by_date,
    )
    export_df = _build_calendar_df(pivot, days, unassigned_by_date)

    out_path = out_dir / (fname or f"roster_{sunday.isoformat()}.xlsx")
    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        export_df.to_excel(xw, sheet_name="Week", index=False, header=False)
        xw.sheets["Week"].sheet_view.rightToLeft = True
    return out_path

def export_month_to_xlsx(
    roster_df: pd.DataFrame,
    month: str,            # "YYYY-MM"
    *,
    out_dir: str | Path = _OUT_DIR,
    fname: str | None = None,
    unassigned_by_date: Dict[str, Sequence[str | tuple[str, str]]] | None = None,
    days_off_by_date: Dict[str, Sequence[str]] | None = None,
    template_path: str | Path = _XLSM_TEMPLATE,
) -> Path:
    """
    Export a macro-enabled workbook (.xlsm) by loading a VBA template and
    overwriting the relevant sheet contents while preserving existing VBA.

    Sheets:
      1) <YYYY-MM> — full month calendar (stacked weeks)
      2) תורנויות
      3) עובדים
      4) ימי שישי
      5) ייעוצים
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    template_path = Path(template_path)
    if not template_path.exists():
        raise FileNotFoundError(f"XLSM template not found: {template_path}")

    if unassigned_by_date is None:
        unassigned_by_date = _auto_unassigned(roster_df)
    if days_off_by_date is None:
        days_off_by_date = _auto_days_off()
    nights_off_by_date = _auto_nights_off()

    yr, mon = map(int, month.split("-"))
    first = date(yr, mon, 1)
    first_su = _sunday_of(first)

    weeks: List[List[date]] = []
    cur = first_su
    while cur.month == mon or (cur + timedelta(days=6)).month == mon:
        weeks.append(_week_range(cur))
        cur += timedelta(days=7)

    # ----------------------------------------------------------
    # Load VBA container template
    # ----------------------------------------------------------
    wb = load_workbook(template_path, keep_vba=True)
    wb.calculation.calcMode = "auto"
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True

    # Reuse the existing template month sheet object to preserve worksheet VBA
    ws = _find_template_month_sheet(wb)
    if ws.title != month:
        ws.title = month
    _prepare_month_sheet(ws)

    # Get existing fixed sheets by name and reuse them as well
    ws_toranut = wb["תורנויות"]
    ws_ovdim = wb["עובדים"]
    ws_fridays = wb["ימי שישי"]
    ws_yoatzim = wb["ייעוצים"]

    _prepare_toranut_sheet(ws_toranut)
    _prepare_ovdim_sheet(ws_ovdim)
    _prepare_fridays_sheet(ws_fridays)
    _prepare_yoatzim_sheet(ws_yoatzim)

    # ==========================================================
    # Build mapping date_iso -> main month-sheet cell refs
    # used by ייעוצים and אחרי תורנות formulas
    # ==========================================================
    yoatz_ref_by_date: Dict[str, str] = {}
    toren_mion_ref_by_date: Dict[str, str] = {}
    toren_machlaka_ref_by_date: Dict[str, str] = {}
    after_cell_pos_by_date: Dict[str, tuple[int, int]] = {}
    friday_refs_by_date: Dict[str, List[str]] = {}

    yoatz_row_off = 2 + SHIFT_ORDER.index("ייעוצים מובילים")
    toren_mion_row_off = 2 + SHIFT_ORDER.index("ת.מיון")
    toren_mach_row_off = 2 + SHIFT_ORDER.index("ת.מיון 2")
    after_row_off = 2 + SHIFT_ORDER.index("אחרי תורנות")
    friday_row_offsets = {
        shift: 2 + SHIFT_ORDER.index(shift)
        for shift in FRIDAY_LINK_SHIFTS
    }

    row_ptr = 1
    for seven in weeks:
        block_start = row_ptr

        yoatz_row = block_start + yoatz_row_off
        tmion_row = block_start + toren_mion_row_off
        tmach_row = block_start + toren_mach_row_off
        after_row = block_start + after_row_off

        for i, dte in enumerate(seven):
            col = 2 + i  # B..H
            col_letter = get_column_letter(col)
            d_iso = dte.isoformat()

            yoatz_ref_by_date[d_iso] = f"'{month}'!{col_letter}${yoatz_row}"
            toren_mion_ref_by_date[d_iso] = f"'{month}'!{col_letter}${tmion_row}"
            toren_machlaka_ref_by_date[d_iso] = f"'{month}'!{col_letter}${tmach_row}"
            after_cell_pos_by_date[d_iso] = (after_row, col)
            if dte.month == mon and dte.weekday() == 4:
                friday_refs_by_date[d_iso] = [
                    f"'{month}'!{col_letter}${block_start + row_off}"
                    for row_off in friday_row_offsets.values()
                ]

        pivot = _pivot_for_days(
            roster_df,
            [d.isoformat() for d in seven],
            month_filter=month,
            days_off_by_date=days_off_by_date,
        )

        block_df = _build_calendar_df(pivot, seven)

        grey_cols = [1 + i for i, d in enumerate(seven, start=1) if d.month != mon]

        row_ptr = _write_block(ws, row_ptr, block_df, seven, grey_cols=grey_cols)

        # Reinsert the Excel formula row for 'לא שובצו'
        _set_unassigned_formula_row(
            ws,
            block_start=block_start,
            seven_dates=seven,
            mon=mon,
        )

        # Rebuild lookup formulas on the main month sheet after the block was written
        _set_main_month_lookup_formulas(ws, block_start=block_start, seven_dates=seven)

        row_ptr += 2  # one for 'לא שובצו' row, one spacer

    # ----------------------------------------------------------
    # Fill "אחרי תורנות" row formulas:
    # = yesterday's (ת.מיון + ת.מיון 2), comma-separated
    # ----------------------------------------------------------
    def _blank_if_dash_or_empty(ref: str) -> str:
        return f'IF(OR({ref}="-",{ref}=""),"",{ref})'

    for d_iso, (r, c) in after_cell_pos_by_date.items():
        if not d_iso.startswith(month):
            continue

        d = date.fromisoformat(d_iso)
        prev_iso = (d - timedelta(days=1)).isoformat()

        ref_mion = toren_mion_ref_by_date.get(prev_iso)
        ref_mach = toren_machlaka_ref_by_date.get(prev_iso)

        cell = ws.cell(r, c)

        if not ref_mion and not ref_mach:
            cell.value = "-"
            continue

        if ref_mion is None:
            ref_mion = '""'
        if ref_mach is None:
            ref_mach = '""'

        a = _blank_if_dash_or_empty(ref_mion)
        b = _blank_if_dash_or_empty(ref_mach)

        cell.value = (
            f'=IF(AND(OR({ref_mion}="-",{ref_mion}=""),OR({ref_mach}="-",{ref_mach}="")),"-",'
            f'{a}&IF(AND(NOT(OR({ref_mion}="-",{ref_mion}="")),NOT(OR({ref_mach}="-",{ref_mach}=""))),", ","")&{b})'
        )

    ws.freeze_panes = ws["A2"]

    # ----------------------------------------------------------
    # Rebuild the fixed sheets in-place
    # ----------------------------------------------------------
    _build_sheet_toranut(ws_toranut, yr, mon, roster_df, nights_off_by_date=nights_off_by_date)
    _build_sheet_ovdim(ws_ovdim)
    _build_sheet_fridays(ws_fridays, yr, mon, friday_refs_by_date=friday_refs_by_date)
    _build_sheet_yoatzim(ws_yoatzim, roster_df, month, main_ref_by_date=yoatz_ref_by_date)

    out_path = out_dir / _ensure_xlsm_name(month, fname)
    _save_workbook_atomic(wb, out_path)
    return out_path

__all__ = ["export_week_to_xlsx", "export_month_to_xlsx"]
