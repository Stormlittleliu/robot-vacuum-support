# -*- coding: utf-8 -*-
"""Merge read-only trace evidence into a live three-mainlines report.

Generic version: it reads an explicit output directory and never assumes that a
provider 401 is the only possible outcome.  It does not call the product, model,
retriever or reducer and does not change the frozen suite/cases/expected values.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "outputs" / "three-mainlines-v1"


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _trace_for(trace: dict | None) -> dict:
    trace = trace or {}
    run_end = trace.get("run_end") or []
    model_events = trace.get("model_events") or []
    tool_events = trace.get("tool_events") or []
    return {
        "model_errors": sum(1 for event in model_events if event.get("error")),
        "model_attempts": sum(int(run.get("model_calls") or 0) for run in run_end),
        "tool_calls": sum(int(run.get("tool_calls") or 0) for run in run_end),
        "first_error": next((event.get("error") for event in model_events if event.get("error")), None),
        "run_end": run_end,
        "node_errors": trace.get("node_errors") or [],
        "session_last_status": trace.get("session_last_status"),
        "session_error_messages": trace.get("error_messages") or [],
        "embedding_load": trace.get("embedding_load") or [],
        "retrieval_spans": trace.get("retrieval_spans") or [],
        "retrieval_observed": any(
            str(event.get("tool") or "") in {"rag_retrieve", "rag_summarize"}
            for event in tool_events
            if event.get("event") == "tool_end"
        )
        or bool(trace.get("retrieval_spans")),
        "worker_has_model_path_double_nesting": bool(trace.get("worker_has_model_path_double_nesting")),
        "worker_has_pdf_coverage_failure": bool(trace.get("worker_has_pdf_coverage_failure")),
        "worker_has_file_not_found": bool(trace.get("worker_has_file_not_found")),
        "support": trace.get("support") or {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    out = Path(args.out_dir).resolve()
    report = read(out / "report.json")
    transcripts = read(out / "transcripts.json")
    traces = read(out / "trace-summary.json")

    for case in report.get("cases") or []:
        trace = _trace_for(traces.get(str(case.get("session_id"))))
        case["trace"] = trace
        case["session_last_status"] = trace["session_last_status"]
        case["session_error_messages"] = trace["session_error_messages"]
        case["embedding_load"] = trace["embedding_load"]
        case["retrieval_observed"] = trace["retrieval_observed"]

    for case in transcripts.get("cases") or []:
        trace = _trace_for(traces.get(str(case.get("session_id"))))
        case["trace"] = trace
        case["session_last_status"] = trace["session_last_status"]
        case["session_error_messages"] = trace["session_error_messages"]
        case["embedding_load"] = trace["embedding_load"]
        case["retrieval_observed"] = trace["retrieval_observed"]
        for turn in case.get("turns") or []:
            turn["run_end"] = trace["run_end"]
            turn["node_errors"] = trace["node_errors"]

    model_errors = sum(item["model_errors"] for item in (_trace_for(t) for t in traces.values()))
    model_attempts = sum(item["model_attempts"] for item in (_trace_for(t) for t in traces.values()))
    tool_calls = sum(item["tool_calls"] for item in (_trace_for(t) for t in traces.values()))
    failed_runs = sum(
        1
        for trace in traces.values()
        for run in trace.get("run_end") or []
        if str(run.get("status") or "") not in {"completed", "interrupted"}
    )
    auth_sessions = sum(
        1
        for trace in traces.values()
        if any(
            "auth" in str(event.get("error") or "").lower()
            or "401" in str(event.get("error") or "")
            or "403" in str(event.get("error") or "")
            for event in trace.get("model_events") or []
            if event.get("error")
        )
    )
    embedding_double = sum(
        1 for trace in traces.values() if trace.get("worker_has_model_path_double_nesting")
    )
    pdf_failures = sum(
        1 for trace in traces.values() if trace.get("worker_has_pdf_coverage_failure")
    )
    sessions_with_error = sum(
        1 for trace in traces.values() if any(event.get("error") for event in trace.get("model_events") or [])
    )
    sessions_with_failed_run = sum(
        1
        for trace in traces.values()
        if any(str(run.get("status") or "") not in {"completed", "interrupted"} for run in trace.get("run_end") or [])
    )
    embeddings_loaded = sum(
        1
        for trace in traces.values()
        for row in trace.get("embedding_load") or []
        if str(row.get("status") or "") == "ok"
    )
    embeddings_failed = sum(
        1
        for trace in traces.values()
        for row in trace.get("embedding_load") or []
        if str(row.get("status") or "") not in {"ok", "unknown"}
    )
    retrieval_sessions = sum(
        1 for trace in traces.values() if _trace_for(trace)["retrieval_observed"]
    )
    turn_checks = [
        check
        for case in transcripts.get("cases") or []
        for turn in case.get("turns") or []
        for check in turn.get("checks") or []
    ]
    turn_total = sum(len(case.get("turns") or []) for case in transcripts.get("cases") or [])
    turn_retrieval_attempted = sum(
        1
        for check in turn_checks
        if check.get("id") == "retrieval_attempted" and check.get("ok")
    )

    blockers = []
    if auth_sessions:
        blockers.append(
            {
                "id": "model_credentials_401",
                "severity": "blocking_semantic_verification",
                "observed": f"{sessions_with_error}/{len(traces)} sessions recorded a model error; auth-looking sessions={auth_sessions}",
                "product_behavior": "A model failure must be a typed SSE error with a failed session last_status, not HTTP-200 empty answer with completed run_end.",
                "not_faked": True,
            }
        )
    if embedding_double:
        blockers.append(
            {
                "id": "embedding_model_path_double_nesting",
                "severity": "product_configuration_finding",
                "observed": f"{embedding_double}/{len(traces)} worker logs still contain the doubled model path",
            }
        )
    if pdf_failures:
        blockers.append(
            {
                "id": "pdf_coverage_failure",
                "severity": "product_configuration_finding",
                "observed": f"{pdf_failures}/{len(traces)} worker logs contain a PDF coverage failure",
            }
        )

    report["status"] = report.get("status") or "completed_with_findings"
    report["blockers"] = blockers
    report["trace_analysis"] = {
        "sessions": len(traces),
        "model_calls_attempted": model_attempts,
        "model_calls_succeeded_estimate": max(0, model_attempts - model_errors),
        "model_errors": model_errors,
        "sessions_with_model_error": sessions_with_error,
        "auth_looking_sessions": auth_sessions,
        "failed_runs": failed_runs,
        "sessions_with_failed_run": sessions_with_failed_run,
        "tool_calls_observed": tool_calls,
        "sessions_with_retrieval": retrieval_sessions,
        "turns_total": turn_total,
        "turn_retrieval_attempted": turn_retrieval_attempted,
        "embedding_load_spans_observed": embeddings_loaded + embeddings_failed,
        "embedding_load_ok": embeddings_loaded,
        "embedding_load_failed": embeddings_failed,
        "embedding_double_nesting_sessions": embedding_double,
        "pdf_coverage_failure_sessions": pdf_failures,
        "trace_summary_file": str(out / "trace-summary.json"),
    }
    report["model_calls_attempted"] = model_attempts
    report["model_calls_succeeded"] = max(0, model_attempts - model_errors)
    report["model_errors"] = model_errors
    report["sessions_with_model_error"] = sessions_with_error
    report["sessions_with_failed_run"] = sessions_with_failed_run
    report["tool_calls_observed"] = tool_calls
    report["embedding_double_nesting_sessions"] = embedding_double
    report["pdf_coverage_failure_sessions"] = pdf_failures
    report["retrieval_sessions"] = retrieval_sessions
    report["turn_retrieval_attempted"] = turn_retrieval_attempted
    report["turns_total"] = turn_total
    report["embedding_load_spans_observed"] = embeddings_loaded + embeddings_failed
    report["semantic_advisory"] = {
        "status": "pending_main_model_review",
        "file": "semantic-findings.json",
        "authoritative_judge": False,
        "note": "Answers and state are available in transcripts.json; final advisory is written by the main model without changing program checks.",
    }
    report["credential_handling"] = {
        "env_file_used_reported_as_name_only": True,
        "access_token_value_logged": False,
        "deepseek_key_value_logged": False,
        "provider_request_headers_logged": False,
    }
    report["cleanup"]["existing_18080_untouched"] = report["cleanup"].get("existing_18080_open_after") is True

    transcripts["status"] = report["status"]
    transcripts["blockers"] = blockers
    write(out / "report.json", report)
    write(out / "transcripts.json", transcripts)
    write(
        out / "semantic-findings.json",
        {
            "schema_version": "three-mainlines-semantic-advisory/v1",
            "status": "pending_main_model_review",
            "authoritative_judge": False,
            "verdict": "pending",
            "reason": "Main-model advisory review is written after reading the full transcripts; it does not alter program checks.",
            "cases": {
                case.get("id"): {
                    "answer_chars": sum(int(turn.get("answer_chars") or 0) for turn in case.get("turn_summaries") or []),
                    "status": case.get("status"),
                }
                for case in report.get("cases") or []
            },
        },
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "sessions": len(traces),
                "model_calls_attempted": model_attempts,
                "model_errors": model_errors,
                "failed_runs": failed_runs,
                "retrieval_sessions": retrieval_sessions,
                "turn_retrieval_attempted": turn_retrieval_attempted,
                "turn_total": turn_total,
                "embedding_load_ok": embeddings_loaded,
                "embedding_double_nesting": embedding_double,
                "pdf_failures": pdf_failures,
                "blockers": [item["id"] for item in blockers],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
