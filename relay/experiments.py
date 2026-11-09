"""
relay/experiments.py
Open-loop traffic generation for load scenarios and retrieval evaluation.

Scenarios: steady, burst, sustained_overload, rpm_halved, daily_exhausted
Evaluation: retrieval_eval (settings A/B/C)
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, AsyncGenerator

logger = logging.getLogger(__name__)

QUESTIONS_FILE = Path(__file__).parent.parent / "data" / "questions.jsonl"
ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"
SCENARIOS_FILE = ARTIFACTS_DIR / "scenarios.yaml"


@dataclass
class ScenarioConfig:
    name: str
    source: str            # "real" | "stand-in"
    offered_count: int     # total number of requests to offer
    duration_s: float      # arrival window in seconds (open-loop)
    seed: int = 2026
    retrieval_setting: str = "B"
    questions: list[str] = field(default_factory=list)
    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex[:8]}")

    def offered_rate_per_min(self) -> float:
        if self.duration_s <= 0:
            return float("inf")
        return self.offered_count * 60.0 / self.duration_s


@dataclass
class RequestArrival:
    arrival_time: float    # monotonic time to inject
    question: str
    request_id: str
    scenario: str
    retrieval_setting: str


def build_scenario_arrivals(config: ScenarioConfig) -> list[RequestArrival]:
    """
    Build a fixed open-loop arrival schedule for a scenario.
    Arrivals are independent of service completion.
    """
    rng = random.Random(config.seed)
    questions = config.questions or _load_default_questions()
    if not questions:
        questions = [
            "What is scaled dot-product attention?",
            "How does the transformer encoder work?",
            "What is the main contribution of this paper?",
        ]

    arrivals = []
    now = time.monotonic()

    for i in range(config.offered_count):
        if config.duration_s > 0:
            arrival_offset = rng.uniform(0, config.duration_s)
        else:
            arrival_offset = 0.0
        q = questions[i % len(questions)]
        arrivals.append(RequestArrival(
            arrival_time=now + arrival_offset,
            question=q,
            request_id=f"req_{uuid.uuid4().hex[:12]}",
            scenario=config.name,
            retrieval_setting=config.retrieval_setting,
        ))

    arrivals.sort(key=lambda a: a.arrival_time)
    return arrivals


def _load_default_questions() -> list[str]:
    """Load questions from gold set, return as list of question strings."""
    if QUESTIONS_FILE.exists():
        questions = []
        with open(QUESTIONS_FILE) as f:
            for line in f:
                try:
                    q = json.loads(line)
                    questions.append(q.get("question", ""))
                except Exception:
                    pass
        return [q for q in questions if q]
    return []


def load_gold_questions() -> list[dict]:
    """Load full gold question records."""
    if not QUESTIONS_FILE.exists():
        return []
    questions = []
    with open(QUESTIONS_FILE) as f:
        for line in f:
            try:
                questions.append(json.loads(line))
            except Exception:
                pass
    return questions


SCENARIO_SPECS = {
    "steady": {
        "offered_count": 60,
        "duration_s": 120,  # 60 requests over 2 min = 30/min (at capacity)
        "description": "Steady load below saturation baseline",
    },
    "burst": {
        "offered_count": 60,
        "duration_s": 10,   # 60 requests in 10 seconds = 360/min (burst)
        "description": "Sudden burst of requests in a short window",
    },
    "sustained_overload": {
        "offered_count": 150,
        "duration_s": 180,  # 150 over 3 min = 50/min (over capacity)
        "description": "Sustained load exceeding service capacity",
    },
    "rpm_halved": {
        "offered_count": 60,
        "duration_s": 120,
        "description": "Steady load; RPM halves at t=30s mid-run",
    },
    "daily_exhausted": {
        "offered_count": 40,
        "duration_s": 60,
        "description": "Daily budget exhausted from start; tests fallback path",
    },
}


async def run_scenario_standin(
    config: ScenarioConfig,
    standin_url: str,
    process_fn: Callable,
    results_writer,
    run_id: str,
    peak_queue_tracker: Optional[list] = None,
) -> list[dict]:
    """
    Run a stand-in scenario: open-loop arrival, process via stand-in,
    record results.
    """
    import httpx

    arrivals = build_scenario_arrivals(config)
    results = []
    pending = []
    queue_depth = 0

    async def handle_arrival(arrival: RequestArrival):
        nonlocal queue_depth
        result = await process_fn(arrival, standin_url, run_id, config)
        results.append(result)

    # Open-loop: schedule by arrival time
    start_mono = time.monotonic()
    tasks = []
    for arrival in arrivals:
        wait = arrival.arrival_time - start_mono
        if wait > 0:
            await asyncio.sleep(wait)
        # Launch independently
        task = asyncio.create_task(handle_arrival(arrival))
        tasks.append(task)

    await asyncio.gather(*tasks, return_exceptions=True)
    return results


async def run_retrieval_eval(
    setting: str,
    source: str,
    pipeline_fn: Callable,
    results_writer,
    run_id: str,
    questions: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Run retrieval evaluation for one setting.
    Calls pipeline_fn for each question, records results.
    """
    if questions is None:
        questions = load_gold_questions()

    if not questions:
        logger.warning("No gold questions found. Run question generation first.")
        return []

    results = []
    for q_data in questions:
        result = await pipeline_fn(
            question=q_data["question"],
            question_id=q_data["question_id"],
            retrieval_setting=setting,
            source=source,
            run_id=run_id,
            scenario="retrieval_eval",
        )
        results.append(result)

    return results


def save_scenarios_config() -> None:
    """Save the frozen scenario specifications to scenarios.yaml."""
    import yaml
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SCENARIOS_FILE, "w") as f:
        yaml.dump(SCENARIO_SPECS, f, default_flow_style=False)
    logger.info(f"Saved scenario specs to {SCENARIOS_FILE}")
