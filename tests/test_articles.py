"""Stage 4 and 5 tests. Every network call is injected, so this suite is offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mbgpipe import articles, corpus

ROBOTS_OPEN = b"User-agent: *\nAllow: /\n"
ROBOTS_CLOSED = b"User-agent: *\nDisallow: /\n"

ARTICLE_HTML = """
<html><head><title>342 siswa SMPN 35 Bandung keracunan</title>
<meta property="article:published_time" content="2025-04-30"/></head>
<body><nav>Beranda Nasional Regional</nav>
<article><h1>342 siswa SMPN 35 Bandung keracunan</h1>
<p>BANDUNG - Sebanyak 342 siswa SMP Negeri 35 Bandung mengalami keracunan setelah
mengonsumsi paket Makan Bergizi Gratis pada Selasa (29/4/2025). Para korban mengeluhkan
mual, muntah, dan diare beberapa jam setelah menyantap menu yang dibagikan di sekolah.</p>
<p>Kepala Dinas Kesehatan Kota Bandung menyatakan sampel makanan telah dikirim ke
laboratorium untuk diperiksa. Sebagian korban menjalani rawat jalan di puskesmas terdekat,
sementara belasan lainnya dirawat di rumah sakit.</p>
<p>Operasional dapur SPPG yang memasok menu tersebut dihentikan sementara selama masa
penyelidikan berlangsung. Pihak sekolah menyatakan akan menghentikan distribusi sampai
ada kepastian mengenai penyebab keracunan yang menimpa ratusan siswa itu.</p>
</article><aside>Baca juga: berita lainnya</aside></body></html>
""".encode()

PAYWALL_HTML = b"<html><body><article><p>Artikel ini khusus pelanggan.</p></article></body></html>"


class FakeNet:
    """Scripted responses keyed by URL substring, with a call log."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, ua: str = "") -> articles.FetchResult:
        self.calls.append(url)
        for key, (status, body) in self.routes.items():
            if key in url:
                return articles.FetchResult(body, url, status, "text/html")
        return articles.FetchResult(b"", url, 404)


def write_citations(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "x.citations.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def run(tmp_path: Path, rows: list[dict], net: FakeNet, **kwargs):
    return articles.fetch_articles(
        [write_citations(tmp_path, rows)],
        cache_dir=tmp_path / "cache",
        ledger_path=tmp_path / "ledger.jsonl",
        fetcher=net,
        delay=0.0,
        sleep=lambda _: None,
        **kwargs,
    )


# --- url handling -----------------------------------------------------------

def test_domain_strips_www():
    assert articles.domain_of("https://www.tempo.co/politik/x") == "tempo.co"


def test_original_of_unwraps_wayback():
    wrapped = "https://web.archive.org/web/20250923120000/https://example.org/kbb"
    assert articles.original_of(wrapped) == "https://example.org/kbb"
    assert articles.is_wayback(wrapped) is True


@pytest.mark.parametrize("value,expected", [
    ("2025-09-23", "20250923"),
    ("23 September 2025", "20250923"),
    ("September 2025", ""),
    ("", ""),
])
def test_date_to_timestamp(value, expected):
    assert articles.date_to_timestamp(value) == expected


def test_citation_urls_prefer_archive_and_deduplicate(tmp_path):
    path = write_citations(tmp_path, [
        {"ref_id": "a", "url": "https://example.org/x", "archive_url": "", "date": "2025-04-30"},
        {"ref_id": "b", "url": "https://example.org/x", "archive_url": "", "date": ""},
        {"ref_id": "c", "url": "https://example.org/y",
         "archive_url": "https://web.archive.org/web/2025/https://example.org/y", "date": ""},
    ])
    urls = articles.load_citation_urls([path])
    assert len(urls) == 2
    by_url = {u: refs for u, refs, _ in urls}
    assert by_url["https://example.org/x"] == ["a", "b"], "one fetch serves both citations"
    assert "web.archive.org" in " ".join(by_url)


# --- politeness -------------------------------------------------------------

def test_robots_disallow_routes_to_the_archive(tmp_path):
    snapshot = "https://web.archive.org/web/20250430/https://blocked.example/a"
    net = FakeNet({
        "/robots.txt": (200, ROBOTS_CLOSED),
        "archive.org/wayback/available": (200, json.dumps({
            "archived_snapshots": {"closest": {"available": True, "url": snapshot}}
        }).encode()),
        "web.archive.org": (200, ARTICLE_HTML),
    })
    ledger = run(tmp_path, [{"ref_id": "r", "url": "https://blocked.example/a",
                             "archive_url": "", "date": "2025-04-30"}], net)
    assert ledger[0].status == "ok"
    assert ledger[0].via == "wayback"
    assert "archive" in ledger[0].note
    assert not any(c == "https://blocked.example/a" for c in net.calls), \
        "origin must never be hit once robots.txt disallows it"


def test_robots_disallow_without_snapshot_is_recorded_not_dropped(tmp_path):
    net = FakeNet({
        "/robots.txt": (200, ROBOTS_CLOSED),
        "archive.org/wayback/available": (200, b'{"archived_snapshots": {}}'),
    })
    ledger = run(tmp_path, [{"ref_id": "r", "url": "https://blocked.example/a",
                             "archive_url": "", "date": ""}], net)
    assert ledger[0].status == "robots_disallowed"
    assert len(ledger) == 1, "failures stay in the ledger so the hole is countable"


def test_missing_robots_is_permissive(tmp_path):
    net = FakeNet({"/robots.txt": (404, b""), "example.org": (200, ARTICLE_HTML)})
    ledger = run(tmp_path, [{"ref_id": "r", "url": "https://example.org/a",
                             "archive_url": "", "date": ""}], net)
    assert ledger[0].status == "ok"


def test_archive_org_is_throttled_harder_than_publishers_and_as_one_host():
    slept: list[float] = []
    throttle = articles.DomainThrottle(delay=2.0, sleep=slept.append)
    throttle.wait("https://archive.org/wayback/available?url=x")
    throttle.wait("https://web.archive.org/web/2025/https://x")
    assert len(slept) == 1 and slept[0] > 2.0, "API and replay host share one strict limit"


def test_throttle_waits_between_hits_on_one_host():
    slept: list[float] = []
    throttle = articles.DomainThrottle(delay=2.0, sleep=slept.append)
    throttle.wait("https://a.example/1")
    throttle.wait("https://a.example/2")
    throttle.wait("https://b.example/1")
    assert len(slept) == 1 and slept[0] > 0, "same host waits, different host does not"


# --- fallback and classification -------------------------------------------

def test_dead_origin_falls_back_to_archive(tmp_path):
    snapshot = "https://web.archive.org/web/20250430/https://dead.example/a"
    net = FakeNet({
        "/robots.txt": (200, ROBOTS_OPEN),
        "archive.org/wayback/available": (200, json.dumps({
            "archived_snapshots": {"closest": {"available": True, "url": snapshot}}
        }).encode()),
        "web.archive.org": (200, ARTICLE_HTML),
        "dead.example": (404, b""),
    })
    ledger = run(tmp_path, [{"ref_id": "r", "url": "https://dead.example/a",
                             "archive_url": "", "date": "2025-04-30"}], net)
    assert ledger[0].status == "ok"
    assert ledger[0].via == "wayback"


def test_dead_origin_with_no_archive_is_an_http_error(tmp_path):
    net = FakeNet({
        "/robots.txt": (200, ROBOTS_OPEN),
        "archive.org/wayback/available": (200, b'{"archived_snapshots": {}}'),
        "dead.example": (410, b""),
    })
    ledger = run(tmp_path, [{"ref_id": "r", "url": "https://dead.example/a",
                             "archive_url": "", "date": ""}], net)
    assert ledger[0].status == "http_error"
    assert ledger[0].http_status == 410


def test_paywalled_domain_is_labelled_as_such_not_as_a_parse_failure():
    entry = articles.LedgerEntry(url="https://kompas.id/a", domain="kompas.id")
    assert articles.classify(entry, "terlalu pendek") == "paywalled"
    other = articles.LedgerEntry(url="https://example.org/a", domain="example.org")
    assert articles.classify(other, "terlalu pendek") == "short_body"


def test_boilerplate_is_stripped_from_the_body():
    parsed = articles.extract_body(ARTICLE_HTML, url="https://example.org/a")
    assert "342 siswa" in parsed["text"]
    assert "Beranda Nasional Regional" not in parsed["text"], "nav must not survive"
    assert "Baca juga" not in parsed["text"], "related-articles rail must not survive"


def test_resume_skips_urls_already_ok(tmp_path):
    rows = [{"ref_id": "r", "url": "https://example.org/a", "archive_url": "", "date": ""}]
    net = FakeNet({"/robots.txt": (200, ROBOTS_OPEN), "example.org": (200, ARTICLE_HTML)})
    run(tmp_path, rows, net)
    first = len(net.calls)
    run(tmp_path, rows, net)
    assert len(net.calls) == first, "a second run must not re-hit a URL already fetched"


def test_summary_counts_failures_by_domain(tmp_path):
    net = FakeNet({
        "/robots.txt": (200, ROBOTS_OPEN),
        "archive.org/wayback/available": (200, b'{"archived_snapshots": {}}'),
        "good.example": (200, ARTICLE_HTML),
        "bad.example": (404, b""),
    })
    ledger = run(tmp_path, [
        {"ref_id": "a", "url": "https://good.example/1", "archive_url": "", "date": ""},
        {"ref_id": "b", "url": "https://bad.example/1", "archive_url": "", "date": ""},
    ], net)
    stats = articles.summarise(ledger)
    assert stats["total"] == 2
    assert stats["by_status"]["ok"] == 1
    assert stats["failures_by_domain"]["bad.example"] == 1


# --- corpus assembly --------------------------------------------------------

WIRE = ("Sebanyak 342 siswa SMP Negeri 35 Bandung mengalami keracunan setelah mengonsumsi "
        "paket Makan Bergizi Gratis pada Selasa sore. Para korban mengeluhkan mual, muntah "
        "dan diare beberapa jam setelah menyantap menu yang dibagikan di sekolah mereka. "
        "Dinas Kesehatan mengirim sampel makanan ke laboratorium untuk diperiksa lebih lanjut.")


def _doc(doc_id, text, date="2025-04-30", domain="a.example"):
    return corpus.Document(doc_id=doc_id, url=f"https://{domain}/{doc_id}", domain=domain,
                           publish_date=date, n_chars=len(text), text=text)


def test_identical_syndicated_copy_collapses():
    docs = corpus.cluster_duplicates([
        _doc("d1", WIRE, "2025-04-30", "antara.example"),
        _doc("d2", WIRE, "2025-05-01", "regional.example"),
        _doc("d3", "Berita lain yang sama sekali berbeda isinya mengenai anggaran sekolah.",
             "2025-05-02", "lain.example"),
    ])
    by_id = {d.doc_id: d for d in docs}
    assert by_id["d1"].cluster_id == by_id["d2"].cluster_id
    assert by_id["d3"].cluster_id != by_id["d1"].cluster_id
    assert by_id["d1"].is_canonical and not by_id["d2"].is_canonical, "earliest wins"
    assert by_id["d2"].duplicate_of == "d1"


def test_near_duplicate_with_changed_headline_still_collapses():
    rewritten = WIRE.replace("Selasa sore", "Selasa siang") + " Redaksi menambahkan catatan."
    docs = corpus.cluster_duplicates([_doc("d1", WIRE), _doc("d2", rewritten, "2025-05-01")])
    assert docs[0].cluster_id == docs[1].cluster_id


def test_duplicates_are_kept_not_deleted():
    docs = corpus.cluster_duplicates([_doc("d1", WIRE), _doc("d2", WIRE, "2025-05-01")])
    assert len(docs) == 2, "reach is measurable only if the copies survive"
    assert sum(d.is_canonical for d in docs) == 1


def test_distinct_articles_are_not_merged():
    other = ("Sebanyak 186 siswa di SMP Negeri 8 Kota Kupang dilaporkan mengalami gejala "
             "keracunan setelah menyantap paket makan bergizi gratis pada Senin pagi. "
             "Puskesmas setempat menangani puluhan korban yang datang secara bergelombang.")
    docs = corpus.cluster_duplicates([_doc("d1", WIRE), _doc("d2", other)])
    assert docs[0].cluster_id != docs[1].cluster_id


def test_jaccard_bounds():
    assert corpus.jaccard(set(), {"a"}) == 0.0
    assert corpus.jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_build_corpus_joins_documents_to_incidents(tmp_path):
    net = FakeNet({"/robots.txt": (200, ROBOTS_OPEN), "example.org": (200, ARTICLE_HTML)})
    run(tmp_path, [{"ref_id": "bandung", "url": "https://example.org/bandung",
                    "archive_url": "", "date": "2025-04-30"}], net)

    incidents = tmp_path / "incidents.csv"
    incidents.write_text(
        'incident_id,source_urls\nfixture#r0003,"[""https://example.org/bandung""]"\n',
        encoding="utf-8",
    )
    stats = corpus.build_corpus(tmp_path / "ledger.jsonl", tmp_path / "corpus.jsonl",
                               incidents_csv=incidents)
    assert stats["documents"] == 1
    assert stats["documents_linked_to_incidents"] == 1
    row = json.loads((tmp_path / "corpus.jsonl").read_text().strip())
    assert row["incident_ids"] == ["fixture#r0003"]
    assert row["ref_ids"] == ["bandung"]


def test_unset_content_hash_does_not_collapse_the_corpus():
    """Regression: an empty content_hash on every doc once made them all 'identical'."""
    docs = [_doc(f"d{i}", f"Artikel nomor {i} tentang topik yang sepenuhnya berbeda satu "
                          f"sama lain dan tidak memiliki kalimat bersama sama sekali {i}.")
            for i in range(5)]
    assert all(d.content_hash == "" for d in docs)
    clustered = corpus.cluster_duplicates(docs)
    assert len({d.cluster_id for d in clustered}) == 5
    assert all(d.content_hash for d in clustered), "hashes must be filled in, not assumed"


def test_citations_from_filtered_out_sections_are_not_fetched(tmp_path):
    """The India entry's source must not enter the corpus when only Indonesia is kept."""
    cites = write_citations(tmp_path, [
        {"ref_id": "bandung", "url": "https://example.org/bandung", "archive_url": "", "date": ""},
        {"ref_id": "bihar", "url": "https://example.org/bihar", "archive_url": "", "date": ""},
    ])
    blocks = tmp_path / "x.rows.jsonl"
    blocks.write_text(json.dumps({"block_id": "b1", "text": "...", "ref_ids": ["bandung"]}) + "\n",
                      encoding="utf-8")

    keep = articles.refs_cited_by_rows([blocks])
    assert keep == {"bandung"}
    urls = [u for u, _, _ in articles.load_citation_urls([cites], keep_refs=keep)]
    assert urls == ["https://example.org/bandung"]
    assert len(articles.load_citation_urls([cites])) == 2, "unfiltered still returns both"


# --- robustness --------------------------------------------------------------

def test_network_failure_is_a_ledger_row_not_a_crash():
    result = articles.http_fetch("http://nonexistent.invalid/x", timeout=5)
    assert result.http_status == 0 and result.body == b"" and result.error


def test_unreachable_host_does_not_abort_the_crawl(tmp_path):
    class Flaky(FakeNet):
        def __call__(self, url, ua=""):
            if "down.example" in url and "robots" not in url:
                self.calls.append(url)
                return articles.FetchResult(b"", url, 0, error="URLError: name not resolved")
            return super().__call__(url, ua)

    net = Flaky({"/robots.txt": (404, b""), "archive.org/wayback/available": (200, b"{}"),
                 "up.example": (200, ARTICLE_HTML)})
    ledger = run(tmp_path, [
        {"ref_id": "a", "url": "https://down.example/1", "archive_url": "", "date": ""},
        {"ref_id": "b", "url": "https://up.example/1", "archive_url": "", "date": ""},
    ], net)
    assert {e.domain: e.status for e in ledger} == {"down.example": "network_error", "up.example": "ok"}


def test_ledger_survives_a_limited_rerun(tmp_path):
    rows = [{"ref_id": f"r{i}", "url": f"https://site{i}.example/a", "archive_url": "", "date": ""}
            for i in range(3)]
    net = FakeNet({"/robots.txt": (404, b""), "example": (200, ARTICLE_HTML)})
    run(tmp_path, rows, net)
    run(tmp_path, rows, net, limit=1)
    kept = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(kept) == 3, "a --limit rerun must not truncate the ledger"


def test_social_platforms_are_recorded_and_never_requested(tmp_path):
    net = FakeNet({})
    ledger = run(tmp_path, [{"ref_id": "r", "url": "https://www.instagram.com/p/abc/",
                             "archive_url": "", "date": ""}], net)
    assert ledger[0].status == "skipped_domain" and net.calls == []


def test_empty_origin_body_falls_back_to_a_longer_archived_copy(tmp_path):
    snapshot = "https://web.archive.org/web/20250430/https://js.example/a"
    net = FakeNet({
        "/robots.txt": (404, b""),
        "archive.org/wayback/available": (200, json.dumps({
            "archived_snapshots": {"closest": {"available": True, "url": snapshot}}}).encode()),
        "web.archive.org": (200, ARTICLE_HTML),
        "js.example": (200, b"<html><body><div id=app></div></body></html>"),
    })
    ledger = run(tmp_path, [{"ref_id": "r", "url": "https://js.example/a",
                             "archive_url": "", "date": "2025-04-30"}], net)
    assert ledger[0].status == "ok" and ledger[0].via == "wayback"


def test_citation_date_wins_over_extracted_date(tmp_path):
    net = FakeNet({"/robots.txt": (404, b""), "example.org": (200, ARTICLE_HTML)})
    ledger = run(tmp_path, [{"ref_id": "r", "url": "https://example.org/a",
                             "archive_url": "", "date": "2025-05-05"}], net)
    assert ledger[0].publish_date == "2025-05-05"


def test_targets_are_interleaved_across_hosts():
    targets = [(f"https://a.example/{i}", [], "") for i in range(3)] + [("https://b.example/0", [], "")]
    order = [articles.domain_of(u) for u, _, _ in articles.interleave_by_domain(targets)]
    assert order[:2] == ["a.example", "b.example"], "one busy host must not serialise the crawl"


def test_wayback_urls_are_pointed_at_the_raw_capture():
    got = articles.raw_snapshot("http://web.archive.org/web/20250430123456/https://x.example/a")
    assert got == "https://web.archive.org/web/20250430123456id_/https://x.example/a"
    assert articles.original_of(got) == "https://x.example/a"


def test_place_mismatch_between_row_and_article_is_measurable(tmp_path):
    """Regression for a real case: the Aceh Timur row cites an article about Cianjur."""
    net = FakeNet({"/robots.txt": (404, b""), "example.org": (200, ARTICLE_HTML)})
    run(tmp_path, [{"ref_id": "r", "url": "https://example.org/a", "archive_url": "", "date": ""}], net)
    incidents = tmp_path / "incidents.csv"
    incidents.write_text(
        'incident_id,source_urls,kabkota_name\n'
        'fixture#r1,"[""https://example.org/a""]",Aceh Timur\n',
        encoding="utf-8")
    corpus.build_corpus(tmp_path / "ledger.jsonl", tmp_path / "corpus.jsonl", incidents_csv=incidents)
    doc = json.loads((tmp_path / "corpus.jsonl").read_text().strip())
    assert doc["mentions_incident_place"] is False, "article is about Bandung, row says Aceh Timur"


def test_inline_related_story_lines_are_removed():
    html = ARTICLE_HTML.replace(
        b"</article>", b"<p>Baca juga: Pelajar Keracunan di Kota Lain Bertambah</p></article>")
    text = articles.extract_body(html, url="https://example.org/a")["text"]
    assert "342 siswa" in text and "Baca juga" not in text


def test_breaker_opens_on_429_per_host_and_closes_after_cooldown():
    now = [0.0]
    breaker = articles.HostBreaker(cooldown=900, archive_cooldown=300, clock=lambda: now[0])
    assert not breaker.is_open("https://a.example/x")
    breaker.record("https://a.example/x", 429)
    assert breaker.is_open("https://a.example/y") and not breaker.is_open("https://b.example/y")
    now[0] = 899
    assert breaker.is_open("https://a.example/z")
    now[0] = 901
    assert not breaker.is_open("https://a.example/z")


def test_archive_hosts_share_one_breaker_and_a_shorter_cooldown():
    now = [0.0]
    breaker = articles.HostBreaker(cooldown=900, archive_cooldown=300, clock=lambda: now[0])
    breaker.record("https://archive.org/wayback/available?url=x", 429)
    assert breaker.is_open("https://web.archive.org/web/2025/https://x")
    now[0] = 301
    assert not breaker.is_open("https://web.archive.org/web/2025/https://x")


def test_a_run_of_403s_after_success_is_treated_as_being_blocked():
    now = [0.0]
    breaker = articles.HostBreaker(block_cooldown=1800, clock=lambda: now[0])
    url = "https://news.example/a"
    for _ in range(10):                       # a host that never answered is a paywall/blocklist, not a ban
        breaker.record(url, 403)
    assert not breaker.is_open(url)
    breaker.record(url, 200)
    for _ in range(articles.HostBreaker.BLOCK_AFTER_403S):
        breaker.record(url, 403)
    assert breaker.is_open(url) and breaker.remaining(url) == 1800


def test_jitter_lengthens_the_wait_within_bounds():
    slept: list[float] = []
    throttle = articles.DomainThrottle(delay=8.0, sleep=slept.append, jitter=0.5, rng=lambda: 1.0)
    throttle.wait("https://a.example/1")
    throttle.wait("https://a.example/2")
    assert 11.0 < slept[0] <= 12.0, "8 s plus up to 50% jitter"


def test_a_rate_limited_host_is_deferred_and_the_crawl_waits_only_when_nothing_else_remains(tmp_path):
    class Limited(FakeNet):
        def __init__(self, routes):
            super().__init__(routes)
            self.limited_once = False

        def __call__(self, url, ua=""):
            if "slow.example" in url and "robots" not in url and not self.limited_once:
                self.limited_once = True
                self.calls.append(url)
                return articles.FetchResult(b"", url, 429)
            return super().__call__(url, ua)

    slept: list[float] = []
    now = [0.0]

    def sleep(seconds):                       # a real sleep moves the breaker's clock; so does this
        slept.append(seconds)
        now[0] += seconds

    net = Limited({"/robots.txt": (404, b""), "archive.org/wayback/available": (200, b"{}"),
                   "slow.example": (200, ARTICLE_HTML), "fast.example": (200, ARTICLE_HTML)})
    ledger = articles.fetch_articles(
        [write_citations(tmp_path, [
            {"ref_id": "a", "url": "https://slow.example/1", "archive_url": "", "date": ""},
            {"ref_id": "b", "url": "https://slow.example/2", "archive_url": "", "date": ""},
            {"ref_id": "c", "url": "https://fast.example/1", "archive_url": "", "date": ""}])],
        cache_dir=tmp_path / "cache", ledger_path=tmp_path / "ledger.jsonl", fetcher=net, delay=0.0,
        sleep=sleep, ordered=True, use_wayback=False,
        breaker=articles.HostBreaker(cooldown=900, clock=lambda: now[0]))
    by_url = {e.url: e.status for e in ledger}
    assert by_url["https://slow.example/1"] == "http_error", "the 429ed URL is ledgered for the rerun"
    assert by_url["https://fast.example/1"] == "ok"
    assert by_url["https://slow.example/2"] == "ok", "fetched after the cooldown, not skipped"
    order = [c for c in net.calls if "robots" not in c]
    assert order.index("https://fast.example/1") < order.index("https://slow.example/2"), "other hosts go first"
    assert any(s >= 800 for s in slept), "with only the limited host left, wait out the cooldown"


def test_ordered_mode_keeps_the_priority_order(tmp_path):
    net = FakeNet({"/robots.txt": (404, b""), "example": (200, ARTICLE_HTML)})
    rows = [{"ref_id": f"r{i}", "url": f"https://example.org/{name}", "archive_url": "", "date": ""}
            for i, name in enumerate(["zulu", "alpha", "mike"])]
    articles.fetch_articles([write_citations(tmp_path, rows)], cache_dir=tmp_path / "c",
                            ledger_path=tmp_path / "l.jsonl", fetcher=net, delay=0.0,
                            sleep=lambda _: None, ordered=True, use_wayback=False)
    fetched = [c.rsplit("/", 1)[1] for c in net.calls if "robots" not in c]
    assert fetched == ["zulu", "alpha", "mike"], "file order is the priority order"


def test_rate_limited_archive_is_asked_once_not_once_per_failed_url(tmp_path):
    net = FakeNet({
        "/robots.txt": (404, b""),
        "archive.org/wayback/available": (429, b""),
        "dead.example": (403, b""),
    })
    ledger = run(tmp_path, [
        {"ref_id": f"r{i}", "url": f"https://dead.example/{i}", "archive_url": "", "date": ""}
        for i in range(4)
    ], net)
    lookups = [c for c in net.calls if "wayback/available" in c]
    assert len(lookups) == 1, "after one 429 the breaker keeps the crawl off archive.org"
    assert all(e.status == "http_error" for e in ledger)
    assert all("archive lookup HTTP 429" in e.note for e in ledger), "the reason is ledgered for the rerun"


@pytest.mark.parametrize("raw,expected", [
    ("2025-10-08", "2025-10-08"),
    ("2025-10-08T16:19:00+07:00", "2025-10-08"),
    ("8 Oktober 2025 {{!}} 16.19 WIB", "2025-10-08"),
    ("17 April 2026", "2026-04-17"),
    ("Oktober 2025", ""),
    ("", ""),
])
def test_iso_date_normalises_editor_typed_citation_dates(raw, expected):
    assert articles.iso_date(raw) == expected


def test_three_network_errors_in_a_row_pause_the_host_and_a_success_resets_the_count():
    now = [0.0]
    breaker = articles.HostBreaker(error_cooldown=300, clock=lambda: now[0])
    url = "https://a.example/x"
    breaker.record(url, 0)
    breaker.record(url, 0)
    breaker.record(url, 200)                  # the run is broken
    breaker.record(url, 0)
    breaker.record(url, 0)
    assert not breaker.is_open(url)
    breaker.record(url, 0)
    assert breaker.is_open(url) and breaker.remaining(url) == 300
