"""Exploratory NLP over the MBG corpus. Re-run after the corpus changes:

    python analysis/nlp_explore.py

Writes data/processed/nlp/*.csv and data/processed/nlp/report.md.

Method notes
------------
* Lexicon coding by regex, not a model. 308 same-topic documents are too few for topic
  models to separate anything, and regex hits can be read and checked (see `--examples`).
  The cause vocabulary was mined from the corpus (collocates of "penyebab", "diduga",
  "akibat"), not chosen in advance.
* No stemming. Sastrawi's stemmer mangles some affixed words ("perawatan" -> "awat"), so
  concepts are matched with explicit patterns that tolerate Indonesian affixes.
* The unit is the DOCUMENT. Group comparisons use only documents linked to exactly one
  event, so a roundup article does not count towards several groups.
* Every rate prints its denominator. The corpus is what Wikipedia editors cited and the
  crawler could read (see the coverage table); it is not a sample of Indonesian news.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "processed" / "nlp"

# --- lexicons ----------------------------------------------------------------

PATHOGEN = r"bakteri|salmonella|e\.?\s?-?coli|escherichia|staphylo\w*|bacillus|coliform|kuman|norovirus|virus|histamin\w*|parasit"
# Microbes plus the chemical agents that reports name as causes (nitrite at Kudus, Bandung Barat).
AGENT = PATHOGEN + r"|nitrit|nitrat|formalin|boraks|pestisida|sianida|logam\s+berat|toksin"
LAB_RESULT_WORD = r"hasil\s+(?:uji|pengujian|pemeriksaan|analisis|laboratorium|lab)\b|(?:labkesda|bpom)\b"
LAB_POSITIVE = re.compile(
    rf"(?:{LAB_RESULT_WORD})[^.\n]{{0,100}}?"
    rf"(?:positif|mengandung|terdeteksi|terbukti|melebihi|tercemar|"
    rf"(?:menunjukkan|menyatakan)\s+(?:adanya\s+)?(?:kontaminasi|{AGENT}))"
    # "ditemukan" alone matches "ditemukan 112 siswa bergejala", so it must be followed by an agent
    rf"|(?:{LAB_RESULT_WORD})[^.\n]{{0,100}}?ditemukan\s+(?:\w+\s+){{0,3}}?(?:{AGENT}|zat|kandungan|cemaran)"
    rf"|positif\s+(?:mengandung\s+)?(?:{AGENT})"
    rf"|(?:ditemukan|terdeteksi|mengandung)\s+(?:adanya\s+)?(?:{AGENT})"
    rf"|(?:disebabkan|dipicu|akibat|penyebab(?:nya)?\s+(?:adalah|ialah)?)\s+(?:oleh\s+)?(?:kadar\s+)?(?:{AGENT})\s+(?:yang\s+)?(?:tinggi|berlebih)?"
    # passive and "kandungan" phrasings the first audit found missed:
    #   "dari hasil lab ada kandungan bakteri", "hasil penelitian BPOM ... karena adanya bakteri"
    rf"|(?:{LAB_RESULT_WORD}|hasil\s+penelitian)[^.\n]{{0,80}}?(?:ada\s+|adanya\s+)?(?:kandungan\s+)?(?:{AGENT})"
    #   "kandungan e coli melebihi batas aman", "kontaminasi bakteri juga ditemukan pada sampel air"
    rf"|kandungan\s+(?:{AGENT})[^.\n]{{0,40}}(?:melebihi|ditemukan|tinggi)"
    rf"|(?:kontaminasi\s+)?(?:{AGENT})\s+(?:\w+\s+)?ditemukan\s+(?:pada|di|dalam)\s+(?:sampel|makanan|menu|lauk|air|udang|ayam|telur|susu)", re.I)
# A lab result reported as clean. Judged per sentence like LAB_POSITIVE.
LAB_NEGATIVE = re.compile(
    r"hasil\s+(?:uji|pengujian|pemeriksaan|analisis|laboratorium|lab)\b[^.\n]{0,100}?\b(?:negatif|aman|layak\s+konsumsi|"
    r"memenuhi\s+(?:syarat|standar)|tidak\s+(?:ditemukan|terdeteksi|mengandung))\b", re.I)
# A sentence with any of these is a question, a hope or a hedge, not a reported result.
NOT_A_RESULT = re.compile(
    r"\b(?:belum|tidak|bukan|apakah|masih|menunggu|diharapkan|akan|kemungkinan|diduga|dugaan|"
    r"jika|bila|untuk\s+(?:mengetahui|memastikan)|guna|negatif|"
    # generic explainers ("keracunan biasanya disebabkan oleh bakteri") are not findings
    r"biasanya|umumnya|lazim|bisa|dapat|mungkin)\b", re.I)


CAUSAL_START = re.compile(r"(?:disebabkan|dipicu|akibat|penyebab)", re.I)
MODAL_AFTER = re.compile(r"\b(?:dapat|bisa|berpotensi|berisiko|mampu)\b", re.I)


def reports_lab_finding(sentence: str) -> bool:
    """A sentence that states a laboratory finding. A hedge or negation counts only if it comes
    before or inside the finding: "dari hasil lab ada kandungan bakteri, dan tidak menemukan
    unsur pidana" is a finding, "belum ada hasil lab yang menyebut bakteri" is not."""
    m = LAB_POSITIVE.search(sentence)
    if not m or NOT_A_RESULT.search(sentence[:m.end()]):
        return False
    # "nitrat yang dipicu bakteri pengurai DAPAT menyebabkan keracunan" is an explainer: a modal
    # right after a bare causal phrase ("dipicu/disebabkan/akibat X") makes it a possibility.
    if CAUSAL_START.match(m.group(0)) and MODAL_AFTER.search(sentence[m.end():m.end() + 45]):
        return False
    return True


AWAITING = re.compile(
    r"menunggu\s+hasil"
    r"|hasil\b[^.\n]{0,60}\b(?:belum|masih)\b[^.\n]{0,30}(?:keluar|diketahui|ada|muncul|diterima|terbit)"
    r"|belum\s+(?:diketahui|dapat\s+dipastikan|bisa\s+dipastikan|ada\s+kepastian)"
    r"|penyebab\s+pasti[^.\n]{0,60}(?:belum|masih|menunggu)"
    r"|(?:belum|masih)\s+(?:dalam\s+)?(?:proses\s+)?(?:pengujian|pemeriksaan|penyelidikan|diselidiki|diperiksa|diuji|diteliti)", re.I)
SAMPLE = re.compile(
    r"sampel\s+[^.\n]{0,60}(?:dikirim|diambil|diperiksa|diuji|dibawa|diserahkan)"
    r"|(?:mengambil|mengirim|mengirimkan|membawa)\s+(?:\w+\s+){0,2}sampel|pengambilan\s+sampel", re.I)
HEDGE = re.compile(r"\b(?:diduga|dugaan|disinyalir|dicurigai|kemungkinan)\b", re.I)

MECHANISM = {
    "pathogen named (incl. speculation)": re.compile(PATHOGEN, re.I),
    "spoilage / off food (basi, bau, berlendir)": re.compile(r"\b(?:basi|busuk|berlendir|berjamur|berbau|bau\s+(?:tidak\s+sedap|menyengat|asam)|tidak\s+layak\s+konsumsi|rasa\s+(?:aneh|asam|pahit))\b", re.I),
    "timing / storage / temperature": re.compile(r"\b(?:dimasak\s+(?:terlalu\s+)?(?:dini|pagi|malam)|jarak\s+waktu|suhu|penyimpanan|disimpan|terlalu\s+lama|waktu\s+(?:distribusi|memasak|penyajian))\b", re.I),
    "hygiene / sanitation (higienis, sanitasi, SLHS)": re.compile(r"\b(?:higien\w*|sanitasi|kebersihan|slhs|sertifikat\s+laik|kontaminasi|terkontaminasi|cuci\s+tangan)\b", re.I),
    "water / utensils": re.compile(r"\b(?:air\s+(?:tercemar|kotor|sumur|galon)|ompreng|alat\s+makan|peralatan|wadah)\b", re.I),
    "allergy / non-food cause": re.compile(r"\b(?:alergi|alergen|psikologis|psikogenik|histeria|sugesti|kelelahan|sakit\s+sebelumnya)\b", re.I),
    "denies MBG link": re.compile(r"\b(?:bukan\s+(?:karena|akibat)\s+(?:mbg|makanan)|tidak\s+ada\s+(?:hubungan|kaitan)\s+dengan\s+(?:mbg|makanan))", re.I),
}
FOODS = {"ayam": r"ayam", "telur": r"telur", "ikan": r"ikan|lele|tuna|cumi|udang", "susu": r"susu",
         "tahu/tempe": r"tahu|tempe", "sayur": r"sayur\w*|kol|wortel|buncis|brokoli", "buah": r"pisang|jeruk|melon|semangka|apel|salak|pepaya|anggur|pir",
         "daging/olahan": r"daging|sapi|bakso|sosis|nugget|rendang", "mie/pasta": r"mie|mi\s+goreng|spaghetti|bihun",
         "nasi": r"nasi"}
FOOD_SUSPECT = re.compile(r"\b(?:basi|bau|berlendir|berjamur|mentah|kurang\s+matang|asam|pahit|diduga|dicurigai|penyebab|sampel|positif|terkontaminasi|tidak\s+layak|berulat)\b", re.I)

SYMPTOMS = {"mual": r"mual", "muntah": r"muntah", "pusing": r"pusing|sakit\s+kepala", "diare": r"diare|mencret",
            "sakit perut": r"sakit\s+perut|nyeri\s+perut|perut\s+(?:sakit|perih|melilit)|kram\s+perut",
            "lemas": r"lemas|lemah", "demam": r"demam", "sesak": r"sesak", "gatal/ruam": r"gatal|ruam|biduran", "pingsan": r"pingsan|tidak\s+sadar"}
CARE = {"rawat inap / rujuk": r"rawat\s+inap|dirawat\s+inap|opname|dirujuk|dilarikan\s+ke\s+(?:rs|rumah\s+sakit)|icu",
        "rawat jalan / puskesmas": r"rawat\s+jalan|puskesmas|klinik|pustu"}

RESPONSE = {
    "SPPG halted / closed": re.compile(r"sppg[^.\n]{0,80}(?:dihentikan|ditutup|disetop|dibekukan|ditangguhkan|dinonaktifkan)|(?:menghentikan|menutup|menyetop|membekukan)[^.\n]{0,40}sppg|operasional[^.\n]{0,50}(?:dihentikan|ditutup)", re.I),
    "KLB declared / mentioned": re.compile(r"\bklb\b|kejadian\s+luar\s+biasa", re.I),
    "police involved": re.compile(r"\b(?:polisi|polres\w*|polsek|kepolisian|polda|penyidik|penyelidikan\s+polisi)\b", re.I),
    "BGN (national agency) cited": re.compile(r"\bbgn\b|badan\s+gizi\s+nasional", re.I),
    "BPOM cited": re.compile(r"\bbpom\b|badan\s+pengawas\s+obat", re.I),
    "apology": re.compile(r"(?:meminta|minta|memohon|menyampaikan|permohonan|permintaan)\s+maaf", re.I),
    "costs covered": re.compile(r"(?:biaya|pengobatan|perawatan)[^.\n]{0,60}(?:ditanggung|gratis)|menanggung\s+(?:seluruh\s+)?biaya", re.I),
    "SLHS / hygiene certificate": re.compile(r"\bslhs\b|sertifikat\s+laik\s+(?:higiene|hygiene)", re.I),
    "DPR / legislators": re.compile(r"\bdpr\b|anggota\s+dewan|komisi\s+(?:ix|x)\b", re.I),
    "calls for programme halt / full evaluation": re.compile(r"moratorium|(?:hentikan|setop|stop)\s+(?:sementara\s+)?(?:program\s+)?mbg|evaluasi\s+(?:menyeluruh|total|nasional)", re.I),
}
# "Simak Video '...'" / "Lihat video:" teasers carry another story's headline into the text.
RE_TEASER = re.compile(r"(?im)^.*?\b(?:simak|lihat|saksikan)\s+(?:juga\s+)?video\b.*$")
NUM = r"\d{1,3}(?:\.\d{3})+|\d+"
RE_COUNT = re.compile(rf"\b({NUM})\s+(?:orang\s+)?(?:siswa|siswi|murid|pelajar|santri|santriwati|anak|orang|korban|pasien|warga|guru|balita|penerima)", re.I)


# --- data --------------------------------------------------------------------

def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[dict]]:
    proc = ROOT / "data" / "processed"
    docs = pd.read_json(proc / "corpus.jsonl", lines=True)
    docs["text"] = docs.text.apply(lambda t: RE_TEASER.sub("", t))
    inc = pd.read_csv(proc / "incidents.csv")
    ev = pd.read_csv(proc / "events.csv")
    for frame, cols in ((inc, ["source_urls", "symptomatic_by_subject"]), (ev, ["venue_levels", "symptomatic_by_subject"])):
        for c in cols:
            frame[c] = frame[c].apply(json.loads)
    ledger = [json.loads(l) for l in open(ROOT / "data" / "interim" / "fetch_ledger.jsonl", encoding="utf-8")]
    return docs, inc, ev, ledger


def attach_events(docs: pd.DataFrame, inc: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    event_of = inc.set_index("incident_id").event_id
    docs = docs.copy()
    docs["event_ids"] = docs.incident_ids.apply(lambda ids: sorted({event_of[i] for i in ids}))
    docs["n_events"] = docs.event_ids.apply(len)
    docs["title_text"] = docs.title.fillna("")
    single = docs[docs.n_events == 1].copy()
    e = ev.set_index("event_id")
    for col in ("date_start", "date_precision", "province", "symptomatic", "symptomatic_kind", "symptomatic_by_subject"):
        single[col] = single.event_ids.apply(lambda ids, col=col: e.loc[ids[0], col])

    def level(ids):
        lv = e.loc[ids[0], "venue_levels"]
        return lv[0] if len(lv) == 1 else ("mixed" if lv else "unknown")
    single["venue_level"] = single.event_ids.apply(level)
    return docs.merge(single[["doc_id", "date_start", "date_precision", "province", "symptomatic", "symptomatic_kind",
                              "symptomatic_by_subject", "venue_level"]], on="doc_id", how="left")


# --- coding ------------------------------------------------------------------

def sentences(text: str) -> list[str]:
    # Not after a lone capital ("E. coli", "S. aureus"), which would cut the organism name in half.
    return [s for s in re.split(r"(?<=[.!?])(?<!\b[A-Z]\.)\s+|\n+", text) if s.strip()]


def certainty(text: str) -> str:
    """Best-supported epistemic state of the report, strongest first."""
    sents = sentences(text)
    if any(reports_lab_finding(x) for x in sents):
        return "lab: contamination reported"
    if any(LAB_NEGATIVE.search(x) and not re.search(r"\b(?:belum|menunggu|apakah|akan|jika|bila)\b", x, re.I) for x in sents):
        return "lab: result reported clean"
    if AWAITING.search(text):
        return "awaiting lab / cause unknown"
    if SAMPLE.search(text):
        return "samples taken, no result stated"
    return "no cause information"


def code_document(row: pd.Series) -> dict:
    text, title = row["text"], row["title_text"]
    out = {"doc_id": row["doc_id"], "certainty": certainty(text),
           "hedged_body": bool(HEDGE.search(text)), "hedged_title": bool(HEDGE.search(title))}
    for name, rx in MECHANISM.items():
        out[f"mech: {name}"] = bool(rx.search(text))
    sents = sentences(text)
    for food, pat in FOODS.items():
        rx = re.compile(rf"\b(?:{pat})\b", re.I)
        out[f"food mentioned: {food}"] = bool(rx.search(text))
        out[f"food suspected: {food}"] = any(rx.search(s) and FOOD_SUSPECT.search(s) for s in sents)
    for name, pat in SYMPTOMS.items():
        out[f"symptom: {name}"] = bool(re.search(rf"\b(?:{pat})", text, re.I))
    for name, pat in CARE.items():
        out[f"care: {name}"] = bool(re.search(rf"\b(?:{pat})\b", text, re.I))
    for name, rx in RESPONSE.items():
        out[f"response: {name}"] = bool(rx.search(text))
    return out


# "Dari total 517 siswa, hanya enam ..." and "597 siswa yang terdaftar" give a population, not
# victims. Counting them made 7 of 284 events look like the table under-reported.
RE_DENOM_BEFORE = re.compile(r"\b(?:dari|memiliki)\s+(?:total\s+|jumlah\s+)?(?:sekitar\s+)?$", re.I)
RE_DENOM_AFTER = re.compile(r"^(?:\s+\w+){0,3}?\s+(?:yang\s+)?(?:terdaftar|mengikuti|ikut\s+mencicipi)|^\s+penerima\s+manfaat", re.I)


def article_numbers(text: str, title: str) -> list[int]:
    full = f"{title}\n{text}"
    out = set()
    for m in RE_COUNT.finditer(full):
        if RE_DENOM_BEFORE.search(full[max(0, m.start() - 25):m.start()]) or RE_DENOM_AFTER.search(full[m.end():m.end() + 40]):
            continue
        out.add(int(m.group(1).replace(".", "")))
    return sorted(out)


def count_agreement(docs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, d in docs[docs.symptomatic.notna() & (docs.n_events == 1)].iterrows():
        nums = article_numbers(d["text"], d["title_text"])
        table = int(d["symptomatic"])
        parts = set(d["symptomatic_by_subject"].values()) if isinstance(d["symptomatic_by_subject"], dict) else set()
        if not nums:
            verdict = "no counts in article"
        elif table in nums or parts & set(nums):
            verdict = "table figure appears in article"
        elif max(nums) > table:
            verdict = "article reports more than table"
        else:
            verdict = "article reports less than table"
        rows.append({"doc_id": d["doc_id"], "url": d["original_url"], "table": table,
                     "article_max": max(nums) if nums else None, "verdict": verdict,
                     "ratio": round(max(nums) / table, 2) if nums and table else None,
                     "title": d["title_text"][:90]})
    return pd.DataFrame(rows)


def reporting_lag(docs: pd.DataFrame) -> pd.DataFrame:
    d = docs[(docs.date_precision == "day") & docs.date_start.notna() & docs.publish_date.notna()].copy()
    d["lag_days"] = (pd.to_datetime(d.publish_date, errors="coerce") - pd.to_datetime(d.date_start, errors="coerce")).dt.days
    return d[["doc_id", "url", "original_url", "date_start", "publish_date", "lag_days", "title_text"]].dropna(subset=["lag_days"])


# --- reporting ---------------------------------------------------------------

def md_table(df: pd.DataFrame, index: bool = True) -> str:
    df = df.reset_index() if index else df
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join("" if pd.isna(v) else (f"{v:.0%}" if isinstance(v, float) and 0 <= v <= 1 and c != "n" else str(v))
                                       for c, v in zip(cols, r.tolist())) + " |")
    return "\n".join(lines)


def share(coded: pd.DataFrame, prefix: str) -> pd.DataFrame:
    cols = [c for c in coded.columns if c.startswith(prefix)]
    s = pd.DataFrame({"docs": coded[cols].sum(), "share": coded[cols].mean()})
    s.index = [c[len(prefix):] for c in s.index]
    return s.sort_values("docs", ascending=False)


def by_group(coded: pd.DataFrame, group: str, prefix: str, min_n: int = 15) -> pd.DataFrame:
    cols = [c for c in coded.columns if c.startswith(prefix)]
    g = coded.groupby(group)
    sizes = g.size()
    keep = sizes[sizes >= min_n].index
    t = g[cols].mean().loc[keep]
    t.columns = [c[len(prefix):] for c in t.columns]
    t.insert(0, "n", sizes.loc[keep])
    return t


def significance_tests(single: pd.DataFrame) -> pd.DataFrame:
    """Fisher exact tests for the comparisons the report draws attention to. With ~10 symptoms
    and several groups, one p<0.05 in twenty is chance, so Bonferroni-adjusted p is shown."""
    from scipy.stats import fisher_exact

    def test(label, a, b, a_name, b_name):
        p = fisher_exact([[a.sum(), len(a) - a.sum()], [b.sum(), len(b) - b.sum()]])[1]
        return {"comparison": label, "group A": f"{a_name}: {a.mean():.0%} (n={len(a)})",
                "group B": f"{b_name}: {b.mean():.0%} (n={len(b)})", "p": p}

    def level(name, col):
        return single.loc[single.venue_level == name, col]

    rows = [test(f"{s.split(': ')[1]}: primary vs senior secondary", level("primary", s), level("senior_secondary", s),
                 "primary", "senior secondary") for s in ("symptom: diare", "symptom: muntah", "symptom: sakit perut", "symptom: mual")]
    first = single[single.quarter == "2025Q3"]["response: SPPG halted / closed"]
    later = single[single.quarter.isin(["2025Q4", "2026Q1", "2026Q2", "2026Q3"])]["response: SPPG halted / closed"]
    rows.append(test("SPPG halted: 2025Q3 vs later", first, later, "2025Q3", "2025Q4-2026Q3"))
    early = single[single.quarter.isin(["2025Q3", "2025Q4"])].hedged_title
    late = single[single.quarter.isin(["2026Q2", "2026Q3"])].hedged_title
    rows.append(test("title hedged (diduga...): 2025H2 vs 2026Q2-Q3", early, late, "2025H2", "2026Q2-Q3"))
    out = pd.DataFrame(rows)
    def fmt(p: float) -> str:
        return "<0.0001" if p < 0.0001 else f"{p:.4f}"

    out["p (Bonferroni x%d)" % len(out)] = (out.p * len(out)).clip(upper=1).apply(fmt)
    out["p"] = out.p.apply(fmt)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", type=int, default=0, help="print N matched snippets per lexicon for checking")
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)

    docs, inc, ev, ledger = load()
    docs = attach_events(docs, inc, ev)
    coded = pd.DataFrame([code_document(r) for _, r in docs.iterrows()]).merge(
        docs[["doc_id", "domain", "publish_date", "venue_level", "n_events", "original_url", "title_text"]], on="doc_id")
    coded["quarter"] = pd.PeriodIndex(pd.to_datetime(coded.publish_date, errors="coerce"), freq="Q").astype(str)
    coded.to_csv(OUT / "documents_coded.csv", index=False, encoding="utf-8")

    if args.examples:
        show_examples(docs, args.examples)

    ok = {r["url"] for r in ledger if r["status"] == "ok"}
    inc["covered"] = inc.source_urls.apply(lambda us: any(u in ok for u in us))
    cov_prov = inc.groupby("province").covered.agg(rows="size", readable="sum")
    cov_prov["rate"] = cov_prov.readable / cov_prov.rows
    cov_lvl = inc.groupby(inc.venue_level.fillna("unknown")).covered.agg(rows="size", readable="sum")
    cov_lvl["rate"] = cov_lvl.readable / cov_lvl.rows
    single = coded[coded.n_events == 1]

    agree = count_agreement(docs)
    lag = reporting_lag(docs)
    fetch_day = {r["url"]: (r.get("fetched_at") or "")[:10] for r in ledger}
    # A page whose "date" is the crawl day was dated by the extractor, not the newsroom.
    lag["date_is_crawl_day"] = [p == fetch_day.get(u) for p, u in zip(lag.publish_date, lag.url)]
    agree.to_csv(OUT / "count_agreement.csv", index=False, encoding="utf-8")
    lag.to_csv(OUT / "reporting_lag.csv", index=False, encoding="utf-8")

    cert = coded.certainty.value_counts().rename("docs").to_frame()
    cert["share"] = cert.docs / len(coded)
    cert_lvl = pd.crosstab(single.venue_level, single.certainty, normalize="index").assign(
        n=single.venue_level.value_counts())
    cert_lvl = cert_lvl[cert_lvl.n >= 15]
    cert_q = pd.crosstab(single.quarter, single.certainty, normalize="index").assign(n=single.quarter.value_counts())
    trend_cols = ["response: SPPG halted / closed", "response: SLHS / hygiene certificate", "response: KLB declared / mentioned",
                  "response: apology", "response: police involved", "hedged_title"]
    trend = single.groupby("quarter")[trend_cols].mean()
    trend.columns = [c.replace("response: ", "") for c in trend.columns]
    trend.insert(0, "n", single.quarter.value_counts())

    verdicts = agree.verdict.value_counts().rename("events").to_frame()
    verdicts["share"] = verdicts.events / verdicts.events.sum()
    lag_b = pd.cut(lag.lag_days, [-10**6, -1, 0, 1, 3, 7, 30, 10**6],
                   labels=["before event", "same day", "next day", "2-3 days", "4-7 days", "8-30 days", ">30 days"])
    lag_t = lag_b.value_counts().reindex(["before event", "same day", "next day", "2-3 days", "4-7 days", "8-30 days", ">30 days"]).rename("docs").to_frame()
    lag_t["share"] = lag_t.docs / lag_t.docs.sum()
    long_lag = lag[lag.lag_days > 30]

    no_cause = coded[coded.certainty == "no cause information"]
    mech_cols = [c for c in coded.columns if c.startswith("mech: ") and "denies" not in c]
    food_cols = [c for c in coded.columns if c.startswith("food suspected: ")]
    suspects = no_cause[mech_cols + food_cols].any(axis=1)
    foods = share(coded, "food mentioned: ").join(share(coded, "food suspected: "), rsuffix=" (suspected)")
    foods["suspected / mentioned"] = foods["docs (suspected)"] / foods["docs"]
    tests = significance_tests(single)

    parts = [
        "# NLP exploration of the MBG corpus\n",
        f"Generated by `analysis/nlp_explore.py`. {len(coded)} documents, {int(coded.n_events.eq(1).sum())} linked to exactly one event "
        f"(group comparisons use those). Coverage: {int(inc.covered.sum())} of {len(inc)} table rows have a readable article.\n",
        "## How to read this\n\n"
        "- Most articles are written within a day or two of the incident (section 5), so \"awaiting lab\" reflects "
        "*when the article was written*, not that no cause was ever found. Nothing here says what the eventual lab results were.\n"
        "- Symptom and food shares are what reporters chose to mention, not clinical incidence.\n"
        "- Lexicon precision was checked by reading matches (`--examples`); recall was not measured, so the small lab-result "
        "counts are floors.\n"
        "- Differences are only worth quoting if they survive the Bonferroni column in section 6.\n\n"
        "## 0. What the corpus can and cannot represent\n\nShare of table rows with a readable cited article:\n",
        "**By venue level** (fairly even, so level comparisons are defensible):\n", md_table(cov_lvl.round(2)),
        "\n**By province, largest 12** (varies a lot, so per-province text comparisons are NOT defensible):\n",
        md_table(cov_prov.sort_values("rows", ascending=False).head(12).round(2)),
        "\n## 1. What do reports say about cause?\n\nEpistemic state, strongest evidence found in the article "
        f"(n={len(coded)}):\n", md_table(cert),
        "\nBy venue level (single-event docs, groups with n>=15):\n", md_table(cert_lvl.round(2)),
        "\nBy quarter of publication (n shown; treat n<20 as anecdote):\n", md_table(cert_q.round(2)),
        f"\nHedging language (diduga/dugaan/dicurigai/kemungkinan): body {coded.hedged_body.mean():.0%}, title {coded.hedged_title.mean():.0%}.\n",
        "\n### Mechanisms named in the text (documents; a document can name several)\n", md_table(share(coded, "mech: ")),
        f"\nOf the {len(no_cause)} documents with no cause information, {int(suspects.sum())} still name a candidate mechanism "
        "or a suspect food (unverified), and the rest name none.\n",
        "\n### Foods (mentioned anywhere vs. in a sentence with a suspicion cue)\n",
        md_table(foods.round(2)),
        "\n## 2. Symptoms and care\n", md_table(share(coded, "symptom: ")), "\n", md_table(share(coded, "care: ")),
        "\nSymptom share by venue level (single-event docs, n>=15):\n", md_table(by_group(single, "venue_level", "symptom: ").round(2)),
        "\n## 3. Institutional response\n", md_table(share(coded, "response: ")),
        "\nTrend by publication quarter, share of documents (single-event docs):\n", md_table(trend.round(2)),
        "\n## 4. Does the article agree with the table's victim count?\n",
        f"Events with a point count and a single-event article (n={len(agree)}):\n", md_table(verdicts),
        "\nLargest disagreements where the article reports MORE than the table (see `count_agreement.csv`):\n",
        md_table(agree[agree.verdict == "article reports more than table"].sort_values("ratio", ascending=False).head(8)[["table", "article_max", "ratio", "title"]], index=False),
        "\n## 5. Reporting lag (publication date minus incident date, day-precision events)\n",
        f"n={len(lag)}, median {lag.lag_days.median():.0f} days.\n", md_table(lag_t),
        f"\nOf the {len(long_lag)} documents over 30 days, {int(long_lag.date_is_crawl_day.sum())} carry the crawl day as their date "
        "(extractor artefact). Negative lags are citation-date typos on the source page; see `reporting_lag.csv`.\n",
        "\n## 6. Which differences are statistically credible?\n\nFisher exact tests on single-event documents. "
        "Bonferroni-adjusted p is what to read when several comparisons are made.\n", md_table(tests, index=False),
    ]
    (OUT / "report.md").write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT / 'report.md'}")
    return 0


def show_examples(docs: pd.DataFrame, n: int) -> None:
    """Print matched snippets per lexicon, so precision can be judged by reading."""
    def first_hit(rx, text):
        if rx is LAB_POSITIVE:      # judged the way certainty() judges it: per sentence, minus hedges
            for sent in sentences(text):
                if reports_lab_finding(sent):
                    return sent[:220]
            return None
        m = rx.search(text)
        return text[max(0, m.start() - 60):m.end() + 60].replace("\n", " ") if m else None

    banks = {"LAB_POSITIVE (after sentence filter)": LAB_POSITIVE, "AWAITING": AWAITING, "SAMPLE": SAMPLE,
             **{f"MECH {k}": v for k, v in MECHANISM.items()}, **{f"RESP {k}": v for k, v in RESPONSE.items()}}
    for name, rx in banks.items():
        hits = [h for h in (first_hit(rx, t) for t in docs["text"]) if h]
        print(f"\n== {name}: {len(hits)} docs")
        for h in hits[:: max(1, len(hits) // n)][:n]:
            print("  ...", h)


if __name__ == "__main__":
    sys.exit(main())
