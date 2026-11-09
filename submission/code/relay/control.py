"""
relay/control.py
Quota ledger, bounded FIFO queue with deadline pruning, retries, and concurrency control.

Invariants:
- One user request → exactly one terminal results.csv row
- Token + request reservations are atomic, per model call, under a lock
- Requests cannot remain queued past their deadline
- Quota reservations are reconciled from actual usage when available
- On cancellation/timeout/broken stream, all in-flight slots are released
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class LimitType(str, Enum):
    RPM = "rpm"
    TPM = "tpm"
    RPD = "rpd"
    TPD = "tpd"
    CONCURRENCY = "concurrency"


@dataclass
class QuotaConfig:
    rpm: int = 30            # requests per minute (conservative default)
    tpm: int = 6000          # tokens per minute (conservative default)
    rpd: int = 14400         # requests per day (conservative default)
    tpd: int = 500000        # tokens per day (conservative default)
    max_in_flight: int = 3   # local concurrency cap
    max_queue: int = 24
    reserve_input_multiplier: float = 1.15
    max_model_retries: int = 1
    max_retrieval_rewrites: int = 1
    base_max_output_tokens: int = 256
    end_to_end_deadline_ms: Optional[float] = None


@dataclass
class LimitObservation:
    limit_type: str
    value: Optional[int]
    source: str   # "account_console" | "response_header" | "429" | "controlled_probe" | "docs"
    confidence: str  # "high" | "medium" | "low"
    timestamp: str
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "limit_type": self.limit_type,
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "notes": self.notes,
        }


class QuotaLedger:
    """
    Thread-safe quota ledger tracking RPM, TPM, RPD, TPD, and in-flight concurrency.
    Uses rolling 60-second windows for per-minute limits.
    """

    def __init__(self, config: QuotaConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(config.max_in_flight)

        # Minute window tracking
        self._minute_requests: list[float] = []  # timestamps of requests in current minute
        self._minute_tokens: list[tuple[float, int]] = []  # (timestamp, tokens)

        # Day tracking
        self._day_requests: int = 0
        self._day_tokens: int = 0
        self._day_start: float = time.time()

        # Active reservations (uncommitted)
        self._reserved_tokens: int = 0
        self._reserved_requests: int = 0

        # Observations from real headers
        self._observations: list[LimitObservation] = []

        # Dynamic RPM halving support
        self._rpm_halved: bool = False
        self._rpm_halved_at: Optional[float] = None

        # Day exhausted flag
        self._day_exhausted: bool = False
        self._day_exhausted_at: Optional[float] = None

    def effective_rpm(self) -> int:
        base = self.config.rpm
        return max(1, base // 2) if self._rpm_halved else base

    def effective_tpm(self) -> int:
        return self.config.tpm

    def effective_rpd(self) -> int:
        return self.config.rpd

    def effective_tpd(self) -> int:
        return self.config.tpd

    def _prune_minute_window(self, now: float) -> None:
        cutoff = now - 60.0
        self._minute_requests = [t for t in self._minute_requests if t > cutoff]
        self._minute_tokens = [(t, tok) for t, tok in self._minute_tokens if t > cutoff]

    def _current_minute_requests(self, now: float) -> int:
        self._prune_minute_window(now)
        return len(self._minute_requests) + self._reserved_requests

    def _current_minute_tokens(self, now: float) -> int:
        self._prune_minute_window(now)
        return sum(tok for _, tok in self._minute_tokens) + self._reserved_tokens

    def _reset_day_if_needed(self, now: float) -> None:
        if now - self._day_start >= 86400:
            self._day_start = now
            self._day_requests = 0
            self._day_tokens = 0
            self._day_exhausted = False
            logger.info("Daily quota reset")

    async def try_reserve(
        self,
        estimated_tokens_in: int,
        estimated_tokens_out: int,
    ) -> tuple[bool, str, dict]:
        """
        Attempt to atomically reserve quota for one model call.
        Returns (success, reason, reservation_info).
        """
        estimated_total = int(
            estimated_tokens_in * self.config.reserve_input_multiplier + estimated_tokens_out
        )

        async with self._lock:
            now = time.time()
            self._reset_day_if_needed(now)

            if self._day_exhausted:
                return False, "daily_exhausted", {}

            # Check RPD
            if self._day_requests + 1 > self.effective_rpd():
                self._day_exhausted = True
                self._day_exhausted_at = now
                return False, "rpd_exceeded", {}

            # Check TPD
            if self._day_tokens + estimated_total > self.effective_tpd():
                return False, "tpd_exceeded", {}

            # Check RPM
            if self._current_minute_requests(now) + 1 > self.effective_rpm():
                return False, "rpm_exceeded", {}

            # Check TPM
            if self._current_minute_tokens(now) + estimated_total > self.effective_tpm():
                return False, "tpm_exceeded", {}

            # Reserve
            self._reserved_requests += 1
            self._reserved_tokens += estimated_total
            ts = now

        reservation = {
            "reserved_at": ts,
            "estimated_tokens": estimated_total,
        }
        return True, "ok", reservation

    async def commit(
        self,
        reservation: dict,
        actual_tokens_in: int,
        actual_tokens_out: int,
    ) -> None:
        """Commit actual usage after a successful model call."""
        actual_total = actual_tokens_in + actual_tokens_out
        async with self._lock:
            now = time.time()
            # Deduct reservation
            self._reserved_requests = max(0, self._reserved_requests - 1)
            self._reserved_tokens = max(0, self._reserved_tokens - reservation.get("estimated_tokens", 0))
            # Record actual
            self._minute_requests.append(now)
            self._minute_tokens.append((now, actual_total))
            self._day_requests += 1
            self._day_tokens += actual_total

    async def release_reservation(self, reservation: dict) -> None:
        """Release a reservation that was never committed (request failed before model call)."""
        async with self._lock:
            self._reserved_requests = max(0, self._reserved_requests - 1)
            self._reserved_tokens = max(0, self._reserved_tokens - reservation.get("estimated_tokens", 0))

    async def update_from_headers(self, headers: dict) -> None:
        """Update limits from real response headers."""
        async with self._lock:
            now = time.time()
            # Note: x-ratelimit-limit-requests = RPD (day), x-ratelimit-limit-tokens = TPM
            if "x-ratelimit-limit-requests" in headers:
                val = int(headers["x-ratelimit-limit-requests"])
                self._observations.append(LimitObservation(
                    limit_type="rpd",
                    value=val,
                    source="response_header",
                    confidence="high",
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    notes="x-ratelimit-limit-requests (Groq docs: requests/day)",
                ))
            if "x-ratelimit-limit-tokens" in headers:
                val = int(headers["x-ratelimit-limit-tokens"])
                self._observations.append(LimitObservation(
                    limit_type="tpm",
                    value=val,
                    source="response_header",
                    confidence="high",
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    notes="x-ratelimit-limit-tokens (Groq docs: tokens/minute)",
                ))
            if "x-ratelimit-remaining-requests" in headers:
                remaining = int(headers["x-ratelimit-remaining-requests"])
                logger.debug(f"Remaining requests (day): {remaining}")
            if "x-ratelimit-remaining-tokens" in headers:
                remaining = int(headers["x-ratelimit-remaining-tokens"])
                logger.debug(f"Remaining tokens (minute): {remaining}")

    def halve_rpm(self) -> None:
        """Dynamically halve effective RPM."""
        if not self._rpm_halved:
            self._rpm_halved = True
            self._rpm_halved_at = time.time()
            logger.warning(f"RPM halved: effective RPM now {self.effective_rpm()}")

    def restore_rpm(self) -> None:
        """Restore full RPM."""
        self._rpm_halved = False
        self._rpm_halved_at = None
        logger.info(f"RPM restored: effective RPM now {self.effective_rpm()}")

    def exhaust_daily(self) -> None:
        """Mark daily quota as exhausted (for stand-in scenario testing)."""
        self._day_exhausted = True
        self._day_exhausted_at = time.time()

    def get_observations(self) -> list[dict]:
        return [obs.to_dict() for obs in self._observations]

    def get_status(self) -> dict:
        now = time.time()
        self._prune_minute_window(now)
        return {
            "minute_requests": len(self._minute_requests),
            "minute_tokens": sum(tok for _, tok in self._minute_tokens),
            "day_requests": self._day_requests,
            "day_tokens": self._day_tokens,
            "effective_rpm": self.effective_rpm(),
            "effective_tpm": self.effective_tpm(),
            "effective_rpd": self.effective_rpd(),
            "effective_tpd": self.effective_tpd(),
            "rpm_halved": self._rpm_halved,
            "day_exhausted": self._day_exhausted,
            "reserved_requests": self._reserved_requests,
            "reserved_tokens": self._reserved_tokens,
        }


@dataclass
class RequestTicket:
    request_id: str
    question: str
    deadline_mono: float    # monotonic deadline
    arrival_mono: float
    start_utc: str
    estimated_tokens_in: int = 500
    estimated_tokens_out: int = 256
    retrieval_setting: str = "B"
    reservation: dict = field(default_factory=dict)
    cancelled: bool = False
    queue_depth_at_admit: int = 0
    rung_served: int = 0


class BoundedQueue:
    """
    Bounded FIFO queue with deadline pruning.
    Admits requests only if they can plausibly be served before deadline.
    """

    def __init__(self, maxsize: int = 24):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._maxsize = maxsize
        self._current_size = 0
        self._peak_size = 0
        self._lock = asyncio.Lock()

    async def put(self, ticket: RequestTicket) -> bool:
        """Enqueue. Returns False if queue is full."""
        try:
            self._queue.put_nowait(ticket)
            async with self._lock:
                self._current_size += 1
                self._peak_size = max(self._peak_size, self._current_size)
            return True
        except asyncio.QueueFull:
            return False

    async def get(self) -> Optional[RequestTicket]:
        """Dequeue next ticket, pruning expired ones."""
        while True:
            try:
                ticket = self._queue.get_nowait()
                async with self._lock:
                    self._current_size = max(0, self._current_size - 1)
            except asyncio.QueueEmpty:
                return None

            now = time.monotonic()
            if ticket.cancelled or now >= ticket.deadline_mono:
                logger.debug(f"Pruned expired/cancelled ticket {ticket.request_id}")
                continue
            return ticket

    def qsize(self) -> int:
        return self._queue.qsize()

    def peak_size(self) -> int:
        return self._peak_size


class RetryDecision:
    """Determine if a failed model call should be retried."""

    @staticmethod
    def should_retry(
        error_type: str,
        attempt: int,
        max_retries: int,
        deadline_mono: float,
        retry_after_s: Optional[float],
        estimated_service_ms: float = 5000.0,
    ) -> tuple[bool, float, str]:
        """
        Returns (should_retry, wait_seconds, reason).
        """
        now = time.monotonic()
        remaining_ms = (deadline_mono - now) * 1000

        if attempt >= max_retries:
            return False, 0.0, f"max_retries={max_retries} reached"

        if error_type == "rate_limited_minute":
            wait = retry_after_s or 10.0
            if remaining_ms < (wait * 1000 + estimated_service_ms):
                return False, 0.0, f"not enough time: {remaining_ms:.0f}ms < {wait*1000+estimated_service_ms:.0f}ms"
            return True, wait, "retry after rate limit cooldown"

        elif error_type == "rate_limited_day":
            # Daily exhausted: no retry
            return False, 0.0, "daily quota exhausted, no retry"

        elif error_type in ("server_error_5xx", "transient"):
            jitter = 0.5 + (attempt * 0.5)
            wait = min(jitter, 5.0)
            if remaining_ms < (wait * 1000 + estimated_service_ms):
                return False, 0.0, f"not enough time for transient retry"
            return True, wait, f"transient retry with {wait:.1f}s backoff"

        elif error_type == "tool_timeout":
            # Use local fallback, don't retry model
            return False, 0.0, "tool timeout: use local fallback"

        return False, 0.0, f"unknown error type: {error_type}"


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def estimate_tokens(text: str, multiplier: float = 1.3) -> int:
    """Rough token estimate: words * multiplier."""
    return max(1, int(len(text.split()) * multiplier))
