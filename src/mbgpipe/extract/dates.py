"""Indonesian-language date extraction.

Shapes handled, from the incident table's date column and from citation dates:

    "23 April 2025"                        -> exact day
    "7 s/d 9 Mei 2025"                     -> day range (7 May to 9 May)
    "September 2025", "Awal Februari 2026" -> month only
    "Tidak Disebutkan"                     -> no date
    "(22/9/2025)"                          -> numeric, dd/m/yyyy (never mm/dd)

Numeric dates are day-first: 3/4/2026 is 3 April, always.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Optional

MONTHS = {
    "januari": 1, "februari": 2, "pebruari": 2, "maret": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "agustus": 8, "september": 9,
    "oktober": 10, "november": 11, "nopember": 11, "desember": 12,
}

# Month name alternation, longest first so "pebruari" cannot be eaten by "februari".
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

# "16 Juli 2013" / "16 Juli" (year supplied by context)
RE_DMY = re.compile(
    rf"\b(?P<day>\d{{1,2}})\s+(?P<month>{_MONTH_ALT})\s*(?P<year>\d{{4}})?\b",
    re.IGNORECASE,
)

# "bulan September 2025" / "September 2025" with no preceding day
RE_MY = re.compile(
    rf"\b(?:bulan\s+)?(?P<month>{_MONTH_ALT})\s+(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)

# "(22/9/2025)" or "22/9/2025" — the parenthesised form is the house style for
# attaching a date to a day name, e.g. "Senin (22/9/2025)".
RE_NUMERIC = re.compile(r"\b(?P<day>\d{1,2})/(?P<month>\d{1,2})/(?P<year>\d{4})\b")


@dataclass
class DateMatch:
    """A date found in text, with its precision recorded rather than guessed away."""

    iso: str                  # "2025-04-23" or "2025-09" — never padded to fake a day
    precision: str            # "day" | "month"
    raw: str                  # the substring that produced it
    start: int
    end: int

    def as_dict(self) -> dict:
        return asdict(self)


def _valid(day: int, month: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= 31


def find_dates(text: str, default_year: Optional[int] = None) -> list[DateMatch]:
    """Return every date in `text`, ordered by position, de-overlapped.

    `default_year` fills in bare "23 April" constructions, which appear when a
    bullet continues a date established by the preceding sentence. Leave it None
    to drop those rather than invent a year.
    """
    found: list[DateMatch] = []

    for m in RE_NUMERIC.finditer(text):
        day, month, year = int(m["day"]), int(m["month"]), int(m["year"])
        if _valid(day, month):
            found.append(
                DateMatch(f"{year:04d}-{month:02d}-{day:02d}", "day", m.group(0), m.start(), m.end())
            )

    for m in RE_DMY.finditer(text):
        day = int(m["day"])
        month = MONTHS[m["month"].lower()]
        year = int(m["year"]) if m["year"] else default_year
        if year is None or not _valid(day, month):
            continue
        found.append(
            DateMatch(f"{year:04d}-{month:02d}-{day:02d}", "day", m.group(0), m.start(), m.end())
        )

    for m in RE_MY.finditer(text):
        month = MONTHS[m["month"].lower()]
        year = int(m["year"])
        found.append(DateMatch(f"{year:04d}-{month:02d}", "month", m.group(0), m.start(), m.end()))

    # Overlap resolution: a day-precision match always beats a month-precision
    # match covering the same span, because RE_MY happily matches the tail of
    # "16 Juli 2013".
    found.sort(key=lambda d: (d.start, 0 if d.precision == "day" else 1))
    kept: list[DateMatch] = []
    for d in found:
        if any(d.start < k.end and k.start < d.end for k in kept):
            continue
        kept.append(d)
    return kept


def primary_date(text: str, default_year: Optional[int] = None) -> Optional[DateMatch]:
    """The first date in the text.

    In this corpus the incident date leads the sentence and any later date is a
    follow-up (hospital discharge, a status declaration). First-wins is right far
    more often than last-wins, but check `raw` before trusting it downstream.
    """
    dates = find_dates(text, default_year=default_year)
    return dates[0] if dates else None


# "7 s/d 9 Mei 2025" / "30 April s/d 2 Mei 2025" ("s/d" = sampai dengan, "up to")
RE_RANGE = re.compile(
    rf"\b(?P<d1>\d{{1,2}})(?:\s+(?P<m1>{_MONTH_ALT}))?\s*(?:s/?d\.?|sd|-|–)\s*"
    rf"(?P<d2>\d{{1,2}})\s+(?P<m2>{_MONTH_ALT})\s+(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)


@dataclass
class DateCell:
    start: str                # ISO date, or ISO month for month precision
    end: str                  # equals `start` unless a range
    precision: str            # "day" | "month"
    raw: str


def parse_date_cell(text: str) -> Optional[DateCell]:
    """One table date cell -> a DateCell, or None ("Tidak Disebutkan")."""
    m = RE_RANGE.search(text)
    if m:
        y, m2 = int(m["year"]), MONTHS[m["m2"].lower()]
        m1 = MONTHS[m["m1"].lower()] if m["m1"] else m2
        d1, d2 = int(m["d1"]), int(m["d2"])
        if _valid(d1, m1) and _valid(d2, m2):
            return DateCell(f"{y:04d}-{m1:02d}-{d1:02d}", f"{y:04d}-{m2:02d}-{d2:02d}", "day", text)
    first = primary_date(text)
    return DateCell(first.iso, first.iso, first.precision, text) if first else None
