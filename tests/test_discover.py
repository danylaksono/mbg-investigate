"""Stage 6 (feed walking) and 7 (offline linking). Offline: the fetcher is injected."""

import json
from datetime import date
from pathlib import Path

from mbgpipe import articles, discover, link

LISTING = (Path(__file__).parent / "fixtures" / "detik_listing.html").read_text(encoding="utf-8")


# --- listing parser ----------------------------------------------------------

def test_listing_yields_title_url_and_date():
    items = discover.parse_listing(LISTING, "detik", "tag:detik:keracunan-mbg:page=2")
    assert len(items) == 3
    first = items[0]
    assert first.url.startswith("https://www.detik.com/") and "/d-" in first.url
    assert "MBG" in first.title and first.listed_date and first.listed_date.startswith("2026-09")


def test_page_without_articles_parses_to_nothing():
    assert discover.parse_listing("<html><body>tidak ada</body></html>", "detik", "x") == []


def test_abbreviated_indonesian_months_are_read():
    block = ('<article><a href="https://x.example/a"><img alt="Judul"></a>'
             '<span>Kamis, 04 Feb 2026 10:00 WIB</span></article><article><a href="https://x.example/b">'
             '<img alt="Lain"></a><span>28 Agu 2025</span></article>')
    dates = [i.listed_date for i in discover.parse_listing(block, "x", "o")]
    assert dates == ["2026-02-04", "2025-08-28"]


# --- feed walk ---------------------------------------------------------------

class Feed:
    """Serves the fixture listing for pages 1-2 (distinct URLs), then an empty page."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, ua=""):
        self.calls.append(url)
        if "robots.txt" in url:
            return articles.FetchResult(b"", url, 404)
        page = int(url.split("page=")[1]) if "page=" in url else 1
        if page > 2:
            return articles.FetchResult(b"<html></html>", url, 200)
        body = LISTING.replace("/d-", f"/p{page}/d-").encode()
        return articles.FetchResult(body, url, 200)


def test_walk_stops_at_end_of_feed_and_writes_rows(tmp_path):
    feed = Feed()
    out = tmp_path / "detik.tag.jsonl"
    stats = discover.walk_feed("detik", out, delay=0, fetcher=feed, sleep=lambda _: None)
    assert stats["stopped"] == "end_of_feed" and stats["pages"] == 3 and stats["headlines"] == 6
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 6 and rows[0]["origin"].endswith("page=1")


def test_walk_resumes_after_the_last_page_written(tmp_path):
    out = tmp_path / "detik.tag.jsonl"
    discover.walk_feed("detik", out, max_pages=1, delay=0, fetcher=Feed(), sleep=lambda _: None)
    second = Feed()
    discover.walk_feed("detik", out, delay=0, fetcher=second, sleep=lambda _: None)
    assert not any(u.endswith("sortby=time") for u in second.calls), "page 1 must not be fetched again"


def test_walk_stops_when_robots_disallows(tmp_path):
    class Closed(Feed):
        def __call__(self, url, ua=""):
            if "robots.txt" in url:
                return articles.FetchResult(b"User-agent: *\nDisallow: /tag/\n", url, 200)
            return super().__call__(url, ua)

    stats = discover.walk_feed("detik", tmp_path / "x.jsonl", delay=0, fetcher=Closed(), sleep=lambda _: None)
    assert stats["stopped"] == "robots_disallow" and stats["pages"] == 0


# --- linking -----------------------------------------------------------------

def ev(event_id, start, name, end=None, venues=()):
    return {"event_id": event_id, "date_start": start, "date_end": end or start,
            "kabkota_name": name, "venues": list(venues)}


def test_place_and_date_window_link_one_event():
    keys = link.event_keys([ev("e1", "2026-01-24", "Aceh Timur"), ev("e2", "2026-01-24", "Bogor")])
    assert link.link("Keracunan MBG di Aceh Timur", date(2026, 1, 27), keys) == ("linked", ["e1"])


def test_article_before_the_incident_or_long_after_is_not_linked():
    keys = link.event_keys([ev("e1", "2026-01-24", "Aceh Timur")])
    assert link.link("MBG Aceh Timur", date(2026, 1, 22), keys)[0] == "none"
    assert link.link("MBG Aceh Timur", date(2026, 1, 23), keys)[0] == "linked", "one day early is allowed"
    assert link.link("MBG Aceh Timur", date(2026, 3, 1), keys)[0] == "none", "beyond the follow-up window"


def test_two_matching_events_are_ambiguous_never_nearest():
    keys = link.event_keys([ev("kab", "2026-05-01", "Kediri"), ev("kota", "2026-05-03", "Kediri")])
    decision, found = link.link("Siswa di Kediri keracunan MBG", date(2026, 5, 4), keys)
    assert decision == "ambiguous" and sorted(found) == ["kab", "kota"]


def test_one_article_naming_two_places_is_ambiguous():
    keys = link.event_keys([ev("a", "2026-05-01", "Kediri"), ev("b", "2026-05-01", "Blitar")])
    assert link.link("Kediri dan Blitar sama-sama keracunan MBG", date(2026, 5, 2), keys)[0] == "ambiguous"


def test_run_together_spelling_matches():
    keys = link.event_keys([ev("e1", "2026-05-01", "Kulon Progo")])
    assert link.link("104 orang di Kulonprogo keracunan", date(2026, 5, 2), keys)[0] == "linked"


def test_place_name_matches_whole_words_only():
    keys = link.event_keys([ev("e1", "2026-05-01", "Ende")])
    assert link.link("Mendengar kabar keracunan", date(2026, 5, 2), keys)[0] == "none"


def test_specific_venue_links_without_the_place_name():
    keys = link.event_keys([ev("e1", "2026-02-03", "Kudus", venues=["SMAN 2 Kudus"])])
    assert link.link("Insiden di SMAN 2 Kudus gegara nitrit", date(2026, 2, 4), keys)[0] == "linked"


def test_undated_article_or_event_cannot_be_linked():
    keys = link.event_keys([ev("e1", "2026-01-24", "Aceh Timur"), ev("nodate", "", "Bogor")])
    assert [k.event_id for k in keys] == ["e1"]
    assert link.link("MBG Aceh Timur", None, keys)[0] == "none"


def test_month_precision_event_spans_the_month():
    keys = link.event_keys([ev("m", "2025-08", "Bogor", end="2025-08")])
    assert link.link("Bogor keracunan", date(2025, 8, 31), keys)[0] == "linked"


def test_full_kabkota_name_is_reduced_to_the_headline_form():
    keys = link.event_keys([{"event_id": "e1", "date_start": "2026-01-24", "date_end": "2026-01-24",
                             "kabkota": "Kabupaten Aceh Timur", "venues": []}])
    assert link.link("Keracunan MBG di Aceh Timur", date(2026, 1, 25), keys)[0] == "linked"


def test_dataframe_nans_are_treated_as_missing():
    nan = float("nan")
    keys = link.event_keys([{"event_id": "x", "date_start": nan, "date_end": nan, "kabkota_name": nan,
                             "kabkota": nan, "venues": []},
                            {"event_id": "y", "date_start": "2026-01-24", "date_end": nan,
                             "kabkota_name": nan, "kabkota": "Kabupaten Aceh Timur", "venues": []}])
    assert [k.event_id for k in keys] == ["y"]


def test_a_named_venue_outranks_a_bare_place_when_two_events_share_the_regency():
    keys = link.event_keys([ev("man2", "2026-08-01", "Rembang", venues=["MAN 2 Rembang"]),
                            ev("lasem", "2026-08-02", "Rembang", venues=["SMAN 1 Lasem"])])
    assert link.link("Siswa MAN 2 Rembang korban keracunan bertambah", date(2026, 8, 4), keys) == ("linked", ["man2"])
    assert link.link("Keracunan MBG di Rembang bertambah", date(2026, 8, 4), keys)[0] == "ambiguous"


def test_two_venues_named_together_stay_ambiguous():
    keys = link.event_keys([ev("a", "2026-08-01", "Rembang", venues=["MAN 2 Rembang"]),
                            ev("b", "2026-08-01", "Rembang", venues=["SMAN 1 Lasem"])])
    assert link.link("MAN 2 Rembang dan SMAN 1 Lasem", date(2026, 8, 2), keys)[0] == "ambiguous"


# --- full-text linking --------------------------------------------------------

def test_lead_paragraph_place_links_but_a_deep_mention_is_only_weak():
    keys = link.event_keys([ev("e1", "2026-01-24", "Aceh Timur")])
    lead = "IDI, ACEH TIMUR - Belasan siswa keracunan. " + "x " * 400
    assert link.link_article("Keracunan MBG", lead, date(2026, 1, 25), keys).decision == "linked"
    body_only = "Kasus keracunan MBG terjadi pekan ini. " + "kata " * 200 + "Sebelumnya di Aceh Timur juga terjadi."
    res = link.link_article("Keracunan MBG", body_only, date(2026, 1, 25), keys)
    assert res.decision == "weak" and res.tier == "body_only" and res.event_ids == ["e1"]


def test_roundup_naming_many_places_in_the_lead_is_ambiguous():
    keys = link.event_keys([ev("a", "2026-05-01", "Kediri"), ev("b", "2026-05-01", "Blitar")])
    res = link.link_article("Rentetan keracunan MBG", "Di Kediri dan Blitar siswa keracunan.", date(2026, 5, 2), keys)
    assert res.decision == "ambiguous" and sorted(res.event_ids) == ["a", "b"]


def test_decisions_carry_the_matched_term_and_kind_as_evidence():
    keys = link.event_keys([ev("e1", "2026-02-03", "Kudus", venues=["SMAN 2 Kudus"])])
    res = link.link_article("Nitrit tinggi", "Insiden di SMAN 2 Kudus.", date(2026, 2, 4), keys)
    assert res.matches[0].kind == "venue" and res.matches[0].term == "sman 2 kudus"
    assert link.RULES_VERSION


def test_nothing_named_is_none():
    keys = link.event_keys([ev("e1", "2026-01-24", "Aceh Timur")])
    assert link.link_article("Harga cabai naik", "Pasar di Jakarta.", date(2026, 1, 25), keys).decision == "none"


def test_names_event_ignores_dates_so_place_and_date_errors_can_be_told_apart():
    key = link.event_keys([ev("e1", "2026-01-24", "Aceh Timur")])[0]
    assert link.names_event("Kecelakaan di Aceh Timur", key).kind == "place"
    assert link.names_event("Korban keracunan sekolah Cianjur", key) is None
    venue_key = link.event_keys([ev("e2", "2026-02-03", "Kudus", venues=["SMAN 2 Kudus"])])[0]
    assert link.names_event("Siswa SMAN 2 Kudus", venue_key).kind == "venue"


# --- the other outlets' parsers ------------------------------------------------

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIX / f"{name}_listing.html").read_text(encoding="utf-8")


def test_antara_keeps_articles_and_skips_video_items():
    items = discover.parse_listing(fixture("antara"), "antara", "o")
    assert items and all("/berita/" in i.url for i in items)
    assert all(i.title and i.listed_date and i.listed_date.startswith("2026-") for i in items)


def test_kompas_reads_title_and_full_month_date():
    items = discover.parse_listing(fixture("kompas"), "kompas", "o")
    assert len(items) == 3 and items[0].url.startswith("https://") and "/read/" in items[0].url
    assert items[0].listed_date == "2026-09-18" and "MBG" in items[0].title


def test_cnn_date_comes_from_the_url_not_the_render_time_comment():
    items = discover.parse_listing(fixture("cnn"), "cnn", "o")
    assert len(items) == 3
    assert items[0].listed_date == "2026-09-10", "the visible '1 minggu yang lalu' and the comment are render-relative"
    assert items[0].url.split("/")[4].startswith("20260910")


def test_liputan6_reads_text_items_with_title_and_datetime():
    items = discover.parse_listing(fixture("liputan6"), "liputan6", "o")
    assert len(items) == 3 and all("/read/" in i.url for i in items)
    assert items[0].title.startswith("Update Keracunan MBG") and items[0].listed_date == "2026-09-02"


def test_a_page_without_matching_blocks_parses_to_nothing_for_every_outlet():
    for outlet in discover.PARSERS:
        assert discover.parse_listing("<html><body><p>kosong</p></body></html>", outlet, "o") == []


def test_every_outlet_has_a_feed_url_and_a_parser():
    assert set(discover.OUTLETS) == set(discover.PARSERS)
    assert discover.OUTLETS["antara"](2).endswith("/tag/keracunan-mbg/2")
    assert discover.OUTLETS["kompas"](1).endswith("/tag/keracunan-mbg")


def test_raw_listing_pages_are_kept_with_a_hash_manifest(tmp_path):
    out = tmp_path / "detik.tag.jsonl"
    raw = tmp_path / "feeds" / "detik"
    discover.walk_feed("detik", out, delay=0, fetcher=Feed(), sleep=lambda _: None, raw_dir=raw)
    run_dirs = list(raw.iterdir())
    assert len(run_dirs) == 1, "one directory per run, so a later run never overwrites this one"
    manifest = [json.loads(l) for l in (run_dirs[0] / "manifest.jsonl").read_text().splitlines()]
    assert [m["page"] for m in manifest] == [1, 2, 3] and all(len(m["sha256"]) == 64 for m in manifest)
    import gzip, hashlib
    body = gzip.decompress((run_dirs[0] / "page-0001.html.gz").read_bytes())
    assert hashlib.sha256(body).hexdigest() == manifest[0]["sha256"]


def test_a_refusing_host_ends_the_walk_resumably_instead_of_hammering(tmp_path):
    class Refusing(Feed):
        def __call__(self, url, ua=""):
            if "robots.txt" in url:
                return articles.FetchResult(b"", url, 404)
            self.calls.append(url)
            return articles.FetchResult(b"", url, 429)

    feed = Refusing()
    slept = []
    stats = discover.walk_feed("detik", tmp_path / "x.jsonl", delay=0, fetcher=feed, sleep=slept.append)
    assert stats["stopped"] == "refused_429" and len(feed.calls) == 3, "three refusals, then stop"
    assert any(s >= 800 for s in slept), "the breaker's cooldown is waited out between attempts"


def test_a_feed_that_pads_with_unrelated_news_ends_the_walk_and_keeps_that_page_aside(tmp_path):
    """Kompas served generic latest news from page 154 instead of an empty page."""
    padding = "".join(f'<article><a href="https://x.example/{i}"><img alt="Harga cabai naik {i}"></a>'
                       "<span>19 September 2026</span></article>" for i in range(8))

    class Padded(Feed):
        def __call__(self, url, ua=""):
            if "robots.txt" in url:
                return articles.FetchResult(b"", url, 404)
            self.calls.append(url)
            page = int(url.split("page=")[1]) if "page=" in url else 1
            body = LISTING.replace("/d-", f"/p{page}/d-") if page <= 2 else f"<html><body>{padding}</body></html>"
            return articles.FetchResult(body.encode(), url, 200)

    out = tmp_path / "detik.tag.jsonl"
    stats = discover.walk_feed("detik", out, delay=0, fetcher=Padded(), sleep=lambda _: None)
    assert stats["stopped"] == "off_topic"
    kept = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert len(kept) == 6 and not any("cabai" in r["title"] for r in kept)
    aside = [json.loads(l) for l in (tmp_path / "detik.tag.off_topic.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(aside) == 8, "the padding page is kept as evidence, out of the headline list"
