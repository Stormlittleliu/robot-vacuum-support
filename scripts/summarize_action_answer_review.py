# -*- coding: utf-8 -*-
"""Programmatic summary of the single Judge run for the answer-review slice.

The script reads the frozen cases/bundles and the completed read-only Judge
result files. It does not call a model and does not write human reviews. Case
verdicts remain the Judge's program-aggregated verdicts; this report is a
programmatic presentation, not a second model judge.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "action-state-answer-review-v1"
RUN_ID = "asc-live-development-20261114-cdf5aac89d"
JUDGE_RUN_ID = "jr-action-review-20261114-01"
DSH_JUDGE = ROOT.parent / "dsh-judge"
REPORT = ROOT / "outputs" / "action-state-answer-review-report.json"


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def sha256_file(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recheck_refs(findings: list[dict[str, Any]], evidence_by_id: dict[str, str]) -> dict[str, Any]:
    refs = []
    accepted = 0
    rejected = 0
    for finding in findings:
        for ref in finding.get("evidence_refs") or []:
            evidence_text = normalize_ws(evidence_by_id.get(str(ref.get("id")), ""))
            quote = normalize_ws(ref.get("quote"))
            ok = bool(evidence_text and quote and len(quote) >= 6 and quote in evidence_text)
            if ok:
                accepted += 1
            else:
                rejected += 1
            refs.append({
                "criterion_id": finding.get("criterion_id"),
                "role": finding.get("role"),
                "evidence_id": ref.get("id"),
                "quote": ref.get("quote"),
                "quote_normalized": quote,
                "quote_ok": ok,
            })
    return {"checked": len(refs), "accepted": accepted, "rejected": rejected, "refs": refs}


def criterion_summary(bundle: dict[str, Any], result: dict[str, Any], criterion: dict[str, Any]) -> dict[str, Any]:
    criterion_id = criterion["criterion_id"]
    evidence_by_id = {item["id"]: item["text"] for item in bundle["evidence"]}
    merged = {issue.get("criterion_id"): issue for issue in ((result.get("aggregation") or {}).get("issues") or [])}
    issue = merged.get(criterion_id)
    findings = []
    for role in result.get("roles") or []:
        for finding in role.get("findings") or []:
            if finding.get("criterion_id") == criterion_id:
                findings.append({**finding, "role": role.get("role"), "role_verdict": role.get("verdict")})
    citation = recheck_refs(findings, evidence_by_id)
    kinds = sorted({str(item.get("kind")) for item in findings if item.get("kind")})
    summary_machine = {
        "criterion_id": criterion_id,
        "focus": criterion["focus"],
        "turn_scope": criterion["turn_scope"],
        "text": criterion["text"],
        "final_judgment": (issue or {}).get("final_judgment"),
        "coverage": (issue or {}).get("coverage"),
        "unresolved": criterion_id in ((result.get("aggregation") or {}).get("unresolved_issue_ids") or []),
        "arbitration_used": bool((issue or {}).get("arbitration_used")),
        "conflict": bool((issue or {}).get("conflict")),
        "citation_compromised": bool((issue or {}).get("citation_compromised")),
        "judgments": (issue or {}).get("judgments") or [
            {"role": item["role"], "judgment": item.get("judgment"), "coverage": "covered", "unbound": False}
            for item in findings
        ],
        "finding_count": len(findings),
        "kinds": kinds,
        "suggested_layers": sorted({str(item.get("suggested_layer")) for item in findings if item.get("suggested_layer")}),
        "findings": [
            {
                "role": item.get("role"),
                "judgment": item.get("judgment"),
                "kind": item.get("kind"),
                "severity": item.get("severity"),
                "suggested_layer": item.get("suggested_layer"),
                "summary": item.get("summary"),
                "citation_compromised": bool(item.get("citation_compromised")),
                "evidence_refs": item.get("evidence_refs") or [],
            }
            for item in findings
        ],
        "citation": {
            "checked": citation["checked"],
            "accepted": citation["accepted"],
            "rejected": citation["rejected"],
            "refs": citation["refs"],
        },
    }
    return summary_machine


def classify_case(result: dict[str, Any]) -> dict[str, Any]:
    finding_counts: dict[str, int] = {}
    criteria_with_kind: dict[str, list[str]] = {"agent": [], "evaluation": [], "insufficient_evidence": []}
    for role in result.get("roles") or []:
        for finding in role.get("findings") or []:
            kind = str(finding.get("kind") or "unknown")
            finding_counts[kind] = finding_counts.get(kind, 0) + 1
            if kind in criteria_with_kind and finding.get("criterion_id") not in criteria_with_kind[kind]:
                criteria_with_kind[kind].append(finding.get("criterion_id"))
    failed_issue_ids = [
        issue.get("criterion_id")
        for issue in ((result.get("aggregation") or {}).get("issues") or [])
        if issue.get("final_judgment") == "failed"
    ]
    uncertain_issue_ids = [
        issue.get("criterion_id")
        for issue in ((result.get("aggregation") or {}).get("issues") or [])
        if issue.get("final_judgment") == "uncertain"
    ]
    return {
        "finding_counts": finding_counts,
        "criteria_with_kind": criteria_with_kind,
        "failed_issue_ids": failed_issue_ids,
        "uncertain_issue_ids": uncertain_issue_ids,
        "has_agent_kind": bool(criteria_with_kind["agent"]),
        "has_evaluation_kind": bool(criteria_with_kind["evaluation"]),
        "has_insufficient_evidence_kind": bool(criteria_with_kind["insufficient_evidence"]),
    }


def case_summary(case: dict[str, Any], row: dict[str, Any], bundle: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    rubric = case.get("answer_review_rubric") or []
    base = {
        "case_id": case["id"],
        "title": case["title"],
        "case_hash": bundle["case_hash"],
        "input_hash": bundle["input_hash"],
        "session_id": row["session_id"],
        "actual_turns": [
            {
                "turn": turn["index"],
                "user": turn["content"],
                "answer": turn["answer"],
                "request_id": turn.get("request_id"),
                "trace_id": turn.get("trace_id"),
                "retrieval_current_count": len((turn.get("retrieval") or {}).get("current") or []),
                "retrieval_history_count": len((turn.get("retrieval") or {}).get("history") or []),
                "effective_tried": (turn.get("state") or {}).get("effective_tried"),
                "effective_not_tried": (turn.get("state") or {}).get("effective_not_tried"),
                "fact_audit": (turn.get("state") or {}).get("fact_audit"),
                "case_status": (turn.get("state") or {}).get("case_status"),
                "case_stage": (turn.get("state") or {}).get("case_stage"),
            }
            for turn in row["turns"]
        ],
        "program_checks": {
            "count": len(bundle["task"]["checks"]),
            "all_pass": all(item.get("status") == "pass" for item in bundle["task"]["checks"]),
            "note": "Structural checks are frozen separately and are not a semantic success rate.",
        },
    }
    if result is None:
        base.update({
            "judge": None,
            "criteria": [],
            "classification": None,
            "status": "judge_pending",
        })
        return base
    criteria = [criterion_summary(bundle, result, criterion) for criterion in rubric]
    merged = {issue.get("criterion_id"): issue for issue in ((result.get("aggregation") or {}).get("issues") or [])}
    missing_ids = [item["criterion_id"] for item in criteria if item["coverage"] in (None, "missing", "unknown")]
    base.update({
        "status": "judge_completed",
        "judge": {
            "judge_run_id": result.get("judge_run_id"),
            "run_id": result.get("run_id"),
            "status": result.get("status"),
            "verdict": result.get("verdict"),
            "aggregation_case_verdict": (result.get("aggregation") or {}).get("case_verdict"),
            "coverage": result.get("coverage"),
            "aggregation_counts": {
                key: (result.get("aggregation") or {}).get(key)
                for key in (
                    "issue_count", "expected_issue_count", "covered_issue_count", "missing_issue_count",
                    "unbound_issue_count", "confirmed_failed_count", "uncertain_count",
                    "not_applicable_count", "unresolved_count",
                )
            },
            "unresolved_issue_ids": (result.get("aggregation") or {}).get("unresolved_issue_ids") or [],
            "disputed_issue_ids": (result.get("aggregation") or {}).get("disputed_issue_ids") or [],
            "arbitration": result.get("arbitration"),
            "system_errors": result.get("system_errors") or [],
            "failure_roles": (result.get("provenance") or {}).get("failure_roles") or [],
            "citation_validation": (result.get("provenance") or {}).get("citation_validation") or {},
            "roles": [
                {"role": role.get("role"), "verdict": role.get("verdict"), "reason": role.get("reason")}
                for role in result.get("roles") or []
            ],
            "provenance": {
                key: (result.get("provenance") or {}).get(key)
                for key in ("model", "provider", "prompt_version", "workflow_version", "engine", "subagent_provider", "role_runs", "citation_validation")
            },
        },
        "criteria": criteria,
        "case_citation_audit": {
            "judge_citation_validation": (result.get("provenance") or {}).get("citation_validation") or {},
            "remaining_refs_recheck": {
                "checked": sum(item["citation"]["checked"] for item in criteria),
                "accepted": sum(item["citation"]["accepted"] for item in criteria),
                "rejected": sum(item["citation"]["rejected"] for item in criteria),
            },
            "rejected_quote_text": "not retained after the Judge removed the invalid ref; not reconstructed here",
            "note": "The program quote check verifies exact/normalized substring authenticity only, not semantic support. Rejected refs are removed before the final finding; the rejection remains in provenance.",
        },
        "classification": classify_case(result),
        "judge_missing_criteria": missing_ids,
    })
    return base


def opinion_block(case_reports: list[dict[str, Any]], criterion_ids: list[str]) -> list[dict[str, Any]]:
    rows = []
    for case in case_reports:
        for criterion in case.get("criteria") or []:
            if criterion["criterion_id"] in criterion_ids:
                rows.append({
                    "case_id": case["case_id"],
                    "criterion_id": criterion["criterion_id"],
                    "final_judgment": criterion["final_judgment"],
                    "coverage": criterion["coverage"],
                    "unresolved": criterion["unresolved"],
                    "kinds": criterion["kinds"],
                    "summaries": [finding.get("summary") for finding in criterion.get("findings") or []],
                })
    return rows


def build_disputes(case_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": "dispute-old-success-vs-execution-effect",
            "topic": "旧题 success 到底表示“执行了”还是“效果成功”",
            "current_contract": "docs/action-state-contract.md §1/§4: user_executed and outcome are independent; outcome=success only with explicit recovery and clear scope; execution alone is unknown, never success.",
            "real_basis": "S1 T1 clean_contacts/unknown; S2 T1 replace_dock/unknown; both later corrections preserve unknown, not success.",
            "judge_opinion": opinion_block(case_reports, [
                "turn-1-user-intent-and-outcome-separation",
                "turn-3-unknown-not-success-and-no-forced-closure",
                "case-task-completion",
            ]),
            "still_unverified": "No new evidence about the old frozen baseline's success-vs-unknown expectations; old expected values remain historical and were not changed.",
            "suggested_layer": "state + data/oracle",
        },
        {
            "id": "dispute-recall-clarification-citation",
            "topic": "纯回忆/澄清轮是否应有 citation/retrieval",
            "current_contract": "Pure recall/clarification with no external factual claim makes citation/retrieval not_applicable; historical retrieval may be reused; RAG is not forced every turn.",
            "real_basis": "Both T3 recall answers have 0 main-agent calls, 0 current retrieval, and only restate effective facts; 4 historical retrieval excerpts are available.",
            "judge_opinion": opinion_block(case_reports, [
                "turn-3-citation-applicability",
                "turn-2-evidence-support",
                "case-evidence-support",
            ]),
            "still_unverified": "Whether a future recall answer that adds a maintenance/domain claim always switches to source-supported; not exercised by these two recall turns.",
            "suggested_layer": "validation + prompt-context (not keyword rules)",
        },
        {
            "id": "dispute-partial-success-local-effect",
            "topic": "手动放回能充但自动失败时的局部结果",
            "current_contract": "partial_success keeps the case active and does not resolve it; a local action outcome must not be reported as global success.",
            "real_basis": "This six-turn slice does not contain the exact partial-success utterance; the contract and prior action-state slice define the oracle. No Judge conclusion is inferred for an unexercised case.",
            "judge_opinion": opinion_block(case_reports, [
                "turn-3-unknown-not-success-and-no-forced-closure",
                "case-task-completion",
            ]),
            "still_unverified": "A real dialogue with explicit partial success is still needed; current smoke only has execution-unknown plus correction.",
            "suggested_layer": "state/execution + data",
        },
        {
            "id": "dispute-unknown-vs-failed",
            "topic": "unknown 是否等于 failed，用户没说效果时能否判失败",
            "current_contract": "user_executed=true with no effect evidence is outcome=unknown; only explicit failure/still-broken evidence is failed; unknown is not a fail.",
            "real_basis": "S1/S2 T1 and corrected T2 all remain unknown; T3 answers must not call unknown success or failure.",
            "judge_opinion": opinion_block(case_reports, [
                "turn-1-user-intent-and-outcome-separation",
                "turn-2-correction-retracts-old-fact",
                "turn-3-unknown-not-success-and-no-forced-closure",
            ]),
            "still_unverified": "No explicit user failure utterance in this slice; whether a failure after a corrected action is recorded as failed remains covered by offline oracle, not this live dialogue.",
            "suggested_layer": "state/execution",
        },
        {
            "id": "dispute-granularity-followup",
            "topic": "建议颗粒度/跟进是否足够",
            "current_contract": "One appropriate next step or necessary clarification; do not repeat a negated assumption; do not force closure; replace/move are observation-only, not auto-suggested.",
            "real_basis": "Both T1/T2 answers ask only for the outcome of the effective step; both T3 answers repeat the same single confirm step; replace/move are not suggested as automatic next steps.",
            "judge_opinion": opinion_block(case_reports, [
                "turn-1-single-step-and-no-unsupported-claim",
                "turn-2-does-not-repeat-negated-assumption",
                "turn-3-unknown-not-success-and-no-forced-closure",
            ]),
            "still_unverified": "Whether repeating the same confirm step in a short recall turn is always the best UX, or should stop at the recall answer; needs a separate interaction-policy decision.",
            "suggested_layer": "context/interaction policy, not prompt patch",
        },
    ]


def main() -> None:
    freeze = read_json(OUT / "freeze.json")
    manifest = read_json(OUT / "evidence-manifest.json")
    cases = read_jsonl(OUT / "dataset" / "cases.jsonl")
    rows = read_jsonl(OUT / "results" / RUN_ID / "cases.jsonl")
    result_dir = OUT / "judge_results" / JUDGE_RUN_ID
    result_files = sorted(result_dir.glob("*.json")) if result_dir.exists() else []
    results = {}
    for path in result_files:
        payload = read_json(path)
        if isinstance(payload, dict) and payload.get("case_id"):
            results[payload["case_id"]] = payload
    bundle_by_case = {
        case["id"]: read_json(OUT / "evidence_bundles" / f"{case['id']}.json") for case in cases
    }
    row_by_case = {row["case_id"]: row for row in rows}
    case_reports = [
        case_summary(case, row_by_case[case["id"]], bundle_by_case[case["id"]], results.get(case["id"]))
        for case in cases
    ]
    dsh_run_dir = DSH_JUDGE / ".runtime" / "runs" / JUDGE_RUN_ID
    summary = read_json(dsh_run_dir / "summary.json")
    status = "in_progress" if len(results) < len(cases) else "completed_with_machine_results"
    report = {
        "schema_version": "action-state-answer-review-report/v1",
        "status": status,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": "Exploratory 2-dialogue / 6-turn development answer review. Not a holdout, success rate, or statistical result.",
        "run_id": RUN_ID,
        "judge_run_id": JUDGE_RUN_ID,
        "artifact_id": (freeze.get("artifact") or {}).get("artifact_id"),
        "foundation": {
            "workspace": "outputs/action-state-answer-review-v1",
            "freeze": "outputs/action-state-answer-review-v1/freeze.json",
            "freeze_generated_at": freeze.get("generated_at"),
            "source_run": "outputs/action-state-contract-smoke-20261114-3/run.json",
            "source_run_sha256": freeze.get("source_hashes", {}).get("run_json_sha256"),
            "contract": "docs/action-state-contract.md",
            "contract_sha256": freeze.get("contract", {}).get("sha256"),
            "program_checks": "13/13 structural checks passed in the frozen smoke run; these are not semantic answer judgments.",
            "program_checks_sha256": freeze.get("source_hashes", {}).get("program_checks_sha256"),
            "frozen_v1_hashes": freeze.get("frozen_v1", {}).get("hashes"),
            "reviews_workspace_empty": freeze.get("reviews_workspace_empty"),
        },
        "judge_execution": {
            "results_seen": len(results),
            "recovery_external": {
                "report": "dsh-judge/outputs/action-review-recovery.json",
                "status": (read_json(DSH_JUDGE / "outputs" / "action-review-recovery.json", {}) or {}).get("status"),
                "post_summary": ((read_json(DSH_JUDGE / "outputs" / "action-review-recovery.json", {}) or {}).get("post_resume", {}) or {}).get("stdout_tail"),
                "note": "Separate recovery task resumed only the failed arbitrator call; this report did not rerun the Judge or overwrite its results.",
            },
            "result_files": [f"judge_results/{JUDGE_RUN_ID}/{path.name}" for path in result_files],
            "summary": summary,
            "summary_path": str(dsh_run_dir / "summary.json") if (dsh_run_dir / "summary.json").exists() else None,
            "single_invocation_expected": True,
            "no_rerun_note": "This report reads whatever the one Judge invocation produced; failed/partial evidence is preserved, not re-rolled.",
            "workspace_metadata": [
                "judge_run/summary.json",
                "judge_run/run-spec.json",
                "judge_run/implementation-report.json",
            ],
        },
        "cases": case_reports,
        "cross_case": {
            "case_count": len(cases),
            "turn_count": sum(len(row["turns"]) for row in rows),
            "machine_verdicts": {case["case_id"]: (case.get("judge") or {}).get("verdict") for case in case_reports},
            "unresolved_issue_ids": {
                case["case_id"]: (case.get("judge") or {}).get("unresolved_issue_ids") or []
                for case in case_reports
            },
            "classification_totals": {
                "agent": sum(1 for case in case_reports if (case.get("classification") or {}).get("has_agent_kind")),
                "evaluation": sum(1 for case in case_reports if (case.get("classification") or {}).get("has_evaluation_kind")),
                "insufficient_evidence": sum(1 for case in case_reports if (case.get("classification") or {}).get("has_insufficient_evidence_kind")),
            },
            "note": "This is a programmatic presentation of Judge output, not a model-written overall verdict and not a success rate.",
        },
        "disputes": build_disputes(case_reports),
        "documentation": "docs/action-state-answer-review.md",
        "read_only_acceptance": {
            "gui_api": "outputs/action-state-answer-review-gui-readonly.json",
            "headless": "outputs/action-state-answer-review-headless.json",
            "inspect": "outputs/action-state-answer-review-inspect.json",
            "screenshots": [
                "outputs/action-state-answer-review-v1/gui-headless-contact.png",
                "outputs/action-state-answer-review-v1/gui-headless-dock.png",
            ],
            "holdout_ui_regression": "outputs/action-state-answer-review-holdout-ui.json",
            "holdout_ui_note": "UI now reads meta.suite.counts.holdout: 0 hides the split button/gate/reveal and falls back #split=holdout to development; 16 keeps the original exposure gate. Headless fixture test uses counts 0/16 without reading real holdout text.",
        },
        "limitations": [
            "Exploratory two-dialogue/six-turn slice only; no holdout and no denominator for a success rate.",
            "Program checks are structural and are reported separately from semantic Judge findings.",
            "Machine Judge output is not human review; the reviews/ directory remains empty for the user.",
            "Citation quote checks verify authenticity, not semantic support; semantic support remains a Judge judgment.",
            "The old frozen v1 32/16 dataset, reviews and expected values were not changed.",
        ],
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(REPORT),
        "status": status,
        "results_seen": len(results),
        "case_verdicts": {case["case_id"]: (case.get("judge") or {}).get("verdict") for case in case_reports},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
