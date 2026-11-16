# -*- coding: utf-8 -*-
"""Read-only API acceptance for the isolated 8955 review server.

The server is started hidden by the caller; this script only performs GETs. It
checks the two actual answer rows, the bound Judge result, complete coverage,
current case hashes and a still-empty human-review store.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "action-state-answer-review-v1"
RUN_ID = "asc-live-development-20261114-cdf5aac89d"
JUDGE_RUN_ID = "jr-action-review-20261114-01"
BASE = "http://127.0.0.1:8955"
REPORT = ROOT / "outputs" / "action-state-answer-review-gui-readonly.json"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=20) as response:
        return json.load(response)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    freeze = read_json(OUT / "freeze.json")
    rows = {row["case_id"]: row for row in read_jsonl(OUT / "results" / RUN_ID / "cases.jsonl")}
    health = get("/api/health")
    meta = get("/api/meta")
    listing = get("/api/cases?split=development&scope=run_answer")
    judge_runs = get("/api/judge/runs")
    cases = []
    for case_id in ("asc-dev-contact-correction-01", "asc-dev-dock-correction-01"):
        detail = get(f"/api/cases/{case_id}?runId={RUN_ID}")
        run = get(f"/api/cases/{case_id}/run?runId={RUN_ID}")
        judge = get(f"/api/cases/{case_id}/judge?runId={RUN_ID}")
        expected_answers = [turn["answer"] for turn in rows[case_id]["turns"]]
        actual_answers = [turn["answer"] for turn in run["turns"]]
        selected = judge.get("selected") or {}
        assert detail["caseHash"] == freeze["case_hashes"][case_id]
        assert detail["selectedRunId"] == RUN_ID
        assert actual_answers == expected_answers
        assert run["hasAnyAnswer"] is True and run["rowStatus"] == "completed"
        assert selected.get("judgeRunId") == JUDGE_RUN_ID
        assert selected.get("verdict") in ("passed", "failed", "uncertain")
        assert selected.get("coverageComplete") is True
        assert (selected.get("coverage") or {}).get("missing") == 0
        assert selected.get("caseHash") == freeze["case_hashes"][case_id]
        assert selected.get("runId") == RUN_ID
        assert judge.get("available") is True
        cases.append({
            "case_id": case_id,
            "case_hash": detail["caseHash"],
            "selected_run_id": detail["selectedRunId"],
            "actual_answers_equal_projection": actual_answers == expected_answers,
            "turn_count": len(run["turns"]),
            "trace_ids": [turn.get("traceId") for turn in run["turns"]],
            "judge": {
                "available": judge.get("available"),
                "judge_run_id": selected.get("judgeRunId"),
                "verdict": selected.get("verdict"),
                "status": selected.get("status"),
                "coverage": selected.get("coverage"),
                "coverage_complete": selected.get("coverageComplete"),
                "unresolved_issue_ids": selected.get("unresolvedIssueIds"),
                "score_policy": selected.get("scorePolicy"),
                "case_hash_matches_current": selected.get("caseHash") == detail["caseHash"],
            },
            "actual_answers": actual_answers,
        })
    review_records = []
    for path in (OUT / "reviews").rglob("*.json"):
        try:
            payload = read_json(path)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("reviewId"):
            review_records.append(path)
    report = {
        "schema_version": "action-state-answer-review-gui-readonly/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "base_url": BASE,
        "read_only": True,
        "health": health,
        "meta": {
            "suite": meta.get("suite"),
            "store": meta.get("store"),
            "boundaries": meta.get("boundaries"),
        },
        "case_listing": {
            "total_before_filter": listing.get("totalBeforeFilter"),
            "filtered_total": listing.get("filteredTotal"),
            "rows": [
                {
                    "case_id": row.get("caseId"),
                    "case_hash": row.get("caseHash"),
                    "default_run_id": row.get("defaultRunId"),
                    "has_runs": row.get("hasRuns"),
                    "review": row.get("review"),
                }
                for row in listing.get("rows", [])
            ],
        },
        "judge_runs": judge_runs,
        "cases": cases,
        "review_records": [str(path.relative_to(OUT)) for path in review_records],
        "human_review_count": int(health.get("reviewCount") or 0),
        "checks": {
            "health_ok": health.get("ok") is True,
            "review_count_zero": health.get("reviewCount") == 0,
            "judge_result_count_two": health.get("judgeResultCount") == 2,
            "two_cases_listed": len(listing.get("rows", [])) == 2,
            "both_actual_answers_present": all(item["actual_answers_equal_projection"] for item in cases),
            "both_have_selected_judge": all(item["judge"]["available"] for item in cases),
            "both_coverage_complete": all(item["judge"]["coverage_complete"] for item in cases),
            "both_case_hashes_current": all(item["judge"]["case_hash_matches_current"] for item in cases),
            "no_human_review_records": not review_records and int(health.get("reviewCount") or 0) == 0,
        },
        "note": "GET-only acceptance; the server does not run the product or the Judge. Machine Judge output is not human review.",
    }
    assert all(report["checks"].values()), report["checks"]
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(REPORT),
        "checks": report["checks"],
        "case_verdicts": {item["case_id"]: item["judge"]["verdict"] for item in cases},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
