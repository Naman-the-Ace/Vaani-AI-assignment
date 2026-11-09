"""
tests/test_evidence.py
Tests for evidence gate and citation validation.
"""

import pytest
from relay.evidence import (
    evidence_gate,
    validate_answer,
    build_refusal,
    build_capacity_notice,
    extract_citation_ids,
    CITATION_RE,
)
from relay.papers import Passage


def make_passage(chunk_id: str, text: str = "Sample evidence text about attention mechanisms.", page: int = 1) -> Passage:
    parts = chunk_id.split(":")
    return Passage(
        chunk_id=chunk_id,
        arxiv_id=parts[0],
        version=int(parts[0].split("v")[1]) if "v" in parts[0] else 7,
        setting=parts[1] if len(parts) > 1 else "B",
        page=page,
        chunk_index=0,
        text=text,
        title="Test Paper",
        source_url="https://arxiv.org/pdf/test",
    )


VALID_ID_1 = "1706.03762v7:B:p004:c002"
VALID_ID_2 = "2410.00037v1:B:p010:c005"
INVALID_ID = "9999.99999v99:Z:p999:c999"


class TestEvidenceGate:
    def test_no_passages_insufficient(self):
        result = evidence_gate([])
        assert not result.sufficient
        assert result.num_passages == 0

    def test_one_passage_sufficient(self):
        p = make_passage(VALID_ID_1, text="x" * 100)
        result = evidence_gate([p])
        assert result.sufficient

    def test_tiny_text_insufficient(self):
        p = make_passage(VALID_ID_1, text="ok")  # too short
        result = evidence_gate([p])
        assert not result.sufficient

    def test_multiple_passages_sufficient(self):
        passages = [make_passage(VALID_ID_1, text="a" * 100), make_passage(VALID_ID_2, text="b" * 100)]
        result = evidence_gate(passages)
        assert result.sufficient
        assert result.num_passages == 2


class TestValidateAnswer:
    def test_valid_citation_in_retrieved_set(self):
        answer = f"This is explained in [{VALID_ID_1}]."
        result = validate_answer(answer, [VALID_ID_1])
        assert result.valid
        assert VALID_ID_1 in result.cited_ids

    def test_citation_not_in_retrieved_set(self):
        answer = f"This is explained in [{INVALID_ID}]."
        result = validate_answer(answer, [VALID_ID_1])
        assert not result.valid
        assert INVALID_ID in result.invalid_ids

    def test_citation_from_different_paper(self):
        """Citation from paper not in retrieved set must be rejected."""
        other_id = "2507.07955v2:B:p001:c001"
        answer = f"As per [{other_id}], chunking is hierarchical."
        result = validate_answer(answer, [VALID_ID_1])
        assert not result.valid

    def test_no_citations_invalid(self):
        """Answer with no citations at all is invalid."""
        answer = "Attention uses softmax over queries and keys."
        result = validate_answer(answer, [VALID_ID_1])
        assert not result.valid
        assert "no citations" in result.reason.lower()

    def test_multiple_valid_citations(self):
        answer = f"First point [{VALID_ID_1}]. Second point [{VALID_ID_2}]."
        result = validate_answer(answer, [VALID_ID_1, VALID_ID_2])
        assert result.valid
        assert len(result.cited_ids) == 2

    def test_citation_regex_format(self):
        """Test that the citation regex matches the correct format."""
        text = f"See [{VALID_ID_1}] for details."
        ids = extract_citation_ids(text)
        assert ids == [VALID_ID_1]

    def test_malformed_citation_not_matched(self):
        text = "[1706.03762v7:B:p04:c2]"  # wrong padding
        ids = extract_citation_ids(text)
        assert ids == []

    def test_citation_wrong_version_rejected(self):
        """A citation with wrong version should not match if retrieved set uses different version."""
        wrong_version_id = "1706.03762v5:B:p004:c002"  # v5 not v7
        answer = f"See [{wrong_version_id}]."
        result = validate_answer(answer, [VALID_ID_1])
        assert not result.valid  # v5 not in retrieved set


class TestRefusalBuilders:
    def test_build_refusal_no_passages(self):
        msg = build_refusal("No evidence found")
        assert "cannot find" in msg.lower() or "insufficient" in msg.lower() or "No answer" in msg.lower()

    def test_build_refusal_with_passages(self):
        p = make_passage(VALID_ID_1, text="Relevant text from paper." * 10)
        msg = build_refusal("Test reason", passages=[p])
        assert VALID_ID_1 in msg
        assert "excerpt" in msg.lower()

    def test_capacity_notice_no_reset(self):
        msg = build_capacity_notice()
        assert "daily limit" in msg.lower() or "service" in msg.lower()

    def test_capacity_notice_with_reset(self):
        msg = build_capacity_notice("in ~2 hours")
        assert "in ~2 hours" in msg
