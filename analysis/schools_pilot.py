"""Pilot: can incident venues be matched to the official school register, and how many?

    python analysis/schools_pilot.py "Kabupaten Bantul" "Kabupaten Bandung Barat"

Source: Data Referensi Kemendikdasmen (referensi.data.kemendikdasmen.go.id), the public register
browsable province -> regency -> district -> school list, with latitude/longitude on each school's
profile page. There is no bulk download (the Satu Data portal's robots.txt says `Disallow: /`, so it is
not crawled), and a national crawl would be hundreds of thousands of requests, so this pilot loads ONE
regency at a time and measures name matching before anything is scaled.

Politeness: one request at a time, 9 s plus jitter, the shared User-Agent, a pause on 429/503.
Privacy: school profile pages include the principal's name and contact details. Only NPSN, name, address,
status and coordinates are extracted, and raw profile pages are NOT stored. Listing pages (no personal
data) are kept gzipped as evidence.

This is a pilot, not a pipeline stage: coordinates for only the matched venues would read as a map of where
incidents happened, so the match rate decides whether a wider load is worth it.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from mbgpipe import articles as A
from mbgpipe.config import user_agent

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://referensi.data.kemendikdasmen.go.id"
LEVELS = ("dikdas", "dikmen", "paud")     # SD/SMP(+MI/MTs), SMA/SMK(+MA), TK/KB/PAUD
RAW = ROOT / "data" / "raw" / "schools_pilot"
UA = user_agent()
THROTTLE = A.DomainThrottle(delay=9.0, jitter=0.3)
BREAKER = A.HostBreaker()


def get(url: str, keep: bool = True) -> str:
    """Polite GET. Listing pages are kept (gzipped) as evidence; profile pages are not."""
    if BREAKER.is_open(url):
        time.sleep(BREAKER.remaining(url))
    THROTTLE.wait(url)
    r = A.http_fetch(url, UA, timeout=60)
    BREAKER.record(url, r.http_status)
    if r.http_status != 200:
        print(f"  ! HTTP {r.http_status} {url[-60:]} {r.error[:50]}", file=sys.stderr)
        return ""
    if keep:
        RAW.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^a-z0-9]+", "_", url.replace(BASE, "").lower()).strip("_")
        (RAW / f"{name}.html.gz").write_bytes(gzip.compress(r.body))
        with open(RAW / "manifest.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"url": url, "bytes": len(r.body), "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
    return r.body.decode("utf-8", "replace")


def table_rows(html: str) -> list[tuple[list[str], list[str]]]:
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).replace("&nbsp;", " ").strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if cells:
            out.append((cells, re.findall(r'href="([^"]+)"', tr)))
    return out


def norm_region(name: str) -> str:
    name = re.sub(r"^(kab\.?|kabupaten|kota|kota administrasi)\s+", "", name.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_regency(kabkota_name: str, province_code: str) -> tuple[str, str]:
    """Portal region code of a regency, from its province page."""
    html = get(f"{BASE}/pendidikan/dikdas/{province_code}/1")
    want = norm_region(kabkota_name)
    for cells, links in table_rows(html):
        if len(cells) > 1 and norm_region(cells[1]) == want and links:
            return cells[1], links[0].rstrip("/").split("/")[-2]
    raise LookupError(f"{kabkota_name} not on the province page {province_code}")


def load_regency(kabkota_name: str, province_code: str) -> pd.DataFrame:
    label, code = find_regency(kabkota_name, province_code)
    html = get(f"{BASE}/pendidikan/dikdas/{code}/2")
    kecamatan = [(c[1], l[0].rstrip("/").split("/")[-2]) for c, l in table_rows(html) if len(c) > 1 and l and c[0].isdigit()]
    print(f"{label}: portal code {code}, {len(kecamatan)} kecamatan", file=sys.stderr)
    rows = []
    for level in LEVELS:
        for name, kec in kecamatan:
            page = get(f"{BASE}/pendidikan/{level}/{kec}/3")
            for cells, links in table_rows(page):
                if len(cells) >= 6 and cells[1].isdigit() and links:
                    rows.append({"kabkota": kabkota_name, "kecamatan": name, "level_portal": level, "npsn": cells[1],
                                 "school": cells[2], "address": cells[3], "kelurahan": cells[4], "status": cells[5],
                                 "profile": links[0]})
    print(f"  {len(rows)} schools listed", file=sys.stderr)
    return pd.DataFrame(rows)


# --- name matching -----------------------------------------------------------------------------------------

# "SD Negeri", "SD N" and "SDN" are one thing. Wikipedia writes both forms and the register uses several,
# and the register marks private schools with a trailing S (MTSS, SMKS, MAS, MIS, RAS).
TYPES = ["sd", "smp", "sma", "smk", "mi", "mts", "ma", "tk", "kb", "paud", "ra", "slb", "sps", "tpa"]
FILLER = {"negeri", "swasta", "kabupaten", "kab", "kota", "desa", "ds", "kelurahan", "kel", "kec", "kecamatan", "al", "dan", "di"}
SPELLING_RATIO = 0.86


def name_tokens(name: str) -> tuple[str, str, set[str], set[str]]:
    """(type, state 'n'/'s'/'', numbers, place/name words). State is state (negeri) or private (swasta)."""
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    s = re.sub(r"\b(sd|smp|sma|smk|mi|mts|ma)\s+negeri\b", r"\1n", s)
    s = re.sub(r"\b(sd|smp|sma|smk|mi|mts|ma)\s+n\b", r"\1n", s)
    toks = s.split()
    first = toks[0] if toks else ""
    kind, state = "", ""
    for t in TYPES:
        for suffix, st in (("", ""), ("n", "n"), ("s", "s")):
            if first == t + suffix:
                kind, state = t, st
                break
        if kind:
            break
    if not kind:
        return "", "", set(), set(toks) - FILLER
    rest = toks[1:]
    numbers = {t.lstrip("0") or "0" for t in rest if t.isdigit()}        # "01" and "1" are the same school
    words = {t for t in rest if not t.isdigit() and t not in FILLER}
    return kind, state, numbers, words


def _spelling_subset(words: set[str], candidates: set[str]) -> bool:
    """Every venue word has a near-identical word in the school's name (fiqri/fikri, manarul/manaarul)."""
    import difflib
    return bool(words) and all(w in candidates or difflib.get_close_matches(w, candidates, n=1, cutoff=SPELLING_RATIO) for w in words)


def match_venue(venue: str, schools: pd.DataFrame) -> tuple[str, list[str], str]:
    """(result, npsn list, tier). Type and numbers must agree. The venue's words must appear in the school's
    NAME (tier "name"); only if none does are its address and village tried (tier "address"), and only after
    that are near-identical spellings accepted (tier "spelling"). A tier is only used when the tier before it
    found nothing, and two or more candidates in a tier make the venue ambiguous rather than guessed."""
    kind, state, numbers, words = name_tokens(venue)
    if not kind:
        return "none", [], ""
    pool = []
    for s in schools.itertuples():
        k2, st2, n2, w2 = name_tokens(s.school)
        if k2 != kind or n2 != numbers or (state and st2 != state):
            continue
        place = set(re.sub(r"[^a-z0-9 ]+", " ", f"{s.kelurahan} {s.address} {s.kecamatan}".lower()).split())
        pool.append((s.npsn, w2, place))
    for tier, test in (("name", lambda w2, place: words <= w2),
                       ("address", lambda w2, place: words <= (w2 | place)),
                       ("spelling", lambda w2, place: _spelling_subset(words, w2))):
        hits = [npsn for npsn, w2, place in pool if test(w2, place)]
        if hits:
            hits = list(dict.fromkeys(hits))
            return ("matched" if len(hits) == 1 else "ambiguous"), hits, tier
    return "none", [], ""


COORDS_CSV = ROOT / "data" / "reference" / "schools_pilot_coords.csv"


def _coords_cache() -> dict[str, tuple[float, float]]:
    if not COORDS_CSV.exists():
        return {}
    df = pd.read_csv(COORDS_CSV, dtype={"npsn": str})
    return {r.npsn: (r.lat, r.lng) for r in df.itertuples()}


def profile_coords(url: str) -> tuple[float | None, float | None]:
    npsn = url.rstrip("/").split("/")[-1]
    cache = _coords_cache()
    if npsn in cache:                                # a school already fetched is never fetched again
        return cache[npsn]
    html = get(url, keep=False)                     # profile pages hold personal data: never stored
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    la = re.search(r"Lintang\s*:?\s*(-?\d+\.\d+)", text)
    lo = re.search(r"Bujur\s*:?\s*(-?\d+\.\d+)", text)
    if not (la and lo):
        return None, None
    with open(COORDS_CSV, "a", encoding="utf-8") as fh:
        if fh.tell() == 0:
            fh.write("npsn,lat,lng\n")
        fh.write(f"{npsn},{la.group(1)},{lo.group(1)}\n")
    return float(la.group(1)), float(lo.group(1))


def km(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    x = math.sin((la2 - la1) * p / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


PROVINCE_CODE = {"DI Yogyakarta": "040000", "Jawa Barat": "020000", "Jawa Timur": "050000", "Jawa Tengah": "030000",
                 "DKI Jakarta": "010000"}


def main(argv=None) -> int:
    names = [a for a in (argv if argv is not None else sys.argv[1:]) if not a.startswith("--")] or ["Kabupaten Bantul"]
    inc = pd.read_csv(ROOT / "data/processed/incidents.csv")
    ref = pd.read_csv(ROOT / "data/reference/kabkota_kemendagri.csv", dtype=str)
    report = []
    for kab in names:
        rows = inc[inc.kabkota == kab]
        province = rows.province.iloc[0]
        saved = ROOT / "data" / "reference" / f"schools_pilot_{re.sub(r'[^a-z]', '', kab.lower())}.csv"
        if "--reuse" in sys.argv and saved.exists():
            schools = pd.read_csv(saved, dtype=str)                 # no requests for the listing pages
        else:
            schools = load_regency(kab, PROVINCE_CODE[province])
            schools.to_csv(saved, index=False, encoding="utf-8")
        centre = ref[ref.nama == kab].iloc[0]
        for r in rows.itertuples():
            if not isinstance(r.venue, str):
                report.append({"kabkota": kab, "venue": r.venue_raw, "level": r.venue_level, "result": "no venue named", "npsn": ""})
                continue
            res, hits, tier = match_venue(r.venue, schools)
            lat = lng = None
            dist = None
            if res == "matched":
                lat, lng = profile_coords(schools.loc[schools.npsn == hits[0], "profile"].iloc[0])
                dist = km((lat, lng), (float(centre.lat), float(centre.lng))) if lat is not None else None
            report.append({"kabkota": kab, "venue": r.venue, "level": r.venue_level, "result": res, "tier": tier,
                           "npsn": ",".join(hits[:3]), "lat": lat, "lng": lng, "km_from_regency_centre": None if dist is None else round(dist, 1)})
    df = pd.DataFrame(report)
    out = ROOT / "data" / "reference" / "schools_pilot_matches.csv"
    df.to_csv(out, index=False, encoding="utf-8")
    print(df.groupby("kabkota").result.value_counts().unstack(fill_value=0).to_string())
    print()
    print(df.groupby("level", dropna=False).result.value_counts().unstack(fill_value=0).to_string())
    print("\nmatched with coordinates:", int(df.get("lat", pd.Series(dtype=float)).notna().sum()), "| max km from regency centre:",
          df.km_from_regency_centre.max())
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
