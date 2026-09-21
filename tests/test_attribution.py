"""Who is a finding attributed to? The evidence builder stores this so a reader can weigh a
laboratory statement from a health office differently from one by the programme's own operator."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import build_evidence as B  # noqa: E402


def test_a_health_office_is_recognised():
    assert B.attribution("Kepala Dinas Kesehatan Jombang mengatakan hasil uji menunjukkan bakteri E. coli.") == ["health_body"]
    assert "health_body" in B.attribution("Dari tiga sampel yang diperiksa BPOM, hanya nasi positif E. coli.")
    assert "health_body" in B.attribution("Hasil Labkesda Jawa Barat menunjukkan adanya bakteri.")


def test_the_speaker_is_often_named_in_the_previous_sentence():
    previous = "Kepala Dinas Kesehatan Kota Bandung, Anhar Hadian, menjelaskan hasilnya."
    assert B.attribution("Ia menyebut hasil uji laboratorium menemukan bakteri.", previous) == ["health_body"]
    assert B.attribution("Ia menyebut hasil uji laboratorium menemukan bakteri.") == []


def test_the_programme_operator_is_kept_apart_from_health_bodies():
    """BGN runs the programme, so its statements are an interested party's and must be filterable."""
    assert B.attribution("BGN menyebut insiden disebabkan nitrit yang tinggi.") == ["programme_operator"]
    assert B.attribution("Kepala SPPG mengatakan makanan aman dan layak konsumsi.") == ["programme_operator"]


def test_several_bodies_are_all_listed():
    got = B.attribution("Bupati mengatakan BGN dan Dinkes sudah memeriksa sampel.")
    assert set(got) == {"local_government", "programme_operator", "health_body"}


def test_police_and_schools_are_distinct_classes():
    assert B.attribution("Polres mengamankan sampel makanan.") == ["police"]
    assert B.attribution("Kepala sekolah menyebut siswa muntah setelah makan.") == ["school"]


def test_an_unattributed_sentence_is_empty_not_guessed():
    assert B.attribution("Hasil uji laboratorium menemukan bakteri E. coli pada nasi.") == []


def test_findings_carry_their_attribution():
    text = ("Kepala Dinas Kesehatan Kabupaten Jombang menjelaskan kepada wartawan. "
            "Hasil pemeriksaan laboratorium menunjukkan kandungan bakteri e coli melebihi batas aman.")
    found = {f["kind"]: f for f in B.findings_for(text)}
    assert found["lab_contamination"]["attributed_to"] == ["health_body"]
    assert found["lab_contamination"]["agents"] == ["E. coli", "bacteria (unspecified)"]
