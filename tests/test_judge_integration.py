# -*- coding: utf-8 -*-
"""Contract tests for the read-only DSH judge adapter, API and Inspect entry.

All judge artifacts here are temporary synthetic results.  They verify the
contract and the UI/Inspect wiring; they are *not* a real DSH 6-case run.
Nothing writes the production dataset, reviews store, runs, assets or runtime.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import judge_adapter  # noqa: E402
from eval.judge_adapter import JudgeContractError, JudgeRepo  # noqa: E402
from eval.review_server import case_hash as server_case_hash  # noqa: E402
from eval.review_server import create_app  # noqa: E402

BASE_URL = "http://127.0.0.1:8952"
HEADERS = {
    "Origin": BASE_URL,
    "X-Requested-With": "manual-review",
    "Content-Type": "application/json",
}
DEV_CASE = "rsv1-dev-boundary-unknown-model-price-01"
RUN_A = "v1-live-regrade-development-20261113-033753-69b438"
RUN_B = "v1-live-development-20261113-032228-2f644a"
DATASET_FILES = ("suite.json", "cases.jsonl", "split.json", "audit.json", "evidence_index.json")


def load_cases() -> dict[str, dict]:
    lines = (ROOT / "eval" / "datasets" / "v1" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    return {case["id"]: case for line in lines if line.strip() for case in [json.loads(line)]}


CASES = load_cases()
CASE_HASHES = {case_id: judge_adapter.case_definition_hash(case) for case_id, case in CASES.items()}


def dataset_digest() -> dict[str, str]:
    return {
        name: hashlib.sha256((ROOT / "eval" / "datasets" / "v1" / name).read_bytes()).hexdigest()
        for name in DATASET_FILES
    }


def make_payload(
    case_id: str,
    *,
    judge_run_id: str,
    run_id: str = RUN_A,
    case_hash: str | None = None,
    status: str = "completed",
    verdict: str = "passed",
    roles: list[dict] | None = None,
    created_at: str = "2026-11-14T12:00:00+08:00",
    provenance: dict | None = None,
) -> dict:
    if roles is None:
        roles = [
            {
                "role": "semantic_support",
                "verdict": verdict,
                "findings": [
                    {
                        "kind": "agent",
                        "severity": "low",
                        "summary": f"角色 {verdict} 的合成证据",
                        "evidence_refs": [
                            {"id": "fault-troubleshooting#1", "quote": "先清理充电触点，再尝试回充。"}
                        ],
                        "suggested_layer": "validation",
                    }
                ],
                "reason": "合成契约测试，不代表真实 DSH 结果",
            }
        ]
    return {
        "schema_version": "dsh-judge/v1",
        "case_id": case_id,
        "run_id": run_id,
        "case_hash": case_hash or CASE_HASHES[case_id],
        "input_hash": "input-" + hashlib.sha256(f"{case_id}|{run_id}".encode()).hexdigest(),
        "judge_run_id": judge_run_id,
        "status": status,
        "verdict": verdict,
        "roles": roles,
        "arbitration": {"required": False, "reason": "合成角色一致"},
        "aggregation": {
            "source": "deterministic_issue_aggregation",
            "case_verdict": verdict,
            "coverage": {
                "complete": True,
                "missing_issue_ids": [],
                "unbound_issue_ids": [],
                "uncertain_issue_ids": [],
                "unresolved_issue_ids": [],
                "issues_total": 1,
                "issues_covered": 1,
            },
            "counts": {"issues_total": 1, "issues_judged": 1},
            "disputed_issue_ids": [],
            "unresolved_issue_ids": [],
            "cross_focus_verdict_mismatch_is_not_dispute": True,
            "issues": [],
        },
        "limitations": ["合成契约测试；真实 DSH 6 case 由另一任务运行"],
        "provenance": provenance
        or {
            "dsh_version": "synthetic-test",
            "model": "none",
            "prompt_version": "judge-prompt/v2",
            "workflow_version": "dsh-judge-workflow/v2",
        },
        "created_at": created_at,
        "finished_at": created_at,
    }


def write_result(root: Path, judge_run_id: str, case_id: str, **overrides) -> Path:
    payload = make_payload(case_id, judge_run_id=judge_run_id, **overrides)
    run_dir = root / judge_run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{case_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def judge_client(tmp_path):
    root = tmp_path / "judge_results"
    root.mkdir()
    store = tmp_path / "reviews"
    app = create_app(store_dir=store, judge_results_dir=root)
    with TestClient(app, base_url=BASE_URL) as client:
        yield client, root, store


def judge_for(client: TestClient, run_id: str, case_id: str = DEV_CASE) -> dict:
    response = client.get(f"/api/cases/{case_id}/judge", params={"runId": run_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_case_hash_matches_server_algorithm():
    assert CASE_HASHES[DEV_CASE] == server_case_hash(CASES[DEV_CASE])


def test_binds_case_and_run_and_marks_historical(judge_client):
    client, root, _ = judge_client
    assert judge_for(client, RUN_A)["available"] is False
    assert judge_for(client, RUN_A)["message"] == "该 case + run 暂无 Judge 结果。"

    write_result(root, "judge_run_a_current", DEV_CASE, run_id=RUN_A, verdict="passed")
    write_result(root, "judge_run_b_current", DEV_CASE, run_id=RUN_B, verdict="failed")
    write_result(
        root,
        "judge_run_old_hash",
        DEV_CASE,
        run_id=RUN_A,
        case_hash="0" * 64,
        verdict="passed",
    )

    run_a = judge_for(client, RUN_A)
    assert run_a["available"] is True
    assert run_a["selected"]["judgeRunId"] == "judge_run_a_current"
    assert run_a["selected"]["runId"] == RUN_A
    assert run_a["selected"]["scorePolicy"]["included"] is True
    assert len(run_a["historical"]) == 1
    assert run_a["historical"][0]["caseHash"] == "0" * 64

    run_b = judge_for(client, RUN_B)
    assert run_b["selected"]["judgeRunId"] == "judge_run_b_current"
    assert run_b["selected"]["verdict"] == "failed"

    # A result for another run is never silently reused for the selected run.
    write_result(root, "judge_run_only_b", DEV_CASE, run_id=RUN_B)
    only_b = JudgeRepo(root).resolve_for_case(DEV_CASE, "run-does-not-exist", CASE_HASHES[DEV_CASE])
    assert only_b["available"] is False
    assert only_b["selected"] is None
    assert only_b["message"].startswith("存在 Judge 结果")


def test_newest_timestamp_wins_not_filesystem_order(tmp_path):
    root = tmp_path / "judge_results"
    write_result(root, "zzz_old", DEV_CASE, run_id=RUN_A, created_at="2026-01-01T00:00:00+08:00")
    write_result(root, "aaa_new", DEV_CASE, run_id=RUN_A, created_at="2026-06-01T00:00:00+08:00")
    resolved = JudgeRepo(root).resolve_for_case(DEV_CASE, RUN_A, CASE_HASHES[DEV_CASE])
    assert resolved["selected"]["judgeRunId"] == "aaa_new"
    assert resolved["selected"]["selection"]["candidateCount"] == 2


def test_error_partial_uncertain_and_missing_evidence_never_pass(judge_client):
    client, root, _ = judge_client
    cases = [
        ("judge_error", {"status": "error", "verdict": "passed"}),
        ("judge_partial", {"status": "partial", "verdict": "passed"}),
        ("judge_uncertain", {"status": "completed", "verdict": "uncertain"}),
        ("judge_not_applicable", {"status": "completed", "verdict": "not_applicable"}),
    ]
    expected = {
        "judge_error": "status_error",
        "judge_partial": "status_partial",
        "judge_uncertain": "verdict_uncertain",
        "judge_not_applicable": "verdict_not_applicable",
    }
    for judge_run_id, overrides in cases:
        write_result(root, judge_run_id, DEV_CASE, run_id=RUN_A, **overrides)
        # Each API call resolves newest-first; use the direct artifact endpoint
        # to assert the policy of each individual synthetic result.
        response = client.get(f"/api/judge/results/{judge_run_id}/{DEV_CASE}")
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        policy = judge_adapter.score_policy(
            result,
            current_case_hash=CASE_HASHES[DEV_CASE],
            run_id=RUN_A,
            historical=False,
        )
        assert policy["included"] is False
        assert policy["reason"] == expected[judge_run_id], policy
        assert policy["reason"] != "passed"

    # A completed pass without any quoted evidence is excluded, not scored.
    no_evidence_roles = [
        {
            "role": "semantic_support",
            "verdict": "passed",
            "findings": [
                {"kind": "agent", "severity": "low", "summary": "无原文引用", "evidence_refs": []}
            ],
            "reason": "合成缺证据",
        }
    ]
    write_result(root, "judge_missing_evidence", DEV_CASE, run_id=RUN_A, roles=no_evidence_roles)
    result = client.get(f"/api/judge/results/judge_missing_evidence/{DEV_CASE}").json()["result"]
    policy = judge_adapter.score_policy(
        result, current_case_hash=CASE_HASHES[DEV_CASE], run_id=RUN_A, historical=False
    )
    assert policy["included"] is False
    assert policy["reason"] == "missing_evidence"


def test_malformed_and_path_traversal_are_rejected(judge_client):
    client, root, _ = judge_client
    malformed_dir = root / "judge_malformed"
    malformed_dir.mkdir()
    (malformed_dir / f"{DEV_CASE}.json").write_text("{not valid json", encoding="utf-8")

    resolved = judge_for(client, RUN_A)
    assert resolved["available"] is False
    assert resolved["errors"]
    assert resolved["errors"][0]["code"] == "invalid_json"

    invalid_schema = make_payload(DEV_CASE, judge_run_id="judge_bad_schema", run_id=RUN_A)
    invalid_schema["schema_version"] = "dsh-judge/v0"
    bad_dir = root / "judge_bad_schema"
    bad_dir.mkdir()
    (bad_dir / f"{DEV_CASE}.json").write_text(json.dumps(invalid_schema), encoding="utf-8")
    errors = judge_for(client, RUN_A)["errors"]
    assert any(item["code"] == "unsupported_schema_version" for item in errors)

    repo = JudgeRepo(root)
    for bad_run, bad_case in (
        ("..", DEV_CASE),
        ("judge_run_a", "../outside"),
        ("nested/../../escape", DEV_CASE),
    ):
        with pytest.raises(JudgeContractError):
            repo.result_for(bad_run, bad_case)

    outside = judge_client[0].get("/api/judge/results/..%2F..%2Foutside/" + DEV_CASE)
    assert outside.status_code in (400, 404, 422)
    assert outside.status_code != 200
    direct = client.get("/api/judge/results/..%2F..%2Foutside/" + DEV_CASE)
    assert direct.status_code in (400, 404, 422)


def test_read_only_api_does_not_touch_human_record_dataset_or_judge_files(judge_client):
    client, root, store = judge_client
    judge_path = write_result(root, "judge_read_only", DEV_CASE, run_id=RUN_A)
    before_dataset = dataset_digest()
    before_judge = hashlib.sha256(judge_path.read_bytes()).hexdigest()

    save = client.post(
        "/api/reviews",
        json={
            "caseId": DEV_CASE,
            "caseHash": CASE_HASHES[DEV_CASE],
            "suiteRevision": "v1.1",
            "reviewScope": "run_answer",
            "runId": RUN_A,
            "status": "uncertain",
            "notes": "用户原始备注：不要把 Judge 结果写回这里",
            "expectedRevision": 0,
        },
        headers=HEADERS,
    )
    assert save.status_code == 200, save.text
    review_files = list((store / "reviews").glob("*.json"))
    assert len(review_files) == 1
    review_before = review_files[0].read_bytes()

    payload = judge_for(client, RUN_A)
    assert payload["available"] is True
    client.get(f"/api/judge/results/judge_read_only/{DEV_CASE}")
    client.get("/api/judge/runs")
    client.get(f"/api/cases/{DEV_CASE}")

    assert review_files[0].read_bytes() == review_before
    assert dataset_digest() == before_dataset
    assert hashlib.sha256(judge_path.read_bytes()).hexdigest() == before_judge
    loaded = client.get(
        "/api/reviews", params={"case_id": DEV_CASE, "scope": "run_answer", "runId": RUN_A}
    ).json()["current"]
    assert loaded["status"] == "uncertain"
    assert loaded["notes"] == "用户原始备注：不要把 Judge 结果写回这里"
    assert loaded["runSnapshot"]["runId"] == RUN_A


def test_multi_case_multi_run_overview_and_empty_state(judge_client):
    client, root, _ = judge_client
    health = client.get("/api/health").json()
    assert health["judgeRunCount"] == 0
    assert health["canRunJudge"] is False
    assert client.get("/api/cases/" + DEV_CASE + "/judge").json()["available"] is False

    other_case = "rsv1-dev-boundary-unknown-model-price-02"
    write_result(root, "judge_multi_a", DEV_CASE, run_id=RUN_A)
    write_result(root, "judge_multi_a", other_case, run_id=RUN_A)
    write_result(root, "judge_multi_b", other_case, run_id=RUN_B, verdict="failed")
    runs = client.get("/api/judge/runs").json()
    assert runs["runCount"] == 2
    assert runs["resultCount"] == 3
    assert runs["readOnly"] is True and runs["noModel"] is True
    health = client.get("/api/health").json()
    assert health["judgeRunCount"] == 2 and health["judgeResultCount"] == 3

    assert judge_for(client, RUN_A, other_case)["selected"]["verdict"] == "passed"
    assert judge_for(client, RUN_B, other_case)["selected"]["verdict"] == "failed"


def test_optional_aggregation_and_finding_scope_fields_preserved_and_old_compatible(judge_client):
    client, root, _ = judge_client
    path = write_result(root, "judge_native_fields", DEV_CASE, run_id=RUN_A)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["roles"][0]["findings"][0].update(
        {
            "criterion_id": "turn-1-evidence-support",
            "turn_scope": 1,
            "judgment": "uncertain",
        }
    )
    payload["aggregation"] = {
        "source": "deterministic_issue_aggregation",
        "case_verdict": "failed",
        "coverage": {
            "complete": True,
            "missing_issue_ids": [],
            "unbound_issue_ids": [],
            "uncertain_issue_ids": [],
            "unresolved_issue_ids": ["turn-1-evidence-support"],
            "issues_total": 1,
            "issues_covered": 1,
        },
        "counts": {"issues_total": 1, "issues_judged": 1},
        "disputed_issue_ids": ["turn-1-evidence-support"],
        "unresolved_issue_ids": ["turn-1-evidence-support"],
        "cross_focus_verdict_mismatch_is_not_dispute": True,
        "issues": [
            {
                "criterion_id": "turn-1-evidence-support",
                "focus": "evidence_support",
                "turn_scope": 1,
                "judgments": [{"role": "evidence_reviewer", "judgment": "uncertain", "unbound": False}],
                "final_judgment": "uncertain",
                "arbitration_used": True,
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    result = client.get(f"/api/judge/results/judge_native_fields/{DEV_CASE}").json()["result"]
    finding = result["roles"][0]["findings"][0]
    assert finding["criterionId"] == "turn-1-evidence-support"
    assert finding["turnScope"] == 1
    assert finding["judgment"] == "uncertain"
    assert result["aggregation"]["source"] == "deterministic_issue_aggregation"
    assert result["aggregation"]["unresolved_issue_ids"] == ["turn-1-evidence-support"]
    assert result["aggregation"]["cross_focus_verdict_mismatch_is_not_dispute"] is True
    assert result["aggregation"]["issues"][0]["final_judgment"] == "uncertain"

    # Old v1 artifacts without the optional fields must still normalize and
    # remain visible as legacy history, but must not be scored as trusted.
    old_payload = make_payload(DEV_CASE, judge_run_id="judge_old_shape", run_id=RUN_A)
    old_payload.pop("aggregation", None)
    old_payload["provenance"] = {
        "dsh_version": "synthetic-test",
        "model": "none",
        "prompt_version": "judge-prompt/v1",
        "workflow_version": "dsh-judge-workflow/v1",
    }
    old_dir = root / "judge_old_shape"
    old_dir.mkdir()
    (old_dir / f"{DEV_CASE}.json").write_text(
        json.dumps(old_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    old = client.get(f"/api/judge/results/judge_old_shape/{DEV_CASE}").json()["result"]
    assert old["aggregation"] is None
    assert old["roles"][0]["findings"][0]["criterionId"] is None
    assert old["roles"][0]["findings"][0]["turnScope"] is None
    assert old["roles"][0]["findings"][0]["judgment"] is None
    old_policy = judge_adapter.score_policy(
        old, current_case_hash=CASE_HASHES[DEV_CASE], run_id=RUN_A, historical=False
    )
    assert old_policy["included"] is False
    assert old_policy["reason"] == "legacy_unverified"
    assert old_policy["reviewTrust"] == "legacy_unverified"


def test_inspect_judge_scorer_is_separate_and_denominator_aware(tmp_path):
    from eval import inspect_tasks

    root = tmp_path / "judge_results"
    cases = [
        CASES["rsv1-dev-boundary-unknown-model-price-01"],
        CASES["rsv1-dev-boundary-unknown-model-price-02"],
        CASES["rsv1-dev-direct-suction-decline-02"],
        CASES["rsv1-dev-negation-filter-not-cleaned-01"],
    ]
    ids = [case["id"] for case in cases]
    write_result(root, "judge_inspect_pass", ids[0], run_id="inspect-run")
    write_result(root, "judge_inspect_error", ids[1], run_id="inspect-run", status="error", verdict="passed")
    write_result(root, "judge_inspect_uncertain", ids[2], run_id="inspect-run", verdict="uncertain")
    # ids[3] intentionally has no judge artifact -> pending/no_result, excluded.
    logs = inspect_tasks.run_inspect_state_smoke(
        cases,
        log_dir=tmp_path / "inspect-logs",
        judge_results_root=root,
        run_id="inspect-run",
    )
    assert logs and logs[0].status == "success"
    samples = {sample.id: sample for sample in logs[0].samples}
    pass_scores = samples[ids[0]].scores
    error_scores = samples[ids[1]].scores
    uncertain_scores = samples[ids[2]].scores
    missing_scores = samples[ids[3]].scores
    assert "robot_support_v1_structural" in pass_scores
    assert "robot_support_v1_judge_semantic" in pass_scores
    judge_pass = pass_scores["robot_support_v1_judge_semantic"]
    judge_error = error_scores["robot_support_v1_judge_semantic"]
    judge_uncertain = uncertain_scores["robot_support_v1_judge_semantic"]
    judge_missing = missing_scores["robot_support_v1_judge_semantic"]
    assert judge_pass.value["score"] == 1.0
    assert judge_pass.metadata["denominator"] == "included"
    assert judge_error.value["score"] is None
    assert judge_error.metadata["denominator_reason"] == "status_error"
    assert judge_uncertain.value["score"] is None
    assert judge_uncertain.metadata["denominator_reason"] == "verdict_uncertain"
    assert judge_missing.value["score"] is None
    assert judge_missing.metadata["denominator_reason"] == "no_result"
    # Structural scorer keeps its own value and never absorbs the judge verdict.
    assert pass_scores["robot_support_v1_structural"].metadata["score_kind"] == "program_checks"
    assert pass_scores["robot_support_v1_structural"].value in (0.0, 1.0)

def test_run_binding_unresolved_and_evaluation_only_are_excluded(judge_client):
    client, root, _ = judge_client
    # Unresolved arbitration: displayed but not a trusted final score.
    unresolved = [
        {
            "role": "task_reviewer",
            "verdict": "failed",
            "findings": [
                {
                    "kind": "agent",
                    "judgment": "failed",
                    "criterion_id": "case-task-completion",
                    "severity": "medium",
                    "summary": "合成未决 case",
                    "evidence_refs": [{"id": "ev-1", "quote": "合成证据"}],
                }
            ],
        }
    ]
    path = write_result(root, "judge_unresolved", DEV_CASE, run_id=RUN_A, verdict="failed", roles=unresolved)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["aggregation"] = {
        "source": "deterministic_issue_aggregation",
        "case_verdict": "failed",
        "coverage": {
            "complete": True,
            "missing_issue_ids": [],
            "unbound_issue_ids": [],
            "uncertain_issue_ids": [],
            "unresolved_issue_ids": ["case-evidence-support"],
        },
        "counts": {"issues_total": 1, "issues_judged": 1},
        "disputed_issue_ids": ["case-evidence-support"],
        "unresolved_issue_ids": ["case-evidence-support"],
        "issues": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    resolved = judge_for(client, RUN_A)
    assert resolved["available"] is True
    selected = resolved["selected"]
    assert selected["scorePolicy"]["included"] is False
    assert selected["scorePolicy"]["reason"] == "unresolved_arbitration"
    assert selected["scorePolicy"]["reviewTrust"] == "provisional_unresolved"
    assert selected["aggregation"]["unresolved_issue_ids"] == ["case-evidence-support"]

    # A result may not be scored without an explicit matching run id.
    result = judge_adapter.normalize_result(
        payload, judge_run_id="judge_unresolved", case_id=DEV_CASE
    )
    policy = judge_adapter.score_policy(
        result, current_case_hash=CASE_HASHES[DEV_CASE], run_id=None, historical=False
    )
    assert policy["included"] is False
    assert policy["reason"] == "run_mismatch"

    # failed with only evaluation findings is an evaluator problem, not an
    # agent failure; it must not enter the agent denominator.
    evaluation_only = [
        {
            "role": "task_reviewer",
            "verdict": "failed",
            "findings": [
                {
                    "kind": "evaluation",
                    "judgment": "failed",
                    "criterion_id": "turn-1-required-claim-1",
                    "severity": "medium",
                    "summary": "rubric 不适用",
                    "evidence_refs": [{"id": "ev-rubric", "quote": "合成 rubric 原文"}],
                }
            ],
        }
    ]
    write_result(root, "judge_eval_only", DEV_CASE, run_id=RUN_A, verdict="failed", roles=evaluation_only)
    result = client.get(f"/api/judge/results/judge_eval_only/{DEV_CASE}").json()["result"]
    policy = judge_adapter.score_policy(
        result, current_case_hash=CASE_HASHES[DEV_CASE], run_id=RUN_A, historical=False
    )
    assert policy["included"] is False
    assert policy["reason"] == "failed_evaluation_only"
    assert policy["reviewTrust"] == "evaluation_only"


def test_newest_uses_provenance_completed_at_when_top_level_timestamps_absent(tmp_path):
    root = tmp_path / "judge_results"
    for judge_run_id, completed_at in (
        ("zzz_older", "2026-01-01T00:00:00Z"),
        ("aaa_newer", "2026-06-01T00:00:00Z"),
    ):
        payload = make_payload(DEV_CASE, judge_run_id=judge_run_id, run_id=RUN_A)
        payload.pop("created_at", None)
        payload.pop("finished_at", None)
        payload["provenance"] = {
            "dsh_version": "synthetic-test",
            "completed_at": completed_at,
        }
        run_dir = root / judge_run_id
        run_dir.mkdir(parents=True)
        (run_dir / f"{DEV_CASE}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    resolved = JudgeRepo(root).resolve_for_case(DEV_CASE, RUN_A, CASE_HASHES[DEV_CASE])
    # Lexicographic judge_run_id order would pick zzz_older; provenance time wins.
    assert resolved["selected"]["judgeRunId"] == "aaa_newer"


def test_quote_trust_verification_flags_fabricated_quotes(judge_client):
    client, root, _ = judge_client
    roles = [
        {
            "role": "semantic_support",
            "verdict": "failed",
            "findings": [
                {
                    "kind": "agent",
                    "judgment": "failed",
                    "summary": "引用了不存在于 run 的原文",
                    "evidence_refs": [
                        {"id": "ev-real", "quote": CASES[DEV_CASE]["title"]},
                        {"id": "ev-fake", "quote": "这句话不在 dataset 也不在 run 里"},
                    ],
                }
            ],
        }
    ]
    write_result(root, "judge_quote_trust", DEV_CASE, run_id=RUN_A, verdict="failed", roles=roles)
    resolved = judge_for(client, RUN_A)
    trust = resolved["selected"]["evidenceTrust"]
    assert trust["checked"] == 2
    assert trust["verified"] == 1
    assert trust["unverifiedIds"] == ["ev-fake"]


def test_inspect_judge_scorer_uses_actual_run_answer(tmp_path):
    from eval import inspect_tasks

    root = tmp_path / "judge_results"
    case = CASES["rsv1-dev-boundary-unknown-model-price-01"]
    write_result(root, "judge_actual_answer", case["id"], run_id="inspect-run")
    actual = "这是实际 live 回答，不是 state solver 的 JSON。"
    logs = inspect_tasks.run_inspect_state_smoke(
        [case],
        log_dir=tmp_path / "inspect-logs",
        judge_results_root=root,
        run_id="inspect-run",
        run_answers={case["id"]: actual},
    )
    assert logs and logs[0].status == "success"
    score = logs[0].samples[0].scores["robot_support_v1_judge_semantic"]
    assert score.answer == actual
    assert score.metadata["answer_source"] == "run_answer"
    assert score.metadata["actual_answer_available"] is True

def test_v2_missing_coverage_and_v1_are_excluded(judge_client):
    client, root, _ = judge_client
    # v2 without aggregation.coverage: version alone is not trusted.
    path = write_result(root, "judge_v2_no_coverage", DEV_CASE, run_id=RUN_A)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["aggregation"].pop("coverage", None)
    payload["provenance"]["workflow_version"] = "dsh-judge-workflow/v2"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    v2 = client.get(f"/api/judge/results/judge_v2_no_coverage/{DEV_CASE}").json()["result"]
    policy = judge_adapter.score_policy(
        v2, current_case_hash=CASE_HASHES[DEV_CASE], run_id=RUN_A, historical=False
    )
    assert policy["included"] is False
    assert policy["reason"] == "coverage_missing"
    assert policy["reviewTrust"] == "coverage_missing"

    # v1 artifact: legacy history, even if it happens to carry a coverage block.
    old_path = write_result(root, "judge_v1_legacy", DEV_CASE, run_id=RUN_A)
    old_payload = json.loads(old_path.read_text(encoding="utf-8"))
    old_payload["provenance"]["workflow_version"] = "dsh-judge-workflow/v1"
    old_path.write_text(json.dumps(old_payload, ensure_ascii=False), encoding="utf-8")
    old = client.get(f"/api/judge/results/judge_v1_legacy/{DEV_CASE}").json()["result"]
    old_policy = judge_adapter.score_policy(
        old, current_case_hash=CASE_HASHES[DEV_CASE], run_id=RUN_A, historical=False
    )
    assert old_policy["included"] is False
    assert old_policy["reason"] == "legacy_unverified"
    assert old_policy["legacyUnverified"] is True


def test_v2_complete_coverage_included_and_incomplete_preserved(judge_client):
    client, root, _ = judge_client
    complete = write_result(root, "judge_v2_complete", DEV_CASE, run_id=RUN_A)
    complete_result = client.get(f"/api/judge/results/judge_v2_complete/{DEV_CASE}").json()["result"]
    complete_policy = judge_adapter.score_policy(
        complete_result, current_case_hash=CASE_HASHES[DEV_CASE], run_id=RUN_A, historical=False
    )
    assert complete_policy["included"] is True
    assert complete_result["coverageComplete"] is True
    assert complete_result["aggregationCounts"]["issues_total"] == 1

    incomplete = write_result(root, "judge_v2_incomplete", DEV_CASE, run_id=RUN_A)
    payload = json.loads(incomplete.read_text(encoding="utf-8"))
    payload["aggregation"]["coverage"].update(
        {
            "complete": False,
            "missing_issue_ids": ["turn-2-evidence-support"],
            "unbound_issue_ids": ["turn-2-required-claim-1"],
            "uncertain_issue_ids": ["case-task-completion"],
            "unresolved_issue_ids": ["case-evidence-support"],
        }
    )
    incomplete.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = client.get(f"/api/judge/results/judge_v2_incomplete/{DEV_CASE}").json()["result"]
    policy = judge_adapter.score_policy(
        result, current_case_hash=CASE_HASHES[DEV_CASE], run_id=RUN_A, historical=False
    )
    assert policy["included"] is False
    assert policy["reason"] == "coverage_incomplete"
    assert result["coverageMissingIssueIds"] == ["turn-2-evidence-support"]
    assert result["coverageUnboundIssueIds"] == ["turn-2-required-claim-1"]
    assert result["coverageUncertainIssueIds"] == ["case-task-completion"]
    assert result["coverageUnresolvedIssueIds"] == ["case-evidence-support"]


def test_system_errors_excluded_and_preserved(judge_client):
    client, root, _ = judge_client
    path = write_result(root, "judge_system_errors", DEV_CASE, run_id=RUN_A, verdict="passed")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["system_errors"] = [{"role": "task_reviewer", "error": "timeout"}]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = client.get(f"/api/judge/results/judge_system_errors/{DEV_CASE}").json()["result"]
    assert result["systemErrors"] == [{"role": "task_reviewer", "error": "timeout"}]
    policy = judge_adapter.score_policy(
        result, current_case_hash=CASE_HASHES[DEV_CASE], run_id=RUN_A, historical=False
    )
    assert policy["included"] is False
    assert policy["reason"] == "system_errors"
