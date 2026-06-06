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
from openpyxl.formatting.rule import FormulaRule
from openpyxl.workbook import Workbook
from openpyxl.worksheet.formula import ArrayFormula

from core.elig_utils import workers_df, unavail_lookup   # auto-build of unassigned lists
from core.data import backend_tables
from core.holiday_utils import holiday_names_from_tables
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
# formatted Excel template
_XLSM_TEMPLATE = _PROJECT_ROOT / "templates" / "neuroshift_template.xlsm"

_FIXED_TEMPLATE_SHEETS = {"תורנויות", "עובדים", "ימי שישי", "ייעוצים"}

#  fills
_ORANGE = PatternFill("solid", fgColor="FFD99B")  # Fri/Sat
_GREY   = PatternFill("solid", fgColor="E6E6E6")  # out-of-month
_BLUE   = PatternFill("solid", fgColor="CDEBF7")  # holidays / erev-chag (ייעוצים sheet)
_DUP_RED_FILL = PatternFill("solid", fgColor="FFC7CE")
_DUP_RED_FONT = Font(color="9C0006")

TORANUT_SENIORS: List[str] = [
    "ברטל", "גנדלמן", "כהן", "פרץ", "קימיאגר", "קינן", "הרש", "שמעון",
]
TORANUT_RESIDENTS: List[str] = [
    "ברג", "גלינסקיה", "דקל", "הסר", "לקן", "עסלי", "פריאנטה",
    "שמואל", "חדיג'ה", "ארדשירוב", "סעוב",
]

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

def _holiday_label(d: date, holiday_names: Dict[date, str]) -> str:
    if d in holiday_names:
        return holiday_names[d]
    next_name = holiday_names.get(d + timedelta(days=1))
    if next_name:
        return f"ערב {next_name}"
    return ""

def _weekday_header(d: date, holiday_names: Dict[date, str]) -> str:
    label = _holiday_label(d, holiday_names)
    weekday = _WEEKDAY_LETTERS[d.weekday()]
    return f"{weekday}\n{label}" if label else weekday

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
    unassigned: Dict[str, Sequence[str | tuple[str, str]]] | None = None,
    holiday_names: Dict[date, str] | None = None,
) -> pd.DataFrame:
    """Return DF = 2-row header + body + optional ‘un-assigned’ row."""
    holiday_names = holiday_names or {}
    date_cols = [d.strftime("%d/%m/%Y") for d in seven_dates]
    cols      = ["תפקיד"] + date_cols

    # Weekday first (bolded top row) then date row
    hdr1 = [""] + [_weekday_header(d, holiday_names) for d in seven_dates]
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
    grey_cols: Sequence[int] = (),
    holiday_cols: Sequence[int] = (),
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
            elif c in holiday_cols:
                cell.fill = _BLUE
            elif c in highlight_cols:
                cell.fill = _ORANGE

def _write_block(
    ws,
    start_row: int,
    df_block: pd.DataFrame,
    seven_dates: List[date],
    grey_cols: Sequence[int] = (),
    holiday_names: Dict[date, str] | None = None,
) -> int:
    """Write *df_block* (top-left at A<start_row>) – return next free row."""
    holiday_names = holiday_names or {}
    n_rows, n_cols = df_block.shape

    # write values
    for r_off, (_, row_vals) in enumerate(df_block.iterrows()):
        for c_off, val in enumerate(row_vals):
            ws.cell(start_row + r_off, 1 + c_off, val)

    # which visible columns are Fri/Sat? (skip “תפקיד” which is col 1)
    highlight = [
        1 + i
        for i, d in enumerate(seven_dates, start=1)
        if d.weekday() in (4, 5) or (d + timedelta(days=1)) in holiday_names
    ]
    holiday_cols = [
        1 + i
        for i, d in enumerate(seven_dates, start=1)
        if d in holiday_names
    ]

    _style_block(ws, start_row, 1, n_rows, n_cols,
                 highlight_cols=highlight,
                 grey_cols=grey_cols,
                 holiday_cols=holiday_cols)
    return start_row + n_rows

# ----------  Extra sheet builders  ----------------------------
def _new_sheet(wb: Workbook, title: str):
    ws = wb.create_sheet(title=title)
    ws.sheet_view.rightToLeft = True
    return ws

def _find_template_month_sheet(wb: Workbook):
    """
    Return the single non-fixed sheet in the template.
    This is the template month sheet that must be reused, not deleted.
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
    Keeps the worksheet object itself intact, preserving template formatting/settings.
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
    Clear the exported calendar area and the generated summary table.
    """
    _clear_range(ws, 1, 1, ws.max_row, 8, clear_merges=True)   # A:H only
    _clear_range(ws, 1, 9, ws.max_row, 19, clear_merges=False)  # I:S summary
    ws.sheet_view.rightToLeft = True
    ws.freeze_panes = None

def _prepare_toranut_sheet(ws) -> None:
    """
    Clear only the areas owned by the Python exporter:
    - main toranut table in B:F
    - available-worker/formula area in H
    - generated counting/formula area in I:Q
    - helper night-unavailability column in R
    """
    _clear_range(ws, 1, 2, ws.max_row, 6, clear_merges=True)  # B:F
    _clear_range(ws, 1, 8, ws.max_row, 8, clear_merges=False)  # H:H
    _clear_range(ws, 1, 9, ws.max_row, 17, clear_merges=False)  # I:Q
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
    This reduces the chance of ending up with a corrupted workbook if saving fails midway.
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

_FRIDAY_TOKEN_WIDTH = 99
_FRIDAY_MAX_NAMES_PER_SOURCE = 4
_FRIDAY_VISIBLE_NAME_ROWS = 6

def _friday_name_token_expr(source_ref: str, token_index: int) -> str:
    """Return an old-Excel-compatible formula fragment for one comma-separated name."""
    start = 1 + (token_index * _FRIDAY_TOKEN_WIDTH)
    return (
        f'TRIM(MID(SUBSTITUTE({source_ref},",",'
        f'REPT(" ",{_FRIDAY_TOKEN_WIDTH})),{start},{_FRIDAY_TOKEN_WIDTH}))'
    )

def _friday_link_formula(
    source_ref: str,
    token_index: int,
    col_letter: str,
    row_num: int,
    dedupe_start_row: int,
) -> str:
    """
    Link one visible Friday-sheet cell to one name token in the month sheet.
    Duplicate names already shown above in the same Friday column are suppressed.
    """
    token = _friday_name_token_expr(source_ref, token_index)
    blank_or_dash = f'OR({token}="",{token}="-")'
    if row_num <= dedupe_start_row:
        return f'=IFERROR(IF({blank_or_dash},"",{token}),"")'

    previous_names = f"{col_letter}${dedupe_start_row}:{col_letter}{row_num - 1}"
    return (
        f'=IFERROR(IF({blank_or_dash},"",'
        f'IF(COUNTIF({previous_names},{token})=0,{token},"")),"")'
    )

def _friday_visible_name_formula(
    col_letter: str,
    helper_start_row: int,
    helper_end_row: int,
    visible_start_row: int,
    visible_row: int,
) -> str:
    """
    Pick the next non-empty hidden helper name using only scalar references.

    Excel adds the implicit-intersection '@' operator to some generated range
    formulas. These nested scalar checks avoid that while keeping live links to
    the month sheet through the hidden helper rows.
    """
    previous_visible = [
        f"{col_letter}${r}"
        for r in range(visible_start_row, visible_row)
    ]

    expr = '""'
    for helper_row in range(helper_end_row, helper_start_row - 1, -1):
        helper_ref = f"{col_letter}${helper_row}"
        checks = [f'{helper_ref}<>""']
        checks.extend(f"{helper_ref}<>{prev}" for prev in previous_visible)
        expr = f'IF(AND({",".join(checks)}),{helper_ref},{expr})'

    return f'=IFERROR({expr},"")'

def _duplicate_name_formula(
    cell_ref: str,
    column_range: str,
    *,
    max_tokens: int = 6,
) -> str:
    """
    Conditional-formatting formula for a comma-separated name cell.
    It turns true when any token in the current cell appears in another
    assignment cell in the same date column.
    """
    normalized_column = f'SUBSTITUTE(TEXTJOIN(",",TRUE,{column_range}),", ",",")'
    checks = []
    for token_index in range(max_tokens):
        token = _friday_name_token_expr(cell_ref, token_index)
        checks.append(
            f'AND({token}<>"",'
            f'SUMPRODUCT(--ISNUMBER(SEARCH(","&{token}&",",","&{normalized_column}&",")))>1)'
        )
    return f"OR({','.join(checks)})"

def _add_duplicate_name_conditional_formatting(
    ws,
    *,
    block_start: int,
    seven_dates: Sequence[date],
    mon: int,
) -> None:
    first_body_row = block_start + 2
    last_body_row = first_body_row + len(SHIFT_ORDER) - 1

    for i, dte in enumerate(seven_dates):
        if dte.month != mon:
            continue

        col = 2 + i  # B..H
        col_letter = get_column_letter(col)
        column_range = f"{col_letter}${first_body_row}:{col_letter}${last_body_row}"
        cf_range = f"{col_letter}{first_body_row}:{col_letter}{last_body_row}"
        formula = _duplicate_name_formula(
            f"{col_letter}{first_body_row}",
            column_range,
        )

        ws.conditional_formatting.add(
            cf_range,
            FormulaRule(formula=[formula], fill=_DUP_RED_FILL, font=_DUP_RED_FONT),
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
    holiday_names: Dict[date, str] | None = None,
) -> None:
    """
    Write the Excel formula row for '⚠️ לא שובצו' under one weekly block.
    """
    row_num = block_start + 2 + len(SHIFT_ORDER)
    holiday_names = holiday_names or {}

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
        elif d in holiday_names:
            cell.fill = _BLUE
        elif d.weekday() in (4, 5):
            cell.fill = _ORANGE
        elif d + timedelta(days=1) in holiday_names:
            cell.fill = _ORANGE

def _summary_names() -> List[str]:
    return TORANUT_SENIORS + TORANUT_RESIDENTS

def _formula_ref_list(refs: Sequence[str]) -> str:
    return ",".join(refs) if refs else '""'

def _count_name_in_refs_formula(name_ref: str, refs: Sequence[str]) -> str:
    if not refs:
        return "=0"

    joined_refs = _formula_ref_list(refs)
    return (
        f'=_xlfn.LET('
        f'_xlpm.joined,SUBSTITUTE(_xlfn.TEXTJOIN(",",TRUE,{joined_refs}),CHAR(10),","),'
        f'_xlpm.raw,_xlfn.TEXTSPLIT(_xlpm.joined,","),'
        f'_xlpm.tokens,_xlfn._xlws.FILTER(TRIM(_xlpm.raw),TRIM(_xlpm.raw)<>""),'
        f'IFERROR(SUMPRODUCT(--(_xlpm.tokens={name_ref})),0)'
        f')'
    )

def _count_name_in_ref_groups_formula(name_ref: str, ref_groups: Sequence[Sequence[str]]) -> str:
    group_checks: List[str] = []
    for refs in ref_groups:
        if not refs:
            continue
        joined_refs = _formula_ref_list(refs)
        group_checks.append(
            f'IFERROR(--ISNUMBER(_xlfn.XMATCH('
            f'{name_ref},'
            f'TRIM(_xlfn.TEXTSPLIT(SUBSTITUTE(_xlfn.TEXTJOIN(",",TRUE,{joined_refs}),CHAR(10),","),","))'
            f')),0)'
        )

    if not group_checks:
        return "=0"
    return f'=_xlfn.LET(_xlpm.name,{name_ref},SUM({",".join(group_checks)}))'

def _apply_summary_table_style(
    ws,
    *,
    min_row: int,
    max_row: int,
    min_col: int,
    max_col: int,
    name_col: int,
) -> None:
    header_fill = PatternFill("solid", fgColor="D9EAD3")
    senior_fill = PatternFill("solid", fgColor="CDEBF7")
    resident_fill = PatternFill("solid", fgColor="C6EFCE")
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for row in range(min_row, max_row + 1):
        for col in range(min_col, max_col + 1):
            cell = ws.cell(row, col)
            cell.border = border
            cell.alignment = Alignment(
                horizontal="center" if row == min_row or col != name_col else "right",
                vertical="center",
                wrap_text=True,
                readingOrder=2,
            )
            if row == min_row:
                cell.font = Font(bold=True)
                cell.fill = header_fill
            elif col == name_col:
                cell.fill = senior_fill if row < min_row + 1 + len(TORANUT_SENIORS) else resident_fill

def _write_array_formula(cell, formula: str) -> None:
    cell.value = ArrayFormula(cell.coordinate, formula)

def _build_month_summary_table(
    ws,
    *,
    month: str,
    refs_by_metric: Dict[str, Sequence[str]],
    friday_ref_groups: Sequence[Sequence[str]],
) -> None:
    headers = [
        "שם",
        "ייעוצים",
        "ימי שישי",
        "מחקר",
        "EEG",
        "חופש (א-ה)",
        "רוטציה",
        "תורנויות",
        "כוננויות",
        "לא שובצו",
    ]
    start_row = 3
    start_col = 10  # J

    for offset, header in enumerate(headers):
        ws.cell(start_row, start_col + offset, header)
        ws.column_dimensions[get_column_letter(start_col + offset)].width = 14
    ws.column_dimensions["J"].width = 18
    ws.column_dimensions["O"].width = 16
    ws.column_dimensions["S"].width = 16

    metric_to_col = {
        "ייעוצים": 11,
        "ימי שישי": 12,
        "מחקר": 13,
        "EEG": 14,
        "חופש": 15,
        "רוטציה": 16,
        "תורנויות": 17,
        "כוננויות": 18,
        "לא שובצו": 19,
    }

    for row_offset, name in enumerate(_summary_names(), start=1):
        row = start_row + row_offset
        name_ref = f"$J{row}"
        ws.cell(row, 10, name)

        for metric, col in metric_to_col.items():
            cell = ws.cell(row, col)
            if metric == "ימי שישי":
                _write_array_formula(cell, _count_name_in_ref_groups_formula(name_ref, friday_ref_groups))
            else:
                _write_array_formula(cell, _count_name_in_refs_formula(name_ref, refs_by_metric.get(metric, [])))

    _apply_summary_table_style(
        ws,
        min_row=start_row,
        max_row=start_row + len(_summary_names()),
        min_col=start_col,
        max_col=start_col + len(headers) - 1,
        name_col=10,
    )

def _build_toranut_summary_table(ws, *, first_day_row: int, last_day_row: int) -> None:
    blue = PatternFill("solid", fgColor="CDEBF7")
    green = PatternFill("solid", fgColor="C6EFCE")
    header_fill = PatternFill("solid", fgColor="D9EAD3")
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    headers = ["", "כ.מיון", "ת.מיון", "ת.מיון 2", "שישי/שבת", "ימי רביעי", "ימי חמישי", "סנדוויץ'", 'סה"כ']
    for offset, header in enumerate(headers):
        col = 9 + offset
        cell = ws.cell(4, col, header)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True, readingOrder=2)
        cell.border = border
        ws.column_dimensions[get_column_letter(col)].width = 13
    ws.column_dimensions["I"].width = 18
    ws.column_dimensions["M"].width = 14
    ws.column_dimensions["P"].width = 14
    ws.column_dimensions["Q"].width = 10

    rows_by_name: Dict[str, int] = {}
    row = 5
    for name in _summary_names():
        rows_by_name[name] = row
        cell = ws.cell(row, 9, name)
        cell.fill = blue if name in TORANUT_SENIORS else green
        cell.alignment = Alignment(horizontal="right", vertical="center", readingOrder=2)
        cell.border = border
        row += 1

    date_rows: Dict[date, int] = {}
    for r in range(first_day_row, last_day_row + 1):
        try:
            d = datetime.strptime(str(ws.cell(r, 2).value), "%d/%m/%Y").date()
        except Exception:
            continue
        date_rows[d] = r

    all_rows = list(range(first_day_row, last_day_row + 1))
    weekend_rows = [r for d, r in date_rows.items() if d.weekday() in (4, 5)]
    wed_rows = [r for d, r in date_rows.items() if d.weekday() == 2]
    thu_rows = [r for d, r in date_rows.items() if d.weekday() == 3]

    def refs_for(rows: Sequence[int], cols: Sequence[int]) -> List[str]:
        return [f"${get_column_letter(c)}${r}" for r in rows for c in cols]

    for name, row in rows_by_name.items():
        name_ref = f"$I{row}"
        formulas = {
            10: _count_name_in_refs_formula(name_ref, refs_for(all_rows, [6])),           # J כ.מיון
            11: _count_name_in_refs_formula(name_ref, refs_for(all_rows, [4])),           # K ת.מיון
            12: _count_name_in_refs_formula(name_ref, refs_for(all_rows, [5])),           # L ת.מיון 2
            13: _count_name_in_refs_formula(name_ref, refs_for(weekend_rows, [4, 5, 6])), # M שישי/שבת
            14: _count_name_in_refs_formula(name_ref, refs_for(wed_rows, [4, 5])),        # N ימי רביעי
            15: _count_name_in_refs_formula(name_ref, refs_for(thu_rows, [4, 5])),        # O ימי חמישי
        }

        for col, formula in formulas.items():
            _write_array_formula(ws.cell(row, col), formula)

        if last_day_row - first_day_row >= 2:
            first_sandwich = first_day_row
            last_sandwich = last_day_row - 2
            ws.cell(row, 16).value = (
                f'=SUMPRODUCT('
                f'SIGN(($D${first_sandwich}:$D${last_sandwich}=$I{row})+($E${first_sandwich}:$E${last_sandwich}=$I{row}))*'
                f'(1-SIGN(($D${first_sandwich + 1}:$D${last_sandwich + 1}=$I{row})+($E${first_sandwich + 1}:$E${last_sandwich + 1}=$I{row})))*'
                f'SIGN(($D${first_sandwich + 2}:$D${last_sandwich + 2}=$I{row})+($E${first_sandwich + 2}:$E${last_sandwich + 2}=$I{row}))'
                f')'
            )
        else:
            ws.cell(row, 16).value = 0

        ws.cell(row, 17).value = f"=J{row}+K{row}+L{row}"

        for col in range(10, 18):
            cell = ws.cell(row, col)
            cell.alignment = Alignment(horizontal="center", vertical="center", readingOrder=2)
            cell.border = border

    for r in range(4, row):
        for c in range(9, 18):
            ws.cell(r, c).border = border

def _build_sheet_toranut(
    ws,
    year: int,
    mon: int,
    roster_df: pd.DataFrame,
    holidays: Sequence[date] = (),
    holidays_named: Dict[date, str] | None = None,
    nights_off_by_date: Dict[str, Sequence[str]] | None = None,
):
    """
    Sheet 'תורנויות':
    - Row 1: merged C1:E1 title "תורנויות כוננויות <Month YY>"
    - Row 2: empty spacer row
    - Row 3: headers (B:תאריך, C:יום, D:ת.מיון, E:ת.מיון 2, F:כ.מיון)
    - Rows 4+: one row per day in months
    - D/E/F are populated from roster_df:
        D <- ת.מיון
        E <- ת.מיון 2
        F <- כונן מיון
    - Column H: available resident formula
    - Columns I:Q: preserved summary table from the template
    - Column R: names marked "לא זמין לתורנות" for that date
    """
    title = f"תורנויות כוננויות {_heb_month_name(year, mon)}"
    ws.merge_cells(start_row=1, start_column=3, end_row=1, end_column=5)
    title_cell = ws.cell(1, 3, title)
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

    # The resident list and summary formulas live in the template table at I:Q.
    # Keep that block untouched and reuse the resident-name range for column H.
    worker_list = "$I$13:$I$23"

    # Build lookup from roster_df
    month_prefix = f"{year:04d}-{mon:02d}"
    subset = roster_df[roster_df["Date"].str.startswith(month_prefix)].copy()

    assigned_by_date_shift: Dict[tuple[str, str], str] = {}
    for _, row in subset.iterrows():
        shift = str(row["Shift"])
        if shift not in {"ת.מיון", "ת.מיון 2", "כונן מיון"}:
            continue
        d_iso = str(row["Date"])
        names_list = list(_names(str(row["Assigned"])))
        names = ", ".join(names_list) or "-"
        assigned_by_date_shift[(d_iso, shift)] = names

    ORANGE = PatternFill("solid", fgColor="FFD99B")   # Fri / erev hag
    YELLOW = PatternFill("solid", fgColor="FFF299")   # Sat
    thin = Side(style="thin", color="000000")
    thick = Side(style="medium", color="000000")
    holidays = set(holidays)
    holidays_named = holidays_named or {}

    first = date(year, mon, 1)
    cur = first
    row_ptr = 4
    while cur.month == mon:
        d_iso = cur.isoformat()

        ws.cell(row_ptr, 2, cur.strftime("%d/%m/%Y"))                               # B תאריך
        day_label = _WEEKDAY_LETTERS[cur.weekday()]
        holiday_label = _holiday_label(cur, holidays_named)
        if holiday_label:
            day_label = f"{day_label} - {holiday_label}"

        ws.cell(row_ptr, 3, day_label)                                              # C יום
        ws.cell(row_ptr, 4, assigned_by_date_shift.get((d_iso, "ת.מיון"), ""))  # D
        ws.cell(row_ptr, 5, assigned_by_date_shift.get((d_iso, "ת.מיון 2"), "")) # E
        ws.cell(row_ptr, 6, assigned_by_date_shift.get((d_iso, "כונן מיון"), ""))  # F

        night_blocked = ", ".join(nights_off_by_date.get(d_iso, [])) if nights_off_by_date else ""
        ws.cell(row_ptr, 18, night_blocked)                                         # R

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
                if d in holidays:
                    cell.fill = _BLUE
                elif d.weekday() == 4 or (d + timedelta(days=1)) in holidays:
                    cell.fill = ORANGE
                elif d.weekday() == 5:
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
            if d in holidays:
                cell.fill = _BLUE
            elif d.weekday() == 4 or (d + timedelta(days=1)) in holidays:
                cell.fill = ORANGE
            elif d.weekday() == 5:
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

    _build_toranut_summary_table(ws, first_day_row=4, last_day_row=last_row)
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
      - Rows 5-10: first six live linked names from selected Friday rows in <YYYY-MM>
        one name per cell, with repeats hidden inside each Friday column
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

    visible_start_row = 5
    visible_end_row = visible_start_row + _FRIDAY_VISIBLE_NAME_ROWS - 1
    helper_start_row = visible_end_row + 1
    rows_per_friday = len(FRIDAY_LINK_SHIFTS) * _FRIDAY_MAX_NAMES_PER_SOURCE
    helper_end_row = helper_start_row + rows_per_friday - 1

    # Hidden helper rows split source cells into one de-duplicated name per row.
    for c, dte in enumerate(fridays, start=1):
        col_letter = get_column_letter(c)
        row_num = helper_start_row
        refs = friday_refs_by_date.get(dte.isoformat(), [])

        for source_ref in refs:
            for token_index in range(_FRIDAY_MAX_NAMES_PER_SOURCE):
                cell = ws.cell(row_num, c)
                cell.value = _friday_link_formula(
                    source_ref,
                    token_index,
                    col_letter,
                    row_num,
                    helper_start_row,
                )
                cell.alignment = Alignment(
                    horizontal="right", vertical="top", wrap_text=True, readingOrder=2
                )
                row_num += 1

        while row_num <= helper_end_row:
            cell = ws.cell(row_num, c, "")
            cell.alignment = Alignment(
                horizontal="right", vertical="top", wrap_text=True, readingOrder=2
            )
            row_num += 1

        for r in range(visible_start_row, visible_end_row + 1):
            cell = ws.cell(r, c)
            cell.value = _friday_visible_name_formula(
                col_letter,
                helper_start_row,
                helper_end_row,
                visible_start_row,
                r,
            )
            cell.alignment = Alignment(
                horizontal="right", vertical="top", wrap_text=True, readingOrder=2
            )

    for r in range(visible_start_row, visible_end_row + 1):
        ws.row_dimensions[r].height = 22

    for r in range(helper_start_row, helper_end_row + 1):
        ws.row_dimensions[r].hidden = True

    # Orange fill and borders for table
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for r in range(4, visible_end_row + 1):
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
        elif (d + timedelta(days=1)) in holidays_set:
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
        if d in holidays_set:
            fill = _BLUE
        elif (d + timedelta(days=1)) in holidays_set:
            fill = _ORANGE
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
    Export a macro-free workbook (.xlsx) by loading the formatted template and
    overwriting the relevant sheet contents.

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
    holiday_names = holiday_names_from_tables(backend_tables())
    holidays = set(holiday_names)

    yr, mon = map(int, month.split("-"))
    first = date(yr, mon, 1)
    first_su = _sunday_of(first)

    weeks: List[List[date]] = []
    cur = first_su
    while cur.month == mon or (cur + timedelta(days=6)).month == mon:
        weeks.append(_week_range(cur))
        cur += timedelta(days=7)

    # ----------------------------------------------------------
    # Load the formatted template without preserving VBA.
    # Keeping macros/event handlers clears Excel's Undo stack on open/edit.
    # ----------------------------------------------------------
    wb = load_workbook(template_path, keep_vba=True)
    wb.calculation.calcMode = "auto"
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True

    # Reuse the existing template month sheet object to preserve formatting/settings
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
    month_summary_refs: Dict[str, List[str]] = {
        "ייעוצים": [],
        "מחקר": [],
        "EEG": [],
        "חופש": [],
        "רוטציה": [],
        "תורנויות": [],
        "כוננויות": [],
        "לא שובצו": [],
    }
    month_friday_ref_groups: List[List[str]] = []

    yoatz_row_off = 2 + SHIFT_ORDER.index("ייעוצים מובילים")
    toren_mion_row_off = 2 + SHIFT_ORDER.index("ת.מיון")
    toren_mach_row_off = 2 + SHIFT_ORDER.index("ת.מיון 2")
    konen_mion_row_off = 2 + SHIFT_ORDER.index("כונן מיון")
    after_row_off = 2 + SHIFT_ORDER.index("אחרי תורנות")
    summary_shift_row_offsets = {
        "ייעוצים": 2 + SHIFT_ORDER.index("ייעוצים מובילים"),
        "מחקר": 2 + SHIFT_ORDER.index("מחקר"),
        "EEG": 2 + SHIFT_ORDER.index("EEG"),
        "חופש": 2 + SHIFT_ORDER.index("חופש"),
        "רוטציה": 2 + SHIFT_ORDER.index("רוטציה"),
    }
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
        konen_row = block_start + konen_mion_row_off
        after_row = block_start + after_row_off
        unassigned_row = block_start + 2 + len(SHIFT_ORDER)

        for i, dte in enumerate(seven):
            col = 2 + i  # B..H
            col_letter = get_column_letter(col)
            d_iso = dte.isoformat()

            yoatz_ref_by_date[d_iso] = f"'{month}'!{col_letter}${yoatz_row}"
            toren_mion_ref_by_date[d_iso] = f"'{month}'!{col_letter}${tmion_row}"
            toren_machlaka_ref_by_date[d_iso] = f"'{month}'!{col_letter}${tmach_row}"
            after_cell_pos_by_date[d_iso] = (after_row, col)
            if dte.month == mon:
                for metric, row_off in summary_shift_row_offsets.items():
                    if metric == "חופש" and dte.weekday() not in (6, 0, 1, 2, 3):
                        continue
                    month_summary_refs[metric].append(
                        f"'{month}'!{col_letter}${block_start + row_off}"
                    )

                month_summary_refs["תורנויות"].extend([
                    f"'{month}'!{col_letter}${tmion_row}",
                    f"'{month}'!{col_letter}${tmach_row}",
                ])
                month_summary_refs["כוננויות"].append(f"'{month}'!{col_letter}${konen_row}")

                if dte.weekday() in (6, 0, 1, 2, 3):
                    month_summary_refs["לא שובצו"].append(f"'{month}'!{col_letter}${unassigned_row}")

            if dte.month == mon and dte.weekday() == 4:
                friday_refs_by_date[d_iso] = [
                    f"'{month}'!{col_letter}${block_start + row_off}"
                    for row_off in friday_row_offsets.values()
                ]
                month_friday_ref_groups.append(friday_refs_by_date[d_iso])

        pivot = _pivot_for_days(
            roster_df,
            [d.isoformat() for d in seven],
            month_filter=month,
            days_off_by_date=days_off_by_date,
        )

        block_df = _build_calendar_df(pivot, seven, holiday_names=holiday_names)

        grey_cols = [1 + i for i, d in enumerate(seven, start=1) if d.month != mon]

        row_ptr = _write_block(
            ws,
            row_ptr,
            block_df,
            seven,
            grey_cols=grey_cols,
            holiday_names=holiday_names,
        )

        # Reinsert the Excel formula row for 'לא שובצו'
        _set_unassigned_formula_row(
            ws,
            block_start=block_start,
            seven_dates=seven,
            mon=mon,
        )

        # Rebuild lookup formulas on the main month sheet after the block was written
        _set_main_month_lookup_formulas(ws, block_start=block_start, seven_dates=seven)
        _add_duplicate_name_conditional_formatting(
            ws,
            block_start=block_start,
            seven_dates=seven,
            mon=mon,
        )

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
    _build_month_summary_table(
        ws,
        month=month,
        refs_by_metric=month_summary_refs,
        friday_ref_groups=month_friday_ref_groups,
    )

    # ----------------------------------------------------------
    # Rebuild the fixed sheets in-place
    # ----------------------------------------------------------
    _build_sheet_toranut(
        ws_toranut,
        yr,
        mon,
        roster_df,
        holidays=holidays,
        holidays_named=holiday_names,
        nights_off_by_date=nights_off_by_date,
    )
    _build_sheet_ovdim(ws_ovdim)
    _build_sheet_fridays(
        ws_fridays,
        yr,
        mon,
        friday_refs_by_date=friday_refs_by_date,
    )
    _build_sheet_yoatzim(
        ws_yoatzim,
        roster_df,
        month,
        holidays=holidays,
        holidays_named=holiday_names,
        main_ref_by_date=yoatz_ref_by_date,
    )

    out_path = out_dir / _ensure_xlsm_name(month, fname)
    _save_workbook_atomic(wb, out_path)
    return out_path

__all__ = ["export_week_to_xlsx", "export_month_to_xlsx"]
