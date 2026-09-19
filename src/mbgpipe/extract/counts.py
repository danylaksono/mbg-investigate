"""Victim counts from a table cell such as "33 Santri", "± 99 Siswa",
"45 Siswa + 3 Guru", "Ratusan Siswa" or "TBA (Tidak disebutkan)".

Three things make this harder than pulling out integers:

1.  Indonesian uses "." as the thousands separator. "1.333 Siswa" is 1333 people.
2.  Some cells are vague ("Puluhan", "Ratusan"). Coercing those to a number
    fabricates data; dropping them fabricates an absence. They are recorded as
    bounds with a null point estimate.
3.  Populations inside one cell are DISJOINT: "45 Siswa + 3 Guru" is 48 people.
    That is the opposite of prose like "400 pelajar, 12 di antaranya dirawat",
    where the second figure is nested inside the first. The table never nests, so
    terms are summed and the per-subject breakdown kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Bounds for vague quantifiers. Deliberately wide: reporting conventions, not measurements.
VAGUE = {"belasan": (11, 19), "puluhan": (20, 99), "ratusan": (100, 999), "ribuan": (1000, 9999)}

SUBJECTS = {
    "siswa": "student", "siswi": "student", "murid": "student", "pelajar": "student",
    "santri": "santri", "santriwati": "santri",
    "anak": "child", "balita": "child",
    "guru": "teacher", "kepala sekolah": "principal",
    "warga": "resident", "orang": "person",
}

_NUM = r"\d{1,3}(?:\.\d{3})+|\d+"
_SUBJ = "|".join(sorted(SUBJECTS, key=len, reverse=True))
RE_TERM = re.compile(rf"(?P<n>{_NUM})\s*(?P<subj>{_SUBJ})\b", re.I)
RE_VAGUE = re.compile(rf"\b(?P<v>{'|'.join(VAGUE)})\s+(?P<subj>{_SUBJ})\b", re.I)
RE_SUBJ_WORD = re.compile(rf"\b(?:{_SUBJ})\b", re.I)
RE_APPROX = re.compile(r"^\s*(?:±|~|sekitar\b)", re.I)
RE_LOWER = re.compile(r"^\s*(?:>|≥|lebih dari\b|minimal\b)", re.I)


@dataclass
class CountCell:
    kind: str                       # exact | approx | lower_bound | vague | unreported | unparsed
    total: Optional[int] = None     # point estimate; None for vague / unreported
    minimum: Optional[int] = None
    maximum: Optional[int] = None   # None = unbounded above (lower_bound)
    by_subject: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    raw: str = ""


def parse_number(token: str) -> int:
    """'1.333' -> 1333, '342' -> 342."""
    return int(token.replace(".", ""))


def parse_count_cell(raw: str) -> CountCell:
    text = (raw or "").strip()
    if not text or text.upper().startswith("TBA"):
        return CountCell("unreported", raw=text)

    terms = list(RE_TERM.finditer(text))
    if terms:
        by_subject: dict[str, int] = {}
        for m in terms:
            label = SUBJECTS[m["subj"].lower()]
            by_subject[label] = by_subject.get(label, 0) + parse_number(m["n"])
        total = sum(by_subject.values())
        kind = "approx" if RE_APPROX.match(text) else "lower_bound" if RE_LOWER.match(text) else "exact"
        flags = []
        if RE_SUBJ_WORD.search(RE_TERM.sub(" ", text)):
            # "99 Siswa dan Guru": a subject with no figure of its own, so the total
            # is at best a floor and may already include the teachers.
            flags.append("unnumbered_subject")
        return CountCell(kind, total, total, None if kind == "lower_bound" else total,
                         by_subject, flags, text)

    vague = RE_VAGUE.search(text)
    if vague:
        lo, hi = VAGUE[vague["v"].lower()]
        return CountCell("vague", None, lo, hi, {}, [], text)
    return CountCell("unparsed", raw=text)
