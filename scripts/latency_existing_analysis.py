# -*- coding: utf-8 -*-
"""Offline attribution of the latest approved development run.

This script does not call a model and does not rewrite the run. It reads the
stored cases.jsonl plus each session's trace JSONL and separates only what the
existing meter actually measured. Unmeasured phases are reported as unknown.

Usage:
    python scripts/latency_existing_analysis.py --run eval/results/<run> --out outputs/<file>.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _trace_events(run: Path, session_id: str, trace_id: str) -> list[dict[str, Any]]:
    directory = run / "instance-storage" / "sessions" / session_id / "outputs" / "traces"
    events: list[dict[str, Any]] = []
    for path in sorted(directory.glob("trace_*.jsonl")):
        for row in _load_jsonl(path):
            if row.get("trace_id") == trace_id:
                events.append(row)
    return events


def _transcript(run: Path, session_id: str) -> list[dict[str, Any]]:
    return _load_jsonl(
        run / "instance-storage" / "sessions" / session_id / "transcript.jsonl"
    )


def _turn_bounds(transcript: list[dict[str, Any]], turn: int) -> tuple[float | None, float | None]:
    user_at = None
    assistant_at = None
    for row in transcript:
        if int(row.get("turn") or 0) != turn:
            continue
        if row.get("role") == "user" and user_at is None:
            user_at = float(row["at"])
        if row.get("role") == "assistant":
            assistant_at = float(row["at"])
    return user_at, assistant_at


def _logical_tools(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return outermost tool runs only.

    LangChain emits an outer model tool call and an inner spec invocation for the
    same logical retrieval. The inner run's parent is the outer tool run; counting
    both would double count. This is a trace-shape fact, not a duration guess.
    """
    starts = {row.get("run_id"): row for row in events if row.get("event") == "tool_start"}
    tool_ids = set(starts)
    ends = {row.get("run_id"): row for row in events if row.get("event") in {"tool_end", "tool_error"}}

    result: list[dict[str, Any]] = []
    for run_id, start in starts.items():
        parent = start.get("parent_run_id")
        if parent in tool_ids:
            continue  # nested duplicate of the same logical tool call
        end = ends.get(run_id) or {}
        result.append({"start": start, "end": end})
    return result


def _model_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    starts = {row.get("run_id"): row for row in events if row.get("event") == "model_start"}
    ends = {
        row.get("run_id"): row
        for row in events
        if row.get("event") in {"model_end", "model_error"}
    }
    calls = []
    for run_id, start in starts.items():
        end = ends.get(run_id) or {}
        calls.append(
            {
                "run_id": run_id,
                "parent_run_id": start.get("parent_run_id"),
                "node": start.get("node"),
                "start_ts": _ts(start["ts"]) if start.get("ts") else None,
                "end_ts": _ts(end["ts"]) if end.get("ts") else None,
                "latency_ms": end.get("latency_ms"),
                "status": "error" if end.get("event") == "model_error" else "ok",
                "tokens": end.get("total_tokens"),
            }
        )
    return calls


def analyse_turn(
    *,
    run: Path,
    case_id: str,
    session_id: str,
    turn_index: int,
    turn_latency_ms: float,
    trace_id: str,
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    transcript = _transcript(run, session_id)
    user_at, assistant_at = _turn_bounds(transcript, turn_index)
    events = _trace_events(run, session_id, trace_id)
    models = _model_calls(events)
    tools = _logical_tools(events)
    run_end = next((row for row in events if row.get("event") == "run_end"), {})
    run_end_ts = _ts(run_end["ts"]) if run_end.get("ts") else None
    run_duration = run_end.get("duration_ms")

    main_models = [item for item in models if item.get("node") != "tools"]
    tool_models = [item for item in models if item.get("node") == "tools"]
    main_model_sum = sum(int(item.get("latency_ms") or 0) for item in main_models)
    tool_model_sum = sum(int(item.get("latency_ms") or 0) for item in tool_models)

    tool_rows = []
    for item in tools:
        start = item["start"]
        end = item["end"]
        tool_rows.append(
            {
                "tool": start.get("tool"),
                "run_id": start.get("run_id"),
                "tool_call_id": start.get("tool_call_id"),
                "latency_ms": end.get("latency_ms"),
                "status": end.get("status"),
                "nested": False,
            }
        )
    # Only outermost runs remain; nested run ids were filtered above. The inner
    # spec run is still useful evidence that the trace has a second layer.
    all_tool_ids = {
        row.get("run_id") for row in events if row.get("event") == "tool_start"
    }
    outer_ids = {row["start"].get("run_id") for row in tools}
    nested_count = len(all_tool_ids - outer_ids)

    interp = (((usage or {}).get("turn_metrics") or {}).get("by_layer") or {}).get(
        "interpretation"
    ) or {}
    interp_ms = interp.get("latency_ms")
    main_layer = (((usage or {}).get("turn_metrics") or {}).get("by_layer") or {}).get(
        "main_agent"
    ) or {}

    model_span = [item for item in main_models if item.get("start_ts") and item.get("end_ts")]
    first_main_start = min((item["start_ts"] for item in model_span), default=None)
    last_main_end = max((item["end_ts"] for item in model_span), default=None)

    pre_interpretation = None
    if user_at is not None and first_main_start is not None and interp_ms is not None:
        pre_interpretation = round(
            (first_main_start - float(interp_ms) / 1000.0 - user_at) * 1000.0, 3
        )
    post_run = None
    if run_end_ts is not None and assistant_at is not None:
        post_run = round((assistant_at - run_end_ts) * 1000.0, 3)
    run_start_ts = (
        run_end_ts - float(run_duration) / 1000.0
        if run_end_ts is not None and run_duration is not None
        else None
    )
    interp_to_run = (
        round((run_start_ts - (first_main_start - float(interp_ms) / 1000.0)) * 1000.0, 3)
        if run_start_ts is not None and first_main_start is not None and interp_ms is not None
        else None
    )

    return {
        "case_id": case_id,
        "session_id": session_id,
        "turn": turn_index,
        "trace_id": trace_id,
        "turn_latency_ms": turn_latency_ms,
        "transcript_user_at": user_at,
        "transcript_assistant_at": assistant_at,
        "measured": {
            "interpretation_ms": interp_ms,
            "main_run_inclusive_ms": run_duration,
            "main_model_calls": len(main_models),
            "main_model_sum_ms": main_model_sum,
            "main_model_latencies_ms": [item.get("latency_ms") for item in main_models],
            "tool_model_calls": len(tool_models),
            "tool_model_sum_ms": tool_model_sum,
            "tool_model_latencies_ms": [item.get("latency_ms") for item in tool_models],
            "logical_tool_calls": len(tool_rows),
            "nested_tool_events_ignored": nested_count,
            "tools": tool_rows,
            "turn_model_requests": (((usage or {}).get("turn_metrics") or {}).get("requests")),
            "turn_model_calls": (((usage or {}).get("turn_metrics") or {}).get("model_calls")),
            "turn_ledger_events": (((usage or {}).get("turn_metrics") or {}).get("ledger_events")),
            "main_layer_latency_ms": main_layer.get("latency_ms"),
        },
        "derived": {
            "pre_interpretation_gap_ms": pre_interpretation,
            "interpretation_to_run_start_gap_ms": interp_to_run,
            "post_run_to_persist_ms": post_run,
            "residual_after_interpretation_and_run_ms": (
                round(turn_latency_ms - float(interp_ms or 0) - float(run_duration or 0), 3)
                if interp_ms is not None and run_duration is not None
                else None
            ),
            "main_model_sum_minus_run_ms": (
                round(run_duration - main_model_sum, 3)
                if run_duration is not None
                else None
            ),
        },
        "unknown": [
            "client request send -> server receive (no client span in stored run)",
            "server receive -> worker spawn/seed/interpretation start (only residual inferred)",
            "worker process import / scenario build / Chroma open / embedding load (no span in stored run)",
            "first visible answer delta / TTFB / TTFT (not recorded in stored run)",
            "provider DNS/TCP/TLS/send/headers/first chunk (no transport hook in stored run)",
            "retrieval vector vs BM25 vs RRF union inside a rag_retrieve tool (only tool total)",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    run = Path(args.run)
    cases = _load_jsonl(run / "cases.jsonl")
    turns: list[dict[str, Any]] = []
    for case in cases:
        usage = case.get("usage") or {}
        for turn in case.get("turns") or []:
            if turn.get("role") != "user":
                continue
            turns.append(
                analyse_turn(
                    run=run,
                    case_id=case.get("case_id"),
                    session_id=case.get("session_id") or "",
                    turn_index=int(turn.get("index") or 0),
                    turn_latency_ms=float(turn.get("latency_ms") or 0),
                    trace_id=turn.get("trace_id") or "",
                    usage=turn.get("usage") or usage,
                )
            )
    # Multi-turn cases reuse session; each later turn has its own trace_id but the
    # case-level usage is a roll-up. Do not pretend per-turn usage is exact there.
    payload = {
        "schema_version": "latency-existing-analysis/v1",
        "source_run": str(run),
        "n_cases": len(cases),
        "n_turns": len(turns),
        "turns": turns,
        "scope_note": (
            "Existing run only. Per-turn usage for multi-turn cases is not "
            "separately persisted; interpretation latency is exact only for "
            "single-turn cases. Main/tool model call latencies come from trace."
        ),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "turns": len(turns)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
