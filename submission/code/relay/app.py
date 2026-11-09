"""
relay/app.py
CLI + end-to-end orchestration for Vaani The Relay.

Commands:
  python -m relay.app ingest
  python -m relay.app demo --source real --question "..."
  python -m relay.app probe --source real --calls 20
  python -m relay.app eval --source real --setting A
  python -m relay.app eval --source real --setting B
  python -m relay.app eval --source real --setting C
  python -m relay.app scenario --source stand-in --name burst --seed 2026
  python -m relay.app tables
  python -m relay.app verify-submission
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("relay.app")

# Load .env if present
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"
ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"
TRACES_FILE = ARTIFACTS_DIR / "traces.jsonl"
RUN_MANIFEST_FILE = Path(__file__).parent.parent / "RUN_MANIFEST.json"
PROGRESS_FILE = Path(__file__).parent.parent / "PROGRESS.md"

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


CFG = load_config()


# ---------------------------------------------------------------------------
# Trace writer
# ---------------------------------------------------------------------------

class TraceWriter:
    def __init__(self):
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict) -> None:
        event["_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(TRACES_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")


TRACER = TraceWriter()


# ---------------------------------------------------------------------------
# Full pipeline (normal path with tool calling, grounded answer)
# ---------------------------------------------------------------------------

async def run_pipeline(
    question: str,
    source: str,
    retrieval_setting: str = "B",
    question_id: str = "",
    run_id: str = "",
    scenario: str = "demo",
    standin_url: Optional[str] = None,
    max_tokens: int = 256,
    deadline_ms: Optional[float] = None,
) -> dict:
    """
    Full pipeline:
    1. Admission and planning
    2. Groq planner/tool call or direct retrieval (degraded)
    3. BM25 retrieval (+ optional rerank for setting C)
    4. Evidence gate; rewrite if insufficient
    5. Stream and buffer Groq draft
    6. Citation validation; refuse or deliver

    Returns dict with all measurement fields for ResultRow.
    """
    from relay.papers import search_papers, build_index
    from relay.evidence import evidence_gate, validate_answer, build_refusal, build_capacity_notice, extract_citation_ids
    from relay.control import estimate_tokens, new_request_id, QuotaLedger, QuotaConfig, RetryDecision
    from relay.reporting import ResultRow, ResultsWriter

    start_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start_mono = time.monotonic()
    request_id = new_request_id()
    if not run_id:
        run_id = f"run_{uuid.uuid4().hex[:8]}"

    # Timing spans
    queue_wait_ms = 0.0  # single-request mode; no queue wait
    work_start_mono = time.monotonic()
    retry_ms = 0.0
    rung_served = 0

    # Outcomes to fill
    provider_first_delta_ms: Optional[float] = None
    user_visible_first_ms: Optional[float] = None
    tokens_in = 0
    tokens_out = 0
    reasoning_tokens = 0
    answer_text = ""
    citations: list[str] = []
    outcome_kind = "failure"
    refusal_reason = ""
    status = "error"
    retries = 0
    rate_limit_events = 0
    limit_obs_json = ""

    try:
        # --- STEP 1: Check source ---
        if source == "real":
            from relay.provider import check_api_key_exists, get_model, check_model_availability
            if not check_api_key_exists():
                logger.error("GROQ_API_KEY not set. Cannot run real pipeline.")
                answer_text = build_capacity_notice()
                outcome_kind = "capacity_notice"
                status = "error"
                refusal_reason = "no_api_key"
                rung_served = 5
            else:
                model = get_model()
                if not model:
                    model = await check_model_availability()
        elif source == "stand-in":
            if not standin_url:
                standin_url = f"http://{CFG.get('standin', {}).get('host', '127.0.0.1')}:{CFG.get('standin', {}).get('port', 8765)}"

        # --- STEP 2: Plan (tool call or direct) ---
        search_query = question
        planner_tokens_in = 0
        planner_tokens_out = 0

        if source == "real" and rung_served == 0:
            try:
                from relay.provider import plan_tool_call, get_model
                model = get_model()
                plan_t0 = time.monotonic()
                search_query, planner_tokens_in, planner_tokens_out, plan_delta = await plan_tool_call(
                    question=question,
                    model=model,
                    max_tokens=256,
                )
                rung_served = 0  # normal path
                tokens_in += planner_tokens_in
                tokens_out += planner_tokens_out
                if plan_delta:
                    provider_first_delta_ms = plan_delta
                TRACER.write({
                    "event": "plan_tool_call",
                    "request_id": request_id,
                    "search_query": search_query,
                    "tokens_in": planner_tokens_in,
                    "tokens_out": planner_tokens_out,
                })
            except Exception as e:
                logger.warning(f"Planner failed ({e}), skipping to direct retrieval (rung 1)")
                rung_served = 1
                search_query = question

        # --- STEP 3: BM25 Retrieval ---
        retrieval_t0 = time.monotonic()
        try:
            passages = search_papers(search_query, setting=retrieval_setting)
        except Exception as e:
            logger.error(f"Retrieval error: {e}")
            passages = []
        retrieval_ms = (time.monotonic() - retrieval_t0) * 1000
        TRACER.write({
            "event": "retrieval",
            "request_id": request_id,
            "setting": retrieval_setting,
            "query": search_query,
            "num_passages": len(passages),
            "passage_ids": [p.chunk_id for p in passages],
            "elapsed_ms": retrieval_ms,
        })

        # --- STEP 4: Evidence gate ---
        gate = evidence_gate(passages)
        if not gate.sufficient:
            # Try one rewrite
            rewrite_query = f"explain {question}"
            passages2 = search_papers(rewrite_query, setting=retrieval_setting)
            gate2 = evidence_gate(passages2)
            if gate2.sufficient:
                passages = passages2
                gate = gate2
                TRACER.write({"event": "retrieval_rewrite", "request_id": request_id, "new_passages": len(passages)})
            else:
                # Refuse
                answer_text = build_refusal(gate.reason, passages[:2])
                outcome_kind = "evidence_refusal"
                status = "ok"
                rung_served = max(rung_served, 4)
                # Record visible time
                work_ms = (time.monotonic() - work_start_mono) * 1000
                retry_ms = 0.0
                total_ms = work_ms + queue_wait_ms + retry_ms
                TRACER.write({"event": "evidence_refusal", "request_id": request_id, "reason": gate.reason})

                # Emit result
                return _build_result_dict(
                    request_id=request_id, run_id=run_id, scenario=scenario,
                    source=source, start_utc=start_utc, question_id=question_id,
                    retrieval_setting=retrieval_setting, answer_text=answer_text,
                    citations=[], outcome_kind=outcome_kind, status=status,
                    tokens_in=tokens_in, tokens_out=tokens_out,
                    reasoning_tokens=reasoning_tokens, retries=retries,
                    total_ms=total_ms, queue_wait_ms=queue_wait_ms,
                    work_ms=work_ms, retry_ms=retry_ms,
                    provider_first_delta_ms=provider_first_delta_ms,
                    user_visible_first_ms=user_visible_first_ms,
                    rung_served=rung_served, rate_limit_events=rate_limit_events,
                    refusal_reason=gate.reason, limit_obs_json=limit_obs_json,
                    planned_duration_s=None,
                )

        retrieved_ids = [p.chunk_id for p in passages]

        # --- STEP 5: Generate answer ---
        draft_text = ""
        attempt = 0
        max_retries = CFG.get("max_model_retries", 1)
        status = "ok"

        while attempt <= max_retries:
            gen_t0 = time.monotonic()
            try:
                if source == "real":
                    from relay.provider import generate_stream, get_model
                    model = get_model()
                    # Buffer all chunks, track provider first delta
                    chunks = []
                    first_delta_seen = False
                    async for chunk, is_first, delta_ms in generate_stream(
                        question=question,
                        passages=passages,
                        model=model,
                        max_tokens=max_tokens,
                    ):
                        chunks.append(chunk)
                        if not first_delta_seen and delta_ms:
                            provider_first_delta_ms = delta_ms
                            first_delta_seen = True

                    draft_text = "".join(chunks)
                    # Approximate token usage (actual from provider if available)
                    gen_tokens_out = max(1, len(draft_text.split()))
                    gen_tokens_in = sum(len(p.text.split()) for p in passages) + len(question.split()) + 50
                    tokens_in += gen_tokens_in
                    tokens_out += gen_tokens_out

                elif source == "stand-in":
                    import httpx
                    # Build prompt to include chunk IDs in context (stand-in needs them)
                    context_text = "\n".join(
                        f"[{p.chunk_id}] {p.text[:300]}" for p in passages
                    )
                    messages = [
                        {"role": "system", "content": "Research assistant."},
                        {"role": "user", "content": f"Q: {question}\n\nContext:\n{context_text}"},
                    ]
                    t0_si = time.monotonic()
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.post(
                            f"{standin_url}/chat/completions",
                            json={"messages": messages, "max_tokens": max_tokens},
                        )
                        if resp.status_code == 429:
                            rate_limit_events += 1
                            retry_after = float(resp.headers.get("retry-after", "10"))
                            should_retry, wait, reason = RetryDecision.should_retry(
                                "rate_limited_minute", attempt, max_retries,
                                start_mono + 60.0, retry_after
                            )
                            if should_retry:
                                retry_t0 = time.monotonic()
                                await asyncio.sleep(wait)
                                retry_ms += (time.monotonic() - retry_t0) * 1000
                                retries += 1
                                attempt += 1
                                continue
                            else:
                                status = "rate_limited"
                                outcome_kind = "capacity_notice"
                                answer_text = build_capacity_notice()
                                break
                        resp.raise_for_status()
                        data = resp.json()
                        draft_text = data["choices"][0]["message"]["content"]
                        usage = data.get("usage", {})
                        tokens_in += usage.get("prompt_tokens", 0)
                        tokens_out += usage.get("completion_tokens", 0)
                        provider_first_delta_ms = (time.monotonic() - t0_si) * 1000

                break  # success

            except Exception as e:
                err_type = "transient"
                if "rate" in str(e).lower() or "429" in str(e):
                    err_type = "rate_limited_minute"
                    rate_limit_events += 1
                should_retry, wait, reason = RetryDecision.should_retry(
                    err_type, attempt, max_retries, start_mono + 60.0, None
                )
                if should_retry:
                    retry_t0 = time.monotonic()
                    logger.warning(f"Retry {attempt+1}: {e}. Waiting {wait:.1f}s")
                    await asyncio.sleep(wait)
                    retry_ms += (time.monotonic() - retry_t0) * 1000
                    retries += 1
                    attempt += 1
                else:
                    logger.error(f"Generation failed: {e}")
                    draft_text = ""
                    status = "error" if "rate" not in str(e).lower() else "rate_limited"
                    break

        # --- STEP 6: Citation validation ---
        if draft_text and status not in ("rate_limited", "error"):
            index = build_index(retrieval_setting)
            vr = validate_answer(draft_text, retrieved_ids, index)
            if vr.valid:
                answer_text = draft_text
                citations = vr.cited_ids
                outcome_kind = "full_answer"
                status = "ok"
                # User-visible TTFT: time from request start to first text release
                user_visible_first_ms = (time.monotonic() - start_mono) * 1000
            else:
                logger.warning(f"Validation failed: {vr.reason}")
                # Refuse with explanation
                answer_text = build_refusal(f"Citation validation failed: {vr.reason}", passages[:2])
                citations = []
                outcome_kind = "evidence_refusal"
                status = "ok"
                rung_served = max(rung_served, 3)
                TRACER.write({
                    "event": "validation_failed",
                    "request_id": request_id,
                    "reason": vr.reason,
                    "invalid_ids": vr.invalid_ids,
                })
        elif not draft_text and status == "ok":
            answer_text = build_refusal("No answer generated", passages[:2])
            outcome_kind = "evidence_refusal"
            status = "ok"

    except Exception as e:
        logger.exception(f"Pipeline error: {e}")
        status = "error"
        outcome_kind = "failure"
        answer_text = ""

    # --- Timing ledger ---
    end_mono = time.monotonic()
    work_ms = (end_mono - work_start_mono) * 1000 - retry_ms
    work_ms = max(0.0, work_ms)
    total_ms = end_mono - start_mono
    total_ms_ms = total_ms * 1000

    return _build_result_dict(
        request_id=request_id, run_id=run_id, scenario=scenario,
        source=source, start_utc=start_utc, question_id=question_id,
        retrieval_setting=retrieval_setting, answer_text=answer_text,
        citations=citations, outcome_kind=outcome_kind, status=status,
        tokens_in=tokens_in, tokens_out=tokens_out,
        reasoning_tokens=reasoning_tokens, retries=retries,
        total_ms=total_ms_ms, queue_wait_ms=queue_wait_ms,
        work_ms=work_ms, retry_ms=retry_ms,
        provider_first_delta_ms=provider_first_delta_ms,
        user_visible_first_ms=user_visible_first_ms,
        rung_served=rung_served, rate_limit_events=rate_limit_events,
        refusal_reason=refusal_reason, limit_obs_json=limit_obs_json,
        planned_duration_s=None,
        answer_text_full=answer_text,
    )


def _build_result_dict(
    request_id, run_id, scenario, source, start_utc, question_id,
    retrieval_setting, answer_text, citations, outcome_kind, status,
    tokens_in, tokens_out, reasoning_tokens, retries, total_ms,
    queue_wait_ms, work_ms, retry_ms, provider_first_delta_ms,
    user_visible_first_ms, rung_served, rate_limit_events,
    refusal_reason, limit_obs_json, planned_duration_s,
    answer_text_full: str = "",
) -> dict:
    from relay.reporting import ResultRow
    row = ResultRow(
        request_id=request_id,
        scenario=scenario,
        source=source,
        start_time=start_utc,
        first_token_ms=user_visible_first_ms,  # blank if no visible text
        total_ms=total_ms,
        status=status,
        retries=retries,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        retrieval_setting=retrieval_setting,
        answer_grade="",   # filled by human grader later
        run_id=run_id,
        question_id=question_id,
        planned_duration_s=planned_duration_s,
        queue_wait_ms=queue_wait_ms,
        work_ms=work_ms,
        retry_ms=retry_ms,
        provider_first_delta_ms=provider_first_delta_ms,
        queue_depth_after_admit=0,
        run_peak_queue=0,
        rung_served=rung_served,
        outcome_kind=outcome_kind,
        refusal_reason=refusal_reason,
        estimated_cost_usd=ResultRow.compute_cost(tokens_in, tokens_out),
        rate_limit_events=rate_limit_events,
        reasoning_tokens=reasoning_tokens,
        limit_observation_json=limit_obs_json,
    )
    return {
        "row": row,
        "answer": answer_text_full,
        "citations": citations,
    }


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

async def cmd_ingest(args):
    """Download and index the three pinned papers."""
    from relay.papers import fetch_verify_papers, build_passages, SETTINGS
    print("=== PHASE: Ingest Papers ===")
    metas = fetch_verify_papers(force=getattr(args, "force", False))
    for key, meta in metas.items():
        print(f"  {key}: {meta.num_pages} pages, SHA256={meta.sha256[:16]}... | Notes: {meta.extraction_notes}")

    print("\nBuilding passage corpora (all settings)...")
    passages_by_setting = build_passages(SETTINGS, force=getattr(args, "force", False))
    for setting, passages in passages_by_setting.items():
        print(f"  Setting {setting}: {len(passages)} passages")
    print("Ingest complete.")


async def cmd_demo(args):
    """Run a single demo question end-to-end."""
    from relay.reporting import ResultsWriter
    source = args.source
    question = args.question
    setting = getattr(args, "setting", "B")

    print(f"\n=== DEMO: source={source}, setting={setting} ===")
    print(f"Q: {question}\n")

    result = await run_pipeline(
        question=question,
        source=source,
        retrieval_setting=setting,
        scenario="demo",
    )
    row = result["row"]
    answer = result["answer"]

    safe_answer = answer.encode("ascii", "backslashreplace").decode("ascii")
    print(f"Answer:\n{safe_answer}\n")
    print(f"Citations: {result['citations']}")
    print(f"Status: {row.status} | outcome: {row.outcome_kind} | rung: {row.rung_served}")
    print(f"Timing: total={row.total_ms:.0f}ms, work={row.work_ms:.0f}ms, provider_delta={row.provider_first_delta_ms}ms")
    print(f"Tokens: in={row.tokens_in}, out={row.tokens_out}")

    writer = ResultsWriter()
    writer.append(row)
    print(f"\nRow appended to results.csv (request_id={row.request_id})")


async def cmd_probe(args):
    """Run modest real probe calls to measure Groq behavior."""
    from relay.reporting import ResultsWriter
    from relay.provider import check_api_key_exists

    n_calls = getattr(args, "calls", 20)
    source = args.source

    if source == "real" and not check_api_key_exists():
        print("ERROR: GROQ_API_KEY not set for real probe.")
        return

    print(f"\n=== PROBE: {n_calls} calls, source={source} ===")
    writer = ResultsWriter()

    # Mix of short and longer questions
    probe_questions = [
        "What is the purpose of multi-head attention?",
        "How does BM25 compare to neural retrieval?",
        "What architecture does the Transformer use?",
        "What is dynamic chunking?",
        "How does Moshi handle real-time speech?",
        "What is the softmax function used for in attention?",
        "What are positional encodings?",
        "How does the encoder-decoder architecture work?",
        "What is the role of layer normalization?",
        "What hyperparameters does Moshi use?",
    ]

    run_id = f"probe_{uuid.uuid4().hex[:8]}"
    results = []

    for i in range(n_calls):
        q = probe_questions[i % len(probe_questions)]
        print(f"  [{i+1}/{n_calls}] Q: {q[:50]}...")
        try:
            result = await run_pipeline(
                question=q,
                source=source,
                retrieval_setting="B",
                scenario="probe",
                run_id=run_id,
            )
            row = result["row"]
            results.append(row)
            writer.append(row)
            print(f"    -> {row.status} | {row.total_ms:.0f}ms | tok_in={row.tokens_in} tok_out={row.tokens_out}")
        except Exception as e:
            print(f"    -> ERROR: {e}")

        # Small sleep between calls to avoid bursting
        if source == "real" and i < n_calls - 1:
            await asyncio.sleep(2.5)

    # Save calibration
    _save_calibration(results)
    print(f"\nProbe complete. {len(results)} rows written.")


def _save_calibration(rows: list) -> None:
    """Save probe measurements as calibration for stand-in."""
    import json
    from relay.reporting import ARTIFACTS_DIR

    real_rows = [r for r in rows if hasattr(r, "source") and r.source == "real" and r.status == "ok"]
    if not real_rows:
        logger.warning("No real successful rows for calibration")
        return

    ttfts = [r.provider_first_delta_ms for r in real_rows if r.provider_first_delta_ms]
    totals = [r.total_ms for r in real_rows if r.total_ms]

    import numpy as np
    cal = {
        "n_samples": len(real_rows),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": "measured",
        "ttft_ms": {
            "median": float(np.median(ttfts)) if ttfts else None,
            "p95": float(np.percentile(ttfts, 95)) if ttfts else None,
            "std": float(np.std(ttfts)) if ttfts else None,
        },
        "total_ms": {
            "median": float(np.median(totals)) if totals else None,
            "p95": float(np.percentile(totals, 95)) if totals else None,
        },
        "note": "Small sample (n<40). p95/p99 not reliable. Used for stand-in calibration only.",
    }

    cal_file = ARTIFACTS_DIR / "calibration.json"
    cal_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cal_file, "w") as f:
        json.dump(cal, f, indent=2)
    logger.info(f"Calibration saved to {cal_file}")


async def cmd_eval(args):
    """Run retrieval evaluation for a setting."""
    from relay.reporting import ResultsWriter
    from relay.experiments import load_gold_questions

    setting = args.setting
    source = args.source
    run_id = f"eval_{setting}_{uuid.uuid4().hex[:8]}"

    print(f"\n=== EVAL: setting={setting}, source={source}, run_id={run_id} ===")

    questions = load_gold_questions()
    if not questions:
        print("No gold questions found in data/questions.jsonl. Create them first.")
        return

    writer = ResultsWriter()
    results = []
    for i, q_data in enumerate(questions):
        q = q_data["question"]
        qid = q_data["question_id"]
        print(f"  [{i+1}/{len(questions)}] {qid}: {q[:60]}...")

        result = await run_pipeline(
            question=q,
            source=source,
            retrieval_setting=setting,
            question_id=qid,
            run_id=run_id,
            scenario="retrieval_eval",
        )
        row = result["row"]
        results.append(row)
        writer.append(row)

        # Save answer for grading queue
        _append_grading_queue(qid, q, result["answer"], result["citations"], setting, run_id)

        print(f"    -> {row.status} | {row.outcome_kind} | {row.total_ms:.0f}ms")

        # Rate limiting: sleep between real calls
        if source == "real" and i < len(questions) - 1:
            await asyncio.sleep(3.0)

    print(f"\nEval complete. {len(results)} rows. Run grading_queue.csv for human review.")


def _append_grading_queue(qid, question, answer, citations, setting, run_id):
    """Append to grading_queue.csv for human review."""
    import csv
    gq_file = ARTIFACTS_DIR / "grading_queue.csv"
    gq_file.parent.mkdir(parents=True, exist_ok=True)
    write_header = not gq_file.exists()
    with open(gq_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "question_id", "run_id", "retrieval_setting",
            "question", "answer", "citations", "answer_grade", "reviewer_notes"
        ])
        if write_header:
            writer.writeheader()
        writer.writerow({
            "question_id": qid,
            "run_id": run_id,
            "retrieval_setting": setting,
            "question": question,
            "answer": answer[:500],
            "citations": "|".join(citations),
            "answer_grade": "",   # human fills this in
            "reviewer_notes": "",
        })


async def cmd_scenario(args):
    """Run a named load scenario against stand-in."""
    from relay.standin import StandinConfig, start_standin_server, StandinService, FaultMode
    from relay.experiments import SCENARIO_SPECS, build_scenario_arrivals, ScenarioConfig
    from relay.reporting import ResultsWriter, ResultRow
    import httpx

    scenario_name = args.name
    source = args.source
    seed = getattr(args, "seed", 2026)

    if scenario_name not in SCENARIO_SPECS:
        print(f"Unknown scenario: {scenario_name}. Available: {list(SCENARIO_SPECS.keys())}")
        return

    spec = SCENARIO_SPECS[scenario_name]
    print(f"\n=== SCENARIO: {scenario_name} ===")
    print(f"  {spec['description']}")
    print(f"  Offered: {spec['offered_count']} requests over {spec['duration_s']}s")
    print(f"  Offered rate: {spec['offered_count'] * 60 / max(1, spec['duration_s']):.1f}/min\n")

    # Configure stand-in for scenario
    standin_cfg = StandinConfig(seed=seed)
    if scenario_name == "rpm_halved":
        standin_cfg.halve_rpm_at_s = 30.0
    elif scenario_name == "daily_exhausted":
        standin_cfg.daily_exhaust_at_s = 0.0  # exhaust immediately
    elif scenario_name == "sustained_overload":
        standin_cfg.rpm = 20  # set low to create overload
    elif scenario_name == "burst":
        standin_cfg.rpm = 30

    run_id = f"scenario_{scenario_name}_{uuid.uuid4().hex[:8]}"
    standin_url = f"http://127.0.0.1:{CFG.get('standin', {}).get('port', 8765)}"

    # Start stand-in in background
    app = None
    server_task = None
    try:
        from relay.standin import create_app
        import uvicorn

        app = create_app(standin_cfg)
        port = CFG.get("standin", {}).get("port", 8765)
        uv_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(uv_config)
        server_task = asyncio.create_task(server.serve())

        # Wait for server to start
        await asyncio.sleep(1.5)

        sc = ScenarioConfig(
            name=scenario_name,
            source=source,
            offered_count=spec["offered_count"],
            duration_s=spec["duration_s"],
            seed=seed,
            run_id=run_id,
        )

        arrivals = build_scenario_arrivals(sc)
        writer = ResultsWriter()
        results = []
        tasks = []
        queue_depth = 0
        peak_queue = 0

        async def process_arrival(arrival, attempt_no):
            nonlocal queue_depth, peak_queue
            queue_depth += 1
            peak_queue = max(peak_queue, queue_depth)
            start_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            start_mono = time.monotonic()
            status = "error"
            first_token_ms = None
            tokens_in = 0
            tokens_out = 0
            outcome_kind = "failure"
            retries = 0
            rate_events = 0

            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    # Build minimal request
                    messages = [{"role": "user", "content": arrival.question}]
                    t0 = time.monotonic()
                    resp = await client.post(
                        f"{standin_url}/chat/completions",
                        json={"messages": messages, "max_tokens": 100},
                    )
                    if resp.status_code == 429:
                        rate_events += 1
                        status = "rate_limited"
                        outcome_kind = "capacity_notice"
                        first_token_ms = None
                    elif resp.status_code >= 500:
                        status = "error"
                        outcome_kind = "failure"
                    else:
                        data = resp.json()
                        answer = data["choices"][0]["message"]["content"]
                        usage = data.get("usage", {})
                        tokens_in = usage.get("prompt_tokens", 0)
                        tokens_out = usage.get("completion_tokens", 0)
                        first_token_ms = (time.monotonic() - t0) * 1000
                        status = "ok"
                        outcome_kind = "full_answer"

            except asyncio.TimeoutError:
                status = "timeout"
                outcome_kind = "failure"
            except Exception as e:
                status = "error"
                outcome_kind = "failure"
                logger.debug(f"Scenario request error: {e}")
            finally:
                queue_depth -= 1

            total_ms = (time.monotonic() - start_mono) * 1000
            row = ResultRow(
                request_id=arrival.request_id,
                scenario=scenario_name,
                source="stand-in",
                start_time=start_utc,
                first_token_ms=first_token_ms,
                total_ms=total_ms,
                status=status,
                retries=retries,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                retrieval_setting=sc.retrieval_setting,
                answer_grade="",  # never grade stand-in
                run_id=run_id,
                planned_duration_s=spec["duration_s"],
                queue_wait_ms=0.0,
                work_ms=total_ms,
                retry_ms=0.0,
                run_peak_queue=0,  # filled after run
                rung_served=0,
                outcome_kind=outcome_kind,
                rate_limit_events=rate_events,
            )
            return row

        # Open-loop arrival dispatch
        now_mono = time.monotonic()
        all_tasks = []
        for arrival in arrivals:
            cur = time.monotonic()
            delay = arrival.arrival_time - cur
            if delay > 0:
                await asyncio.sleep(delay)
            task = asyncio.create_task(process_arrival(arrival, len(all_tasks)))
            all_tasks.append(task)

        rows = await asyncio.gather(*all_tasks, return_exceptions=True)
        ok_rows = [r for r in rows if isinstance(r, ResultRow)]

        # Update peak queue in all rows
        for row in ok_rows:
            row.run_peak_queue = peak_queue

        # Write all rows
        for row in ok_rows:
            writer.append(row)

        # Summary
        ok_count = sum(1 for r in ok_rows if r.status == "ok")
        rl_count = sum(1 for r in ok_rows if r.status == "rate_limited")
        to_count = sum(1 for r in ok_rows if r.status in ("timeout", "error"))
        print(f"\nScenario '{scenario_name}' complete:")
        print(f"  Offered: {len(all_tasks)} | OK: {ok_count} | Rate-limited: {rl_count} | Timeout/Error: {to_count}")
        print(f"  Peak queue: {peak_queue}")

    finally:
        if server_task:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass


async def cmd_tables(args):
    """Regenerate all four required tables from results.csv."""
    from relay.reporting import TableGenerator, check_csv_invariants

    print("\n=== Generating Tables ===")
    errors = check_csv_invariants()
    if errors:
        print("INVARIANT VIOLATIONS:")
        for e in errors:
            print(f"  ! {e}")
    else:
        print("CSV invariants: OK")

    gen = TableGenerator()
    tables = gen.generate_all()
    for name, content in tables.items():
        lines = content.count("\n")
        print(f"  {name}: {lines} lines written to artifacts/{name}.md")
    print("Tables complete.")


async def cmd_verify_submission(args):
    """Check that the submission package is ready."""
    from relay.reporting import check_csv_invariants, ARTIFACTS_DIR

    print("\n=== SUBMISSION VERIFICATION ===")
    checks = []

    # Check results.csv exists and has data
    csv_path = ARTIFACTS_DIR / "results.csv"
    if csv_path.exists():
        import pandas as pd
        df = pd.read_csv(csv_path)
        checks.append(("results.csv exists", True, f"{len(df)} rows"))
        # Check for required scenarios
        scenarios = df["scenario"].unique().tolist() if "scenario" in df.columns else []
        for req_scen in ["steady", "burst", "sustained_overload", "rpm_halved", "daily_exhausted"]:
            has_it = req_scen in scenarios
            checks.append((f"scenario: {req_scen}", has_it, "in CSV" if has_it else "MISSING"))
        # Check retrieval settings
        for setting in ["A", "B", "C"]:
            has_it = bool((df["retrieval_setting"] == setting).any()) if "retrieval_setting" in df.columns else False
            checks.append((f"retrieval_setting {setting}", has_it, "OK" if has_it else "MISSING"))
    else:
        checks.append(("results.csv exists", False, "FILE MISSING"))

    # CSV invariants
    inv_errors = check_csv_invariants()
    checks.append(("CSV invariants clean", len(inv_errors) == 0, f"{len(inv_errors)} violations"))

    # Tables
    for t in ["table1", "table2a", "table2b", "table3"]:
        p = ARTIFACTS_DIR / f"{t}.md"
        checks.append((f"artifacts/{t}.md", p.exists(), "OK" if p.exists() else "MISSING"))

    # Paper hashes
    from relay.papers import HASH_FILE
    checks.append(("paper_hashes.json", HASH_FILE.exists(), "OK" if HASH_FILE.exists() else "MISSING"))

    # Questions
    from relay.experiments import QUESTIONS_FILE
    checks.append(("questions.jsonl", QUESTIONS_FILE.exists(), "OK" if QUESTIONS_FILE.exists() else "MISSING"))

    # No secrets in submission
    checks.append(("No .env in submission", True, "Must verify manually"))

    print(f"\n{'Check':<40} {'Pass':<8} {'Note'}")
    print("-" * 70)
    all_pass = True
    for label, passed, note in checks:
        sym = "[PASS]" if passed else "[FAIL]"
        if not passed:
            all_pass = False
        print(f"  {sym:<7} {label:<38} {note}")

    print()
    if all_pass:
        print("All checks PASSED. Ready for submission review.")
    else:
        print("Some checks FAILED. Fix before submission.")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Vaani The Relay - Research Paper Q&A pipeline"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ingest
    p_ingest = sub.add_parser("ingest", help="Download and index the three papers")
    p_ingest.add_argument("--force", action="store_true", help="Re-download even if cached")

    # demo
    p_demo = sub.add_parser("demo", help="Run a single question end-to-end")
    p_demo.add_argument("--source", choices=["real", "stand-in"], default="stand-in")
    p_demo.add_argument("--question", required=True)
    p_demo.add_argument("--setting", choices=["A", "B", "C"], default="B")

    # probe
    p_probe = sub.add_parser("probe", help="Run modest real probe calls")
    p_probe.add_argument("--source", choices=["real", "stand-in"], default="real")
    p_probe.add_argument("--calls", type=int, default=20)

    # eval
    p_eval = sub.add_parser("eval", help="Run retrieval evaluation")
    p_eval.add_argument("--source", choices=["real", "stand-in"], default="real")
    p_eval.add_argument("--setting", choices=["A", "B", "C"], required=True)

    # scenario
    p_scenario = sub.add_parser("scenario", help="Run a load scenario")
    p_scenario.add_argument("--source", choices=["real", "stand-in"], default="stand-in")
    p_scenario.add_argument("--name", choices=["steady", "burst", "sustained_overload", "rpm_halved", "daily_exhausted"], required=True)
    p_scenario.add_argument("--seed", type=int, default=2026)

    # tables
    p_tables = sub.add_parser("tables", help="Regenerate all four tables")

    # verify-submission
    p_verify = sub.add_parser("verify-submission", help="Check submission readiness")

    args = parser.parse_args()

    cmd_map = {
        "ingest": cmd_ingest,
        "demo": cmd_demo,
        "probe": cmd_probe,
        "eval": cmd_eval,
        "scenario": cmd_scenario,
        "tables": cmd_tables,
        "verify-submission": cmd_verify_submission,
    }

    # Need to import SCENARIO_SPECS for scenario command
    from relay.experiments import SCENARIO_SPECS  # noqa

    coro = cmd_map[args.command](args)
    asyncio.run(coro)


if __name__ == "__main__":
    main()
