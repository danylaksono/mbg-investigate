"""Link every article (Wikipedia-cited and independently discovered) to events, extract cause
findings as quoted sentences, and keep it all as evidence.

    python analysis/build_evidence.py

Each run writes a NEW directory, data/evidence/runs/<UTC timestamp>/, and never overwrites an
earlier one, so a finding can always be traced to what was known when it was made:

    manifest.json        inputs (sha256), rule and lexicon versions, counts
    articles.jsonl       every article considered: source, date and where the date came from,
                         fetch status, sha256 of the raw page, path of the cached text
    link_decisions.jsonl one row per article: decision, tier, matched terms, candidate events
    findings.jsonl       one row per extracted sentence: kind, the sentence, its character
                         offset in the cached text, the event(s) it was linked to
    event_summary.csv    per event: articles from each source, best cause state, first
                         cause-finding date, lag from the incident, agents named
    summary.json         the headline numbers

Findings are quoted, not paraphrased, so each can be checked against the cached article.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nlp_explore as N  # noqa: E402
from mbgpipe import articles as A  # noqa: E402
from mbgpipe import link  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DISC = ROOT / "data" / "interim" / "discovered"

STATE_RANK = {"lab: contamination reported": 4, "lab: result reported clean": 3,
              "awaiting lab / cause unknown": 2, "samples taken, no result stated": 1,
              "no cause information": 0, "no article read": -1}
RESOLVED = {"lab: contamination reported", "lab: result reported clean"}

AGENT_NAMES = [
    (re.compile(r"e\.?\s?-?coli|escherichia", re.I), "E. coli"),
    (re.compile(r"salmonella", re.I), "Salmonella"),
    (re.compile(r"staphylo\w*", re.I), "Staphylococcus"),
    (re.compile(r"bacillus", re.I), "Bacillus"),
    (re.compile(r"coliform", re.I), "coliform"),
    (re.compile(r"nitrit|nitrat", re.I), "nitrite/nitrate"),
    (re.compile(r"histamin\w*", re.I), "histamine"),
    (re.compile(r"formalin|boraks|pestisida|sianida|logam\s+berat", re.I), "chemical (other)"),
    (re.compile(r"norovirus|virus", re.I), "virus"),
    (re.compile(r"\bbakteri\b|kuman", re.I), "bacteria (unspecified)"),
]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def agents_in(sentence: str) -> list[str]:
    return [name for rx, name in AGENT_NAMES if rx.search(sentence)]


# Who is the finding attributed to? Lab results reach us through journalists quoting officials, so this is
# the "grain of salt" made explicit. A health office or laboratory reporting a sample is one thing; the
# programme's own operator (BGN, a kitchen, its foundation) or a politician saying the food was fine is
# an interested party. The classes are stored, not scored: the reader decides what to trust.
ATTRIBUTION = {
    "health_body": re.compile(
        r"dinas\s+kesehatan|\bdinkes\w*|labkesda|laboratorium\s+kesehatan|\bbb?pom\b|balai\s+(?:besar\s+)?pengawas\s+obat|"
        r"kemenkes|menkes|kementerian\s+kesehatan|\bbtkl\b|puskesmas|\brsud\b|dokter|\bdr\.?\s|epidemiolog|"
        r"kepala\s+bidang\s+(?:kesehatan|p2p)", re.I),
    "programme_operator": re.compile(
        r"\bbgn\b|badan\s+gizi|\bsppg\b|satuan\s+pelayanan|satgas\s+mbg|mitra\s+mbg|yayasan|kepala\s+dapur|penyedia", re.I),
    "police": re.compile(r"polres|polsek|polda|polisi|kapolres|kasat|kepolisian|penyidik|labfor", re.I),
    "local_government": re.compile(
        r"bupati|wali\s?kota|gubernur|\bwagub\b|wakil\s+(?:bupati|wali)|\bsekda\b|pemkab|pemkot|pemprov|pemda|\bdprd\b", re.I),
    "school": re.compile(r"kepala\s+sekolah|\bkepsek\b|\bguru\b|komite\s+sekolah|pihak\s+sekolah", re.I),
}


def attribution(sentence: str, previous: str = "") -> list[str]:
    """Bodies named in the sentence or the one before it (the speaker is often introduced there:
    "Kepala Dinkes X mengatakan... Ia menyebut hasil lab..."). Empty means unattributed."""
    context = f"{previous} {sentence}"
    return [name for name, rx in ATTRIBUTION.items() if rx.search(context)]


def to_date(value) -> date | None:
    iso = A.iso_date(value) if isinstance(value, str) else ""
    return date.fromisoformat(iso) if iso else None


def findings_for(text: str) -> list[dict]:
    """Quoted sentences worth keeping as evidence, at most one per kind per article. Each carries
    `attributed_to`: the bodies named in that sentence or the one before it."""
    out, seen = [], set()
    sents = N.sentences(text)

    def add(kind, i, **extra):
        if kind not in seen:
            seen.add(kind)
            sentence = sents[i]
            out.append({"kind": kind, "sentence": sentence[:400], "char_start": text.find(sentence[:80]),
                        "attributed_to": attribution(sentence, sents[i - 1] if i else ""), **extra})

    for i, s in enumerate(sents):
        if N.reports_lab_finding(s):
            add("lab_contamination", i, agents=agents_in(s))
        elif N.reports_lab_clean(s):
            add("lab_clean", i)
        elif N.AWAITING.search(s):
            add("awaiting_result", i)
        if N.RESPONSE["SPPG halted / closed"].search(s):
            add("sppg_halted", i)
        for name, rx in N.MECHANISM.items():
            if name.startswith(("spoilage", "timing", "hygiene")) and rx.search(s):
                add(f"mechanism: {name.split(' (')[0]}", i)
    return out


def load_articles(keys, docs, ledger_rows) -> dict[str, dict]:
    """One record per article URL, merging the two sources."""
    arts: dict[str, dict] = {}
    ledger = {r["url"]: r for r in ledger_rows}
    for _, d in docs.iterrows():                       # Wikipedia-cited
        led = ledger.get(d["url"], {})
        when, source = to_date(d["publish_date"]), "citation_or_extracted"
        # A page "dated" on the day we crawled it was dated by the extractor (a modified-on or
        # scrape date), not the newsroom. Keeping it would push the article out of every window.
        if when and led and str(when) == (led.get("fetched_at") or "")[:10] and str(when) == A.iso_date(led.get("extracted_date", "")):
            when, source = None, "extractor_crawl_day_discarded"
        arts[d["original_url"]] = {
            "url": d["original_url"], "sources": ["wikipedia_citation"], "title": d["title_text"], "text": d["text"],
            "when": when, "date_source": source,
            "status": "ok", "sha256": led.get("sha256", ""), "fetched_at": led.get("fetched_at", ""),
            "cache_path": led.get("cache_path", ""),
            "cited_events": list(d["event_ids"]), "doc_id": d["doc_id"], "listed_title": ""}
    for f in sorted(DISC.glob("*.tag.jsonl")):
        outlet = f.name.split(".")[0]
        for line in open(f, encoding="utf-8"):
            row = json.loads(line)
            a = arts.get(row["url"])
            if a is None:
                led = ledger.get(row["url"], {})
                text = ""
                cache = led.get("cache_path", "")
                if led.get("status") == "ok" and cache and Path(cache).with_suffix("").with_suffix(".txt").exists():
                    text = Path(cache).with_suffix("").with_suffix(".txt").read_text(encoding="utf-8")
                a = arts[row["url"]] = {
                    "url": row["url"], "sources": [], "title": row["title"], "text": text,
                    "when": None, "date_source": "", "status": led.get("status", "not_fetched"),
                    "sha256": led.get("sha256", ""), "fetched_at": led.get("fetched_at", ""), "cache_path": cache,
                    "cited_events": [], "doc_id": hashlib.sha1(row["url"].encode()).hexdigest()[:12],
                    "listed_title": row["title"]}
            a["sources"].append(f"tag_feed:{outlet}")
            listed = to_date(row["listed_date"])
            if listed and a["date_source"] != "feed_listed_date":   # the newsroom's own date beats an extracted one
                a["when"], a["date_source"] = listed, "feed_listed_date"
    return arts


def main() -> int:
    docs, inc, ev, ledger = N.load()
    docs = N.attach_events(docs, inc, ev)
    events = pd.read_csv(ROOT / "data/processed/events.csv")
    events["venues"] = events.venues.apply(json.loads)
    keys = link.event_keys(events.to_dict("records"))
    key_by_id = {k.event_id: k for k in keys}

    disc_ledger = []
    for f in DISC.glob("*.fetch_ledger.jsonl"):
        disc_ledger += [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
    arts = load_articles(keys, docs, ledger + disc_ledger)

    run_dir = ROOT / "data" / "evidence" / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True)
    decisions, findings, per_event = [], [], defaultdict(list)
    audit = Counter()

    for url, a in arts.items():
        res = link.link_article(a["title"], a["text"], a["when"], keys)
        state = N.certainty(a["text"]) if a["text"] else "no article read"
        cited = a["cited_events"]
        check = ""
        if cited:
            lead = f"{a['title']}\n{a['text'][:link.LEAD_CHARS]}"
            names_cited = any(link.names_event(lead, key_by_id[e]) for e in cited if e in key_by_id)
            if res.decision == "linked" and res.event_ids[0] in cited:
                check = "agrees"
            elif res.decision == "ambiguous" and set(res.event_ids) & set(cited):
                check = "ambiguous_includes_cited"
            elif not names_cited:
                # The headline and lead never name the cited place or venue: the likely
                # wrong-source rows (an Aceh Timur row citing a Cianjur story).
                check = "place_not_named_in_lead"
            elif a["when"] is None:
                check = "place_ok_date_unknown"
            else:
                # The place is named but the date rule points elsewhere: another event in the
                # same regency, or a date error on the page or in the article.
                check = "place_ok_but_dates_or_other_event"
            audit[check] += 1
        decisions.append({"url": url, "doc_id": a["doc_id"], "sources": a["sources"], "when": str(a["when"] or ""),
                          "decision": res.decision, "tier": res.tier, "event_ids": res.event_ids,
                          "matches": [m.__dict__ for m in res.matches], "cited_events": cited,
                          "cited_vs_text": check, "state": state, "rules_version": link.RULES_VERSION})
        event_ids = cited or (res.event_ids if res.decision == "linked" else [])
        for eid in event_ids:
            per_event[eid].append({"url": url, "sources": a["sources"], "state": state, "when": a["when"],
                                   "via": "citation" if cited else "link"})
        if event_ids and a["text"]:
            for f in findings_for(a["text"]):
                findings.append({"url": url, "doc_id": a["doc_id"], "event_ids": event_ids, "when": str(a["when"] or ""),
                                 "sources": a["sources"], "sha256": a["sha256"], "cache_path": a["cache_path"], **f})

    rows = []
    for _, e in events.iterrows():
        items = per_event.get(e.event_id, [])
        cited_only = [i for i in items if "wikipedia_citation" in i["sources"]]

        def best(xs):
            return max((i["state"] for i in xs), key=lambda s: STATE_RANK[s], default="no article read")

        allb, citb = best(items), best(cited_only)
        resolved_dates = sorted(i["when"] for i in items if i["state"] in RESOLVED and i["when"])
        start = link.event_keys([e.to_dict()])
        lag = (resolved_dates[0] - start[0].start).days if resolved_dates and start else None
        agents = sorted({a for f in findings if e.event_id in f["event_ids"] for a in f.get("agents", [])})
        result_findings = [f for f in findings if e.event_id in f["event_ids"] and f["kind"] in ("lab_contamination", "lab_clean")]
        who = sorted({b for f in result_findings for b in f["attributed_to"]})
        if not result_findings:
            attributed = ""
        elif "health_body" in who:
            attributed = "health_body"          # a health office or laboratory is named as the source
        elif who:
            attributed = "other_official_only"  # only the programme operator, police, a politician or a school
        else:
            attributed = "unattributed"
        rows.append({"event_id": e.event_id, "date_start": e.date_start, "kabkota": e.kabkota, "province": e.province,
                     "cited_articles": len(cited_only), "discovered_articles": sum(1 for i in items if i["via"] == "link"),
                     "state_cited_only": citb, "state_all": allb,
                     "resolved_by_discovery": citb not in RESOLVED and allb in RESOLVED,
                     "first_cause_finding_date": str(resolved_dates[0]) if resolved_dates else "",
                     "days_to_first_cause_finding": lag, "agents": json.dumps(agents),
                     "result_attributed_to": attributed, "result_attribution_classes": json.dumps(who)})
    summ = pd.DataFrame(rows)

    def dump(name, records):
        with open(run_dir / name, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    dump("articles.jsonl", [{k: (str(v) if isinstance(v, date) else v) for k, v in a.items() if k != "text"}
                            | {"n_chars": len(a["text"])} for a in arts.values()])
    dump("link_decisions.jsonl", decisions)
    dump("findings.jsonl", findings)
    summ.to_csv(run_dir / "event_summary.csv", index=False, encoding="utf-8")

    in_span = summ[summ.date_start >= "2025-04-26"]
    headline = {
        "articles": len(arts), "cited": sum("wikipedia_citation" in a["sources"] for a in arts.values()),
        "discovered_only": sum(a["sources"] == [s for s in a["sources"] if s.startswith("tag_feed")] for a in arts.values()),
        "decisions": dict(Counter(d["decision"] for d in decisions)),
        "cited_vs_text": dict(audit),
        "events": len(summ), "events_in_feed_span": len(in_span),
        "events_resolved_cited_only": int(summ.state_cited_only.isin(RESOLVED).sum()),
        "events_resolved_with_discovery": int(summ.state_all.isin(RESOLVED).sum()),
        "events_newly_resolved_by_discovery": int(summ.resolved_by_discovery.sum()),
        "median_days_to_first_cause_finding": (None if summ.days_to_first_cause_finding.dropna().empty
                                               else float(summ.days_to_first_cause_finding.dropna().median())),
        "findings": dict(Counter(f["kind"] for f in findings)),
        "resolved_events_by_attribution": dict(Counter(summ[summ.state_all.isin(RESOLVED)].result_attributed_to)),
    }
    (run_dir / "summary.json").write_text(json.dumps(headline, indent=2, ensure_ascii=False), encoding="utf-8")
    inputs = {str(p.relative_to(ROOT)): sha256_file(p) for p in [
        ROOT / "data/processed/events.csv", ROOT / "data/processed/incidents.csv", ROOT / "data/processed/corpus.jsonl",
        *sorted(DISC.glob("*.tag.jsonl")), *sorted(DISC.glob("*.fetch_ledger.jsonl"))]}
    lexicon = hashlib.sha256("|".join(rx.pattern for rx in (N.LAB_POSITIVE, N.LAB_NEGATIVE, N.AWAITING, N.SAMPLE, N.NOT_A_RESULT)).encode()).hexdigest()
    (run_dir / "manifest.json").write_text(json.dumps({
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "link_rules_version": link.RULES_VERSION,
        "lexicon_sha256": lexicon, "inputs_sha256": inputs, "python": sys.version.split()[0]}, indent=2), encoding="utf-8")
    (ROOT / "data" / "evidence" / "LATEST").write_text(run_dir.name, encoding="utf-8")

    print(json.dumps(headline, indent=2, ensure_ascii=False))
    print("run directory:", run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
