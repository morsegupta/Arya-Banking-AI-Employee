"""Document Generator: render a tabular document (PDF) from a role pack template. Plain code, no LLM.

A template names the title, the columns, the notes and which totals to show.
The caller supplies already-formatted values (the role pack decides how
amounts and dates look), so this module knows nothing about any domain.
Every value printed comes from the caller's data: the generator never adds facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fpdf import FPDF


@dataclass
class Column:
    key: str
    label: str
    width: float = 30
    align: str = "L"          # L | R | C


@dataclass
class TableDocument:
    title: str
    issuer_lines: list[str]                 # who the document is from
    fields: list[tuple[str, str]]           # label/value block under the title
    columns: list[Column]
    rows: list[dict[str, Any]]
    totals: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    created_at: datetime | None = None      # fixed date: same input -> same bytes


def _latin1(text: Any) -> str:
    """Core PDF fonts are latin-1 only. Replace what cannot be printed rather than fail."""
    return str(text).replace("₹", "Rs. ").replace("–", "-").replace("’", "'").encode("latin-1", "replace") \
        .decode("latin-1")


def render(doc: TableDocument, out: Path) -> Path:
    pdf = FPDF(format="A4")
    if doc.created_at:
        pdf.set_creation_date(doc.created_at)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    if doc.issuer_lines:
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, _latin1(doc.issuer_lines[0]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for line in doc.issuer_lines[1:]:
            pdf.cell(0, 5, _latin1(line), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _latin1(doc.title), new_x="LMARGIN", new_y="NEXT")
    for label, value in doc.fields:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(45, 6, _latin1(label))
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, _latin1(value), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 9)
    for c in doc.columns:
        pdf.cell(c.width, 7, _latin1(c.label), border=1, align=c.align)
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for row in doc.rows:
        for c in doc.columns:
            pdf.cell(c.width, 7, _latin1(row.get(c.key, "")), border=1, align=c.align)
        pdf.ln()
    if doc.totals:
        pdf.ln(2)
        label_w = sum(c.width for c in doc.columns[:-1])
        pdf.set_font("Helvetica", "B", 10)
        for label, value in doc.totals:
            pdf.cell(label_w, 7, _latin1(label), align="R")
            pdf.cell(doc.columns[-1].width if doc.columns else 30, 7, _latin1(value), border=1,
                     align=doc.columns[-1].align if doc.columns else "R")
            pdf.ln()
    if doc.notes:
        pdf.ln(4)
        pdf.set_font("Helvetica", "", 9)
        for note in doc.notes:
            pdf.multi_cell(0, 5, _latin1(note), new_x="LMARGIN", new_y="NEXT")
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out))
    return out


def columns_from(spec: list[dict[str, Any]]) -> list[Column]:
    return [Column(key=c["key"], label=c.get("label", c["key"]), width=float(c.get("width", 30)),
                   align=c.get("align", "L")) for c in spec]


def fill(template: str, values: dict[str, Any]) -> str:
    """str.format with missing keys left visible, so a template typo never crashes a send."""
    class _Keep(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return template.format_map(_Keep(values))
