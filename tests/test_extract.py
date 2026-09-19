"""Cell-level extractors: counts, dates, places."""

import pytest

from mbgpipe import normalize
from mbgpipe.extract import counts as C
from mbgpipe.extract import dates as D
from mbgpipe.extract import places as P


# --- counts ------------------------------------------------------------------

def test_indonesian_thousands_separator():
    assert C.parse_number("1.333") == 1333
    assert C.parse_number("342") == 342
    assert C.parse_count_cell("1.333 Siswa").total == 1333, "a dotted group of three is thousands"


@pytest.mark.parametrize("raw,kind,total", [
    ("3 Siswa", "exact", 3),
    ("999 SIswa", "exact", 999),
    ("± 99 Siswa", "approx", 99),
    ("~40 siswa", "approx", 40),
    ("> 300 Santri", "lower_bound", 300),
    (">999 Siswa", "lower_bound", 999),
    ("777 Orang", "exact", 777),
])
def test_count_kinds(raw, kind, total):
    cell = C.parse_count_cell(raw)
    assert (cell.kind, cell.total) == (kind, total)


def test_lower_bound_has_no_upper_bound():
    assert C.parse_count_cell("> 300 Santri").maximum is None


def test_populations_in_one_cell_are_disjoint_and_summed():
    for raw in ("45 Siswa + 3 Guru", "45 Siswa dan 3 Guru", "45 Siswa 3 Guru"):
        cell = C.parse_count_cell(raw)
        assert cell.total == 48 and cell.by_subject == {"student": 45, "teacher": 3}, raw
    three = C.parse_count_cell("99 Siswa, 9 Guru dan 9 Kepala Sekolah")
    assert three.total == 117 and three.by_subject["principal"] == 9


def test_subject_without_a_figure_is_flagged():
    cell = C.parse_count_cell("99 Siswa dan Guru")
    assert cell.total == 99 and "unnumbered_subject" in cell.flags


@pytest.mark.parametrize("raw,bounds", [
    ("Puluhan Siswa", (20, 99)), ("Ratusan siswa", (100, 999)), ("Belasan Siswa", (11, 19)),
])
def test_vague_quantifier_has_bounds_but_no_point_estimate(raw, bounds):
    cell = C.parse_count_cell(raw)
    assert cell.kind == "vague" and cell.total is None
    assert (cell.minimum, cell.maximum) == bounds


@pytest.mark.parametrize("raw", ["", "TBA (Tidak Diumumkan)", "TBA (Spesifik tidak disebutkan)"])
def test_unreported_is_not_zero(raw):
    cell = C.parse_count_cell(raw)
    assert cell.kind == "unreported" and cell.total is None


def test_unrecognised_cell_is_marked_unparsed_not_guessed():
    assert C.parse_count_cell("beberapa").kind == "unparsed"


# --- dates -------------------------------------------------------------------

def test_day_month_year():
    cell = D.parse_date_cell("29 September 2025")
    assert (cell.start, cell.end, cell.precision) == ("2025-09-29", "2025-09-29", "day")


def test_sampai_dengan_range():
    cell = D.parse_date_cell("7 S/d 9 Mei 2025")
    assert (cell.start, cell.end, cell.precision) == ("2025-05-07", "2025-05-09", "day")
    across = D.parse_date_cell("30 April s/d 2 Mei 2025")
    assert (across.start, across.end) == ("2025-04-30", "2025-05-02")


@pytest.mark.parametrize("raw,iso", [("Agustus 2025", "2025-08"), ("Awal Februari 2026", "2026-02")])
def test_month_only_is_never_padded_to_a_day(raw, iso):
    cell = D.parse_date_cell(raw)
    assert cell.start == iso and cell.precision == "month"


def test_no_date():
    assert D.parse_date_cell("Tidak Disebutkan") is None


def test_day_beats_month_on_overlap():
    assert [m.precision for m in D.find_dates("Pada tanggal 16 Juli 2013 terjadi")] == ["day"]


def test_numeric_dates_are_day_first():
    assert D.primary_date("pada Jumat (11/9/2026).").iso == "2026-09-11"


# --- places ------------------------------------------------------------------

def test_wikipedia_titles_map_to_canonical_provinces():
    assert P.canonical_province("Daerah Istimewa Yogyakarta") == "DI Yogyakarta"
    assert P.canonical_province("Daerah Khusus Ibukota Jakarta") == "DKI Jakarta"
    assert P.canonical_province("", "Nanggroe Aceh Darussalam") == "Aceh"
    assert P.canonical_province("Atlantis") is None


def test_kota_and_kabupaten_are_not_conflated():
    assert P.kabkota_parts("Kota Bandung") == ("kota", "Bandung")
    assert P.kabkota_parts("Kabupaten Bandung") == ("kabupaten", "Bandung")
    assert P.kabkota_parts("Kota Administrasi Jakarta Utara") == ("kota", "Jakarta Utara")
    assert P.kabkota_parts("Bandung") == (None, "Bandung")


@pytest.mark.parametrize("venue,level", [
    ("SDN 6 Matangkuli", "primary"), ("SD Negeri Cipari", "primary"), ("MIN 2 Seram", "primary"),
    ("SMPN 1 Cisarua", "junior_secondary"), ("MTs Darul Fiqri", "junior_secondary"),
    ("SMAN 1 Kuala", "senior_secondary"), ("SMK Pembangunan", "senior_secondary"), ("MAN 1", "senior_secondary"),
    ("TK Nur", "early_childhood"), ("PAUD Melati", "early_childhood"),
    ("Pondok Pesantren X", "pesantren"), ("Ponpes Y", "pesantren"),
    ("Posyandu Mawar", "community_health_post"),
    ("SD (Spesifik tidak disebutkan)", "primary"),
    ("Madrasah Aliyah Z", None), ("warga Simpang Mamplam", None), ("", None),
])
def test_venue_level(venue, level):
    assert normalize.venue_level(venue) == level
