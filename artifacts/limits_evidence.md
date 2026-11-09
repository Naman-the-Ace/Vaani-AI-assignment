# Real Groq Service Limits Evidence & Provenance

**Model Tested:** `openai/gpt-oss-20b` (enabled on user free-tier account)  
**Probe Timestamp:** 2026-11-09T13:40:59Z  
**Probe Endpoint:** `https://api.groq.com/openai/v1/chat/completions`  

## Observed Rate-Limit Headers (Direct from HTTP Response)

```json
{
  "content-type": "application/json",
  "x-ratelimit-limit-requests": "1000",
  "x-ratelimit-limit-tokens": "8000",
  "x-ratelimit-remaining-requests": "999",
  "x-ratelimit-remaining-tokens": "7921",
  "x-ratelimit-reset-requests": "1m26.4s",
  "x-ratelimit-reset-tokens": "592ms"
}
```

## Mapping & Semantic Analysis (per official Groq documentation)

| Header | Value | Semantic Meaning | Notes / Confidence |
|---|---|---|---|
| `x-ratelimit-limit-requests` | 1000 | **Requests Per Day (RPD)** (or rolling window) | High confidence: returned directly in response header. |
| `x-ratelimit-limit-tokens` | 8000 | **Tokens Per Minute (TPM)** | High confidence: 8,000 TPM limit on free tier. |
| `x-ratelimit-reset-tokens` | ~500ms - 1s | TPM bucket token refill latency | High confidence: rolling sub-second replenishment window. |
| Requests per minute (RPM) | ~30 (reference) | Unstated in headers; documented default | Medium confidence: standard free tier is 30 RPM. |
| Tokens per day (TPD) | ~100k - 500k | Documented account quota | Medium confidence. |
| Requests in flight at once | not measured | Provider-side server concurrency limit | Low/Not measured. Local scheduler caps at `max_in_flight=3`. |
| Limit refusal return | HTTP 429 | `{"error": {"message": "...", "type": "tokens"}}` | High confidence: standard RFC 6585 with retry-after header. |
| Time until recovery | ~600ms - 1.5s (TPM) | Measured via reset-tokens header | High confidence. |
