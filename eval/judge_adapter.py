# -*- coding: utf-8 -*-
"""Read-only adapter for independent DSH judge results (``dsh-judge/v1``).

Boundary
--------
* Results live only under ``eval/judge_results/<judge_run_id>/<case_id>.json``.
* The adapter never accepts a caller-supplied path; ids are validated and the
  resolved file must stay directly under the configured root.
* It never calls a model, never executes a case and never writes anything.
* A result is bound to a run id and a case-definition hash.  Old results whose
  case hash no longer matches are reported as ``historical`` and are never used
  as the current recommendation.
* ``status`` is not the same as ``verdict``.  ``error`` / ``partial`` can carry
  any verdict; they are never treated as a pass and never enter the judge score
  denominator.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "dsh-judge/v1"
JUDGE_STATUSES = ("completed", "error", "partial")
JUDGE_VERDICTS = ("passed", "failed", "uncertain", "not_applicable")
FINDING_KINDS = ("agent", "evaluation", "insufficient_evidence")
INCLUDED_JUDGE_VERDICTS = ("passed", "failed")

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class JudgeContractError(ValueError):
    """Raised when a judge artifact violates the read-only contract."""

    def __init__(self, code: str, message: str, *, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def case_definition_hash(case: dict[str, Any]) -> str:
    """Same algorithm as the manual-review server's ``caseHash``."""
    payload = {key: value for key, value in case.items() if key != "review"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_id(value: str, label: str = "id") -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or value in (".", ".."):
        raise JudgeContractError(
            "invalid_id",
            f"{label} 不是安全标识符（只允许字母数字 . _ -，且不能是 . / ..）。",
            path=label,
        )
    return value


def _text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _id_list(value: Any) -> list[str]:
    """Coverage counters are numbers; only strings/lists are issue ids."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return []


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finding(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise JudgeContractError("invalid_finding", f"findings[{index}] 必须是对象。", path=f"roles[].findings[{index}]")
    kind = _text(item.get("kind"))
    if kind not in FINDING_KINDS:
        raise JudgeContractError(
            "invalid_finding_kind",
            f"findings[{index}].kind 必须是 agent/evaluation/insufficient_evidence。",
            path=f"findings[{index}].kind",
        )
    summary = _text(item.get("summary")).strip()
    if not summary:
        raise JudgeContractError("missing_finding_summary", f"findings[{index}].summary 不能为空。", path=f"findings[{index}].summary")
    refs: list[dict[str, str]] = []
    raw_refs = item.get("evidence_refs")
    if raw_refs is not None:
        if not isinstance(raw_refs, list):
            raise JudgeContractError("invalid_evidence_refs", f"findings[{index}].evidence_refs 必须是数组。", path=f"findings[{index}].evidence_refs")
        for ref_index, ref in enumerate(raw_refs):
            if not isinstance(ref, dict):
                raise JudgeContractError("invalid_evidence_ref", f"evidence_refs[{ref_index}] 必须是对象。", path=f"findings[{index}].evidence_refs[{ref_index}]")
            ref_id = _text(ref.get("id")).strip()
            quote = _text(ref.get("quote")).strip()
            if not ref_id or not quote:
                raise JudgeContractError(
                    "incomplete_evidence_ref",
                    f"evidence_refs[{ref_index}] 需要非空 id 和 quote。",
                    path=f"findings[{index}].evidence_refs[{ref_index}]",
                )
            refs.append({"id": ref_id, "quote": quote})
    return {
        "kind": kind,
        "severity": _text(item.get("severity"), default="unknown"),
        "summary": summary,
        "evidenceRefs": refs,
        "suggestedLayer": _text(item.get("suggested_layer")) or None,
        # Optional native-plugin fields. Old results without them stay valid.
        "criterionId": _text(item.get("criterion_id")) or None,
        "turnScope": item.get("turn_scope"),
        "judgment": item.get("judgment"),
    }


def _role(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise JudgeContractError("invalid_role", f"roles[{index}] 必须是对象。", path=f"roles[{index}]")
    role = _text(item.get("role")).strip()
    if not role:
        raise JudgeContractError("missing_role", f"roles[{index}].role 不能为空。", path=f"roles[{index}].role")
    verdict = _text(item.get("verdict"))
    if verdict not in JUDGE_VERDICTS:
        raise JudgeContractError(
            "invalid_role_verdict",
            f"roles[{index}].verdict 必须是 {'/'.join(JUDGE_VERDICTS)}。",
            path=f"roles[{index}].verdict",
        )
    raw_findings = item.get("findings")
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        raise JudgeContractError("invalid_findings", f"roles[{index}].findings 必须是数组。", path=f"roles[{index}].findings")
    return {
        "role": role,
        "verdict": verdict,
        "findings": [_finding(finding, finding_index) for finding_index, finding in enumerate(raw_findings)],
        "reason": _text(item.get("reason")),
    }


def normalize_result(payload: Any, *, judge_run_id: str, case_id: str) -> dict[str, Any]:
    """Validate and normalize one judge artifact.

    Raises ``JudgeContractError`` with a stable code; callers turn that into an
    explicit malformed/error view instead of a silent skip.
    """
    safe_id(judge_run_id, "judge_run_id")
    safe_id(case_id, "case_id")
    if not isinstance(payload, dict):
        raise JudgeContractError("invalid_payload", "Judge 结果必须是一个 JSON 对象。")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise JudgeContractError(
            "unsupported_schema_version",
            f"schema_version 必须是 {SCHEMA_VERSION!r}。",
            path="schema_version",
        )
    if _text(payload.get("case_id")) != case_id:
        raise JudgeContractError("case_id_mismatch", "结果内的 case_id 与文件名/请求不一致。", path="case_id")
    if _text(payload.get("judge_run_id")) != judge_run_id:
        raise JudgeContractError("judge_run_id_mismatch", "结果内的 judge_run_id 与目录不一致。", path="judge_run_id")
    run_id = _text(payload.get("run_id")).strip()
    case_hash = _text(payload.get("case_hash")).strip()
    input_hash = _text(payload.get("input_hash")).strip()
    if not run_id:
        raise JudgeContractError("missing_run_id", "run_id 不能为空。", path="run_id")
    if not case_hash:
        raise JudgeContractError("missing_case_hash", "case_hash 不能为空。", path="case_hash")
    if not input_hash:
        raise JudgeContractError("missing_input_hash", "input_hash 不能为空。", path="input_hash")
    status = _text(payload.get("status"))
    if status not in JUDGE_STATUSES:
        raise JudgeContractError("invalid_status", f"status 必须是 {'/'.join(JUDGE_STATUSES)}。", path="status")
    verdict = _text(payload.get("verdict"))
    if verdict not in JUDGE_VERDICTS:
        raise JudgeContractError("invalid_verdict", f"verdict 必须是 {'/'.join(JUDGE_VERDICTS)}。", path="verdict")
    raw_roles = payload.get("roles")
    if not isinstance(raw_roles, list):
        raise JudgeContractError("invalid_roles", "roles 必须是数组。", path="roles")
    roles = [_role(item, index) for index, item in enumerate(raw_roles)]
    arbitration = payload.get("arbitration")
    if not isinstance(arbitration, dict):
        raise JudgeContractError("invalid_arbitration", "arbitration 必须是对象。", path="arbitration")
    if "required" not in arbitration or not isinstance(arbitration.get("required"), bool):
        raise JudgeContractError("invalid_arbitration_required", "arbitration.required 必须是布尔值。", path="arbitration.required")
    limitations = payload.get("limitations")
    if limitations is None:
        limitations = []
    if not isinstance(limitations, list):
        raise JudgeContractError("invalid_limitations", "limitations 必须是数组。", path="limitations")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise JudgeContractError("invalid_provenance", "provenance 必须是对象。", path="provenance")
    # Optional deterministic aggregation block (native plugin).  Preserve it
    # verbatim so criterion ids / unresolved ids / arbitration basis are not
    # lost by normalization; older artifacts simply have no such block.
    aggregation = payload.get("aggregation")
    if aggregation is not None and not isinstance(aggregation, dict):
        raise JudgeContractError("invalid_aggregation", "aggregation 必须是对象。", path="aggregation")
    system_errors_raw = payload.get("system_errors")
    if system_errors_raw is None:
        system_errors_raw = []
    elif isinstance(system_errors_raw, (str, dict)):
        # Optional field: keep an old/single-value shape displayable instead of
        # turning the whole artifact into a contract error.
        system_errors_raw = [system_errors_raw]
    if not isinstance(system_errors_raw, list):
        raise JudgeContractError("invalid_system_errors", "system_errors 必须是数组。", path="system_errors")
    errors = _string_list(payload.get("errors") if payload.get("errors") is not None else payload.get("error"))
    evidence_count = sum(
        len(finding["evidenceRefs"]) for role in roles for finding in role["findings"]
    )
    # Deterministic trust metadata.  The case verdict is the top-level
    # ``verdict``; the aggregation block can additionally expose unresolved
    # criteria and per-finding kinds.  A failed case whose only failed findings
    # are ``evaluation`` (rubric/checker) findings must not be scored as an
    # agent failure.  Old artifacts without these optional fields keep the old
    # behaviour because ``findings_with_judgment`` stays 0.
    unresolved_ids = _string_list((aggregation or {}).get("unresolved_issue_ids"))
    disputed_ids = _string_list((aggregation or {}).get("disputed_issue_ids"))
    aggregation_case_verdict = _text((aggregation or {}).get("case_verdict")) or None
    # Native contract v2 adds optional coverage/counts/system_errors.  Never
    # trust a version string alone: a v2 artifact without a complete coverage
    # block is explicitly uncertain.
    coverage = (aggregation or {}).get("coverage")
    coverage_present = isinstance(coverage, dict) or isinstance(coverage, (list, tuple))
    coverage_missing: list[str] = []
    coverage_unbound: list[str] = []
    coverage_uncertain: list[str] = []
    coverage_unresolved: list[str] = []
    coverage_complete_flag: bool | None = None
    coverage_counts = (aggregation or {}).get("counts")
    if isinstance(coverage, dict):
        coverage_missing = _id_list(
            coverage.get("missing_issue_ids")
            or coverage.get("missing_ids")
            or coverage.get("missing")
        )
        coverage_unbound = _id_list(
            coverage.get("unbound_issue_ids")
            or coverage.get("unbound_ids")
            or coverage.get("unbound")
        )
        coverage_uncertain = _id_list(
            coverage.get("uncertain_issue_ids")
            or coverage.get("uncertain")
        )
        coverage_unresolved = _id_list(
            coverage.get("unresolved_issue_ids")
            or coverage.get("unresolved")
        )
        for flag_key in ("complete", "is_complete", "coverage_complete"):
            if isinstance(coverage.get(flag_key), bool):
                coverage_complete_flag = coverage[flag_key]
                break
        total = None
        covered = None
        for key in ("issues_total", "criteria_total", "expected", "expected_issue_count", "total"):
            if key in coverage:
                total = _as_int(coverage.get(key))
                break
        for key in ("issues_covered", "criteria_covered", "judged", "covered", "covered_issue_count"):
            if key in coverage:
                covered = _as_int(coverage.get(key))
                break
        missing_count = _as_int(coverage.get("missing_issue_count") if "missing_issue_count" in coverage else coverage.get("missing"))
        unbound_count = _as_int(coverage.get("unbound_issue_count") if "unbound_issue_count" in coverage else coverage.get("unbound"))
        uncertain_count = _as_int(coverage.get("uncertain_count") if "uncertain_count" in coverage else coverage.get("uncertain"))
        unresolved_count = _as_int(coverage.get("unresolved_count") if "unresolved_count" in coverage else coverage.get("unresolved"))
        binding_errors = coverage.get("binding_errors")
        if coverage_complete_flag is None:
            coverage_complete_flag = not (
                coverage_missing
                or coverage_unbound
                or coverage_uncertain
                or coverage_unresolved
                or (missing_count or 0) > 0
                or (unbound_count or 0) > 0
                or (uncertain_count or 0) > 0
                or (unresolved_count or 0) > 0
                or bool(binding_errors)
            )
        if total is not None and covered is not None:
            coverage_complete_flag = bool(coverage_complete_flag) and covered >= total
    else:
        missing_count = unbound_count = uncertain_count = unresolved_count = 0
    coverage_complete = bool(coverage_present and coverage_complete_flag)
    if not isinstance(coverage_counts, dict):
        # Native v2 also exposes flat ``*_count`` fields at aggregation level.
        coverage_counts = {
            str(key): value
            for key, value in (aggregation or {}).items()
            if key.endswith("_count") and isinstance(value, (int, float))
        } or None
    coverage_complete = bool(coverage_present and coverage_complete_flag)
    workflow_version = _text(provenance.get("workflow_version")).strip()
    version_match = re.search(r"v(\d+)", workflow_version, re.IGNORECASE)
    major_version = int(version_match.group(1)) if version_match else None
    # v1/missing-version artifacts predate the coverage contract.  They stay
    # visible as history but are excluded from the trusted denominator.
    legacy_unverified = bool(
        (major_version is not None and major_version < 2)
        or (major_version is None and not coverage_present)
    )
    findings_with_judgment = 0
    failed_agent = 0
    failed_evaluation = 0
    failed_insufficient = 0
    for role in roles:
        for finding in role["findings"]:
            judgment = finding.get("judgment")
            if judgment is None:
                continue
            findings_with_judgment += 1
            if judgment != "failed":
                continue
            if finding["kind"] == "agent":
                failed_agent += 1
            elif finding["kind"] == "evaluation":
                failed_evaluation += 1
            else:
                failed_insufficient += 1
    failure_basis = {
        "findingsWithJudgment": findings_with_judgment,
        "agentFailed": failed_agent,
        "evaluationFailed": failed_evaluation,
        "insufficientEvidenceFailed": failed_insufficient,
    }
    aggregation_mismatch = bool(
        aggregation_case_verdict and aggregation_case_verdict != verdict
    )
    # Prefer explicit top-level timestamps, then the native provenance block.
    # Without this fallback the "newest result" ordering silently used the
    # judge_run_id as the only key for real native artifacts.
    created_at = _text(payload.get("created_at")) or _text(provenance.get("created_at")) or None
    finished_at = (
        _text(payload.get("finished_at"))
        or _text(provenance.get("completed_at"))
        or _text(provenance.get("finished_at"))
        or None
    )
    # Extra arbitration fields are preserved as JSON for display; no arbitrary
    # file reads, just data already inside the validated artifact.
    arbitration_extra = {
        str(key): value
        for key, value in arbitration.items()
        if key not in ("required", "reason")
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "caseId": case_id,
        "runId": run_id,
        "caseHash": case_hash,
        "inputHash": input_hash,
        "judgeRunId": judge_run_id,
        "status": status,
        "verdict": verdict,
        "roles": roles,
        "arbitration": {
            "required": arbitration["required"],
            "reason": _text(arbitration.get("reason")),
            "details": arbitration_extra,
        },
        "aggregation": aggregation,
        "aggregationCaseVerdict": aggregation_case_verdict,
        "aggregationMismatch": aggregation_mismatch,
        "aggregationCounts": coverage_counts,
        "coverage": coverage,
        "coveragePresent": coverage_present,
        "coverageComplete": coverage_complete,
        "coverageMissingIssueIds": coverage_missing,
        "coverageUnboundIssueIds": coverage_unbound,
        "coverageUncertainIssueIds": coverage_uncertain,
        "coverageUnresolvedIssueIds": coverage_unresolved,
        "workflowVersion": workflow_version or None,
        "legacyUnverified": legacy_unverified,
        "systemErrors": system_errors_raw,
        "unresolvedIssueIds": unresolved_ids,
        "disputedIssueIds": disputed_ids,
        "failureBasis": failure_basis,
        "limitations": [str(item) for item in limitations],
        "provenance": {str(key): value for key, value in provenance.items()},
        "errors": errors,
        "error": _text(payload.get("error")) or (errors[0] if errors else None),
        "createdAt": created_at,
        "finishedAt": finished_at,
        "evidenceCount": evidence_count,
    }


def score_policy(result: dict[str, Any] | None, *, current_case_hash: str, run_id: str | None,
                 error: str | None = None, historical: bool = False) -> dict[str, Any]:
    """Compute the semantic-score metadata + denominator decision.

    Only a ``completed`` artifact with verdict passed/failed, a matching run,
    a matching case hash, no unresolved arbitration issue, no evaluation-only
    failure, and at least one quoted evidence reference enters the judge
    denominator.  Everything else is explicitly excluded with a reason; none of
    the excluded states is silently counted as a pass.
    """
    if result is None:
        reason = "no_result"
        return {
            "included": False,
            "denominator": "excluded",
            "reason": reason,
            "reasonLabel": "无 Judge 结果（pending）",
            "reviewTrust": "no_result",
            "historical": historical,
            "error": error,
        }
    status = result.get("status")
    verdict = result.get("verdict")
    basis = result.get("failureBasis") or {}
    findings_with_judgment = int(basis.get("findingsWithJudgment") or 0)
    unresolved = result.get("unresolvedIssueIds") or []
    system_errors = result.get("systemErrors") or []
    coverage_present = bool(result.get("coveragePresent"))
    coverage_complete = bool(result.get("coverageComplete"))
    legacy = bool(result.get("legacyUnverified"))
    if error:
        reason = "malformed_result"
    elif historical or result.get("caseHash") != current_case_hash:
        reason = "historical_case_hash"
    elif run_id is None or result.get("runId") != run_id:
        # A result must be bound to the exact run/answer it judged.  Allowing
        # ``run_id=None`` here would let an arbitrary current-hash artifact be
        # scored without matching the selected run.
        reason = "run_mismatch"
    elif system_errors:
        reason = "system_errors"
    elif legacy:
        # Native contract v1 (or an artifact with no version and no coverage)
        # predates the complete-coverage contract.  Keep it visible as a
        # historical artifact, never as a trusted current score.
        reason = "legacy_unverified"
    elif not coverage_present:
        # v2 alone is not enough: without coverage the case may not have been
        # fully judged, so it is explicitly uncertain, not trusted.
        reason = "coverage_missing"
    elif not coverage_complete:
        reason = "coverage_incomplete"
    elif status == "error":
        reason = "status_error"
    elif status == "partial":
        reason = "status_partial"
    elif unresolved:
        # A disputed criterion that the arbitrator left unresolved is not a
        # final case verdict; it must not enter a trusted denominator.
        reason = "unresolved_arbitration"
    elif result.get("aggregationMismatch"):
        reason = "aggregation_verdict_mismatch"
    elif (
        verdict == "failed"
        and findings_with_judgment > 0
        and int(basis.get("agentFailed") or 0) == 0
        and int(basis.get("evaluationFailed") or 0) > 0
    ):
        # The judge failed the case only because of rubric/checker
        # (evaluation) findings, not an observed agent failure.
        reason = "failed_evaluation_only"
    elif verdict == "uncertain":
        reason = "verdict_uncertain"
    elif verdict == "not_applicable":
        reason = "verdict_not_applicable"
    elif verdict not in INCLUDED_JUDGE_VERDICTS:
        reason = "verdict_not_scored"
    elif int(result.get("evidenceCount") or 0) <= 0:
        reason = "missing_evidence"
    else:
        return {
            "included": True,
            "denominator": "included",
            "reason": "completed_verdict_with_evidence",
            "reasonLabel": "v2 完整 coverage、hash/run 匹配、无未决仲裁、含原文证据",
            "reviewTrust": "trusted",
            "coverageComplete": True,
            "legacyUnverified": False,
            "historical": False,
            "error": None,
        }
    labels = {
        "malformed_result": "结果格式错误，不能评分",
        "historical_case_hash": "caseHash 不匹配当前题目，按历史快照处理",
        "run_mismatch": "run 不匹配或未绑定当前回答，不能用于当前评分",
        "system_errors": "存在 system_errors，机器结果不完整，不能评分",
        "legacy_unverified": "旧 contract/无完整 coverage，按 legacy_unverified 排除可信评分（仍可查看历史）",
        "coverage_missing": "v2 结果缺少 coverage，不能仅凭版本信任，按 uncertain 排除",
        "coverage_incomplete": "coverage 未完整（missing/unbound/uncertain 非空），按 uncertain 排除",
        "status_error": "Judge 状态 error，不能当通过",
        "status_partial": "Judge 状态 partial，不能当通过",
        "unresolved_arbitration": "存在未决仲裁项，不能作为最终 case 评分",
        "aggregation_verdict_mismatch": "aggregation 的 case_verdict 与顶层 verdict 不一致，不能评分",
        "failed_evaluation_only": "failed 只由 evaluation（rubric/检查器）问题支撑，不计为 Agent 失败",
        "verdict_uncertain": "Judge 判定 uncertain，缺足够证据",
        "verdict_not_applicable": "Judge 判定 not_applicable，不进分母",
        "verdict_not_scored": "verdict 不可计分",
        "missing_evidence": "缺少可引用的原文证据，不进分母",
        "no_result": "暂无 Judge 结果（pending）",
    }
    trust = {
        "malformed_result": "not_trusted_contract",
        "historical_case_hash": "historical",
        "run_mismatch": "wrong_run",
        "system_errors": "system_errors",
        "legacy_unverified": "legacy_unverified",
        "coverage_missing": "coverage_missing",
        "coverage_incomplete": "coverage_incomplete",
        "status_error": "not_trusted_status",
        "status_partial": "not_trusted_status",
        "unresolved_arbitration": "provisional_unresolved",
        "aggregation_verdict_mismatch": "inconsistent_aggregation",
        "failed_evaluation_only": "evaluation_only",
        "verdict_uncertain": "uncertain",
        "verdict_not_applicable": "not_applicable",
        "verdict_not_scored": "not_scored",
        "missing_evidence": "missing_evidence",
        "no_result": "no_result",
    }.get(reason, "not_trusted")
    return {
        "included": False,
        "denominator": "excluded",
        "reason": reason,
        "reasonLabel": labels.get(reason, reason),
        "reviewTrust": trust,
        "coverageComplete": coverage_complete,
        "legacyUnverified": legacy,
        "historical": bool(historical),
        "error": error,
    }


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def text_corpus(value: Any) -> str:
    """Flatten every string value (and key) for quote verification.

    ``json.dumps`` keeps newlines escaped as ``\\n``; a multi-line judge quote
    would then never match the serialized row.  Flattening the actual strings
    fixes that without reading any file outside the validated case/run.
    """
    parts: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for key, child in item.items():
                parts.append(str(key))
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return "\n".join(parts)


def _quote_matches(quote: str, compact_corpus: str) -> bool:
    """Conservative containment check for a judge-supplied quote.

    Real judge findings often quote a *fragment* of a JSON check object whose
    key order differs from the serialized row (e.g. ``"name":..., "status":...``
    while the row stores ``"dimension"`` between them).  A plain substring check
    would flag those as fabricated.  Accept the fragment when every
    ``"key":"value"`` pair, or every quoted token for array-like fragments, is
    present in the corpus.
    """
    compact_quote = _compact_text(quote)
    if not compact_quote:
        return False
    if compact_quote in compact_corpus:
        return True
    pairs = re.findall(r'"([^"]{1,120})"\s*:\s*"([^"]{0,240})"', quote)
    if pairs and all(f'"{key}":"{value}"' in compact_corpus for key, value in pairs):
        return True
    tokens = [token for token in re.findall(r'"([^"]{2,240})"', quote) if token]
    if len(tokens) >= 2 and all(f'"{token}"' in compact_corpus for token in tokens):
        return True
    return False


def verify_evidence_quotes(result: dict[str, Any], corpus: str) -> dict[str, Any]:
    """Locally check that judge-supplied quotes occur in the read-only corpus.

    The DSH judge is an untrusted external artifact.  Its quotes are displayed
    as evidence, so this adapter verifies they are actually present in the
    dataset case + selected run text instead of trusting
    ``provenance.citation_validation``.  Verification is conservative: a
    normalized substring or structured-fragment match.  A missing quote is
    reported, not deleted, and never becomes a pass by itself.
    """
    compact_corpus = _compact_text(corpus)
    checked = 0
    verified = 0
    unverified_ids: list[str] = []
    for role in result.get("roles") or []:
        for finding in role.get("findings") or []:
            for ref in finding.get("evidenceRefs") or []:
                checked += 1
                if _quote_matches(str(ref.get("quote") or ""), compact_corpus):
                    verified += 1
                else:
                    unverified_ids.append(str(ref.get("id") or "?"))
    return {
        "checked": checked,
        "verified": verified,
        "unverified": len(unverified_ids),
        "unverifiedIds": unverified_ids,
        "corpus": "dataset_case+selected_run",
        "policy": "normalized substring or key/value fragment; judge quotes are untrusted until matched",
        "interpretation": "local_unverified_not_fabrication",
        "coverageLimit": "only parsed dataset case + selected run text; judge prompt/evidence-bundle rubric or derived criteria may be legitimate but are outside this whitelist",
        "note": "未在本地白名单输入中追溯 ≠ 伪造，也不据此判 agent 错；保持存疑，待输入证据包/人工语义审核。",
    }


class JudgeRepo:
    """Index of ``eval/judge_results`` restricted to the documented layout."""

    def __init__(self, results_root: Path | str):
        self.root = Path(results_root).resolve()
        self._lock = threading.Lock()
        self._errors: list[dict[str, Any]] = []

    def exists(self) -> bool:
        return self.root.is_dir()

    def _run_dirs(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        result: list[Path] = []
        for entry in sorted(self.root.iterdir(), key=lambda item: item.name):
            # Skip non-conforming names instead of following them.
            if not entry.is_dir() or not _SAFE_ID.fullmatch(entry.name):
                continue
            resolved = entry.resolve()
            if resolved.parent != self.root:
                continue
            result.append(resolved)
        return result

    def _result_files(self, run_dir: Path) -> list[Path]:
        result: list[Path] = []
        for entry in sorted(run_dir.glob("*.json"), key=lambda item: item.name):
            if not entry.is_file():
                continue
            if not _SAFE_ID.fullmatch(entry.stem):
                continue
            resolved = entry.resolve()
            # A symlink that escapes the run directory is not a judge result.
            if resolved.parent != run_dir:
                continue
            result.append(resolved)
        return result

    def _load_one(self, judge_run_id: str, case_id: str) -> dict[str, Any]:
        safe_id(judge_run_id, "judge_run_id")
        safe_id(case_id, "case_id")
        path = self.root / judge_run_id / f"{case_id}.json"
        resolved = path.resolve()
        expected_parent = (self.root / judge_run_id).resolve()
        if resolved != path or resolved.parent != expected_parent:
            raise JudgeContractError("path_escape", "结果路径越出白名单目录。")
        try:
            raw = resolved.read_text(encoding="utf-8")
        except OSError as error:
            raise JudgeContractError("unreadable_result", f"无法读取结果：{error}") from error
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise JudgeContractError("invalid_json", "结果不是合法 JSON。") from error
        return normalize_result(payload, judge_run_id=judge_run_id, case_id=case_id)

    def list_runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for run_dir in self._run_dirs():
            case_ids: list[str] = []
            malformed = 0
            statuses: dict[str, int] = {status: 0 for status in JUDGE_STATUSES}
            verdicts: dict[str, int] = {verdict: 0 for verdict in JUDGE_VERDICTS}
            provenance: dict[str, Any] = {}
            for file in self._result_files(run_dir):
                case_ids.append(file.stem)
                try:
                    result = self._load_one(run_dir.name, file.stem)
                except JudgeContractError:
                    malformed += 1
                    continue
                statuses[result["status"]] = statuses.get(result["status"], 0) + 1
                verdicts[result["verdict"]] = verdicts.get(result["verdict"], 0) + 1
                if not provenance:
                    provenance = result["provenance"]
            runs.append(
                {
                    "judgeRunId": run_dir.name,
                    "caseCount": len(case_ids),
                    "caseIds": case_ids,
                    "malformedCount": malformed,
                    "statuses": statuses,
                    "verdicts": verdicts,
                    "provenance": provenance,
                }
            )
        return runs

    def result_for(self, judge_run_id: str, case_id: str) -> dict[str, Any]:
        return self._load_one(judge_run_id, case_id)

    def resolve_for_case(
        self,
        case_id: str,
        run_id: str | None,
        current_case_hash: str,
    ) -> dict[str, Any]:
        """Find current judge results for one case/run, history kept separate."""
        safe_id(case_id, "case_id")
        if run_id is not None:
            safe_id(run_id, "run_id")
        candidates: list[dict[str, Any]] = []
        current_other_runs: list[dict[str, Any]] = []
        historical: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        runs = self.list_runs()
        for run in runs:
            if case_id not in run["caseIds"]:
                continue
            judge_run_id = run["judgeRunId"]
            try:
                result = self._load_one(judge_run_id, case_id)
            except JudgeContractError as error:
                errors.append(
                    {
                        "judgeRunId": judge_run_id,
                        "caseId": case_id,
                        "code": error.code,
                        "message": error.message,
                        "path": error.path,
                    }
                )
                continue
            result["historical"] = result["caseHash"] != current_case_hash
            result["runMatches"] = run_id is not None and result["runId"] == run_id
            result["scorePolicy"] = score_policy(
                result,
                current_case_hash=current_case_hash,
                run_id=run_id,
                historical=result["historical"],
            )
            if result["historical"]:
                historical.append(result)
            elif run_id is not None and result["runMatches"]:
                candidates.append(result)
            else:
                current_other_runs.append(result)
        # Deterministic newest-first ordering: explicit judge timestamp when
        # present, then judge_run_id; never filesystem enumeration order.
        def _order(item: dict[str, Any]) -> tuple[str, str]:
            return (item.get("finishedAt") or item.get("createdAt") or "", item["judgeRunId"])

        for bucket in (candidates, current_other_runs, historical):
            bucket.sort(key=_order, reverse=True)
        selected = candidates[0] if candidates and run_id is not None else None
        if selected is not None:
            selected["selection"] = {
                "rule": "run_id + current caseHash + newest judge timestamp; current candidates preferred",
                "candidateCount": len(candidates),
                "selectedJudgeRunId": selected["judgeRunId"],
            }
            message = None
        elif run_id is None:
            message = "请选择一个 run 后再查看 Judge 建议。"
        elif candidates == [] and current_other_runs:
            message = "存在 Judge 结果，但绑定的是其他 run；不作为当前 run 的建议。"
        elif historical:
            message = "存在 Judge 结果，但 run/caseHash 与当前选择不匹配；已标为历史，不作为当前建议。"
        elif errors:
            message = "存在 Judge 结果文件，但格式不符合 dsh-judge/v1；未当作通过。"
        else:
            message = "该 case + run 暂无 Judge 结果。"
        return {
            "ok": True,
            "caseId": case_id,
            "runId": run_id,
            "currentCaseHash": current_case_hash,
            "available": selected is not None,
            "selected": selected,
            "results": current_other_runs,
            "historical": historical,
            "errors": errors,
            "message": message,
            "selectionPolicy": "只接受 run_id 与当前 caseHash 匹配的结果；新旧按 judge 完成时间稳定排序。",
        }


__all__ = [
    "FINDING_KINDS",
    "INCLUDED_JUDGE_VERDICTS",
    "JUDGE_STATUSES",
    "JUDGE_VERDICTS",
    "JudgeContractError",
    "JudgeRepo",
    "SCHEMA_VERSION",
    "case_definition_hash",
    "normalize_result",
    "safe_id",
    "score_policy",
    "text_corpus",
    "verify_evidence_quotes",
]
