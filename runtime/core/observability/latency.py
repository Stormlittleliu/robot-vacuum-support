# -*- coding: utf-8 -*-
"""Opt-in latency spans for the robot-vacuum instance runtime.

Why this exists
---------------
A user-visible turn latency is not one number owned by one layer.  The server
receives the request, may spawn a worker, the interpreter calls a model, the
worker builds the scenario and loads local RAG assets, the main agent makes
several model/tool calls, and the answer is persisted and sent back.  Existing
trace timings are inclusive (a main run contains tool waits and parallel tool
calls), so adding layer numbers is wrong.

This module records *spans* with explicit ``parent_span_id`` and monotonic
clocks.  It is disabled unless ``RVS_LATENCY_TRACE`` is truthy.  When disabled
every helper is a cheap no-op; it never changes prompts, routing, budgets or
timeouts.  It never records credentials, request bodies or headers.

Clock scope
-----------
``start_mono_ns`` / ``end_mono_ns`` are process-local ``time.perf_counter_ns``
values.  They are NOT comparable across processes.  Each span also carries a
wall anchor so a diagnostic reader can align the server, worker and client on
one wall timeline.  The monotonic duration is the authoritative duration; the
wall timestamps are for ordering only.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

SPAN_SCHEMA_VERSION = "latency-span/v1"
METER_VERSION = "rvs-latency-meter/2026-11-13"
CLOCK_SCOPE = "process_monotonic_ns+wall_anchor_ns"

_TRUTHY = {"1", "true", "yes", "on"}
_ENABLED = os.getenv("RVS_LATENCY_TRACE", "").strip().lower() in _TRUTHY
_PROCESS_ROLE = os.getenv("RVS_LATENCY_PROCESS", "server").strip() or "server"
_PID = os.getpid()
_ANCHOR_MONO_NS = time.perf_counter_ns()
_ANCHOR_WALL_NS = time.time_ns()
_WRITE_LOCK = threading.Lock()
_WRITER: "_SpanWriter | None" = None

_CTX: ContextVar[dict[str, Any] | None] = ContextVar("rvs_latency_ctx", default=None)
_ACTIVE_SPAN: ContextVar[str | None] = ContextVar("rvs_latency_active_span", default=None)
_ACTIVE_CALL: ContextVar[str | None] = ContextVar("rvs_latency_active_call", default=None)
_CALL_SPAN: ContextVar[str | None] = ContextVar("rvs_latency_call_span", default=None)


def enabled() -> bool:
    return _ENABLED


def process_role() -> str:
    return _PROCESS_ROLE


def _now_pair() -> tuple[int, int]:
    mono = time.perf_counter_ns()
    return mono, _ANCHOR_WALL_NS + (mono - _ANCHOR_MONO_NS)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def current_context() -> dict[str, Any]:
    return dict(_CTX.get() or {})


def context_ids() -> dict[str, Any]:
    ctx = current_context()
    return {
        "request_id": ctx.get("request_id"),
        "turn_id": ctx.get("turn_id"),
        "trace_id": ctx.get("trace_id"),
        "cold": ctx.get("cold"),
        "worker_parent_span_id": ctx.get("worker_parent_span_id"),
    }


@contextmanager
def bind_context(**values: Any) -> Iterator[dict[str, Any]]:
    """Bind request/turn/cold ids for this thread (and its children)."""
    merged = {**current_context(), **{k: v for k, v in values.items() if v is not None}}
    token = _CTX.set(merged)
    try:
        yield merged
    finally:
        _CTX.reset(token)


@contextmanager
def bind_call(call_id: str, *, layer: str | None = None) -> Iterator[None]:
    """Tag provider transport spans with the model call they belong to."""
    ctx = _CTX.get() or {}
    token = _ACTIVE_CALL.set(call_id)
    ctx_token = _CTX.set({**ctx, "call_id": call_id, "layer": layer or ctx.get("layer")})
    try:
        yield
    finally:
        _CTX.reset(ctx_token)
        _ACTIVE_CALL.reset(token)


def current_call_id() -> str | None:
    return _ACTIVE_CALL.get()


def push_call_span(span_id: str | None) -> Any:
    """Remember the model-call span id across callback start/end."""
    return _CALL_SPAN.set(span_id)


def pop_call_span(token: Any) -> None:
    try:
        _CALL_SPAN.reset(token)
    except Exception:
        pass


def current_call_span() -> str | None:
    return _CALL_SPAN.get()


def _writer() -> "_SpanWriter":
    global _WRITER
    if _WRITER is None:
        _WRITER = _SpanWriter()
    return _WRITER


class _SpanWriter:
    def __init__(self) -> None:
        explicit = os.getenv("RVS_LATENCY_DIR", "").strip()
        if explicit:
            self.path = Path(explicit) / f"spans_{_PROCESS_ROLE}_{_PID}.jsonl"
        else:
            try:
                from core.paths import output_path

                self.path = output_path("latency", f"spans_{_PROCESS_ROLE}_{_PID}.jsonl")
            except Exception:  # pragma: no cover - artifact without core.paths
                self.path = Path("outputs") / "latency" / f"spans_{_PROCESS_ROLE}_{_PID}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, row: dict[str, Any]) -> None:
        from core.observability.redact import redact_value

        line = json.dumps(redact_value(row), ensure_ascii=False, default=str)
        with _WRITE_LOCK:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class Span:
    """An open span. ``end`` is idempotent; a never-ended span is not written."""

    def __init__(
        self,
        stage: str,
        *,
        parent_span_id: str | None = None,
        context: dict[str, Any] | None = None,
        **attrs: Any,
    ) -> None:
        self.span_id = new_id("sp")
        self.stage = stage
        self.parent_span_id = parent_span_id
        self.context = dict(context or current_context())
        self.call_id = self.context.get("call_id")
        self.attrs = {k: v for k, v in attrs.items() if v is not None}
        self.start_mono_ns, self.start_wall_ns = _now_pair()
        self.end_mono_ns: int | None = None
        self.end_wall_ns: int | None = None
        self.status = "unknown"
        self._closed = False

    def end(self, status: str = "ok", **attrs: Any) -> "Span":
        if self._closed:
            return self
        self._closed = True
        self.status = status
        self.end_mono_ns, self.end_wall_ns = _now_pair()
        self.attrs.update({k: v for k, v in attrs.items() if v is not None})
        if _ENABLED:
            try:
                _writer().write(self.to_dict())
            except Exception:
                # Diagnostics must never break a turn.
                pass
        return self

    def to_dict(self) -> dict[str, Any]:
        duration = (
            (self.end_mono_ns - self.start_mono_ns) / 1_000_000.0
            if self.end_mono_ns is not None
            else None
        )
        return {
            "schema_version": SPAN_SCHEMA_VERSION,
            "meter_version": METER_VERSION,
            "clock_scope": CLOCK_SCOPE,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id or self.context.get("worker_parent_span_id"),
            "request_id": self.context.get("request_id"),
            "turn_id": self.context.get("turn_id"),
            "trace_id": self.context.get("trace_id"),
            "call_id": self.call_id or current_call_id(),
            "stage": self.stage,
            "process_role": _PROCESS_ROLE,
            "pid": _PID,
            "cold": self.context.get("cold"),
            "status": self.status,
            "start_mono_ns": self.start_mono_ns,
            "end_mono_ns": self.end_mono_ns,
            "duration_ms": round(duration, 3) if duration is not None else None,
            "start_wall_ns": self.start_wall_ns,
            "end_wall_ns": self.end_wall_ns,
            "wall_anchor_mono_ns": _ANCHOR_MONO_NS,
            "wall_anchor_wall_ns": _ANCHOR_WALL_NS,
            "attrs": self.attrs,
        }


def open_span(
    stage: str,
    *,
    parent_span_id: str | None = None,
    context: dict[str, Any] | None = None,
    **attrs: Any,
) -> Span | None:
    """Open a span that is ended later (callback start/end pairs)."""
    if not _ENABLED:
        return None
    return Span(
        stage,
        parent_span_id=parent_span_id or _ACTIVE_SPAN.get(),
        context=context,
        **attrs,
    )


@contextmanager
def span(
    stage: str,
    *,
    parent_span_id: str | None = None,
    context: dict[str, Any] | None = None,
    **attrs: Any,
) -> Iterator[Span | None]:
    """Context manager span. No-op and zero allocation when disabled."""
    if not _ENABLED:
        yield None
        return
    item = Span(
        stage,
        parent_span_id=parent_span_id or _ACTIVE_SPAN.get(),
        context=context,
        **attrs,
    )
    token = _ACTIVE_SPAN.set(item.span_id)
    try:
        yield item
    except BaseException as exc:  # noqa: BLE001 - preserve failure status, re-raise
        item.end(status="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        raise
    else:
        item.end(status="ok")
    finally:
        _ACTIVE_SPAN.reset(token)


def point(
    stage: str,
    *,
    parent_span_id: str | None = None,
    context: dict[str, Any] | None = None,
    status: str = "ok",
    **attrs: Any,
) -> None:
    """Zero-duration point span (receive, first byte, first delta)."""
    if not _ENABLED:
        return
    item = Span(
        stage,
        parent_span_id=parent_span_id or _ACTIVE_SPAN.get(),
        context=context,
        **attrs,
    )
    item.end(status=status)


def child_of(span_id: str | None) -> dict[str, Any]:
    """Small helper for callbacks that need the current open span id."""
    return {"parent_span_id": span_id or _ACTIVE_SPAN.get()}


def active_span_id() -> str | None:
    return _ACTIVE_SPAN.get()


def push_active_span(span_id: str | None) -> Any:
    return _ACTIVE_SPAN.set(span_id)


def pop_active_span(token: Any) -> None:
    try:
        _ACTIVE_SPAN.reset(token)
    except Exception:
        pass


def read_spans(directory: str | Path) -> list[dict[str, Any]]:
    """Read all span JSONL files in a directory, tolerating partial lines."""
    root = Path(directory)
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("spans_*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return rows


def summarize_calls(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise model call count and first/visible answer timing.

    The product path uses a non-streaming model invoke inside the ReAct agent,
    so there is no token-level TTFT in these spans.  ``worker.first_delta`` is
    the first *visible* answer chunk after the full answer exists; it must not be
    reported as token TTFB.
    """
    model_calls = [row for row in spans if row.get("stage") == "model.call"]
    provider_calls = [row for row in spans if row.get("stage") == "provider.request"]
    first_delta = next((row for row in spans if row.get("stage") == "worker.first_delta"), None)
    return {
        "model_calls": len(model_calls),
        "model_call_latencies_ms": [row.get("duration_ms") for row in model_calls],
        "provider_requests": len(provider_calls),
        "provider_statuses": [row.get("status") for row in provider_calls],
        "first_visible_delta_wall_ns": (first_delta or {}).get("start_wall_ns"),
        "ttft_available": False,
        "ttft_reason": "product path uses non-streaming invoke; provider first body chunk is not a first-token timestamp",
        "zero_calls_status": "not_called" if not model_calls else "called",
    }


def _interval(row: dict[str, Any]) -> tuple[int, int] | None:
    start = row.get("start_wall_ns")
    end = row.get("end_wall_ns")
    if start is None or end is None or end < start:
        return None
    return int(start), int(end)


def union_ms(spans: list[dict[str, Any]]) -> float | None:
    """Wall-clock union of the given spans.

    Use this for parallel siblings.  Summing them double counts overlap; this
    merges overlapping intervals instead.  Different processes are aligned by
    their wall anchors, so the result is an ordering/attribution aid, not a
    replacement for a process-local monotonic duration.
    """
    intervals = sorted(item for item in (_interval(row) for row in spans) if item)
    if not intervals:
        return None
    merged: list[list[int]] = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return round(sum(end - start for start, end in merged) / 1_000_000.0, 3)


def exclusive_ms(parent: dict[str, Any], children: list[dict[str, Any]]) -> float | None:
    """Parent inclusive duration minus the union of child intervals.

    Children are clipped to the parent interval so a wide child cannot make the
    residual negative.  A negative residual means the spans are inconsistent
    (for example a different clock scope); callers should surface that instead
    of hiding it.
    """
    parent_interval = _interval(parent)
    parent_ms = parent.get("duration_ms")
    if parent_interval is None or parent_ms is None:
        return None
    p_start, p_end = parent_interval
    clipped = []
    for row in children:
        interval = _interval(row)
        if interval is None:
            continue
        start, end = interval
        start, end = max(start, p_start), min(end, p_end)
        if end > start:
            clipped.append({"start_wall_ns": start, "end_wall_ns": end})
    child_union = union_ms(clipped) or 0.0
    return round(float(parent_ms) - child_union, 3)
