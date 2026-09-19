# mbgpipe

Builds two linked datasets on mass food poisoning linked to Indonesia's Makan Bergizi
Gratis (MBG) programme, from one curated source: the Indonesia table on
[Daftar kasus keracunan massal makan siang gratis](https://id.wikipedia.org/wiki/Daftar_kasus_keracunan_massal_makan_siang_gratis#Indonesia)
(id.wikipedia).

- a **structured incident table**: date, province, kabupaten/kota, venue, victims, deaths, sources
- a **text corpus** of the news articles the table cites, de-syndicated, each linked back to its incident rows

Each stage reads the previous stage's files off disk, so any one can be re-run or replaced
alone. **How the data was built, what was checked and how far to trust it:
[docs/METHODOLOGY.md](docs/METHODOLOGY.md).**

```
1 fetch      MediaWiki API          -> data/raw/*.wikitext, *.meta.json      (revision id recorded)
2 parse      wikitext               -> data/interim/*.rows.jsonl             (table rows)
                                       data/interim/*.citations.jsonl        (every <ref>)
3 normalize  rows + citations       -> data/processed/incidents.csv          (one row per table row)
                                       data/processed/events.csv             (one row per count cell)
                                       data/processed/qc_report.json
4 articles   citations              -> data/raw/articles/*.html.gz + *.txt
                                       data/interim/fetch_ledger.jsonl       (every URL, incl. failures)
5 corpus     ledger + incidents     -> data/processed/corpus.jsonl
6 discover   outlet topic feed      -> data/interim/discovered/*.tag.jsonl   (headline, url, listed date)
7 link       headlines + events     -> data/interim/discovered/*.links.csv   (analysis/pilot_link.py)
```

Stages 1-5 build the two datasets from the page. Stages 6-7 are a pilot for finding coverage
the page does not cite.

## Run

```bash
python -m venv .venv && .venv/Scripts/activate      # Python 3.11+
pip install -e ".[dev]"
export MBG_USER_AGENT="mbg-research/0.2 (you@example.org)"   # optional, but polite

python -m mbgpipe fetch
python -m mbgpipe parse
python -m mbgpipe normalize
python -m mbgpipe articles          # slow: ~440 URLs, 2 s per host, archive.org 6 s. Resumable.
python -m mbgpipe corpus

pytest -q                            # offline
```

## Layout

```
src/mbgpipe/
  fetch.py  parse.py  normalize.py  articles.py  corpus.py  cli.py   one module per stage
  discover.py  link.py            stage 6 (feed walking) and 7 (offline linking)
  wikitable.py                    wikitable -> grid, rowspan-aware
  extract/{counts,dates,places}.py  cell-level parsers
  config.py                       User-Agent
analysis/                         nlp_explore.py, pilot_link.py, build_kode_wilayah.py
tests/                            offline; tests/fixtures/sample_wikitext.txt is real page wikitext
data/{raw,interim,processed,reference}/   reference/ holds the kode wilayah list
```

## The one thing to know before analysing incidents.csv

The source table merges cells with `rowspan`. When two venues share one count, the count is
written once and **both rows display it**. Summing the displayed column counts it twice.

| column | meaning |
|---|---|
| `symptomatic` | what the table shows on this row (carried down) |
| `symptomatic_counted` | the same figure on the **first** row of its cell only, null elsewhere |

**Sum `symptomatic_counted`, never `symptomatic`.** On the full table the naive sum is ~3x
the correct one. `events.csv` is the same data collapsed to one row per source count cell, so
`events.symptomatic` sums correctly too. `deaths` / `deaths_counted` work the same way.

## Output schema

`incidents.csv`, one row per rendered table row. List and dict columns are JSON.

| field | notes |
|---|---|
| `date_iso`, `date_end_iso`, `date_precision`, `date_raw` | precision `day` or `month`. A month-only date is never padded to a fake day. "7 s/d 9 Mei 2025" is a range (`date_end_iso` differs) |
| `province`, `kabkota`, `kabkota_type`, `kabkota_name` | from the table's own wikilinks, so `Kabupaten Bandung` and `Kota Bandung` stay apart |
| `kabkota_kode`, `provinsi_kode`, `kabkota_lat`, `kabkota_lng`, `kabkota_population`, `kabkota_area_km2` | Kemendagri code and centroid: the join keys for maps. `kabkota_kode_match` is `exact` or `alias` |
| `venue`, `venue_level`, `venue_raw` | `venue` is null for "TBA / Spesifik tidak disebutkan"; `venue_level` (primary, junior_secondary, pesantren...) is kept where the raw text gives one |
| `symptomatic_raw`, `symptomatic_kind` | `exact` / `approx` (±) / `lower_bound` (>) / `vague` / `unreported` |
| `symptomatic`, `symptomatic_min`, `symptomatic_max` | vague quantifiers ("Ratusan") get bounds and a **null** point estimate; `lower_bound` has a null max |
| `symptomatic_by_subject` | `{"student": 45, "teacher": 3}`. Populations within one cell are disjoint and summed |
| `deaths` | blank on the page means *not reported*, not zero. Only 2 deaths are recorded in the whole table: do not read this column as mortality |
| `event_id` | rows sharing one source count cell |
| `source_urls`, `ref_ids` | archive URL preferred over live URL |
| `flags` | QC review queue |

Flags: `no_date`, `month_precision_only`, `date_range`, `no_venue`, `count_unreported`,
`count_vague`, `unnumbered_subject`, `count_shared_across_rows`, `no_source_url`,
`unknown_province`, `kabkota_unlinked`, `kabkota_no_kode`, `kabkota_province_mismatch`.

`corpus.jsonl`, one row per fetched article: `text`, `title`, `domain`, `publish_date`,
`incident_ids` (join to `incidents.incident_id`), `cluster_id`, `is_canonical`, `cluster_size`,
`duplicate_of`, `mentions_incident_place`. Analyse the canonical documents; use `cluster_size`
when you want reach. Duplicates are kept, not deleted.

## Exploratory NLP

```bash
pip install -e ".[analysis]"
python analysis/nlp_explore.py               # writes data/processed/nlp/report.md and CSVs
python analysis/nlp_explore.py --examples 8  # print matched snippets to check lexicon precision
```

Lexicon coding by regex over the 308 documents: how reports describe cause (lab result vs
awaiting vs nothing), mechanisms, foods, symptoms, institutional response, agreement between
each article's victim count and the table, and reporting lag. Group comparisons use only
documents linked to exactly one event and report the readable-article rate for the group, and
the report says which differences survive a Bonferroni correction. There is no topic model:
308 documents on one subject are too few for one to separate anything. Re-run it after the
crawl is retried.

## Independent discovery (pilot)

The cited articles are mostly first-day reports, so they say little about cause. Stage 6
walks an outlet's topic feed (a bounded stream of exactly this subject) instead of searching
once per event, and stage 7 links headlines to events offline:

```bash
python -m mbgpipe discover --outlet detik        # ~106 pages, ~8 min, resumable
python analysis/pilot_link.py --show 25          # event-level yield + headlines to read
```

`link.py` links an article to an event only if it names the event's kabupaten/kota or venue
and its date falls within 1 day before to 30 days after the incident. A named venue outranks a
bare place name. An article matching two or more events is `ambiguous` and never assigned to
the nearest one.

Full-text step, all five outlets (detik, Antara, Kompas, CNN Indonesia, Liputan6): 2,853 discovered
articles fetched (2,830 read; the other 23 are detik photo galleries, which are captions only) at
8 s plus up to 50% jitter per request, with no host returning a 403 or 429.
Kompas is restricted to headline-linked and ambiguous items (1,127 of its 2,288 headlines).
```bash
python -m mbgpipe discover --outlet <outlet>              # walk the topic feed, keep raw pages
python analysis/pilot_link.py --outlet <outlet>           # link headlines to events
python analysis/make_fetch_targets.py --outlet <outlet>   # priority list (--include linked,ambiguous for big feeds)
python -m mbgpipe articles --targets data/interim/discovered/<outlet>.fetch_targets.jsonl \
    --ledger data/interim/discovered/<outlet>.fetch_ledger.jsonl --cache data/raw/articles_discovered \
    --delay 8 --jitter 0.5 --no-wayback
python analysis/build_evidence.py                         # link, extract findings, write an evidence run
```
Headline or lead paragraph naming the venue or place links an article; a place named only deep
in the body is `weak` and not linked. Result, per event, over all 459 events (evidence run
`20260919T201142Z`; its `summary.json` is authoritative): events with no article at all fell from
129 to 60; 246 events gained at least one independently found article (1,864 in all); and
**events whose articles report a laboratory result went from 10 to 51 (11% of events), a median 9
days after the incident**. Agents named: unspecified bacteria (38 events), E. coli (21), Bacillus
(7), Salmonella (6), Staphylococcus (3), nitrite (2), histamine (1), other chemical (1). 176 events
still read "awaiting lab results" after every article.

Read this as "a result was reported **within 30 days**": linking accepts articles only up to 30
days after the incident, so the lag distribution (maximum 27 days) is cut off at 30 by
construction, and results published later are not counted. It is also coverage-limited (five
outlets, no tribunnews or Jawapos group) and regex-coded with recall checked only by spot-check,
so it is a floor, not a rate.

Reading the findings by hand caught false positives at each stage: a headcount matched by the word
"ditemukan", an inspection's "risk factors", negligence, and a generic explainer ("nitrat yang
dipicu bakteri pengurai dapat menyebabkan keracunan"). Each was fixed and pinned in
`tests/test_nlp_lexicon.py`; the earlier evidence runs are kept to show the difference.

Tempo needs JavaScript and Beritasatu blocks crawlers, so both are excluded.

### Retrying failures

The machine sleeps during long crawls, which shows up as `network_error` rows (DNS lookups failing,
reset connections). After any crawl, rerun the same `articles` command: it retries every URL that is
not `ok` and skips the rest. Do this per ledger, one host at a time, and then rebuild the evidence:

```bash
python -m mbgpipe articles --targets data/interim/discovered/<outlet>.fetch_targets.jsonl \
    --ledger data/interim/discovered/<outlet>.fetch_ledger.jsonl --cache data/raw/articles_discovered \
    --delay 8 --jitter 0.5 --no-wayback
python analysis/build_evidence.py
```

For the Wikipedia-cited set, rerun `python -m mbgpipe articles` (add `--delay 8 --jitter 0.5`),
which also retries the 403s through the Wayback Machine once archive.org stops rate-limiting.

### Evidence trail

`analysis/build_evidence.py` writes a NEW directory under `data/evidence/runs/<UTC time>/` each
time and never overwrites an earlier one; `data/evidence/LATEST` names the newest.

| file | holds |
|---|---|
| `manifest.json` | sha256 of every input, link-rule version, lexicon hash |
| `articles.jsonl` | every article considered: source(s), date and where it came from, fetch status, sha256 of the raw page, cache path |
| `link_decisions.jsonl` | per article: decision, tier, the terms that matched, candidate events, and for Wikipedia-cited articles whether the text supports the cited row |
| `findings.jsonl` | quoted sentences (lab contamination, lab clean, awaiting, mechanisms, kitchen closures) with character offset in the cached text and the event they were linked to |
| `event_summary.csv` | per event: best cause state from cited articles alone and with discovery, first finding date, lag, agents |

Raw pages live in `data/raw/articles*/` (gzipped HTML plus extracted text) and their hashes in the
fetch ledgers, so every quoted sentence can be checked against the bytes it came from.

### Auditing the Wikipedia citations

The same linking checks each cited article against the row that cites it. Of 308: 239 agree,
42 are ambiguous but include the cited event, 10 are the right place with a date error or another
event in the same regency, 7 are undated, and 10 never name the cited place in the headline or
lead. Of those 10, about five look genuinely wrong (an Aceh Timur row citing a story about
Cianjur; Salatiga citing Karanganyar; Jakarta Selatan, Lampung Selatan and Metro citing
provincial statements), four are national roundups or statements that cannot corroborate one
incident, and one is a miss of the abbreviation "Pangkep". This is a review queue, not a verdict.

## Design decisions worth knowing

**The source is a table, so it is parsed as one.** `wikitable.py` resolves rowspans and records
each value's origin cell. It is strict: if the header changes or a `||` appears, it raises
rather than misaligning columns. The page is live and edited.

**No event clustering by proximity.** The table's rowspans already say which rows describe one
incident. Merging by place-and-date window would fuse a school hit two days running.

**Wikitext, not rendered HTML or markitdown.** Rendered pages collapse citations to `[1][2]`,
losing the `{{Cite web}}` fields (url, title, work, date, archive-url). Indonesian Wikipedia
mixes English and Indonesian parameter names in one article; `parse.PARAM_ALIASES` folds both.

**Stage 4 is polite and never loses work.** One request at a time per host, robots.txt
honoured, `MBG_USER_AGENT` sent. A robots disallow or a dead link routes to the Wayback
Machine, picking the snapshot nearest the citation's date. Every URL lands in the ledger
(`ok`, `paywalled`, `short_body`, `empty`, `robots_disallowed`, `http_error`, `network_error`,
`no_archive`, `skipped_domain`), written as each URL finishes. Social platforms are skipped and
recorded. Failed URLs are retried on the next run; `ok` ones are not.

**De-syndication.** Exact matches by normalised hash, near matches by Jaccard over word
5-grams, candidate pairs found through an inverted index. Shingles in over 35% of documents
are treated as site furniture. The canonical is the earliest-dated, falling back to the longest.

## Known limitations

**Wikipedia is the only source, and it is a secondary one.** The table inherits media
attention bias (Java is over-covered relative to eastern Indonesia) and editors' errors.
Two examples found while building this: the page's own `TOTAL` row (about 11,390) stopped
being updated around 1 Oct 2025 while the table kept growing; and at least one row cites an
article about a different regency. `qc_report.json` shows the date the rowspan-aware running
total reaches the page's `TOTAL`, which is how the first was found. That check exposes the
editors' figure as stale; it does not validate the parse.

**The corpus is a sample of coverage, not of incidents.** It contains what Wikipedia editors
chose to cite. The first full run (revision 29900932) read 308 of 425 cited URLs (72%), which
covers 601 of 842 table rows and 330 of 459 events. Of the 117 failures, 67 are HTTP 403 from
publishers that block crawlers (tribunnews.com alone is 43, the Jawapos group 15), 16 are
JS-rendered pages that extract to nothing, 11 are dead links, 6 are Instagram (skipped by
design). The Wayback fallback recovered none in that run because archive.org rate-limited the
crawl IP (429); `python -m mbgpipe articles` retries only the failures, so rerun it later. That
hole is not random, so read `fetch_ledger.jsonl` before making claims about outlets or regions.

**De-syndication found nothing to merge on this corpus.** The most similar pair of documents
has Jaccard 0.16 against a 0.70 threshold: each table row cites its own story, and no wire copy
is cited twice. The stage is kept because it will matter if you add sources beyond this page.

**`mentions_incident_place` is a review queue, not a verdict.** It flagged 10 of 308 documents.
A few are real wrong-source rows (an Aceh Timur row citing a Cianjur story; a Salatiga row citing
Karanganyar); others are spelling variants (Deli Serdang vs "Deliserdang", Pangkajene dan
Kepulauan vs "Pangkep") or a multi-region roundup.

**Region codes are Kemendagri, from a third-party dump.** `kabkota_kode` (e.g. `11.08`) comes
from `data/reference/kabkota_kemendagri.csv`, built by `analysis/build_kode_wilayah.py` from
github.com/cahyadsn/wilayah (MIT), which states it follows Kepmendagri No 300.2.2-2138/2025.
It has the expected 38 provinces and 514 kabupaten/kota, but it is not the official file, and
it has at least one error (code 16.02 is named "Ogan Komering" instead of "Ogan Komering
Ilir"). Kemendagri codes differ from BPS codes for some regions. Names are joined exactly;
six variants on the source page (typos such as "Bombaba", and spellings such as "Palangka
Raya") go through a reviewed alias table in `extract/places.py`, and anything else is flagged
`kabkota_no_kode` rather than fuzzy-matched. `kabkota_population` is as given in that file and
undated, so treat per-capita rates as approximate.

**Counts are victims reported as symptomatic, at the time the editor wrote the row.** They are
not later-revised totals, and one cell can mix students and teachers (see `symptomatic_by_subject`).

**Dropped from the first draft:** the `revisions` and `wp` (JPPI/WordPress) side channels were
declared but never written, and neither serves the analysis. Revision timestamps are also an
editorial-attention series, not incidence, and must never be plotted as one.
