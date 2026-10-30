"""Report rendering: one neutral structure (title, key facts, tables) rendered to Excel or PDF.

Builders produce a `ReportData`; renderers know nothing about vendors or RFQs. PDF uses the built-in Helvetica
font, which covers Western European text; characters outside Latin-1 are replaced (the Excel version keeps them).
"""

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list[Any]]
    widths: list[float] | None = None  # relative column widths for the PDF (defaults to equal)
    note: str = ""


@dataclass
class ReportData:
    title: str
    subtitle: str = ""
    facts: list[tuple[str, str]] = field(default_factory=list)  # key facts shown above the tables
    tables: list[Table] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value if v not in (None, ""))
    return value


def to_xlsx(data: ReportData) -> bytes:
    wb = Workbook()
    summary = wb.active
    assert summary is not None
    summary.title = "Summary"
    summary.append([data.title])
    summary["A1"].font = Font(bold=True, size=14)
    if data.subtitle:
        summary.append([data.subtitle])
    summary.append([f"Generated {data.generated_at:%Y-%m-%d %H:%M} UTC"])
    summary.append([])
    for key, value in data.facts:
        summary.append([key, value])
    summary.column_dimensions["A"].width = 32
    summary.column_dimensions["B"].width = 60
    header_fill = PatternFill("solid", fgColor="E8EEF5")
    for i, table in enumerate(data.tables, start=1):
        ws = wb.create_sheet((table.title or f"Table {i}")[:31].replace("/", "-"))
        ws.append(table.columns)
        for col in range(1, len(table.columns) + 1):
            c = ws.cell(row=1, column=col)
            c.font, c.fill = Font(bold=True), header_fill
        for row in table.rows:
            ws.append([_cell(v) for v in row])
        for col, name in enumerate(table.columns, start=1):
            longest = max([len(str(name)), *(len(str(_cell(r[col - 1]))) for r in table.rows[:500])])
            ws.column_dimensions[get_column_letter(col)].width = min(max(10, longest + 2), 60)
            for cell in ws[get_column_letter(col)][1:]:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
        if table.note:
            ws.append([])
            ws.append([table.note])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _latin1(text: Any) -> str:
    return str(_cell(text)).encode("latin-1", "replace").decode("latin-1")


class _Pdf(FPDF):
    def __init__(self, title: str) -> None:
        super().__init__(orientation="L", unit="mm", format="A4")
        self.report_title = title
        self.set_auto_page_break(auto=True, margin=14)
        self.set_margins(12, 12, 12)

    def footer(self) -> None:
        self.set_y(-10)
        self.set_font("Helvetica", size=7)
        self.set_text_color(120)
        self.cell(0, 5, _latin1(f"{self.report_title} - MasterAtlas AI"), align="L")
        self.cell(0, 5, f"Page {self.page_no()}", align="R")


def to_pdf(data: ReportData) -> bytes:
    pdf = _Pdf(data.title)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 8, _latin1(data.title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(90)
    if data.subtitle:
        pdf.multi_cell(0, 5, _latin1(data.subtitle), new_x="LMARGIN", new_y="NEXT")
    pdf.multi_cell(0, 5, f"Generated {data.generated_at:%Y-%m-%d %H:%M} UTC", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0)
    pdf.ln(2)
    if data.facts:
        pdf.set_font("Helvetica", size=9)
        for key, value in data.facts:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(55, 5, _latin1(key))
            pdf.set_font("Helvetica", size=9)
            pdf.multi_cell(0, 5, _latin1(value), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
    for table in data.tables:
        pdf.set_font("Helvetica", "B", 11)
        pdf.multi_cell(0, 7, _latin1(table.title), new_x="LMARGIN", new_y="NEXT")
        if not table.rows:
            pdf.set_font("Helvetica", "I", 9)
            pdf.multi_cell(0, 5, "No rows.", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
            continue
        weights = table.widths or [1.0] * len(table.columns)
        total = sum(weights)
        widths = [pdf.epw * w / total for w in weights]
        pdf.set_font("Helvetica", size=8)
        with pdf.table(col_widths=widths, line_height=4.5, text_align="LEFT",
                       headings_style=_heading_style()) as t:  # fmt: skip
            t.row([_latin1(c) for c in table.columns])
            for row in table.rows:
                t.row([_latin1(v)[:600] for v in row])
        if table.note:
            pdf.set_font("Helvetica", "I", 8)
            pdf.multi_cell(0, 5, _latin1(table.note), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
    return bytes(pdf.output())


def _heading_style() -> Any:
    from fpdf.fonts import FontFace

    return FontFace(emphasis="BOLD", fill_color=(232, 238, 245))
