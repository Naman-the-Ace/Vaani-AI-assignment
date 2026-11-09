"""
relay/standin.py
Local HTTP + SSE stand-in model service, calibrated from real Groq measurements.

Exposes a chat-completion-compatible endpoint on localhost.
Supports configurable behaviors:
  1. Normal success with deterministic responses referencing valid chunk IDs
  2. Variable prefill + TTFT latency
  3. Incremental token emission (SSE)
  4. Immediate 5xx, delayed 5xx, 429 with retry-after
  5. Slow calls, hangs/timeouts, broken stream after first chunk
  6. Per-minute request/token and per-day request/token caps
  7. Load-dependent slowdown (more concurrency → slower)
  8. Dynamic RPM halving and daily exhaustion
  9. Invalid citation injection for validation testing
  10. Seeded RNG for reproducibility

NOTE: Stand-in answers are FIXTURE STRINGS, not real LLM output.
      answer_grade must stay BLANK for all stand-in rows.
      Stand-in behavior is calibrated to real measurements, but
      DOES NOT simulate true inference semantics, multi-tenancy, or token variance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, AsyncGenerator

logger = logging.getLogger(__name__)


class FaultMode(str, Enum):
    NORMAL = "normal"
    SLOW = "slow"
    HANG = "hang"
    FAIL_IMMEDIATE_5XX = "fail_immediate_5xx"
    FAIL_DELAYED_5XX = "fail_delayed_5xx"
    RATE_LIMITED_429 = "rate_limited_429"
    BROKEN_STREAM = "broken_stream"
    INVALID_CITATION = "invalid_citation"
    DAILY_EXHAUSTED = "daily_exhausted"


@dataclass
class StandinConfig:
    """Configuration for the stand-in service."""
    seed: int = 2026

    # Latency calibration (fitted from real Groq n=15 probe measurements)
    base_prefill_ms: float = 659.3    # median TTFT from real probe (measured: 659.3ms)
    base_prefill_p95_ms: float = 1163.1  # p95 TTFT from real probe (measured: 1163.1ms)
    tokens_per_second: float = 38.5   # measured generation speed
    prefill_std_ms: float = 322.3     # measured std dev (322.3ms)

    # Load-dependent slowdown
    concurrency_slowdown_factor: float = 0.35  # each extra in-flight adds ~35% to prefill

    # Quota (stand-in internal limits for scenario testing)
    rpm: int = 30
    tpm: int = 6000
    rpd: int = 500
    tpd: int = 100000

    # Retry-after on 429
    retry_after_s: float = 10.0

    # Fault injection
    default_fault: FaultMode = FaultMode.NORMAL
    fault_schedule: dict = field(default_factory=dict)  # {t_seconds_from_start: FaultMode}

    # RPM halving
    halve_rpm_at_s: Optional[float] = None  # seconds from run start
    daily_exhaust_at_s: Optional[float] = None  # seconds from run start

    # Answer template chunk ID
    # Will be replaced with real chunk ID from prompt at runtime
    fixture_answer_template: str = (
        "Based on the retrieved passages, {summary} [{chunk_id}]. "
        "This is explained in detail in section {section} of the paper."
    )


class StandinQuotaLedger:
    """Stand-in internal quota tracking."""

    def __init__(self, config: StandinConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._minute_requests: list[float] = []
        self._minute_tokens: list[tuple[float, int]] = []
        self._day_requests: int = 0
        self._day_tokens: int = 0
        self._day_start: float = time.time()
        self._rpm_halved: bool = False
        self._day_exhausted: bool = False
        self._in_flight: int = 0

    def effective_rpm(self) -> int:
        base = self.config.rpm
        return max(1, base // 2) if self._rpm_halved else base

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        self._minute_requests = [t for t in self._minute_requests if t > cutoff]
        self._minute_tokens = [(t, tok) for t, tok in self._minute_tokens if t > cutoff]

    async def check_and_reserve(self, tokens: int = 100) -> tuple[bool, str]:
        async with self._lock:
            now = time.time()
            self._prune(now)

            if self._day_exhausted:
                return False, "daily_exhausted"
            if len(self._minute_requests) >= self.effective_rpm():
                return False, "rpm_exceeded"
            if sum(t for _, t in self._minute_tokens) + tokens > self.config.tpm:
                return False, "tpm_exceeded"
            if self._day_requests >= self.config.rpd:
                self._day_exhausted = True
                return False, "daily_exhausted"

            self._minute_requests.append(now)
            self._minute_tokens.append((now, tokens))
            self._day_requests += 1
            self._day_tokens += tokens
            self._in_flight += 1
            return True, "ok"

    async def release(self) -> None:
        async with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    def current_in_flight(self) -> int:
        return self._in_flight

    def halve_rpm(self) -> None:
        self._rpm_halved = True

    def exhaust_daily(self) -> None:
        self._day_exhausted = True


class StandinService:
    """
    Stand-in service state machine. Used by the FastAPI app.
    """

    def __init__(self, config: StandinConfig):
        self.config = config
        self._rng = random.Random(config.seed)
        self._quota = StandinQuotaLedger(config)
        self._start_time: float = time.time()
        self._current_fault: FaultMode = config.default_fault

    def _elapsed(self) -> float:
        return time.time() - self._start_time

    def _update_scheduled_events(self) -> None:
        """Check if any scheduled events (RPM halve, daily exhaust) should trigger."""
        elapsed = self._elapsed()
        if self.config.halve_rpm_at_s and elapsed >= self.config.halve_rpm_at_s:
            if not self._quota._rpm_halved:
                logger.info(f"[StandIn] Halving RPM at t={elapsed:.1f}s")
                self._quota.halve_rpm()
        if self.config.daily_exhaust_at_s and elapsed >= self.config.daily_exhaust_at_s:
            if not self._quota._day_exhausted:
                logger.info(f"[StandIn] Exhausting daily quota at t={elapsed:.1f}s")
                self._quota.exhaust_daily()

    def _get_fault_mode(self) -> FaultMode:
        """Determine current fault mode based on schedule."""
        self._update_scheduled_events()
        elapsed = self._elapsed()
        # Check fault schedule
        active_fault = self.config.default_fault
        for t_s, fault in sorted(self.config.fault_schedule.items()):
            if elapsed >= float(t_s):
                active_fault = FaultMode(fault)
        return active_fault

    def _sample_prefill_ms(self) -> float:
        """Sample prefill delay from calibrated distribution."""
        base = self.config.base_prefill_ms
        std = self.config.prefill_std_ms
        # Lognormal to match real heavy tail
        mean_log = (
            (base ** 2) / ((base ** 2 + std ** 2) ** 0.5)
        )
        # Simple: use lognormal parameterization
        import math
        sigma = math.sqrt(math.log(1 + (std / base) ** 2))
        mu = math.log(base) - sigma ** 2 / 2
        sample = self._rng.lognormvariate(mu, sigma)
        return max(50.0, sample)

    def _concurrency_slowdown(self) -> float:
        """Additional prefill multiplier based on current in-flight."""
        in_flight = self._quota.current_in_flight()
        return 1.0 + self.config.concurrency_slowdown_factor * max(0, in_flight - 1)

    def _extract_chunk_id_from_prompt(self, prompt_text: str) -> Optional[str]:
        """
        Try to extract a real chunk ID from the prompt context,
        so fixture answers reference real IDs.
        """
        import re
        matches = re.findall(r"\d{4}\.\d{5}v\d+:[A-C]:p\d{3}:c\d{3}", prompt_text)
        if matches:
            return matches[0]
        return None

    async def handle_request(
        self,
        messages: list[dict],
        max_tokens: int = 256,
        stream: bool = False,
    ) -> dict | AsyncGenerator:
        """Main handler for incoming requests."""
        fault = self._get_fault_mode()

        # Check quota
        ok, reason = await self._quota.check_and_reserve(tokens=max_tokens + 100)
        if not ok:
            raise StandinRateLimitError(
                reason=reason,
                retry_after=self.config.retry_after_s,
            )

        try:
            if fault == FaultMode.RATE_LIMITED_429:
                raise StandinRateLimitError("fault_injection", self.config.retry_after_s)
            elif fault == FaultMode.FAIL_IMMEDIATE_5XX:
                raise StandinServerError("immediate 5xx fault injection")
            elif fault == FaultMode.HANG:
                await asyncio.sleep(60.0)  # effectively hang
                raise StandinTimeoutError("hang timeout")

            # Normal path with possible delayed fault
            prefill_ms = self._sample_prefill_ms() * self._concurrency_slowdown()
            if fault == FaultMode.SLOW:
                prefill_ms *= 3.0

            if stream:
                return self._stream_response(
                    messages, max_tokens, prefill_ms, fault
                )
            else:
                return await self._non_stream_response(
                    messages, max_tokens, prefill_ms, fault
                )
        finally:
            await self._quota.release()

    async def _non_stream_response(
        self, messages: list, max_tokens: int, prefill_ms: float, fault: FaultMode
    ) -> dict:
        await asyncio.sleep(prefill_ms / 1000.0)

        if fault == FaultMode.FAIL_DELAYED_5XX:
            raise StandinServerError("delayed 5xx fault injection")

        answer, chunk_id = self._make_fixture_answer(messages, fault)
        tokens_out = min(max_tokens, max(10, len(answer.split())))
        tokens_in = sum(len(m.get("content", "").split()) for m in messages)

        return {
            "id": f"standin-{uuid.uuid4().hex[:8]}",
            "choices": [{"message": {"content": answer, "role": "assistant"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out, "total_tokens": tokens_in + tokens_out},
            "model": "standin-v1",
        }

    async def _stream_response(
        self, messages: list, max_tokens: int, prefill_ms: float, fault: FaultMode
    ) -> AsyncGenerator[str, None]:
        """Async generator yielding SSE chunks."""
        await asyncio.sleep(prefill_ms / 1000.0)

        if fault == FaultMode.FAIL_DELAYED_5XX:
            raise StandinServerError("delayed 5xx")

        answer, chunk_id = self._make_fixture_answer(messages, fault)
        tokens_out = min(max_tokens, max(10, len(answer.split())))
        tokens_in = sum(len(m.get("content", "").split()) for m in messages)

        # Emit tokens in small chunks to simulate streaming
        words = answer.split()
        chunk_size = max(1, len(words) // 8)
        emitted = 0

        for i in range(0, len(words), chunk_size):
            if emitted > 0 and fault == FaultMode.BROKEN_STREAM:
                raise StandinStreamError("broken stream after first chunk")

            chunk_text = " ".join(words[i:i + chunk_size])
            if i > 0:
                chunk_text = " " + chunk_text

            # Simulate per-token delay
            token_delay = len(words[i:i + chunk_size]) / self.config.tokens_per_second
            await asyncio.sleep(token_delay)

            sse_data = {
                "id": f"standin-{uuid.uuid4().hex[:8]}",
                "choices": [{"delta": {"content": chunk_text, "role": "assistant"}, "finish_reason": None}],
                "model": "standin-v1",
            }
            yield f"data: {json.dumps(sse_data)}\n\n"
            emitted += 1

        # Final chunk
        final = {
            "id": f"standin-{uuid.uuid4().hex[:8]}",
            "choices": [{"delta": {"content": "", "role": "assistant"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out, "total_tokens": tokens_in + tokens_out},
            "model": "standin-v1",
        }
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"

    def _make_fixture_answer(
        self, messages: list, fault: FaultMode
    ) -> tuple[str, str]:
        """
        Generate a deterministic fixture answer.
        Uses chunk IDs found in the prompt, or a placeholder.
        Returns (answer_text, used_chunk_id).
        """
        # Extract chunk ID from prompt
        prompt_text = " ".join(m.get("content", "") for m in messages)
        chunk_id = self._extract_chunk_id_from_prompt(prompt_text)

        if fault == FaultMode.INVALID_CITATION:
            # Inject invalid citation for validation testing
            invalid_id = "9999.99999v99:Z:p999:c999"
            return (
                f"The model explains this concept thoroughly [{invalid_id}]. "
                f"Further details are available in the referenced section.",
                invalid_id,
            )

        if chunk_id:
            answer = (
                f"Based on the retrieved passages, the question is addressed in the paper. "
                f"[{chunk_id}] The relevant section explains the core mechanism "
                f"and provides supporting evidence for the conclusion."
            )
        else:
            answer = (
                "Based on the retrieved passages, this topic is covered in the research. "
                "[FIXTURE-ANSWER: no chunk ID found in prompt] "
                "Please check the retrieval configuration."
            )
        return answer, chunk_id or "unknown"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class StandinRateLimitError(Exception):
    def __init__(self, reason: str, retry_after: float = 10.0):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(f"Rate limited: {reason}")


class StandinServerError(Exception):
    pass


class StandinTimeoutError(Exception):
    pass


class StandinStreamError(Exception):
    pass


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

from fastapi import Request as FastAPIRequest

def create_app(config: StandinConfig) -> "FastAPI":
    """Create the FastAPI stand-in application."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse, JSONResponse

    service = StandinService(config)
    app = FastAPI(title="Vaani Relay Stand-in Service")

    @app.post("/chat/completions")
    async def chat_completions(request: FastAPIRequest):
        body = await request.json()
        messages = body.get("messages", [])
        max_tokens = body.get("max_tokens", 256)
        stream = body.get("stream", False)

        try:
            if stream:
                gen = await service.handle_request(messages, max_tokens, stream=True)
                return StreamingResponse(gen, media_type="text/event-stream")
            else:
                result = await service.handle_request(messages, max_tokens, stream=False)
                return JSONResponse(result)

        except StandinRateLimitError as e:
            return JSONResponse(
                status_code=429,
                content={"error": {"message": str(e), "type": "rate_limit_error"}},
                headers={"retry-after": str(int(e.retry_after))},
            )
        except StandinServerError as e:
            return JSONResponse(
                status_code=503,
                content={"error": {"message": str(e), "type": "server_error"}},
            )
        except StandinTimeoutError as e:
            return JSONResponse(
                status_code=504,
                content={"error": {"message": str(e), "type": "timeout"}},
            )

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "standin-v1"}

    @app.post("/admin/halve-rpm")
    async def admin_halve_rpm():
        service._quota.halve_rpm()
        return {"action": "halved_rpm", "effective_rpm": service._quota.effective_rpm()}

    @app.post("/admin/exhaust-daily")
    async def admin_exhaust_daily():
        service._quota.exhaust_daily()
        return {"action": "exhausted_daily"}

    @app.post("/admin/set-fault")
    async def admin_set_fault(request: Request):
        body = await request.json()
        fault = FaultMode(body.get("fault", "normal"))
        service.config.default_fault = fault
        return {"action": "set_fault", "fault": fault}

    return app


async def start_standin_server(
    config: StandinConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Start the stand-in service. Runs until cancelled."""
    import uvicorn
    app = create_app(config)
    uv_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(uv_config)
    logger.info(f"Starting stand-in server at http://{host}:{port}")
    await server.serve()
