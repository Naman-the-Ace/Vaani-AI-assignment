# Before vs. After Comparison — Vaani "The Relay"

This document records the exact numerical differences in report tables following the engineering fixes applied to the stand-in server and experimental logs.

---

## 1. Table 2a: Latency Changes (daily_exhausted)

| Metric | Before Fix (Old Run) | After Fix (Verified Run) | Note |
|---|---|---|---|
| Requests Offered | 40 | 40 | Unchanged |
| Requests Succeeded | 31 | 0 | Expected under 100% daily quota exhaustion |
| Requests Rate-limited | 9 | 40 | Truthiness bug fixed in `standin.py` |
| TTFT p50 / p95 | 15,338.7 ms / 27,416.6 ms | **not measured** / **not measured** | No tokens emitted under immediate 429 |
| Total p50 | 8,995.3 ms | **953.3 ms** | Fast 429 capacity notice response |
| Total p95 | 28,763.1 ms | **4,780.1 ms** | Bounded client wait time |

---

## 2. Table 2b: Outcome Changes (daily_exhausted)

| Scenario Metric | Before Fix | After Fix |
|---|---|---|
| Succeeded % | 77.5% (31/40) | **0.0% (0/40)** |
| Rate-limited % | 22.5% (9/40) | **100.0% (40/40)** |
| Timed out % | 0.0% | 0.0% |
| Peak Queue | 24 | **7** |
| Tokens/Answer | 39 | **not measured** (no full answers generated) |
| Cost/1,000 Answers | $0.0276 | **not measured** |

---

## 3. Paper Title Accuracy

| Field | Before Fix | After Fix |
|---|---|---|
| Paper 3 Title | "Advances in RAG systems" | **"Dynamic Chunking for End-to-End Hierarchical Sequence Modeling"** |
| Paper 3 Pages | 12 | **39** |
| Corpus Focus | General RAG survey | Hierarchical sequence modeling (H-Net) |
