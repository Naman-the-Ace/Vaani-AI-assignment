# Initial Repository Audit — Vaani "The Relay"

**Audit Date:** 2026-11-09  
**Auditor:** Engineering audit (automated + manual review)  
**Repository:** `vaani-relay/`  
**Commit at audit time:** pre-fix HEAD

---

## 1. Architecture Overview

The pipeline implements a **5-step sequential architecture** with one branching gate and one retry loop:

```
User Query
    │
    ▼
[Step 1] Intent & Planning (rung 0)
    │  Groq tool call: plan_query()
    │  Outputs: rewritten query, retrieval hints
    ▼
[Step 2] Evidence Retrieval (rung 1)
    │  BM25 search across 3 retrieval settings (A/B/C)
    │  Returns top-k passages with chunk IDs
    ▼
[Step 3] Evidence Gate (rung 4 — short-circuit branch)
    │  Gate check: ≥1 passage with direct answer keyword?
    │  ├─ FAIL → evidence_refusal (refused_correctly / refused_wrongly)
    │  └─ PASS → continue
    ▼
[Step 4] Draft Generation (rung 2)
    │  Groq streaming call: gpt-oss-20b
    │  Requires inline citations [arxiv_id:setting:pNNN:cNNN]
    │  Retry loop: ≤1 retry on transient/TPM 429
    ▼
[Step 5] Citation Validation (rung 3)
    │  Checks every inline citation against retrieved set
    │  ├─ Invalid citation → evidence_refusal
    │  └─ All valid → ok (answer delivered to user)
    ▼
Terminal Response
```

**Key design decisions:**
- BoundedQueue (maxsize=24) for admission control in `relay/control.py`
- `RetryDecision` class classifies 429 errors as minute-level (retriable) vs day-level (not retriable)
- Citation format: `[arxiv_id:setting:pNNN:cNNN]` — strict inline per-claim requirement

---

## 2. Files Audited

| File | Purpose | Lines |
|---|---|---|
| `relay/standin.py` | FastAPI stand-in server simulating Groq | ~450 |
| `relay/control.py` | Request orchestrator, BoundedQueue, retry logic | ~380 |
| `relay/evidence.py` | BM25 retrieval, citation validation, evidence gate | ~220 |
| `relay/papers.py` | PDF ingestion, chunking (settings A/B/C) | ~400 |
| `relay/provider.py` | Groq HTTP client wrapper, TTFT measurement | ~300 |
| `relay/app.py` | CLI entrypoint, scenario runner, eval runner | ~900 |
| `relay/experiments.py` | Load shape definitions (steady/burst/etc.) | ~180 |
| `relay/reporting.py` | Table generation from results.csv | ~550 |
| `artifacts/results.csv` | Raw result rows (all runs) | 497 rows pre-fix |
| `artifacts/grading_queue.csv` | Human-graded eval answers | 72 rows |

---

## 3. Bugs Found

### P0 Bug #1 — Python Truthiness Bug in `standin.py` (FIXED)

**Location:** `relay/standin.py` lines 168, 172  
**Symptom:** When `daily_exhaust_at_s=0.0` (daily quota exhausted from t=0), the stand-in was treating the value as falsy (`if not 0.0 → True`) and skipping the daily exhaustion check entirely.  
**Impact:** The `scenario_daily_exhausted` run with run_id `scenario_daily_exhausted_8036e76d` returned 31 `ok` responses and only 9 `rate_limited` responses instead of the correct 40 `rate_limited`.  
**Root cause:** Python evaluates `0.0` as falsy. Code used `if not daily_exhaust_at_s:` guard which skipped the branch when the value was exactly `0.0`.  
**Fix:** Changed truthiness checks to explicit `is None` comparisons:
```python
# Before (broken):
if not daily_exhaust_at_s:
    ...
# After (fixed):
if daily_exhaust_at_s is None:
    ...
```
**Verification:** New run `scenario_daily_exhausted_ef44d9a5` produced 40 `rate_limited`, 0 `ok`. ✅

---

### P0 Bug #2 — Stale `daily_exhausted` Rows in `results.csv` (FIXED)

**Location:** `artifacts/results.csv`  
**Symptom:** results.csv contained 80 rows tagged as `daily_exhausted` — 40 from the broken pre-fix run (`scenario_daily_exhausted_8036e76d`) plus 40 from the corrected run (`scenario_daily_exhausted_ef44d9a5`).  
**Impact:** Table 2b showed `daily_exhausted` as 77.5% succeeded / 22.5% rate-limited (wrong). The correct result is 0% succeeded / 100% rate-limited.  
**Fix:** Removed all 40 rows with `run_id == scenario_daily_exhausted_8036e76d`:
```python
df = df[df['run_id'] != 'scenario_daily_exhausted_8036e76d']
df.to_csv('artifacts/results.csv', index=False)
```
Row count: 497 → 457.  
**Verification:** Regenerated tables confirm daily_exhausted: 0.0% ok, 100.0% rate_limited. ✅

---

### P2 Bug #3 — Third Paper Mislabelled as "RAG Survey" (FIXED)

**Location:** `docs/writeup.md` Section 1 table, comments in `relay/papers.py`  
**Symptom:** Paper `2507.07955v2` was described as "Advances in RAG systems" in the writeup table and some inline comments.  
**Actual title:** "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling (H-Net)"  
**Fix:** Updated writeup table row and relevant comments to use correct title.

---

## 4. Suspected Issues (Not Bugs)

### Issue #1 — 42/72 Eval Answers are `refused_wrongly`

The `openai/gpt-oss-20b` model consistently places citations at the end of paragraphs rather than inline after each claim. Because the citation validation layer strictly requires `[arxiv_id:setting:pNNN:cNNN]` after every factual assertion, 42 of 72 answerable questions resulted in `evidence_refusal` even though the model's underlying answer was factually correct.

This is a **design tension**, not a code bug: the strict citation policy trades recall for grounding integrity.

### Issue #2 — `retries=0` for All Scenario Runs

All scenario rows in `results.csv` show `retries=0`. The stand-in scenario runner dispatches requests open-loop over HTTP and treats 429 as terminal `rate_limited` without entering the full pipeline's retry path. The retry logic in `relay/control.py:relay_one_request()` is exercised only in the real eval runs (probe + retrieval_eval scenarios).

---

## 5. Test Results at Audit

```
98 passed in <runtime>
```

All test modules passed:
- `tests/test_standin.py` — stand-in behavior, rate limit simulation
- `tests/test_control.py` — BoundedQueue, RetryDecision, pipeline orchestration
- `tests/test_evidence.py` — BM25 retrieval, citation validation, evidence gate
- `tests/test_papers.py` — PDF ingestion, chunking (settings A/B/C)
- `tests/test_reporting.py` — Table generation, CSV schema validation

---

## 6. API Budget Used

| Run type | Real Groq calls | Notes |
|---|---|---|
| Probe run | 15 | Measured TTFT, latency distributions |
| Retrieval eval (A) | 24 | 24 questions × setting A |
| Retrieval eval (B) | 24 | 24 questions × setting B |
| Retrieval eval (C) | 24 | 24 questions × setting C |
| **Total** | **87** | ~8.7% of daily RPD=1,000 budget |

No stand-in scenario runs consumed real Groq API calls.
