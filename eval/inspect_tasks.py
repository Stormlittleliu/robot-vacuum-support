# -*- coding: utf-8 -*-
"""Inspect AI adapter for the formal robot-support eval v1.

This is a thin adapter, not a second evaluation platform:

* one Inspect ``Sample`` per v1 case;
* a custom deterministic solver replays the same pure state contract used by
  ``eval/runner.py run --mode state``;
* a custom ``Scorer`` converts the separated structural checks into an Inspect
  ``Score``.

Semantic rubrics stay ``pending_human`` by default.  When a read-only
``dsh-judge/v1`` result is supplied, a *second and separate* scorer exposes the
completed judge verdict.  Program checks are never folded into that judge score,
and excluded states (uncertain / error / partial / not_applicable / missing
evidence / historical hash / no result) carry explicit denominator metadata
instead of being counted as passes.

Live mode is intentionally run by ``runner.py`` because it needs a source-built
artifact, a real instance, a temporary port and a human review export.  The
Inspect task here is for framework/scorer verification and deterministic state
runs, not a mock substitute for the real product.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime"))

from inspect_ai import Task, eval as inspect_eval  # noqa: E402
from inspect_ai.dataset import Sample  # noqa: E402
from inspect_ai.scorer import Score as InspectScore  # noqa: E402
from inspect_ai.scorer import Target, accuracy, scorer  # noqa: E402
from inspect_ai.solver import Generate, TaskState, solver  # noqa: E402

from eval.judge_adapter import (  # noqa: E402
    SCHEMA_VERSION as JUDGE_SCHEMA_VERSION,
    JudgeContractError,
    JudgeRepo,
    case_definition_hash,
    score_policy,
)
from eval.runner import run_state_case  # noqa: E402

_CASE_REGISTRY: dict[str, dict[str, Any]] = {}


def _first_user_input(case: dict[str, Any]) -> str:
    for turn in case.get("turns") or []:
        if turn.get("role") == "user":
            return str(turn.get("content") or "")
    return ""


def _judge_semantic_meta(
    case: dict[str, Any],
    *,
    repo: JudgeRepo | None,
    run_id: str | None,
    judge_run_id: str | None,
) -> dict[str, Any]:
    """Resolve one read-only judge result into Inspect metadata.

    Matching is by run id + current case-definition hash.  No result, malformed
    JSON, a different run, or a historical hash all become explicit excluded
    metadata; none of them becomes a semantic pass.
    """
    case_id = str(case.get("id"))
    current_hash = case_definition_hash(case)
    base: dict[str, Any] = {
        "schemaVersion": JUDGE_SCHEMA_VERSION,
        "caseId": case_id,
        "runId": run_id,
        "judgeRunId": None,
        "status": None,
        "verdict": None,
        "denominator": "excluded",
        "denominatorReason": "no_result",
        "denominatorReasonLabel": "暂无 Judge 结果（pending）",
        "evidenceCount": 0,
        "historical": False,
        "selection": None,
        "roles": [],
        "arbitration": None,
        "aggregation": None,
        "aggregationCounts": None,
        "coverage": None,
        "coveragePresent": False,
        "coverageComplete": False,
        "coverageMissingIssueIds": [],
        "coverageUnboundIssueIds": [],
        "coverageUncertainIssueIds": [],
        "coverageUnresolvedIssueIds": [],
        "workflowVersion": None,
        "legacyUnverified": False,
        "systemErrors": [],
        "unresolvedIssueIds": [],
        "failureBasis": {},
        "aggregationMismatch": False,
        "scorePolicy": None,
        "limitations": [],
        "provenance": {},
        "errors": [],
        "programChecksSeparate": True,
        "semanticReview": "pending_human",
    }
    if repo is None:
        base["denominatorReason"] = "judge_not_configured"
        base["denominatorReasonLabel"] = "未配置 Judge 结果目录"
        return base
    selected: dict[str, Any] | None = None
    error_message: str | None = None
    if judge_run_id:
        try:
            result = repo.result_for(judge_run_id, case_id)
        except JudgeContractError as error:
            result = None
            error_message = error.message
            base["errors"] = [f"{error.code}: {error.message}"]
        if result is not None:
            historical = result["caseHash"] != current_hash
            policy = score_policy(
                result,
                current_case_hash=current_hash,
                run_id=run_id,
                historical=historical,
            )
            result["historical"] = historical
            result["scorePolicy"] = policy
            if policy["included"]:
                selected = result
            else:
                base.update(
                    {
                        "judgeRunId": judge_run_id,
                        "status": result["status"],
                        "verdict": result["verdict"],
                        "denominator": policy["denominator"],
                        "denominatorReason": policy["reason"],
                        "denominatorReasonLabel": policy["reasonLabel"],
                        "evidenceCount": result["evidenceCount"],
                        "historical": historical,
                        "roles": result["roles"],
                        "arbitration": result["arbitration"],
                        "aggregation": result.get("aggregation"),
                        "aggregationCounts": result.get("aggregationCounts"),
                        "coverage": result.get("coverage"),
                        "coveragePresent": bool(result.get("coveragePresent")),
                        "coverageComplete": bool(result.get("coverageComplete")),
                        "coverageMissingIssueIds": result.get("coverageMissingIssueIds") or [],
                        "coverageUnboundIssueIds": result.get("coverageUnboundIssueIds") or [],
                        "coverageUncertainIssueIds": result.get("coverageUncertainIssueIds") or [],
                        "coverageUnresolvedIssueIds": result.get("coverageUnresolvedIssueIds") or [],
                        "workflowVersion": result.get("workflowVersion"),
                        "legacyUnverified": bool(result.get("legacyUnverified")),
                        "systemErrors": result.get("systemErrors") or [],
                        "unresolvedIssueIds": result.get("unresolvedIssueIds") or [],
                        "failureBasis": result.get("failureBasis") or {},
                        "aggregationMismatch": bool(result.get("aggregationMismatch")),
                        "scorePolicy": policy,
                        "limitations": result["limitations"],
                        "provenance": result["provenance"],
                        "errors": result["errors"],
                        "semanticReview": "judge_excluded",
                    }
                )
                return base
    else:
        resolution = repo.resolve_for_case(case_id, run_id, current_hash)
        selected = resolution["selected"]
        base["selection"] = {
            "available": resolution["available"],
            "message": resolution["message"],
            "candidateCount": len(resolution["results"]),
            "historicalCount": len(resolution["historical"]),
            "errorCount": len(resolution["errors"]),
        }
        if resolution["errors"]:
            base["errors"] = [
                f"{item['judgeRunId']}: {item['code']}: {item['message']}"
                for item in resolution["errors"]
            ]
            base["denominatorReason"] = "malformed_result"
            base["denominatorReasonLabel"] = "Judge 结果格式错误，不能评分"
        if selected is None:
            if resolution["historical"]:
                base["denominatorReason"] = "historical_case_hash"
                base["denominatorReasonLabel"] = "caseHash/run 不匹配当前选择，按历史快照处理"
                base["historical"] = True
            elif resolution["results"]:
                base["denominatorReason"] = "run_mismatch"
                base["denominatorReasonLabel"] = "结果绑定的 run 与当前选择不匹配"
            elif base["errors"]:
                base["denominatorReason"] = "malformed_result"
            else:
                base["denominatorReason"] = "no_result"
            return base
    if selected is None:
        if error_message:
            base["errors"] = [error_message]
            base["denominatorReason"] = "malformed_result"
            base["denominatorReasonLabel"] = "Judge 结果格式错误，不能评分"
        return base
    policy = selected.get("scorePolicy") or score_policy(
        selected,
        current_case_hash=current_hash,
        run_id=run_id,
        historical=selected.get("historical", False),
    )
    base.update(
        {
            "judgeRunId": selected["judgeRunId"],
            "status": selected["status"],
            "verdict": selected["verdict"],
            "denominator": policy["denominator"],
            "denominatorReason": policy["reason"],
            "denominatorReasonLabel": policy["reasonLabel"],
            "evidenceCount": selected["evidenceCount"],
            "historical": bool(selected.get("historical")),
            "roles": selected["roles"],
            "arbitration": selected["arbitration"],
            "aggregation": selected.get("aggregation"),
            "aggregationCounts": selected.get("aggregationCounts"),
            "coverage": selected.get("coverage"),
            "coveragePresent": bool(selected.get("coveragePresent")),
            "coverageComplete": bool(selected.get("coverageComplete")),
            "coverageMissingIssueIds": selected.get("coverageMissingIssueIds") or [],
            "coverageUnboundIssueIds": selected.get("coverageUnboundIssueIds") or [],
            "coverageUncertainIssueIds": selected.get("coverageUncertainIssueIds") or [],
            "coverageUnresolvedIssueIds": selected.get("coverageUnresolvedIssueIds") or [],
            "workflowVersion": selected.get("workflowVersion"),
            "legacyUnverified": bool(selected.get("legacyUnverified")),
            "systemErrors": selected.get("systemErrors") or [],
            "unresolvedIssueIds": selected.get("unresolvedIssueIds") or [],
            "failureBasis": selected.get("failureBasis") or {},
            "aggregationMismatch": bool(selected.get("aggregationMismatch")),
            "scorePolicy": policy,
            "limitations": selected["limitations"],
            "provenance": selected["provenance"],
            "errors": selected["errors"],
            "semanticReview": (
                "judge_completed" if policy["included"] else "judge_excluded"
            ),
        }
    )
    return base


def build_inspect_task(
    cases: list[dict[str, Any]],
    *,
    name: str = "robot_support_v1_state",
    log_dir: str | Path | None = None,
    judge_results_root: str | Path | None = None,
    judge_run_id: str | None = None,
    run_id: str | None = None,
    run_answers: dict[str, str] | None = None,
) -> Task:
    """Create a real Inspect Task for deterministic state cases.

    ``judge_results_root`` enables the separate semantic scorer.  It reads only
    ``<root>/<judge_run_id>/<case_id>.json`` and never triggers a model.
    ``run_answers`` maps ``case_id -> actual live answer`` so the judge scorer's
    ``answer`` is the text actually judged, not the state-solver JSON.
    """
    judge_repo = JudgeRepo(judge_results_root) if judge_results_root is not None else None
    answers = dict(run_answers or {})
    samples: list[Sample] = []
    for case in cases:
        case_id = str(case.get("id"))
        _CASE_REGISTRY[case_id] = case
        judge_meta = _judge_semantic_meta(
            case,
            repo=judge_repo,
            run_id=run_id,
            judge_run_id=judge_run_id,
        )
        actual_answer = answers.get(case_id)
        samples.append(
            Sample(
                id=case_id,
                input=_first_user_input(case),
                target=case_id,
                metadata={
                    "case_id": case_id,
                    "family_id": case.get("family_id"),
                    "split": case.get("split"),
                    "turn_count": len(case.get("turns") or []),
                    "actual_answer": actual_answer,
                    "actual_answer_available": actual_answer is not None,
                    "judge_semantic": judge_meta if judge_repo is not None else None,
                },
            )
        )

    @solver(name="robot_support_state_solver")
    def _solver():
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            case_id = str(state.sample_id)
            case = _CASE_REGISTRY.get(case_id)
            if case is None:
                raise RuntimeError(f"case not registered: {case_id}")
            row = run_state_case(case)
            state.metadata["eval_row"] = row
            state.metadata["case_id"] = case_id
            # Program checks stay in their own field; the judge verdict is a
            # separate semantic signal and never rewrites structural status.
            state.metadata["program_checks"] = row.get("checks") or []
            state.output.completion = json.dumps(
                {
                    "status": row.get("status"),
                    "checks": row.get("checks"),
                    "semantic_review": row.get("semantic_review"),
                    "judge_semantic": state.metadata.get("judge_semantic"),
                },
                ensure_ascii=False,
            )
            return state

        return solve

    @scorer(metrics=[accuracy()], name="robot_support_v1_structural")
    def _scorer():
        async def score(state: TaskState, target: Target) -> InspectScore:
            row = (state.metadata or {}).get("eval_row") or {}
            checks = row.get("checks") or []
            failed = [item for item in checks if item.get("status") == "fail"]
            passed = [item for item in checks if item.get("status") == "pass"]
            status = row.get("status") or "unknown"
            value = 1.0 if status == "passed" else 0.0
            return InspectScore(
                value=value,
                answer=str(state.output.completion or "")[:4000],
                explanation=(
                    f"structural_status={status}; pass={len(passed)}; "
                    f"fail={len(failed)}; semantic review is not auto-passed"
                ),
                metadata={
                    "case_id": row.get("case_id"),
                    "status": status,
                    "checks": checks,
                    "semantic_review": row.get("semantic_review") or [],
                    "semantic_counts": row.get("semantic_counts") or {},
                    "score_kind": "program_checks",
                },
            )

        return score

    @scorer(metrics=[], name="robot_support_v1_judge_semantic")
    def _judge_scorer():
        async def score(state: TaskState, target: Target) -> InspectScore:
            judge = (state.metadata or {}).get("judge_semantic") or {}
            included = judge.get("denominator") == "included"
            verdict = judge.get("verdict")
            actual_answer = (state.metadata or {}).get("actual_answer")
            if actual_answer is not None:
                answer_text = str(actual_answer)
                answer_source = "run_answer"
            else:
                # No live answer supplied: keep the structural payload but make
                # clear that it is not the text the judge saw.
                answer_text = str(state.output.completion or "")
                answer_source = "state_completion_fallback"
            if included:
                value: Any = {"score": 1.0 if verdict == "passed" else 0.0,
                              "denominator": "included"}
                explanation = f"judge_verdict={verdict}; included in semantic denominator"
            else:
                # Excluded stays visibly unscored, never 0.0 = failure.
                value = {
                    "score": None,
                    "denominator": judge.get("denominator", "excluded"),
                    "reason": judge.get("denominatorReason"),
                }
                explanation = (
                    f"judge_excluded={judge.get('denominatorReason')}; "
                    "not counted as pass or fail"
                )
            return InspectScore(
                value=value,
                answer=answer_text[:4000],
                explanation=explanation,
                metadata={
                    "case_id": judge.get("caseId"),
                    "judge_run_id": judge.get("judgeRunId"),
                    "run_id": judge.get("runId"),
                    "status": judge.get("status"),
                    "verdict": verdict,
                    "denominator": judge.get("denominator", "excluded"),
                    "denominator_reason": judge.get("denominatorReason"),
                    "denominator_reason_label": judge.get("denominatorReasonLabel"),
                    "review_trust": (judge.get("scorePolicy") or {}).get("reviewTrust")
                    if isinstance(judge.get("scorePolicy"), dict)
                    else None,
                    "unresolved_issue_ids": judge.get("unresolvedIssueIds") or [],
                    "failure_basis": judge.get("failureBasis") or {},
                    "aggregation_mismatch": bool(judge.get("aggregationMismatch")),
                    "coverage_present": bool(judge.get("coveragePresent")),
                    "coverage_complete": bool(judge.get("coverageComplete")),
                    "coverage_missing": judge.get("coverageMissingIssueIds") or [],
                    "coverage_unbound": judge.get("coverageUnboundIssueIds") or [],
                    "coverage_uncertain": judge.get("coverageUncertainIssueIds") or [],
                    "coverage_unresolved": judge.get("coverageUnresolvedIssueIds") or [],
                    "aggregation_counts": judge.get("aggregationCounts"),
                    "workflow_version": judge.get("workflowVersion"),
                    "legacy_unverified": bool(judge.get("legacyUnverified")),
                    "system_errors": judge.get("systemErrors") or [],
                    "answer_source": answer_source,
                    "actual_answer_available": actual_answer is not None,
                    "evidence_count": judge.get("evidenceCount", 0),
                    "historical": judge.get("historical", False),
                    "roles": judge.get("roles") or [],
                    "arbitration": judge.get("arbitration"),
                    "limitations": judge.get("limitations") or [],
                    "provenance": judge.get("provenance") or {},
                    "judge_errors": judge.get("errors") or [],
                    "program_checks_separate": True,
                    "score_kind": "judge_semantic",
                },
            )

        return score

    scorers = [_scorer()]
    if judge_repo is not None:
        scorers.append(_judge_scorer())
    return Task(
        dataset=samples,
        solver=_solver(),
        scorer=scorers,
        name=name,
        version="1",
        metadata={
            "suite_id": "robot-support-v1",
            "mode": "state",
            "semantic_review": "pending_human" if judge_repo is None else "dsh_judge_read_only",
            "judge": {
                "schema_version": JUDGE_SCHEMA_VERSION,
                "enabled": judge_repo is not None,
                "read_only": True,
                "no_model": True,
                "run_id": run_id,
                "judge_run_id": judge_run_id,
            },
            "program_checks_separate": True,
            "note": "Inspect task covers the deterministic state path; live uses runner.py.",
        },
    )


def run_inspect_state_smoke(
    cases: list[dict[str, Any]],
    *,
    log_dir: str | Path,
    judge_results_root: str | Path | None = None,
    judge_run_id: str | None = None,
    run_id: str | None = None,
    run_answers: dict[str, str] | None = None,
) -> Any:
    """Run one real ``inspect_ai.eval`` call with a mock model placeholder.

    The custom solver never calls the placeholder model; this proves the Task,
    solver and scorers import and execute through the actual framework.
    """
    task = build_inspect_task(
        cases,
        judge_results_root=judge_results_root,
        judge_run_id=judge_run_id,
        run_id=run_id,
        run_answers=run_answers,
    )
    return inspect_eval(
        task,
        model="mockllm/model",
        log_dir=str(log_dir),
        display="none",
        fail_on_error=False,
    )


__all__ = ["build_inspect_task", "run_inspect_state_smoke"]
