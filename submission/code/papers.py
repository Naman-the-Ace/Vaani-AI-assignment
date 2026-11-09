"""
relay/papers.py
PDF ingestion, chunking, BM25 retrieval, and citation lookup.

EXACT PINNED VERSIONS (do not change):
  1706.03762v7  Attention Is All You Need
  2410.00037v1  Moshi
  2507.07955v2  Dynamic Chunking

Chunk ID format: {arxiv_id}v{version}:{setting}:p{page:03d}:c{chunk:03d}
e.g.  1706.03762v7:B:p004:c002
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PINNED_PAPERS = [
    {
        "arxiv_id": "1706.03762",
        "version": 7,
        "url": "https://arxiv.org/pdf/1706.03762v7",
        "abstract_url": "https://arxiv.org/abs/1706.03762v7",
        "title": "Attention Is All You Need",
    },
    {
        "arxiv_id": "2410.00037",
        "version": 1,
        "url": "https://arxiv.org/pdf/2410.00037v1",
        "abstract_url": "https://arxiv.org/abs/2410.00037v1",
        "title": "Moshi: a speech-text foundation model for real-time dialogue",
    },
    {
        "arxiv_id": "2507.07955",
        "version": 2,
        "url": "https://arxiv.org/pdf/2507.07955v2",
        "abstract_url": "https://arxiv.org/abs/2507.07955v2",
        "title": "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling",
    },
]

# Retrieval settings (frozen before evaluation)
SETTINGS = {
    "A": {"chunk_tokens": 300, "overlap_pct": 0.10, "bm25_candidates": 2, "final_top_k": 2, "rerank": False},
    "B": {"chunk_tokens": 500, "overlap_pct": 0.10, "bm25_candidates": 3, "final_top_k": 3, "rerank": False},
    "C": {"chunk_tokens": 700, "overlap_pct": 0.10, "bm25_candidates": 12, "final_top_k": 4, "rerank": True},
}

DATA_DIR = Path(__file__).parent.parent / "data"
PAPERS_DIR = DATA_DIR / "papers"
PASSAGES_DIR = DATA_DIR / "passages"
HASH_FILE = DATA_DIR / "paper_hashes.json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Passage:
    chunk_id: str           # e.g. "1706.03762v7:B:p004:c002"
    arxiv_id: str
    version: int
    setting: str
    page: int               # 1-based PDF physical page
    chunk_index: int        # 0-based index within page
    text: str
    title: str
    source_url: str
    char_start: int = 0
    char_end: int = 0
    section_hint: str = ""

    def short_id(self) -> str:
        return self.chunk_id

    def to_dict(self) -> dict:
        return asdict(self)

    def versioned_key(self) -> str:
        return f"{self.arxiv_id}v{self.version}"


@dataclass
class PaperMeta:
    arxiv_id: str
    version: int
    title: str
    url: str
    sha256: str
    download_ts: str
    num_pages: int
    extraction_notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Download & verify
# ---------------------------------------------------------------------------

def _paper_filename(arxiv_id: str, version: int) -> Path:
    return PAPERS_DIR / f"{arxiv_id}v{version}.pdf"


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_verify_papers(force: bool = False) -> dict[str, PaperMeta]:
    """Download the three pinned PDFs and verify them. Returns metadata dict keyed by arxiv_id+version."""
    import httpx

    PAPERS_DIR.mkdir(parents=True, exist_ok=True)

    hashes: dict = {}
    if HASH_FILE.exists():
        with open(HASH_FILE) as f:
            hashes = json.load(f)

    metas = {}
    for paper in PINNED_PAPERS:
        arxiv_id = paper["arxiv_id"]
        version = paper["version"]
        key = f"{arxiv_id}v{version}"
        dest = _paper_filename(arxiv_id, version)

        if dest.exists() and not force:
            logger.info(f"Already downloaded: {dest.name}")
        else:
            logger.info(f"Downloading {paper['url']} ...")
            headers = {"User-Agent": "vaani-relay/0.1 (research; arxiv fetch)"}
            with httpx.Client(timeout=120, follow_redirects=True, headers=headers) as client:
                for attempt in range(3):
                    try:
                        resp = client.get(paper["url"])
                        resp.raise_for_status()
                        PAPERS_DIR.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(resp.content)
                        logger.info(f"Downloaded {dest.name} ({len(resp.content)} bytes)")
                        break
                    except Exception as e:
                        if attempt == 2:
                            raise RuntimeError(f"Failed to download {paper['url']}: {e}") from e
                        time.sleep(2 ** attempt)

        # Verify it's a valid PDF and check version
        sha = _compute_sha256(dest)
        num_pages, notes = _verify_pdf_version(dest, arxiv_id, version)

        meta = PaperMeta(
            arxiv_id=arxiv_id,
            version=version,
            title=paper["title"],
            url=paper["url"],
            sha256=sha,
            download_ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            num_pages=num_pages,
            extraction_notes=notes,
        )
        metas[key] = meta
        hashes[key] = {
            "sha256": sha,
            "title": paper["title"],
            "url": paper["url"],
            "version": version,
            "num_pages": num_pages,
            "download_ts": meta.download_ts,
            "extraction_notes": notes,
        }

    HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HASH_FILE, "w") as f:
        json.dump(hashes, f, indent=2)
    logger.info(f"Paper hashes saved to {HASH_FILE}")
    return metas


def _verify_pdf_version(path: Path, arxiv_id: str, version: int) -> tuple[int, list[str]]:
    """Open PDF, check it's readable, and try to confirm version. Returns (num_pages, notes)."""
    import pymupdf as fitz
    notes = []
    try:
        doc = fitz.open(str(path))
        num_pages = len(doc)
        # Check first page text for arxiv ID or title
        first_text = doc[0].get_text()[:2000]
        if arxiv_id.replace(".", "") not in first_text and arxiv_id not in first_text:
            notes.append(f"WARNING: arxiv ID {arxiv_id} not found in page 1 text")
        # Check metadata
        meta = doc.metadata
        if meta.get("title"):
            notes.append(f"PDF title: {meta.get('title', '')!r}")
        doc.close()
        return num_pages, notes
    except Exception as e:
        notes.append(f"ERROR verifying PDF: {e}")
        return 0, notes


# ---------------------------------------------------------------------------
# Text extraction & chunking
# ---------------------------------------------------------------------------

def _tokenize_approx(text: str) -> list[str]:
    """Approximate word-level tokenization for chunk sizing."""
    return text.split()


def _extract_page_text(doc, page_idx: int) -> tuple[str, str]:
    """Extract text from a page. Returns (text, section_hint)."""
    page = doc[page_idx]
    text = page.get_text()
    # Simple section hint: look for heading-like lines
    lines = text.split("\n")
    section_hint = ""
    for line in lines[:10]:
        line = line.strip()
        if len(line) > 3 and len(line) < 80 and line[0].isupper():
            # Might be a heading
            section_hint = line[:60]
            break
    return text, section_hint


def build_passages(settings: dict = SETTINGS, force: bool = False) -> dict[str, list[Passage]]:
    """
    Build passage corpora for all retrieval settings.
    Returns dict mapping setting_key -> list[Passage].
    Saves passages to data/passages/{setting}.jsonl.
    """
    import pymupdf as fitz

    PASSAGES_DIR.mkdir(parents=True, exist_ok=True)

    result: dict[str, list[Passage]] = {}

    for setting_key, cfg in settings.items():
        out_file = PASSAGES_DIR / f"{setting_key}.jsonl"
        if out_file.exists() and not force:
            logger.info(f"Loading cached passages for setting {setting_key}")
            passages = []
            with open(out_file) as f:
                for line in f:
                    d = json.loads(line)
                    passages.append(Passage(**d))
            result[setting_key] = passages
            continue

        passages = []
        for paper in PINNED_PAPERS:
            arxiv_id = paper["arxiv_id"]
            version = paper["version"]
            path = _paper_filename(arxiv_id, version)
            if not path.exists():
                raise FileNotFoundError(f"Paper not found: {path}. Run `ingest` first.")

            doc = fitz.open(str(path))
            chunk_tokens = cfg["chunk_tokens"]
            overlap_pct = cfg["overlap_pct"]
            overlap_tokens = int(chunk_tokens * overlap_pct)

            chunk_idx = 0
            for page_idx in range(len(doc)):
                page_num = page_idx + 1  # 1-based
                text, section_hint = _extract_page_text(doc, page_idx)
                words = _tokenize_approx(text)

                if not words:
                    continue

                stride = max(1, chunk_tokens - overlap_tokens)
                pos = 0
                while pos < len(words):
                    chunk_words = words[pos: pos + chunk_tokens]
                    chunk_text = " ".join(chunk_words)

                    # Compute character offsets (approximate)
                    char_start = text.find(chunk_words[0]) if chunk_words else 0
                    char_end = char_start + len(chunk_text)

                    chunk_id = f"{arxiv_id}v{version}:{setting_key}:p{page_num:03d}:c{chunk_idx:03d}"
                    p = Passage(
                        chunk_id=chunk_id,
                        arxiv_id=arxiv_id,
                        version=version,
                        setting=setting_key,
                        page=page_num,
                        chunk_index=chunk_idx,
                        text=chunk_text,
                        title=paper["title"],
                        source_url=paper["url"],
                        char_start=char_start,
                        char_end=char_end,
                        section_hint=section_hint,
                    )
                    passages.append(p)
                    chunk_idx += 1
                    pos += stride

            doc.close()

        result[setting_key] = passages
        with open(out_file, "w") as f:
            for p in passages:
                f.write(json.dumps(p.to_dict()) + "\n")
        logger.info(f"Built {len(passages)} passages for setting {setting_key}")

    return result


# ---------------------------------------------------------------------------
# BM25 Index & search
# ---------------------------------------------------------------------------

class PaperIndex:
    """BM25 index over passages for one retrieval setting."""

    def __init__(self, passages: list[Passage], setting: str):
        from rank_bm25 import BM25Okapi
        self.passages = passages
        self.setting = setting
        cfg = SETTINGS[setting]
        self.bm25_candidates = cfg["bm25_candidates"]
        self.final_top_k = cfg["final_top_k"]
        self.do_rerank = cfg["rerank"]

        # Tokenize for BM25
        tokenized = [self._tokenize(p.text) for p in passages]
        self.bm25 = BM25Okapi(tokenized)
        self._id_to_passage: dict[str, Passage] = {p.chunk_id: p for p in passages}

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Simple whitespace+lowercase tokenization for BM25."""
        return re.findall(r"\w+", text.lower())

    def search(self, query: str, top_k: int | None = None) -> list[Passage]:
        """Search and return top passages for query."""
        tokens = self._tokenize(query)
        scores = self.bm25.get_scores(tokens)

        # Get BM25 candidates
        n_candidates = self.bm25_candidates if top_k is None else top_k
        indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n_candidates]
        candidates = [(self.passages[i], scores[i]) for i in indices if scores[i] > 0]

        if self.do_rerank:
            candidates = self._bm25_diversify_rerank(candidates, query)

        final_k = self.final_top_k if top_k is None else top_k
        return [p for p, _ in candidates[:final_k]]

    def _bm25_diversify_rerank(
        self, candidates: list[tuple[Passage, float]], query: str
    ) -> list[tuple[Passage, float]]:
        """
        Local deterministic reranking: Maximal Marginal Relevance-like diversification
        using token overlap as a proxy for redundancy. NOT a neural reranker.
        Returns reranked list of (Passage, score).
        """
        if not candidates:
            return candidates

        query_tokens = set(self._tokenize(query))
        selected: list[tuple[Passage, float]] = []
        remaining = list(candidates)
        final_k = self.final_top_k

        while remaining and len(selected) < final_k:
            # Score = BM25_score * (1 - max_overlap_with_selected)
            best_idx = 0
            best_mmr = -1.0
            for idx, (p, score) in enumerate(remaining):
                p_tokens = set(self._tokenize(p.text))
                # Relevance: normalized BM25
                rel = score / (candidates[0][1] + 1e-9)
                # Redundancy: max Jaccard with already-selected
                if selected:
                    max_sim = max(
                        len(p_tokens & set(self._tokenize(s.text))) /
                        max(1, len(p_tokens | set(self._tokenize(s.text))))
                        for s, _ in selected
                    )
                else:
                    max_sim = 0.0
                mmr = 0.5 * rel - 0.5 * max_sim
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = idx
            selected.append(remaining.pop(best_idx))

        return selected

    def lookup(self, chunk_id: str) -> Passage | None:
        return self._id_to_passage.get(chunk_id)

    def validate_chunk_ids(self, chunk_ids: list[str]) -> tuple[list[str], list[str]]:
        """Return (valid_ids, invalid_ids)."""
        valid = [cid for cid in chunk_ids if cid in self._id_to_passage]
        invalid = [cid for cid in chunk_ids if cid not in self._id_to_passage]
        return valid, invalid


# ---------------------------------------------------------------------------
# Global index registry
# ---------------------------------------------------------------------------

_indices: dict[str, PaperIndex] = {}
_passages_cache: dict[str, list[Passage]] = {}


def build_index(setting: str = "B", force: bool = False) -> PaperIndex:
    """Build or return cached PaperIndex for a setting."""
    if setting in _indices and not force:
        return _indices[setting]
    if setting not in _passages_cache or force:
        _passages_cache.update(build_passages({setting: SETTINGS[setting]}, force=force))
    passages = _passages_cache[setting]
    idx = PaperIndex(passages, setting)
    _indices[setting] = idx
    logger.info(f"Built BM25 index for setting {setting}: {len(passages)} passages")
    return idx


def search_papers(query: str, setting: str = "B", top_k: int | None = None) -> list[Passage]:
    """Main retrieval function. Searches only the pinned corpus."""
    idx = build_index(setting)
    return idx.search(query, top_k=top_k)


def lookup_passage(chunk_id: str, setting: str = "B") -> Passage | None:
    """Look up a passage by its stable chunk ID."""
    idx = build_index(setting)
    return idx.lookup(chunk_id)


def get_paper_hashes() -> dict:
    if HASH_FILE.exists():
        with open(HASH_FILE) as f:
            return json.load(f)
    return {}
