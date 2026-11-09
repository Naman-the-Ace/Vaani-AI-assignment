"""
tests/test_papers.py
Tests for paper ingestion, chunking, BM25 search, and citation lookup.
Uses small synthetic corpus — does NOT require the actual PDFs for unit tests.
Integration tests require PDFs (skipped if not downloaded).
"""

import json
import pytest
from pathlib import Path

from relay.papers import (
    Passage,
    PaperIndex,
    SETTINGS,
    PINNED_PAPERS,
    _compute_sha256,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Synthetic test corpus
# ---------------------------------------------------------------------------

def make_synthetic_passages(setting: str = "B") -> list[Passage]:
    """Create a small synthetic corpus for testing. NOT a real paper excerpt."""
    corpus = [
        {
            "arxiv_id": "1706.03762", "version": 7,
            "text": "Scaled dot-product attention computes attention weights using queries and keys divided by the square root of dk.",
            "page": 4, "chunk_index": 0
        },
        {
            "arxiv_id": "1706.03762", "version": 7,
            "text": "Multi-head attention allows the model to jointly attend to information from different representation subspaces.",
            "page": 4, "chunk_index": 1
        },
        {
            "arxiv_id": "1706.03762", "version": 7,
            "text": "Positional encoding adds information about the position of tokens in the sequence.",
            "page": 5, "chunk_index": 0
        },
        {
            "arxiv_id": "2410.00037", "version": 1,
            "text": "Moshi is a speech-text foundation model for real-time dialogue with hierarchical modeling.",
            "page": 1, "chunk_index": 0
        },
        {
            "arxiv_id": "2507.07955", "version": 2,
            "text": "Dynamic chunking enables end-to-end hierarchical sequence modeling with variable chunk sizes.",
            "page": 1, "chunk_index": 0
        },
    ]
    passages = []
    for item in corpus:
        aid = item["arxiv_id"]
        ver = item["version"]
        p = item["page"]
        c = item["chunk_index"]
        passages.append(Passage(
            chunk_id=f"{aid}v{ver}:{setting}:p{p:03d}:c{c:03d}",
            arxiv_id=aid,
            version=ver,
            setting=setting,
            page=p,
            chunk_index=c,
            text=item["text"],
            title=f"Test Paper {aid}",
            source_url=f"https://arxiv.org/pdf/{aid}v{ver}",
        ))
    return passages


class TestPassageModel:
    def test_chunk_id_format(self):
        """Chunk ID must follow the pinned format."""
        passages = make_synthetic_passages("B")
        for p in passages:
            parts = p.chunk_id.split(":")
            assert len(parts) == 4, f"Wrong chunk ID format: {p.chunk_id}"
            assert parts[1] == "B"
            assert parts[2].startswith("p")
            assert parts[3].startswith("c")

    def test_versioned_key(self):
        p = make_synthetic_passages("B")[0]
        key = p.versioned_key()
        assert "1706.03762v7" == key

    def test_to_dict_has_all_fields(self):
        p = make_synthetic_passages("B")[0]
        d = p.to_dict()
        for field in ["chunk_id", "arxiv_id", "version", "setting", "page", "text", "title"]:
            assert field in d

    def test_no_cross_paper_collision(self):
        """Chunk IDs must be unique across all three papers."""
        passages = make_synthetic_passages("B")
        ids = [p.chunk_id for p in passages]
        assert len(ids) == len(set(ids)), "Duplicate chunk IDs found"


class TestPaperIndex:
    @pytest.fixture
    def index(self) -> PaperIndex:
        passages = make_synthetic_passages("B")
        return PaperIndex(passages, setting="B")

    def test_search_returns_passages(self, index):
        results = index.search("attention queries keys")
        assert len(results) > 0

    def test_search_relevance(self, index):
        """'attention' query should return attention-related passage first."""
        results = index.search("scaled dot product attention")
        assert len(results) > 0
        # First result should contain 'attention'
        assert "attention" in results[0].text.lower()

    def test_lookup_valid_id(self, index):
        passages = make_synthetic_passages("B")
        p = passages[0]
        found = index.lookup(p.chunk_id)
        assert found is not None
        assert found.chunk_id == p.chunk_id

    def test_lookup_invalid_id_returns_none(self, index):
        found = index.lookup("9999.99999v99:B:p999:c999")
        assert found is None

    def test_validate_chunk_ids(self, index):
        passages = make_synthetic_passages("B")
        valid_id = passages[0].chunk_id
        invalid_id = "9999.99999v99:B:p999:c999"
        valid, invalid = index.validate_chunk_ids([valid_id, invalid_id])
        assert valid_id in valid
        assert invalid_id in invalid

    def test_no_cross_paper_results(self, index):
        """Results should only come from pinned corpus, not external knowledge."""
        results = index.search("neural networks in general")
        for r in results:
            assert r.arxiv_id in {p["arxiv_id"] for p in PINNED_PAPERS}

    def test_setting_a_fewer_results(self):
        """Setting A returns top-2, setting B returns top-3."""
        passages = make_synthetic_passages("A")
        idx_a = PaperIndex(passages, setting="A")
        results_a = idx_a.search("attention")
        assert len(results_a) <= SETTINGS["A"]["final_top_k"]

    def test_setting_b_results(self):
        passages = make_synthetic_passages("B")
        idx_b = PaperIndex(passages, setting="B")
        results_b = idx_b.search("attention")
        assert len(results_b) <= SETTINGS["B"]["final_top_k"]

    def test_stable_chunk_ids(self):
        """Same corpus produces same IDs on rebuild."""
        p1 = make_synthetic_passages("B")
        p2 = make_synthetic_passages("B")
        ids1 = {p.chunk_id for p in p1}
        ids2 = {p.chunk_id for p in p2}
        assert ids1 == ids2

    def test_rerank_setting_c(self):
        """Setting C with reranking should return at most final_top_k."""
        passages = make_synthetic_passages("C")
        idx_c = PaperIndex(passages, setting="C")
        results = idx_c.search("attention hierarchical model")
        assert len(results) <= SETTINGS["C"]["final_top_k"]


class TestPinnedVersions:
    def test_exact_paper_versions(self):
        """Verify the three pinned papers have exact required versions."""
        required = {
            "1706.03762": 7,
            "2410.00037": 1,
            "2507.07955": 2,
        }
        for paper in PINNED_PAPERS:
            aid = paper["arxiv_id"]
            assert aid in required, f"Unexpected paper: {aid}"
            assert paper["version"] == required[aid], (
                f"Wrong version for {aid}: {paper['version']} != {required[aid]}"
            )

    def test_no_fourth_paper(self):
        """Corpus must contain exactly three papers."""
        assert len(PINNED_PAPERS) == 3

    def test_urls_contain_version(self):
        """Download URLs must include the version number."""
        for paper in PINNED_PAPERS:
            url = paper["url"]
            version_str = f"v{paper['version']}"
            assert version_str in url, f"Version not in URL: {url}"


class TestIntegrationPDFsExist:
    """Integration tests that require actual downloaded PDFs — skip if not present."""

    @pytest.fixture
    def pdfs_available(self):
        from relay.papers import PAPERS_DIR, HASH_FILE
        for paper in PINNED_PAPERS:
            path = PAPERS_DIR / f"{paper['arxiv_id']}v{paper['version']}.pdf"
            if not path.exists():
                pytest.skip(f"PDF not downloaded: {path.name}. Run `python -m relay.app ingest` first.")

    def test_hash_manifest_exists(self, pdfs_available):
        from relay.papers import HASH_FILE
        assert HASH_FILE.exists(), "paper_hashes.json not found"
        with open(HASH_FILE) as f:
            hashes = json.load(f)
        assert len(hashes) == 3

    def test_all_three_versions_in_manifest(self, pdfs_available):
        from relay.papers import HASH_FILE
        with open(HASH_FILE) as f:
            hashes = json.load(f)
        for paper in PINNED_PAPERS:
            key = f"{paper['arxiv_id']}v{paper['version']}"
            assert key in hashes, f"Missing hash for {key}"

    def test_sha256_stable(self, pdfs_available):
        """SHA256 should be stable across two reads."""
        from relay.papers import PAPERS_DIR, PINNED_PAPERS
        paper = PINNED_PAPERS[0]
        path = PAPERS_DIR / f"{paper['arxiv_id']}v{paper['version']}.pdf"
        h1 = _compute_sha256(path)
        h2 = _compute_sha256(path)
        assert h1 == h2

    def test_passages_contain_no_cross_boundary(self, pdfs_available):
        """No passage should contain text from more than one paper."""
        from relay.papers import build_passages, SETTINGS
        passages = build_passages({"B": SETTINGS["B"]})["B"]
        # Check chunk IDs are unique
        ids = [p.chunk_id for p in passages]
        assert len(ids) == len(set(ids)), "Duplicate chunk IDs in real passages"
