# -*- coding: utf-8 -*-
"""Merge read-only trace evidence into the live report.

Does not call the product, model, retriever or reducer and does not change the
frozen suite/cases/expected values.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "three-mainlines-v1"


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    report = read(OUT / "report.json")
    transcripts = read(OUT / "transcripts.json")
    traces = read(OUT / "trace-summary.json")
    initial = OUT / "raw" / "report.before-trace-analysis.json"
    if not initial.is_file():
        write(initial, report)

    model_errors = 0
    model_attempts = 0
    tool_calls = 0
    for item in traces.values():
        model_attempts += sum(int(run.get("model_calls") or 0) for run in item.get("run_end") or [])
        tool_calls += sum(int(run.get("tool_calls") or 0) for run in item.get("run_end") or [])
        model_errors += sum(1 for event in item.get("model_events") or [] if event.get("error"))
    first_error = next(
        (event["error"] for item in traces.values() for event in item.get("model_events") or [] if event.get("error")),
        None,
    )
    embedding_failures = sum(1 for item in traces.values() if item.get("worker_has_model_path_double_nesting"))
    pdf_failures = sum(1 for item in traces.values() if item.get("worker_has_pdf_coverage_failure"))
    sessions = len(traces)
    sessions_with_error = sum(1 for item in traces.values() if any(event.get("error") for event in item.get("model_events") or []))

    blockers = [
        {
            "id": "model_credentials_401",
            "severity": "blocking_semantic_verification",
            "observed": f"{model_errors} model calls across {sessions_with_error}/{sessions} sessions returned HTTP 401 authentication failure",
            "provider_error": first_error,
            "product_behavior": "API returned HTTP 200 with an empty answer event; trace run_end had status=completed and error=null; no SSE error event was emitted.",
            "not_faked": True,
            "action": "Stopped after the frozen 18 turns. Did not substitute another key, mock, mini-agent or retry-to-green.",
        },
        {
            "id": "embedding_model_path_double_nesting",
            "severity": "separate_product_configuration_finding",
            "observed": f"{embedding_failures}/{sessions} session worker logs show FileNotFoundError for /assets/models/bge-small-zh-v1.5/bge-small-zh-v1.5",
            "pdf_coverage_failure_sessions": pdf_failures,
            "note": "Seen during session seeding; it is independent of the 401 model failure. Retrieval never ran in this batch because the main model call failed first.",
            "not_fixed_this_round": True,
        },
    ]

    for case in report.get("cases") or []:
        sid = case.get("session_id")
        trace = traces.get(str(sid)) or {}
        case["trace"] = {
            "model_errors": sum(1 for event in trace.get("model_events") or [] if event.get("error")),
            "model_attempts": sum(int(run.get("model_calls") or 0) for run in trace.get("run_end") or []),
            "tool_calls": sum(int(run.get("tool_calls") or 0) for run in trace.get("run_end") or []),
            "first_error": next((event.get("error") for event in trace.get("model_events") or [] if event.get("error")), None),
            "run_end": trace.get("run_end"),
            "embedding_model_path_double_nesting": bool(trace.get("worker_has_model_path_double_nesting")),
            "pdf_coverage_failure": bool(trace.get("worker_has_pdf_coverage_failure")),
        }
        case["failure_cause"] = "model_credentials_401" if case["trace"]["model_errors"] else "not_observed"
        case["semantic_status"] = "blocked_model_credentials"

    for case in transcripts.get("cases") or []:
        sid = case.get("session_id")
        trace = traces.get(str(sid)) or {}
        case["trace"] = {
            "model_errors": sum(1 for event in trace.get("model_events") or [] if event.get("error")),
            "model_attempts": sum(int(run.get("model_calls") or 0) for run in trace.get("run_end") or []),
            "tool_calls": sum(int(run.get("tool_calls") or 0) for run in trace.get("run_end") or []),
            "first_error": next((event.get("error") for event in trace.get("model_events") or [] if event.get("error")), None),
            "run_end": trace.get("run_end"),
            "embedding_model_path_double_nesting": bool(trace.get("worker_has_model_path_double_nesting")),
            "pdf_coverage_failure": bool(trace.get("worker_has_pdf_coverage_failure")),
        }
        case["failure_cause"] = "model_credentials_401" if case["trace"]["model_errors"] else "not_observed"
        for turn in case.get("turns") or []:
            turn["model_error_401"] = bool(case["trace"]["model_errors"])

    report["status"] = "blocked_model_credentials_401"
    report["blockers"] = blockers
    report["program_checks_interpretation"] = (
        "All 12 cases failed the empty-answer / retrieval / citation checks because every model call returned 401. "
        "Those are real product-path observations, but they are not a semantic answer-quality verdict."
    )
    report["model_calls_attempted"] = model_attempts
    report["model_calls_succeeded"] = 0
    report["model_errors_401"] = model_errors
    report["sessions_with_model_error"] = sessions_with_error
    report["tool_calls_observed"] = tool_calls
    report["embedding_double_nesting_sessions"] = embedding_failures
    report["pdf_coverage_failure_sessions"] = pdf_failures
    report["trace_summary_file"] = "outputs/three-mainlines-v1/trace-summary.json"
    report["semantic_advisory"] = {
        "status": "blocked_model_credentials",
        "verdict": "unassessed",
        "file": "semantic-findings.json",
        "authoritative_judge": False,
        "note": "No full model answer exists to inspect: all 18 turns returned empty answers after provider 401. No advisory semantic verdict is invented.",
    }
    report["not_verified_cases"] = [case.get("id") for case in report.get("cases") or []]
    report["not_run_cases"] = []
    report["credential_handling"] = {
        "env_file_used": ".env.docker",
        "access_token_read_in_process_for_bearer": True,
        "access_token_value_logged": False,
        "deepseek_key_injected_by_compose": True,
        "deepseek_key_value_logged": False,
    }
    report["cleanup"]["existing_18080_untouched"] = report["cleanup"].get("existing_18080_open_after") is True

    transcripts["status"] = report["status"]
    transcripts["blockers"] = blockers
    write(OUT / "report.json", report)
    write(OUT / "transcripts.json", transcripts)
    write(
        OUT / "semantic-findings.json",
        {
            "schema_version": "three-mainlines-semantic-advisory/v1",
            "status": "blocked_model_credentials",
            "authoritative_judge": False,
            "verdict": "unassessed",
            "reason": "18/18 turns returned empty answers after provider 401; there is no answer text to review semantically.",
            "cases": {case.get("id"): "unassessed_no_answer" for case in report.get("cases") or []},
        },
    )
    print(json.dumps({
        "status": report["status"],
        "model_calls_attempted": model_attempts,
        "model_errors_401": model_errors,
        "tool_calls": tool_calls,
        "embedding_failures": embedding_failures,
        "pdf_failures": pdf_failures,
        "not_verified_cases": len(report["not_verified_cases"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
