"""Stage 1 — fetch raw wikitext from the MediaWiki API.

Wikitext rather than rendered HTML, because the citation templates carry
structured fields (url, title, work, date, archive-url) that the HTML render
flattens into "[1][2][3]". One fetch, two datasets.

Everything is cached to disk so stages 2 and 3 can be re-run without touching
the network. The page is live and edited, so the revision id is recorded in
`*.meta.json`: that is what makes a given corpus reproducible.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import user_agent

# The list page the whole pipeline is built around. Older titles for it
# ("... program Makan Bergizi Gratis") are redirects to this one.
PAGES = {"id": ["Daftar kasus keracunan massal makan siang gratis"]}


@dataclass
class Page:
    lang: str
    title: str                  # the RESOLVED title, after following redirects
    wikitext: str
    revid: Optional[int] = None
    revision_timestamp: Optional[str] = None


def _api(lang: str) -> str:
    return f"https://{lang}.wikipedia.org/w/api.php"


def _get_json(url: str, params: dict, retries: int = 3) -> dict:
    query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": user_agent()})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — retry on anything transient
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"API request failed after {retries} attempts: {last}")


def fetch_page(title: str, lang: str = "id") -> Page:
    """Current wikitext for one page, redirects followed."""
    data = _get_json(
        _api(lang),
        {"action": "query", "prop": "revisions", "rvprop": "content|ids|timestamp",
         "rvslots": "main", "titles": title, "redirects": "1"},
    )
    pages = data.get("query", {}).get("pages", [])
    if not pages or "missing" in pages[0]:
        raise LookupError(f"page not found: {lang}:{title}")
    rev = pages[0]["revisions"][0]
    return Page(lang=lang, title=pages[0]["title"], wikitext=rev["slots"]["main"]["content"],
                revid=rev.get("revid"), revision_timestamp=rev.get("timestamp"))


def cache_pages(out_dir: Path, pages: Optional[dict[str, list[str]]] = None,
                delay: float = 1.0) -> list[Path]:
    """Fetch every configured page and write `.wikitext` + `.meta.json` to `out_dir`.

    Files are named by the resolved title, and a page reached twice (two titles, one
    redirect) is written once. Written twice, every row downstream would be duplicated.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for lang, titles in (pages or PAGES).items():
        for title in titles:
            page = fetch_page(title, lang=lang)
            slug = f"{lang}.{page.title.replace(' ', '_').replace('/', '-')}"
            if slug in written:
                continue
            path = out_dir / f"{slug}.wikitext"
            path.write_text(page.wikitext, encoding="utf-8")
            (out_dir / f"{slug}.meta.json").write_text(json.dumps({
                "lang": page.lang, "title": page.title, "requested_title": title,
                "revid": page.revid, "revision_timestamp": page.revision_timestamp,
                "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            written[slug] = path
            time.sleep(delay)
    return list(written.values())
