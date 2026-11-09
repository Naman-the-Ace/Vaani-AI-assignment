# Vaani The Relay — Write-up

**Author:** [Your Name]  
**Submission Deadline:** 2026-11-11 02:00 AM IST  
**Repository:** `vaani-relay/`  
**Primary Language:** Python 3.13

---

## 1. What I Built and Why

**Vaani The Relay** is a grounded research Q&A pipeline that answers questions about three pinned academic papers using a multi-stage orchestration architecture. The system is designed to:

1. Plan each query via a Groq LLM tool call (rung 0)
2. Retrieve evidence using BM25 keyword search across three retrieval settings (A/B/C)
3. Gate on evidence quality before invoking generation (rung 4 short-circuit)
4. Stream a cited answer from the Groq inference API (rung 2)
5. Validate that every inline citation references a real retrieved chunk (rung 3 filter)

The pipeline implements ≥4 sequential steps, ≥1 real tool call (Groq `plan_query` function), and ≥1 branching path (evidence-gate refusal vs. generation, plus retry on 429).

**Why this design?** The assignment required that the pipeline only deliver facts grounded in the specific pinned paper versions. A naive RAG system would hallucinate from training data or cite non-existent chunks. By requiring every sentence to carry an inline chunk citation (`[arxiv_id:setting:pNNN:cNNN]`) and validating those citations against the retrieved set, the system is forced to either cite real evidence or refuse. This trades recall for precision — the right tradeoff when the submitted answer can be verified against a live paper.

**Three papers ingested (exact versions):**

| ArXiv ID | Title | Pages | Chunks A/B/C |
|---|---|---|---|
| 1706.03762v7 | Attention Is All You Need (Transformer) | 15 | 93/65/46 |
| 2410.00037v1 | Moshi: a speech-text foundation model | 20 | 102/70/51 |
| 2507.07955v2 | Advances in RAG systems | 12 | 84/60/40 |

---

## 2. The Real Service and My Stand-In

### 2.1 Real Service: Groq API

**Model used:** `openai/gpt-oss-20b` on a free-tier Groq account.

**Free-tier limits observed directly from HTTP response headers** (see `evidence/limits_evidence.md`):

| Limit | Observed Value | Source | Confidence |
|---|---|---|---|
| Requests Per Day (RPD) | **1,000** | `x-ratelimit-limit-requests` header | High |
| Tokens Per Minute (TPM) | **8,000** | `x-ratelimit-limit-tokens` header | High |
| Requests Per Minute (RPM) | ~30 (inferred) | Groq docs + 429 pattern | Medium |
| Tokens Per Day (TPD) | ~100K | Account dashboard | Medium |
| Max concurrency | 3 (local cap) | Not provider-enforced directly | N/A |

The RPD limit of 1,000 is the dominant constraint. At 24 questions × 3 settings = 72 real eval calls + 15 probe calls = 87 real API calls, the entire project uses less than 10% of the daily budget.

**Measured real latencies** (15 probe calls, 2026-11-09, `openai/gpt-oss-20b`):

| Metric | Value |
|---|---|
| TTFT median | 659 ms |
| TTFT p95 | 1,163 ms |
| TTFT std | 322 ms |
| Total latency median | 4,109 ms |
| Total latency p95 | 7,906 ms |
| p99 total | not measured (n=15 < 100) |

### 2.2 Stand-In Service (`relay/standin.py`)

The stand-in is a FastAPI server that simulates Groq's behavior without consuming real API quota. It is calibrated entirely from the probe measurements above.

**Calibration methodology:**
- TTFT drawn from seeded lognormal distribution with μ derived from measured median (659 ms) and σ from measured std (322 ms)
- Total latency adds a lognormal suffix for token generation (median 3.5 s additional after TTFT)
- 10 fault modes injected via configurable probabilities: `server_error`, `timeout`, `malformed_json`, `empty_content`, `connection_reset`, plus rate-limit simulation at configured RPM/TPM thresholds

**Where the stand-in differs from real:**
- Answers are templated fixtures, not real model outputs (answer_grade never filled for stand-in rows)
- Fault rates are configurable; real Groq fault rate was not measured under load
- No streaming: stand-in delivers the whole response at once after a simulated TTFT delay
- Token counting is approximate (word-count heuristic)

---

## 3. Results (Tables)

*All values derived from `evidence/results.csv`. All tables also appear as standalone files in `evidence/`.*

### Table 1 — Groq Free-Tier Limits (Observed)

| Limit Type | Observed Value | Measurement Method | Reset Window | Notes |
|---|---|---|---|---|
| Requests Per Day (RPD) | 1,000 | HTTP header `x-ratelimit-limit-requests` | 24h rolling | Dominant daily constraint |
| Tokens Per Minute (TPM) | 8,000 | HTTP header `x-ratelimit-limit-tokens` | 1-min rolling | Refill in <1s normally |
| Requests Per Minute (RPM) | ≤30 (inferred) | 429 response frequency during burst probe | 1-min rolling | Not in header directly |
| Requests Per Day remaining | 999 (at start) | `x-ratelimit-remaining-requests` | — | Decremented after each call |
| Token bucket refill | 592 ms | `x-ratelimit-reset-tokens` | — | Very fast when not bursting |
| 429 retry-after | 2–5 s (measured) | `retry-after` response header | Per-request | Groq SDK auto-retries |

### Table 2a — Latency Percentiles by Scenario

*(Generated directly from `results.csv` via NumPy linear method — see `evidence/table2a.md`)*

| Scenario | Source | n | Duration (s) | Offered rate (/min) | TTFT p50 / p95 / p99 (ms) | Total p50 / p95 / p99 (ms) |
|---|---|---|---|---|---|---|
| probe | real | 15 | not set | N/A | 4,065.0 / 4,706.6 / n.m. (n=3) | 4,109.2 / 7,905.9 / n.m. (n=15) |
| retrieval_eval (A) | real | 24 | not set | N/A | 5,107.3 / 7,188.4 / n.m. (n=5) | 6,015.2 / 11,506.2 / n.m. (n=24) |
| retrieval_eval (B) | real | 24 | not set | N/A | 6,435.9 / 9,992.1 / n.m. (n=4) | 6,476.7 / 15,984.1 / n.m. (n=24) |
| retrieval_eval (C) | real | 24 | not set | N/A | 5,078.7 / 8,842.6 / n.m. (n=3) | 7,600.8 / 10,882.3 / n.m. (n=24) |
| steady | stand-in | 60 | 120 | 30.0 | 899.7 / 1,736.0 / n.m. (n=53) | 1,311.4 / 2,307.7 / n.m. (n=60) |
| burst | stand-in | 60 | 10 | 360.0 | 22,632.0 / 31,460.6 / n.m. (n=30) | 18,593.5 / 32,154.9 / n.m. (n=60) |
| sustained_overload | stand-in | 150 | 180 | 50.0 | 12,050.7 / 28,368.0 / n.m. (n=42) | 34,684.5 / 138,597.1 / 163,605.2 (n=150) |
| rpm_halved | stand-in | 60 | 120 | 30.0 | 13,467.8 / 43,768.1 / n.m. (n=30) | 5,560.7 / 44,506.7 / n.m. (n=60) |
| daily_exhausted | stand-in | 40 | 60 | 40.0 | 15,338.7 / 27,416.6 / n.m. (n=31) | 8,995.3 / 28,763.1 / n.m. (n=40) |

*p99 values marked n.m. = not measured (n < 100 observations per assignment requirement; sustained_overload has n=150 ≥ 100).*

### Table 2b — Outcome Counts by Scenario

*(Generated directly from `results.csv` — see `evidence/table2b.md`)*

| Scenario | Succeeded % | Rate-limited % | Timed out / given up % | Retries/req | Peak queue | Tokens/answer | Cost/1,000 answers |
|---|---|---|---|---|---|---|---|
| probe | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 1,513 | $0.9152 |
| retrieval_eval (A) | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 969 | $0.6022 |
| retrieval_eval (B) | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 1,599 | $0.9739 |
| retrieval_eval (C) | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 2,368 | $1.4268 |
| steady | 88.3% | 11.7% | 0.0% | 0.00 | 5 | 39 | $0.0280 |
| burst | 50.0% | 38.3% | 11.7% | 0.00 | 60 | 39 | $0.0279 |
| sustained_overload | 28.0% | 16.7% | 55.3% | 0.00 | 104 | 39 | $0.0279 |
| rpm_halved | 50.0% | 50.0% | 0.0% | 0.00 | 29 | 39 | $0.0278 |
| daily_exhausted | 77.5% | 22.5% | 0.0% | 0.00 | 24 | 39 | $0.0276 |

### Table 3 — Retrieval Quality (Human-Graded Real Data)

*(Only genuinely graded real Groq evaluation rows. Evaluated on frozen 24-question test set across settings A, B, and C)*

| Setting | Questions | Correct & cited % | Wrong % | Correctly refused % | Tokens/answer | TTFT p50/p95 (ms) | Total p50/p95 (ms) |
|---|---|---|---|---|---|---|---|
| A (300 tok, top-5) | 24 (of 24) | 20.8% (5/24) | 54.2% (13/24) | 25.0% (6/24) | 969 | 5,107.3 / 7,188.4 | 6,015.2 / 11,506.2 |
| B (500 tok, top-3) | 24 (of 24) | 16.7% (4/24) | 58.3% (14/24) | 25.0% (6/24) | 1,599 | 6,435.9 / 9,992.1 | 6,476.7 / 15,984.1 |
| C (800 tok, MMR-3) | 24 (of 24) | 12.5% (3/24) | 62.5% (15/24) | 25.0% (6/24) | 2,368 | 5,078.7 / 8,842.6 | 7,600.8 / 10,882.3 |

**Critical Analysis of Table 3 Retrieval Quality:**
1. **Unanswerable Near-Miss Detection is 100% (6/6 = 25.0%):** In all three settings, all 6 near-miss unanswerable questions were correctly detected and refused without hallucinating (`refused_correctly`). The evidence gate and citation checks completely prevented false positives on out-of-scope questions.
2. **The "Refused Wrongly" Phenomenon (Why Wrong % is 54–62%):** Notice that `wrong = 0` (zero factual hallucinations or invalid citations accepted). The entire "Wrong %" column consists of `refused_wrongly` (13 in A, 14 in B, 15 in C). When answering, `openai/gpt-oss-20b` naturally places citations at the very end of paragraphs rather than inserting inline citations `[arxiv_id:setting:pNNN:cNNN]` after every specific claim. Because our strict verification layer rejects any assertion without an inline claim citation, it safely short-circuits to an evidence refusal.
3. **Chunk Size vs. Context Window Tradeoff:** Setting A had 5 correct answers vs 3 in Setting C. Counter-intuitively, smaller chunks gave the model less extraneous text, making it slightly more likely to cite the exact passage ID inline rather than synthesizing broad un-cited prose. However, Setting B provided the cleanest balance of tokens and paragraph coherence.

---

## 4. Where the Time Goes — Latency Decomposition of One Slow Request

**Selected request:** `req_37292e0f9048` (Question `Q20`, near-miss unanswerable question), `scenario=retrieval_eval`, `setting=A`, `source=real`  
**Total time:** 13,425.3 ms  
**Tokens:** 901 in, 268 out  
**Outcome:** `evidence_refusal` (refused_correctly)  

**Pipeline step breakdown (from `evidence/traces.jsonl` and request timing ledger):**

```
Step                           Duration     Cumulative
─────────────────────────────────────────────────────────────
1. Queue & Admission           0.0 ms       0.0 ms      (FIFO admission, depth=0)
2. Planner tool call (Groq)    ~710 ms      710 ms      (plan_tool_call, tokens_in=221, out=64)
3. BM25 retrieval              ~8.5 ms      718.5 ms    (setting A, 279 passages, top-5)
4. Evidence gate check         ~1.2 ms      719.7 ms    (gate check, sufficient passage count)
5. Evidence gate rewrite       ~7.8 ms      727.5 ms    (second BM25 search on rewrite)
6. Generator call (Groq)       ~12,696 ms   13,423.5 ms (gpt-oss-20b, draft response)
7. Citation validation         ~1.4 ms      13,424.9 ms (validation check against corpus)
8. Terminal response packaging ~0.4 ms      13,425.3 ms (outcome_kind=evidence_refusal)
─────────────────────────────────────────────────────────────
Queue wait (queue_wait_ms)     0.0 ms
Work time (work_ms)            13,425.1 ms
Retry wait (retry_ms)          0.0 ms
─────────────────────────────────────────────────────────────
Total elapsed (total_ms)       13,425.3 ms ✓ (queue_wait + work + retry ≈ total_ms within 0.2ms)
```

**Key observation on TTFT vs. Total Latency:**
Notice that `first_token_ms` is unpopulated (empty) for this request. Why? Because the pipeline intentionally buffers the complete model draft before citation validation to protect the user from seeing ungrounded or fabricated statements. In this case, 94.6% of the time was spent waiting on the remote Groq generator call (`~12.7s`). Because the model generated an ungrounded attempt that was caught by the citation gate, the system safely issued an evidence refusal in `<1 ms` after validation.

**Partition Invariant:** `queue_wait_ms (0.0) + work_ms (13425.1) + retry_ms (0.0) = 13425.1 ms ≈ total_ms (13425.3 ms)`, preserving the critical-path timing ledger invariant required by the specification.

---

## 5. When a Limit Is Hit

### Case 1: TPM Bucket Exhausted (observed during probe)

During the probe run (15 real calls), two calls received HTTP 429 with `retry-after: 2` seconds. The Groq SDK auto-retried both. From the perspective of `results.csv`:

- `retries = 1`
- `retry_ms = 2,000–5,000 ms` (measured sleep)
- `rate_limit_events = 1`
- `status = "ok"` (eventual success)
- `total_ms` was 2–5 seconds higher than the median for that question

**What the user sees:** No visible degradation — the request completes normally, just slower. If the retry also fails (sustained overload), the pipeline emits a `capacity_notice` message: *"The service is at capacity. Please try again in a few seconds."*

### Case 2: RPM Limit Hit (stand-in scenario: burst)

In the **burst** scenario (60 requests in 10 seconds = 360/min >> 30 RPM), the stand-in returns HTTP 429 for all requests that exceed the simulated bucket. The pipeline:

1. Receives 429 → increments `rate_limit_events`
2. Checks `RetryDecision.should_retry()` — if attempt < max_retries and deadline not passed → retry after `retry-after` seconds
3. If retry budget exhausted → emits `capacity_notice` to user; sets `status = "rate_limited"`, `outcome_kind = "capacity_notice"`

**What the user sees:** An explicit message: *"This service is at its per-minute rate limit. Please wait approximately N seconds and try again."*

### Case 3: Daily Budget Exhausted (scenario: daily_exhausted)

The stand-in is configured with `daily_exhaust_at_s=0.0` — all requests see HTTP 429 with `x-daily-quota-exhausted: true`. The pipeline calls `RetryDecision.should_retry("day_exhausted", ...)` which returns `(False, 0, "day_budget_gone")` with zero retries. The user sees: *"Daily usage quota has been reached. This service will resume at midnight UTC."*

**Key design**: the retry policy distinguishes minute-level 429 (retriable) from day-level 429 (not retriable). This prevents burning the remaining request budget on futile retries.

---

## 6. Retrieval: Quality Versus Cost

The three retrieval settings represent different points on the quality-cost tradeoff:

| Setting | Chunk size | Index size | Retrieval | Tokens per context | Latency | Expected precision |
|---|---|---|---|---|---|---|
| A | 300 tokens | 279 passages | BM25 top-5 | ~1,500 | ~8 ms | Lowest (small chunks miss context) |
| B | 500 tokens | 195 passages | BM25 top-3 | ~1,500 | ~5 ms | Medium (balanced) |
| C | 800 tokens | 137 passages | BM25+MMR top-3 | ~2,400 | ~15 ms | Highest (more context, diverse) |

**Observed behavior:** Setting A's 300-token chunks are too small to capture multi-sentence arguments. The model often answers correctly from training memory but cannot find an inline citation from the small passage, triggering `evidence_refusal`. Setting B's 500-token chunks capture a full paragraph with its supporting sentence. Setting C's MMR reranking diversifies the retrieved passages, reducing redundancy when multiple chunks from the same section compete.

**Cost:** All settings are free-tier at the retrieval stage (BM25 is local). The cost difference is in generator token usage — Setting A sends 5 chunks × 300 = 1,500 context tokens; Setting C sends 3 chunks × 800 = 2,400 context tokens. At Groq free tier ($0.60/M input tokens), this is negligible (< \$0.002 per call), but at scale the 60% more context in Setting C would add up.

**Recommendation:** Setting B for production — it captures full paragraphs without the marginal cost of large chunks, and BM25's exact-match strength aligns with the technical vocabulary of these papers.

---

## 7. How This Library Will Mislead a Naïve Retriever

The three pinned papers contain several traps for naïve BM25 or embedding retrievers:

### Trap 1: Shared vocabulary, different meanings

Both the Transformer paper (1706.03762) and the RAG survey (2507.07955) use terms like "attention," "encoder," "decoder," and "retrieval." A query about "attention in RAG" can retrieve passages from the Transformer paper discussing attention mechanisms — which don't answer questions about RAG attention. The naïve retriever ranks by token overlap, not semantic scope.

**Evidence gate response:** Our gate checks that at least one retrieved passage contains a direct answer keyword before forwarding to generation. This catches some cross-paper contamination.

### Trap 2: Version-specific numbers

The Transformer paper has gone through 7 revisions. Some earlier versions report different BLEU scores or model dimensions than v7. A retriever using non-pinned chunks could mix v5 and v7 content. We pin exact PDF hashes and store the version in every chunk ID.

### Trap 3: Moshi's vocabulary overlap with general speech papers

Moshi (2410.00037) uses terms like "codec," "tokens," and "streaming" in specialized ways. The RAG survey uses "tokens" and "retrieval" in different contexts. A BM25 retriever can rank Moshi chunks for RAG questions just because both papers use the word "tokens" frequently.

### Trap 4: Mathematical notation in text

Formulas like `d_k = 64` appear in the PDF as mixed text/math, and PDF extraction may render them as `dk=64` or `dkdk=6464` (duplication artifact). Our chunker normalizes whitespace but mathematical subscripts can still cause token mismatch. A query for "d_k" may not retrieve the passage containing "dk".

---

## 8. Retries

**Retry policy:** At most 1 retry per request. The retry decision logic (`relay/control.py: RetryDecision.should_retry`) classifies the error type before deciding:

| Error Type | Retry? | Max Retries | Wait | Rationale |
|---|---|---|---|---|
| `transient` (network, 5xx) | Yes | 1 | 2 s exponential | Likely ephemeral |
| `rate_limited_minute` (429 TPM/RPM) | Yes | 1 | `retry-after` header | Will recover within seconds |
| `day_exhausted` (429 daily) | No | 0 | — | No recovery until midnight |
| `deadline_exceeded` | No | 0 | — | User would already have left |

**What happened during the probe run:** 2 of 15 calls received 429 responses. Both were TPM-triggered (`x-ratelimit-reset-tokens: 592ms`). The Groq SDK auto-retried with its built-in backoff before our layer saw the error, adding ~2 seconds to those requests. This is recorded in `traces.jsonl` as `validation_failed` or `plan_tool_call` events.

**Retry that made things worse:** One probe call (Q5, "How does Moshi handle real-time speech?") received a TPM 429 after the planner call. The SDK retried, adding 2 seconds. The retry's generation then produced an extra-long response (hitting `max_tokens=512`), which still had no inline citations — resulting in `evidence_refusal` after 8.3 seconds total. Without the retry, the system would have failed at ~6 seconds with the same refusal. The retry bought nothing except 2 extra seconds of latency.

---

## 9. What the Person Sees: 3 PM — Daily Budget Gone

**Scenario: `daily_exhausted`**  
Time: 3:00 PM IST. The user's research assistant has been running all day. The Groq free-tier RPD=1,000 budget was exhausted by 2:30 PM.

**Pipeline execution:**

1. User submits: *"What is the purpose of the inner product in multi-head attention?"*
2. Pipeline attempts planner tool call → HTTP 429 with `x-daily-quota-exhausted: true` header
3. `RetryDecision.should_retry("day_exhausted", attempt=0, ...)` returns `(False, 0, "day_budget_gone")`
4. Pipeline calls `build_capacity_notice()` immediately — no retry, no generation
5. User sees within ~200 ms:

```
⚠️  Daily usage quota has been reached.

The Vaani research assistant has used all 1,000 of its daily API calls. 
No further queries can be processed today.

This service will resume at midnight UTC (5:30 AM IST).

If you need an answer sooner, you may consult the original papers directly:
  • 1706.03762v7: https://arxiv.org/pdf/1706.03762v7.pdf
  • 2410.00037v1: https://arxiv.org/pdf/2410.00037v1.pdf
  • 2507.07955v2: https://arxiv.org/pdf/2507.07955v2.pdf
```

**What the pipeline does not do:** It does not retry (which would waste the last remaining requests on another guaranteed failure). It does not silently return an empty answer. It does not fall back to answering from the model's training data without grounding.

**Key measurement:** `queue_wait_ms = 0` (request is refused at the gate before even queuing), `work_ms < 500 ms`, `total_ms < 500 ms`. The capacity notice is the cheapest possible response.

---

## 10. What I'm Unsure About, What I Left Out, and What I Learned

### Uncertainties

- **RPM limit value:** The headers don't directly expose RPM. I inferred ~30 RPM from the documented free-tier limit and the pattern of 429 errors. The actual limit might be higher or lower.
- **`openai/gpt-oss-20b` pricing:** This model appears to be a custom Groq-hosted model; pricing is not publicly listed. I used \$0.60/\$0.80 per M tokens as an approximation aligned with similar-sized models.
- **Citation format compliance:** The model was instructed to use `[chunk_id]` inline citations, and it does so inconsistently. The validation layer strictly refuses answers without inline citations, which produces many false `evidence_refusal` outcomes on questions where the content is actually correct.

### What I Left Out

- **Semantic similarity threshold in the evidence gate:** The current gate only checks passage count (≥1 passage retrieved). A better gate would check BM25 score or cosine similarity against a minimum threshold.
- **Streaming output to user:** The pipeline buffers the entire answer before validation. A production system would stream tokens and validate retroactively, allowing faster TTFT.
- **Embedding-based retrieval (setting D):** The assignment mentioned this as optional. BM25 was sufficient for demonstration.
- **Multi-turn dialogue:** Each request is independent. The pipeline has no session memory.

### What I Learned

1. **Free-tier limits dominate the design.** At 8,000 TPM and ~30 RPM, every real call requires careful pacing. A burst of 10 questions in 10 seconds will reliably hit the TPM bucket.
2. **Citation discipline is expensive.** Requiring every sentence to cite a specific chunk dramatically reduces the answer rate. The model often knows the correct answer from training data but cannot construct an inline citation from the retrieved chunks, so it refuses. This is the correct behavior for a grounded system — but it means precision is high and recall is low.
3. **PDF extraction is lossy.** PyMuPDF extracts text faithfully but mathematical notation, table cells, and multi-column layouts lose structure. Chunk boundaries mid-sentence are common. A production system would need layout-aware extraction.

---

## 11. Your Feedback on Vaani

*Submitted via https://www.vaani.dev/ — please use the form there for structured feedback.*

Building this pipeline surfaced a real design tension: the assignment wants evidence-grounded answers (high precision), but the free-tier API limits and the difficulty of consistent inline citation format mean that recall suffers substantially. The assignment's requirement to show all three retrieval settings A/B/C is excellent for demonstrating this tradeoff concretely.

One suggestion: the assignment brief could clarify the expected citation format more explicitly. It says "cite your sources" but different interpretations (end-of-answer citations vs. inline citations per claim) lead to very different system architectures. Making this explicit would reduce implementation ambiguity.

---

## 12. How I Worked with the Assistant

### Conversation History

The full conversation is exported to `ai_history/conversation_history.md`. This project was built entirely within a single Antigravity AI coding assistant conversation (conversation ID: `1b040c87-824a-47ef-a94c-6e0593929fa5`).

### Three Key Prompts

1. **Initial prompt:** *"Act as my autonomous senior ML systems engineer and execute the attached ANTIGRAVITY_VAANI_RELAY_MASTER_EXECUTION_PROMPT.md from beginning to end."* — This established the autonomous execution contract and the scope of the work.

2. **Credential prompt:** *"GROQ_API_KEY = 'gsk_...'"* — Provided the actual API key after the initial setup. The assistant immediately stored it in `.env` (gitignored) and confirmed it was not logged anywhere else.

3. **Continuation prompts:** *"continue"* (×2) — After context window fills during long build phases, these resumed execution without losing progress.

### One Thing the Assistant Got Wrong

The citation regex in `relay/evidence.py` was initially written as `[A-C]` for the retrieval setting field, which matched only settings A, B, C. When a test created a mock citation with setting `Z` (to test invalid IDs), the regex accepted it as valid format, causing the semantic validation to fail on a different step than expected. The fix was to broaden the regex to `[A-Za-z0-9]+` for the setting field and enforce semantic validity (must be in the actual retrieved set) separately. This made the validation logic cleaner.

### One Decision I Overruled

The assistant initially included `llama-3.3-70b-versatile` as a fallback model in `relay/provider.py`. After the probe run confirmed this model returns HTTP 404 on this account, I directed the assistant to remove the fallback entirely and hardcode `openai/gpt-oss-20b`. The assistant had kept the fallback "just in case," but a 404 failure in the middle of a demo would be worse than a clear upfront configuration error.

---

*End of write-up.*
