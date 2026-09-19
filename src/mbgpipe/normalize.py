"""Stage 3 — table rows in, a structured incident table and an event table out.

Every extracted value keeps its provenance (`*_raw`) and its uncertainty
(`date_precision`, `symptomatic_kind`, `flags`). Nothing is coerced into a clean
value it does not have: "Ratusan Siswa" is a null point estimate with bounds
100-999, not a fabricated 100.

The one thing that will silently wreck an analysis is rowspan. When the table
writes "33 Santri" once beside two venues, both rendered rows show 33. So:

    symptomatic          what the table shows on this row (carried down by rowspan)
    symptomatic_counted  the same figure on the FIRST row of its source cell only,
                         null elsewhere — sum THIS column, never `symptomatic`

`events.csv` is the same data collapsed to one row per source count cell.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable, Optional

from .extract import counts as C
from .extract import dates as D
from .extract import places as P

# Start of a venue name -> education level. Matched on the raw venue, so
# "SD (Spesifik tidak disebutkan)" still yields a level though it names no school.
LEVELS = [
    (re.compile(r"(?:TK\w*|PAUD|RA|KB)\b", re.I), "early_childhood"),
    (re.compile(r"(?:SD\w*|MI[NSM]?)\b", re.I), "primary"),
    (re.compile(r"(?:SMP\w*|MTs\w*)\b", re.I), "junior_secondary"),
    (re.compile(r"(?:SMA\w*|SMK\w*|MA[NSK]?)\b", re.I), "senior_secondary"),
    (re.compile(r"SLB\b", re.I), "special_needs"),
    (re.compile(r"(?:pesantren|ponpes|pondok)\b", re.I), "pesantren"),
    (re.compile(r"(?:posyandu|pasyandu)\b", re.I), "community_health_post"),
]
RE_UNSPECIFIED = re.compile(r"tidak\s+(?:disebutkan|diumumkan)|spesifik", re.I)
RE_REF_BASE = re.compile(r"\.\d+$")


@dataclass
class Incident:
    incident_id: str
    source_page: str
    table_row: int
    event_id: str = ""

    date_iso: Optional[str] = None
    date_end_iso: Optional[str] = None
    date_precision: Optional[str] = None          # "day" | "month"
    date_raw: str = ""

    province: Optional[str] = None
    province_raw: str = ""
    kabkota: Optional[str] = None                 # "Kabupaten Aceh Utara" (wikilink target)
    kabkota_type: Optional[str] = None
    kabkota_name: str = ""                        # "Aceh Utara"
    # From the Kemendagri master list (data/reference). Kode is the join key for maps and
    # other datasets; population and area are as given in that list, undated.
    kabkota_kode: str = ""                        # "11.08"
    kabkota_kode_match: str = ""                  # "exact" | "alias" | "none"
    provinsi_kode: str = ""
    kabkota_lat: Optional[float] = None           # centroid
    kabkota_lng: Optional[float] = None
    kabkota_population: Optional[int] = None
    kabkota_area_km2: Optional[float] = None
    venue: Optional[str] = None
    venue_level: Optional[str] = None
    venue_raw: str = ""

    symptomatic_raw: str = ""
    symptomatic_kind: str = ""
    symptomatic: Optional[int] = None
    symptomatic_min: Optional[int] = None
    symptomatic_max: Optional[int] = None
    symptomatic_by_subject: dict[str, int] = field(default_factory=dict)
    symptomatic_counted: Optional[int] = None
    deaths_raw: str = ""
    deaths: Optional[int] = None
    deaths_counted: Optional[int] = None

    ref_ids: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def venue_level(venue: str) -> Optional[str]:
    venue = venue.strip()
    return next((level for pattern, level in LEVELS if pattern.match(venue)), None)


def load_urls_by_ref(citations_path: Optional[Path]) -> dict[str, list[str]]:
    """Ref id -> every URL cited under it. A `<ref>` holding two templates is stored
    as `id` and `id.1` by stage 2; both fold back onto the base id a row cites."""
    urls: dict[str, list[str]] = {}
    if citations_path and Path(citations_path).exists():
        with open(citations_path, encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                url = row.get("archive_url") or row.get("url")
                if url:
                    urls.setdefault(RE_REF_BASE.sub("", row["ref_id"]), []).append(url)
    return urls


def normalize_row(row: dict, urls_by_ref: dict[str, list[str]], seen_cells: set,
                  kabkota_codes: Optional[dict] = None) -> Incident:
    """One table row -> one Incident. `seen_cells` is shared across the whole table so
    a figure written once and displayed on several rows is counted on the first only."""
    origins = row["origins"]
    inc = Incident(
        incident_id=row["row_id"], source_page=row["source_page"], table_row=row["table_row"],
        event_id=f"{row['source_page']}#e{origins['symptomatic']:04d}",
        ref_ids=row["ref_ids"],
        source_urls=[u for r in row["ref_ids"] for u in urls_by_ref.get(r, [])],
    )

    date = D.parse_date_cell(row["date"])
    inc.date_raw = row["date"]
    if date:
        inc.date_iso, inc.date_end_iso, inc.date_precision = date.start, date.end, date.precision
    else:
        inc.flags.append("no_date")
    if inc.date_precision == "month":
        inc.flags.append("month_precision_only")
    if date and date.end != date.start:
        inc.flags.append("date_range")

    inc.province_raw = row["province"]
    inc.province = P.canonical_province(row["province_link"], row["province"])
    inc.kabkota = row["kabkota_link"] or row["kabkota"] or None
    inc.kabkota_type, inc.kabkota_name = P.kabkota_parts(inc.kabkota or "")
    if inc.province is None:
        inc.flags.append("unknown_province")
    if not row["kabkota_link"]:
        inc.flags.append("kabkota_unlinked")
    if kabkota_codes is not None:
        rec, how = P.lookup_kabkota(kabkota_codes, inc.kabkota or "")
        inc.kabkota_kode_match = how
        if rec is None:
            inc.flags.append("kabkota_no_kode")
        else:
            inc.kabkota_kode, inc.provinsi_kode = rec.kode, rec.provinsi_kode
            inc.kabkota_lat, inc.kabkota_lng = rec.lat, rec.lng
            inc.kabkota_population, inc.kabkota_area_km2 = rec.penduduk, rec.luas_km2
            if inc.province and rec.provinsi != inc.province:
                inc.flags.append("kabkota_province_mismatch")   # the page lists it under another province

    inc.venue_raw = row["venue"]
    inc.venue_level = venue_level(row["venue"])
    inc.venue = None if not row["venue"] or RE_UNSPECIFIED.search(row["venue"]) else row["venue"]
    if inc.venue is None:
        inc.flags.append("no_venue")

    cell = C.parse_count_cell(row["symptomatic"])
    inc.symptomatic_raw, inc.symptomatic_kind = row["symptomatic"], cell.kind
    inc.symptomatic, inc.symptomatic_min, inc.symptomatic_max = cell.total, cell.minimum, cell.maximum
    inc.symptomatic_by_subject = cell.by_subject
    inc.flags.extend(cell.flags)
    if cell.kind in ("unreported", "vague", "unparsed"):
        inc.flags.append(f"count_{cell.kind}")
    key = ("symptomatic", origins["symptomatic"])
    if key in seen_cells:
        inc.flags.append("count_shared_across_rows")
    else:
        seen_cells.add(key)
        inc.symptomatic_counted = cell.total

    deaths = C.parse_count_cell(row["deaths"])
    inc.deaths_raw, inc.deaths = row["deaths"], deaths.total
    key = ("deaths", origins["deaths"])
    if key not in seen_cells:
        seen_cells.add(key)
        inc.deaths_counted = deaths.total

    if not inc.source_urls:
        inc.flags.append("no_source_url")
    return inc


def normalize_file(rows_path: Path, citations_path: Optional[Path] = None,
                   kabkota_codes: Optional[dict] = None) -> list[Incident]:
    urls_by_ref = load_urls_by_ref(citations_path)
    seen: set = set()
    with open(rows_path, encoding="utf-8") as fh:
        return [normalize_row(json.loads(line), urls_by_ref, seen, kabkota_codes) for line in fh]


def build_events(incidents: Iterable[Incident]) -> list[dict]:
    """Collapse rows that share one source count cell into a single event.

    No time-window or name matching: the table's own rowspans say which rows
    describe one incident, and guessing beyond that would merge distinct ones
    (a school hit two days running is two events).
    """
    groups: dict[str, list[Incident]] = {}
    for inc in incidents:
        groups.setdefault(inc.event_id, []).append(inc)

    events = []
    for event_id, rows in groups.items():
        first = rows[0]
        dates = sorted({r.date_iso for r in rows if r.date_iso})
        ends = sorted({r.date_end_iso for r in rows if r.date_end_iso})
        events.append({
            "event_id": event_id,
            "date_start": dates[0] if dates else None,
            "date_end": ends[-1] if ends else None,
            "date_precision": first.date_precision,
            "province": first.province,
            "kabkota": first.kabkota,
            "kabkota_type": first.kabkota_type,
            "kabkota_name": first.kabkota_name,
            "kabkota_kode": first.kabkota_kode,
            "kabkota_lat": first.kabkota_lat,
            "kabkota_lng": first.kabkota_lng,
            "kabkota_population": first.kabkota_population,
            "n_rows": len(rows),
            "venues": [r.venue_raw for r in rows if r.venue_raw],
            "venue_levels": sorted({r.venue_level for r in rows if r.venue_level}),
            "symptomatic": first.symptomatic,
            "symptomatic_min": first.symptomatic_min,
            "symptomatic_max": first.symptomatic_max,
            "symptomatic_kind": first.symptomatic_kind,
            "symptomatic_by_subject": first.symptomatic_by_subject,
            "deaths": sum(r.deaths_counted or 0 for r in rows) or None,
            "source_urls": sorted({u for r in rows for u in r.source_urls}),
            "incident_ids": [r.incident_id for r in rows],
            "flags": sorted({f for r in rows for f in r.flags}),
        })
    events.sort(key=lambda e: (e["date_start"] or "9999", e["event_id"]))
    return events


def qc_report(incidents: list[Incident], events: list[dict], wikipedia_total: Optional[dict]) -> dict:
    """Arithmetic and coverage checks, printed rather than buried in a test.

    The page's own TOTAL row is a hand-edited figure that goes stale (at the time of
    writing it reads ~11,390 against a table that has run for another year), so it is
    not a pass/fail oracle. What it does show is the DATE at which the rowspan-aware
    cumulative sum reaches it: if that is when the editors last updated the row,
    rowspans are being handled right; the naive per-row sum overshoots it many times over.
    """
    counted = sum(i.symptomatic_counted or 0 for i in incidents)
    naive = sum(i.symptomatic or 0 for i in incidents)
    page_total = None
    if wikipedia_total:
        m = re.search(r"\d{1,3}(?:\.\d{3})+|\d+", wikipedia_total.get("symptomatic", ""))
        page_total = C.parse_number(m.group(0)) if m else None

    reaches = None
    if page_total:
        running = 0
        for i in sorted((i for i in incidents if i.date_iso and i.symptomatic_counted),
                        key=lambda i: i.date_iso):
            running += i.symptomatic_counted
            if running >= page_total:
                reaches = i.date_iso
                break

    flag_counts: dict[str, int] = {}
    for inc in incidents:
        for f in inc.flags:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    dated = sorted(i.date_iso for i in incidents if i.date_iso)
    return {
        "incident_rows": len(incidents),
        "events": len(events),
        "date_span": [dated[0], dated[-1]] if dated else None,
        "symptomatic_counted_once_per_cell": counted,
        "symptomatic_if_summed_per_row_WRONG": naive,
        "rowspan_inflation_factor": round(naive / counted, 2) if counted else None,
        "wikipedia_total_row": page_total,
        "cumulative_reaches_wikipedia_total_on": reaches,
        "kabkota_with_kode": sum(1 for i in incidents if i.kabkota_kode),
        "kabkota_kode_via_alias": sorted({i.kabkota for i in incidents if i.kabkota_kode_match == "alias"}),
        "kabkota_without_kode": sorted({i.kabkota for i in incidents if i.kabkota_kode_match == "none"}),
        "deaths_counted": sum(i.deaths_counted or 0 for i in incidents),
        "events_with_vague_or_unreported_count": sum(
            1 for e in events if e["symptomatic_kind"] in ("vague", "unreported", "unparsed")),
        "flag_counts": dict(sorted(flag_counts.items(), key=lambda kv: -kv[1])),
    }


JSON_FIELDS = {"symptomatic_by_subject", "ref_ids", "source_urls", "flags",
               "venues", "venue_levels", "incident_ids"}


def write_csv(rows: Iterable, path: Path) -> int:
    """List and dict fields are written as JSON so URLs and subjects survive intact."""
    rows = [r.as_dict() if hasattr(r, "as_dict") else r for r in rows]
    if not rows:
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS else v
                             for k, v in row.items()})
    return len(rows)
