"""
relay/evidence.py
Evidence gate: validates that retrieved passages are sufficient,
and validates that model-generated answers cite only real retrieved passages.

Citation format: [1706.03762v7:B:p004:c002]
Validation: syntactic only. Factual entailment requires human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Regex for valid citation format: [arxiv_id v version : setting : p page : c chunk]
# Matches: [1706.03762v7:B:p004:c002] or general [chunk_id]
CITATION_RE = re.compile(
    r"\[(\d{4}\.\d{5}v\d+:[A-Za-z0-9]+:p\d{3}:c\d{3})\]"
)
# Also catch any bracketed pattern that looks like a chunk ID attempt
ANY_CITATION_RE = re.compile(
    r"\[([a-zA-Z0-9\._\-]+:[a-zA-Z0-9\._\-]+:p\d+:c\d+)\]"
)


@dataclass
class EvidenceResult:
    sufficient: bool
    num_passages: int
    reason: str


@dataclass
class ValidationResult:
    valid: bool
    cited_ids: list[str]
    invalid_ids: list[str]       # IDs not in retrieved set or malformed
    unresolvable_ids: list[str]  # IDs that don't exist in any index
    quote_issues: list[str]      # verbatim quote claims that couldn't be verified
    reason: str


def evidence_gate(passages: list, min_passages: int = 1) -> EvidenceResult:
    """
    Determine if retrieved passages are sufficient to attempt answering.
    Returns EvidenceResult.
    """
    if not passages:
        return EvidenceResult(
            sufficient=False,
            num_passages=0,
            reason="No passages retrieved",
        )
    if len(passages) < min_passages:
        return EvidenceResult(
            sufficient=False,
            num_passages=len(passages),
            reason=f"Only {len(passages)} passages retrieved, need {min_passages}",
        )
    # Check passages have non-trivial text
    total_text = sum(len(p.text.strip()) for p in passages)
    if total_text < 50:
        return EvidenceResult(
            sufficient=False,
            num_passages=len(passages),
            reason="Retrieved passages have too little text",
        )
    return EvidenceResult(
        sufficient=True,
        num_passages=len(passages),
        reason=f"OK: {len(passages)} passages, {total_text} chars",
    )


def validate_answer(
    text: str,
    retrieved_passage_ids: list[str],
    index=None,
) -> ValidationResult:
    """
    Validate that all citation IDs in `text` belong to the current retrieved set.
    Does NOT prove semantic correctness — that requires human review.

    Args:
        text: Generated answer text
        retrieved_passage_ids: Chunk IDs from current retrieval (the only valid set)
        index: Optional PaperIndex to verify chunk ID existence in corpus
    """
    retrieved_set = set(retrieved_passage_ids)
    cited_ids = CITATION_RE.findall(text)

    invalid_ids = [cid for cid in cited_ids if cid not in retrieved_set]
    valid_cited = [cid for cid in cited_ids if cid in retrieved_set]

    # Check if IDs are resolvable in the index
    unresolvable = []
    if index is not None:
        for cid in valid_cited:
            p = index.lookup(cid)
            if p is None:
                unresolvable.append(cid)

    # Check verbatim quote claims
    quote_issues = _check_verbatim_quotes(text, retrieved_passage_ids, index)

    if invalid_ids:
        reason = (
            f"Answer contains {len(invalid_ids)} citation(s) not in retrieved set: "
            + ", ".join(invalid_ids[:3])
        )
        return ValidationResult(
            valid=False,
            cited_ids=cited_ids,
            invalid_ids=invalid_ids,
            unresolvable_ids=unresolvable,
            quote_issues=quote_issues,
            reason=reason,
        )

    if unresolvable:
        reason = f"Answer contains {len(unresolvable)} citation(s) not found in index"
        return ValidationResult(
            valid=False,
            cited_ids=cited_ids,
            invalid_ids=[],
            unresolvable_ids=unresolvable,
            quote_issues=quote_issues,
            reason=reason,
        )

    if not cited_ids:
        reason = "Answer contains no citations — unsafe to send unsupported claims"
        return ValidationResult(
            valid=False,
            cited_ids=[],
            invalid_ids=[],
            unresolvable_ids=[],
            quote_issues=quote_issues,
            reason=reason,
        )

    return ValidationResult(
        valid=True,
        cited_ids=cited_ids,
        invalid_ids=[],
        unresolvable_ids=[],
        quote_issues=quote_issues,
        reason="OK: all citations valid",
    )


def _check_verbatim_quotes(
    text: str, retrieved_ids: list[str], index
) -> list[str]:
    """
    Look for text wrapped in quotes and check if it appears verbatim in retrieved passages.
    Returns list of issues found. Syntactic / heuristic check only.
    """
    if index is None:
        return []

    issues = []
    # Find quoted strings > 20 chars
    quote_re = re.compile(r'"([^"]{20,})"')
    for m in quote_re.finditer(text):
        claimed_quote = m.group(1)
        found = False
        for cid in retrieved_ids:
            p = index.lookup(cid)
            if p and claimed_quote.lower() in p.text.lower():
                found = True
                break
        if not found:
            issues.append(f"Verbatim quote not found in retrieved passages: {claimed_quote[:60]!r}...")
    return issues


def build_refusal(reason: str, passages=None) -> str:
    """
    Build a safe refusal message. Optionally include raw passage excerpts
    labeled as excerpts, not as a generated explanation.
    """
    msg = (
        "I cannot find sufficient evidence in the three research papers to answer this question reliably. "
        f"({reason}) "
        "No answer is better than a wrong one."
    )
    if passages:
        msg += "\n\nRelevant excerpts from the papers (not a verified explanation):\n"
        for p in passages[:2]:
            excerpt = p.text[:300].strip()
            msg += f"\n[{p.chunk_id}] (page {p.page}, {p.title})\n> {excerpt}...\n"
    return msg


def build_capacity_notice(reset_info: str | None = None) -> str:
    """Build user-facing capacity notice for daily/minute limit exhaustion."""
    base = (
        "I can't create and verify a new explanation right now because the model service "
        "has reached its daily limit. I can show matching excerpts from the three research papers, "
        "labeled with their source pages, but they are not a checked explanation. "
    )
    if reset_info:
        base += f"Please retry once the service is available again ({reset_info})."
    else:
        base += "Please retry once the service is available again."
    return base


def extract_citation_ids(text: str) -> list[str]:
    """Extract all citation IDs from answer text."""
    return CITATION_RE.findall(text)
