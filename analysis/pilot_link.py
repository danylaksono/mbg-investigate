"""Pilot: how much cause information do independently discovered headlines add?

    python analysis/pilot_link.py [--outlet detik] [--show 25]

Links each discovered headline (title + listed date) to events using the conservative rule in
`mbgpipe.link`, then reports at EVENT level: how many events gain any headline, how many gain
a headline with a cause cue, and how many of those were unresolved on the Wikipedia-cited
article alone. Headlines only: precision of the cause cue is checked by reading (--show), and
full-text linking comes after this pilot shows the yield is worth it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nlp_explore as N  # noqa: E402
from mbgpipe import link  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# A headline that reports a finding, not merely an incident. Coarse on purpose: read the
# sample (--show) before believing the count.
CAUSE_CUE = re.compile(
    r"\b(?:bakteri|e\.?\s?-?coli|salmonella|staphylo\w*|nitrit|histamin\w*|positif|mengandung|kontaminasi|terkontaminasi|"
    r"pemicu|penyebab|biang|terungkap|terbukti|dipastikan|hasil\s+(?:lab|uji|pemeriksaan|laboratorium)|"
    r"basi|berulat|mentah)\b", re.I)
# Headlines that ask for or await a cause rather than report one.
NOT_YET = re.compile(r"\b(?:menunggu|menanti|tunggu|belum|diselidiki|dugaan|diduga|selidiki|telusuri|menelusuri|usut|diuji|cari\s+tahu)\b", re.I)

STATE_RANK = {"lab: contamination reported": 4, "lab: result reported clean": 3,
              "awaiting lab / cause unknown": 2, "samples taken, no result stated": 1,
              "no cause information": 0}


def event_states() -> dict[str, str]:
    """Best epistemic state per event across the Wikipedia-cited articles."""
    docs, inc, ev, _ = N.load()
    docs = N.attach_events(docs, inc, ev)
    best: dict[str, str] = {}
    for _, d in docs.iterrows():
        state = N.certainty(d["text"])
        for eid in d["event_ids"]:
            if eid not in best or STATE_RANK[state] > STATE_RANK[best[eid]]:
                best[eid] = state
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outlet", default="detik")
    ap.add_argument("--show", type=int, default=0, help="print N linked cause-cue headlines to judge precision")
    args = ap.parse_args(argv)

    events = pd.read_csv(ROOT / "data/processed/events.csv")
    events["venues"] = events.venues.apply(json.loads)
    keys = link.event_keys(events.to_dict("records"))
    rows = [json.loads(l) for l in open(ROOT / f"data/interim/discovered/{args.outlet}.tag.jsonl", encoding="utf-8")]

    out = []
    for r in rows:
        when = date.fromisoformat(r["listed_date"]) if r["listed_date"] else None
        decision, ids = link.link(r["title"], when, keys)
        cue = bool(CAUSE_CUE.search(r["title"])) and not NOT_YET.search(r["title"])
        out.append({**r, "decision": decision, "event_ids": ids, "cause_cue": cue})
    df = pd.DataFrame(out)
    df.to_csv(ROOT / f"data/interim/discovered/{args.outlet}.links.csv", index=False, encoding="utf-8")

    states = event_states()
    ev_state = {e: states.get(e, "no cited article read") for e in events.event_id}
    linked = df[df.decision == "linked"].copy()
    linked["event_id"] = linked.event_ids.str[0]
    per_event = linked.groupby("event_id").agg(headlines=("url", "size"), cause_headlines=("cause_cue", "sum"))
    per_event["state"] = per_event.index.map(ev_state)

    lo, hi = df.listed_date.min(), df.listed_date.max()
    print(f"headlines: {len(df)} ({lo} .. {hi}); undated {df.listed_date.isna().sum()}")
    print("decisions:", dict(df.decision.value_counts()))
    print(f"events with any linked headline: {len(per_event)} of {len(events)}"
          f" (events dated inside the feed's span: {int(events.date_start.between(lo, hi).sum())})")
    print(f"events with a cause-cue headline: {int((per_event.cause_headlines > 0).sum())}")
    span = events[events.date_start.between(lo, hi)]
    print("\nOf events inside the feed's span, by what the Wikipedia-cited articles alone gave:")
    tab = pd.DataFrame({"events": span.event_id.map(ev_state).value_counts()})
    tab["gain a headline"] = span.event_id.map(lambda e: e in per_event.index).groupby(span.event_id.map(ev_state)).sum()
    tab["gain a cause-cue headline"] = span.event_id.map(
        lambda e: e in per_event.index and per_event.loc[e, "cause_headlines"] > 0).groupby(span.event_id.map(ev_state)).sum()
    print(tab.to_string())

    if args.show:
        print("\nLinked headlines with a cause cue (judge precision by reading):")
        ex = linked[linked.cause_cue].sample(min(args.show, int(linked.cause_cue.sum())), random_state=3)
        for _, r in ex.iterrows():
            print(f"  [{r.listed_date}] {r.title[:110]}  -> {r.event_id.split('#')[1]}")
    print("\nambiguous examples:")
    for _, r in df[df.decision == "ambiguous"].head(5).iterrows():
        print(f"  {r.title[:90]} -> {len(r.event_ids)} events")
    print("\nhow many headlines per event:", dict(Counter(per_event.headlines).most_common(6)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
