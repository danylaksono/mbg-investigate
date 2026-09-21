# Human labelling of cause sentences

The cause lexicon (`analysis/nlp_explore.py`) decides which sentences count as a laboratory finding,
a clean result or "awaiting results". Every check of it so far was a spot-check by the assistant that
built it (METHODOLOGY section 10). This package lets people measure it.

## What is measured

| question | answered by |
|---|---|
| **Precision.** Of sentences the rules call a finding, how many are? | the `FLAG_*` strata (well measured: about 18 items, interval roughly 67-97% at 89%) |
| **Miss rate where misses are most likely.** | `UNFLAGGED_HIGH`: how many of the 178 sentences that name an agent and a lab word, yet were not flagged, are findings. Well measured (24 items). |
| **Recall, overall.** | `UNFLAGGED_STRONG` extrapolated to its 2,579 sentences. **Not well measured**: see below. |
| Does the weak vocabulary hide findings? | `UNFLAGGED_WEAK`, a diagnostic only |
| Is the codebook clear? | agreement between two labellers (Cohen's kappa) |
| Is a labeller consistent? | four sentences appear twice under different ids |

Unit: the sentence, with the sentence before and after. Frame: sentences from articles linked to an
incident, from the latest evidence run, that contain cause vocabulary. Recall here is therefore
**sentence-level, within that frame**. It is not the share of events whose cause was found.

## The sample (84 items)

| stratum | pool | drawn | what it is |
|---|---|---|---|
| FLAG_CONTAMINATION | 136 | 18 | rules say lab finding |
| FLAG_CLEAN | 11 | 9 | rules say lab result clean (2 of the 11 fall to the one-per-article / two-per-event rules) |
| FLAG_AWAITING | 752 | 5 | rules say awaiting a result |
| UNFLAGGED_HIGH | 178 | 24 | not flagged, but names an agent (bacteria, nitrite ...) **and** a lab word |
| UNFLAGGED_STRONG | 2,579 | 14 | not flagged, other strong cause vocabulary |
| UNFLAGGED_WEAK | 8,757 | 8 | not flagged, only weak cue words |

At most one sentence per article and two per event; near-duplicate (syndicated) sentences dropped;
seed 20260920 (`--seed` changes it); pool sizes and quotas are in `data/labelling/sampling_frame.json`.

**Why the unflagged pool is split.** A first design drew 12 sentences from one pool of about 8,800
"unflagged, weak cue" sentences. One stray finding in 12 would move the recall estimate from 20% to
100%, so the number would mean nothing. Missed findings are concentrated where an agent and a lab word
appear together (the HIGH pool, 178 sentences, 24 drawn), so that is where the sample goes. The weak pool is reported with a worst
case and never extrapolated.

**What 84 items cannot do: pin down overall recall.** Recall is `flagged / (flagged + missed)`, and the
"missed" term is dominated by the 2,579-sentence STRONG pool. With 14 items drawn and a true miss rate
of about 5% (the earlier 5-of-60 audit found 8%), the standard error of that rate is 0.058, so
`2,579 x 0.058` is about 150 missed sentences against roughly 120 flagged. To bring it to a 0.02
standard error would take about 120 STRONG items, more than the whole sheet. Moving the 84 items
between strata does not fix this. Simulated labels put through the scorer gave a recall interval of
about 9-94%.

So the report leads with a **worst case** (each miss rate at its upper 95% limit) next to the point
estimate, and says "too wide to count as a measurement" when the range spans more than 30 points.
It never reports "no misses found" as 100%: with zero misses the bootstrap has no spread, and the
worst case does. **Treat the HIGH miss rate, the precision, and the list of missed sentences as the
results; treat the recall figure as a bound.** A tighter recall needs about 120 more STRONG items, or
an event-level design (for each of a sample of events, does any article say a cause the rules missed?),
which is a different and probably more useful question.

## Running it

1. `python analysis/make_label_sample.py` writes to `data/labelling/`:
   - `labelling_sheet.xlsx` (Instructions tab, Items tab with dropdowns) and `labelling_sheet.csv`
   - `labelling_key_PRIVATE.csv`: stratum and the rules' decision per item
   - `sampling_frame.json`
2. **Send only `labelling_sheet.xlsx`.** The sheet is blind: it carries no regex output and no stratum.
   Do not send the key, and do not paste the rules' decisions into the conversation, because a labeller
   who knows what the machine said will anchor on it.
3. Two labellers each fill their **own copy**, independently (about 30-40 minutes). Save as, for example,
   `labelled_A.xlsx` and `labelled_B.xlsx` in `data/labelling/`.
4. `python analysis/score_labels.py data/labelling/labelled_A.xlsx data/labelling/labelled_B.xlsx`
   (one file also works, without the agreement section). It refuses to score while any row has a missing or invalid
   answer and lists them. Output goes to `data/labelling/results/<UTC>/`: `report.md`, `scored_*.csv`,
   `disagreements.csv`.

## Reading the report

- Every proportion has a **Wilson 95% interval**; the recall estimate has a bootstrap interval and a
  worst case. With 5-24 items per stratum the intervals are wide (18 flagged items with 16 agreeing is
  89%, interval about 67-97%). Read the intervals, not the point estimates. Intervals are not narrowed
  for small pools (they stay slightly too wide).
- A **miss** means the same thing everywhere in the report: an unflagged sentence the labeller calls a
  `LAB_FINDING` about the linked event. A missed `LAB_CLEAN` is listed under "misses" (so the lexicon can learn it)
  but is not counted in the finding recall.
- A finding counts as a positive only when the labeller also says it is about the **linked event**
  (`YES`). A real lab result about another incident is a linker problem, not a lexicon problem.
- "Misses" lists the unflagged sentences a labeller called findings. **Read them before touching the
  lexicon.** Change the rules only after scoring, and keep the earlier evidence run for comparison.
- Kappa below about 0.6 means the codebook is ambiguous for these items. Discuss `disagreements.csv`
  before trusting either labeller's numbers.

## Limits

- **Not a pristine held-out set.** The lexicon was tuned on sentences read in earlier audits, and some
  may be drawn again. The scores are an upper bound on what a fresh sample would show.
- Two labellers, 84 items. Precision and the HIGH-pool miss rate are measured; overall recall is bounded, not measured.
- The repeated items sit 16-48 rows from their originals, and four is a smoke test for a labeller's
  consistency, not an estimate.
- The sample is frozen against evidence run `20260919T232808Z`. If the lexicon or linker changes, the
  strata in the key no longer describe what the current rules would decide; rebuild the sample instead of
  re-scoring an old one.
- Cause findings are attributed to whoever the article names (METHODOLOGY section 9); this audit does
  not test attribution.
