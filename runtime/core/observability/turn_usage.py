# -*- coding: utf-8 -*-
"""Per-turn model usage meter.

Why this exists: a turn can make several model calls in different layers
(interpretation in the server process, the ReAct answer in the worker, and the
RAG summarize call inside a tool).  Adding ``trace.total_tokens`` and a
``budget.total_tokens`` double-counts; adding only the answer hides the other
layers.  This meter records each *raw model response* once by ``call_id`` and
attributes it to exactly one layer.

Missing provider usage is stored as ``None`` and marks the turn ``partial`` or
``unavailable``; nothing is estimated.  Cost is deliberately not computed here:
the repository pricing file is an example, not an authoritative current price.
"""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

TURN_USAGE_SCHEMA_VERSION = "turn-usage/v1"
LAYERS = ("interpretation", "main_agent", "rag_summary")

_current = ContextVar("support_turn_usage", default=None)


def _normalize_usage(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict) or not usage:
        return None
    prompt = usage.get("input_tokens")
    if prompt is None:
        prompt = usage.get("prompt_tokens")
    completion = usage.get("output_tokens")
    if completion is None:
        completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if total is None and (prompt is not None or completion is not None):
        total = int(prompt or 0) + int(completion or 0)
    if prompt is None and completion is None and total is None:
        return None
    return {
        "input_tokens": int(prompt) if prompt is not None else None,
        "output_tokens": int(completion) if completion is not None else None,
        "total_tokens": int(total) if total is not None else None,
    }


@dataclass
class TurnUsage:
    calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def record(
        self,
        *,
        layer: str,
        usage: Any,
        model: str | None = None,
        latency_ms: int | None = None,
        call_id: str | None = None,
        method: str | None = None,
        route: str | None = None,
        fallback: bool = False,
        reused: bool = False,
        model_requests: int = 1,
        **extra: Any,
    ) -> str:
        call_id = call_id or f"{layer}-{uuid.uuid4().hex[:12]}"
        normalized = _normalize_usage(usage)
        row = {
            "call_id": call_id,
            "layer": layer if layer in LAYERS else "other",
            "model": model,
            "method": method,
            "route": route,
            "latency_ms": latency_ms,
            "usage": normalized,
            "usage_status": "reported" if normalized else "unavailable",
            "fallback": bool(fallback),
            "reused": bool(reused),
            # Ledger events and real model requests are different things.  A
            # main-agent aggregate event can stand for two trace model calls
            # while carrying one combined usage object.
            "model_requests": max(0, int(model_requests)),
            "recorded_at": time.time(),
            **extra,
        }
        with self._lock:
            if any(item.get("call_id") == call_id for item in self.calls):
                return call_id
            self.calls.append(row)
        return call_id

    def merge(self, summary: dict[str, Any] | None) -> None:
        if not isinstance(summary, dict):
            return
        for row in summary.get("calls") or []:
            if not isinstance(row, dict):
                continue
            with self._lock:
                if any(item.get("call_id") == row.get("call_id") for item in self.calls):
                    continue
                self.calls.append(dict(row))

    def summary(self) -> dict[str, Any]:
        with self._lock:
            rows = [dict(item) for item in self.calls]
        by_layer: dict[str, dict[str, Any]] = {}
        total_prompt = 0
        total_completion = 0
        total_tokens = 0
        total_model_requests = 0
        all_reported = True
        any_call = bool(rows)
        for layer in LAYERS:
            layer_rows = [row for row in rows if row.get("layer") == layer]
            if not layer_rows:
                continue
            reported = [row for row in layer_rows if row.get("usage")]
            if len(reported) != len(layer_rows):
                all_reported = False
            prompt = sum((row["usage"] or {}).get("input_tokens") or 0 for row in reported)
            completion = sum((row["usage"] or {}).get("output_tokens") or 0 for row in reported)
            total = sum((row["usage"] or {}).get("total_tokens") or 0 for row in reported)
            model_requests = sum(int(row.get("model_requests") or 0) for row in layer_rows)
            total_prompt += prompt
            total_completion += completion
            total_tokens += total
            total_model_requests += model_requests
            by_layer[layer] = {
                "requests": model_requests,
                "model_calls": model_requests,
                "ledger_events": len(layer_rows),
                "aggregated_events": sum(1 for row in layer_rows if int(row.get("model_requests") or 1) > 1),
                "reported_calls": len(reported),
                "input_tokens": prompt if reported else None,
                "output_tokens": completion if reported else None,
                "total_tokens": total if reported else None,
                "usage_status": "reported"
                if len(reported) == len(layer_rows)
                else ("partial" if reported else "not_called"),
                "latency_ms": sum(int(row.get("latency_ms") or 0) for row in layer_rows),
                "models": sorted({str(row.get("model")) for row in layer_rows if row.get("model")}),
                "methods": sorted({str(row.get("method")) for row in layer_rows if row.get("method")}),
                "fallback_calls": sum(1 for row in layer_rows if row.get("fallback")),
                "reused_calls": sum(1 for row in layer_rows if row.get("reused")),
            }
        if total_model_requests == 0:
            # Cached replay / protocol route: a ledger event may exist, but no
            # model request was made, so this is not a reported token total.
            usage_status = "not_called"
            prompt_out = completion_out = total_out = None
        elif not any_call:
            usage_status = "not_called"
            prompt_out = completion_out = total_out = None
        elif all_reported:
            usage_status = "reported"
            prompt_out, completion_out, total_out = total_prompt, total_completion, total_tokens
        else:
            usage_status = "partial"
            prompt_out = completion_out = total_out = None
        return {
            "schema_version": TURN_USAGE_SCHEMA_VERSION,
            "calls": rows,
            "by_layer": by_layer,
            # ``model_calls`` is the true number of model requests.  The ledger
            # can contain fewer events when one aggregate row represents several
            # calls; tokens are still counted once from that aggregate row.
            "requests": total_model_requests,
            "model_calls": total_model_requests,
            "ledger_events": len(rows),
            "aggregated_events": sum(1 for row in rows if int(row.get("model_requests") or 1) > 1),
            "total": {
                "input_tokens": prompt_out,
                "output_tokens": completion_out,
                "total_tokens": total_out,
            },
            "usage_status": usage_status,
            "cost": None,
            "cost_currency": "CNY",
            "cost_status": "unavailable_no_authoritative_unit_price",
            "note": "cost is null because the local pricing file is explicitly an example, not a current authoritative price; model_calls sums model_requests per ledger event, while ledger_events counts aggregate rows",
        }


@contextmanager
def turn_usage_scope(meter: TurnUsage):
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


def current_turn_usage() -> TurnUsage | None:
    return _current.get()


def record_model_usage(
    *,
    layer: str,
    usage: Any,
    model: str | None = None,
    latency_ms: int | None = None,
    call_id: str | None = None,
    **extra: Any,
) -> str:
    """Record a raw model response; no-op when no turn meter is active."""
    meter = current_turn_usage()
    if meter is None:
        return ""
    return meter.record(
        layer=layer,
        usage=usage,
        model=model,
        latency_ms=latency_ms,
        call_id=call_id,
        **extra,
    )
