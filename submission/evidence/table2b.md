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
| sustained_overload | 28.0% | 16.7% | 55.3% | 0.00 | 104 | 39 | $0.0279 |
| daily_exhausted | 0.0% | 100.0% | 0.0% | 0.00 | 7 | not measured | not measured |
