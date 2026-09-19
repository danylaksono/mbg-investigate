"""Build data/reference/kabkota_kemendagri.csv from a public Kemendagri kode-wilayah dump.

Source: github.com/cahyadsn/wilayah (MIT), db/wilayah_level_1_2.sql, which its header
states follows Kepmendagri No 300.2.2-2138 Tahun 2025. Download it to
data/reference/wilayah_level_1_2.sql first:

    curl -o data/reference/wilayah_level_1_2.sql \
      https://raw.githubusercontent.com/cahyadsn/wilayah/master/db/wilayah_level_1_2.sql

The dump is 23 MB because it embeds boundary polygons; only the small columns are kept.
Codes are Kemendagri codes ("11.01"), which differ from BPS codes ("1101") for some regions.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

REF = Path(__file__).resolve().parents[1] / "data" / "reference"
STR = r"'(?:[^'\\]|\\.|'')*'"
NUM = r"[-\d.eE]+|NULL"
RECORD = re.compile(
    rf"\('(?P<kode>[\d.]+)',\s*(?P<nama>{STR}),\s*(?P<ibukota>NULL|{STR}),\s*(?P<lat>{NUM}),\s*(?P<lng>{NUM}),\s*"
    rf"(?P<elv>{NUM}),\s*(?P<tz>{NUM}),\s*(?P<luas>{NUM}),\s*(?P<penduduk>{NUM}),")


def unquote(value: str) -> str:
    return value[1:-1].replace("\\'", "'").replace("''", "'")


def main() -> int:
    raw = (REF / "wilayah_level_1_2.sql").read_text(encoding="utf-8")
    body = raw[raw.index("INSERT INTO `wilayah_level_1_2`"):]
    rows = [{**m.groupdict(), "nama": unquote(m["nama"])} for m in RECORD.finditer(body)]
    provinces = {r["kode"]: r["nama"] for r in rows if "." not in r["kode"]}
    kabkota = [r for r in rows if r["kode"].count(".") == 1]
    print(f"{len(provinces)} provinces, {len(kabkota)} kabupaten/kota")
    with open(REF / "kabkota_kemendagri.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["kode", "provinsi_kode", "provinsi", "nama", "lat", "lng", "luas_km2", "penduduk"])
        for r in kabkota:
            pk = r["kode"].split(".")[0]
            writer.writerow([r["kode"], pk, provinces.get(pk, ""), r["nama"], r["lat"], r["lng"], r["luas"], r["penduduk"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
