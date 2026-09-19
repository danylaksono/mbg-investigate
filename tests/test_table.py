"""Stages 2 and 3 against an excerpt of the real page (tests/fixtures/sample_wikitext.txt).

The fixture is real wikitext: seven complete province blocks, the page's own
placeholder and TOTAL rows, and one bullet each from the India and Korea sections.
It is the shape that can actually go wrong, rowspans included.
"""

from pathlib import Path

import pytest

from mbgpipe import articles, normalize, parse, wikitable

FIXTURE = Path(__file__).parent / "fixtures" / "sample_wikitext.txt"
WIKITEXT = FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed():
    rows, report = parse.extract_incident_table(WIKITEXT, "fixture")
    citations = parse.extract_citations(WIKITEXT, "fixture")
    return rows, report, citations


@pytest.fixture(scope="module")
def incidents(parsed):
    rows, _, citations = parsed
    urls: dict[str, list[str]] = {}
    for c in citations:
        urls.setdefault(normalize.RE_REF_BASE.sub("", c.ref_id), []).append(c.best_url)
    seen: set = set()
    return [normalize.normalize_row(r.as_dict(), urls, seen) for r in rows]


# --- the table ---------------------------------------------------------------

def test_only_the_indonesia_table_is_read(parsed):
    rows, report, _ = parsed
    assert len(rows) == 66
    assert not any("Gandaman" in r.venue + r.kabkota for r in rows), "India bullet is not a row"
    assert report["rendered_rows"] == 68          # 66 incidents + placeholder + TOTAL
    assert report["placeholder_rows"] == 1


def test_the_pages_total_row_is_set_aside_not_treated_as_an_incident(parsed):
    rows, report, _ = parsed
    assert report["wikipedia_total"]["symptomatic"].endswith("11.390")
    assert not any(r.date.upper() == "TOTAL" for r in rows)


def test_link_targets_give_canonical_place_names(parsed):
    first = parsed[0][0]
    assert first.kabkota == "Aceh Utara" and first.kabkota_link == "Kabupaten Aceh Utara"
    assert first.province == "Nanggroe Aceh Darussalam" and first.province_link == "Aceh"


def test_rowspan_carries_values_and_remembers_origin(parsed):
    rows = parsed[0]
    a, b = [r for r in rows if r.kabkota == "Aceh Singkil"]
    assert (a.date, b.date) == ("11 Februari 2026", "11 Februari 2026")
    assert a.symptomatic == b.symptomatic == "33 Santri"
    assert a.origins["symptomatic"] == b.origins["symptomatic"] == a.table_row
    assert a.venue != b.venue and a.origins["venue"] != b.origins["venue"]
    assert a.ref_ids == b.ref_ids, "one ref cell spans both rows"


def test_header_change_fails_loudly():
    tampered = WIKITEXT.replace("!Bergejala", "!Dirawat")
    with pytest.raises(wikitable.WikitableError, match="header changed"):
        parse.extract_incident_table(tampered, "fixture")


def test_inline_cell_separator_fails_loudly():
    tampered = WIKITEXT.replace("|SDN 6 Matangkuli", "|SDN 6 Matangkuli || extra", 1)
    with pytest.raises(wikitable.WikitableError, match="inline cell separator"):
        parse.extract_incident_table(tampered, "fixture")


def test_misaligned_row_fails_loudly():
    with pytest.raises(wikitable.WikitableError, match="expected 7 columns"):
        wikitable.resolve(wikitable.split_rows(["|a", "|b"]), 7)


def test_colspan_and_rowspan_resolution():
    lines = ['|-', '| rowspan="2" |A', '|B', '|-', '|C']
    grid = wikitable.resolve(wikitable.split_rows(lines), 2)
    assert [[c.text for c in row] for row in grid] == [["A", "B"], ["A", "C"]]
    assert grid[1][0].origin == (0, 0) and grid[1][0].is_origin is False


# --- citations ---------------------------------------------------------------

def test_citations_handle_both_parameter_languages(parsed):
    cites = {c.ref_id: c for c in parsed[2]}
    assert cites[":3"].work == "KOMPAS.com"
    assert cites[":3"].url.startswith("https://regional.kompas.com/")
    assert cites[":3"].date == "2025-10-03"


def test_citations_are_page_wide_but_stage_4_only_fetches_cited_rows(parsed, tmp_path):
    rows, _, citations = parsed
    assert any("bbc.co.uk" in c.url for c in citations), "India source is in the citation table"
    rows_file = tmp_path / "x.rows.jsonl"
    parse.write_jsonl(rows, rows_file)
    cites_file = tmp_path / "x.citations.jsonl"
    parse.write_jsonl(citations, cites_file)
    keep = articles.refs_cited_by_rows([rows_file])
    urls = [u for u, _, _ in articles.load_citation_urls([cites_file], keep_refs=keep)]
    assert urls and not any("bbc.co.uk" in u for u in urls)


# --- normalisation -----------------------------------------------------------

def test_shared_count_is_counted_once(incidents):
    a, b = [i for i in incidents if i.kabkota_name == "Aceh Singkil"]
    assert a.symptomatic == b.symptomatic == 33
    assert a.symptomatic_counted == 33 and b.symptomatic_counted is None
    assert "count_shared_across_rows" in b.flags and "count_shared_across_rows" not in a.flags
    assert a.event_id == b.event_id


def test_summing_the_counted_column_is_not_the_naive_sum(incidents):
    counted = sum(i.symptomatic_counted or 0 for i in incidents)
    naive = sum(i.symptomatic or 0 for i in incidents)
    assert counted == 2346 and naive == 10466, "rowspan inflation is real and large"


def test_events_are_one_per_source_cell_and_totals_agree(incidents):
    events = normalize.build_events(incidents)
    assert len(events) == len({i.event_id for i in incidents}) == 39
    assert sum(e["symptomatic"] or 0 for e in events) == sum(i.symptomatic_counted or 0 for i in incidents)
    singkil = next(e for e in events if e["kabkota"] == "Kabupaten Aceh Singkil")
    assert singkil["n_rows"] == 2 and len(singkil["venues"]) == 2


def test_vague_count_has_bounds_and_no_point_estimate(incidents):
    batam = next(i for i in incidents if i.kabkota == "Kota Batam" and i.symptomatic_kind == "vague")
    assert batam.symptomatic is None and batam.symptomatic_counted is None
    assert (batam.symptomatic_min, batam.symptomatic_max) == (100, 999)
    assert "count_vague" in batam.flags


def test_undated_row_is_flagged_not_dropped(incidents):
    jakut = next(i for i in incidents if i.kabkota_name == "Jakarta Utara" and i.date_raw == "Tidak Disebutkan")
    assert jakut.date_iso is None and "no_date" in jakut.flags
    assert jakut.kabkota_type == "kota" and jakut.province == "DKI Jakarta"


def test_deaths_are_null_unless_reported(incidents):
    reported = [i for i in incidents if i.deaths_raw]
    assert len(reported) == 1 and reported[0].deaths == 1
    assert all(i.deaths is None for i in incidents if not i.deaths_raw), "blank means not reported, not zero"


def test_unspecified_venue_is_null_but_level_is_kept(incidents):
    aceh_timur = next(i for i in incidents if i.kabkota_name == "Aceh Timur")
    assert aceh_timur.venue is None and "no_venue" in aceh_timur.flags
    sd = next(i for i in incidents if i.venue_raw.startswith("SD (Spesifik"))
    assert sd.venue is None and sd.venue_level == "primary"


def test_source_urls_resolve_through_back_references(incidents):
    assert all(i.source_urls for i in incidents), "every fixture row cites something fetchable"


def test_qc_report_shows_the_rowspan_inflation_and_the_stale_total(parsed, incidents):
    report = normalize.qc_report(incidents, normalize.build_events(incidents),
                                 parsed[1]["wikipedia_total"])
    # 4.46x on this fixture (rowspan-dense blocks); the full table is ~3x.
    assert report["rowspan_inflation_factor"] > 4
    assert report["wikipedia_total_row"] == 11390
    assert report["cumulative_reaches_wikipedia_total_on"] is None, "fixture is a fraction of the table"


def test_csv_round_trips_lists_as_json(incidents, tmp_path):
    import csv, json
    normalize.write_csv(incidents, tmp_path / "i.csv")
    with open(tmp_path / "i.csv", encoding="utf-8") as fh:
        row = next(csv.DictReader(fh))
    assert json.loads(row["source_urls"])[0].startswith("https://")
    assert json.loads(row["symptomatic_by_subject"]) == {"student": 3}
