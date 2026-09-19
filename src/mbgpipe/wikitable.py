"""Wikitable markup -> rectangular grid, with rowspan and colspan resolved.

A rowspan copies its value into every row it covers, which is right for display
and wrong for arithmetic: "33 Santri" stated once beside two venues is 33 people,
not 66. So every grid cell keeps `origin`, the (row, col) of the cell it was
written in. Summing over distinct origins counts each stated figure once.

The parser is strict on purpose. Wikipedia pages are live and edited; a header that
moves or a stray `||` would silently misalign columns if tolerated. It raises
`WikitableError` instead, so a re-fetch fails loudly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_ATTR = r'(?:rowspan|colspan|style|class|align|width|scope|bgcolor|valign)'
_ATTR_VALUE = r'''(?:"[^"]*"|'[^']*'|[^\s|"']+)'''
RE_ATTRS = re.compile(rf"^\s*((?:{_ATTR}\s*=\s*{_ATTR_VALUE}\s*)+)\|(?!\|)(.*)$", re.S | re.I)
RE_SPAN = re.compile(r'(rowspan|colspan)\s*=\s*"?(\d+)"?', re.I)


class WikitableError(ValueError):
    pass


@dataclass
class Cell:
    text: str
    rowspan: int = 1
    colspan: int = 1
    header: bool = False


@dataclass
class GridCell:
    text: str
    origin: tuple[int, int]     # (row, col) of the cell this value was written in
    is_origin: bool             # False when the value was carried down by a rowspan


def _make_cell(raw: str, header: bool) -> Cell:
    m = RE_ATTRS.match(raw)
    attrs, text = (m.group(1), m.group(2)) if m else ("", raw)
    spans = {k.lower(): int(v) for k, v in RE_SPAN.findall(attrs)}
    return Cell(text.strip(), spans.get("rowspan", 1), spans.get("colspan", 1), header)


def split_rows(lines: Iterable[str]) -> list[list[Cell]]:
    """Rows of cells for one table's lines (`{|` through `|}` inclusive)."""
    rows: list[list[Cell]] = [[]]
    for line in lines:
        if line.startswith(("{|", "|}", "|+")):
            continue
        if line.startswith("|-"):
            rows.append([])
        elif line.startswith(("|", "!")):
            marker = line[0]
            if "||" in line or "!!" in line:
                raise WikitableError(f"inline cell separator not supported: {line[:80]!r}")
            rows[-1].append(_make_cell(line[1:], header=marker == "!"))
        elif rows[-1]:
            rows[-1][-1].text += "\n" + line.strip()   # multi-line cell
    return [r for r in rows if r]


def resolve(rows: list[list[Cell]], ncols: int) -> list[list[GridCell]]:
    """Fill rowspans/colspans down and across into a `len(rows) x ncols` grid."""
    pending: dict[int, list] = {}          # col -> [rows still to cover, GridCell]
    grid: list[list[GridCell]] = []
    for r, cells in enumerate(rows):
        it = iter(cells)
        out: list[GridCell] = []
        col = 0
        while col < ncols:
            if col in pending:
                remaining, carried = pending[col]
                out.append(GridCell(carried.text, carried.origin, False))
                if remaining <= 1:
                    del pending[col]
                else:
                    pending[col][0] = remaining - 1
                col += 1
                continue
            cell = next(it, None)
            if cell is None:
                break
            for k in range(cell.colspan):
                gc = GridCell(cell.text, (r, col), k == 0)
                out.append(gc)
                if cell.rowspan > 1:
                    pending[col] = [cell.rowspan - 1, gc]
                col += 1
        if len(out) != ncols or next(it, None) is not None:
            raise WikitableError(f"row {r}: expected {ncols} columns, got {len(out)}+")
        grid.append(out)
    return grid


def table_lines(wikitext_lines: Iterable[str]) -> list[str]:
    """The lines of the first `{| ... |}` block in `wikitext_lines`."""
    block: list[str] = []
    inside = False
    for line in wikitext_lines:
        if line.startswith("{|"):
            inside = True
        if inside:
            block.append(line)
            if line.startswith("|}"):
                break
    if not block:
        raise WikitableError("no table found")
    return block
