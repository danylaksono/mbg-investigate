"""Stage 4 — fetch the article bodies behind the citation manifest.

Input is `*.citations.jsonl` from stage 2. Output is a cache of raw HTML plus a
ledger recording what happened to every URL, including the ones that failed.
The failures matter: a corpus that silently omits everything Kompas.id paywalled
is a corpus with a systematic hole in it, and you want that hole countable.

Design notes:

*   Politeness is not optional. One request at a time per host, a real delay
    between requests to the same host, robots.txt honoured, and a User-Agent that
    says who you are. These are small newsrooms; do not hammer them.
*   robots.txt disallow is treated as a routing decision, not a dead end. The
    Wayback Machine is consulted instead, which is the correct way to read a
    page whose publisher does not want crawlers on their origin server.
*   The citation's own date picks the Wayback snapshot, so you get the article
    as it read around publication rather than whatever it says today. Victim
    counts in this corpus get edited after the fact.
*   The ledger is appended to as each URL finishes. A crash or Ctrl-C loses
    nothing, and a rerun retries only what has not succeeded.
*   Every network call goes through an injectable `fetcher`, so the whole module
    is testable offline.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import re
import time
from collections import deque
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import user_agent
from .extract.dates import primary_date

WAYBACK_API = "https://archive.org/wayback/available"
ARCHIVE_MIN_DELAY = 6.0

# Domains known to put articles behind a hard paywall. Not a blocklist — they are
# still attempted — but a short extraction from one of these is paywall truncation
# rather than a parser failure, and the ledger should say so.
PAYWALLED_DOMAINS = {"kompas.id", "koran.tempo.co", "majalah.tempo.co"}

# Social platforms are login-walled and forbid scraping; they hold no article body.
# Recorded in the ledger as `skipped_domain` rather than dropped.
SKIP_DOMAINS = {"instagram.com", "facebook.com", "tiktok.com", "x.com", "twitter.com",
                "youtube.com", "youtu.be"}

# Minimum extracted characters before a body is considered a real article.
MIN_BODY_CHARS = 400
MAX_BODY_BYTES = 8_000_000

RE_WS = re.compile(r"\s+")
# Inline related-story links ("Baca juga: ...") survive extraction on Kompas and others.
# Left in, they add another headline's vocabulary to the article and skew term counts.
RE_RELATED_LINE = re.compile(r"^[ \t]*(?:baca|simak|lihat)\s+(?:juga|selengkapnya|lainnya)\b.*$", re.I | re.M)
RE_BLANK_RUN = re.compile(r"\n{3,}")
RE_BASE_REF = re.compile(r"\.\d+$")


class FetchResult:
    """Raw bytes plus how we got them. `http_status` 0 means no HTTP response at all."""

    def __init__(self, body: bytes, final_url: str, http_status: int,
                 content_type: str = "", via: str = "origin", error: str = ""):
        self.body = body
        self.final_url = final_url
        self.http_status = http_status
        self.content_type = content_type
        self.via = via
        self.error = error


Fetcher = Callable[[str, str], FetchResult]


def http_fetch(url: str, ua: str = "", timeout: int = 30) -> FetchResult:
    """Default fetcher. Never raises: HTTP errors and network failures both come
    back as results, so one bad host cannot abort a crawl of hundreds."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua or user_agent(),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "id,en;q=0.7",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY_BYTES)
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return FetchResult(raw, resp.geturl(), resp.status,
                               resp.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as exc:
        return FetchResult(b"", url, exc.code, "", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — DNS, TLS, timeout, reset, bad gzip...
        return FetchResult(b"", url, 0, "", error=f"{type(exc).__name__}: {exc}")


@dataclass
class LedgerEntry:
    url: str
    ref_ids: list[str] = field(default_factory=list)
    status: str = "pending"
    # ok | short_body | paywalled | empty | robots_disallowed | http_error
    # | network_error | no_archive | skipped_domain
    http_status: Optional[int] = None
    via: str = ""               # "origin" | "wayback"
    final_url: str = ""
    domain: str = ""
    cache_path: str = ""
    sha256: str = ""
    n_chars: int = 0
    title: str = ""
    author: str = ""
    publish_date: str = ""      # the citation's date, else the extracted one
    extracted_date: str = ""    # trafilatura's reading of the page, for comparison
    fetched_at: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def domain_of(url: str) -> str:
    netloc = urllib.parse.urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def is_wayback(url: str) -> bool:
    return "web.archive.org/web/" in url


def original_of(url: str) -> str:
    """Strip a Wayback wrapper back to the origin URL."""
    m = re.search(r"/web/\d+[a-z_]*/(https?://.+)$", url)
    return m.group(1) if m else url


def _registrable(domain: str) -> str:
    """'news.detik.com' -> 'detik.com' — enough to match SKIP_DOMAINS."""
    return ".".join(domain.split(".")[-2:])


class RobotsCache:
    """One robots.txt per domain, fetched once, failures treated as permissive.

    A missing or unreachable robots.txt is not a disallow — that is what the
    standard says, and treating it as a block would drop working sources.
    """

    def __init__(self, fetcher: Optional[Fetcher] = None, ua: str = ""):
        self._cache: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._fetcher = fetcher or http_fetch
        self.ua = ua or user_agent()

    def allowed(self, url: str) -> bool:
        if is_wayback(url):
            return True  # the archive is not the publisher's origin server
        domain = urllib.parse.urlparse(url).netloc
        if domain not in self._cache:
            self._cache[domain] = self._load(url)
        parser = self._cache[domain]
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.ua, url)
        except Exception:  # noqa: BLE001
            return True

    def _load(self, url: str):
        parts = urllib.parse.urlparse(url)
        try:
            result = self._fetcher(f"{parts.scheme}://{parts.netloc}/robots.txt", self.ua)
            if result.http_status != 200 or not result.body:
                return None
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(result.body.decode("utf-8", errors="replace").splitlines())
            return parser
        except Exception:  # noqa: BLE001
            return None


class DomainThrottle:
    """Minimum seconds between requests to the same host, plus random jitter.

    `jitter` is a fraction of the delay added at random (0.5 turns 8 s into 8-12 s), so a
    crawl does not hit a host on a metronome. That regularity is what rate limiters notice.
    """

    def __init__(self, delay: float = 2.0, sleep: Callable[[float], None] = time.sleep,
                 jitter: float = 0.0, rng: Callable[[], float] = random.random):
        self.delay = delay
        self.jitter = jitter
        self._rng = rng
        self._last: dict[str, float] = {}
        self._sleep = sleep
        self._clock = time.monotonic

    def wait(self, url: str) -> None:
        host = urllib.parse.urlparse(url).netloc
        delay = self.delay
        if host.endswith("archive.org"):
            # The availability API and the replay host share one rate limit, and it
            # is strict: a burst earns a long run of 429s.
            host, delay = "archive.org", max(self.delay, ARCHIVE_MIN_DELAY)
        delay *= 1 + self.jitter * self._rng()
        now = self._clock()
        last = self._last.get(host)
        if last is not None:
            remaining = delay - (now - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()


def host_key(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    return "archive.org" if host.endswith("archive.org") else host


class HostBreaker:
    """Circuit breaker per host. Once a host answers 429/503, more requests only prolong
    the ban, so stop asking it for `cooldown` seconds (archive.org, which limits hard and
    recovers slowly, gets its own). A run of 403s from a host that had been answering
    normally is treated the same way: that pattern is being blocked, not a paywall.
    Skipped URLs are ledgered with the reason and retried on the next run."""

    BLOCK_AFTER_403S = 5
    PAUSE_AFTER_ERRORS = 3

    def __init__(self, cooldown: float = 900.0, archive_cooldown: float = 300.0,
                 block_cooldown: float = 1800.0, error_cooldown: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        self.cooldown, self.archive_cooldown, self.block_cooldown = cooldown, archive_cooldown, block_cooldown
        self.error_cooldown = error_cooldown
        self._clock = clock
        self._until: dict[str, float] = {}
        self._ok_seen: set[str] = set()
        self._run_403: dict[str, int] = {}
        self._run_err: dict[str, int] = {}

    def trip(self, url: str, cooldown: Optional[float] = None) -> None:
        host = host_key(url)
        default = self.archive_cooldown if host == "archive.org" else self.cooldown
        self._until[host] = self._clock() + (cooldown or default)

    def is_open(self, url: str) -> bool:
        return self._clock() < self._until.get(host_key(url), 0.0)

    def remaining(self, url: str) -> float:
        return max(0.0, self._until.get(host_key(url), 0.0) - self._clock())

    def record(self, url: str, status: int) -> None:
        """Feed every response in; trips the breaker on 429/503 or a run of 403s after success."""
        host = host_key(url)
        if status == 0:                               # no HTTP response: reset, timeout, DNS
            self._run_err[host] = self._run_err.get(host, 0) + 1
            if self._run_err[host] >= self.PAUSE_AFTER_ERRORS:
                self.trip(url, self.error_cooldown)
                self._run_err[host] = 0
            return
        self._run_err[host] = 0
        if status in (429, 503):
            self.trip(url)
        elif status == 403 and host in self._ok_seen:
            self._run_403[host] = self._run_403.get(host, 0) + 1
            if self._run_403[host] >= self.BLOCK_AFTER_403S:
                self.trip(url, self.block_cooldown)
                self._run_403[host] = 0
        elif 200 <= status < 300:
            self._ok_seen.add(host)
            self._run_403[host] = 0


def raw_snapshot(url: str) -> str:
    """Point a Wayback URL at the archived bytes without the replay toolbar/rewriting
    (`/web/<ts>id_/`), which is closer to what the publisher served."""
    return re.sub(r"(/web/\d+)(/https?://)", r"\1id_\2", url.replace("http://web.archive.org", "https://web.archive.org"))


def wayback_lookup(
    url: str,
    timestamp: str = "",
    fetcher: Optional[Fetcher] = None,
    ua: str = "",
    info: Optional[dict] = None,
) -> Optional[str]:
    """Closest Wayback snapshot for `url`, or None. `info["status"]` receives the
    HTTP status of the lookup, so a rate-limited miss (429) is told apart from a
    URL that was simply never archived.

    `timestamp` is YYYYMMDD; pass the citation's own date so the snapshot matches
    what the article said at the time rather than after later edits.
    """
    fetcher = fetcher or http_fetch
    params = {"url": original_of(url)}
    if timestamp:
        params["timestamp"] = timestamp
    try:
        result = fetcher(f"{WAYBACK_API}?{urllib.parse.urlencode(params)}", ua or user_agent())
        if info is not None:
            info["status"] = result.http_status
        if result.http_status != 200 or not result.body:
            return None
        payload = json.loads(result.body.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
    snapshot = (payload.get("archived_snapshots") or {}).get("closest") or {}
    if snapshot.get("available") and snapshot.get("url"):
        return raw_snapshot(snapshot["url"])
    return None


def date_to_timestamp(value: str) -> str:
    """'2025-09-23' or '23 September 2025' -> '20250923'. Empty when unparseable."""
    if not value:
        return ""
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
    if iso:
        return "".join(iso.groups())
    match = primary_date(value)
    if match and match.precision == "day":
        return match.iso.replace("-", "")
    return ""


def iso_date(value: str) -> str:
    """'8 Oktober 2025 {{!}} 16.19 WIB' or '2025-10-08T..' -> '2025-10-08'. Empty if unparseable.
    Citation dates are editor-typed and not always ISO; the corpus sorts on this."""
    ts = date_to_timestamp(value or "")
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:]}" if ts else ""


def extract_body(html: bytes, url: str = "") -> dict:
    """Article text and metadata via trafilatura.

    trafilatura rather than markitdown: it was built to strip news boilerplate —
    nav, related-articles rails, comment widgets — which is exactly what these
    sites are made of. markitdown would faithfully convert all of it.
    """
    import trafilatura

    text_input = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    text = trafilatura.extract(
        text_input, url=url or None, include_comments=False,
        include_tables=True, favor_precision=True,
    ) or ""
    meta_title = meta_author = meta_date = ""
    try:
        meta = trafilatura.extract_metadata(text_input, default_url=url or None)
        if meta:
            meta_title = meta.title or ""
            meta_author = meta.author or ""
            meta_date = meta.date or ""
    except Exception:  # noqa: BLE001
        pass
    text = RE_BLANK_RUN.sub("\n\n", RE_RELATED_LINE.sub("", text))
    return {"text": text.strip(), "title": meta_title,
            "author": meta_author, "publish_date": meta_date}


def classify(entry: LedgerEntry, text: str) -> str:
    """Final status for one fetched document."""
    if not text:
        return "empty"
    if len(text) < MIN_BODY_CHARS:
        return "paywalled" if entry.domain in PAYWALLED_DOMAINS else "short_body"
    return "ok"


def refs_cited_by_rows(paths: Iterable[Path]) -> set[str]:
    """Ref ids actually cited by the incident-table rows kept at stage 2.

    Citations are extracted page-wide, so they include the India, South Korea and
    China entries and the footnotes. Without this filter stage 4 spends requests
    on articles no incident row will ever join to.
    """
    keep: set[str] = set()
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                keep.update(json.loads(line).get("ref_ids", []))
    return keep


def load_citation_urls(
    paths: Iterable[Path],
    keep_refs: Optional[set[str]] = None,
    keep_order: bool = False,
) -> list[tuple[str, list[str], str]]:
    """Collapse citations to unique (url, ref_ids, date) triples, sorted by URL, or in file
    order with `keep_order` (a priority list stays in priority order).

    Archive URL wins over the live one, and the same URL cited from several
    rows is fetched once. `keep_refs` restricts the set to citations reachable
    from the incident rows.
    """
    merged: dict[str, dict] = {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if keep_refs is not None and RE_BASE_REF.sub("", row["ref_id"]) not in keep_refs:
                    continue
                url = (row.get("archive_url") or row.get("url") or "").strip()
                if not url.startswith("http"):
                    continue
                slot = merged.setdefault(url, {"ref_ids": [], "date": row.get("date", "")})
                slot["ref_ids"].append(row["ref_id"])
                if not slot["date"]:
                    slot["date"] = row.get("date", "")
    items = merged.items() if keep_order else sorted(merged.items())
    return [(url, sorted(set(v["ref_ids"])), v["date"]) for url, v in items]


def interleave_by_domain(targets: list[tuple[str, list[str], str]]) -> list[tuple[str, list[str], str]]:
    """Round-robin across hosts so the per-host delay overlaps other hosts' work
    instead of idling. Sorted order would sleep through 60 consecutive detik.com hits."""
    queues: dict[str, list] = {}
    for t in targets:
        queues.setdefault(domain_of(original_of(t[0])), []).append(t)
    out = []
    while queues:
        for domain in list(queues):
            out.append(queues[domain].pop(0))
            if not queues[domain]:
                del queues[domain]
    return out


def fetch_articles(
    citation_paths: Iterable[Path],
    cache_dir: Path,
    ledger_path: Path,
    fetcher: Optional[Fetcher] = None,
    ua: str = "",
    delay: float = 2.0,
    limit: Optional[int] = None,
    resume: bool = True,
    use_wayback: bool = True,
    keep_refs: Optional[set[str]] = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Optional[Callable[[int, int, LedgerEntry], None]] = None,
    jitter: float = 0.0,
    ordered: bool = False,
    breaker: Optional[HostBreaker] = None,
) -> list[LedgerEntry]:
    """Fetch every cited URL, caching bodies and recording outcomes.

    Resumable: URLs already marked `ok` are skipped, so an interrupted run costs
    nothing to restart and re-runs only retry failures. The ledger keeps entries
    for URLs outside this run's targets (e.g. when `limit` is set).

    `ordered=True` keeps the given priority order instead of interleaving hosts, for a
    single-site crawl where the most useful URLs should come first. A host that answers
    429/503 is put on cooldown: other hosts' URLs go first, and if only that host remains
    the crawl sleeps until the cooldown ends rather than hammering it.
    """
    fetcher = fetcher or http_fetch
    ua = ua or user_agent()
    cache_dir, ledger_path = Path(cache_dir), Path(ledger_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    known: dict[str, LedgerEntry] = {}
    if resume and ledger_path.exists():
        with open(ledger_path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    known[row["url"]] = LedgerEntry(**row)   # later lines win

    robots = RobotsCache(fetcher, ua)
    throttle = DomainThrottle(delay, sleep, jitter)
    targets = load_citation_urls(citation_paths, keep_refs, keep_order=ordered)
    targets = targets if ordered else interleave_by_domain(targets)
    if limit:
        targets = targets[:limit]

    breaker = breaker or HostBreaker()

    def polite_get(target: str) -> FetchResult:
        if breaker.is_open(target):
            return FetchResult(b"", target, 429, error=f"{host_key(target)} cooling down after a 429")
        throttle.wait(target)
        result = fetcher(target, ua)
        breaker.record(target, result.http_status)
        return result

    processed: list[LedgerEntry] = []
    queue = deque(targets)
    n = 0
    with open(ledger_path, "a", encoding="utf-8") as ledger_fh:
        while queue:
            url, ref_ids, cite_date = queue.popleft()
            previous = known.get(url)
            if previous and previous.status == "ok":
                processed.append(previous)
                n += 1
                continue
            if breaker.is_open(url) and not host_key(url) == "archive.org":
                if any(not breaker.is_open(u) for u, _, _ in queue):
                    queue.append((url, ref_ids, cite_date))      # try other hosts first
                    continue
                sleep(breaker.remaining(url))                    # only this host is left: wait it out

            n += 1
            entry = fetch_one(url, ref_ids, cite_date, cache_dir, robots, polite_get,
                              fetcher, ua, use_wayback)
            known[url] = entry
            processed.append(entry)
            ledger_fh.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
            ledger_fh.flush()
            if progress:
                progress(n, len(targets), entry)

    with open(ledger_path, "w", encoding="utf-8") as fh:      # compact: one line per URL
        for entry in known.values():
            fh.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
    return processed


def fetch_one(url, ref_ids, cite_date, cache_dir, robots, polite_get, fetcher, ua,
              use_wayback) -> LedgerEntry:
    entry = LedgerEntry(url=url, ref_ids=ref_ids, domain=domain_of(original_of(url)))
    if _registrable(entry.domain) in SKIP_DOMAINS:
        entry.status = "skipped_domain"
        entry.note = "social platform: login-walled, no article body"
        return entry

    def snapshot() -> Optional[str]:
        if not use_wayback:
            return None
        info: dict = {}
        found = wayback_lookup(url, date_to_timestamp(cite_date), lambda u, _ua: polite_get(u), ua, info)
        if not found and info.get("status") not in (None, 200):
            entry.note = (entry.note + "; " if entry.note else "") + f"archive lookup HTTP {info['status']}"
        return found

    target, via = url, "wayback" if is_wayback(url) else "origin"
    if not robots.allowed(url):
        snap = snapshot()
        if not snap:
            entry.status = "robots_disallowed"
            entry.note = "robots.txt disallow and no archive snapshot"
            return entry
        target, via = snap, "wayback"
        entry.note = "origin disallowed by robots.txt; read from archive"

    result = polite_get(target)
    if result.http_status >= 400 or result.http_status == 0 or not result.body:
        snap = snapshot() if via == "origin" else None
        if snap:
            result = polite_get(snap)
            target, via = snap, "wayback"
            entry.note = (entry.note + "; " if entry.note else "") + "origin failed; archived copy"
        if result.http_status >= 400 or result.http_status == 0 or not result.body:
            entry.status = ("network_error" if result.http_status == 0
                            else "http_error" if result.http_status >= 400 else "no_archive")
            entry.http_status, entry.via = result.http_status, via
            entry.note = (entry.note + "; " if entry.note else "") + (result.error or "")[:200]
            return entry

    def read(res: FetchResult, where: str) -> dict:
        try:
            return extract_body(res.body, url=original_of(where))
        except Exception as exc:  # noqa: BLE001 — a parser crash on one page must not end the run
            entry.note = (entry.note + "; " if entry.note else "") + f"extract failed: {type(exc).__name__}"
            return {"text": "", "title": "", "author": "", "publish_date": ""}

    parsed = read(result, target)
    if (via == "origin" and len(parsed["text"]) < MIN_BODY_CHARS
            and entry.domain not in PAYWALLED_DOMAINS):
        # Often a JS-rendered shell or a truncated page; the archive may hold the full text.
        snap = snapshot()
        if snap:
            alt = polite_get(snap)
            alt_parsed = read(alt, snap) if alt.body and alt.http_status < 400 else None
            if alt_parsed and len(alt_parsed["text"]) > len(parsed["text"]):
                result, target, via, parsed = alt, snap, "wayback", alt_parsed
                entry.note = (entry.note + "; " if entry.note else "") + "origin body empty/short; archived copy"

    entry.http_status, entry.via, entry.final_url = result.http_status, via, result.final_url
    entry.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry.sha256 = hashlib.sha256(result.body).hexdigest()

    stem = hashlib.sha1(url.encode()).hexdigest()
    cache_path = Path(cache_dir) / f"{stem}.html.gz"
    cache_path.write_bytes(gzip.compress(result.body))
    entry.cache_path = str(cache_path)

    entry.title, entry.author = parsed["title"], parsed["author"]
    # The citation's own date is the editors' publication date. trafilatura's guess
    # can be a modified-on or scrape date, so it is kept alongside, not instead.
    entry.publish_date = iso_date(cite_date) or iso_date(parsed["publish_date"])
    entry.extracted_date = parsed["publish_date"]
    entry.n_chars = len(parsed["text"])
    entry.status = classify(entry, parsed["text"])
    (Path(cache_dir) / f"{stem}.txt").write_text(parsed["text"], encoding="utf-8")
    return entry


def summarise(ledger: Iterable[LedgerEntry]) -> dict:
    ledger = list(ledger)
    by_status: dict[str, int] = {}
    by_domain_fail: dict[str, int] = {}
    for entry in ledger:
        by_status[entry.status] = by_status.get(entry.status, 0) + 1
        if entry.status != "ok":
            by_domain_fail[entry.domain] = by_domain_fail.get(entry.domain, 0) + 1
    return {
        "total": len(ledger),
        "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        "via_wayback": sum(1 for e in ledger if e.via == "wayback"),
        "recovered_by_archive": sum(
            1 for e in ledger if e.via == "wayback" and e.status == "ok" and e.note
        ),
        "failures_by_domain": dict(sorted(by_domain_fail.items(), key=lambda kv: -kv[1])),
    }
