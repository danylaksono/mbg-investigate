"""Stage 5 — assemble the fetched articles into an analysis-ready corpus.

The problem this exists to solve is syndication. A single Antara or Reuters wire
story is republished verbatim across a dozen Indonesian outlets, and regional
desks re-run each other's copy with a changed headline. Counting those as a
dozen documents inflates every frequency you will later compute — term counts,
outlet shares, coverage-by-province — by whatever the syndication factor happens
to be in that region, which is not constant.

So documents are clustered by content similarity, one canonical member is marked
per cluster, and the rest are kept (not deleted) with a pointer to the canonical.
Analyse canonicals; use the cluster size when you want to measure reach.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from .articles import iso_date, original_of

RE_NONWORD = re.compile(r"[^\w\s]", re.UNICODE)
RE_WS = re.compile(r"\s+")

SHINGLE_K = 5
NEAR_DUPLICATE_THRESHOLD = 0.70
# Shingles appearing in more than this share of documents are boilerplate
# ("baca juga", "advertisement") and carry no signal for matching.
MAX_SHINGLE_DOC_RATIO = 0.35


@dataclass
class Document:
    doc_id: str
    url: str                        # what was fetched: the archive copy when the citation gave one
    original_url: str = ""
    domain: str = ""
    title: str = ""
    author: str = ""
    publish_date: str = ""
    n_chars: int = 0
    text: str = ""
    ref_ids: list[str] = field(default_factory=list)
    incident_ids: list[str] = field(default_factory=list)
    via: str = ""
    status: str = ""
    content_hash: str = ""
    # Does the article name the kabupaten/kota of the row that cites it? None when no
    # row links here. False flags a citation pointing at the wrong story, which the
    # source table does contain (an Aceh Timur row citing a Cianjur article).
    mentions_incident_place: Optional[bool] = None
    cluster_id: str = ""
    is_canonical: bool = True
    cluster_size: int = 1
    duplicate_of: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def normalise(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return RE_WS.sub(" ", RE_NONWORD.sub(" ", text.lower())).strip()


def mentions_place(text: str, places: list[str]) -> Optional[bool]:
    places = [normalise(p) for p in places if p]
    if not places:
        return None
    haystack = f" {normalise(text)} "
    return any(f" {p} " in haystack for p in places)


def content_hash(text: str) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def shingles(text: str, k: int = SHINGLE_K) -> set[str]:
    """Word k-grams. Word-level rather than character-level because Indonesian
    affixation makes character shingles noisy across otherwise identical copy."""
    words = normalise(text).split()
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    return intersection / (len(a) + len(b) - intersection)


def load_documents(
    ledger_path: Path,
    statuses: tuple[str, ...] = ("ok",),
    incidents_csv: Optional[Path] = None,
) -> list[Document]:
    """Read the stage-4 ledger and its cached text into Document records.

    `statuses` defaults to fetched-and-substantial only. Widen it to
    ("ok", "short_body") if you want to eyeball the truncated ones.
    """
    url_to_incidents: dict[str, list[str]] = {}
    place_of: dict[str, str] = {}
    if incidents_csv and Path(incidents_csv).exists():
        with open(incidents_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                place_of[row["incident_id"]] = row.get("kabkota_name", "")
                for url in json.loads(row.get("source_urls") or "[]"):
                    url_to_incidents.setdefault(url, []).append(row["incident_id"])

    docs: list[Document] = []
    with open(ledger_path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("status") not in statuses:
                continue
            cache_path = row.get("cache_path", "")
            text_path = Path(cache_path).with_suffix("").with_suffix(".txt") if cache_path else None
            if not text_path or not text_path.exists():
                continue
            text = text_path.read_text(encoding="utf-8")
            incident_ids = url_to_incidents.get(row["url"], [])
            docs.append(
                Document(
                    doc_id=hashlib.sha1(row["url"].encode()).hexdigest()[:12],
                    url=row["url"],
                    original_url=original_of(row["url"]),
                    domain=row.get("domain", ""),
                    title=row.get("title", ""),
                    author=row.get("author", ""),
                    publish_date=iso_date(row.get("publish_date", "")) or iso_date(row.get("extracted_date", "")),
                    n_chars=len(text),
                    text=text,
                    ref_ids=row.get("ref_ids", []),
                    incident_ids=incident_ids,
                    mentions_incident_place=mentions_place(
                        text, [place_of.get(i, "") for i in incident_ids]),
                    via=row.get("via", ""),
                    status=row.get("status", ""),
                    content_hash=content_hash(text),
                )
            )
    return docs


def cluster_duplicates(
    docs: list[Document],
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
    k: int = SHINGLE_K,
) -> list[Document]:
    """Group exact and near duplicates, mark one canonical per group.

    Exact matches collapse on a normalised content hash. Near matches use Jaccard
    over word k-grams, with candidate generation via an inverted index so this
    stays tractable: only documents sharing a non-boilerplate shingle are ever
    compared. The canonical is the earliest-dated member, falling back to the
    longest — earliest because in a syndication chain the original usually ran
    first, and later copies get trimmed.
    """
    if not docs:
        return docs

    sig = {d.doc_id: shingles(d.text, k) for d in docs}

    # Drop shingles that appear nearly everywhere: site furniture, not content.
    df: dict[str, int] = {}
    for s in sig.values():
        for shingle in s:
            df[shingle] = df.get(shingle, 0) + 1
    cutoff = max(2, int(len(docs) * MAX_SHINGLE_DOC_RATIO))
    index: dict[str, list[str]] = {}
    for doc_id, s in sig.items():
        for shingle in s:
            if df[shingle] <= cutoff:
                index.setdefault(shingle, []).append(doc_id)

    parent: dict[str, str] = {d.doc_id: d.doc_id for d in docs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_hash: dict[str, str] = {}
    for d in docs:
        # Never trust an upstream field to be populated. An unset content_hash is
        # empty string on every document, which would make them all exact matches
        # and collapse the entire corpus into one cluster.
        if not d.content_hash:
            d.content_hash = content_hash(d.text)
        if d.content_hash in by_hash:
            union(by_hash[d.content_hash], d.doc_id)
        else:
            by_hash[d.content_hash] = d.doc_id

    for d in docs:
        candidates: set[str] = set()
        for shingle in sig[d.doc_id]:
            candidates.update(index.get(shingle, ()))
        candidates.discard(d.doc_id)
        for other in candidates:
            if find(d.doc_id) == find(other):
                continue
            if jaccard(sig[d.doc_id], sig[other]) >= threshold:
                union(d.doc_id, other)

    groups: dict[str, list[Document]] = {}
    by_id = {d.doc_id: d for d in docs}
    for doc_id in parent:
        groups.setdefault(find(doc_id), []).append(by_id[doc_id])

    for root, members in groups.items():
        members.sort(key=lambda d: (d.publish_date or "9999", -d.n_chars))
        canonical = members[0]
        for d in members:
            d.cluster_id = f"c_{root}"
            d.cluster_size = len(members)
            d.is_canonical = d.doc_id == canonical.doc_id
            d.duplicate_of = None if d.is_canonical else canonical.doc_id
    return docs


def build_corpus(
    ledger_path: Path,
    out_path: Path,
    incidents_csv: Optional[Path] = None,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
    statuses: tuple[str, ...] = ("ok",),
) -> dict:
    docs = load_documents(Path(ledger_path), statuses, incidents_csv)
    docs = cluster_duplicates(docs, threshold)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for d in sorted(docs, key=lambda d: (d.publish_date or "", d.doc_id)):
            fh.write(json.dumps(d.as_dict(), ensure_ascii=False) + "\n")

    canonical = [d for d in docs if d.is_canonical]
    clustered = [d for d in docs if d.cluster_size > 1]
    return {
        "documents": len(docs),
        "canonical_documents": len(canonical),
        "duplicates_absorbed": len(docs) - len(canonical),
        "clusters_with_syndication": len({d.cluster_id for d in clustered}),
        "largest_cluster": max((d.cluster_size for d in docs), default=0),
        "documents_linked_to_incidents": sum(1 for d in docs if d.incident_ids),
        "linked_documents_not_mentioning_the_row_place": sum(
            1 for d in docs if d.mentions_incident_place is False),
        "domains": len({d.domain for d in docs}),
        "median_chars": sorted(d.n_chars for d in docs)[len(docs) // 2] if docs else 0,
    }
