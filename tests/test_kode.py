"""Kemendagri kode wilayah join: exact names, reviewed aliases, and no guessing."""

import json

import pytest

from mbgpipe import normalize
from mbgpipe.extract import places as P

MASTER = """kode,provinsi_kode,provinsi,nama,lat,lng,luas_km2,penduduk
11.08,11,Aceh,Kabupaten Aceh Utara,5.1,97.1,3236.86,641007
16.01,16,Sumatera Selatan,Kabupaten Ogan Komering Ulu,-4.0,104.0,3620.0,375000
16.02,16,Sumatera Selatan,Kabupaten Ogan Komering,-3.4,104.8,17075.7,801059
62.71,62,Kalimantan Tengah,Kota Palangkaraya,-2.2,113.9,2400.0,300000
31.72,31,Daerah Khusus Ibukota Jakarta,Kota Administrasi Jakarta Utara,-6.1,106.9,146.0,1832032
"""


@pytest.fixture()
def codes(tmp_path):
    path = tmp_path / "kabkota.csv"
    path.write_text(MASTER, encoding="utf-8")
    return P.load_kabkota_codes(path)


def test_exact_match_carries_code_coordinates_and_population(codes):
    rec, how = P.lookup_kabkota(codes, "Kabupaten Aceh Utara")
    assert how == "exact" and rec.kode == "11.08" and rec.penduduk == 641007
    assert (rec.lat, rec.lng) == (5.1, 97.1)


def test_province_names_are_canonicalised(codes):
    rec, _ = P.lookup_kabkota(codes, "Kota Administrasi Jakarta Utara")
    assert rec.provinsi == "DKI Jakarta"


def test_reviewed_alias_resolves_a_spelling_variant(codes):
    rec, how = P.lookup_kabkota(codes, "Kota Palangka Raya")
    assert how == "alias" and rec.kode == "62.71"


def test_ogan_komering_ilir_is_not_confused_with_ulu(codes):
    """The nearest string to 'Ilir' in the master list is 'Ulu', a different regency, which is
    why fuzzy matching is not used. 'Ilir' is 16.02, which the master list truncates."""
    rec, how = P.lookup_kabkota(codes, "Kabupaten Ogan Komering Ilir")
    assert how == "alias" and rec.kode == "16.02"
    assert P.lookup_kabkota(codes, "Kabupaten Ogan Komering Ulu")[0].kode == "16.01"


def test_unknown_name_is_left_unmatched_not_guessed(codes):
    assert P.lookup_kabkota(codes, "Kabupaten Aceh Utaraa") == (None, "none")


def _row(kabkota, province="Aceh"):
    return {"row_id": "t#r1", "source_page": "t", "table_row": 1, "date": "1 Mei 2025",
            "province": province, "province_link": province, "kabkota": kabkota.split(" ", 1)[1],
            "kabkota_link": kabkota, "venue": "SDN 1", "symptomatic": "5 Siswa", "deaths": "",
            "ref_ids": [], "origins": {c: 1 for c in
                                       ("date", "province", "kabkota", "venue", "symptomatic", "deaths", "ref")}}


def test_incident_gets_kode_and_flags_unmatched_names(codes):
    ok = normalize.normalize_row(_row("Kabupaten Aceh Utara"), {}, set(), codes)
    assert ok.kabkota_kode == "11.08" and ok.provinsi_kode == "11" and ok.kabkota_kode_match == "exact"
    assert "kabkota_no_kode" not in ok.flags
    bad = normalize.normalize_row(_row("Kabupaten Atlantis"), {}, set(), codes)
    assert bad.kabkota_kode == "" and "kabkota_no_kode" in bad.flags


def test_province_disagreement_with_the_master_list_is_flagged(codes):
    inc = normalize.normalize_row(_row("Kabupaten Aceh Utara", province="Sumatera Utara"), {}, set(), codes)
    assert "kabkota_province_mismatch" in inc.flags


def test_codes_are_optional():
    inc = normalize.normalize_row(_row("Kabupaten Aceh Utara"), {}, set(), None)
    assert inc.kabkota_kode == "" and "kabkota_no_kode" not in inc.flags


def test_event_rows_carry_the_kode(codes):
    inc = normalize.normalize_row(_row("Kabupaten Aceh Utara"), {}, set(), codes)
    event = normalize.build_events([inc])[0]
    assert event["kabkota_kode"] == "11.08" and event["kabkota_population"] == 641007
    json.dumps(event)   # still serialisable
