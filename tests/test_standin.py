"""
tests/test_standin.py
Tests for the stand-in service: fault modes, 429, streaming, latency calibration.
Uses a real local server on a free port for integration tests.
"""

import asyncio
import json
import random
import time
import pytest
import httpx

from relay.standin import (
    StandinConfig,
    StandinService,
    FaultMode,
    StandinRateLimitError,
    StandinServerError,
    create_app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def get_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def start_test_server(config: StandinConfig, port: int):
    """Start the stand-in FastAPI server on the given port."""
    import uvicorn
    app = create_app(config)
    uv_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(uv_config)
    task = asyncio.create_task(server.serve())
    # Wait for startup
    await asyncio.sleep(1.0)
    return server, task


def make_messages(content: str = "Tell me about attention.") -> list[dict]:
    return [{"role": "user", "content": content}]


# ---------------------------------------------------------------------------
# Unit tests (no server required)
# ---------------------------------------------------------------------------

class TestStandinService:
    def test_deterministic_seed(self):
        """Same seed produces same latency sequence."""
        cfg = StandinConfig(seed=42)
        s1 = StandinService(cfg)
        s2 = StandinService(cfg)
        lat1 = s1._sample_prefill_ms()
        lat2 = s2._sample_prefill_ms()
        assert lat1 == lat2, f"Deterministic seed broken: {lat1} != {lat2}"

    def test_concurrency_slowdown(self):
        """Higher in-flight concurrency = higher slowdown multiplier."""
        cfg = StandinConfig(seed=42, concurrency_slowdown_factor=0.5)
        svc = StandinService(cfg)
        # Simulate increasing in-flight
        base = svc._concurrency_slowdown()  # 0 in-flight
        svc._quota._in_flight = 3
        with_load = svc._concurrency_slowdown()
        assert with_load > base

    def test_chunk_id_extraction(self):
        cfg = StandinConfig(seed=42)
        svc = StandinService(cfg)
        prompt_text = "Context: [1706.03762v7:B:p004:c002] Some text here."
        cid = svc._extract_chunk_id_from_prompt(prompt_text)
        assert cid == "1706.03762v7:B:p004:c002"

    def test_fixture_answer_has_chunk_id(self):
        cfg = StandinConfig(seed=42)
        svc = StandinService(cfg)
        messages = [{"role": "user", "content": "[1706.03762v7:B:p004:c002] What is attention?"}]
        answer, cid = svc._make_fixture_answer(messages, FaultMode.NORMAL)
        assert "1706.03762v7:B:p004:c002" in answer
        assert cid == "1706.03762v7:B:p004:c002"

    def test_invalid_citation_injection(self):
        cfg = StandinConfig(seed=42)
        svc = StandinService(cfg)
        messages = [{"role": "user", "content": "Some prompt"}]
        answer, cid = svc._make_fixture_answer(messages, FaultMode.INVALID_CITATION)
        assert "9999.99999v99:Z:p999:c999" in answer

    @pytest.mark.asyncio
    async def test_rate_limit_check(self):
        cfg = StandinConfig(seed=42)
        cfg.rpm = 1  # very low limit
        svc = StandinService(cfg)
        # First request consumes the limit
        ok1, reason1 = await svc._quota.check_and_reserve(100)
        assert ok1
        # Second should fail
        ok2, reason2 = await svc._quota.check_and_reserve(100)
        assert not ok2
        assert "rpm" in reason2 or "exceeded" in reason2

    @pytest.mark.asyncio
    async def test_daily_exhaust(self):
        cfg = StandinConfig(seed=42)
        svc = StandinService(cfg)
        svc._quota.exhaust_daily()
        ok, reason = await svc._quota.check_and_reserve(100)
        assert not ok
        assert "daily" in reason

    @pytest.mark.asyncio
    async def test_halve_rpm(self):
        cfg = StandinConfig(seed=42)
        svc = StandinService(cfg)
        original_rpm = svc._quota.effective_rpm()
        svc._quota.halve_rpm()
        halved_rpm = svc._quota.effective_rpm()
        assert halved_rpm == original_rpm // 2


# ---------------------------------------------------------------------------
# Integration tests (real HTTP server)
# ---------------------------------------------------------------------------

class TestStandinIntegration:
    @pytest.mark.asyncio
    async def test_normal_request_ok(self):
        port = await get_free_port()
        cfg = StandinConfig(seed=42)
        cfg.base_prefill_ms = 10.0  # fast for tests
        cfg.prefill_std_ms = 1.0
        server, task = await start_test_server(cfg, port)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/chat/completions",
                    json={"messages": make_messages(), "max_tokens": 50},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert "choices" in data
                assert data["choices"][0]["message"]["content"]
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_429_rate_limit(self):
        port = await get_free_port()
        cfg = StandinConfig(seed=42)
        cfg.base_prefill_ms = 5.0
        cfg.prefill_std_ms = 1.0
        cfg.rpm = 1  # limit to 1 RPM
        cfg.retry_after_s = 2.0
        server, task = await start_test_server(cfg, port)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # First request
                r1 = await client.post(
                    f"http://127.0.0.1:{port}/chat/completions",
                    json={"messages": make_messages(), "max_tokens": 10},
                )
                assert r1.status_code == 200
                # Second request (should be rate limited)
                r2 = await client.post(
                    f"http://127.0.0.1:{port}/chat/completions",
                    json={"messages": make_messages(), "max_tokens": 10},
                )
                assert r2.status_code == 429
                assert "retry-after" in r2.headers
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_immediate_5xx_fault(self):
        port = await get_free_port()
        cfg = StandinConfig(seed=42, default_fault=FaultMode.FAIL_IMMEDIATE_5XX)
        cfg.base_prefill_ms = 5.0
        server, task = await start_test_server(cfg, port)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/chat/completions",
                    json={"messages": make_messages(), "max_tokens": 10},
                )
                assert resp.status_code == 503
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_streaming_response(self):
        port = await get_free_port()
        cfg = StandinConfig(seed=42)
        cfg.base_prefill_ms = 10.0
        cfg.prefill_std_ms = 1.0
        cfg.tokens_per_second = 500.0  # fast for tests
        server, task = await start_test_server(cfg, port)
        try:
            chunks_received = []
            async with httpx.AsyncClient(timeout=15) as client:
                async with client.stream(
                    "POST",
                    f"http://127.0.0.1:{port}/chat/completions",
                    json={"messages": make_messages("[1706.03762v7:B:p004:c002] test"), "max_tokens": 50, "stream": True},
                ) as resp:
                    assert resp.status_code == 200
                    async for line in resp.aiter_lines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            data = json.loads(line[6:])
                            content = data["choices"][0]["delta"].get("content", "")
                            if content:
                                chunks_received.append(content)
            assert len(chunks_received) > 0, "No streaming chunks received"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        port = await get_free_port()
        cfg = StandinConfig(seed=42)
        server, task = await start_test_server(cfg, port)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://127.0.0.1:{port}/health")
                assert resp.status_code == 200
                assert resp.json()["status"] == "ok"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_admin_halve_rpm(self):
        port = await get_free_port()
        cfg = StandinConfig(seed=42)
        cfg.rpm = 10
        server, task = await start_test_server(cfg, port)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(f"http://127.0.0.1:{port}/admin/halve-rpm")
                assert resp.status_code == 200
                data = resp.json()
                assert data["effective_rpm"] == 5
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
