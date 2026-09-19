"""Audit the cause lexicon for false negatives.

    python analysis/recall_audit.py [--n 60] [--seed 7]

Draws a random sample of sentences that contain strong cause vocabulary (lab, bacteria,
nitrite, "penyebab", "disebabkan", ...) but were NOT flagged as a lab finding, from articles
linked to an event, and prints them for reading. Judge each: does it report a cause finding
(a miss), speculate, or say nothing about cause? The miss rate among them says how far the
lab-result counts are undercounts. The sample and seed are printed so an audit is repeatable.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nlp_explore as N  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

STRONG = re.compile(
    r"hasil\s+(?:lab|uji|pemeriksaan|laboratorium|analisis)|labkesda|\bbpom\b|bakteri|e\.?\s?-?coli|salmonella|"
    r"nitrit|histamin|penyebab|disebabkan|dipicu|positif|terkontaminasi|kontaminasi|\bbasi\b", re.I)


def flagged(sentence: str) -> bool:
    return bool(N.reports_lab_finding(sentence)
                or N.LAB_NEGATIVE.search(sentence) or N.AWAITING.search(sentence))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    run = (ROOT / "data/evidence/LATEST").read_text().strip()
    run_dir = ROOT / "data/evidence/runs" / run
    linked = {}
    for line in open(run_dir / "link_decisions.jsonl", encoding="utf-8"):
        d = json.loads(line)
        if d["decision"] == "linked" or d["cited_events"]:
            linked[d["url"]] = d
    docs, inc, ev, ledger = N.load()
    texts = {r["original_url"]: r["text"] for _, r in docs.iterrows()}
    cache = {}
    for f in (ROOT / "data/interim/discovered").glob("*.fetch_ledger.jsonl"):
        for line in open(f, encoding="utf-8"):
            r = json.loads(line)
            if r.get("status") == "ok" and r.get("cache_path"):
                p = Path(r["cache_path"]).with_suffix("").with_suffix(".txt")
                if p.exists():
                    cache[r["url"]] = p

    pool = []
    for url in linked:
        text = texts.get(url) or (cache[url].read_text(encoding="utf-8") if url in cache else "")
        for s in N.sentences(text):
            if STRONG.search(s) and not flagged(s) and 40 < len(s) < 400:
                pool.append((url, s))
    random.Random(args.seed).shuffle(pool)
    print(f"evidence run {run}: {len(linked)} linked articles, {len(pool)} unflagged strong-cue sentences; "
          f"showing {args.n} (seed {args.seed})\n")
    for i, (url, s) in enumerate(pool[:args.n], 1):
        print(f"{i:2}. {s}\n    {url[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
