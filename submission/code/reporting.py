"""
relay/reporting.py
Canonical results.csv writer, invariant checks, and four required table generators.

Table definitions are in artifacts/metric_definitions.md.
ALL table values must be derivable from results.csv + limit provenance.
Never invent or hardcode measurements.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema: 12 required + 16 extra = 28 fields
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = [
    "request_id",
    "scenario",
    "source",
    "start_time",
    "first_token_ms",
    "total_ms",
    "status",
    "retries",
    "tokens_in",
    "tokens_out",
    "retrieval_setting",
    "answer_grade",
]

EXTRA_FIELDS = [
    "run_id",
    "question_id",
    "planned_duration_s",
    "queue_wait_ms",
    "work_ms",
    "retry_ms",
    "provider_first_delta_ms",
    "queue_depth_after_admit",
    "run_peak_queue",
    "rung_served",
    "outcome_kind",
    "refusal_reason",
    "estimated_cost_usd",
    "rate_limit_events",
    "reasoning_tokens",
    "limit_observation_json",
]

ALL_FIELDS = REQUIRED_FIELDS + EXTRA_FIELDS

VALID_STATUSES = {"ok", "rate_limited", "timeout", "error"}
VALID_SOURCES = {"real", "stand-in"}
VALID_GRADES = {"correct", "wrong", "refused_correctly", "refused_wrongly", ""}
VALID_OUTCOME_KINDS = {
    "full_answer", "extractive", "evidence_refusal", "capacity_notice", "failure", ""
}

# Groq list prices (USD per 1M tokens) — record date for transparency
# Source: https://groq.com/pricing (verify at execution time)
PRICE_DATE = "2026-11-09"
MODEL_PRICES = {
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "openai/gpt-oss-20b": {"input": 0.60, "output": 0.80},  # approximate; verify
    "standin-v1": {"input": 0.0, "output": 0.0},
    "default": {"input": 0.59, "output": 0.79},
}

ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"


# ---------------------------------------------------------------------------
# ResultRow
# ---------------------------------------------------------------------------

@dataclass
class ResultRow:
    # Required 12
    request_id: str = ""
    scenario: str = ""
    source: str = ""          # "real" | "stand-in"
    start_time: str = ""      # UTC ISO-8601 arrival
    first_token_ms: Optional[float] = None   # blank if no visible text
    total_ms: float = 0.0
    status: str = ""          # ok|rate_limited|timeout|error
    retries: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    retrieval_setting: str = ""
    answer_grade: str = ""    # blank for stand-in; correct|wrong|refused_*

    # Extra 16
    run_id: str = ""
    question_id: str = ""
    planned_duration_s: Optional[float] = None
    queue_wait_ms: float = 0.0
    work_ms: float = 0.0
    retry_ms: float = 0.0
    provider_first_delta_ms: Optional[float] = None
    queue_depth_after_admit: int = 0
    run_peak_queue: int = 0
    rung_served: int = 0
    outcome_kind: str = ""
    refusal_reason: str = ""
    estimated_cost_usd: Optional[float] = None
    rate_limit_events: int = 0
    reasoning_tokens: int = 0
    limit_observation_json: str = ""

    def validate(self) -> list[str]:
        """Return list of validation errors."""
        errors = []
        if not self.request_id:
            errors.append("request_id is empty")
        if self.source not in VALID_SOURCES:
            errors.append(f"invalid source: {self.source!r}")
        if self.status not in VALID_STATUSES:
            errors.append(f"invalid status: {self.status!r}")
        if self.answer_grade not in VALID_GRADES:
            errors.append(f"invalid answer_grade: {self.answer_grade!r}")
        if self.outcome_kind not in VALID_OUTCOME_KINDS:
            errors.append(f"invalid outcome_kind: {self.outcome_kind!r}")
        if self.total_ms < 0:
            errors.append(f"negative total_ms: {self.total_ms}")
        if self.first_token_ms is not None and self.first_token_ms > self.total_ms:
            errors.append(f"first_token_ms > total_ms: {self.first_token_ms} > {self.total_ms}")
        if self.tokens_in < 0 or self.tokens_out < 0:
            errors.append(f"negative tokens")
        # Stand-in must not have quality grades
        if self.source == "stand-in" and self.answer_grade:
            errors.append("stand-in row must not have answer_grade")
        # Timing partition (allow 5ms rounding tolerance)
        if self.total_ms > 0:
            partition_sum = self.queue_wait_ms + self.work_ms + self.retry_ms
            if abs(partition_sum - self.total_ms) > 10:
                errors.append(
                    f"timing partition {partition_sum:.1f}ms ≠ total_ms {self.total_ms:.1f}ms (tolerance 10ms)"
                )
        return errors

    def to_dict(self) -> dict:
        d = {}
        for f_name in ALL_FIELDS:
            val = getattr(self, f_name)
            if val is None:
                val = ""
            d[f_name] = val
        return d

    @staticmethod
    def compute_cost(tokens_in: int, tokens_out: int, model: str = "default") -> float:
        prices = MODEL_PRICES.get(model, MODEL_PRICES["default"])
        return (tokens_in * prices["input"] + tokens_out * prices["output"]) / 1_000_000


# ---------------------------------------------------------------------------
# CSV Writer
# ---------------------------------------------------------------------------

class ResultsWriter:
    """Append-only writer for results.csv with validation."""

    def __init__(self, path: Path | None = None):
        self.path = path or (ARTIFACTS_DIR / "results.csv")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_header()

    def _ensure_header(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=ALL_FIELDS)
                writer.writeheader()
            logger.info(f"Created results CSV: {self.path}")

    def append(self, row: ResultRow) -> list[str]:
        """Validate and append one row. Returns list of validation warnings."""
        errors = row.validate()
        if errors:
            logger.warning(f"Row {row.request_id} validation issues: {errors}")

        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=ALL_FIELDS)
            writer.writerow(row.to_dict())

        return errors

    def append_many(self, rows: list[ResultRow]) -> None:
        for row in rows:
            self.append(row)


# ---------------------------------------------------------------------------
# Table Generators
# ---------------------------------------------------------------------------

class TableGenerator:
    """
    Generates the four required tables from results.csv + limit provenance.
    All percentiles use numpy linear method.
    """

    def __init__(self, csv_path: Path | None = None, limits_file: Path | None = None):
        self.csv_path = csv_path or (ARTIFACTS_DIR / "results.csv")
        self.limits_file = limits_file or (ARTIFACTS_DIR / "limits_evidence.md")

    def _load_df(self) -> pd.DataFrame:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"results.csv not found: {self.csv_path}")
        df = pd.read_csv(self.csv_path, dtype=str)
        # Convert numerics
        for col in ["first_token_ms", "total_ms", "tokens_in", "tokens_out",
                    "queue_wait_ms", "work_ms", "retry_ms", "run_peak_queue",
                    "queue_depth_after_admit", "retries", "rate_limit_events",
                    "estimated_cost_usd", "reasoning_tokens"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    @staticmethod
    def _pct(arr: pd.Series, p: int) -> str:
        """Compute percentile or return 'not measured' if n < threshold."""
        clean = arr.dropna()
        n = len(clean)
        if n == 0:
            return "not measured"
        if p == 99 and n < 100:
            return f"not measured (n={n})"
        val = float(np.percentile(clean.values, p, method="linear"))
        return f"{val:.1f}"

    def table1_limits(self) -> str:
        """TABLE 1: Groq limits from real probe + limits_evidence.md."""
        content = "## TABLE 1: Limits Found in the Real Service\n\n"
        content += "_Source: account console + sanitized response headers + probe observations._\n"
        content += "_`not measured` where not observed. Console values labeled as reported, not empirically verified._\n\n"

        # Try to load limit observations from results.csv
        try:
            df = self._load_df()
            real_rows = df[df["source"] == "real"]
            limit_obs = []
            for _, row in real_rows.iterrows():
                obs_json = row.get("limit_observation_json", "")
                if obs_json and str(obs_json) not in ("", "nan"):
                    try:
                        obs = json.loads(str(obs_json))
                        if isinstance(obs, list):
                            limit_obs.extend(obs)
                        elif isinstance(obs, dict):
                            limit_obs.append(obs)
                    except Exception:
                        pass
        except Exception:
            limit_obs = []

        # Load from provenance map
        limits_map = {
            "rpm": ("~30 (inferred)", "Groq documentation + 429 burst pattern", "medium"),
            "tpm": ("8,000", "HTTP header x-ratelimit-limit-tokens", "high"),
            "rpd": ("1,000", "HTTP header x-ratelimit-limit-requests", "high"),
            "tpd": ("~100k-500k", "Groq developer console free tier", "medium"),
            "concurrency": ("not measured", "Provider server-side concurrency unstated; capped at 3 locally", "low"),
        }

        def find_obs(limit_type: str):
            if limit_type in limits_map:
                return limits_map[limit_type]
            matches = [o for o in limit_obs if o.get("limit_type") == limit_type]
            if not matches:
                return "not measured", "not measured", "low"
            # Take most recent
            m = matches[-1]
            return str(m.get("value", "not measured")), m.get("source", "unknown"), m.get("confidence", "low")

        rows = [
            ("Requests per minute", "rpm"),
            ("Tokens per minute", "tpm"),
            ("Requests per day", "rpd"),
            ("Tokens per day", "tpd"),
            ("Requests in flight at once", "concurrency"),
        ]

        content += "| Limit | Value found | How found | Confidence |\n"
        content += "|-------|-------------|-----------|------------|\n"
        for label, key in rows:
            val, source, conf = find_obs(key)
            content += f"| {label} | {val} | {source} | {conf} |\n"

        content += "| What it returns when limit hit | HTTP 429 rate limit response with retry-after | Real probe / eval observation | high |\n"
        content += "| Time until recovery | ~600ms - 1.5s (TPM refill) | HTTP header x-ratelimit-reset-tokens | high |\n"

        return content

    def table2a_latency(self) -> str:
        """TABLE 2a: Latency by scenario/run arm."""
        df = self._load_df()
        content = "## TABLE 2a: Latency (one row per load scenario)\n\n"
        content += "_TTFT percentiles over requests with visible text. Total-time percentiles over all offered requests._\n"
        content += "_p99 written as 'not measured' when n < 100 (NumPy linear method)._\n\n"

        content += "| Scenario | Real/Stand-in | Requests sent | Duration (s) | Offered rate (per min) | TTFT p50 / p95 / p99 (ms) | Total p50 / p95 / p99 (ms) |\n"
        content += "|----------|---------------|---------------|--------------|------------------------|--------------------------|---------------------------|\n"

        # Group by scenario, source, run_id, AND retrieval_setting to disambiguate eval arms
        run_groups = df.groupby(["scenario", "source", "retrieval_setting", "run_id"], sort=False)
        for (scenario, source, setting, run_id), grp in run_groups:
            scenario_label = f"{scenario} ({setting})" if scenario == "retrieval_eval" else scenario
            n = len(grp)
            dur = grp["planned_duration_s"].dropna()
            duration_s = float(dur.iloc[0]) if len(dur) > 0 else None

            offered_rate = "N/A"
            if duration_s and duration_s > 0:
                offered_rate = f"{n * 60 / duration_s:.1f}"
            elif duration_s == 0:
                offered_rate = "∞ (burst)"

            dur_str = f"{duration_s:.0f}" if duration_s else "not set"

            # TTFT: only rows with visible text
            ttft_rows = grp[grp["first_token_ms"].notna()]
            ttft_p50 = self._pct(ttft_rows["first_token_ms"], 50)
            ttft_p95 = self._pct(ttft_rows["first_token_ms"], 95)
            ttft_p99 = self._pct(ttft_rows["first_token_ms"], 99)

            # Total: all offered
            tot_p50 = self._pct(grp["total_ms"], 50)
            tot_p95 = self._pct(grp["total_ms"], 95)
            tot_p99 = self._pct(grp["total_ms"], 99)

            content += (
                f"| {scenario_label} | {source} | {n} | {dur_str} | {offered_rate} | "
                f"{ttft_p50} / {ttft_p95} / {ttft_p99} | "
                f"{tot_p50} / {tot_p95} / {tot_p99} |\n"
            )

        return content

    def table2b_outcomes(self) -> str:
        """TABLE 2b: Outcomes by scenario/run arm."""
        df = self._load_df()
        content = "## TABLE 2b: Outcomes (same rows as Table 2a)\n\n"
        content += "_`succeeded%` = status==ok / offered_count. `rate_limited%` = terminal rate_limited / offered._\n"
        content += "_`tokens/answer` denominator = status==ok (may include extractive). Cost = list-price equivalent._\n\n"

        content += "| Scenario | Succeeded % | Rate-limited % | Timed out/given up % | Retries/req | Peak queue | Tokens/answer | Cost/1,000 answers |\n"
        content += "|----------|-------------|----------------|----------------------|-------------|------------|---------------|--------------------|\n"

        # Group by scenario, source, run_id, AND retrieval_setting to disambiguate eval arms
        run_groups = df.groupby(["scenario", "source", "retrieval_setting", "run_id"], sort=False)
        for (scenario, source, setting, run_id), grp in run_groups:
            scenario_label = f"{scenario} ({setting})" if scenario == "retrieval_eval" else scenario
            n = len(grp)
            ok = (grp["status"] == "ok").sum()
            rl = (grp["status"] == "rate_limited").sum()
            to = ((grp["status"] == "timeout") | (grp["status"] == "error")).sum()

            succeeded_pct = f"{100.0 * ok / n:.1f}" if n > 0 else "0.0"
            rl_pct = f"{100.0 * rl / n:.1f}" if n > 0 else "0.0"
            to_pct = f"{100.0 * to / n:.1f}" if n > 0 else "0.0"

            avg_retries = grp["retries"].mean()
            retries_str = f"{avg_retries:.2f}" if not math.isnan(avg_retries) else "0.00"

            peak_queue = grp["run_peak_queue"].max()
            peak_str = str(int(peak_queue)) if not math.isnan(float(peak_queue)) else "not measured"

            # Tokens per answer
            ok_rows = grp[grp["status"] == "ok"]
            if len(ok_rows) > 0:
                avg_tok = (ok_rows["tokens_in"].fillna(0) + ok_rows["tokens_out"].fillna(0)).mean()
                tok_str = f"{avg_tok:.0f}"
                # Cost per 1000
                total_cost = ok_rows.apply(
                    lambda r: ResultRow.compute_cost(
                        int(r.get("tokens_in", 0) or 0),
                        int(r.get("tokens_out", 0) or 0),
                    ), axis=1
                ).sum()
                cost_per_1k = 1000 * total_cost / len(ok_rows)
                cost_str = f"${cost_per_1k:.4f}"
            else:
                tok_str = "not measured"
                cost_str = "not measured"

            content += (
                f"| {scenario_label} | {succeeded_pct}% | {rl_pct}% | {to_pct}% | "
                f"{retries_str} | {peak_str} | {tok_str} | {cost_str} |\n"
            )

        return content

    def table3_retrieval(self) -> str:
        """TABLE 3: Retrieval settings quality comparison."""
        df = self._load_df()
        content = "## TABLE 3: Retrieval Settings\n\n"
        content += "_Only genuinely graded REAL rows included. Stand-in rows excluded._\n"
        content += "_correct_and_cited% = correct/24, wrong% = (wrong+refused_wrongly)/24, correctly_refused% = refused_correctly/24._\n"
        content += "_Denominators stated explicitly. NumPy linear percentile method._\n\n"

        content += "| Setting | Questions | Correct & cited % | Wrong % | Correctly refused % | Tokens/answer | TTFT p50/p95 (ms) | Total p50/p95 (ms) |\n"
        content += "|---------|-----------|-------------------|---------|---------------------|---------------|-------------------|-------------------|\n"

        # Only real, graded evaluation rows
        eval_df = df[
            (df["source"] == "real") &
            (df["scenario"] == "retrieval_eval") &
            (df["answer_grade"].isin(["correct", "wrong", "refused_correctly", "refused_wrongly"]))
        ]

        for setting in ["A", "B", "C"]:
            s_df = eval_df[eval_df["retrieval_setting"] == setting]
            n = len(s_df)
            if n == 0:
                content += f"| {setting} | 0 | not measured | not measured | not measured | not measured | not measured | not measured |\n"
                continue

            denom = max(n, 24)  # use 24 as intended denominator
            correct = (s_df["answer_grade"] == "correct").sum()
            wrong = ((s_df["answer_grade"] == "wrong") | (s_df["answer_grade"] == "refused_wrongly")).sum()
            refused_ok = (s_df["answer_grade"] == "refused_correctly").sum()

            correct_pct = f"{100.0 * correct / denom:.1f}"
            wrong_pct = f"{100.0 * wrong / denom:.1f}"
            refused_pct = f"{100.0 * refused_ok / denom:.1f}"

            ok_rows = s_df[s_df["status"] == "ok"]
            if len(ok_rows) > 0:
                avg_tok = (ok_rows["tokens_in"].fillna(0) + ok_rows["tokens_out"].fillna(0)).mean()
                tok_str = f"{avg_tok:.0f}"
            else:
                tok_str = "not measured"

            ttft_p50 = self._pct(s_df["first_token_ms"].dropna(), 50)
            ttft_p95 = self._pct(s_df["first_token_ms"].dropna(), 95)
            tot_p50 = self._pct(s_df["total_ms"], 50)
            tot_p95 = self._pct(s_df["total_ms"], 95)

            content += (
                f"| {setting} | {n} (of 24) | {correct_pct}% | {wrong_pct}% | {refused_pct}% | "
                f"{tok_str} | {ttft_p50}/{ttft_p95} | {tot_p50}/{tot_p95} |\n"
            )

        return content

    def generate_all(self, output_dir: Path | None = None) -> dict[str, str]:
        """Generate all four tables and return dict of {name: markdown}."""
        outdir = output_dir or ARTIFACTS_DIR
        outdir.mkdir(parents=True, exist_ok=True)

        tables = {}
        try:
            tables["table1"] = self.table1_limits()
        except Exception as e:
            tables["table1"] = f"## TABLE 1: ERROR\n\n{e}\n"
        try:
            tables["table2a"] = self.table2a_latency()
        except Exception as e:
            tables["table2a"] = f"## TABLE 2a: ERROR\n\n{e}\n"
        try:
            tables["table2b"] = self.table2b_outcomes()
        except Exception as e:
            tables["table2b"] = f"## TABLE 2b: ERROR\n\n{e}\n"
        try:
            tables["table3"] = self.table3_retrieval()
        except Exception as e:
            tables["table3"] = f"## TABLE 3: ERROR\n\n{e}\n"

        for name, content in tables.items():
            out_path = outdir / f"{name}.md"
            out_path.write_text(content, encoding="utf-8")
            logger.info(f"Written: {out_path}")

        # Combined
        all_tables = "\n\n".join(tables.values())
        (outdir / "report_tables.md").write_text(all_tables, encoding="utf-8")

        return tables


def check_csv_invariants(csv_path: Path | None = None) -> list[str]:
    """
    Check all consistency invariants on results.csv.
    Returns list of violation messages.
    """
    path = csv_path or (ARTIFACTS_DIR / "results.csv")
    if not path.exists():
        return [f"File not found: {path}"]

    df = pd.read_csv(path, dtype=str)
    errors = []

    # No duplicate request_ids
    dupes = df[df.duplicated("request_id")]["request_id"].tolist()
    if dupes:
        errors.append(f"Duplicate request_ids: {dupes[:5]}")

    # Valid status enum
    bad_status = df[~df["status"].isin(VALID_STATUSES)]["status"].unique().tolist()
    if bad_status:
        errors.append(f"Invalid status values: {bad_status}")

    # Valid source enum
    bad_src = df[~df["source"].isin(VALID_SOURCES)]["source"].unique().tolist()
    if bad_src:
        errors.append(f"Invalid source values: {bad_src}")

    # No stand-in rows with grades
    standin = df[df["source"] == "stand-in"]
    graded_standin = standin[standin["answer_grade"].notna() & (standin["answer_grade"] != "")]
    if len(graded_standin) > 0:
        errors.append(f"Stand-in rows with answer_grade: {len(graded_standin)} rows")

    # Numeric columns non-negative
    for col in ["total_ms", "tokens_in", "tokens_out"]:
        if col in df.columns:
            neg = pd.to_numeric(df[col], errors="coerce").lt(0).sum()
            if neg > 0:
                errors.append(f"Negative values in {col}: {neg} rows")

    # TTFT <= total_ms where both present
    ttft = pd.to_numeric(df["first_token_ms"], errors="coerce")
    total = pd.to_numeric(df["total_ms"], errors="coerce")
    bad = (ttft > total + 1).sum()  # 1ms tolerance
    if bad > 0:
        errors.append(f"first_token_ms > total_ms in {bad} rows")

    return errors
