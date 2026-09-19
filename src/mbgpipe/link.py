"""Stage 7 — link independently discovered articles to incident events, offline.

The rule is deliberately conservative, because a wrong link is invisible in an aggregate:

    an article links to an event only if
      1. its headline or lead paragraph names the event's venue or its kabupaten/kota, AND
      2. its date falls in [date_start - 1 day, date_end + WINDOW_DAYS], AND
      3. exactly ONE event matches.

A named venue is specific evidence and a bare place is general, so venue matches win. Two or
more matches make the article `ambiguous`: the candidates are recorded and it is never assigned
to the nearest. A place named only deep in the body is `weak`: kept as evidence, not linked,
because roundups and follow-ups name many places in passing.

Every decision carries the tier and the terms that matched, so it can be re-examined later.
Kabupaten X and Kota X share a name (Kediri, Bandung, Malang, ...), so ambiguity is common and
is reported rather than guessed away.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

from .extract.places import kabkota_parts

RULES_VERSION = "2026-09-19.1"
WINDOW_DAYS = 30
LEAD_CHARS = 500          # the lead paragraph: where an article says where it happened

RE_NONALNUM = re.compile(r"[^a-z0-9]+")


def norm(text: str) -> str:
    """Lowercase, strip accents, punctuation -> single spaces, padded so ' x ' tests words."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return f" {RE_NONALNUM.sub(' ', text).strip()} "


def place_forms(kabkota_name: str) -> list[str]:
    """Written forms of a place name: as-is and run together ('Kulon Progo' -> 'kulonprogo'),
    since Indonesian headlines write both."""
    base = norm(kabkota_name).strip()
    if not base:
        return []
    return sorted({f" {base} ", f" {base.replace(' ', '')} "})


@dataclass
class EventKey:
    event_id: str
    start: date
    end: date
    places: list[str]
    venues: list[str] = field(default_factory=list)


@dataclass
class Match:
    event_id: str
    kind: str            # "venue" | "place"
    term: str            # the normalised term that matched


@dataclass
class LinkResult:
    decision: str                        # "linked" | "ambiguous" | "weak" | "none"
    event_ids: list[str]
    tier: str = ""                       # "headline_or_lead" | "body_only" | ""
    matches: list[Match] = field(default_factory=list)


def _to_date(value, end_of_month: bool = False) -> Optional[date]:
    if not isinstance(value, str) or not value:      # None, "" or a dataframe's NaN
        return None
    try:
        if len(value) == 7:                          # month precision "2025-08"
            y, m = int(value[:4]), int(value[5:7])
            first = date(y, m, 1)
            return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)) if end_of_month else first
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def event_keys(events: Iterable[dict]) -> list[EventKey]:
    """EventKey per event that has a date and a place. Events with no date cannot be windowed."""
    keys = []
    for e in events:
        start = _to_date(e.get("date_start"))
        last = e.get("date_end") if isinstance(e.get("date_end"), str) and e.get("date_end") else e.get("date_start")
        end = _to_date(last, end_of_month=True)
        name = e.get("kabkota_name")
        if not isinstance(name, str) or not name:
            full = e.get("kabkota")
            name = kabkota_parts(full)[1] if isinstance(full, str) else ""
        if start is None or end is None or not name:
            continue
        venues = [norm(v) for v in e.get("venues", []) if len(norm(v).strip()) >= 8 and "spesifik" not in v.lower()]
        keys.append(EventKey(e["event_id"], start, max(start, end), place_forms(name), venues))
    return keys


def in_window(when: date, k: EventKey) -> bool:
    return k.start - timedelta(days=1) <= when <= k.end + timedelta(days=WINDOW_DAYS)


def matches(text: str, when: Optional[date], keys: list[EventKey]) -> list[Match]:
    """Events whose window contains `when` and that `text` names.

    When any event is matched by venue, only those are returned: "MAN 2 Rembang" picks its
    event even though two events in Rembang are in the window.
    """
    if when is None:
        return []
    haystack = norm(text)
    by_venue, by_place = [], []
    for k in keys:
        if not in_window(when, k):
            continue
        venue = next((v for v in k.venues if v in haystack), None)
        if venue:
            by_venue.append(Match(k.event_id, "venue", venue.strip()))
            continue
        place = next((f for f in k.places if f in haystack), None)
        if place:
            by_place.append(Match(k.event_id, "place", place.strip()))
    return by_venue or by_place


def names_event(text: str, key: EventKey) -> Optional[Match]:
    """Does `text` name this event's venue or place, ignoring dates? Used to audit a citation:
    it separates 'the article is about somewhere else' from 'the dates disagree'."""
    haystack = norm(text)
    venue = next((v for v in key.venues if v in haystack), None)
    if venue:
        return Match(key.event_id, "venue", venue.strip())
    place = next((f for f in key.places if f in haystack), None)
    return Match(key.event_id, "place", place.strip()) if place else None


def candidates(text: str, when: Optional[date], keys: list[EventKey]) -> list[str]:
    return [m.event_id for m in matches(text, when, keys)]


def link(text: str, when: Optional[date], keys: list[EventKey]) -> tuple[str, list[str]]:
    """Headline-only linking: (decision, event ids) with decision 'linked' | 'ambiguous' | 'none'."""
    found = candidates(text, when, keys)
    if len(found) == 1:
        return "linked", found
    return ("ambiguous", found) if found else ("none", [])


def link_article(title: str, body: str, when: Optional[date], keys: list[EventKey]) -> LinkResult:
    """Full-text linking. Headline and lead are strong evidence; the rest of the body is weak."""
    strong = matches(f"{title}\n{body[:LEAD_CHARS]}", when, keys)
    if len(strong) == 1:
        return LinkResult("linked", [strong[0].event_id], "headline_or_lead", strong)
    if strong:
        return LinkResult("ambiguous", [m.event_id for m in strong], "headline_or_lead", strong)
    weak = matches(body, when, keys)
    if weak:
        return LinkResult("weak", [m.event_id for m in weak], "body_only", weak)
    return LinkResult("none", [])
