"""Spreadsheet tools: operate a team's own workbook (a desktop app's file) like a careful colleague.

A sheet map (packs/companies/<co>/sheets/<name>.yaml) says where the workbook is, which worksheet, which
column identifies a row, and which columns the employee may write. Everything else (people's notes,
formulas, other sheets) is never touched.

    <name>.read        rows as header + values
    <name>.update_row  set some writable columns of the row whose key matches; refused while the
                       workbook is open in the desktop app (its lock file exists), so a person's unsaved
                       edits are never overwritten; saved atomically; verified by reading it back

This is the file route of ADR-7 (API -> DOM -> vision): when the app must be driven on screen, a
vision (computer-use) driver would be the next rung; this runtime has none (the models are text-only),
so a locked workbook is retried later and then handed to a human.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar

import yaml
from openpyxl import load_workbook
from pydantic import BaseModel, Field

from core.models import Evidence, ToolResult
from tools.base import PermanentToolError, RunContext, Tool, ToolDenied, ToolRegistry, TransientToolError


class SheetMap(BaseModel):
    name: str
    title: str
    path: str                                   # relative to the shared drive
    worksheet: str
    key: str                                    # header of the column that identifies a row
    writable: list[str]                         # headers the employee may write
    numeric: list[str] = Field(default_factory=list)
    lock_files: list[str] = Field(default_factory=lambda: [".~lock.{file}#", "~${file}"])
    notes: list[str] = Field(default_factory=list)
    values: dict[str, Any] = Field(default_factory=dict)   # company vocabulary, e.g. status labels per state


def load_sheet_maps(directory: Path | str) -> dict[str, SheetMap]:
    d = Path(directory)
    maps = [SheetMap.model_validate(yaml.safe_load(p.read_text())) for p in sorted(d.glob("*.yaml"))] if d.exists() else []
    return {m.name: m for m in maps}


class ReadArgs(BaseModel):
    max_rows: int = Field(1000, ge=1, le=10000)


class UpdateArgs(BaseModel):
    key: str = Field(description="Value in the key column of the row to update")
    values: dict[str, Any] = Field(description="Column header -> new value (writable columns only)")


class _SheetTool(Tool):
    risk: ClassVar[str] = "read"
    kind: ClassVar[str] = "file"

    def __init__(self, sheet: SheetMap, drive_root: Path):
        self.sheet, self.file = sheet, (Path(drive_root) / sheet.path).resolve()
        self.app = sheet.name                   # capability scope is per sheet

    def _locked(self) -> str | None:
        for pattern in self.sheet.lock_files:
            lock = self.file.parent / pattern.format(file=self.file.name)
            if lock.exists():
                return lock.name
        return None

    def _open(self, read_only: bool):
        if not self.file.exists():
            raise PermanentToolError(f"no workbook at {self.sheet.path}")
        wb = load_workbook(self.file, read_only=read_only)
        if self.sheet.worksheet not in wb.sheetnames:
            raise PermanentToolError(f"no worksheet {self.sheet.worksheet!r} in {self.file.name}")
        return wb, wb[self.sheet.worksheet]


def _cell(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (date, datetime)) else v


class ReadSheet(_SheetTool):
    input_schema = ReadArgs

    def __init__(self, sheet: SheetMap, drive_root: Path):
        super().__init__(sheet, drive_root)
        self.name = f"{sheet.name}.read"
        self.description = f"Read the rows of {sheet.title}."

    async def run(self, args: ReadArgs, ctx: RunContext) -> ToolResult:
        wb, ws = self._open(read_only=True)
        try:
            rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
        header, body = (rows[0], rows[1:]) if rows else ([], [])
        return ToolResult(ok=True, output={"header": header, "rows": body[:args.max_rows], "row_count": len(body)})


class UpdateRow(_SheetTool):
    risk = "write"
    input_schema = UpdateArgs

    def __init__(self, sheet: SheetMap, drive_root: Path):
        super().__init__(sheet, drive_root)
        self.name = f"{sheet.name}.update_row"
        self.description = (f"Update one row of {sheet.title} by its {sheet.key!r}. Writable columns: "
                            f"{', '.join(sheet.writable)}.")

    async def run(self, args: UpdateArgs, ctx: RunContext) -> ToolResult:
        refused = [c for c in args.values if c not in self.sheet.writable]
        if refused:
            raise ToolDenied(f"columns not writable in {self.sheet.name}: {refused}")
        lock = self._locked()
        if lock:
            raise TransientToolError(f"{self.file.name} is open in the desktop app ({lock}); not overwriting it")
        wb, ws = self._open(read_only=False)
        header = [c.value for c in ws[1]]
        missing = [c for c in [self.sheet.key, *args.values] if c not in header]
        if missing:
            wb.close()
            raise PermanentToolError(f"{self.sheet.name} has no column(s) {missing} (header: {header})")
        key_col = header.index(self.sheet.key) + 1
        row = next((r for r in range(2, ws.max_row + 1) if str(ws.cell(r, key_col).value) == args.key), None)
        if row is None:
            wb.close()
            raise PermanentToolError(f"no row with {self.sheet.key} = {args.key!r}")
        before, after = {}, {}
        for col, value in args.values.items():
            cell = ws.cell(row, header.index(col) + 1)
            before[col] = _cell(cell.value)
            if col in self.sheet.numeric and value not in (None, ""):
                value = float(value)
            cell.value = value if value != "" else None
            after[col] = value
        fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=self.file.parent)
        os.close(fd)
        try:
            wb.save(tmp)
            os.replace(tmp, self.file)          # atomic: a reader never sees half a workbook
        finally:
            wb.close()
            Path(tmp).unlink(missing_ok=True)
        return ToolResult(ok=True, output={"key": args.key, "row": row, "before": before, "after": after},
                          evidence=[Evidence(kind="record", ref=f"{self.sheet.name}:{args.key}",
                                             summary=f"{self.sheet.title} row {row} updated: "
                                                     + ", ".join(f"{k}={v}" for k, v in after.items()),
                                             data={"before": before, "after": after})])

    async def verify(self, args: UpdateArgs, result: ToolResult, ctx: RunContext) -> bool | None:
        wb, ws = self._open(read_only=True)
        try:
            rows = list(ws.iter_rows(values_only=True))
        finally:
            wb.close()
        header = list(rows[0])
        row = next((r for r in rows[1:] if str(r[header.index(self.sheet.key)]) == args.key), None)
        if row is None:
            return False
        for col, want in result.output["after"].items():
            got = row[header.index(col)]
            if not (got == want or (want in ("", None) and got in ("", None))
                    or (isinstance(want, float) and got is not None and abs(float(got) - want) < 0.005)):
                return False
        return True


def register_sheet_tools(registry: ToolRegistry, sheets: dict[str, SheetMap], drive_root: Path) -> None:
    for sheet in sheets.values():
        registry.register(ReadSheet(sheet, drive_root))
        registry.register(UpdateRow(sheet, drive_root))
