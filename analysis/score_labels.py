"""Score human labels against the lexicon's decisions.

    python analysis/score_labels.py data/labelling/labelled_A.xlsx [data/labelling/labelled_B.xlsx]

Reads completed labelling sheets (xlsx or csv), joins them to the PRIVATE key, and reports:

  precision   of sentences the rules flagged as a finding / clean / awaiting, how many the labeller agrees
  miss rate   of sentences the rules did NOT flag, how many the labeller says are lab findings, by stratum
  recall      an estimate over the strong-vocabulary frame, weighting each stratum by its pool size
  agreement   between two labellers (Cohen's kappa) and between a labeller's repeated items
  misses      the unflagged sentences a labeller called lab findings, to improve the lexicon

Every proportion carries a Wilson 95% interval and the recall estimate a bootstrap interval, because the sample
is small (about 80 items). Read the intervals, not the point estimates.

Scope: sentence level, inside the sampling frame (strong cause vocabulary). It is not event-level recall, and the
lexicon was tuned on sentences read in earlier audits, so this is not a pristine held-out set.
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "data" / "labelling"
VALID_LABELS = {"LAB_FINDING", "LAB_CLEAN", "AWAITING", "CAUSE_SPECULATION", "NONE"}
VALID_ABOUT = {"YES", "NO_OTHER_INCIDENT", "NO_GENERAL", "UNSURE"}
TARGET = {"FLAG_CONTAMINATION": "LAB_FINDING", "FLAG_CLEAN": "LAB_CLEAN", "FLAG_AWAITING": "AWAITING"}
UNFLAGGED = ("UNFLAGGED_HIGH", "UNFLAGGED_STRONG", "UNFLAGGED_WEAK")
# Only these unflagged strata feed the recall estimate. The weak stratum's pool is so large (thousands of
# sentences) that a single stray positive in a sample of a few would swing the estimate from 20% to 100%, so
# it is reported with its worst case instead of being extrapolated.
EXTRAPOLATED = ("UNFLAGGED_HIGH", "UNFLAGGED_STRONG")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def wilson_or_census(k: int, n: int, pool: int | None) -> tuple[float, float]:
    """Wilson interval, or the bare proportion when the sample is the whole pool. No finite-population correction
    otherwise: it does not combine cleanly with Wilson at 0% or 100% (the corrected interval can exclude its own point
    estimate), so a sample that is most of a small pool keeps a conservative, slightly-too-wide interval."""
    if pool and n >= pool and n > 0:
        return (k / n, k / n)
    return wilson(k, n)


def describe_recall(est: dict) -> str:
    """One sentence for the recall estimate. It leads with the WORST CASE (each unflagged stratum's miss rate at its
    Wilson upper limit), because the bootstrap interval collapses when a stratum has no misses, and says so when the
    plausible range is too wide (over 30 points) to count as a measurement."""
    lo, hi = est["recall_ci"]
    worst = est["recall_worst"]
    text = (f"Sentence-level recall over the strong-vocabulary frame: point estimate **{est['recall']:.0%}**, bootstrap 95% "
            f"interval {lo:.0%}-{hi:.0%}, worst case (upper 95% limit on every miss rate) **{worst:.0%}**; precision "
            f"{est['precision']:.0%}. About {est['true_findings_flagged']:.0f} true finding sentences flagged, "
            f"{est['true_findings_missed']:.0f} missed (worst case {est['missed_worst']:.0f}).")
    if est["recall"] - worst > 0.30 or hi - lo > 0.30:
        text += (" **The range is too wide to count as a measurement.** The missed-finding count is dominated by the "
                 "large not-flagged pool, and a sample of this size cannot pin its rate down. Rely on the per-stratum miss "
                 "rates and the listed misses, not on this figure.")
    return text


def cohen_kappa(a: list, b: list) -> float:
    """Cohen's kappa for two raters over the same items. 1 = perfect, 0 = chance, <0 = worse than chance."""
    n = len(a)
    if n == 0:
        return float("nan")
    observed = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum((ca[c] / n) * (cb[c] / n) for c in set(a) | set(b))
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def load_labels(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="Items", dtype=str) if str(path).endswith(".xlsx") else pd.read_csv(path, dtype=str)
    df = df.rename(columns={"about this event?": "about"})
    for col in ("label", "about"):
        df[col] = df[col].fillna("").str.strip().str.upper()
    return df[["item_id", "label", "about", "notes"] + (["SENTENCE TO LABEL"] if "SENTENCE TO LABEL" in df else [])]


def validate(df: pd.DataFrame, name: str) -> list[str]:
    problems = []
    for col, valid in (("label", VALID_LABELS), ("about", VALID_ABOUT)):
        empty = df[df[col] == ""].item_id.tolist()
        bad = df[(df[col] != "") & ~df[col].isin(valid)]
        if empty:
            problems.append(f"{name}: {len(empty)} rows have no {col}: {empty[:6]}")
        if len(bad):
            problems.append(f"{name}: invalid {col} values {sorted(set(bad[col]))} in {bad.item_id.tolist()[:6]}")
    return problems


def is_positive(row, target: str, strict: bool = True) -> bool:
    """Human agrees the sentence is `target`, and that it is about the linked event (UNSURE counts unless strict)."""
    return row["label"] == target and (row["about"] == "YES" or (not strict and row["about"] == "UNSURE"))


def stratum_table(scored: pd.DataFrame, labeller: str, pools: dict | None = None) -> pd.DataFrame:
    """One row per stratum. A MISS is an unflagged sentence the labeller calls a LAB_FINDING about the linked event,
    the same definition estimate_recall uses, so the two reconcile."""
    pools = pools or {}
    rows = []
    for stratum in list(TARGET) + list(UNFLAGGED):
        part = scored[scored.stratum == stratum]
        part = part[part.repeat_of_item.isna()]
        if part.empty:
            continue
        if stratum in TARGET:
            k = int(sum(is_positive(r, TARGET[stratum]) for _, r in part.iterrows()))
            what = f"agrees it is {TARGET[stratum]} about this event"
        else:
            k = int(sum(is_positive(r, "LAB_FINDING") for _, r in part.iterrows()))
            what = "is a LAB_FINDING about this event (a MISS)"
        lo, hi = wilson_or_census(k, len(part), pools.get(stratum))
        rows.append({"labeller": labeller, "stratum": stratum, "pool": pools.get(stratum), "n": len(part), "k": k, "rate": k / len(part),
                     "ci_low": lo, "ci_high": hi, "meaning": what})
    return pd.DataFrame(rows)


def md_table(df: pd.DataFrame) -> str:
    """A markdown table without the optional `tabulate` dependency."""
    head = "| " + " | ".join(map(str, df.columns)) + " |"
    rule = "|" + "|".join("---" for _ in df.columns) + "|"
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, rule, *body])


def estimate_recall(scored: pd.DataFrame, frame: dict, draws: int = 2000, seed: int = 1) -> dict:
    """Sentence-level precision and recall of the lab-finding rule over the whole frame, by weighting each stratum
    by its pool size. Recall = TP / (TP + FN): TP from the flagged-finding pool, FN from the unflagged pools."""
    pools = frame["pool_sizes"]
    part = scored[scored.repeat_of_item.isna()]
    flagged = part[part.stratum == "FLAG_CONTAMINATION"]
    pos_flag = [is_positive(r, "LAB_FINDING") for _, r in flagged.iterrows()]
    pos_unflag = {s: [is_positive(r, "LAB_FINDING") for _, r in part[part.stratum == s].iterrows()] for s in EXTRAPOLATED}
    if not pos_flag or any(not v for v in pos_unflag.values()):
        return {}
    rng = random.Random(seed)

    def one(flag_sample, unflag_samples):
        tp = pools["FLAG_CONTAMINATION"] * sum(flag_sample) / len(flag_sample)
        fn = sum(pools[s] * sum(v) / len(v) for s, v in unflag_samples.items())
        return tp, fn, (tp / (tp + fn) if tp + fn else float("nan"))

    tp, fn, recall = one(pos_flag, pos_unflag)
    boots = []
    for _ in range(draws):
        fs = [rng.choice(pos_flag) for _ in pos_flag]
        us = {s: [rng.choice(v) for _ in v] for s, v in pos_unflag.items()}
        boots.append(one(fs, us))
    rec = sorted(b[2] for b in boots if not math.isnan(b[2]))
    fns = sorted(b[1] for b in boots)
    fn_worst = sum(pools[s] * wilson(sum(v), len(v))[1] for s, v in pos_unflag.items())
    return {"true_findings_flagged": tp, "true_findings_missed": fn, "recall": recall,
            "recall_worst": tp / (tp + fn_worst) if tp + fn_worst else float("nan"), "missed_worst": fn_worst,
            "recall_ci": (rec[int(0.025 * len(rec))], rec[int(0.975 * len(rec)) - 1]),
            "missed_ci": (fns[int(0.025 * len(fns))], fns[int(0.975 * len(fns)) - 1]),
            "precision": sum(pos_flag) / len(pos_flag)}


def main(argv=None) -> int:
    paths = [Path(a) for a in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        print(__doc__)
        return 2
    key = pd.read_csv(LAB / "labelling_key_PRIVATE.csv")
    frame = json.loads((LAB / "sampling_frame.json").read_text(encoding="utf-8"))
    labels = {p.stem: load_labels(p) for p in paths}
    problems = [m for name, df in labels.items() for m in validate(df, name)]
    if problems:
        print("Cannot score yet:\n  " + "\n  ".join(problems))
        return 1

    out_dir = LAB / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# Human-label scoring", "", f"Sampling frame: evidence run `{frame['evidence_run']}`, seed {frame['seed']}, {frame['items']} items "
             f"(including {frame['repeats']} repeats). Pool sizes: {frame['pool_sizes']}.", ""]
    tables, scored_by = [], {}
    for name, df in labels.items():
        scored = key.merge(df, on="item_id")
        scored_by[name] = scored
        scored.to_csv(out_dir / f"scored_{name}.csv", index=False, encoding="utf-8")
        t = stratum_table(scored, name, frame["pool_sizes"])
        tables.append(t)
        lines += [f"## {name}", "", "Proportions with Wilson 95% intervals. For FLAG strata, `rate` is precision; for UNFLAGGED strata it "
                  "is the share of sentences the rules did not flag that this labeller calls a lab finding about the event.", ""]
        lines.append(md_table(t.assign(rate=t.rate.map("{:.0%}".format), ci=t.apply(lambda r: f"{r.ci_low:.0%}-{r.ci_high:.0%}", axis=1))
                              [["stratum", "pool", "n", "k", "rate", "ci", "meaning"]]))
        est = estimate_recall(scored, frame)
        if est:
            lines += ["", describe_recall(est), ""]
        weak = scored[(scored.stratum == "UNFLAGGED_WEAK") & scored.repeat_of_item.isna()]
        if len(weak):
            wk = int(sum(is_positive(r, "LAB_FINDING") for _, r in weak.iterrows()))
            wlo, whi = wilson(wk, len(weak))
            lines += ["", f"Weak-cue stratum (a diagnostic, NOT in the estimate above): {wk} of {len(weak)} were findings "
                      f"(95% {wlo:.0%}-{whi:.0%}). Its pool is {frame['pool_sizes']['UNFLAGGED_WEAK']} sentences, so the worst "
                      f"case is about {whi * frame['pool_sizes']['UNFLAGGED_WEAK']:.0f} more missed sentences; a rate near zero "
                      f"means findings phrased without the strong vocabulary are rare."]
        rep = scored[scored.repeat_of_item.notna()]
        if len(rep):
            first = scored.set_index("item_id")
            same = sum(first.loc[r.repeat_of_item, "label"] == r.label for _, r in rep.iterrows())
            lines.append(f"Repeated items labelled the same way: {same} of {len(rep)}.")
        misses = scored[(scored.stratum.isin(UNFLAGGED)) & scored.apply(lambda r: is_positive(r, "LAB_FINDING") or is_positive(r, "LAB_CLEAN"), axis=1)]
        # findings feed the recall estimate; a missed clean result is listed too, since the lexicon should learn it
        if len(misses):
            lines += ["", f"Sentences {name} called lab findings or clean results that the rules missed (to improve the lexicon):", ""]
            lines += [f"- `{r.item_id}` ({r.stratum}, {r.label}): {r['SENTENCE TO LABEL'] if 'SENTENCE TO LABEL' in r and isinstance(r['SENTENCE TO LABEL'], str) else ''}"
                      for _, r in misses.iterrows()]
        lines.append("")
    if len(labels) == 2:
        (na, a), (nb, b) = list(labels.items())
        m = a.merge(b, on="item_id", suffixes=("_a", "_b"))
        binary = lambda s: ["FINDING" if x in ("LAB_FINDING", "LAB_CLEAN") else "OTHER" for x in s]
        lines += [f"## Agreement between {na} and {nb}", "",
                  f"- label, five classes: kappa {cohen_kappa(m.label_a.tolist(), m.label_b.tolist()):.2f} "
                  f"(agree on {(m.label_a == m.label_b).mean():.0%} of {len(m)} items)",
                  f"- finding vs anything else: kappa {cohen_kappa(binary(m.label_a), binary(m.label_b)):.2f}",
                  f"- about this event: kappa {cohen_kappa(m.about_a.tolist(), m.about_b.tolist()):.2f}", "",
                  "Kappa below about 0.6 means the codebook is ambiguous for these items, and the disagreements should be "
                  "discussed before any labeller's numbers are trusted.", ""]
        m[m.label_a != m.label_b][["item_id", "label_a", "label_b", "about_a", "about_b"]].to_csv(out_dir / "disagreements.csv", index=False)
    report = "\n".join(lines)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print("\nwritten to", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
