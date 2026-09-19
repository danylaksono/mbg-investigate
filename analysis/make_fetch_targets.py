"""Turn discovered headlines into a priority list for the `articles` stage.

    python analysis/make_fetch_targets.py --outlet detik

Order: headlines that link to one event, then ambiguous ones, then the rest. The rest name no
place in the headline, but the article body often does, which is what full-text linking uses.
Skipped: video pages (no article text), and URLs already fetched successfully for the
Wikipedia citations, whose cached text the link stage reuses.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DISC = ROOT / "data" / "interim" / "discovered"


def is_video(url: str, title: str) -> bool:
    host, path = url.split("/")[2], url.split("/", 3)[3] if url.count("/") >= 3 else ""
    # detik photo galleries (/foto-news/, /fotohealth/, /foto/, /foto-bisnis/) are captions only
    return (host.startswith(("20.", "video.")) or path.startswith(("video/", "foto/", "tv/"))
            or bool(re.search(r"(?:^|/)foto[a-z-]*/", path))
            or title.lower().startswith(("video:", "video ", "foto:")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outlet", default="detik")
    ap.add_argument("--include", default="linked,ambiguous,none",
                    help="headline decisions to fetch, comma-separated. A big feed (Kompas: 2,288 headlines) "
                         "is limited to linked,ambiguous so the crawl stays a modest load on the site")
    args = ap.parse_args(argv)
    include = set(args.include.split(","))

    links = pd.read_csv(DISC / f"{args.outlet}.links.csv")
    cited_ok = set()
    ledger = ROOT / "data" / "interim" / "fetch_ledger.jsonl"
    if ledger.exists():
        cited_ok = {json.loads(l)["url"] for l in open(ledger, encoding="utf-8") if '"status": "ok"' in l}

    rank = {"linked": 0, "ambiguous": 1, "none": 2}
    links["rank"] = links.decision.map(rank)
    keep = links[~links.apply(lambda r: is_video(r.url, r.title), axis=1) & ~links.url.isin(cited_ok)
                 & links.decision.isin(include)]
    keep = keep.sort_values(["rank", "listed_date"], ascending=[True, False], kind="stable")

    out = DISC / f"{args.outlet}.fetch_targets.jsonl"
    with open(out, "w", encoding="utf-8") as fh:
        for i, r in enumerate(keep.itertuples()):
            fh.write(json.dumps({"ref_id": f"disc:{args.outlet}:{i:05d}", "url": r.url,
                                 "date": r.listed_date if isinstance(r.listed_date, str) else ""}) + "\n")
    print(f"{len(links)} headlines -> {len(keep)} targets "
          f"(skipped {len(links) - len(keep)}: video pages, already fetched, or decision not in {sorted(include)})")
    print(keep.decision.value_counts().to_dict(), "->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
