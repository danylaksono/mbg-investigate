"""Command line entry point. One subcommand per stage, each reading the previous
stage's artefacts off disk, so any stage can be re-run or swapped out alone.

    python -m mbgpipe fetch      --out data/raw
    python -m mbgpipe parse      --raw data/raw --out data/interim
    python -m mbgpipe normalize  --interim data/interim --out data/processed
    python -m mbgpipe articles   --interim data/interim --cache data/raw/articles
    python -m mbgpipe corpus     --ledger data/interim/fetch_ledger.jsonl --out data/processed/corpus.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import articles, corpus, discover, fetch, normalize, parse
from .extract import places


def cmd_fetch(args) -> int:
    for path in fetch.cache_pages(Path(args.out)):
        print(f"wrote {path}")
    return 0


def cmd_parse(args) -> int:
    raw, out = Path(args.raw), Path(args.out)
    files = sorted(raw.glob("*.wikitext"))
    if not files:
        print(f"no .wikitext files in {raw} — run `fetch` first", file=sys.stderr)
        return 1
    for path in files:
        print(json.dumps(parse.parse_file(path, out, section=args.section), ensure_ascii=False))
    return 0


def cmd_normalize(args) -> int:
    interim, out = Path(args.interim), Path(args.out)
    row_files = sorted(interim.glob("*.rows.jsonl"))
    if not row_files:
        print(f"no *.rows.jsonl in {interim} — run `parse` first", file=sys.stderr)
        return 1
    codes_path = Path(args.kabkota_codes)
    codes = places.load_kabkota_codes(codes_path) if codes_path.exists() else None
    if codes is None:
        print(f"note: {codes_path} not found, so no kabupaten/kota codes; "
              "see analysis/build_kode_wilayah.py", file=sys.stderr)
    incidents: list[normalize.Incident] = []
    wikipedia_total = None
    for rpath in row_files:
        stem = rpath.name.removesuffix(".rows.jsonl")
        incidents.extend(normalize.normalize_file(rpath, interim / f"{stem}.citations.jsonl", codes))
        report = json.loads((interim / f"{stem}.table_report.json").read_text(encoding="utf-8"))
        wikipedia_total = wikipedia_total or report.get("wikipedia_total")
    events = normalize.build_events(incidents)
    normalize.write_csv(incidents, out / "incidents.csv")
    normalize.write_csv(events, out / "events.csv")
    report = normalize.qc_report(incidents, events, wikipedia_total)
    (out / "qc_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_articles(args) -> int:
    interim = Path(args.interim)
    if args.targets:                       # a priority list from discovery, not the page's citations
        paths, keep_refs = [Path(args.targets)], None
    else:
        paths = sorted(interim.glob("*.citations.jsonl"))
        if not paths:
            print(f"no citations in {interim} — run `parse` first", file=sys.stderr)
            return 1
        keep_refs = None
        if not args.all_citations:
            row_files = sorted(interim.glob("*.rows.jsonl"))
            if row_files:
                keep_refs = articles.refs_cited_by_rows(row_files)
    ledger = articles.fetch_articles(
        paths,
        keep_refs=keep_refs,
        cache_dir=Path(args.cache),
        ledger_path=Path(args.ledger or interim / "fetch_ledger.jsonl"),
        delay=args.delay,
        jitter=args.jitter,
        ordered=args.ordered or bool(args.targets),
        limit=args.limit,
        resume=not args.no_resume,
        use_wayback=not args.no_wayback,
        progress=lambda n, total, e: print(
            f"[{n}/{total}] {e.status:16} {e.domain}", file=sys.stderr, flush=True),
    )
    print(json.dumps(articles.summarise(ledger), indent=2, ensure_ascii=False))
    return 0


def cmd_corpus(args) -> int:
    stats = corpus.build_corpus(
        Path(args.ledger),
        Path(args.out),
        incidents_csv=Path(args.incidents) if args.incidents else None,
        threshold=args.threshold,
    )
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


def cmd_discover(args) -> int:
    return discover.main(args.outlet, Path(args.out) / f"{args.outlet}.tag.jsonl", args.max_pages, args.delay,
                         args.jitter, Path(args.raw_dir) / args.outlet)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mbgpipe", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="stage 1: cache wikitext from the MediaWiki API")
    f.add_argument("--out", default="data/raw")
    f.set_defaults(func=cmd_fetch)

    pa = sub.add_parser("parse", help="stage 2: wikitext -> citations + table rows")
    pa.add_argument("--raw", default="data/raw")
    pa.add_argument("--out", default="data/interim")
    pa.add_argument("--section", default="Indonesia",
                    help="heading whose first table holds the incidents")
    pa.set_defaults(func=cmd_parse)

    n = sub.add_parser("normalize", help="stage 3: rows -> incidents.csv + events.csv + qc_report.json")
    n.add_argument("--interim", default="data/interim")
    n.add_argument("--out", default="data/processed")
    n.add_argument("--kabkota-codes", default="data/reference/kabkota_kemendagri.csv",
                   help="Kemendagri kode wilayah for the 514 kabupaten/kota (optional)")
    n.set_defaults(func=cmd_normalize)

    a = sub.add_parser("articles", help="stage 4: fetch article bodies behind the citations")
    a.add_argument("--interim", default="data/interim")
    a.add_argument("--cache", default="data/raw/articles")
    a.add_argument("--ledger", default=None)
    a.add_argument("--delay", type=float, default=2.0, help="seconds between hits on one host")
    a.add_argument("--jitter", type=float, default=0.0,
                   help="random extra delay as a fraction of --delay (0.5: 8 s becomes 8-12 s)")
    a.add_argument("--ordered", action="store_true", help="keep the input order instead of interleaving hosts")
    a.add_argument("--targets", default=None,
                   help="jsonl of {ref_id,url,date} in priority order (discovery); implies --ordered")
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--no-resume", action="store_true", help="re-fetch URLs already marked ok")
    a.add_argument("--no-wayback", action="store_true", help="skip the archive fallback")
    a.add_argument("--all-citations", action="store_true",
                   help="fetch every citation on the page, not just those cited by table rows")
    a.set_defaults(func=cmd_articles)

    c = sub.add_parser("corpus", help="stage 5: assemble and de-syndicate the corpus")
    c.add_argument("--ledger", default="data/interim/fetch_ledger.jsonl")
    c.add_argument("--out", default="data/processed/corpus.jsonl")
    c.add_argument("--incidents", default="data/processed/incidents.csv")
    c.add_argument("--threshold", type=float, default=corpus.NEAR_DUPLICATE_THRESHOLD)
    c.set_defaults(func=cmd_corpus)

    d = sub.add_parser("discover", help="stage 6: enumerate an outlet's topic feed, independent of Wikipedia")
    d.add_argument("--outlet", choices=sorted(discover.OUTLETS), default="detik")
    d.add_argument("--out", default="data/interim/discovered")
    d.add_argument("--max-pages", type=int, default=400)
    d.add_argument("--delay", type=float, default=10.0, help="seconds between listing pages (plus jitter)")
    d.add_argument("--jitter", type=float, default=0.5, help="random extra delay as a fraction of --delay")
    d.add_argument("--raw-dir", default="data/raw/feeds",
                   help="keep every listing page as gzipped HTML here, one directory per run")
    d.set_defaults(func=cmd_discover)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
