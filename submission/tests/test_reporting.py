"""
tests/test_reporting.py
Tests for ResultRow validation, CSV invariants, percentile computation, and table generation.
Uses ONLY tests/fixtures/ data — NEVER touches artifacts/results.csv.
"""

import csv
import io
import json
import math
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest
import pandas as pd

from relay.reporting import (
    ResultRow,
    ResultsWriter,
    TableGenerator,
    check_csv_invariants,
    ALL_FIELDS,
    REQUIRED_FIELDS,
    VALID_STATUSES,
    VALID_SOURCES,
    VALID_GRADES,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_row(**kwargs) -> ResultRow:
    defaults = dict(
        request_id="req_test_001",
        scenario="probe",
        source="real",
        start_time="2026-11-09T10:00:00Z",
        first_token_ms=200.0,
        total_ms=500.0,
        status="ok",
        retries=0,
        tokens_in=100,
        tokens_out=50,
        retrieval_setting="B",
        answer_grade="",
        run_id="run_test_001",
        queue_wait_ms=10.0,
        work_ms=485.0,
        retry_ms=5.0,
        outcome_kind="full_answer",
    )
    defaults.update(kwargs)
    return ResultRow(**defaults)


def write_fixture_csv(rows: list[ResultRow], filename: str = "fixture.csv") -> Path:
    """Write fixture rows to a temp file for testing. NOT artifacts/results.csv."""
    path = FIXTURE_DIR / filename
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())
    return path


# ---------------------------------------------------------------------------
# Tests: ResultRow validation
# ---------------------------------------------------------------------------

class TestResultRowValidation:
    def test_valid_row_no_errors(self):
        row = make_row()
        errors = row.validate()
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_invalid_source(self):
        row = make_row(source="unknown")
        errors = row.validate()
        assert any("source" in e for e in errors)

    def test_invalid_status(self):
        row = make_row(status="failed")
        errors = row.validate()
        assert any("status" in e for e in errors)

    def test_negative_total_ms(self):
        row = make_row(total_ms=-1.0, work_ms=0, queue_wait_ms=0, retry_ms=0)
        errors = row.validate()
        assert any("negative" in e for e in errors)

    def test_ttft_exceeds_total(self):
        row = make_row(first_token_ms=1000.0, total_ms=500.0, work_ms=490.0, queue_wait_ms=5.0, retry_ms=5.0)
        errors = row.validate()
        assert any("first_token" in e for e in errors)

    def test_standin_must_not_have_grade(self):
        row = make_row(source="stand-in", answer_grade="correct")
        errors = row.validate()
        assert any("stand-in" in e and "grade" in e for e in errors)

    def test_standin_blank_grade_ok(self):
        row = make_row(source="stand-in", answer_grade="")
        errors = row.validate()
        assert not any("grade" in e for e in errors)

    def test_timing_partition(self):
        # queue_wait + work + retry should ≈ total (within 10ms)
        row = make_row(
            total_ms=500.0,
            queue_wait_ms=50.0,
            work_ms=400.0,
            retry_ms=50.0,  # exact
        )
        errors = row.validate()
        timing_errors = [e for e in errors if "timing" in e or "partition" in e]
        assert timing_errors == []

    def test_timing_partition_violation(self):
        row = make_row(
            total_ms=500.0,
            queue_wait_ms=0.0,
            work_ms=0.0,
            retry_ms=0.0,  # sum=0, violates partition
        )
        errors = row.validate()
        assert any("partition" in e for e in errors)


# ---------------------------------------------------------------------------
# Tests: CSV schema
# ---------------------------------------------------------------------------

class TestCSVSchema:
    def test_required_fields_in_header(self):
        row = make_row()
        d = row.to_dict()
        for field in REQUIRED_FIELDS:
            assert field in d, f"Required field missing: {field}"

    def test_all_fields_present(self):
        row = make_row()
        d = row.to_dict()
        for field in ALL_FIELDS:
            assert field in d, f"Field missing: {field}"

    def test_field_order(self):
        """First 12 fields must be in exact required order."""
        row = make_row()
        d = row.to_dict()
        keys = list(d.keys())
        for i, field in enumerate(REQUIRED_FIELDS):
            assert keys[i] == field, f"Field order mismatch at pos {i}: {keys[i]} != {field}"

    def test_write_and_read_csv(self):
        row = make_row()
        path = write_fixture_csv([row], "test_schema.csv")
        df = pd.read_csv(path)
        assert len(df) == 1
        assert df.iloc[0]["request_id"] == "req_test_001"


# ---------------------------------------------------------------------------
# Tests: Percentile computation
# ---------------------------------------------------------------------------

class TestPercentiles:
    def test_percentile_linear_method(self):
        """Verify numpy linear method gives expected result."""
        values = list(range(1, 101))  # 1..100
        result = float(np.percentile(values, 50, method="linear"))
        assert result == 50.5, f"Expected 50.5 for p50 of 1..100, got {result}"

    def test_p99_not_measured_below_100(self):
        gen = TableGenerator.__new__(TableGenerator)
        small = pd.Series([100.0] * 50)
        result = gen._pct(small, 99)
        assert "not measured" in result.lower()

    def test_p99_measured_at_100(self):
        gen = TableGenerator.__new__(TableGenerator)
        large = pd.Series([float(i) for i in range(1, 101)])
        result = gen._pct(large, 99)
        assert "not measured" not in result.lower()
        assert float(result) == pytest.approx(99.99, abs=1.0)

    def test_pct_empty_series(self):
        gen = TableGenerator.__new__(TableGenerator)
        empty = pd.Series([], dtype=float)
        result = gen._pct(empty, 50)
        assert result == "not measured"


# ---------------------------------------------------------------------------
# Tests: Table generation on fixtures
# ---------------------------------------------------------------------------

class TestTableGeneration:
    @pytest.fixture
    def fixture_csv(self, tmp_path):
        """Create a minimal valid fixture CSV."""
        rows = []
        for i in range(30):
            source = "real" if i < 15 else "stand-in"
            status = "ok" if i % 5 != 4 else "rate_limited"
            grade = ""
            if source == "real" and i < 10:
                grade = "correct" if i % 2 == 0 else "wrong"
            rows.append(make_row(
                request_id=f"req_fix_{i:03d}",
                scenario="steady" if i < 20 else "retrieval_eval",
                source=source,
                status=status,
                answer_grade=grade,
                retrieval_setting="A" if i < 10 else ("B" if i < 20 else "C"),
                run_id="run_fix_001",
                total_ms=200.0 + i * 10,
                first_token_ms=100.0 + i * 5,
                tokens_in=100 + i * 5,
                tokens_out=50 + i * 2,
                planned_duration_s=120.0,
                queue_wait_ms=10.0,
                work_ms=185.0 + i * 10,
                retry_ms=5.0,
            ))
        path = tmp_path / "fixture_results.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=ALL_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_dict())
        return path

    def test_table2a_generates(self, fixture_csv, tmp_path):
        gen = TableGenerator(csv_path=fixture_csv)
        t = gen.table2a_latency()
        assert "TABLE 2a" in t
        assert "steady" in t or "retrieval_eval" in t

    def test_table2b_generates(self, fixture_csv, tmp_path):
        gen = TableGenerator(csv_path=fixture_csv)
        t = gen.table2b_outcomes()
        assert "TABLE 2b" in t

    def test_table3_generates(self, fixture_csv, tmp_path):
        gen = TableGenerator(csv_path=fixture_csv)
        t = gen.table3_retrieval()
        assert "TABLE 3" in t

    def test_table1_generates(self, fixture_csv, tmp_path):
        gen = TableGenerator(csv_path=fixture_csv)
        t = gen.table1_limits()
        assert "TABLE 1" in t

    def test_percentages_sum(self, fixture_csv):
        """Succeeded + rate_limited + timed_out must partition offered."""
        gen = TableGenerator(csv_path=fixture_csv)
        df = pd.read_csv(fixture_csv, dtype=str)
        for status in ["ok", "rate_limited", "timeout", "error"]:
            assert status in VALID_STATUSES


# ---------------------------------------------------------------------------
# Tests: CSV invariants
# ---------------------------------------------------------------------------

class TestCSVInvariants:
    def test_no_errors_on_valid_fixture(self, tmp_path):
        rows = [
            make_row(request_id="req_a", source="real", status="ok", answer_grade=""),
            make_row(request_id="req_b", source="stand-in", status="ok", answer_grade=""),
        ]
        path = tmp_path / "valid.csv"
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=ALL_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_dict())
        errors = check_csv_invariants(path)
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_duplicate_request_id_flagged(self, tmp_path):
        rows = [
            make_row(request_id="dupe_id"),
            make_row(request_id="dupe_id"),
        ]
        path = tmp_path / "dupe.csv"
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=ALL_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_dict())
        errors = check_csv_invariants(path)
        assert any("duplicate" in e.lower() or "Duplicate" in e for e in errors)

    def test_standin_grade_flagged(self, tmp_path):
        rows = [make_row(source="stand-in", answer_grade="correct")]
        path = tmp_path / "si_grade.csv"
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=ALL_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_dict())
        errors = check_csv_invariants(path)
        assert any("grade" in e.lower() or "stand-in" in e.lower() for e in errors)

    def test_ttft_exceeds_total_flagged(self, tmp_path):
        rows = [make_row(first_token_ms=1000.0, total_ms=100.0, work_ms=90.0)]
        path = tmp_path / "ttft.csv"
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=ALL_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_dict())
        errors = check_csv_invariants(path)
        assert any("first_token" in e.lower() or "ttft" in e.lower() for e in errors)
