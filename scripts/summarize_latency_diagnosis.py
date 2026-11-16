# -*- coding: utf-8 -*-
"""Summarise the completed 3-turn latency diagnosis without double counting.

Reads outputs/latency-diagnosis/run.json plus raw-spans.json. It filters spans to
the three request ids in that run, so stale spans from an earlier failed attempt
in the same folder cannot leak into the table.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from core.observability import latency  # noqa: E402


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _wall(row: dict[str, Any], key: str = "start_wall_ns") -> int | None:
    value = row.get(key)
    return int(value) if value is not None else None


def _within(row: dict[str, Any], start: int, end: int) -> bool:
    s, e = _wall(row), _wall(row, "end_wall_ns")
    return s is not None and e is not None and e >= start and s <= end


def _spans_for(spans: list[dict[str, Any]], request_id: str, stage: str) -> list[dict[str, Any]]:
    return [row for row in spans if row.get("request_id") == request_id and row.get("stage") == stage]


def _union(spans: list[dict[str, Any]]) -> float | None:
    return latency.union_ms(spans)


def _exclusive(parent: dict[str, Any], children: list[dict[str, Any]]) -> float | None:
    return latency.exclusive_ms(parent, children)


def main() -> int:
    run_dir = ROOT / "outputs" / "latency-diagnosis"
    run = _load(run_dir / "run.json")
    raw = _load(run_dir / "raw-spans.json")
    request_ids = [turn["request_id"] for turn in run["turns"]]
    current = [row for row in raw["spans"] if row.get("request_id") in set(request_ids)]
    server_receive = {rid: _spans_for(current, rid, "server.receive") for rid in request_ids}
    server_request = {rid: _spans_for(current, rid, "server.request")[0] for rid in request_ids}
    # Attribute import-time spans (no request id) to turn 1 by wall window.
    first_start = min(_wall(row) for rows in server_receive.values() for row in rows)
    last_end = max(_wall(server_request[rid], "end_wall_ns") for rid in request_ids)
    import_spans = [
        row for row in raw["spans"]
        if row.get("request_id") is None and _within(row, first_start, last_end)
    ]

    turns: list[dict[str, Any]] = []
    for index, turn in enumerate(run["turns"], 1):
        rid = turn["request_id"]
        rows = [row for row in current if row.get("request_id") == rid]
        stage = lambda name: _spans_for(rows, rid, name)  # noqa: E731
        model_calls = stage("model.call")
        tool_calls = stage("tool.call")
        retrieval = stage("retrieval.total")
        rag = stage("rag.summary.invoke")
        post = stage("server.postprocess") + stage("server.persist_assistant") + stage("server.persist_support_state")
        reserve = stage("server.reserve")
        interp = stage("server.interpretation")
        worker_run = stage("server.worker_run")
        worker_exec = stage("worker.execution")
        scenario = stage("worker.scenario_resolve")
        main_run = stage("main.run")
        model_load = [
            row for row in rows if row.get("stage") == "model.load.chat"
        ] + [
            row for row in import_spans
            if row.get("stage") == "model.load.chat" and (_wall(row) or 0) <= last_end
        ]
        worker_boot = [
            row for row in rows
            if row.get("stage") in {
                "worker.process_start", "worker.ready", "worker.spawn",
                "worker.seed_assets", "worker.rag_service_init", "retrieval.chroma_open",
                "worker.workflow_build",
            }
        ]
        if index == 1:
            worker_boot += [
                row for row in import_spans
                if row.get("stage") in {"worker.process_start", "worker.ready"}
            ]
        # Deduplicate worker boot rows by stage for readability.
        boot_by_stage: dict[str, list[dict[str, Any]]] = {}
        for row in worker_boot:
            boot_by_stage.setdefault(row["stage"], []).append(row)
        model_sum = sum(float(row.get("duration_ms") or 0) for row in model_calls)
        tool_union = _union(tool_calls)
        retrieval_union = _union(retrieval)
        rag_union = _union(rag)
        post_union = _union(post)
        vector_union = _union(stage("retrieval.vector"))
        bm25_union = _union(stage("retrieval.bm25"))
        rrf_union = _union(stage("retrieval.rrf"))
        embed_query_union = _union(stage("retrieval.embedding_query"))
        embed_load_union = _union(stage("embedding.model_load"))
        server_total = float((server_request[rid].get("duration_ms") or 0))
        main_total = float((main_run[0].get("duration_ms") if main_run else 0) or 0)
        exec_total = float((worker_exec[0].get("duration_ms") if worker_exec else 0) or 0)
        scenario_total = float((scenario[0].get("duration_ms") if scenario else 0) or 0)
        interp_total = float((interp[0].get("duration_ms") if interp else 0) or 0)
        reserve_total = float((reserve[0].get("duration_ms") if reserve else 0) or 0)
        main_children = model_calls + tool_calls
        main_excl = _exclusive(main_run[0], main_children) if main_run else None
        tool_excl = (
            _exclusive(tool_calls[0], retrieval + rag) if tool_calls else None
        )
        turns.append(
            {
                "turn": index,
                "cold": turn.get("cold_header") == "1",
                "request_id": rid,
                "client_total_ms": turn.get("client_total_ms"),
                "server_total_ms": round(server_total, 3),
                "server_reserve_ms": round(reserve_total, 3),
                "interpretation_ms": round(interp_total, 3),
                "worker_run_ms": round(float((worker_run[0].get("duration_ms") if worker_run else 0) or 0), 3),
                "worker_execution_ms": round(exec_total, 3),
                "scenario_resolve_ms": round(scenario_total, 3),
                "main_run_ms": round(main_total, 3),
                "main_exclusive_ms": main_excl,
                "model_calls_ms": [row.get("duration_ms") for row in model_calls],
                "model_calls_sum_ms": round(model_sum, 3),
                "model_calls_union_ms": _union(model_calls),
                "tool_calls_count": len(tool_calls),
                "tool_calls_ms": [row.get("duration_ms") for row in tool_calls],
                "tool_union_ms": tool_union,
                "tool_exclusive_ms": tool_excl,
                "retrieval_total_ms": retrieval_union,
                "retrieval_vector_union_ms": vector_union,
                "retrieval_bm25_union_ms": bm25_union,
                "retrieval_rrf_union_ms": rrf_union,
                "embedding_query_union_ms": embed_query_union,
                "embedding_model_load_union_ms": embed_load_union,
                "rag_summary_ms": rag_union,
                "postprocess_ms": post_union,
                "first_visible_answer_delta_ms": turn.get("first_answer_delta_ms"),
                "answer_event_ms": turn.get("answer_event_ms"),
                "headers_ms": turn.get("headers_ms"),
                "client_first_sse_line_ms": turn.get("first_sse_line_ms"),
                "boot": {
                    name: {
                        "count": len(items),
                        "duration_ms": [row.get("duration_ms") for row in items],
                    }
                    for name, items in boot_by_stage.items()
                },
                "model_load_chat_ms": [row.get("duration_ms") for row in model_load],
            }
        )

    payload = {
        "schema_version": "latency-diagnosis-summary/v1",
        "source_run": str(run_dir / "run.json"),
        "source_raw_spans": str(run_dir / "raw-spans.json"),
        "n": len(turns),
        "sample_note": "n=3 process samples; no p95 or distribution claim",
        "turns": turns,
        "definitions": {
            "first_visible_answer_delta_ms": "client receives the first answer_delta SSE event; not provider TTFT",
            "provider_ttft": "unavailable: non-streaming model path and proxy-safe transport tracing disabled",
            "main_run_ms": "inclusive; contains model calls and tool calls",
            "model_calls_sum_ms": "sum of serial model requests, not the main_run inclusive value",
            "tool_union_ms": "union of overlapping outer/inner tool spans; nested duplicates are not added",
            "retrieval_total_ms": "union of retrieval spans; RRF overlaps vector/BM25 and is not added on top",
        },
    }
    out = run_dir / "summary.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
