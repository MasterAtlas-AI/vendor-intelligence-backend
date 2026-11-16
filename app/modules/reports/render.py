"""Report rendering: one neutral structure (title, key facts, tables) rendered to Excel or PDF.

Builders produce a `ReportData`; renderers know nothing about vendors or RFQs. PDF uses the built-in Helvetica
font, which covers Western European text; characters outside Latin-1 are replaced (the Excel version keeps them).
"""

import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
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
    kind: str = ""  # the report type, shown in the PDF header ("RFQ summary")
    organization: str = ""  # who it was prepared for, shown in the header and footer
    organization_slug: str = ""  # picks the organization's logo for the PDF header
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


# Common typography outside Latin-1, spelled with the nearest characters the PDF font has.
_PLAIN = str.maketrans({
    **dict.fromkeys([0x2010, 0x2011, 0x2013, 0x2014, 0x2212, 0x2022], "-"),  # dashes, minus, bullet
    **dict.fromkeys([0x2018, 0x2019, 0x201A, 0x2032], "'"),  # single quotes, prime
    **dict.fromkeys([0x201C, 0x201D, 0x201E, 0x2033], '"'),  # double quotes, double prime
    **dict.fromkeys([0x00A0, 0x2009, 0x202F], " "),  # no-break and thin spaces
    0x2026: "...", 0x2264: "<=", 0x2265: ">=", 0x2248: "~", 0x2122: "(TM)", 0x20AC: "EUR",
})  # fmt: skip


def _latin1(text: Any) -> str:
    plain = str(_cell(text)).translate(_PLAIN)
    if not plain.isascii():  # accented letters outside Latin-1 (e.g. "ł") lose only the accent
        plain = "".join(
            ch if ord(ch) < 256 else (unicodedata.normalize("NFKD", ch)[:1] or "?") for ch in plain
        )
    return plain.encode("latin-1", "replace").decode("latin-1")


# ---------------------------------------------------------------------------------------------------------- PDF design
# A4 landscape. First page: a navy band with the brand, report kind, title and who it was prepared for; then the key
# facts as cards; then each table as a titled section (navy header row, zebra rows, numbers right-aligned, statuses
# coloured). Later pages carry a slim running header. Every page: a "Confidential" footer and "Page x of y".

Rgb = tuple[int, int, int]
NAVY: Rgb = (17, 39, 74)
ACCENT: Rgb = (37, 99, 235)
INK: Rgb = (17, 24, 39)
MUTED: Rgb = (100, 116, 139)
LINE: Rgb = (226, 232, 240)
CARD: Rgb = (248, 250, 252)
ZEBRA: Rgb = (245, 248, 252)

# Status words -> (text colour, background) for pills and status cells.
_TONES: dict[str, tuple[Rgb, Rgb]] = {
    "good": ((21, 128, 61), (220, 252, 231)),
    "info": ((29, 78, 216), (219, 234, 254)),
    "warn": ((180, 83, 9), (254, 243, 199)),
    "bad": ((185, 28, 28), (254, 226, 226)),
    "plain": ((71, 85, 105), (241, 245, 249)),
}
_STATUS_TONE = {
    **dict.fromkeys(
        ["awarded", "approved", "preferred", "quoted", "yes", "active", "closed", "complete"], "good"
    ),
    **dict.fromkeys(["issued", "invited", "evaluating", "shortlisted", "qualified", "open"], "info"),
    **dict.fromkeys(["draft", "pending", "conditional", "under review", "candidate", "on hold"], "warn"),
    **dict.fromkeys(["lost", "declined", "rejected", "blocked", "cancelled", "disqualified", "no"], "bad"),
}
_STATUS_COLUMNS = {"status", "avl status", "your avl status", "stage", "mandatory"}
_NUMBER = re.compile(r"^-?[\d,]+(\.\d+)?\s?%?$")


# Each organization's own logo for its PDF header: assets/logos/<organization slug>.png (or .jpg). A PNG with a
# transparent background works best; it sits on a white tile, so any colour logo reads on the navy band. An
# organization without a logo file gets the MasterAtlas AI wordmark, never another organization's logo.
LOGO_DIR = Path(__file__).parent / "assets" / "logos"
LOGO_EXTENSIONS = (".png", ".jpg", ".jpeg")


@cache
def _logo(slug: str) -> tuple[str, float] | None:
    """The organization's logo path and its width/height ratio, or None when it has no logo file."""
    from PIL import Image

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug or ""):
        return None
    for ext in LOGO_EXTENSIONS:
        path = LOGO_DIR / f"{slug}{ext}"
        if path.is_file():
            with Image.open(path) as img:
                w, h = img.size
            return str(path), w / h if h else 1.0
    return None


def _tone(value: Any) -> tuple[Rgb, Rgb]:
    return _TONES[_STATUS_TONE.get(str(value).strip().lower().replace("_", " "), "plain")]


def _short_url(value: Any) -> str:
    return re.sub(r"^https?://(www\.)?", "", str(_cell(value))).rstrip("/")


def _is_numeric(rows: list[list[Any]], i: int) -> bool:
    values = [str(_cell(r[i])).strip() for r in rows if i < len(r)]
    filled = [v for v in values if v]
    return bool(filled) and all(_NUMBER.match(v) for v in filled)


class _Pdf(FPDF):
    def __init__(self, data: ReportData) -> None:
        super().__init__(orientation="L", unit="mm", format="A4")
        self.data = data
        self.title_width = self.epw  # narrowed on the first page to leave room for the logo
        self.alias_nb_pages()
        self.set_auto_page_break(auto=True, margin=16)
        self.set_margins(14, 14, 14)
        self.set_title(_latin1(data.title))
        self.set_author("MasterAtlas AI")
        self.set_creator("MasterAtlas AI")

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_y(7)
        if logo := _logo(self.data.organization_slug):
            self.image(logo[0], x=self.l_margin, y=5.4, h=7)
        else:
            self.set_font("Helvetica", "B", 8)
            self.set_text_color(*NAVY)
            self.cell(0, 5, "MASTERATLAS AI", align="L")
        self.set_x(self.l_margin)
        self.set_font("Helvetica", size=8)
        self.set_text_color(*MUTED)
        self.cell(0, 5, _latin1(self.data.title)[:110], align="R")
        self.set_draw_color(*LINE)
        self.line(self.l_margin, 13, self.w - self.r_margin, 13)
        self.set_y(17)

    def footer(self) -> None:
        self.set_y(-11)
        self.set_draw_color(*LINE)
        self.line(self.l_margin, self.get_y() - 1, self.w - self.r_margin, self.get_y() - 1)
        self.set_font("Helvetica", size=7)
        self.set_text_color(*MUTED)
        org = f"  |  {self.data.organization}" if self.data.organization else ""
        self.cell(
            0, 5, _latin1(f"MasterAtlas AI{org}  |  Confidential: for internal procurement use"), align="L"
        )
        self.set_x(self.l_margin)
        self.cell(0, 5, f"Page {self.page_no()} of {self.str_alias_nb_pages}", align="R")

    # -- building blocks ---------------------------------------------------------------------------------------------

    def cover_band(self) -> None:
        d = self.data
        band = 38 if d.subtitle else 32
        self.set_fill_color(*NAVY)
        self.rect(0, 0, self.w, band, style="F")
        self.set_fill_color(*ACCENT)
        self.rect(0, band, self.w, 1.2, style="F")
        # Logo on a white tile at the top right (or a text wordmark), the report kind above the title.
        right: float = self.w - self.r_margin
        if logo := _logo(self.data.organization_slug):
            tile_h = band - 12
            img_h: float = tile_h - 4
            img_w = min(img_h * logo[1], 70)
            img_h = img_w / logo[1]
            tile_w = img_w + 6
            self.set_fill_color(255, 255, 255)
            self.rect(right - tile_w, 6, tile_w, tile_h, style="F", round_corners=True, corner_radius=2)
            self.image(logo[0], x=right - tile_w + 3, y=6 + (tile_h - img_h) / 2, w=img_w, h=img_h)
            right -= tile_w + 6
        else:
            self.set_fill_color(*ACCENT)
            self.rect(right - 38, 8, 3.2, 3.2, style="F")
            self.set_xy(right - 33, 7.2)
            self.set_font("Helvetica", "B", 9)
            self.set_text_color(255, 255, 255)
            self.cell(33, 5, "MASTERATLAS AI", align="R")
            right -= 44
        self.title_width = right - self.l_margin
        if d.kind:
            self.set_xy(self.l_margin, 7.2)
            self.set_font("Helvetica", "B", 8)
            self.set_text_color(147, 197, 253)
            self.cell(self.title_width, 5, _latin1(d.kind.upper()))
        self.set_xy(self.l_margin, 14)
        self.set_font("Helvetica", "B", 19)
        self.set_text_color(255, 255, 255)
        title = _latin1(d.title)
        while len(title) > 10 and self.get_string_width(title) > self.title_width:
            title = title[:-4] + "..."
        self.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
        if d.subtitle:
            self.set_font("Helvetica", size=9)
            self.set_text_color(203, 213, 225)
            sub = _latin1(" ".join(d.subtitle.split()))
            while len(sub) > 10 and self.get_string_width(sub) > self.title_width:
                sub = sub[:-4] + "..."
            self.cell(0, 5, sub, new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", size=8)
        self.set_text_color(203, 213, 225)
        who = f"Prepared for {d.organization}  |  " if d.organization else ""
        self.set_xy(self.l_margin, band - 7)
        self.cell(0, 5, _latin1(f"{who}Generated {d.generated_at:%d %b %Y, %H:%M} UTC"))
        self.set_y(band + 7)

    def fact_cards(self, facts: list[tuple[str, str]]) -> None:
        if not facts:
            return
        per_row = min(5, len(facts)) if len(facts) != 4 else 4
        per_row = max(per_row, 3)
        gap, h = 4.0, 17.0
        w = (self.epw - gap * (per_row - 1)) / per_row
        top = self.get_y()
        for i, (key, value) in enumerate(facts):
            col = i % per_row
            x, y = self.l_margin + col * (w + gap), top + (i // per_row) * (h + gap)
            self.set_fill_color(*CARD)
            self.set_draw_color(*LINE)
            self.rect(x, y, w, h, style="DF", round_corners=True, corner_radius=1.6)
            self.set_fill_color(*ACCENT)
            self.rect(x, y + 3, 0.9, h - 6, style="F")
            self.set_xy(x + 4, y + 2.6)
            self.set_font("Helvetica", "B", 6.5)
            self.set_text_color(*MUTED)
            self.cell(w - 6, 4, _latin1(key.upper()))
            text = _latin1(value or "-")
            if key.strip().lower() == "status" and text != "-":
                self.pill(x + 4, y + 8.4, text, size=8)
                continue
            self.set_xy(x + 4, y + 7.6)
            self.set_font("Helvetica", "B", 11 if len(text) <= 22 else 8.5)
            self.set_text_color(*INK)
            while len(text) > 4 and self.get_string_width(text) > w - 7:
                text = text[:-4] + "..."
            self.cell(w - 6, 6, text)
        rows = (len(facts) + per_row - 1) // per_row
        self.set_y(top + rows * h + (rows - 1) * gap + 9)

    def pill(self, x: float, y: float, text: str, size: float = 7) -> None:
        fg, bg = _tone(text)
        self.set_font("Helvetica", "B", size)
        label = text.capitalize()
        w, h = self.get_string_width(label) + 6, size * 0.6
        self.set_fill_color(*bg)
        self.rect(x, y, w, h, style="F", round_corners=True, corner_radius=h / 2)
        self.set_text_color(*fg)
        self.set_xy(x, y)
        self.cell(w, h, label, align="C")

    def section_title(self, title: str, count: int) -> None:
        if self.get_y() + 24 > self.page_break_trigger:
            self.add_page()
        y = self.get_y()
        self.set_fill_color(*ACCENT)
        self.rect(self.l_margin, y + 0.8, 1.2, 5, style="F")
        self.set_xy(self.l_margin + 3.5, y)
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(*INK)
        label = _latin1(title)
        self.cell(self.get_string_width(label) + 3, 6.5, label)
        self.set_font("Helvetica", size=8)
        self.set_text_color(*MUTED)
        self.cell(40, 6.5, f"{count} {'row' if count == 1 else 'rows'}")
        self.set_y(y + 9)

    def empty_state(self, title: str) -> None:
        y = self.get_y()
        self.set_fill_color(*CARD)
        self.set_draw_color(203, 213, 225)
        with self.local_context(dash_pattern={"dash": 1.2, "gap": 1.2}):
            self.rect(self.l_margin, y, self.epw, 15, style="DF", round_corners=True, corner_radius=1.6)
        self.set_xy(self.l_margin, y + 3)
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*MUTED)
        self.cell(self.epw, 5, _latin1(f"No {title.lower()} yet"), align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", size=7.5)
        self.cell(
            self.epw, 4, "This section fills in as the information is added in MasterAtlas AI.", align="C"
        )
        self.set_y(y + 22)

    def data_table(self, table: Table) -> None:
        from fpdf.enums import TableBordersLayout
        from fpdf.fonts import FontFace

        self.section_title(table.title, len(table.rows))
        if not table.rows:
            self.empty_state(table.title)
            return
        weights = table.widths or [1.0] * len(table.columns)
        total = sum(weights)
        widths = [self.epw * w / total for w in weights]
        lower = [c.strip().lower() for c in table.columns]
        numeric = [_is_numeric(table.rows, i) for i in range(len(table.columns))]
        align = ["RIGHT" if n else "LEFT" for n in numeric]
        heading = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=NAVY, size_pt=7.5)
        self.set_fill_color(255, 255, 255)  # rows without zebra fill stay white
        self.set_font("Helvetica", size=7.8)
        self.set_text_color(*INK)
        self.set_draw_color(*LINE)
        with self.table(
            col_widths=widths,
            line_height=4.6,
            text_align=align,
            headings_style=heading,
            cell_fill_color=ZEBRA,
            cell_fill_mode="ROWS",
            borders_layout=TableBordersLayout.HORIZONTAL_LINES,
            padding=(1.6, 2),
            repeat_headings=1,
        ) as t:
            head = t.row()
            for c in table.columns:
                head.cell(_latin1(c))
            for row in table.rows:
                r = t.row()
                for i, value in enumerate(row):
                    text = _latin1(value)[:600]
                    if lower[i] in _STATUS_COLUMNS and text:
                        r.cell(text.capitalize(), style=FontFace(emphasis="BOLD", color=_tone(text)[0]))
                    elif lower[i] == "website" and text:
                        url = text if text.startswith("http") else f"https://{text}"
                        r.cell(_latin1(_short_url(value)), style=FontFace(color=ACCENT), link=url)
                    elif i == 0:
                        r.cell(text, style=FontFace(emphasis="BOLD"))
                    else:
                        r.cell(text)
        if table.note:
            self.set_font("Helvetica", "I", 7.5)
            self.set_text_color(*MUTED)
            self.multi_cell(0, 4.5, _latin1(table.note), new_x="LMARGIN", new_y="NEXT")
        self.ln(8)


def to_pdf(data: ReportData) -> bytes:
    pdf = _Pdf(data)
    pdf.add_page()
    pdf.cover_band()
    pdf.fact_cards(data.facts)
    for table in data.tables:
        pdf.data_table(table)
    return bytes(pdf.output())
