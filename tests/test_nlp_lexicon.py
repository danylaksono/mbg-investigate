"""Regression cases for the cause lexicon in analysis/nlp_explore.py, taken from sentences the
first full run misjudged or judged correctly."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import nlp_explore as N  # noqa: E402


def reports_a_lab_finding(sentence: str) -> bool:
    return N.reports_lab_finding(sentence)


@pytest.mark.parametrize("sentence", [
    "Dari hasil sampel makanan yang diteliti, ditemukan bakteri Escherichia Coli dan Salmonella Sp.",
    "Hasil pemeriksaan laboratorium menunjukkan bakteri patogen ditemukan pada sampel ayam serundeng.",
    "Hasil uji ditemukan nitrit pada telur asin.",
    "Bahkan dari 14 sampel rectal swab, semuanya juga positif E-coli.",
    "BGN menyebut insiden disebabkan nitrit yang tinggi.",
    "Hasil pemeriksaan laboratorium menunjukkan kandungan bakteri e coli melebihi batas aman di udang saus padang.",
])
def test_reported_findings_are_recognised(sentence):
    assert reports_a_lab_finding(sentence)


@pytest.mark.parametrize("sentence", [
    # "ditemukan" alone once matched this: 112 students were found symptomatic, not a lab result
    "Dari hasil pemeriksaan secara keseluruhan ditemukan ada total sebanyak 112 siswa yang mengalami gejala keracunan.",
    # an inspection finding, not a laboratory one
    "Dari hasil pemeriksaan ditemukan sejumlah faktor risiko yang berpotensi memicu kontaminasi pangan.",
    # a hope and a question, not results
    "Hasil uji laboratorium diharapkan dapat mengungkap penyebab pasti gangguan kesehatan.",
    "Kami masih menunggu hasil lab untuk mengetahui apakah mengandung nitrit atau bakteri.",
])
def test_speculation_and_non_laboratory_findings_are_not_counted(sentence):
    assert not reports_a_lab_finding(sentence)


def test_a_clean_result_is_not_a_contamination_finding():
    assert not reports_a_lab_finding("Hasil pemeriksaan menunjukkan air minum tidak mengandung bakteri E Coli.")
    assert N.LAB_NEGATIVE.search("Hasil uji sampel air dari dapur dinyatakan memenuhi syarat kesehatan.")


@pytest.mark.parametrize("sentence", [
    # the five misses found by reading a random sample of unflagged sentences (recall_audit.py)
    "Kasat Reskrim Polres Lebong mengatakan dari hasil penelitian yang dilakukan BPOM, keracunan massal itu terjadi karena adanya bakteri di makanan.",
    "Darmawel menjelaskan, dari hasil lab ada kandungan bakteri, dan tidak menemukan unsur pidana.",
    "Kontaminasi bakteri juga ditemukan pada sampel air bersih yang digunakan di SPPG berdasarkan hasil uji Labkesda.",
    "Kandungan e coli melebihi batas aman justru ditemukan di udang saus padang.",
])
def test_findings_the_first_audit_missed_are_now_recognised(sentence):
    assert reports_a_lab_finding(sentence)


@pytest.mark.parametrize("sentence", [
    "Keracunan biasanya disebabkan oleh bakteri pada makanan yang basi.",
    "Diare bisa disebabkan bakteri atau faktor lain menurut hasil pemeriksaan umum.",
    "Kondisi ini lazim ditemukan pada kasus yang disebabkan kontaminasi bakteri atau toksin tertentu.",
])
def test_generic_explainers_are_not_findings(sentence):
    assert not reports_a_lab_finding(sentence)


def test_an_organism_name_is_not_split_across_sentences():
    parts = N.sentences("Hasilnya, ada satu jenis makanan yang kadar bakteri E. coli melebihi batas. Kalimat berikutnya.")
    assert len(parts) == 2 and "E. coli" in parts[0]


def test_a_modal_after_a_bare_causal_phrase_makes_it_an_explainer():
    """Found by reading the all-outlet run: this resolved a Cianjur event it did not concern."""
    assert not reports_a_lab_finding(
        "Risiko perubahan zat kimia seperti nitrat yang dipicu bakteri pengurai dapat menyebabkan keracunan.")


def test_a_modal_does_not_discount_a_genuine_finding_that_mentions_what_the_agent_can_do():
    assert reports_a_lab_finding(
        "Hasil uji laboratorium menunjukkan adanya bakteri E. coli yang dapat menyebabkan diare.")
    assert reports_a_lab_finding("BGN menyebut insiden disebabkan nitrit yang tinggi.")


def test_a_clean_lab_result_needs_a_lab_word_and_is_not_a_psychological_exam():
    """Found by reading attributions: "hasil pemeriksaan psikologis ... komentar negatif" matched."""
    assert not N.reports_lab_clean(
        "Berdasarkan hasil pemeriksaan psikologis sebagian korban mengalami gangguan mental akibat komentar negatif.")
    assert N.reports_lab_clean("Hasil uji sampel air dari dapur tersebut dinyatakan memenuhi syarat kesehatan.")
    assert N.reports_lab_clean("Hasil uji laboratorium BPOM menunjukkan tidak ditemukan bakteri E. coli.")
    assert not N.reports_lab_clean("Kami menunggu hasil uji laboratorium apakah sampel aman.")
