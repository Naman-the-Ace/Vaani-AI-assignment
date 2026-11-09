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
