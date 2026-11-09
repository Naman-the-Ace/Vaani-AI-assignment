# Requirements Traceability Matrix — Vaani "The Relay"

**Date:** 2026-11-09  
**Status:** All 16 core brief requirements satisfied

| Requirement ID | Brief Requirement Description | Implementation Location | Empirical Evidence | Status |
|---|---|---|---|---|
| **REQ-01** | Pipeline with at least 4 sequential steps | `relay/app.py:run_pipeline` | Traces show 5 distinct stages: plan, retrieve, gate, generate, validate | **PASS** |
| **REQ-02** | At least one model tool call | `relay/provider.py:plan_query` | Tool calling `plan_query` executes before retrieval in rung 0 | **PASS** |
| **REQ-03** | At least one branch or loop | `relay/app.py`, `relay/evidence.py`, `relay/control.py` | Evidence gate branch (short-circuit refusal) and retry loop on 429 | **PASS** |
| **REQ-04** | Pinned paper 1: 1706.03762v7 | `data/papers/1706.03762v7.pdf` | SHA256 verified against `paper_hashes.json` | **PASS** |
| **REQ-05** | Pinned paper 2: 2410.00037v1 | `data/papers/2410.00037v1.pdf` | SHA256 verified against `paper_hashes.json` | **PASS** |
| **REQ-06** | Pinned paper 3: 2507.07955v2 | `data/papers/2507.07955v2.pdf` | SHA256 verified against `paper_hashes.json` | **PASS** |
| **REQ-07** | Answer only from papers / cite sources | `relay/evidence.py:validate_answer` | Enforces exact syntactic chunk citations matching retrieved set | **PASS** |
| **REQ-08** | Real hosted model on Groq free tier | `relay/provider.py` | `openai/gpt-oss-20b` executed on Groq API (87 real calls recorded) | **PASS** |
| **REQ-09** | Stand-in model service simulating limits | `relay/standin.py` | FastAPI server with rate limiting, latency delays, and 10 fault modes | **PASS** |
| **REQ-10** | Stand-in calibrated from real measurements | `artifacts/calibration.json`, `relay/standin.py` | Lognormal parameters calibrated from 15 real probe measurements | **PASS** |
| **REQ-11** | Minimum 3 distinct load shapes | `relay/experiments.py` | Steady (0.5 rps), Burst (6 rps), Sustained Overload (0.83 rps) + rpm_halved & daily_exhausted | **PASS** |
| **REQ-12** | Raw results preserved for real and stand-in | `artifacts/results.csv`, `artifacts/traces.jsonl` | 457 total recorded requests with timing partitions and metadata | **PASS** |
| **REQ-13** | Latency metrics (TTFT & Total at median & tail) | `artifacts/table2a.md`, `relay/reporting.py` | Table 2a details p50/p95 (and p99 where n>=100) using NumPy linear interpolation | **PASS** |
| **REQ-14** | Answer quality evaluation on >= 15 questions | `data/questions.jsonl`, `artifacts/table3.md` | 24 frozen questions (18 answerable, 6 near-miss) across 3 retrieval settings (72 eval runs) | **PASS** |
| **REQ-15** | Token usage per answer reported | `artifacts/table2b.md`, `artifacts/table3.md` | Reported across all scenarios and retrieval settings | **PASS** |
| **REQ-16** | 2-page report with specified structure | `docs/writeup.md`, `docs/report_2_pages.pdf` | Strict 2-page PDF adhering to all 12 mandatory sections | **PASS** |
