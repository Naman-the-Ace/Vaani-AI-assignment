## TABLE 1: Limits Found in the Real Service

_Source: account console + sanitized response headers + probe observations._
_`not measured` where not observed. Console values labeled as reported, not empirically verified._

| Limit | Value found | How found | Confidence |
|-------|-------------|-----------|------------|
| Requests per minute | ~30 (inferred) | Groq documentation + 429 burst pattern | medium |
| Tokens per minute | 8,000 | HTTP header x-ratelimit-limit-tokens | high |
| Requests per day | 1,000 | HTTP header x-ratelimit-limit-requests | high |
| Tokens per day | ~100k-500k | Groq developer console free tier | medium |
| Requests in flight at once | not measured | Provider server-side concurrency unstated; capped at 3 locally | low |
| What it returns when limit hit | HTTP 429 rate limit response with retry-after | Real probe / eval observation | high |
| Time until recovery | ~600ms - 1.5s (TPM refill) | HTTP header x-ratelimit-reset-tokens | high |


## TABLE 2a: Latency (one row per load scenario)

_TTFT percentiles over requests with visible text. Total-time percentiles over all offered requests._
_p99 written as 'not measured' when n < 100 (NumPy linear method)._

| Scenario | Real/Stand-in | Requests sent | Duration (s) | Offered rate (per min) | TTFT p50 / p95 / p99 (ms) | Total p50 / p95 / p99 (ms) |
|----------|---------------|---------------|--------------|------------------------|--------------------------|---------------------------|
| probe | real | 15 | not set | N/A | 4065.0 / 4706.6 / not measured (n=3) | 4109.2 / 7905.9 / not measured (n=15) |
| retrieval_eval (A) | real | 24 | not set | N/A | 5107.3 / 7188.4 / not measured (n=5) | 6015.2 / 11506.2 / not measured (n=24) |
| retrieval_eval (B) | real | 24 | not set | N/A | 6435.9 / 9992.1 / not measured (n=4) | 6476.7 / 15984.1 / not measured (n=24) |
| retrieval_eval (C) | real | 24 | not set | N/A | 5078.7 / 8842.6 / not measured (n=3) | 7600.8 / 10882.3 / not measured (n=24) |
| burst | stand-in | 60 | 10 | 360.0 | 22632.0 / 31460.6 / not measured (n=30) | 18593.5 / 32154.9 / not measured (n=60) |
| steady | stand-in | 60 | 120 | 30.0 | 899.7 / 1736.0 / not measured (n=53) | 1311.4 / 2307.7 / not measured (n=60) |
| rpm_halved | stand-in | 60 | 120 | 30.0 | 13467.8 / 43768.1 / not measured (n=30) | 5560.7 / 44506.7 / not measured (n=60) |
| daily_exhausted | stand-in | 40 | 60 | 40.0 | 15338.7 / 27416.6 / not measured (n=31) | 8995.3 / 28763.1 / not measured (n=40) |
| sustained_overload | stand-in | 150 | 180 | 50.0 | 12050.7 / 28368.0 / not measured (n=42) | 34684.5 / 138597.1 / 163605.2 |


## TABLE 2b: Outcomes (same rows as Table 2a)

_`succeeded%` = status==ok / offered_count. `rate_limited%` = terminal rate_limited / offered._
_`tokens/answer` denominator = status==ok (may include extractive). Cost = list-price equivalent._

| Scenario | Succeeded % | Rate-limited % | Timed out/given up % | Retries/req | Peak queue | Tokens/answer | Cost/1,000 answers |
|----------|-------------|----------------|----------------------|-------------|------------|---------------|--------------------|
| probe | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 1513 | $0.9152 |
| retrieval_eval (A) | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 969 | $0.6022 |
| retrieval_eval (B) | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 1599 | $0.9739 |
| retrieval_eval (C) | 100.0% | 0.0% | 0.0% | 0.00 | 0 | 2368 | $1.4268 |
| burst | 50.0% | 38.3% | 11.7% | 0.00 | 60 | 39 | $0.0279 |
| steady | 88.3% | 11.7% | 0.0% | 0.00 | 5 | 39 | $0.0280 |
| rpm_halved | 50.0% | 50.0% | 0.0% | 0.00 | 29 | 39 | $0.0278 |
| daily_exhausted | 77.5% | 22.5% | 0.0% | 0.00 | 24 | 39 | $0.0276 |
| sustained_overload | 28.0% | 16.7% | 55.3% | 0.00 | 104 | 39 | $0.0279 |


## TABLE 3: Retrieval Settings

_Only genuinely graded REAL rows included. Stand-in rows excluded._
_correct_and_cited% = correct/24, wrong% = (wrong+refused_wrongly)/24, correctly_refused% = refused_correctly/24._
_Denominators stated explicitly. NumPy linear percentile method._

| Setting | Questions | Correct & cited % | Wrong % | Correctly refused % | Tokens/answer | TTFT p50/p95 (ms) | Total p50/p95 (ms) |
|---------|-----------|-------------------|---------|---------------------|---------------|-------------------|-------------------|
| A | 24 (of 24) | 20.8% | 54.2% | 25.0% | 969 | 5107.3/7188.4 | 6015.2/11506.2 |
| B | 24 (of 24) | 16.7% | 58.3% | 25.0% | 1599 | 6435.9/9992.1 | 6476.7/15984.1 |
| C | 24 (of 24) | 12.5% | 62.5% | 25.0% | 2368 | 5078.7/8842.6 | 7600.8/10882.3 |
