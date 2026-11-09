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
