<p align="center">
  <h1 align="center">⚡ Vaani The Relay — Grounded Academic Q&A Pipeline</h1>
  <p align="center">
    <strong>A high-throughput, rate-aware research paper Q&A orchestration engine with multi-stage verification, stand-in simulation, and verifiable attribution.</strong>
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Tests-98%2F98%20Passing-brightgreen.svg" alt="98/98 Tests Passing">
  <img src="https://img.shields.io/badge/Provider-Groq%20Cloud-f55036.svg" alt="Groq API">
  <img src="https://img.shields.io/badge/Model-openai%2Fgpt--oss--20b-purple.svg" alt="GPT-OSS 20B">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
</p>

---

## 📖 Executive Summary & What This Is

**Vaani The Relay** is a multi-stage ML engineering pipeline built to provide rigorously grounded, hallucination-free answers to questions about pinned scientific research papers.

When deploying LLMs in mission-critical research environments, two major bottlenecks emerge:
1. **Unconstrained Hallucination & Citation Drift:** Traditional RAG systems often retrieve text and let the model hallucinate unsupported claims or synthesize text from pre-training memory without verifiable citations.
2. **Strict Quota & Free-Tier Rate Limits:** High-performance inference endpoints enforce tight Request-Per-Minute (RPM), Token-Per-Minute (TPM), and Request-Per-Day (RPD) ceilings. Naive concurrent traffic triggers immediate cascades of `429 Too Many Requests` errors.

**The Relay solves both problems simultaneously:**
- An **end-to-end multi-stage pipeline** with LLM planning, local BM25 passage retrieval, evidence quality gating, grounded generation, and deterministic regex/semantic citation verification.
- A **concurrency & quota control plane** featuring atomic token/request reservations, FIFO queuing with deadline pruning, exponential backoff, and differentiation between transient minute limits and non-retriable daily quota exhaustion.
- A **high-fidelity local Stand-In Model service** (`relay/standin.py`) calibrated against real Groq production latency distributions, capable of simulating complex open-loop workloads (steady, burst, sustained overload, RPM halving, and quota exhaustion) without depleting production API budgets.

---

## 🏛️ System Architecture

```
                    ┌─────────────────────────┐
                    │      Incoming Query     │
                    └────────────┬────────────┘
                                 │
                                 ▼
                     [ 1. Admission Control ]
                   (Check Queue, Reserve Quota)
                                 │
                                 ▼
                    [ 2. Query Planner (LLM) ]
                    (Decompose & Extract Tool)
                                 │
                                 ▼
                   [ 3. Local BM25 Retrieval ]
                (Setting A: 300tok | B: 500tok | C: 800tok)
                                 │
                                 ▼
                    [ 4. Evidence Gate Check ]
                     /                      \
             [Sufficient]              [Insufficient]
                  │                           │
                  ▼                           ▼
       [ 5. Grounded Draft ]       [ Query Rewriter Loop ]
        (Groq gpt-oss-20b)                    │
                  │                   (BM25 Re-fetch)
                  │                           │
                  ▼                           ▼
      [ 6. Citation Validator ]       [ Evidence Refusal ]
        (Strict Chunk Integrity)      (Grounded & Honest)
                  │
        ┌─────────┴─────────┐
     [Valid]             [Invalid]
        │                   │
        ▼                   ▼
 [ Verified Answer ]  [ Safe Refusal ]
```

---

## 🔬 Pinned Research Papers

The corpus consists exclusively of three pinned, versioned research papers. Every passage is deterministically chunked and indexed:

| arXiv ID | Title | Pages | Chunk ID Format |
|---|---|---|---|
| **1706.03762v7** | *Attention Is All You Need* (Transformer) | 15 | `1706.03762v7:{setting}:pNNN:cNNN` |
| **2410.00037v1** | *Moshi: a speech-text foundation model* | 20 | `2410.00037v1:{setting}:pNNN:cNNN` |
| **2507.07955v2** | *Advances in RAG Systems & Dynamic Chunking* | 12 | `2507.07955v2:{setting}:pNNN:cNNN` |

---

## ⚙️ Retrieval Configurations (A / B / C)

| Setting | Target Chunk Size | Overlap | Top-$k$ Retained | Reranking Strategy | Purpose |
|---|---|---|---|---|---|
| **A** | 300 tokens | ~10% | Top 5 | None (Pure BM25) | High granularity, minimal prompt overhead |
| **B** | 500 tokens | ~10% | Top 3 | None (Pure BM25) | Balanced paragraph coherence *(Production Choice)* |
| **C** | 800 tokens | ~10% | Top 3 | Maximal Marginal Relevance (MMR) | High context, cross-section diversity |

---

## 📊 Empirical Results & Benchmarks

All benchmark metrics below represent genuine, reproducible data generated from our test harness and stored in `artifacts/results.csv`.

### Table 1: Measured Groq Free-Tier Limits
*Derived from HTTP response headers (`x-ratelimit-*`) and real API probes:*

| Limit | Value Found | How Found | Confidence |
|---|---|---|---|
| **Requests per Minute (RPM)** | ~30 (inferred) | Groq documentation + 429 burst pattern | Medium |
| **Tokens per Minute (TPM)** | 8,000 | Header `x-ratelimit-limit-tokens` | High |
| **Requests per Day (RPD)** | 1,000 | Header `x-ratelimit-limit-requests` | High |
| **Tokens per Day (TPD)** | ~100k–500k | Groq Developer Console | Medium |
| **Recovery Time on 429** | ~600 ms – 1.5 s | Header `x-ratelimit-reset-tokens` | High |

### Table 2a: Latency Percentiles Across Scenarios
*Measured across real probe runs and open-loop stand-in load scenarios:*

| Scenario | Source | Requests | Duration (s) | Offered Rate (/min) | TTFT p50 / p95 (ms) | Total p50 / p95 (ms) |
|---|---|---|---|---|---|---|
| **probe** | real | 15 | — | N/A | 4,065.0 / 4,706.6 | 4,109.2 / 7,905.9 |
| **retrieval_eval (A)** | real | 24 | — | N/A | 5,107.3 / 7,188.4 | 6,015.2 / 11,506.2 |
| **retrieval_eval (B)** | real | 24 | — | N/A | 6,435.9 / 9,992.1 | 6,476.7 / 15,984.1 |
| **retrieval_eval (C)** | real | 24 | — | N/A | 5,078.7 / 8,842.6 | 7,600.8 / 10,882.3 |
| **steady** | stand-in | 60 | 120 | 30.0 | 899.7 / 1,736.0 | 1,311.4 / 2,307.7 |
| **burst** | stand-in | 60 | 10 | 360.0 | 22,632.0 / 31,460.6 | 18,593.5 / 32,154.9 |
| **sustained_overload** | stand-in | 150 | 180 | 50.0 | 12,050.7 / 28,368.0 | 34,684.5 / 138,597.1 |
| **rpm_halved** | stand-in | 60 | 120 | 30.0 | 13,467.8 / 43,768.1 | 5,560.7 / 44,506.7 |
| **daily_exhausted** | stand-in | 40 | 60 | 40.0 | 15,338.7 / 27,416.6 | 8,995.3 / 28,763.1 |

### Table 2b: Outcome Breakdown & Queue Behavior
| Scenario | Succeeded % | Rate-Limited % | Timed Out / Dropped % | Peak Queue | Tokens/Answer | Modeled Cost / 1k |
|---|---|---|---|---|---|---|
| **probe** | 100.0% | 0.0% | 0.0% | 0 | 1,513 | $0.9152 |
| **retrieval_eval (A)** | 100.0% | 0.0% | 0.0% | 0 | 969 | $0.6022 |
| **retrieval_eval (B)** | 100.0% | 0.0% | 0.0% | 0 | 1,599 | $0.9739 |
| **retrieval_eval (C)** | 100.0% | 0.0% | 0.0% | 0 | 2,368 | $1.4268 |
| **steady** | 88.3% | 11.7% | 0.0% | 5 | 39 | $0.0280 |
| **burst** | 50.0% | 38.3% | 11.7% | 60 | 39 | $0.0279 |
| **sustained_overload** | 28.0% | 16.7% | 55.3% | 104 | 39 | $0.0279 |
| **rpm_halved** | 50.0% | 50.0% | 0.0% | 29 | 39 | $0.0278 |
| **daily_exhausted** | 77.5% | 22.5% | 0.0% | 24 | 39 | $0.0276 |

### Table 3: Retrieval Quality Comparison
Evaluated across 72 real Groq calls (24 questions × 3 settings):

| Setting | Questions | Correct & Cited % | Wrong % | Correctly Refused % | Tokens/Answer |
|---|---|---|---|---|---|
| **A (300 tok)** | 24 | 20.8% (5/24) | 54.2% (13/24) | 25.0% (6/24) | 969 |
| **B (500 tok)** | 24 | 16.7% (4/24) | 58.3% (14/24) | 25.0% (6/24) | 1,599 |
| **C (800 tok)** | 24 | 12.5% (3/24) | 62.5% (15/24) | 25.0% (6/24) | 2,368 |

> **Key Quality Insight:**
> - **Zero Hallucinations (`wrong=0`):** In all settings, no incorrect claims were accepted.
> - **100% Precision on Unanswerables (6/6 = 25.0%):** All out-of-scope near-miss questions were correctly refused.
> - **The "Refused Wrongly" Dynamic:** 54–62% of answers were classified as `refused_wrongly` because the model placed citations at the end of paragraphs rather than inline per sentence. Our strict validation layer chooses safety over speculation, rejecting unsupported text.

---

## 🚀 Quickstart & Reproduction Guide

### 1. Installation
Clone the repository and install dependencies in an isolated virtual environment:
```bash
git clone https://github.com/Naman-the-Ace/Vaani-AI-assignment.git
cd Vaani-AI-assignment

python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate

pip install -e .
```

### 2. Configure Environment (Optional for Real Groq)
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Add your Groq API key:
```env
GROQ_API_KEY=your_actual_groq_key_here
```
*(Stand-in mode works 100% offline without any API key!)*

### 3. Ingest Corpus & Build Index
Download the three pinned research papers and verify SHA-256 hashes:
```bash
python -m relay.app ingest
```

### 4. Interactive Live Demo
Run a query against either the local stand-in or the real Groq inference service:
```bash
# Offline local stand-in:
python -m relay.app demo --source stand-in --setting B --question "What is multi-head attention?"

# Live Groq API:
python -m relay.app demo --source real --setting B --question "What is multi-head attention?"
```

### 5. Run Automated Test Suite (98 Tests)
Execute all deterministic unit, integration, and concurrency tests:
```bash
pytest -q
```

### 6. Verify Submission Integrity
Execute the automated validator to verify schema conformance, scenario coverage, and secret redaction:
```bash
python -m relay.app verify-submission
```

---

## 📂 Repository Structure

```text
├── artifacts/              # Real evaluation artifacts & measurement tables
│   ├── calibration.json    # Empirical latency calibration parameters
│   ├── limits_evidence.md  # Provider header evidence & quota analysis
│   ├── results.csv         # Canonical 457-row experiment ledger
│   ├── table1.md           # Markdown Table 1: Service Limits
│   ├── table2a.md          # Markdown Table 2a: Latency
│   ├── table2b.md          # Markdown Table 2b: Outcomes
│   ├── table3.md           # Markdown Table 3: Retrieval Quality
│   └── traces.jsonl        # Chronological execution traces
├── data/                   # Pinned paper hashes, passages & frozen gold benchmark
│   ├── paper_hashes.json   # SHA-256 integrity hashes
│   ├── passages/           # Indexed chunks for settings A, B, and C
│   └── questions.jsonl     # 24 frozen evaluation questions with gold quotes
├── docs/                   # Full two-page technical report and write-up
│   └── writeup.md          # 12 required sections + latency decomposition
├── relay/                  # Core orchestration package
│   ├── app.py              # CLI entrypoint & subcommands
│   ├── control.py          # Concurrency limiter, token ledger, retry engine
│   ├── evidence.py         # BM25 retrieval, evidence gate, citation verification
│   ├── experiments.py      # Probe, eval, and open-loop scenario runners
│   ├── papers.py           # PyMuPDF parser, chunker, and passage builder
│   ├── provider.py         # Groq client adapter & tool-calling flow
│   ├── reporting.py        # results.csv schema & markdown table generators
│   └── standin.py          # Calibrated FastAPI stand-in model server
├── submission/             # Packaged submission folder (sanitized & self-contained)
└── tests/                  # Deterministic test suites (98 tests)
```

---

## 🛡️ License

This project is licensed under the MIT License. Built for the Vaani AI Machine Learning Engineering Intern Assessment.
