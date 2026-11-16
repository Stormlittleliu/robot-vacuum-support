# -*- coding: utf-8 -*-
"""Timing-attribution contract tests.

These tests use local sleeps and fake streams only.  They verify that spans
parent correctly, that parallel/duplicate intervals are unioned rather than
summed, that first-chunk/end order is recorded, and that failures are not
reported as success.  They make no network call and are not evidence about the
user's real network or provider latency.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from core.observability import latency  # noqa: E402


@pytest.fixture()
def recording(tmp_path, monkeypatch):
    monkeypatch.setenv("RVS_LATENCY_DIR", str(tmp_path / "spans"))
    monkeypatch.setenv("RVS_LATENCY_PROCESS", "test")
    monkeypatch.setattr(latency, "_ENABLED", True)
    monkeypatch.setattr(latency, "_PROCESS_ROLE", "test")
    monkeypatch.setattr(latency, "_WRITER", None)
    return tmp_path / "spans"


def _spans(recording):
    return latency.read_spans(recording)


def test_parent_child_and_exclusive_residual(recording):
    with latency.bind_context(request_id="req-1", turn_id="turn-1", cold=True):
        with latency.span("worker.execution") as parent:
            time.sleep(0.02)
            with latency.span("worker.scenario_resolve"):
                time.sleep(0.05)
            with latency.span("retrieval.total"):
                time.sleep(0.03)
    rows = _spans(recording)
    assert len(rows) == 3
    by_stage = {row["stage"]: row for row in rows}
    assert by_stage["worker.execution"]["duration_ms"] >= 70
    assert by_stage["worker.scenario_resolve"]["parent_span_id"] == by_stage["worker.execution"]["span_id"]
    assert by_stage["retrieval.total"]["parent_span_id"] == by_stage["worker.execution"]["span_id"]
    assert all(row["request_id"] == "req-1" for row in rows)
    children = [by_stage["worker.scenario_resolve"], by_stage["retrieval.total"]]
    residual = latency.exclusive_ms(by_stage["worker.execution"], children)
    assert residual is not None and residual >= 0


def test_parallel_union_does_not_add_siblings(recording):
    with latency.bind_context(request_id="req-2", turn_id="turn-2"):
        with latency.span("tool.call") as tool:
            with latency.span("retrieval.vector"):
                time.sleep(0.06)
            with latency.span("retrieval.bm25"):
                time.sleep(0.06)
    rows = [row for row in _spans(recording) if row["request_id"] == "req-2"]
    by_stage = {row["stage"]: row for row in rows}
    children = [by_stage["retrieval.vector"], by_stage["retrieval.bm25"]]
    summed = sum(row["duration_ms"] for row in children)
    union = latency.union_ms(children)
    # These two children are sequential (no overlap), so union equals sum.
    assert union == pytest.approx(summed, abs=2.0)
    # A true parallel pair overlaps and must not be summed.
    with latency.bind_context(request_id="req-3", turn_id="turn-3"):
        import contextvars
        import threading

        barrier = threading.Barrier(2)

        def work(stage):
            with latency.span(stage):
                barrier.wait()
                time.sleep(0.05)

        threads = []
        for i in range(2):
            ctx = contextvars.copy_context()
            threads.append(
                threading.Thread(target=ctx.run, args=(work, f"parallel.{i}"))
            )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    parallel = [row for row in _spans(recording) if row["request_id"] == "req-3"]
    union_parallel = latency.union_ms(parallel)
    sum_parallel = sum(row["duration_ms"] for row in parallel)
    assert union_parallel is not None
    assert union_parallel < sum_parallel


def test_fake_stream_first_chunk_before_end_and_failure_status(recording):
    from core.observability.provider_trace import _TracedResponseStream

    def chunks():
        time.sleep(0.02)
        yield b"first"
        time.sleep(0.02)
        yield b"second"

    stream = _TracedResponseStream(chunks(), request_span_id="parent-call", host="fake.local")
    assert b"".join(stream) == b"firstsecond"
    rows = _spans(recording)
    first = next(row for row in rows if row["stage"] == "provider.first_chunk")
    body = next(row for row in rows if row["stage"] == "provider.body")
    assert first["parent_span_id"] == body["span_id"]
    assert first["start_wall_ns"] <= body["end_wall_ns"]

    def broken():
        yield b"one"
        raise RuntimeError("stream broken")

    broken_stream = _TracedResponseStream(broken(), request_span_id="parent-call", host="fake.local")
    with pytest.raises(RuntimeError):
        list(broken_stream)
    bodies = [row for row in _spans(recording) if row["stage"] == "provider.body"]
    assert bodies[-1]["status"] == "error"


def test_worker_startup_delay_and_zero_calls(recording):
    with latency.bind_context(request_id="req-4", turn_id="turn-4", cold=True):
        with latency.span("worker.spawn"):
            time.sleep(0.04)
    rows = _spans(recording)
    spawn = next(row for row in rows if row["stage"] == "worker.spawn")
    assert spawn["duration_ms"] >= 30
    summary = latency.summarize_calls([])
    assert summary["zero_calls_status"] == "not_called"
    assert summary["ttft_available"] is False
    assert summary["ttft_reason"]


def test_context_manager_marks_failure(recording):
    with pytest.raises(ValueError):
        with latency.bind_context(request_id="req-5", turn_id="turn-5"):
            with latency.span("worker.execution"):
                raise ValueError("cancelled")
    rows = _spans(recording)
    assert rows[0]["status"] == "error"
    assert "cancelled" in rows[0]["attrs"].get("error", "")
