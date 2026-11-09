# Vaani The Relay — Standalone Codebase

This package contains the complete, reproducible source code for **Vaani The Relay**, a grounded academic paper Q&A pipeline with quota management, rate limiting, and multi-stage orchestration.

## Quickstart

### 1. Requirements & Setup
- Python 3.11+ (tested on Python 3.13)
- Create and activate a virtual environment:
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
```

- Install package dependencies:
```bash
pip install -e .
```

### 2. Configuration & API Key
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Provide your Groq API key:
```env
GROQ_API_KEY=your_groq_api_key_here
```
*(Note: All standalone stand-in simulations run without requiring an external API key).*

### 3. Running Core Commands

#### Ingest Pinned Academic Papers
Downloads and verifies SHA-256 hashes of the three pinned research papers:
```bash
python -m relay.app ingest
```

#### Run Interactive Demo
Query the pipeline using either the real Groq API or the calibrated local stand-in service:
```bash
# Stand-in mode (no API key required):
python -m relay.app demo --source stand-in --setting B --question "What is multi-head attention?"

# Real Groq mode:
python -m relay.app demo --source real --setting B --question "What is multi-head attention?"
```

#### Run All 98 Automated Tests
Deterministic unit and integration tests covering papers, control plane, evidence gates, stand-in HTTP/SSE service, and reporting invariants:
```bash
pytest -q
```

#### Regenerate Report Tables
Recomputes Table 1 (Limits), Table 2a (Latency), Table 2b (Outcomes), and Table 3 (Retrieval) from `results.csv`:
```bash
python -m relay.app tables
```

#### Verify Full Submission Integrity
Runs automated self-verification covering schema, invariant checks, scenario coverage, and secret scanning:
```bash
python -m relay.app verify-submission
```
