"""Stage 6 — enumerate news coverage independently of the Wikipedia citations.

The Wikipedia table cites roughly one article per incident, written on the day. Cause
results (lab findings, closures) come later and are mostly not cited. Rather than searching
once per event, this walks an outlet's topic feed (e.g. detik.com/tag/keracunan-mbg), a
bounded global stream of exactly this subject, and records every headline with its URL and
listed date. Linking those to events happens offline, in `link.py`.

Politeness matches stage 4: robots.txt honoured, one request at a time per host with a delay
and jitter, the shared User-Agent, and a pause on 429/503. Only listing pages are fetched
here; article bodies go through the ordinary `articles` stage afterwards.

Evidence: with `raw_dir` set, every listing page is kept as gzipped HTML with its sha256 and
fetch time in a manifest, so a headline can always be traced to the page it came from.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .articles import DomainThrottle, HostBreaker, RobotsCache, http_fetch
from .config import user_agent

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "jun": 6,
          "jul": 7, "agu": 8, "sep": 9, "okt": 10, "nov": 11, "des": 12}

RE_BLOCK = re.compile(r"<article\b.*?</article>", re.S)
RE_HREF = re.compile(r'<a\s+href="(https?://[^"]+)"')
RE_ALT = re.compile(r'\balt="([^"]+)"')
RE_LISTED_DATE = re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|Mei|Jun|Jul|Agu|Sep|Okt|Nov|Des)[a-z]*\.?\s+(\d{4})", re.I)
RE_TAGS = re.compile(r"<[^>]+>")
# A tag feed is on topic by construction. When it runs out, some sites serve generic latest news
# (Kompas did, from page 154) instead of an empty page, so the end is detected by topic.
TOPIC = re.compile(r"keracunan|mbg|makan[- ]bergizi|bergizi|gizi|sppg|bgn|santap|siswa|murid|pelajar|santri", re.I)
OFF_TOPIC_BELOW = 0.2


@dataclass
class Listed:
    outlet: str
    url: str
    title: str
    listed_date: Optional[str]      # ISO date shown next to the headline, if any
    origin: str                     # e.g. "tag:detik:keracunan-mbg:page=12"

    def as_dict(self) -> dict:
        return asdict(self)


def _text(fragment: str) -> str:
    return html.unescape(RE_TAGS.sub("", fragment)).replace("\xa0", " ").strip()


def _iso(text: str) -> Optional[str]:
    """First 'D Month YYYY' (full or abbreviated Indonesian month) in `text`, as an ISO date."""
    m = RE_LISTED_DATE.search(text)
    return f"{int(m[3]):04d}-{MONTHS[m[2][:3].lower()]:02d}-{int(m[1]):02d}" if m else None


# Each parser returns (url, title, iso date or None) per headline on one listing page.

def parse_detik(page: str) -> list[tuple[str, str, Optional[str]]]:
    """<article> with a link, an image alt (the title) and a date."""
    out = []
    for block in RE_BLOCK.findall(page):
        href, alt = RE_HREF.search(block), RE_ALT.search(block)
        if href and alt:
            out.append((html.unescape(href.group(1)), html.unescape(alt.group(1)).strip(), _iso(RE_TAGS.sub(" ", block))))
    return out


def parse_antara(page: str) -> list[tuple[str, str, Optional[str]]]:
    """`card__post-list` cards: <h2><a href title> and a date span. Sidebar widgets on the same
    page use other classes and are not picked up."""
    out = []
    for block in re.split(r'(?=<div class="card__post card__post-list)', page)[1:]:
        m = re.search(r'<h2 class="h5"><a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if m and "/berita/" in m[1]:                        # the feed also lists /video/ and /foto/ items
            out.append((html.unescape(m[1]), _text(m[2]), _iso(re.sub(r"\s+", " ", block[:3000]))))
    return out


def parse_kompas(page: str) -> list[tuple[str, str, Optional[str]]]:
    out = []
    for block in re.split(r'(?=<div class="articleItem">)', page)[1:]:
        href = re.search(r'class="article-link" href="([^"]+)"', block)
        title = re.search(r'<h2 class="articleTitle">(.*?)</h2>', block, re.S)
        date = re.search(r'<div class="articlePost-date">(.*?)</div>', block, re.S)
        if href and title:
            out.append((html.unescape(href[1]), _text(title[1]), _iso(date[1]) if date else None))
    return out


def parse_cnn(page: str) -> list[tuple[str, str, Optional[str]]]:
    """The visible 'x minggu yang lalu' and the HTML comment beside it are render-relative, not the
    article's date. The URL carries the real one: /<section>/<YYYYMMDDHHMMSS>-<...>/<slug>."""
    out = []
    for block in RE_BLOCK.findall(page):
        href = re.search(r'href="(https://www\.cnnindonesia\.com/[a-z-]+/(\d{4})(\d\d)(\d\d)\d{6}-[^"]+)"', block)
        title = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        if href and title:
            out.append((html.unescape(href[1]), _text(title[1]), f"{href[2]}-{href[3]}-{href[4]}"))
    return out


def parse_liputan6(page: str) -> list[tuple[str, str, Optional[str]]]:
    """Only the tag list's text items (`articles--iridescent-list--text-item`). The page also
    carries a mega-menu and a sidebar of unrelated stories, and video items with no article text."""
    out = []
    for block in re.findall(r'<article\b[^>]*iridescent-list--text-item[^>]*>.*?</article>', page, re.S):
        href = re.search(r'href="(https://www\.liputan6\.com/[a-z0-9-]+/read/\d+/[^"]+)"', block)
        title = re.search(r'data-title="([^"]+)"', block)
        date = re.search(r'datetime="(\d{4}-\d\d-\d\d)', block)
        if href and title:
            out.append((html.unescape(href[1]), html.unescape(title[1]).strip(), date[1] if date else None))
    return out


PARSERS: dict[str, Callable[[str], list[tuple[str, str, Optional[str]]]]] = {
    "detik": parse_detik, "antara": parse_antara, "kompas": parse_kompas,
    "cnn": parse_cnn, "liputan6": parse_liputan6,
}

# outlet -> listing URL for page n of its "keracunan-mbg" topic feed
OUTLETS: dict[str, Callable[[int], str]] = {
    "detik": lambda n: "https://www.detik.com/tag/keracunan-mbg/?sortby=time" + (f"&page={n}" if n > 1 else ""),
    "antara": lambda n: "https://www.antaranews.com/tag/keracunan-mbg" + (f"/{n}" if n > 1 else ""),
    "kompas": lambda n: "https://www.kompas.com/tag/keracunan-mbg" + (f"?page={n}" if n > 1 else ""),
    "cnn": lambda n: "https://www.cnnindonesia.com/tag/keracunan-mbg" + (f"?page={n}" if n > 1 else ""),
    "liputan6": lambda n: "https://www.liputan6.com/tag/keracunan-mbg" + (f"?page={n}" if n > 1 else ""),
}


def parse_listing(page_html: str, outlet: str, origin: str) -> list[Listed]:
    """Headlines from a listing page, or [] for a page with none, which ends the walk."""
    parser = PARSERS.get(outlet, parse_detik)
    return [Listed(outlet, url, title, iso, origin) for url, title, iso in parser(page_html)]


def walk_feed(
    outlet: str,
    out_path: Path,
    max_pages: int = 400,
    since: str = "2024-10-01",
    delay: float = 2.5,
    jitter: float = 0.0,
    fetcher: Callable = http_fetch,
    sleep: Callable[[float], None] = time.sleep,
    progress: Optional[Callable[[int, int, str], None]] = None,
    raw_dir: Optional[Path] = None,
) -> dict:
    """Page through an outlet's feed newest-first until it ends, runs past `since`, or
    `max_pages`. Rows are appended as each page arrives, and a rerun resumes after the last
    page written. (The feed shifts as new articles appear, so resume soon or start over.)

    A host that answers 429/503 or resets connections is paused by the breaker: the walk waits
    it out and continues, and gives up (resumably) if the host keeps refusing.
    """
    page_url = OUTLETS[outlet]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    start = 1
    if out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                seen.add(row["url"])
                start = max(start, int(row["origin"].rsplit("page=", 1)[1]) + 1)

    if raw_dir:
        raw_dir = Path(raw_dir) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        raw_dir.mkdir(parents=True, exist_ok=True)

    ua = user_agent()
    robots, throttle, breaker = RobotsCache(fetcher, ua), DomainThrottle(delay, sleep, jitter), HostBreaker()
    stats = {"pages": 0, "headlines": 0, "stopped": "max_pages", "oldest": None}
    refusals = 0
    with open(out_path, "a", encoding="utf-8") as fh:
        n = start
        while n <= max_pages:
            url = page_url(n)
            if not robots.allowed(url):
                stats["stopped"] = "robots_disallow"
                break
            if breaker.is_open(url):
                sleep(breaker.remaining(url))
            throttle.wait(url)
            result = fetcher(url, ua)
            breaker.record(url, result.http_status)
            if result.http_status in (429, 503, 0):
                refusals += 1
                if refusals >= 3:
                    stats["stopped"] = f"refused_{result.http_status}"
                    break
                continue                                    # breaker has paused the host; retry this page
            refusals = 0
            if result.http_status != 200:
                stats["stopped"] = f"http_{result.http_status}"
                break
            page = result.body.decode("utf-8", "replace")
            items = parse_listing(page, outlet, f"tag:{outlet}:keracunan-mbg:page={n}")
            if raw_dir:
                (raw_dir / f"page-{n:04d}.html.gz").write_bytes(gzip.compress(result.body))
                with open(raw_dir / "manifest.jsonl", "a", encoding="utf-8") as mf:
                    mf.write(json.dumps({"outlet": outlet, "page": n, "url": url, "http_status": result.http_status,
                                         "bytes": len(result.body), "sha256": hashlib.sha256(result.body).hexdigest(),
                                         "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                         "headlines": len(items)}) + "\n")
            new = [i for i in items if i.url not in seen]
            stats["pages"] += 1
            if not items:
                stats["stopped"] = "end_of_feed"
                break
            on_topic = [i for i in items if TOPIC.search(f"{i.title} {i.url}")]
            if len(items) >= 5 and len(on_topic) / len(items) < OFF_TOPIC_BELOW:
                # The feed has run out and the site is padding with unrelated news. Keep the page
                # aside as evidence, but not in the headline list.
                with open(out_path.with_suffix(".off_topic.jsonl"), "a", encoding="utf-8") as off:
                    for item in new:
                        off.write(json.dumps(item.as_dict(), ensure_ascii=False) + "\n")
                stats["stopped"] = "off_topic"
                break
            for item in new:
                seen.add(item.url)
                fh.write(json.dumps(item.as_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            stats["headlines"] += len(new)
            dated = [i.listed_date for i in items if i.listed_date]
            if dated:
                stats["oldest"] = min(dated + ([stats["oldest"]] if stats["oldest"] else []))
                if max(dated) < since:
                    stats["stopped"] = f"reached_{since}"
                    break
            if progress:
                progress(n, len(new), min(dated) if dated else "")
            n += 1
    return stats


def main(outlet: str, out: Path, max_pages: int, delay: float, jitter: float = 0.5,
         raw_dir: Optional[Path] = None) -> int:
    stats = walk_feed(outlet, out, max_pages=max_pages, delay=delay, jitter=jitter, raw_dir=raw_dir,
                      progress=lambda n, k, d: print(f"[{outlet}] page {n}: {k} new, oldest {d}",
                                                     file=sys.stderr, flush=True))
    print(json.dumps(stats, indent=2))
    return 0

