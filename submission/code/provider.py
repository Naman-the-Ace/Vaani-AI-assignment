"""
relay/provider.py
Groq adapter: tool calling, streaming, real inference.

Implements:
  - plan_tool_call: LLM-initiated search_papers tool call (normal path)
  - generate_stream: streamed answer generation from retrieved passages
  - Direct retrieval fallback (degraded path, records rung_served=1)

NEVER logs or stores GROQ_API_KEY. All secrets must come from env.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, Optional

# Automatically load .env if present
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

PREFERRED_MODEL = "openai/gpt-oss-20b"
FALLBACK_MODEL = "openai/gpt-oss-20b"
CURRENT_MODEL = "openai/gpt-oss-20b"

ANSWER_SYSTEM_PROMPT = """You are a research assistant answering questions about three specific papers.
Answer ONLY from the retrieved passages provided.
CRITICAL CITATION RULE:
Every single claim or sentence MUST include a citation bracket with the EXACT passage ID from the provided passages, for example: [1706.03762v7:B:p004:c004].
Do not state any fact without immediately attaching its citation [chunk_id].
If the passages do not contain enough information to answer, state: "The retrieved passages do not contain sufficient evidence to answer this question."
Do not use outside knowledge. Keep answers concise."""

PLAN_SYSTEM_PROMPT = """You are a question planner for a research paper Q&A system.
Analyze the user's question and invoke the search_papers function with a concise search query to find relevant passages."""

SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_papers",
        "description": "Search the research paper corpus for passages relevant to the query. Returns passage IDs and text.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to find relevant paper passages",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of passages to retrieve (1-5)",
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "required": ["query"],
        },
    },
}


@dataclass
class ProviderCall:
    """Record of a single provider API call."""
    call_id: str
    model: str
    tokens_in: int
    tokens_out: int
    reasoning_tokens: int
    provider_first_delta_ms: Optional[float]  # ms from request to first SSE chunk
    total_provider_ms: float
    status: str   # "ok" | "rate_limited" | "timeout" | "error"
    retry_after_s: Optional[float]
    error_msg: str = ""
    headers: dict = None

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "reasoning_tokens": self.reasoning_tokens,
            "provider_first_delta_ms": self.provider_first_delta_ms,
            "total_provider_ms": self.total_provider_ms,
            "status": self.status,
            "retry_after_s": self.retry_after_s,
            "error_msg": self.error_msg,
        }


@dataclass
class AnswerResult:
    text: str
    citations: list[str]
    outcome_kind: str  # "full_answer"|"extractive"|"evidence_refusal"|"capacity_notice"|"failure"
    provider_calls: list[ProviderCall]
    total_tokens_in: int
    total_tokens_out: int
    reasoning_tokens: int
    provider_first_delta_ms: Optional[float]
    user_visible_first_ms: Optional[float]  # measured at caller
    rung_served: int
    retrieval_setting: str


def _get_groq_client():
    """Get Groq client. Raises if key not available."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "GROQ_API_KEY not set. Set it as an environment variable or in .env"
        )
    from groq import Groq
    return Groq(api_key=api_key)


def _get_async_groq_client():
    """Get async Groq client."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set.")
    from groq import AsyncGroq
    return AsyncGroq(api_key=api_key)


def check_api_key_exists() -> bool:
    """Boolean check only — never logs the key."""
    return bool(os.environ.get("GROQ_API_KEY", "").strip())


def get_model() -> str:
    """Return the model to use, checking preference at runtime."""
    global CURRENT_MODEL
    if CURRENT_MODEL:
        return CURRENT_MODEL
    # Default to fallback for safety; check_model_availability() sets preferred
    return FALLBACK_MODEL


async def check_model_availability() -> str:
    """Check which model is available on the account."""
    global CURRENT_MODEL
    try:
        client = _get_async_groq_client()
        models = await client.models.list()
        available = {m.id for m in models.data}
        logger.info(f"Available models: {sorted(available)}")
        if PREFERRED_MODEL in available:
            CURRENT_MODEL = PREFERRED_MODEL
            logger.info(f"Using preferred model: {PREFERRED_MODEL}")
        else:
            CURRENT_MODEL = FALLBACK_MODEL
            logger.info(f"Preferred model not available, using: {FALLBACK_MODEL}")
        return CURRENT_MODEL
    except Exception as e:
        logger.warning(f"Could not check model availability: {e}. Using fallback.")
        CURRENT_MODEL = FALLBACK_MODEL
        return CURRENT_MODEL


async def plan_tool_call(
    question: str,
    model: str,
    max_tokens: int = 256,
) -> tuple[str, int, int, Optional[float]]:
    """
    Call Groq to plan retrieval via tool use.
    Returns (search_query, tokens_in, tokens_out, provider_first_delta_ms).
    Raises on API error.
    """
    client = _get_async_groq_client()
    messages = [
        {"role": "system", "content": PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    t0 = time.monotonic()
    provider_first_delta_ms = None

    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        tools=[SEARCH_TOOL_SCHEMA],
        tool_choice="auto",
        max_tokens=max_tokens,
        temperature=0.0,
    )

    t1 = time.monotonic()
    provider_first_delta_ms = (t1 - t0) * 1000  # non-streaming, full response

    usage = response.usage
    tokens_in = usage.prompt_tokens if usage else 0
    tokens_out = usage.completion_tokens if usage else 0

    # Extract search query from tool call
    choice = response.choices[0]
    search_query = question  # fallback to original question

    if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
        tc = choice.message.tool_calls[0]
        if tc.function.name != "search_papers":
            raise ValueError(f"Unexpected tool call: {tc.function.name}")
        try:
            args = json.loads(tc.function.arguments)
            search_query = args.get("query", question)
            logger.debug(f"Planner tool call: query={search_query!r}, args={args}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Could not parse tool args: {e}. Using original question.")
    else:
        logger.warning(f"Model did not call tool (finish_reason={choice.finish_reason}). Using original question.")

    return search_query, tokens_in, tokens_out, provider_first_delta_ms


async def generate_stream(
    question: str,
    passages: list,
    model: str,
    max_tokens: int = 256,
) -> AsyncGenerator[tuple[str, bool, Optional[float]], None]:
    """
    Stream answer generation from retrieved passages.
    Yields (chunk_text, is_first_delta, provider_first_delta_ms).
    Caller is responsible for buffering and citation validation.
    """
    client = _get_async_groq_client()

    # Build prompt with passage context
    context = _build_context(passages)
    messages = [
        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Question: {question}\n\nRelevant passages:\n{context}",
        },
    ]

    t0 = time.monotonic()
    first_delta_ms: Optional[float] = None
    is_first = True

    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.0,
        stream=True,
    )

    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            if is_first:
                first_delta_ms = (time.monotonic() - t0) * 1000
                is_first = False
            yield delta.content, is_first, first_delta_ms


def _build_context(passages: list) -> str:
    """Format retrieved passages for the answer prompt."""
    parts = []
    for p in passages:
        parts.append(
            f"[{p.chunk_id}] (Page {p.page}, {p.title})\n{p.text[:800]}"
        )
    return "\n\n---\n\n".join(parts)


async def simple_completion(
    messages: list[dict],
    model: str,
    max_tokens: int = 256,
) -> tuple[str, int, int, Optional[float]]:
    """
    Non-streaming completion for simple use cases.
    Returns (text, tokens_in, tokens_out, provider_first_delta_ms).
    """
    client = _get_async_groq_client()
    t0 = time.monotonic()
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000
    usage = response.usage
    text = response.choices[0].message.content or ""
    return (
        text,
        usage.prompt_tokens if usage else 0,
        usage.completion_tokens if usage else 0,
        elapsed_ms,
    )


def sanitize_headers(headers: dict) -> dict:
    """
    Remove any auth credentials from headers before logging.
    Only keeps x-ratelimit-* and content-type headers.
    """
    safe = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl.startswith("x-ratelimit") or kl in ("content-type", "retry-after"):
            safe[k] = v
    return safe
