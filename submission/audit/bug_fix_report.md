# Bug Fix Report — Vaani "The Relay"

**Date:** 2026-11-09  
**Status:** All Identified Bugs Repaired & Verified

---

## Bug #1: Python Truthiness Bug in Stand-in Dynamic Limiter (P0)

- **File:** `relay/standin.py` (L168, L172)
- **Description:** When evaluating dynamic limit threshold timestamps, Python's truthiness check `if self.config.daily_exhaust_at_s:` treated `0.0` as `False`. When running scenario `daily_exhausted` with `daily_exhaust_at_s=0.0` (exhaust quota immediately at test start), the stand-in failed to enforce rate limits, allowing 31 of 40 requests to succeed with full answers.
- **Root Cause:**
  ```python
  # Buggy code:
  if self.config.halve_rpm_at_s and elapsed >= self.config.halve_rpm_at_s:
  if self.config.daily_exhaust_at_s and elapsed >= self.config.daily_exhaust_at_s:
  ```
- **Fix:** Explicitly checked for `None`:
  ```python
  # Fixed code:
  if self.config.halve_rpm_at_s is not None and elapsed >= self.config.halve_rpm_at_s:
  if self.config.daily_exhaust_at_s is not None and elapsed >= self.config.daily_exhaust_at_s:
  ```
- **Verification:** Re-ran `daily_exhausted` scenario against stand-in. Confirmed exactly 40/40 requests were rate-limited (HTTP 429) immediately.

---

## Bug #2: Results CSV Duplication and Stale Run Data (P0)

- **File:** `artifacts/results.csv`
- **Description:** After fixing Bug #1, results.csv contained both the initial broken run (`scenario_daily_exhausted_8036e76d`, 31 OK / 9 rate-limited) and the new rerun (`scenario_daily_exhausted_ef44d9a5`, 40 rate-limited), skewing Table 2a and Table 2b.
- **Fix:** Removed the stale 40 rows from run `scenario_daily_exhausted_8036e76d`. Re-ran `.venv\Scripts\python -m relay.app tables`.
- **Verification:** Table 2b now accurately reports `daily_exhausted: 0.0% Succeeded, 100.0% Rate-limited, Peak queue: 7`.

---

## Bug #3: Erroneous Paper Description in Writeup and Documentation (P2)

- **Files:** `docs/writeup.md`, `submission/writeup.md`
- **Description:** Paper `2507.07955v2` was referenced as "Advances in RAG systems" in the paper table and trap discussion.
- **Root Cause:** Placeholder draft text from early scaffolding.
- **Fix:** Corrected title to **"Dynamic Chunking for End-to-End Hierarchical Sequence Modeling (H-Net)"** (39 pages). Updated retrieval traps discussion to accurately reference sequence modeling vs speech tokens.

---

## Bug #4: Ambiguity in `peak_queue` and `retries` Metric Documentation (P1)

- **Files:** `docs/writeup.md`, `relay/reporting.py`
- **Description:** Table 2b showed peak queue reaching 60 and 104 in load scenarios, while `control.py` specifies `BoundedQueue.maxsize=24`. Furthermore, `retries` showed `0.00` across stand-in scenarios.
- **Clarification Added:**
  - `peak_queue` in scenario tests measures open-loop concurrent HTTP requests spawned by `process_arrival` to test system behavior under unrestrained arrival rates.
  - Stand-in scenario HTTP runner treats 429 as terminal to isolate provider load curves, whereas pipeline requests invoke the `RetryDecision` engine.
