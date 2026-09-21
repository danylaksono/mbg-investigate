"""The venue -> school register matcher (analysis/schools_pilot.py). Cases come from the two-regency
pilot; they pin what makes a match safe and what must stay ambiguous."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import schools_pilot as S  # noqa: E402


def register(*names, kel="X"):
    return pd.DataFrame([dict(npsn=str(i), school=n, kelurahan=kel, address="", kecamatan="K") for i, n in enumerate(names)])


def test_state_school_written_three_ways_is_one_school():
    reg = register("SD NEGERI 1 SUMBERAGUNG")
    for venue in ("SDN 1 Sumberagung", "SD Negeri 1 Sumberagung", "SD N 1 Sumberagung"):
        assert S.match_venue(venue, reg)[:2] == ("matched", ["0"]), venue


def test_word_order_and_zero_padded_numbers_do_not_matter():
    assert S.match_venue("SDN 1 Cibodas", register("SD NEGERI CIBODAS 01"))[0] == "matched"


def test_private_schools_carry_a_trailing_s_in_the_register():
    reg = register("MTSS MANAARUL HUDA", "SMKS WIDYA KARYA PADALARANG", "MAS AL MUKHTARIYAH MANDE")
    assert S.match_venue("SMK Widya Karya", reg)[0] == "matched"
    assert S.match_venue("MA Al Mukhtariyah", reg)[0] == "matched"


def test_the_name_is_tried_before_the_address():
    """"SDN 1 Sumberagung" must not match "SD NEGERI 1 BARONGAN", whose village happens to be Sumberagung."""
    reg = pd.DataFrame([dict(npsn="a", school="SD NEGERI 1 BARONGAN", kelurahan="Sumberagung", address="", kecamatan="K"),
                        dict(npsn="b", school="SD NEGERI 1 SUMBERAGUNG", kelurahan="Sumberagung", address="", kecamatan="K")])
    assert S.match_venue("SDN 1 Sumberagung", reg) == ("matched", ["b"], "name")


def test_address_is_only_a_fallback_and_is_labelled():
    reg = pd.DataFrame([dict(npsn="a", school="SD NEGERI 1 BARONGAN", kelurahan="Sumberagung", address="", kecamatan="K")])
    assert S.match_venue("SDN 1 Sumberagung", reg) == ("matched", ["a"], "address")


def test_near_identical_spelling_is_accepted_last_and_labelled():
    assert S.match_venue("MTs Manarul Huda", register("MTSS MANAARUL HUDA")) == ("matched", ["0"], "spelling")
    assert S.match_venue("SDN 1 Sutenjaya", register("SD NEGERI 1 SUNTENJAYA")) == ("matched", ["0"], "spelling")


def test_identical_names_in_two_districts_stay_ambiguous_not_guessed():
    res, hits, _ = S.match_venue("SDN 2 Cibodas", register("SD NEGERI 2 CIBODAS", "SD NEGERI 2 CIBODAS"))
    assert res == "ambiguous" and len(hits) == 2
    reg2 = pd.DataFrame([dict(npsn="a", school="SD NEGERI 2 CIBODAS", kelurahan="A", address="", kecamatan="K1"),
                         dict(npsn="b", school="SD NEGERI 2 CIBODAS", kelurahan="B", address="", kecamatan="K2")])
    assert S.match_venue("SDN 2 Cibodas", reg2)[0] == "ambiguous"


def test_a_different_number_or_type_is_never_a_match():
    reg = register("SD NEGERI 1 JETIS", "SMP NEGERI 3 JETIS")
    assert S.match_venue("SMPN 1 Jetis", reg)[0] == "none"
    assert S.match_venue("SDN 3 Jetis", reg)[0] == "none"


def test_state_versus_private_is_respected_when_the_venue_says_which():
    reg = register("SMP NEGERI 1 JETIS", "SMPS 1 JETIS")
    assert S.match_venue("SMPN 1 Jetis", reg)[:2] == ("matched", ["0"])


def test_free_text_that_is_not_a_school_name_matches_nothing():
    reg = register("SD NEGERI SIMPANG")
    assert S.match_venue("warga Simpang Mamplam dan Pandrah", reg)[0] == "none"
    assert S.match_venue("pesantren Bani Sulaiman", reg)[0] == "none"
