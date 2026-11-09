"""
tests/test_control.py
Tests for quota ledger, bounded queue, deadline pruning, and retry logic.
Uses fast injectable clocks and simulated concurrency to avoid long sleeps.
"""

import asyncio
import time
import uuid
import pytest

from relay.control import (
    QuotaConfig,
    QuotaLedger,
    BoundedQueue,
    RequestTicket,
    RetryDecision,
    estimate_tokens,
    new_request_id,
)


def make_config(**kwargs) -> QuotaConfig:
    defaults = dict(rpm=10, tpm=1000, rpd=100, tpd=50000, max_in_flight=3, max_queue=5)
    defaults.update(kwargs)
    return QuotaConfig(**defaults)


def make_ticket(deadline_offset_s: float = 10.0, **kwargs) -> RequestTicket:
    return RequestTicket(
        request_id=new_request_id(),
        question="test question",
        deadline_mono=time.monotonic() + deadline_offset_s,
        arrival_mono=time.monotonic(),
        start_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **kwargs,
    )


class TestQuotaLedger:
    @pytest.mark.asyncio
    async def test_basic_reservation_ok(self):
        ledger = QuotaLedger(make_config())
        ok, reason, res = await ledger.try_reserve(100, 50)
        assert ok, f"Expected ok but got: {reason}"

    @pytest.mark.asyncio
    async def test_rpm_exceeded(self):
        cfg = make_config(rpm=2)
        ledger = QuotaLedger(cfg)
        # Reserve 2 (hitting RPM)
        ok1, _, res1 = await ledger.try_reserve(10, 10)
        ok2, _, res2 = await ledger.try_reserve(10, 10)
        assert ok1
        assert ok2
        # Commit them so they count
        await ledger.commit(res1, 10, 10)
        await ledger.commit(res2, 10, 10)
        # Third should fail
        ok3, reason3, _ = await ledger.try_reserve(10, 10)
        assert not ok3
        assert "rpm" in reason3

    @pytest.mark.asyncio
    async def test_rpd_exceeded(self):
        cfg = make_config(rpd=1)
        ledger = QuotaLedger(cfg)
        ok1, _, res1 = await ledger.try_reserve(10, 10)
        assert ok1
        await ledger.commit(res1, 10, 10)
        ok2, reason2, _ = await ledger.try_reserve(10, 10)
        assert not ok2
        assert "rpd" in reason2

    @pytest.mark.asyncio
    async def test_tpm_exceeded(self):
        cfg = make_config(tpm=100)
        ledger = QuotaLedger(cfg)
        ok1, _, res1 = await ledger.try_reserve(50, 10)
        assert ok1
        await ledger.commit(res1, 50, 10)
        # Next request would exceed TPM: current tokens = 60, next estimate = 50*1.15 + 10 = 67 -> 60 + 67 = 127 > 100
        ok2, reason2, _ = await ledger.try_reserve(50, 10)
        assert not ok2
        assert "tpm" in reason2

    @pytest.mark.asyncio
    async def test_concurrent_reservations_no_oversubscribe(self):
        """50 concurrent tasks must not oversubscribe RPM=5."""
        cfg = make_config(rpm=5, tpm=100000, rpd=1000, tpd=10000000)
        ledger = QuotaLedger(cfg)

        successes = []
        lock = asyncio.Lock()

        async def try_reserve():
            ok, reason, res = await ledger.try_reserve(10, 10)
            async with lock:
                if ok:
                    successes.append(res)
                    await ledger.commit(res, 10, 10)

        tasks = [try_reserve() for _ in range(50)]
        await asyncio.gather(*tasks)
        # Should not exceed RPM limit
        assert len(successes) <= cfg.rpm, f"Oversubscribed: {len(successes)} > {cfg.rpm}"

    @pytest.mark.asyncio
    async def test_release_reservation_on_failure(self):
        """Released reservation should not count toward limits."""
        cfg = make_config(rpm=2)
        ledger = QuotaLedger(cfg)
        ok, _, res = await ledger.try_reserve(10, 10)
        assert ok
        # Release without committing (request failed before model call)
        await ledger.release_reservation(res)
        # Should be able to reserve again
        ok2, reason2, res2 = await ledger.try_reserve(10, 10)
        assert ok2, f"Expected ok after release, got: {reason2}"

    @pytest.mark.asyncio
    async def test_halve_rpm(self):
        cfg = make_config(rpm=10)
        ledger = QuotaLedger(cfg)
        assert ledger.effective_rpm() == 10
        ledger.halve_rpm()
        assert ledger.effective_rpm() == 5

    @pytest.mark.asyncio
    async def test_day_exhausted(self):
        cfg = make_config(rpd=10)
        ledger = QuotaLedger(cfg)
        ledger.exhaust_daily()
        ok, reason, _ = await ledger.try_reserve(10, 10)
        assert not ok
        assert "exhausted" in reason or "daily" in reason

    @pytest.mark.asyncio
    async def test_day_exhausted_flag_set_on_rpd(self):
        cfg = make_config(rpd=1)
        ledger = QuotaLedger(cfg)
        ok1, _, res1 = await ledger.try_reserve(10, 10)
        await ledger.commit(res1, 10, 10)
        ok2, _, _ = await ledger.try_reserve(10, 10)  # triggers day_exhausted
        assert not ok2
        assert ledger._day_exhausted


class TestBoundedQueue:
    @pytest.mark.asyncio
    async def test_put_and_get(self):
        q = BoundedQueue(maxsize=5)
        ticket = make_ticket()
        ok = await q.put(ticket)
        assert ok
        got = await q.get()
        assert got is not None
        assert got.request_id == ticket.request_id

    @pytest.mark.asyncio
    async def test_full_queue_returns_false(self):
        q = BoundedQueue(maxsize=2)
        t1 = make_ticket()
        t2 = make_ticket()
        t3 = make_ticket()
        ok1 = await q.put(t1)
        ok2 = await q.put(t2)
        ok3 = await q.put(t3)
        assert ok1 and ok2
        assert not ok3

    @pytest.mark.asyncio
    async def test_expired_ticket_pruned(self):
        q = BoundedQueue(maxsize=5)
        # Expired ticket (deadline in the past)
        expired = make_ticket(deadline_offset_s=-1.0)
        await q.put(expired)
        got = await q.get()
        assert got is None  # pruned

    @pytest.mark.asyncio
    async def test_cancelled_ticket_pruned(self):
        q = BoundedQueue(maxsize=5)
        ticket = make_ticket()
        ticket.cancelled = True
        await q.put(ticket)
        got = await q.get()
        assert got is None  # pruned

    @pytest.mark.asyncio
    async def test_peak_queue_tracking(self):
        q = BoundedQueue(maxsize=10)
        for i in range(5):
            await q.put(make_ticket())
        assert q.peak_size() == 5


class TestRetryDecision:
    def test_minute_rate_limit_retry_ok(self):
        should, wait, reason = RetryDecision.should_retry(
            error_type="rate_limited_minute",
            attempt=0,
            max_retries=1,
            deadline_mono=time.monotonic() + 60.0,
            retry_after_s=5.0,
            estimated_service_ms=2000.0,
        )
        assert should
        assert wait == 5.0

    def test_minute_rate_limit_no_time(self):
        """Not enough time to retry: should not retry."""
        should, wait, reason = RetryDecision.should_retry(
            error_type="rate_limited_minute",
            attempt=0,
            max_retries=1,
            deadline_mono=time.monotonic() + 1.0,  # only 1 second left
            retry_after_s=10.0,
            estimated_service_ms=2000.0,
        )
        assert not should

    def test_daily_exhausted_no_retry(self):
        should, wait, reason = RetryDecision.should_retry(
            error_type="rate_limited_day",
            attempt=0,
            max_retries=3,
            deadline_mono=time.monotonic() + 3600.0,
            retry_after_s=None,
        )
        assert not should

    def test_max_retries_reached(self):
        should, _, _ = RetryDecision.should_retry(
            error_type="rate_limited_minute",
            attempt=1,
            max_retries=1,
            deadline_mono=time.monotonic() + 60.0,
            retry_after_s=5.0,
        )
        assert not should

    def test_transient_5xx_retry(self):
        should, wait, reason = RetryDecision.should_retry(
            error_type="server_error_5xx",
            attempt=0,
            max_retries=1,
            deadline_mono=time.monotonic() + 30.0,
            retry_after_s=None,
        )
        assert should
        assert wait > 0


class TestHelpers:
    def test_estimate_tokens_nonzero(self):
        n = estimate_tokens("hello world this is a test")
        assert n > 0

    def test_new_request_id_unique(self):
        ids = {new_request_id() for _ in range(100)}
        assert len(ids) == 100  # all unique
