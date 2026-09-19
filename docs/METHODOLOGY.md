# Methodology

How the MBG poisoning dataset was built, what was checked, and how far each result can be
trusted. Figures are dated; where a run was still in progress when this was written, the file
that holds the authoritative number is named.

## 1. Purpose and scope

Goal: understand the **causes** of mass food-poisoning incidents linked to Indonesia's Makan
Bergizi Gratis (MBG) programme, using one curated public source as the incident list and news
coverage as the evidence of what was found.

Two questions drive the design:

1. *What happened, where, when, to how many?* Answered from a structured incident table.
2. *What was reported about the cause, and how quickly?* Answered from news articles linked to
   those incidents.

The data describes **what was reported**. It is not an incidence estimate, not a count of every
poisoning, and not a finding about what caused any incident that no article reported on.

## 2. Principles that shaped the decisions

| principle | consequence |
|---|---|
| Nothing is coerced into a cleaner value than the source has | "Ratusan siswa" is a null point estimate with bounds 100-999, not 100. A month-only date is never padded to a day. |
| Uncertainty and provenance travel with every value | `*_raw` fields, `*_kind`, `flags`, quoted evidence sentences. |
| A wrong link is invisible in an aggregate, so be conservative | Ambiguous matches are recorded and never assigned to the nearest event. |
| Failures are data | Every URL that could not be read stays in a ledger with the reason. |
| Do not lose work, do not overwrite evidence | Append-only ledgers; each analysis run writes a new directory. |
| Be a polite guest | Robots.txt, one request at a time per host, long delays with jitter, automatic pauses. |
| Verify by reading, and say what was read | Lexicon precision and recall were checked on samples; the checks and their limits are recorded in section 9. |

## 3. Pipeline overview

```
1 fetch      MediaWiki API           -> data/raw/*.wikitext + meta.json (revision id)
2 parse      wikitext                -> data/interim/*.rows.jsonl, *.citations.jsonl
3 normalize  rows + citations        -> data/processed/incidents.csv, events.csv, qc_report.json
4 articles   citations               -> data/raw/articles/*.html.gz + .txt, fetch_ledger.jsonl
5 corpus     ledger + incidents      -> data/processed/corpus.jsonl
  (analysis) corpus + incidents      -> data/processed/nlp/report.md and CSVs
6 discover   outlet topic feeds      -> data/interim/discovered/<outlet>.tag.jsonl, raw pages in data/raw/feeds/
7 link       headlines/text + events -> data/evidence/runs/<UTC>/
```

Stages 1-5 build two linked datasets from the Wikipedia page. Stages 6-7 look for coverage the
page does not cite. Each stage reads the previous stage's files, so any one can be re-run.

## 4. Data source

The Indonesia table on id.wikipedia, *Daftar kasus keracunan massal makan siang gratis*
(<https://id.wikipedia.org/wiki/Daftar_kasus_keracunan_massal_makan_siang_gratis#Indonesia>).

- Used at **revision 29900932** (timestamp 2026-09-18T16:14:04Z). The revision id is recorded in
  `data/raw/*.meta.json`. The page is live and was edited during the work, so this is what makes
  the dataset reproducible.
- Columns: date, province, kabupaten/kota, venue, symptomatic count, deaths, reference.
- Older titles of the page ("... program Makan Bergizi Gratis") are redirects to it. Fetching
  both would write the page twice and duplicate every row, so files are named by the *resolved*
  title.
- It is a secondary source built by volunteer editors from news reports. It inherits media
  attention bias and editor error (section 11).

Other pages were considered and dropped: the English article redirects to a general programme
article, and the bullet lists for India, South Korea and China are out of scope.

## 5. Structured layer (stages 2-3)

### 5.1 Reading the table

The Indonesia section is a **wikitable**, not prose. `wikitable.py` parses it into a grid and
resolves `rowspan`/`colspan`.

- The parser is strict: it raises if the header changes or a `||` inline separator appears, so a
  future edit fails loudly instead of misaligning columns.
- Every grid cell keeps its **origin** (the row and column where the value was written).
- 846 rendered rows -> 842 incident rows (3 blank placeholder rows and the page's own `TOTAL` row
  are set aside).

### 5.2 The rowspan problem

Where two venues share one count, the table writes the count once and both rows display it.
Summing the displayed column counts it twice.

| column | meaning |
|---|---|
| `symptomatic` | what the table shows on this row (carried down) |
| `symptomatic_counted` | the figure on the **first** row of its source cell only, null elsewhere |

Summing `symptomatic_counted` gives **43,479**; summing `symptomatic` gives 129,336, an inflation
of 2.97x. **Events** are rows sharing one source count cell: 842 rows -> **459 events**.

No proximity clustering (same place within N days) is used. The table's own rowspans already say
which rows describe one incident, and a time-window rule would merge distinct incidents such as a
school hit two days running.

### 5.3 Parsing the cells

| field | rule |
|---|---|
| Counts | Indonesian thousands separator ("1.333" = 1333). Kinds: `exact`, `approx` (+-), `lower_bound` (>), `vague` (Belasan 11-19, Puluhan 20-99, Ratusan 100-999, Ribuan 1000-9999), `unreported` (TBA or blank). Vague counts have bounds and a **null** point estimate. |
| Mixed populations in one cell | "45 Siswa + 3 Guru" is 48 people: populations in one table cell are disjoint and are summed, with a per-subject breakdown. This is the opposite of prose such as "400 pelajar, 12 di antaranya dirawat", where the second figure is nested. |
| Dates | Day, range ("7 s/d 9 Mei 2025", start and end kept), month only ("Awal Februari 2026" is month precision), and "Tidak Disebutkan" (no date, flagged, not dropped). |
| Deaths | Blank means *not reported*, never zero. Only 2 deaths are recorded in the whole table, so the column is not usable as mortality. |
| Venue | Null for "TBA / Spesifik tidak disebutkan", but an education level (primary, junior secondary, senior secondary, early childhood, pesantren, community health post) is kept where the raw text gives one. |
| Places | Taken from the table's own wikilinks (the link target is the canonical name), so `Kabupaten Bandung` and `Kota Bandung` stay distinct. No NER or gazetteer matching over prose is needed for the primary source. |

### 5.4 Region codes

`kabkota_kode` (e.g. `11.08`), coordinates, area and population come from a public dump of the
Kemendagri kode wilayah (github.com/cahyadsn/wilayah, MIT; its header cites Kepmendagri No
300.2.2-2138 of 2025), built into `data/reference/kabkota_kemendagri.csv` by
`analysis/build_kode_wilayah.py`.

- Checked: 38 provinces and 514 kabupaten/kota, as expected. All 842 rows resolve.
- **Exact name match only.** 221 of 227 distinct names match exactly; six go through a reviewed
  alias table (Palangka Raya, Baubau, Tojo Una-Una, and two typos on the Wikipedia page,
  "Polewali Manda" and "Bombaba"). Fuzzy matching is deliberately not used: the nearest string to
  "Ogan Komering Ilir" is "Ogan Komering Ulu", a different regency.
- The dump has an error: code 16.02 is named "Ogan Komering" (capital Kayu Agung, i.e. Ilir).
- Not the official file, and Kemendagri codes differ from BPS codes for some regions. Population
  is undated.
- A regency listed under a different province than the master list is flagged
  (`kabkota_province_mismatch`); none were.

### 5.5 Quality control

`qc_report.json` records counts, flag frequencies and the arithmetic checks. The page's own
`TOTAL` row (about 11,390) is **not** a pass/fail oracle: the rowspan-aware running total reaches
it on 2025-10-01, so it stopped being updated about a year before the table's last rows. It
exposes the editors' figure as stale; it does not validate the parse. What supports the parse is
that the rowspan-aware sum tracks the editors' total at that date while the naive sum overshoots
by roughly three times, and that the table's figure appears in the cited article for 88% of
events (section 9).

## 6. Article corpus (stages 4-5)

### 6.1 Fetching

- Citations are extracted from the wikitext page-wide (443, including the India, Korea and China
  sections), then restricted to those cited by the kept table rows: **425 unique URLs**.
  `<ref>` templates use both English and Indonesian parameter names on this wiki, so an alias table
  folds `title`/`judul`, `access-date`/`tanggal akses` and so on. An archive URL is preferred over
  the live URL when the citation has one.
- **Politeness** (all crawls): robots.txt honoured; one request at a time per host; a delay with
  random **jitter** (a fraction of the delay added at random, so requests do not arrive on a
  metronome); a User-Agent that identifies the crawler (`MBG_USER_AGENT` can add contact details).
- **Circuit breaker per host.** 429/503 pauses the host for 15 minutes; three network errors in a
  row pause it for 5 minutes; five 403s in a row *from a host that had been answering* pause it for
  30 minutes (a host that never answered is a paywall or blocklist, not a ban). archive.org shares
  one breaker (5 minutes) and a 6-second floor between requests. If other hosts have work, a
  paused host's URLs are deferred; if only that host remains, the crawl waits out the cooldown.
- A robots disallow or a dead link routes to the **Wayback Machine**, choosing the snapshot nearest
  the citation's own date so the text is what was published, not a later edit. In the first crawl
  archive.org rate-limited the crawling IP (429) and the fallback recovered nothing.
- Social platforms (Instagram etc.) are recorded as `skipped_domain` and never requested.
- Every URL lands in a **ledger** with a status (`ok`, `paywalled`, `short_body`, `empty`,
  `robots_disallowed`, `http_error`, `network_error`, `no_archive`, `skipped_domain`), written as
  each URL finishes. A rerun retries failures and skips `ok`.

### 6.2 Extraction

Text and metadata come from **trafilatura**, which is built to strip navigation and boilerplate;
a generic HTML-to-markdown converter would keep it. Inline "Baca juga / Simak juga" related-story
lines survive extraction and are removed, because they add another headline's vocabulary to the
article. A body under 400 characters is `short_body` (or `paywalled` for known paywalled domains).

The publication date used is the **citation's date** when it is a valid date, else the
extractor's. Citation dates are editor-typed and not always ISO (`8 Oktober 2025 {{!}} 16.19 WIB`),
so they are normalised. An extractor date equal to the day the page was crawled is discarded as an
artefact.

### 6.3 De-syndication

Documents are clustered by content: exact matches on a normalised hash, near matches by Jaccard
over word 5-grams (threshold 0.70), with candidates from an inverted index and shingles present in
over 35% of documents excluded as site furniture. The canonical copy is the earliest-dated, and
duplicates are kept with a pointer, not deleted. On the Wikipedia-cited corpus the most similar
pair had Jaccard 0.16, so nothing merged: each row cites its own story.

### 6.4 Result of the first crawl

308 of 425 URLs read (72%), covering 601 of 842 rows and 330 of 459 events. Of the 117 failures:
67 HTTP 403 (publishers that block crawlers, tribunnews.com alone 43, the Jawapos group 15), 16
JavaScript-rendered pages that extract to nothing, 11 dead links, 7 rate-limited, 6 Instagram, 6
network errors and 4 others (2 robots-disallowed, 1 short body, 1 paywalled). The hole is not random, so readable-article rates by group are reported next to every group
comparison.

## 7. Exploratory NLP

Implemented in `analysis/nlp_explore.py`; output in `data/processed/nlp/report.md`.

**Method: lexicon coding by regular expression, not a statistical model.**

- The 308 cited documents are one subject in one language, mutually dissimilar in wording
  (highest pairwise similarity 0.16) but identical in topic. Topic models would produce clusters of
  "keracunan / siswa / MBG" to be narrated, so none was used.
- The cause vocabulary was mined from the corpus (collocates of *penyebab*, *diduga*, *akibat*),
  not chosen in advance. It turned out to be mostly **epistemic**: *menunggu hasil*, *penyebab
  pasti*, *hasil uji laboratorium*.
- No stemming. The Sastrawi stemmer mangles some affixed words (*perawatan* -> *awat*), so concepts
  are matched with affix-tolerant patterns.
- **Unit of analysis: the document**, and group comparisons use only the 296 documents linked to
  exactly one event, so a roundup article does not count towards several groups.
- Each document gets an epistemic state, strongest evidence first: *lab: contamination reported*,
  *lab: result reported clean*, *awaiting lab / cause unknown*, *samples taken, no result stated*,
  *no cause information*. Sentences with a hedge, negation, question or future marker are not
  counted as findings; a hedge counts only if it comes before or inside the finding.
- Group differences are tested with Fisher's exact test, and the report shows a **Bonferroni**
  adjusted p-value. Only differences that survive it are quoted.

What the first report supported (cited corpus, n=308):

| finding | evidence |
|---|---|
| Cause reporting is overwhelmingly unresolved | 49% no cause information, 30% awaiting lab, 19% samples taken, 3% (8 documents) contamination reported, 1 clean |
| Articles are first-day reports | median lag from incident to article = 1 day; 59% within a day |
| Diarrhoea is mentioned far more for senior secondary (49%) than primary (15%) | Fisher p < 0.0001, Bonferroni 0.0002. It is what reporters mention, not incidence. |
| Kitchen closures rose from 3% (2025 Q3) to 12% later | Bonferroni p = 0.12: **not** robust |
| Hedged headlines rose from 49% to 62% | Bonferroni p = 0.29: **not** robust |

**Coverage bias is stated wherever it matters.** The share of table rows with a readable article
ranges from 19% (Bengkulu) to 96% (Sumatera Selatan) by province but only 67-88% by school level,
so per-province text comparisons are not defensible and school-level ones are.

## 8. Independent discovery and linking (stages 6-7)

### 8.1 Why

The cited articles are mostly written on the day. Lab results and closures come later and are
mostly not cited. So a second source of coverage is needed, independent of what the Wikipedia
editors chose. One search per event (thousands of requests) was rejected in favour of walking each
outlet's **topic feed** once and linking offline.

### 8.2 Feeds

`discover.py` walks each outlet's "keracunan-mbg" tag feed newest-first: detik, Antara, Kompas, CNN
Indonesia, Liputan6. Their robots.txt allows the tag pages. Tempo needs JavaScript and Beritasatu
blocks crawlers, so both are excluded.

| outlet | headlines | pages | ends |
|---|---|---|---|
| detik | 1,047 | 106 | end of feed, back to Apr 2025 |
| Antara | 486 | 38 | end of feed, back to Jan 2025 |
| Kompas | 2,288 | 153 | see below, back to Jan 2025 |
| CNN Indonesia | 303 | 32 | end of feed, back to Apr 2025 |
| Liputan6 | 167 | 10 | 404 past the last page, back to Jan 2025 |

Per-outlet parsers reflect each site's markup, with traps recorded in the tests: CNN's visible "x
minggu yang lalu" and the HTML comment beside it are *render-relative*, so the article date is
taken from the URL; Antara's feed mixes video and photo items with articles; Liputan6's page
carries a sidebar and mega-menu of unrelated stories.

**Kompas did not end with an empty page.** From page 154 it served generic latest news (Europe,
corruption, zodiac signs). The walk was stopped by hand, those 13 rows were moved to a sidecar
file, and a **topic safeguard** now ends any walk when a page has under 20% on-topic headlines and
keeps that page aside. All five feeds were then checked: 0-5% of rows look off-topic, and those are
edge stories.

Politeness for feeds: 10 s per page plus up to 50% jitter, the same breaker as section 6.1, one
process per host. Every listing page is saved as gzipped HTML with its sha256 and fetch time in a
manifest, one directory per run, so a headline can always be traced to its page.

### 8.3 Fetching discovered articles

A priority list orders headlines: linked to one event, then ambiguous, then the rest; video pages
and URLs already read for the citations are skipped. Crawl settings: 8 s per request plus up to 50%
jitter, no archive fallback, separate ledger and cache from the Wikipedia-cited set. Kompas is
restricted to linked and ambiguous headlines (1,127 of 2,288) to keep the load on that site modest.
Across these crawls no host returned a 403 or 429 to the crawler; the few failures (about 1-3% of
URLs) were the local machine's DNS lookups failing or a connection being reset. The computer
went to sleep from time to time during the multi-hour crawls, which is the likely cause. These
are `network_error` rows in the ledger, not evidence about the sites.

**Retrying failures.** A rerun of the same `articles` command retries every URL that is not `ok`
(network errors, and also `short_body`/`empty` pages, which are cheap to recheck) and skips the
rest, so failures caused by the machine sleeping are recovered without redoing successes. The
failures should be rechecked after each long crawl and the evidence rebuilt, because a
`network_error` article contributes nothing to the linking until it is read. Only rows that are
still `http_error` (403 and similar) after a retry should be treated as a real hole.

The first retry (detik) recovered all 7 network errors, giving 823 articles read. The 23 that
remain `short_body` are not failures: they are photo galleries (`/foto-news/`, `/fotohealth/`,
`/foto/`) whose text is a caption, so the target builder now skips them.

### 8.4 Linking rule (`link.py`, rules version 2026-09-19.1)

An article links to an event only if **all** hold:

1. Its **headline or lead paragraph** (first 500 characters) names the event's venue or its
   kabupaten/kota. Run-together spellings ("Kulonprogo") match, and matches are whole-word.
2. Its date is within **1 day before to 30 days after** the incident (month-precision events span
   the month).
3. **Exactly one** event matches.

Tiers and outcomes:

- A named **venue outranks a bare place name**: "MAN 2 Rembang" picks its event even when two
  events in Rembang fall inside the window.
- Two or more matches -> `ambiguous`: candidates are recorded, and the article is **never assigned
  to the nearest** event. Kabupaten X and Kota X share names, so this is common.
- A place named only deep in the body -> `weak`: kept as evidence, not linked, because roundups
  and follow-ups name many places in passing.
- Undated articles and undated events cannot be windowed. A page dated on its crawl day is treated
  as undated.

Each decision records the tier and the matched terms.

### 8.5 Auditing Wikipedia's own citations

The same rule is applied to the cited articles. `names_event` separates two different failures:
the text does not name the cited place at all, versus the place is named but the dates or another
event in the same regency disagree.

Of 308 cited articles: 239 agree, 42 are ambiguous but include the cited event, 10 name the right
place but conflict on date or match another event in the regency, 7 are undated, and **10 never
name the cited place** in the headline or lead. Read by hand, about 5 of those 10 look genuinely
wrong (an Aceh Timur row citing a story about Cianjur, Salatiga citing Karanganyar, Jakarta
Selatan / Lampung Selatan / Metro citing provincial statements), 4 are national roundups or
statements that cannot corroborate one incident, and 1 is a miss on the abbreviation "Pangkep". It
is a review queue, not a verdict.

## 9. Cause findings and event-level analysis

Implemented in `analysis/build_evidence.py`.

- **Findings are quoted sentences**, at most one per kind per article: lab contamination, lab
  clean, awaiting a result, kitchen closure, and mechanisms (spoilage, timing/storage, hygiene).
  Each carries its character offset in the cached text, so it can be checked against the page.
  Agents are normalised (E. coli, Salmonella, Staphylococcus, Bacillus, nitrite/nitrate,
  histamine, unspecified bacteria, chemical, virus).
- **Event state = best state across all articles linked to the event**, cited or discovered:
  contamination reported > clean > awaiting > samples taken > no cause information > no article read.
  An event counts as *resolved* when an article reports a lab contamination or clean result.
- **Lag = days from the incident date to the earliest article date reporting a result.** This, not
  the lag to the first article, says whether causes get published at all. **It is right-censored at
  30 days by construction**: linking accepts articles only up to 30 days after the incident, so the
  longest possible lag is about 30 and a result published later is not counted. "Resolved" therefore
  means "a laboratory result was reported within 30 days". The window was not widened because a
  longer one adds ambiguity (more events in the same regency inside the window) and the feeds
  contain few late follow-ups; whether it should be widened is an open question.
- The headline number is **per event**, because per-document shares change whenever the corpus
  changes composition (adding follow-ups would move "49% no cause information" without anything
  about the incidents changing).

## 10. Validation: what was checked and what it found

| check | how | result |
|---|---|---|
| Rowspan handling | Compared the counted-once sum with the page's TOTAL over time | Tracks it at 2025-10-01; naive sum overshoots ~3x |
| Table vs article counts | Did the table's figure appear in the linked article? | 88% (250 of 284 events). The 14 disagreements were read by hand: school populations, subsets (inpatients), cumulative updates, and the known Cianjur wrong-source row. None was a table undercount. |
| Region join | Exact name match against 514 regencies | 221/227 exact, 6 by reviewed alias, 0 unmatched, 0 province mismatches |
| Wikipedia citations | Text linking (section 8.5) | 239/308 agree; about 5 look wrong |
| Lexicon precision, round 1 | Read matched snippets | Found and fixed: a clinic exam counted as a cause, questions and hopes counted as results, an apology lexicon matching interview filler, video-teaser lines skewing counts |
| Lab-finding precision, round 2 | Read all 24 first flagged findings | 4 were not laboratory causes (a headcount matched by "ditemukan", an inspection's "risk factors", negligence, a clean drinking-water result). Rule tightened; earlier run kept for comparison. |
| Lab-finding **recall** | Read a random sample of 60 sentences containing strong cause vocabulary that were **not** flagged (`analysis/recall_audit.py`, seed 7) | About 8% (5 of 60) were genuine lab findings the rules missed, mostly passive and "kandungan bakteri" phrasings, plus a splitter that cut "E. coli" in half. Fixed. |
| Widened rule | Read every newly flagged sentence | 10 of 11 genuine (one was commentary) |
| All-outlet findings | Read the first finding for each of the 52 events the five-outlet run resolved | 51 stood. One was a false positive (a generic explainer, "nitrat yang dipicu bakteri pengurai **dapat** menyebabkan keracunan", which resolved a Cianjur event it did not concern): a modal after a bare causal phrase now marks an explainer, pinned as a test. Softer cases that remain counted: a preliminary "uji awal positif E.coli" still awaiting official results (Bandar Lampung), a result described as "beberapa waktu lalu" that may belong to an earlier incident (Gunungkidul), a lab finding of three bacteria with the cause "not concluded" (Karo), and clean results counted as results (4 events). |
| Regression | 175 automated tests, offline | Pass. Sentences from each audit are pinned as tests. |

**Who did the reading:** the samples above were read and judged by the analyst working on this
project (an AI assistant), not by independent human annotators. They are spot-checks, not a
measured accuracy. A hand-labelled sample by a human coder would be the proper next check, and
would tell whether the recall improvement holds.

## 11. Known limitations and biases

- **One curated source, and a secondary one.** The incident list is what Wikipedia editors
  recorded. It is not independent of the other public trackers, several of which drew on it. Where
  it is wrong, so is this dataset (section 8.5, and below).
- **Wikipedia errors found:** the TOTAL row is stale; about 5 of 308 rows cite a story about another
  place; some dates are typos on the page or in the citation (a 2025 citation date on a 2026
  incident; a row dated 2025-01-20 with an article from January 2026); two place names are
  misspelled.
- **News is not a sample of incidents.** Coverage follows media attention: Java is over-covered
  relative to eastern Indonesia.
- **Missing readers.** The publishers that block crawlers (tribunnews, Jawapos group) are missing
  from every crawl, so their regions are systematically thinner. Discovery adds the outlets that
  already worked and does not recover this hole; it shifts coverage toward wherever national desks
  report, which is a new bias, not a removed one.
- **Articles are first-day reports.** "Awaiting lab" reflects when the article was written. It says
  nothing about what the laboratory eventually found.
- **Absence of a published result is not absence of a result.** Results may exist in official
  documents that were not searched.
- **Conservative linking undercounts.** Headline-or-lead matching, a 30-day window and the
  refusal to resolve ambiguity all trade recall for precision. A follow-up that names the place
  only in the body is `weak` and does not count. Kompas is only partly crawled (linked and
  ambiguous headlines).
- **The lag to a reported result is censored at 30 days** (section 9), and events from the last few
  weeks before the crawl have had less time to be reported on than earlier ones.
- **Feed depth differs by outlet.** detik and CNN reach back to April 2025, Antara, Kompas and
  Liputan6 to January 2025, so the earliest incidents (2024 Q4 and 2025 Q1: 13 events) have almost no
  discovered coverage and the share resolved by quarter is not comparable across the whole span.
- **Regex coding.** Recall was spot-checked but not measured on a labelled set, so lab-result
  counts are floors, and symptom and food shares are what reporters mentioned, not clinical
  incidence.
- **Region data.** Codes come from a third-party dump with a known error, not the official file;
  population and area are undated.
- **Deaths** are recorded for 2 events on the page and cannot be analysed.

## 12. Evidence trail and reproducibility

- **Raw pages** are kept: gzipped HTML and extracted text for every article
  (`data/raw/articles*/`), and gzipped HTML for every feed listing page
  (`data/raw/feeds/<outlet>/<UTC>/`) with a sha256 manifest.
- **Ledgers** (`fetch_ledger.jsonl`, one per crawl) record every URL, its status, the sha256 of the
  body, the fetch time and the reason for any failure.
- **Evidence runs** in `data/evidence/runs/<UTC>/` are never overwritten; `data/evidence/LATEST`
  names the newest. Each holds `manifest.json` (sha256 of every input, link-rule version, lexicon
  hash), `articles.jsonl`, `link_decisions.jsonl`, `findings.jsonl` (quoted sentences with offsets),
  `event_summary.csv` and `summary.json`. Earlier runs are kept on purpose, including ones made
  with looser lexicons, so a change in a number can be traced to the change in the rule.
- The project is **not under version control**. The evidence runs, ledgers and manifests are the
  record of what was known when.

To reproduce (Python 3.11+):

```bash
pip install -e ".[dev,analysis]"
python -m mbgpipe fetch && python -m mbgpipe parse && python -m mbgpipe normalize
python -m mbgpipe articles && python -m mbgpipe corpus
python analysis/nlp_explore.py
python -m mbgpipe discover --outlet detik            # then antara, kompas, cnn, liputan6
python analysis/pilot_link.py --outlet detik
python analysis/make_fetch_targets.py --outlet detik
python -m mbgpipe articles --targets data/interim/discovered/detik.fetch_targets.jsonl \
    --ledger data/interim/discovered/detik.fetch_ledger.jsonl --cache data/raw/articles_discovered \
    --delay 8 --jitter 0.5 --no-wayback
python analysis/build_evidence.py
pytest -q
```

Re-fetching the page will return a newer revision than 29900932, so numbers will differ; the raw
wikitext and the evidence runs preserve the version analysed here.

## 13. Decisions not taken, and why

| not done | reason |
|---|---|
| Topic modelling (LDA/NMF) | 308 same-topic documents; clusters would be narrated, not found |
| Stemming with Sastrawi | Mangles affixed words; patterns tolerate affixes instead |
| NER / gazetteer matching over prose for the incident list | The table's wikilinks already give canonical places |
| Fuzzy matching of region names | Picks a different regency (Ilir vs Ulu) |
| Event clustering by place and time | The table's rowspans already define events; a window would merge distinct incidents |
| One search per event | Thousands of requests; a bounded topic feed is one crawl |
| Assigning ambiguous articles to the nearest event | Silent overcounting that is invisible in aggregates |
| A `revisions` history and a JPPI/WordPress channel (in the first draft) | Never built; revision timestamps are an editorial-attention series and must not be plotted as incidence |
| Wayback retry for the 117 failed URLs | archive.org was rate-limiting; deferred, not abandoned |
| Fixing the region dump upstream | Handled by an alias in this project |

## 14. Status at the time of writing

All stages are complete. The authoritative numbers are in `data/evidence/runs/20260919T201142Z/summary.json`
(`data/evidence/LATEST` names the newest run).

- Structured layer and the cited-article corpus: revision 29900932; 842 rows, 459 events, 308 of
  425 cited articles read.
- Discovery feeds: complete for all five outlets (section 8.2).
- Discovered-article crawls: 2,853 URLs across the five outlets, 2,830 read. The other 23 are detik
  photo galleries that carry only captions. Failures caused by the computer sleeping (DNS lookups,
  reset connections) were retried per ledger and all recovered: 7 in detik, 6 in Antara and 10 in
  Kompas; Liputan6 and CNN had none.
- Linking (all outlets, 4,504 articles including the 308 cited): 2,111 linked to one event, 564
  ambiguous, 191 weak, 1,638 matched no event.
- **Cause findings, per event over all 459.** Events with a laboratory result reported within 30
  days: **10 from the cited articles alone, 51 with discovery** (41 newly resolved; 47 report
  contamination and 4 a clean result), a median 9 days after the incident (20 within a week, 16
  within 8-14 days, 15 later, maximum 27, censored at 30). Events with no article at all fell from
  129 to 60, and 246 events gained at least one independently found article (1,864 in all). Agents
  named across the 51: unspecified bacteria 38, E. coli 21, Bacillus 7, Salmonella 6,
  Staphylococcus 3, nitrite 2, histamine 1, other chemical 1. 176 events still read "awaiting
  results" after every article. Earlier runs, made with looser lexicons and fewer outlets, are kept
  in `data/evidence/runs/` so the effect of each change can be traced.
- **Not resolved by these data:** 89% of events. That does not mean their causes were never found.
  It means no article in these five outlets, within 30 days, reported a laboratory result the
  regexes recognised. Official documents were not searched, the blocked publishers are missing, and
  recall is spot-checked, not measured (section 10).
- Open next steps: a human-labelled sample to measure recall; testing whether a longer link window
  recovers late results without adding ambiguity; the Wayback retry of the 117 failed
  Wikipedia-cited URLs once archive.org stops rate-limiting; and official sources (BPOM, BGN and
  regional health offices).
