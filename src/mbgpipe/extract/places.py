"""Place names from table cells.

The source table links every province and kabupaten/kota to its own Wikipedia
article, so the link *target* is a canonical name and no gazetteer matching or NER
is needed. That also keeps "Kabupaten Bandung" and "Kota Bandung" apart, which is
exactly the distinction a choropleth depends on.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROVINCES = [
    "Aceh", "Sumatera Utara", "Sumatera Barat", "Riau", "Jambi",
    "Sumatera Selatan", "Bengkulu", "Lampung", "Kepulauan Bangka Belitung",
    "Kepulauan Riau", "DKI Jakarta", "Jawa Barat", "Jawa Tengah",
    "DI Yogyakarta", "Jawa Timur", "Banten", "Bali", "Nusa Tenggara Barat",
    "Nusa Tenggara Timur", "Kalimantan Barat", "Kalimantan Tengah",
    "Kalimantan Selatan", "Kalimantan Timur", "Kalimantan Utara",
    "Sulawesi Utara", "Sulawesi Tengah", "Sulawesi Selatan",
    "Sulawesi Tenggara", "Gorontalo", "Sulawesi Barat", "Maluku",
    "Maluku Utara", "Papua", "Papua Barat", "Papua Selatan", "Papua Tengah",
    "Papua Pegunungan", "Papua Barat Daya",
]

# Wikipedia article titles that differ from the short official form.
ALIASES = {
    "daerah istimewa yogyakarta": "DI Yogyakarta", "yogyakarta": "DI Yogyakarta",
    "daerah khusus ibukota jakarta": "DKI Jakarta", "daerah khusus ibu kota jakarta": "DKI Jakarta",
    "jakarta": "DKI Jakarta", "nanggroe aceh darussalam": "Aceh",
}

_BY_LOWER = {p.lower(): p for p in PROVINCES}


def canonical_province(*names: str) -> Optional[str]:
    """First of `names` that is a known province, in its canonical spelling."""
    for name in names:
        key = (name or "").strip().lower()
        if key in _BY_LOWER:
            return _BY_LOWER[key]
        if key in ALIASES:
            return ALIASES[key]
    return None


def kabkota_parts(name: str) -> tuple[Optional[str], str]:
    """'Kabupaten Aceh Utara' -> ('kabupaten', 'Aceh Utara'); 'Kota Bandung' -> ('kota', 'Bandung').

    Type is None when the name carries no prefix (an unlinked or unusual cell).
    """
    first, _, rest = (name or "").strip().partition(" ")
    if first.lower() in ("kabupaten", "kota") and rest:
        if first.lower() == "kota" and rest.startswith("Administrasi "):
            rest = rest[len("Administrasi "):]
        return first.lower(), rest
    return None, (name or "").strip()


# --- Kemendagri kode wilayah -------------------------------------------------
#
# Codes come from data/reference/kabkota_kemendagri.csv (built by
# analysis/build_kode_wilayah.py from a public dump of the 514 kabupaten/kota). The join is
# by exact name. Fuzzy matching is deliberately not used: the nearest string to "Kabupaten
# Ogan Komering Ilir" is "Ogan Komering Ulu", a different regency.
#
# Names on the source page that differ from the master list, each checked by hand.
# key: table name (lowercase); value: master-list name (lowercase).
KODE_ALIASES = {
    "kota palangka raya": "kota palangkaraya",
    "kota baubau": "kota bau bau",
    "kabupaten tojo una-una": "kabupaten tojo una una",
    "kabupaten polewali manda": "kabupaten polewali mandar",   # typo on the source page
    "kabupaten bombaba": "kabupaten bombana",                    # typo on the source page
    # The master list truncates code 16.02 (capital Kayu Agung) to "Ogan Komering".
    "kabupaten ogan komering ilir": "kabupaten ogan komering",
}


@dataclass
class Kabkota:
    kode: str
    provinsi_kode: str
    provinsi: str               # canonical province name
    nama: str
    lat: Optional[float]
    lng: Optional[float]
    luas_km2: Optional[float]
    penduduk: Optional[int]


def _norm(name: str) -> str:
    return " ".join((name or "").lower().split())


def _num(value: str, cast):
    try:
        return cast(float(value))
    except (TypeError, ValueError):
        return None


def load_kabkota_codes(path: Path) -> dict[str, Kabkota]:
    """Master list keyed by normalised name."""
    out: dict[str, Kabkota] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[_norm(row["nama"])] = Kabkota(
                kode=row["kode"], provinsi_kode=row["provinsi_kode"],
                provinsi=canonical_province(row["provinsi"]) or row["provinsi"], nama=row["nama"].strip(),
                lat=_num(row["lat"], float), lng=_num(row["lng"], float),
                luas_km2=_num(row["luas_km2"], float), penduduk=_num(row["penduduk"], int))
    return out


def lookup_kabkota(codes: dict[str, Kabkota], name: str) -> tuple[Optional[Kabkota], str]:
    """(record, how) with how in "exact" | "alias" | "none"."""
    key = _norm(name)
    if key in codes:
        return codes[key], "exact"
    alias = KODE_ALIASES.get(key)
    if alias and alias in codes:
        return codes[alias], "alias"
    return None, "none"
