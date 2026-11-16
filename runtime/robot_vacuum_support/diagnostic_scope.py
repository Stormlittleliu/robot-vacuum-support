# -*- coding: utf-8 -*-
"""Diagnostic scope: observation -> capability scope -> hypothesis -> closure.

The semantic interpreter may propose a small number of *capability
observations* (what the user directly observed).  This module is the
deterministic authority for:

* capability / result enums and current-turn quote provenance;
* manual-vs-automatic scope separation;
* rejecting hypothetical, quoted-other-source and mislabelled observations;
* one effective observation per capability, supersession/retraction audit;
* an idempotent, bounded derived ``diagnostic_scope`` for the snapshot/prompt.

It does not write assistant text, does not score answers and does not try to
understand arbitrary language with keyword rules.  The explicit fallback only
covers the stable phrases used by the existing recharge flow when the semantic
layer is absent or fails.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from .actions import clause_scope_at, scoped_clauses

SCHEMA_VERSION = "diagnostic-scope/v1"
CAPABILITIES = ("manual_charging", "automatic_return", "dock_power")
RESULTS = ("works", "fails", "unknown")
RESULT_TO_POLARITY = {"works": "positive", "fails": "negative", "unknown": "unknown"}

#: Which capability a step-outcome question is actually measuring.  Used to
#: close a stale "现在能正常充上电吗" question from a known observation, and to
#: avoid re-asking the same measurement.
ACTION_CAPABILITY = {
    "clear_dock_obstacles": "manual_charging",
    "clean_contacts": "manual_charging",
    "reseat_dock": "manual_charging",
    "check_power": "dock_power",
    "check_adapter": "dock_power",
    "check_auto_return": "automatic_return",
    "clean_sensors": "automatic_return",
    "reset_robot": "automatic_return",
    "replace_dock": "automatic_return",
    "move_dock": "automatic_return",
}

CAPABILITY_LABELS = {
    "manual_charging": "手动放回充电",
    "automatic_return": "自动回充",
    "dock_power": "充电座供电",
}
RESULT_LABELS = {"works": "成功", "fails": "失败", "unknown": "结果未说明"}
TASK_GOAL_LABELS = {
    "manual_charging": "确认手动放回后能否充电",
    "automatic_return": "确认机器人能否自动回充",
    "dock_power": "确认充电座供电/指示灯状态",
    "unknown": "尚未确认任务目标",
}

_CLAUSE_SPLIT = re.compile(r"[，。；！？,.;!?]|但(?:是)?|不过|可是|只是|然而")
_HYPOTHETICAL = re.compile(
    r"如果|要是|假如|万一|是否|能否|能不能|可不可以|会不会|是不是|"
    r"怎么才能|何时|吗|嘛|的话"
)
_OTHER_SOURCE = re.compile(
    r"别人|他人|朋友|家人|客服|售后|网上|论坛|说明书|资料|据说|听说|"
    r"有人说|卖家|商家|视频|帖子里"
)
_CORRECTION = re.compile(r"说错了|说错|收回|改口|更正|纠正|不是.{0,6}是|刚才说错")
_NEGATED_EXISTENCE = re.compile(r"没有|没|未|不是|并非|从未")

_MANUAL_CUE = re.compile(
    r"手动|我把它放|把它放|放回|放上|放到|摆到|搁到|按到|搬上|抱上"
)
_AUTO_CUE = re.compile(
    r"自动|自己回|自己找|从房间|回充模式|(?<!放)回充(?!传感器)|自己回到|自动找到|自己开回"
)
_DOCK_POWER_CUE = re.compile(
    r"充电座.{0,10}(?:指示灯|供电|电源|通电|适配器)|"
    r"(?:指示灯|供电|电源适配器|适配器).{0,10}(?:亮|不亮|正常|异常|坏|没电)?"
)
_CAPABILITY_CUES = {
    "manual_charging": _MANUAL_CUE,
    "automatic_return": _AUTO_CUE,
    "dock_power": _DOCK_POWER_CUE,
}

_POS_RESULT = re.compile(
    r"(?<![不没])能充|(?<![不没])可以充|(?<![不])充电正常|充上电|充上了|充进了|"
    r"(?<![不没])能回充|(?<![不])回充正常|(?<![没不])开始充电|"
    r"(?<![不没])能自己回|找到充电座|(?<![不没])成功|(?<![不])正常了|"
    r"(?<![不没未])恢复了|(?<![不])好了|已经开始充电"
)
_NEG_RESULT = re.compile(
    r"充不上|充不进|充不了|不能充|无法充|不可以充|没充上|没有充上|没充电|不充电|"
    r"没反应|没有反应|回不到|回不了|找不到充电座|回不去|失败|不行|"
    r"不能回充|无法回充|没成功|不成功|没恢复|未恢复|没找到|不工作|"
    r"不亮|没电|不通电|坏了|故障"
)

_CAUSE_CANDIDATES = {
    "automatic_return": (
        ("return_sensor", "回充传感器/信号"),
        ("dock_signal", "充电座红外信号"),
        ("obstruction_path", "遮挡/回充路径"),
    ),
    "manual_charging": (
        ("charging_contacts", "充电触点"),
        ("dock_power", "充电座供电"),
        ("battery", "电池/适配器"),
    ),
    "dock_power": (
        ("dock_power", "充电座供电"),
        ("adapter", "电源适配器"),
    ),
}


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_SPLIT.split(str(text or "")) if part.strip()]


def _result_kind(clause: str) -> str:
    """Return works/fails/unknown for one clause, with negation precedence."""
    value = str(clause or "")
    if not value:
        return "unknown"
    positive = list(_POS_RESULT.finditer(value))
    negative = list(_NEG_RESULT.finditer(value))
    if not positive and not negative:
        return "unknown"
    if positive and not negative:
        for match in positive:
            if _NEGATED_EXISTENCE.search(value[: match.start()]):
                return "fails"
        return "works"
    if negative and not positive:
        return "fails"
    last_positive = positive[-1].start()
    last_negative = negative[-1].start()
    if last_positive > last_negative:
        for match in positive:
            if _NEGATED_EXISTENCE.search(value[: match.start()]):
                return "fails"
        return "works"
    return "fails"


def _invalid_clause(clause: str) -> str | None:
    if _HYPOTHETICAL.search(clause):
        return "hypothetical_or_question"
    if _OTHER_SOURCE.search(clause):
        return "other_source_not_user_observation"
    return None


def _capability_cue_conflict(capability: str, clause: str) -> str | None:
    """Reject manual placement labelled as automatic success and vice versa."""
    manual = bool(_MANUAL_CUE.search(clause))
    automatic = bool(_AUTO_CUE.search(clause))
    if capability == "automatic_return" and manual and not automatic:
        return "manual_scope_labelled_automatic"
    if capability == "manual_charging" and automatic and not manual:
        return "automatic_scope_labelled_manual"
    return None


def _span_between(text: str, first: str, second: str) -> str | None:
    first_at = text.find(first)
    second_at = text.find(second)
    if first_at < 0 or second_at < 0:
        return None
    start = min(first_at, second_at)
    end = max(first_at + len(first), second_at + len(second))
    return text[start:end]


def _has_any_capability_cue(clause: str) -> bool:
    return any(pattern.search(clause) for pattern in _CAPABILITY_CUES.values())


def _resolve_capability_clause(capability: str, result: str, text: str) -> str | None:
    """Find the capability cue and, if needed, an adjacent explicit result."""
    parts = _clauses(text)
    cue = _CAPABILITY_CUES[capability]
    for index, part in enumerate(parts):
        if not cue.search(part):
            continue
        if result == "unknown":
            return part
        if _result_kind(part) == result:
            return part
        # The user can put the capability in one comma clause and the result in
        # the next ("我把它放回充电座，充上电了").  Only merge an adjacent clause
        # that has the same result and no competing capability cue.
        for neighbour in (index + 1, index - 1):
            if neighbour < 0 or neighbour >= len(parts):
                continue
            candidate = parts[neighbour]
            if _has_any_capability_cue(candidate):
                continue
            if _result_kind(candidate) != result:
                continue
            combined = _span_between(text, part, candidate)
            if combined:
                return combined
    return None


def _refine_quote(capability: str, result: str, quote: str) -> str | None:
    """Choose the exact clause/span that carries the capability and result."""
    value = str(quote or "")
    if not value:
        return None
    chosen = _resolve_capability_clause(capability, result, value)
    return chosen if chosen and chosen in value else None


def _canonical_quote(item: dict[str, Any]) -> tuple[str | None, str | None]:
    """Accept canonical ``evidence_quote`` or the normalized ``quote`` alias.

    A normalized observation carries both fields; re-normalizing it must be a
    no-op.  If both fields are present and disagree we reject rather than
    choosing one, and we never fill a missing quote from the assistant or from
    some other turn.
    """
    evidence = item.get("evidence_quote")
    quote = item.get("quote")
    evidence_text = str(evidence) if evidence is not None else ""
    quote_text = str(quote) if quote is not None else ""
    if evidence_text and quote_text and evidence_text != quote_text:
        return None, "conflicting_evidence_quote_and_quote"
    value = evidence_text or quote_text
    if not value.strip():
        return None, "missing_evidence_quote"
    return value, None


def validate_proposal_observation(item: Any, text: str) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(item, dict):
        return None, "observation entry is not an object"
    capability = str(item.get("capability") or "").strip().lower()
    if capability not in CAPABILITIES:
        return None, f"capability {capability!r} not allowed"
    result = str(item.get("result") or "").strip().lower()
    if result not in RESULTS:
        return None, f"result {result!r} not allowed"
    source = str(item.get("source") or "user_message").strip().lower()
    if source != "user_message":
        return None, "observation source is not user_message"
    quote, quote_error = _canonical_quote(item)
    if quote_error:
        return None, quote_error
    assert quote is not None
    if quote not in str(text or ""):
        return None, "quote_not_in_current_message"
    clause = _refine_quote(capability, result, quote)
    if clause is None:
        return None, "quote_capability_or_result_mismatch"
    inherited_scope = clause_scope_at(str(text or ""), str(text or "").find(clause))
    if inherited_scope:
        return None, "source_scope_not_current_user"
    invalid = _invalid_clause(clause)
    if invalid:
        return None, invalid
    conflict = _capability_cue_conflict(capability, clause)
    if conflict:
        return None, conflict
    return {
        "capability": capability,
        "result": result,
        "evidence_quote": clause,
        "quote": clause,
        "source": "user_message",
        "extraction": "semantic",
    }, None


def normalize_proposal_observations(items: Any, text: str) -> tuple[list[dict[str, Any]], list[str]]:
    observations: list[dict[str, Any]] = []
    errors: list[str] = []
    if items is None:
        return observations, errors
    if not isinstance(items, (list, tuple)):
        return observations, ["capability_observations is not a list"]
    for item in items:
        observation, error = validate_proposal_observation(item, text)
        if observation is not None:
            observations.append(observation)
        elif error:
            errors.append("capability_observation: " + error)
    return observations, errors


def normalize_retractions(items: Any, text: str) -> tuple[list[dict[str, Any]], list[str]]:
    retractions: list[dict[str, Any]] = []
    errors: list[str] = []
    if items is None:
        return retractions, errors
    if not isinstance(items, (list, tuple)):
        return retractions, ["observation_retractions is not a list"]
    value = str(text or "")
    for item in items:
        if not isinstance(item, dict):
            errors.append("observation_retraction: entry is not an object")
            continue
        capability = str(item.get("capability") or "").strip().lower()
        if capability not in CAPABILITIES:
            errors.append(f"observation_retraction: capability {capability!r} not allowed")
            continue
        quote, quote_error = _canonical_quote(item)
        if quote_error:
            errors.append("observation_retraction: " + quote_error)
            continue
        assert quote is not None
        if quote not in value:
            errors.append("observation_retraction: quote_not_in_current_message")
            continue
        if not (_CORRECTION.search(value) or _NEGATED_EXISTENCE.search(quote)):
            errors.append("observation_retraction: no correction/negation evidence")
            continue
        clause = _refine_quote(capability, "unknown", quote)
        if clause is None:
            errors.append("observation_retraction: quote_capability_mismatch")
            continue
        retractions.append(
            {"capability": capability, "evidence_quote": clause, "quote": clause}
        )
    return retractions, errors


def fallback_observations(text: str) -> list[dict[str, Any]]:
    """Only stable explicit phrases; never a general language-understanding layer."""
    observations: list[dict[str, Any]] = []
    value = str(text or "")
    parts = scoped_clauses(value)
    for index, part_info in enumerate(parts):
        if part_info.get("blocked"):
            continue
        part = str(part_info.get("text") or "")
        if _invalid_clause(part):
            continue
        for capability in CAPABILITIES:
            if not _CAPABILITY_CUES[capability].search(part):
                continue
            result = _result_kind(part)
            quote = part
            if result == "unknown":
                for neighbour in (index + 1, index - 1):
                    if neighbour < 0 or neighbour >= len(parts):
                        continue
                    neighbour_info = parts[neighbour]
                    if neighbour_info.get("blocked"):
                        continue
                    candidate = str(neighbour_info.get("text") or "")
                    if _has_any_capability_cue(candidate):
                        continue
                    neighbour_result = _result_kind(candidate)
                    if neighbour_result == "unknown":
                        continue
                    combined = _span_between(value, part, candidate)
                    if combined:
                        result = neighbour_result
                        quote = combined
                    break
            if result == "unknown":
                continue
            if _invalid_clause(quote):
                continue
            quote_scope = clause_scope_at(value, value.find(quote))
            if quote_scope:
                continue
            conflict = _capability_cue_conflict(capability, quote)
            if conflict:
                continue
            observations.append(
                {
                    "capability": capability,
                    "result": result,
                    "evidence_quote": quote,
                    "quote": quote,
                    "source": "user_message",
                    "extraction": "fallback",
                }
            )
    return observations


def fallback_retractions(text: str) -> list[str]:
    if not _CORRECTION.search(str(text or "")):
        return []
    capabilities: list[str] = []
    for part in scoped_clauses(text):
        if part.get("blocked"):
            continue
        clause = str(part.get("text") or "")
        if _invalid_clause(clause):
            continue
        if _result_kind(clause) in {"works", "fails"}:
            continue
        if not _NEGATED_EXISTENCE.search(clause):
            continue
        for capability in CAPABILITIES:
            if _CAPABILITY_CUES[capability].search(clause) and capability not in capabilities:
                capabilities.append(capability)
    return capabilities


def _observation_id(case: dict[str, Any], observation: dict[str, Any], message_id: str | None, turn: int) -> str:
    raw = "|".join(
        [
            str(case.get("case_id") or ""),
            str(observation.get("capability") or ""),
            str(observation.get("result") or ""),
            str(observation.get("quote") or ""),
            str(message_id or ""),
            str(turn),
        ]
    )
    return "obs-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def migrate_case(case: dict[str, Any]) -> None:
    """Additive compatibility; never rewrite existing user facts."""
    case.setdefault("observations", [])
    case.setdefault("observation_audit", [])
    effective_seen: set[str] = set()
    rows = []
    for row in case.get("observations") or []:
        if not isinstance(row, dict):
            continue
        row.setdefault("fact_status", "effective")
        row.setdefault("polarity", RESULT_TO_POLARITY.get(str(row.get("result")), "unknown"))
        row.setdefault("source", "user_message")
        row.setdefault("extraction", "unknown")
        row.setdefault("quote", "")
        row.setdefault("turn", None)
        row.setdefault("message_id", None)
        # Defensive legacy reconciliation: never leave two effective rows for
        # one capability.  Keep the later one, retain the older as superseded.
        if str(row.get("fact_status")) == "effective":
            capability = str(row.get("capability") or "")
            if capability in effective_seen:
                row["fact_status"] = "superseded"
                row["supersede_reason"] = "legacy_duplicate_capability_observation"
            else:
                effective_seen.add(capability)
        rows.append(row)
    case["observations"] = rows
    if not isinstance(case.get("observation_audit"), list):
        case["observation_audit"] = []


def _append_audit(
    case: dict[str, Any],
    *,
    kind: str,
    observation: dict[str, Any],
    message_id: str | None,
    turn: int,
    quote: str,
    reason: str,
    now: float,
    replaced_fact: dict[str, Any] | None = None,
) -> None:
    audit = case.setdefault("observation_audit", [])
    key = (
        str(kind),
        str(observation.get("capability") or ""),
        str(message_id or ""),
        str(quote or ""),
    )
    for row in audit:
        if not isinstance(row, dict):
            continue
        if (
            str(row.get("kind")),
            str(row.get("capability") or ""),
            str(row.get("message_id") or ""),
            str(row.get("quote") or ""),
        ) == key:
            return
    audit.append(
        {
            "kind": kind,
            "observation_id": observation.get("observation_id"),
            "capability": observation.get("capability"),
            "result": observation.get("result"),
            "message_id": message_id,
            "turn": turn,
            "quote": quote,
            "reason": reason,
            "replaced_fact": copy.deepcopy(replaced_fact),
            "at": now,
        }
    )


def _effective_for(case: dict[str, Any], capability: str) -> dict[str, Any] | None:
    for row in reversed(case.get("observations") or []):
        if (
            isinstance(row, dict)
            and str(row.get("capability")) == capability
            and str(row.get("fact_status") or "effective") == "effective"
        ):
            return row
    return None


def _record_observation(
    case: dict[str, Any],
    observation: dict[str, Any],
    message_id: str | None,
    turn: int,
    now: float,
) -> dict[str, Any]:
    capability = str(observation.get("capability"))
    result = str(observation.get("result"))
    quote = str(observation.get("quote"))
    current = _effective_for(case, capability)
    if current is not None and (
        str(current.get("message_id") or "") == str(message_id or "")
        and str(current.get("quote") or "") == quote
        and str(current.get("result") or "") == result
    ):
        return {"recorded": False, "reason": "duplicate_same_message"}
    row = {
        "observation_id": _observation_id(case, observation, message_id, turn),
        "capability": capability,
        "result": result,
        "polarity": RESULT_TO_POLARITY.get(result, "unknown"),
        "quote": quote,
        "message_id": message_id,
        "turn": turn,
        "source": "user_message",
        "extraction": str(observation.get("extraction") or "unknown"),
        "fact_status": "effective",
        "recorded_at": now,
        "updated_at": now,
        "superseded_turn": None,
        "retracted_turn": None,
        "retraction_reason": None,
    }
    if current is not None:
        current["fact_status"] = "superseded"
        current["superseded_turn"] = turn
        current["supersede_reason"] = "newer_capability_observation"
        current["updated_at"] = now
        _append_audit(
            case,
            kind="supersede_observation",
            observation=current,
            message_id=message_id,
            turn=turn,
            quote=quote,
            reason="newer_capability_observation",
            now=now,
            replaced_fact=current,
        )
    case.setdefault("observations", []).append(row)
    _append_audit(
        case,
        kind="record",
        observation=row,
        message_id=message_id,
        turn=turn,
        quote=quote,
        reason="validated_user_observation",
        now=now,
    )
    return {"recorded": True, "observation_id": row["observation_id"], "superseded": current is not None}


def _retract_capability(
    case: dict[str, Any],
    capability: str,
    message_id: str | None,
    turn: int,
    quote: str,
    now: float,
) -> dict[str, Any]:
    current = _effective_for(case, capability)
    if current is None:
        return {"retracted": False, "reason": "no_effective_observation"}
    current["fact_status"] = "retracted"
    current["retracted_turn"] = turn
    current["retraction_reason"] = "user_retraction"
    current["updated_at"] = now
    _append_audit(
        case,
        kind="retract_observation",
        observation=current,
        message_id=message_id,
        turn=turn,
        quote=quote,
        reason="user_retraction",
        now=now,
        replaced_fact=current,
    )
    return {"retracted": True, "observation_id": current.get("observation_id")}


def _merge_candidates(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    merged: dict[str, dict[str, Any]] = {}
    ambiguous: list[dict[str, Any]] = []
    for observation in candidates:
        capability = str(observation.get("capability"))
        result = str(observation.get("result"))
        if capability not in merged:
            merged[capability] = observation
            continue
        previous = merged[capability]
        if str(previous.get("result")) == result:
            continue
        # Same-turn contradiction: keep one conservative unknown observation
        # rather than selecting whichever extractor happened to run last.
        ambiguous.append(
            {"capability": capability, "results": [previous.get("result"), result]}
        )
        merged[capability] = {
            "capability": capability,
            "result": "unknown",
            "quote": str(previous.get("quote") or observation.get("quote") or ""),
            "source": "user_message",
            "extraction": "mixed",
        }
    return list(merged.values()), ambiguous


def apply_turn(
    case: dict[str, Any],
    text: str,
    message_id: str | None,
    turn: int,
    now: float,
    *,
    proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and reduce this turn's observations; return a compact result."""
    migrate_case(case)
    result: dict[str, Any] = {
        "recorded": [],
        "rejected": [],
        "retracted": [],
        "superseded": [],
        "ambiguous": [],
    }
    proposal = proposal if isinstance(proposal, dict) else {}
    retractions, retraction_errors = normalize_retractions(
        proposal.get("observation_retractions"), text
    )
    for error in retraction_errors:
        result["rejected"].append({"stage": "observation_retraction", "error": error})
    retract_caps = {item["capability"] for item in retractions}
    retract_caps.update(fallback_retractions(text))
    for capability in sorted(retract_caps):
        outcome = _retract_capability(case, capability, message_id, turn, text, now)
        if outcome.get("retracted"):
            result["retracted"].append(outcome.get("observation_id"))

    candidates, observation_errors = normalize_proposal_observations(
        proposal.get("capability_observations"), text
    )
    for error in observation_errors:
        result["rejected"].append({"stage": "capability_observation", "error": error})
    candidates.extend(fallback_observations(text))
    merged, ambiguous = _merge_candidates(candidates)
    result["ambiguous"].extend(ambiguous)
    for observation in merged:
        outcome = _record_observation(case, observation, message_id, turn, now)
        if outcome.get("recorded"):
            result["recorded"].append(outcome.get("observation_id"))
        if outcome.get("superseded"):
            result["superseded"].append(observation.get("capability"))
    for rejected in result["rejected"]:
        _append_audit(
            case,
            kind="reject_observation",
            observation={"capability": "", "result": "", "observation_id": None},
            message_id=message_id,
            turn=turn,
            quote=str(rejected.get("error") or ""),
            reason=str(rejected.get("error") or "validation_error"),
            now=now,
        )
    case["last_observation_result"] = copy.deepcopy(result)
    return result


def infer_task_goal(case: dict[str, Any]) -> tuple[str, str]:
    issue_value = case.get("issue")
    if isinstance(issue_value, dict):
        issue = str(issue_value.get("text") or "")
        reported = str(issue_value.get("reported_text") or "")
    else:
        issue = str(issue_value or "")
        reported = issue
    context = case.get("work_context") or {}
    symptom = str((context.get("observable_symptom") or {}).get("text") or "")
    text = " ".join(item for item in (issue, reported, symptom) if item)
    if _AUTO_CUE.search(text):
        return "automatic_return", issue or reported
    if _DOCK_POWER_CUE.search(text):
        return "dock_power", issue or reported
    if _MANUAL_CUE.search(text):
        return "manual_charging", issue or reported
    return "automatic_return", issue or reported


def _effective_observations(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in case.get("observations") or []
        if isinstance(row, dict) and str(row.get("fact_status") or "effective") == "effective"
    ]


def effective_by_capability(case: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(case, dict):
        return {}
    return {str(row.get("capability")): row for row in _effective_observations(case)}


def derive(case: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(case, dict):
        return None
    observations = _effective_observations(case)
    goal, goal_quote = infer_task_goal(case)
    by_capability = {str(row.get("capability")): row for row in observations}
    goal_row = by_capability.get(goal)
    goal_status = str((goal_row or {}).get("result") or "unknown")
    verified = [
        {
            "capability": row.get("capability"),
            "result": row.get("result"),
            "observation_id": row.get("observation_id"),
            "turn": row.get("turn"),
            "quote": row.get("quote"),
        }
        for row in observations
    ]
    unverified: list[str] = []
    if goal_status not in {"works", "fails"}:
        unverified.append(goal)
    if goal in {"automatic_return", "manual_charging"}:
        other = "manual_charging" if goal == "automatic_return" else "automatic_return"
        if other not in by_capability:
            unverified.append(other)
    if "dock_power" not in by_capability and any(
        str(row.get("result")) == "fails" for row in observations
    ):
        unverified.append("dock_power")
    unverified = [item for item in unverified if item not in {row.get("capability") for row in observations}]

    if goal_status == "works":
        strength = "resolved"
    elif goal_status == "fails":
        strength = "task_goal_failed"
    elif any(str(row.get("result")) == "works" for row in observations):
        strength = "scoped_positive_only"
    else:
        strength = "insufficient"

    candidates: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for row in observations:
        if str(row.get("result")) != "fails":
            continue
        for candidate_id, label in _CAUSE_CANDIDATES.get(str(row.get("capability")), ()):
            if candidate_id in seen_candidates:
                continue
            seen_candidates.add(candidate_id)
            candidates.append(
                {
                    "id": candidate_id,
                    "label": label,
                    "status": "hypothesis",
                    "reason": f"{CAPABILITY_LABELS.get(str(row.get('capability')), str(row.get('capability')))}失败，但原因未证实",
                }
            )

    still_needed: list[str] = []
    if goal_status != "works":
        if goal_status == "unknown":
            still_needed.append(
                {
                    "automatic_return": "在自动回充模式下观察机器人能否自己回到充电座并开始充电。",
                    "manual_charging": "手动把机器人放回充电座，观察是否能充电。",
                    "dock_power": "确认充电座是否通电、指示灯是否亮起。",
                    "unknown": "先确认当前要验证的具体能力，再给一个可观察的下一步。",
                }.get(goal, "先确认当前要验证的具体能力。")
            )
        elif goal == "automatic_return":
            if "manual_charging" not in by_capability:
                still_needed.append("手动把机器人放回充电座观察能否充电（只用于区分充电通路与自动回充）。")
            if "dock_power" not in by_capability:
                still_needed.append("确认充电座供电/指示灯状态（独立观察，不据此断言原因）。")
        elif goal == "manual_charging" and "dock_power" not in by_capability:
            still_needed.append("确认充电座供电/指示灯状态（独立观察，不据此断言原因）。")
    # Deduplicate by meaning without changing the order.
    deduped: list[str] = []
    for item in still_needed:
        if item not in deduped:
            deduped.append(item)
    still_needed = deduped

    payload = {
        "schema_version": SCHEMA_VERSION,
        "scope_id": None,
        "task_goal": goal,
        "goal_quote": goal_quote,
        "task_goal_status": goal_status,
        "verified_capabilities": verified,
        "unverified_capabilities": unverified,
        "allowed_conclusion_strength": strength,
        "root_cause": {"status": "unconfirmed", "candidates": candidates},
        "still_needed": still_needed,
        "resolved": goal_status == "works",
        "source_observation_ids": [row.get("observation_id") for row in observations],
    }
    canonical = hashlib.sha256(
        repr(
            (
                goal,
                goal_status,
                strength,
                [(row.get("capability"), row.get("result"), row.get("observation_id")) for row in observations],
            )
        ).encode("utf-8")
    ).hexdigest()[:12]
    payload["scope_id"] = "dscope-" + canonical
    return payload


def _new_intervention(case: dict[str, Any], turn: int, action_ids: set[str]) -> bool:
    for row in case.get("tried_actions") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("fact_status") or "effective") != "effective":
            continue
        if int(row.get("source_turn") or 0) != int(turn):
            continue
        if str(row.get("action_id")) in action_ids:
            return True
    return False


def _observation_step(capability: str, reason: str) -> dict[str, Any]:
    detail = {
        "automatic_return": "让机器人在自动回充模式下自己回充电座，观察它能否自己找到充电座并开始充电。",
        "manual_charging": "手动把机器人放回充电座，观察是否能充电。",
        "dock_power": "确认充电座是否通电、指示灯是否亮起（独立观察，不据此断言原因）。",
    }[capability]
    if reason in {"after_new_intervention", "retest_after_new_intervention"}:
        detail = "你本轮做了可能影响该能力的操作；现在重新观察：" + detail
    return {
        "capability": capability,
        "label": {
            "automatic_return": "观察自动回充是否恢复",
            "manual_charging": "观察手动充电结果",
            "dock_power": "确认充电座供电",
        }[capability],
        "detail": detail,
        "reason": reason,
    }


def next_observation(
    scope: dict[str, Any] | None,
    case: dict[str, Any],
    turn: int | None = None,
) -> dict[str, Any] | None:
    """Choose the next observed capability without repeating a known measurement."""
    if not isinstance(scope, dict) or scope.get("resolved"):
        return None
    by_capability = effective_by_capability(case)
    goal = str(scope.get("task_goal") or "automatic_return")
    current_turn = int(turn or 0)
    if goal == "automatic_return":
        auto = by_capability.get("automatic_return") or {}
        if not auto:
            reason = (
                "after_new_intervention"
                if _new_intervention(
                    case,
                    current_turn,
                    {
                        "clear_dock_obstacles",
                        "clean_sensors",
                        "move_dock",
                        "replace_dock",
                        "reset_robot",
                    },
                )
                else "task_goal_unverified"
            )
            return _observation_step("automatic_return", reason)
        if str(auto.get("result")) == "works":
            return None
        if _new_intervention(
            case,
            current_turn,
            {
                "clear_dock_obstacles",
                "clean_sensors",
                "move_dock",
                "replace_dock",
                "reset_robot",
            },
        ):
            return _observation_step("automatic_return", "retest_after_new_intervention")
        if "manual_charging" not in by_capability:
            reason = (
                "retest_after_new_intervention"
                if _new_intervention(case, current_turn, {"clean_contacts", "reseat_dock"})
                else "discriminate_manual_path"
            )
            return _observation_step("manual_charging", reason)
        if "dock_power" not in by_capability:
            return _observation_step("dock_power", "supply_unverified")
        return None
    if goal == "manual_charging":
        manual = by_capability.get("manual_charging") or {}
        if not manual:
            reason = (
                "after_new_intervention"
                if _new_intervention(case, current_turn, {"clean_contacts", "reseat_dock"})
                else "task_goal_unverified"
            )
            return _observation_step("manual_charging", reason)
        if str(manual.get("result")) == "works":
            return None
        if _new_intervention(case, current_turn, {"clean_contacts", "reseat_dock"}):
            return _observation_step("manual_charging", "retest_after_new_intervention")
        if "dock_power" not in by_capability:
            return _observation_step("dock_power", "supply_unverified")
        return None
    if goal == "dock_power":
        if "dock_power" not in by_capability:
            return _observation_step("dock_power", "task_goal_unverified")
    return None


def observations_summary(case: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(case, dict):
        return []
    return [
        {
            "observation_id": row.get("observation_id"),
            "capability": row.get("capability"),
            "result": row.get("result"),
            "polarity": row.get("polarity") or RESULT_TO_POLARITY.get(str(row.get("result")), "unknown"),
            "quote": row.get("quote"),
            "message_id": row.get("message_id"),
            "turn": row.get("turn"),
            "source": row.get("source") or "user_message",
            "extraction": row.get("extraction") or "unknown",
            "fact_status": row.get("fact_status") or "effective",
        }
        for row in _effective_observations(case)
    ]


def render_prompt_block(scope: dict[str, Any] | None) -> str:
    """Stable evidence-strength context derived from the snapshot scope."""
    if not isinstance(scope, dict):
        return ""
    lines = ["【诊断范围｜观察 -> 能力范围 -> 诊断假设 -> 全局结案】"]
    lines.append(
        "当前任务目标：{goal}（{quote}）".format(
            goal=TASK_GOAL_LABELS.get(str(scope.get("task_goal")), str(scope.get("task_goal"))),
            quote=str(scope.get("goal_quote") or "未提供")[:80],
        )
    )
    verified = scope.get("verified_capabilities") or []
    if verified:
        lines.append("已观察到的能力（每条只在其能力范围内有效）：")
        for row in verified:
            label = CAPABILITY_LABELS.get(str(row.get("capability")), str(row.get("capability")))
            result = RESULT_LABELS.get(str(row.get("result")), str(row.get("result")))
            lines.append(
                "- {}：{}（用户第{}轮，原文：“{}”）".format(
                    label, result, row.get("turn"), str(row.get("quote") or "")[:80]
                )
            )
    else:
        lines.append("已观察到的能力：暂无可用的结构化观察。")
    unverified = scope.get("unverified_capabilities") or []
    if unverified:
        lines.append(
            "仍未验证："
            + "、".join(CAPABILITY_LABELS.get(str(item), str(item)) for item in unverified)
        )
    strength = str(scope.get("allowed_conclusion_strength") or "insufficient")
    lines.append(
        "允许的结论强度："
        + {
            "resolved": "当前任务目标能力已明确成功，可以结案；仍不得推断未验证的故障原因。",
            "task_goal_failed": "当前任务目标能力明确失败；可指出该能力仍未恢复，但原因仍未证实。",
            "scoped_positive_only": "只有局部能力 positive；不能据此断言全局恢复或排他原因，当前任务仍未结案。",
            "insufficient": "证据不足；只能追问或给一个鉴别观察，不能宣称已解决或已定位原因。",
        }.get(strength, "证据不足。")
    )
    root_cause = scope.get("root_cause") or {}
    candidates = root_cause.get("candidates") or []
    lines.append("root cause：" + ("unconfirmed（候选只是假设）" if root_cause.get("status") == "unconfirmed" else str(root_cause.get("status"))))
    if candidates:
        lines.append(
            "诊断假设（不是事实）："
            + "、".join(
                "{}（{}）".format(item.get("label"), item.get("status")) for item in candidates
            )
            + "。不要把候选原因写成“就是/唯一原因/已经解决”。"
        )
    else:
        lines.append("诊断假设：暂无由失败能力派生的候选；不要把任何原因写成“就是”。")
    still_needed = scope.get("still_needed") or []
    if still_needed:
        lines.append("仍需观察：")
        for item in still_needed[:3]:
            lines.append("- " + str(item))
    lines.append(
        "通用证据原则：手动放上去能充只说明该局部能力，不能据此断言自动回充恢复、触点/遮挡是唯一原因或“不用管”；"
        "只有当前任务目标能力明确成功才可结案。有证据的明确判断仍要明确，不要把已证实事实故意说模糊。"
    )
    return "\n".join(lines)


def success_in_scope(scope: dict[str, Any] | None, text: str) -> bool:
    """Whether an explicit success may close the *current task goal*."""
    if not isinstance(scope, dict):
        return True
    if str(scope.get("task_goal_status")) == "works":
        return True
    goal = str(scope.get("task_goal") or "automatic_return")
    value = str(text or "")
    if goal == "automatic_return":
        # Manual placement/charging is a different capability.  A later semantic
        # closure_signal=explicit_success cannot turn it into automatic recovery.
        if _MANUAL_CUE.search(value) and not _AUTO_CUE.search(value):
            return False
        for row in scope.get("verified_capabilities") or []:
            if str(row.get("capability")) == "manual_charging" and str(row.get("result")) == "works":
                return False
    if goal in {"manual_charging", "dock_power"}:
        for row in scope.get("verified_capabilities") or []:
            if str(row.get("capability")) != goal and str(row.get("result")) == "works":
                return False
    return True
