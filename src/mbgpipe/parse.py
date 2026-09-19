"""Stage 2 — wikitext in, two tables out.

    citations.jsonl   one row per <ref>, with url/title/work/date/archive-url
    rows.jsonl        one row per rendered row of the Indonesia incident table,
                      with plain-text cells, wikilink targets and the ref ids it cites

The Indonesia section of the source page is a wikitable (date | province |
kabupaten/kota | venue | symptomatic | deaths | ref), not prose. Reading it as
table cells rather than regexing sentences is what makes the rest of the pipeline
mostly a matter of parsing short, regular strings.

Indonesian Wikipedia mixes English and Indonesian citation parameter names in
the same article — `|title=` beside `|judul=`, `|access-date=` beside
`|tanggal akses=`. The alias table below normalises both.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

import mwparserfromhell as mw

from . import wikitable

# Citation template parameter aliases. Left column is the canonical field.
PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "url": ("url", "URL", "tautan"),
    "title": ("title", "judul"),
    "work": ("work", "website", "situsweb", "situs web", "newspaper", "surat kabar",
             "magazine", "majalah", "publisher", "penerbit"),
    "date": ("date", "tanggal"),
    "access_date": ("access-date", "accessdate", "tanggal-akses", "tanggal akses",
                    "diakses tanggal", "diakses-tanggal"),
    "archive_url": ("archive-url", "archiveurl", "arsip-url", "url-arsip"),
    "archive_date": ("archive-date", "archivedate", "arsip-tanggal", "tanggal-arsip"),
    "author": ("author", "penulis", "last", "nama-akhir"),
    "language": ("language", "bahasa"),
}

CITE_TEMPLATES = {"cite web", "cite news", "cite journal", "cite book",
                  "citation", "cite press release", "cite report"}

RE_HEADING = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$")
RE_BARE_LINK = re.compile(r"\[(https?://[^\s\]]+)(?:\s+([^\]]*))?\]")
RE_WS = re.compile(r"\s+")


@dataclass
class Citation:
    ref_id: str
    url: str = ""
    title: str = ""
    work: str = ""
    date: str = ""
    access_date: str = ""
    archive_url: str = ""
    archive_date: str = ""
    author: str = ""
    language: str = ""
    template: str = ""
    source_page: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def best_url(self) -> str:
        """Archive first. Indonesian news link rot is severe and the archive copy
        is the one that will still resolve when you re-run this in six months."""
        return self.archive_url or self.url


def _param(template, field_name: str) -> str:
    for alias in PARAM_ALIASES[field_name]:
        if template.has(alias):
            return str(template.get(alias).value).strip()
    return ""


def _citation_from_template(tpl, ref_id: str, source_page: str) -> Citation:
    return Citation(
        ref_id=ref_id,
        template=str(tpl.name).strip().lower(),
        source_page=source_page,
        **{f: _param(tpl, f) for f in PARAM_ALIASES},
    )


def _ref_name(tag) -> Optional[str]:
    for attr in tag.attributes:
        if str(attr.name).strip().lower() == "name":
            return str(attr.value).strip().strip('"\'')
    return None


def _auto_id(contents: str) -> str:
    """Stable id for an unnamed <ref>, derived from its own content.

    A positional counter would be simpler but the citation extractor and the
    block extractor walk the article differently — one over all tags, one over
    bullet lines — so their counters drift apart and unnamed refs silently fail
    to join. Hashing the content means both arrive at the same id independently.
    """
    return "auto:" + hashlib.md5(RE_WS.sub(" ", contents).strip().encode("utf-8")).hexdigest()[:10]


def ref_id_for(tag) -> Optional[str]:
    """The join key for one <ref>: its name, or a hash of its contents."""
    name = _ref_name(tag)
    if name:
        return name
    contents = str(tag.contents) if tag.contents is not None else ""
    return _auto_id(contents) if contents.strip() else None


def extract_citations(wikitext: str, source_page: str = "") -> list[Citation]:
    """One Citation per <ref> that defines content.

    Named refs reused later (`<ref name="x" />`) are not duplicated here; the
    incident blocks reference them by id instead.
    """
    code = mw.parse(wikitext)
    out: list[Citation] = []
    for tag in code.filter_tags(matches=lambda t: str(t.tag).lower() == "ref"):
        if tag.contents is None or not str(tag.contents).strip():
            continue  # back-reference, no payload
        name = ref_id_for(tag)
        if name is None:
            continue
        contents = mw.parse(str(tag.contents))
        templates = [
            t for t in contents.filter_templates()
            if str(t.name).strip().lower() in CITE_TEMPLATES
        ]
        if templates:
            for i, tpl in enumerate(templates):
                rid = name if i == 0 else f"{name}.{i}"
                out.append(_citation_from_template(tpl, rid, source_page))
            continue
        # Fall back to a bare external link, e.g. <ref>[https://... Judul]</ref>
        bare = RE_BARE_LINK.search(str(tag.contents))
        if bare:
            out.append(
                Citation(ref_id=name, url=bare.group(1), title=(bare.group(2) or "").strip(),
                         template="bare_link", source_page=source_page)
            )
    return out


def _refs_in(fragment: str) -> list[str]:
    """Ref ids cited by one line, definitions and back-references alike."""
    ids: list[str] = []
    for tag in mw.parse(fragment).filter_tags(matches=lambda t: str(t.tag).lower() == "ref"):
        rid = ref_id_for(tag)
        if rid:
            ids.append(rid)
    return ids


def iter_lines_with_sections(wikitext: str) -> Iterator[tuple[int, str, list[str]]]:
    """Yield (line_no, line, section_path) walking the article top to bottom."""
    path: list[str] = []
    for i, line in enumerate(wikitext.splitlines()):
        heading = RE_HEADING.match(line.strip())
        if heading:
            level = len(heading.group(1)) - 2  # "==" is depth 0
            title = mw.parse(heading.group(2)).strip_code().strip()
            path = path[:level] + [title]
            continue
        yield i, line, list(path)


# --- the incident table ------------------------------------------------------

COLUMNS = ("date", "province", "kabkota", "venue", "symptomatic", "deaths", "ref")

# Bottom header row, one label per column. Asserted, not assumed: the page is live.
EXPECTED_HEADER = ("tanggal kejadian", "provinsi", "kabupaten/kota", "tempat/sekolah",
                   "bergejala", "meninggal", "ref")


@dataclass
class TableRow:
    row_id: str
    source_page: str
    table_row: int                       # position in the rendered table, 0-based over body rows
    date: str = ""
    province: str = ""
    province_link: str = ""              # wikilink target: the canonical name
    kabkota: str = ""
    kabkota_link: str = ""
    venue: str = ""
    symptomatic: str = ""
    deaths: str = ""
    ref_ids: list[str] = field(default_factory=list)
    # column -> table_row of the cell the value was written in. A value carried
    # down by a rowspan has an origin row different from this row's own.
    origins: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _plain(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    return RE_WS.sub(" ", mw.parse(text).strip_code().replace("\u00a0", " ")).strip()


def _link_target(text: str) -> str:
    links = mw.parse(text).filter_wikilinks()
    return str(links[0].title).strip() if links else ""


def extract_incident_table(
    wikitext: str,
    source_page: str = "",
    section: str = "Indonesia",
) -> tuple[list[TableRow], dict]:
    """Rows of the first table under a heading containing `section`, plus a report.

    The report carries the page's own `TOTAL` row so stage 3 can check its
    arithmetic against the editors'.
    """
    lines = [line for _, line, path in iter_lines_with_sections(wikitext)
             if any(section.lower() in seg.lower() for seg in path)]
    cells = wikitable.split_rows(wikitable.table_lines(lines))

    n_header = next((i for i, r in enumerate(cells) if not all(c.header for c in r)), len(cells))
    header = wikitable.resolve(cells[:n_header], len(COLUMNS))
    labels = tuple(_plain(c.text).lower() for c in header[-1]) if header else ()
    if labels != EXPECTED_HEADER:
        raise wikitable.WikitableError(
            f"table header changed: got {labels}, expected {EXPECTED_HEADER}")

    grid = wikitable.resolve(cells[n_header:], len(COLUMNS))
    rows: list[TableRow] = []
    report = {"rendered_rows": len(grid), "placeholder_rows": 0, "wikipedia_total": None}
    for i, gcells in enumerate(grid):
        text = {c: g.text for c, g in zip(COLUMNS, gcells)}
        if _plain(text["date"]).upper() == "TOTAL":
            report["wikipedia_total"] = {"symptomatic": _plain(text["symptomatic"]),
                                         "deaths": _plain(text["deaths"])}
            continue
        if not any(_plain(text[c]) for c in ("date", "venue", "symptomatic", "ref")):
            report["placeholder_rows"] += 1        # e.g. a province listed with no incidents
            continue
        refs: list[str] = []
        for value in text.values():
            refs.extend(r for r in _refs_in(value) if r not in refs)
        rows.append(TableRow(
            row_id=f"{source_page or 'page'}#r{i:04d}",
            source_page=source_page,
            table_row=i,
            date=_plain(text["date"]),
            province=_plain(text["province"]), province_link=_link_target(text["province"]),
            kabkota=_plain(text["kabkota"]), kabkota_link=_link_target(text["kabkota"]),
            venue=_plain(text["venue"]),
            symptomatic=_plain(text["symptomatic"]), deaths=_plain(text["deaths"]),
            ref_ids=refs,
            origins={c: g.origin[0] for c, g in zip(COLUMNS, gcells)},
        ))
    return rows, report


def write_jsonl(rows: Iterable, path: Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            payload = row.as_dict() if hasattr(row, "as_dict") else row
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            count += 1
    return count


def parse_file(wikitext_path: Path, out_dir: Path, section: str = "Indonesia") -> dict:
    """Run stage 2 over one cached .wikitext file."""
    wikitext_path, out_dir = Path(wikitext_path), Path(out_dir)
    source_page = wikitext_path.stem
    text = wikitext_path.read_text(encoding="utf-8")
    citations = extract_citations(text, source_page)
    rows, report = extract_incident_table(text, source_page, section)
    n_cit = write_jsonl(citations, out_dir / f"{source_page}.citations.jsonl")
    n_rows = write_jsonl(rows, out_dir / f"{source_page}.rows.jsonl")
    (out_dir / f"{source_page}.table_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"source_page": source_page, "citations": n_cit, "rows": n_rows, **report}
