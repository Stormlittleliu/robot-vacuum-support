# -*- coding: utf-8 -*-
"""Case-scoped pending-question lifecycle.

A question is a small record attached to one case.  It exists so the prompt and
the answer generator share one decision: *which* clarification, if any, may be
asked this turn.  Records are deliberately narrow; this is not a task platform.

Core rules (see docs/question-lifecycle.md for the oracle):

* asking is not answering; silence and a weak "谢谢" never resolve a question;
* a topic switch defers pending questions instead of re-asking them every turn;
* an answered/deferred question can be reopened only with user evidence;
* when a predefined slot is satisfied by confirmed facts, the question becomes
  answered without another model call;
* the assistant's actual answer text is audited, so "the program didn't record a
  question" can never be used to claim the model did not ask one.
"""
from __future__ import annotations

import copy
import re
import time
import uuid
from typing import Any

from .diagnostic_scope import ACTION_CAPABILITY, effective_by_capability

QUESTION_SCHEMA_VERSION = "case-question/v1"
QUESTION_STATUSES = ("pending", "answered", "deferred", "superseded", "cancelled")
QUESTION_SOURCES = (
    "ambiguity",
    "clarify",
    "confirm",
    "weak_closure",
    "capability_observation",
    "policy",
    "legacy_migration",
)

#: A question may be asked at most this many times without being answered.
MAX_ASKS = 2
#: Do not immediately re-ask on the very next turn; wait a small gap.
REASK_MIN_GAP = 2

_QUESTION_CUE = re.compile(r"[?？]|请告诉我|想问一下|确认一下|还是说|哪一个|哪一步|哪种")
_QUESTION_SENTENCE = re.compile(r"[^?？]{2,160}[?？]")
_DOCK_BOTH = re.compile(
    r"(?:机器人|机器|它).{0,40}(?:回不去|回不了|回不到|打转|对不上|找不到充电座)"
)
_DOCK_USER = re.compile(
    r"(?:您|你|自己).{0,30}(?:找不到|不知道).{0,12}(?:充电座|基座|座|位置|放哪)|"
    r"(?:充电座|基座).{0,12}(?:位置|放哪|找不到)|"
    r"找不到.{0,6}(?:充电座|基座)"
)
_DOCK_ASK = re.compile(r"确认|想问|还是|哪个|哪种|找不到充电座|回不到充电座")
_RESULT_ASK = re.compile(r"结果|怎么样|正常|恢复|充上|回充|还是|哪一步")
_WEAK_ASK = re.compile(r"现在能|解决了吗|恢复了吗|好(?:了)?吗|怎么样")
_STEP_ASK = re.compile(r"哪一步|哪个操作|实际试")


def _case_questions(case: dict[str, Any]) -> list[dict[str, Any]]:
    rows = case.setdefault("questions", [])
    if not isinstance(rows, list):
        rows = []
        case["questions"] = rows
    return rows


def _now(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


def new_question(
    *,
    slot: str,
    target_case: str,
    source: str,
    question_text: str,
    turn: int | None = None,
    evidence: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": QUESTION_SCHEMA_VERSION,
        "question_id": "q-" + uuid.uuid4().hex[:10],
        "slot": str(slot),
        "target_case": str(target_case),
        "status": "pending",
        "source": source if source in QUESTION_SOURCES else "policy",
        "question_text": str(question_text or ""),
        "evidence": [copy.deepcopy(evidence)] if evidence else [],
        "created_turn": turn,
        "asked_turn": None,
        "last_asked_turn": None,
        "ask_count": 0,
        "answered_turn": None,
        "resolved_turn": None,
        "answer_value": None,
        "answer_evidence": None,
        "deferred_turn": None,
        "defer_reason": None,
        "reopen_reason": None,
        "reopen_evidence": None,
        "updated_at": _now(now),
    }


def find_question(case: dict[str, Any], slot: str) -> dict[str, Any] | None:
    rows = [row for row in _case_questions(case) if row.get("slot") == slot]
    return rows[-1] if rows else None


def _supersede_other_pending(
    case: dict[str, Any], keep_slot: str, turn: int | None, reason: str
) -> None:
    for row in _case_questions(case):
        if row.get("slot") == keep_slot:
            continue
        if row.get("status") == "pending":
            row["status"] = "superseded"
            row["superseded_turn"] = turn
            row["supersede_reason"] = reason
            row["updated_at"] = _now()


def ensure_question(
    case: dict[str, Any],
    *,
    slot: str,
    source: str,
    question_text: str,
    turn: int | None,
    evidence: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Create or return the live question for ``slot`` without asking it yet."""
    existing = find_question(case, slot)
    if existing is not None and existing.get("status") in {"pending", "deferred"}:
        if question_text and existing.get("question_text") != question_text:
            existing["question_text"] = str(question_text)
        if evidence:
            existing.setdefault("evidence", []).append(copy.deepcopy(evidence))
            del existing["evidence"][:-8]
        existing["updated_at"] = _now(now)
        return existing
    if existing is not None and existing.get("status") == "answered":
        # Known facts already answer this slot; do not silently reopen it.
        return existing
    row = new_question(
        slot=slot,
        target_case=str(case.get("case_id") or ""),
        source=source,
        question_text=question_text,
        turn=turn,
        evidence=evidence,
        now=now,
    )
    _case_questions(case).append(row)
    _supersede_other_pending(case, slot, turn, f"new_question:{slot}")
    return row


def mark_asked(
    case: dict[str, Any],
    slot: str,
    *,
    turn: int,
    quote: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    row = find_question(case, slot)
    if row is None:
        return None
    if row.get("asked_turn") is None:
        row["asked_turn"] = turn
    row["last_asked_turn"] = turn
    row["ask_count"] = int(row.get("ask_count") or 0) + 1
    if quote:
        row.setdefault("asked_evidence", []).append(
            {"turn": turn, "quote": str(quote)[:300]}
        )
        del row["asked_evidence"][:-4]
    row["updated_at"] = _now(now)
    return row


def answer_question(
    case: dict[str, Any],
    slot: str,
    *,
    turn: int,
    value: Any,
    evidence: dict[str, Any] | None = None,
    reason: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    row = find_question(case, slot)
    if row is None:
        return None
    row["status"] = "answered"
    row["resolved_turn"] = turn
    row["answered_turn"] = turn
    row["answer_value"] = copy.deepcopy(value)
    if evidence:
        row["answer_evidence"] = copy.deepcopy(evidence)
    if reason:
        row["answer_reason"] = reason
    row["updated_at"] = _now(now)
    return row


def defer_question(
    case: dict[str, Any], slot: str, *, turn: int, reason: str, now: float | None = None
) -> dict[str, Any] | None:
    row = find_question(case, slot)
    if row is None or row.get("status") == "answered":
        return row
    row["status"] = "deferred"
    row["deferred_turn"] = turn
    row["defer_reason"] = str(reason)
    row["updated_at"] = _now(now)
    return row


def defer_pending_questions(
    case: dict[str, Any], *, turn: int, reason: str, now: float | None = None
) -> int:
    count = 0
    for row in _case_questions(case):
        if row.get("status") == "pending":
            row["status"] = "deferred"
            row["deferred_turn"] = turn
            row["defer_reason"] = str(reason)
            row["updated_at"] = _now(now)
            count += 1
    return count


def reopen_question(
    case: dict[str, Any],
    slot: str,
    *,
    turn: int,
    reason: str,
    evidence: dict[str, Any] | None = None,
    question_text: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    existing = find_question(case, slot)
    if existing is None:
        existing = ensure_question(
            case,
            slot=slot,
            source="policy",
            question_text=question_text or "",
            turn=turn,
            evidence=evidence,
            now=now,
        )
        if existing.get("status") != "answered":
            return existing
    # Preserve the old answer as history before reopening.
    history = existing.setdefault("answer_history", [])
    if existing.get("status") == "answered":
        history.append(
            {
                "answer_value": copy.deepcopy(existing.get("answer_value")),
                "resolved_turn": existing.get("resolved_turn"),
                "answer_evidence": copy.deepcopy(existing.get("answer_evidence")),
            }
        )
        del history[:-4]
    existing["status"] = "pending"
    existing["reopen_reason"] = str(reason)
    existing["reopen_evidence"] = copy.deepcopy(evidence) if evidence else None
    existing["reopened_turn"] = turn
    existing["resolved_turn"] = None
    existing["answered_turn"] = None
    existing["answer_value"] = None
    existing["answer_evidence"] = None
    if question_text:
        existing["question_text"] = str(question_text)
    existing["updated_at"] = _now(now)
    _supersede_other_pending(case, slot, turn, f"reopened:{slot}")
    return existing


def can_ask(row: dict[str, Any], turn: int) -> tuple[bool, str]:
    """Frequency policy: no loop every turn, but unanswered stays visible.

    A never-asked pending question is askable.  After one ask the reducer waits
    at least one turn; a deferred question needs an explicit reopen reason and
    may be asked at most ``MAX_ASKS`` times.  Reaching the cap is *not* an
    answer: the record stays pending/deferred and the snapshot exposes it as
    ``blocked_unanswered``.
    """
    if row.get("status") == "answered":
        return False, "already_answered"
    if row.get("status") in {"superseded", "cancelled"}:
        return False, f"status_{row.get('status')}"
    asked = int(row.get("ask_count") or 0)
    last = row.get("last_asked_turn")
    if asked == 0:
        if row.get("status") == "deferred" and not row.get("reopen_reason"):
            # Deferred before ever being asked (e.g. a topic switch or a
            # protocol ack) is not a reason to ask it now.
            return False, "deferred_before_first_ask"
        return True, "first_ask"
    if asked >= MAX_ASKS:
        return False, "reask_limit_reached:not_answered"
    if last is None or turn - int(last) < REASK_MIN_GAP:
        return False, "asked_recently_waiting_answer"
    if row.get("status") == "deferred" and not row.get("reopen_reason"):
        return False, "deferred_without_reopen_reason"
    return True, "reask_after_gap_with_reason"


def current_question(case: dict[str, Any] | None, turn: int) -> dict[str, Any] | None:
    if not case or case.get("status") != "active":
        return None
    candidates: list[tuple[int, dict[str, Any], str]] = []
    for row in _case_questions(case):
        ok, reason = can_ask(row, turn)
        if not ok:
            continue
        priority = 0 if row.get("status") == "pending" else 1
        candidates.append((priority, row, reason))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], int(item[1].get("created_turn") or 0)))
    _, row, reason = candidates[0]
    result = copy.deepcopy(row)
    result["ask_reason"] = reason
    return result


def reconcile_questions(case: dict[str, Any], *, turn: int, now: float | None = None) -> None:
    """Close slots that are already satisfied by confirmed user facts."""
    context = case.get("work_context") or {}
    ambiguity = {
        item.get("id"): item for item in context.get("ambiguity") or [] if isinstance(item, dict)
    }
    dock = ambiguity.get("dock_find_referent") or {}
    if dock.get("status") == "resolved" and dock.get("resolution"):
        row = find_question(case, "dock_find_referent")
        if row is not None and row.get("status") != "answered":
            answer_question(
                case,
                "dock_find_referent",
                turn=int(dock.get("resolved_turn") or turn),
                value=dock.get("resolution"),
                evidence={
                    "quote": dock.get("basis_quote"),
                    "source_turn": dock.get("resolved_turn"),
                    "reason": "known_fact_satisfies_slot",
                },
                reason="known_fact_satisfies_slot",
                now=now,
            )
        elif row is None:
            row = new_question(
                slot="dock_find_referent",
                target_case=str(case.get("case_id") or ""),
                source="legacy_migration",
                question_text="",
                turn=turn,
                now=now,
            )
            _case_questions(case).append(row)
            answer_question(
                case,
                "dock_find_referent",
                turn=int(dock.get("resolved_turn") or turn),
                value=dock.get("resolution"),
                evidence={
                    "quote": dock.get("basis_quote"),
                    "source_turn": dock.get("resolved_turn"),
                    "reason": "lazy_migration_from_typed_ambiguity",
                },
                reason="known_fact_satisfies_slot",
                now=now,
            )
    closure = case.get("closure_evidence") or {}
    weak = find_question(case, "weak_closure_result")
    if weak is not None and closure.get("level") == "explicit_success" and weak.get("status") != "answered":
        answer_question(
            case,
            "weak_closure_result",
            turn=int(closure.get("source_turn") or turn),
            value="explicit_success",
            evidence={"quote": closure.get("text"), "source_turn": closure.get("source_turn")},
            reason="known_fact_satisfies_slot",
            now=now,
        )
    tried = {
        item.get("action_id"): item
        for item in case.get("tried_actions") or []
        if isinstance(item, dict)
        and str(item.get("fact_status") or "effective") == "effective"
    }
    capabilities = effective_by_capability(case)
    for row in list(_case_questions(case)):
        slot = str(row.get("slot") or "")
        if slot.startswith("step_outcome:"):
            action_id = slot.split(":", 1)[1]
            action = tried.get(action_id) or {}
            if not action and row.get("status") == "answered":
                # The tried fact was retracted by a later correction; the old
                # "outcome" answer is no longer authoritative.  Keep it as
                # history, do not re-ask automatically.
                history = row.setdefault("answer_history", [])
                history.append(
                    {
                        "answer_value": copy.deepcopy(row.get("answer_value")),
                        "resolved_turn": row.get("resolved_turn"),
                        "answer_evidence": copy.deepcopy(row.get("answer_evidence")),
                    }
                )
                del history[:-4]
                row["status"] = "superseded"
                row["superseded_turn"] = turn
                row["supersede_reason"] = "source_tried_fact_retracted"
                row["answer_value"] = None
                row["answer_evidence"] = None
                row["updated_at"] = _now(now)
                continue
            capability = ACTION_CAPABILITY.get(action_id)
            observation = capabilities.get(capability or "")
            if (
                observation is not None
                and str(observation.get("result")) in {"works", "fails"}
                and row.get("status") != "answered"
            ):
                observation_turn = int(observation.get("turn") or 0)
                action_turn = int(action.get("source_turn") or 0)
                if not (action_turn == turn and observation_turn < action_turn):
                    answer_question(
                        case,
                        slot,
                        turn=int(observation.get("turn") or turn),
                        value=observation.get("result"),
                        evidence={
                            "quote": observation.get("quote"),
                            "source_turn": observation.get("turn"),
                            "reason": "known_capability_observation_satisfies_slot",
                        },
                        reason="known_capability_observation_satisfies_slot",
                        now=now,
                    )
                    continue
            if action.get("outcome") in {"success", "failed"} and row.get("status") != "answered":
                answer_question(
                    case,
                    slot,
                    turn=int(action.get("source_turn") or turn),
                    value=action.get("outcome"),
                    evidence={"quote": action.get("evidence"), "source_turn": action.get("source_turn")},
                    reason="known_fact_satisfies_slot",
                    now=now,
                )
        if slot.startswith("capability_observation:"):
            capability = slot.split(":", 1)[1]
            observation = capabilities.get(capability)
            if (
                observation is not None
                and str(observation.get("result")) in {"works", "fails"}
                and row.get("status") != "answered"
            ):
                answer_question(
                    case,
                    slot,
                    turn=int(observation.get("turn") or turn),
                    value=observation.get("result"),
                    evidence={
                        "quote": observation.get("quote"),
                        "source_turn": observation.get("turn"),
                        "reason": "known_capability_observation_satisfies_slot",
                    },
                    reason="known_capability_observation_satisfies_slot",
                    now=now,
                )
        if slot == "tried_step" and tried and row.get("status") == "pending":
            answer_question(
                case,
                slot,
                turn=turn,
                value=sorted(tried),
                evidence={"reason": "action_recorded_before_question"},
                reason="known_fact_satisfies_slot",
                now=now,
            )


def question_summary(case: dict[str, Any] | None, turn: int) -> dict[str, Any]:
    if not case:
        return {
            "current_question": None,
            "answered_scope": [],
            "deferred_scope": [],
            "blocked_unanswered": [],
            "must_not_ask": [],
        }
    reconcile_questions(case, turn=turn)
    answered = []
    deferred = []
    blocked = []
    must_not_ask = []
    for row in _case_questions(case):
        status = row.get("status")
        compact = {
            "question_id": row.get("question_id"),
            "slot": row.get("slot"),
            "status": status,
            "answer_value": copy.deepcopy(row.get("answer_value")),
            "resolved_turn": row.get("resolved_turn"),
            "deferred_turn": row.get("deferred_turn"),
            "defer_reason": row.get("defer_reason"),
            "ask_count": row.get("ask_count"),
            "answer_evidence": copy.deepcopy(row.get("answer_evidence")),
        }
        if status == "answered":
            answered.append(compact)
            must_not_ask.append({"slot": row.get("slot"), "reason": "answered"})
        elif status == "deferred":
            deferred.append(compact)
            must_not_ask.append({"slot": row.get("slot"), "reason": "deferred"})
        elif status == "superseded":
            must_not_ask.append({"slot": row.get("slot"), "reason": "superseded"})
        if status in {"pending", "deferred"} and int(row.get("ask_count") or 0) >= MAX_ASKS:
            blocked.append(
                {
                    "question_id": row.get("question_id"),
                    "slot": row.get("slot"),
                    "status": status,
                    "ask_count": row.get("ask_count"),
                    "note": "asked twice without an answer; not treated as answered",
                }
            )
    return {
        "current_question": current_question(case, turn),
        "answered_scope": answered,
        "deferred_scope": deferred,
        "blocked_unanswered": blocked,
        "must_not_ask": must_not_ask,
    }


def detect_slot_question(answer: str, slot: str) -> bool:
    text = str(answer or "")
    if not text or not _QUESTION_CUE.search(text):
        return False
    if slot == "dock_find_referent":
        # Require the robot-reading and user-reading in the *same* question
        # sentence.  A new symptom question ("打转/对不上 vs 停在旁边") must not
        # be misreported as the already-answered referent question.
        for sentence in re.split(r"(?<=[?？])", text):
            if not _QUESTION_CUE.search(sentence):
                continue
            if _DOCK_BOTH.search(sentence) and _DOCK_USER.search(sentence) and _DOCK_ASK.search(sentence):
                return True
        return False
    if slot == "weak_closure_result":
        return bool(_WEAK_ASK.search(text) and _QUESTION_CUE.search(text))
    if slot == "tried_step":
        return bool(_STEP_ASK.search(text) and _QUESTION_CUE.search(text))
    if slot.startswith("step_outcome:"):
        return bool(_RESULT_ASK.search(text) and _QUESTION_CUE.search(text))
    return False


def detect_unexpected_questions(
    answer: str, rows: list[dict[str, Any]], *, current_slot: str | None = None
) -> list[dict[str, Any]]:
    """Return known slots the answer asked even though they are not askable."""
    hits: list[dict[str, Any]] = []
    for row in rows:
        slot = str(row.get("slot") or "")
        if current_slot is not None and slot == current_slot:
            continue
        if row.get("status") in {"answered", "superseded", "cancelled"}:
            if detect_slot_question(answer, slot):
                hits.append({"slot": slot, "status": row.get("status"), "question_id": row.get("question_id")})
            continue
        if row.get("status") in {"pending", "deferred"} and int(row.get("ask_count") or 0) > 0:
            # Already asked but not askable this turn (waiting, deferred, or
            # capped).  Repeating it is an unexpected duplicate ask.
            if detect_slot_question(answer, slot):
                hits.append(
                    {
                        "slot": slot,
                        "status": row.get("status"),
                        "question_id": row.get("question_id"),
                        "reason": "repeat_ask_while_not_askable",
                    }
                )
    return hits


def audit_assistant_answer(
    case: dict[str, Any] | None,
    answer: str,
    *,
    turn: int,
    now: float | None = None,
) -> dict[str, Any]:
    """Inspect the *answer text* and correlate it with question records.

    This is the oracle that prevents "the program didn't record a question" from
    being used as proof the model did not ask one.
    """
    audit: dict[str, Any] = {
        "turn": turn,
        "actual_asked": [],
        "unexpected_asked": [],
        "unplanned_questions": [],
        "missed_question": False,
        "questionless_answer": not bool(_QUESTION_CUE.search(str(answer or ""))),
        "checked_at": _now(now),
    }
    if not case:
        return audit
    reconcile_questions(case, turn=turn, now=now)
    current = current_question(case, turn)
    if current is not None and detect_slot_question(answer, str(current.get("slot") or "")):
        mark_asked(
            case,
            str(current.get("slot")),
            turn=turn,
            quote=str(answer)[:300],
            now=now,
        )
        audit["actual_asked"].append(
            {"slot": current.get("slot"), "question_id": current.get("question_id")}
        )
    elif current is not None and detect_slot_question(answer, str(current.get("slot") or "")) is False:
        audit["missed_question"] = True
    rows = _case_questions(case)
    for hit in detect_unexpected_questions(
        str(answer or ""),
        rows,
        current_slot=str(current.get("slot")) if current else None,
    ):
        if any(item.get("slot") == hit["slot"] for item in audit["actual_asked"]):
            continue
        hit["quote"] = str(answer)[:300]
        audit["unexpected_asked"].append(hit)
    # Record questions the model asked beyond the policy question.  This is the
    # evidence that we inspected the answer instead of trusting the state.
    known_quotes = {
        str((current or {}).get("question_text") or "")[:40],
        *[str(row.get("question_text") or "")[:40] for row in rows],
    }
    unplanned: list[dict[str, Any]] = []
    for match in _QUESTION_SENTENCE.finditer(str(answer or "")):
        sentence = re.sub(r"[*_`#>]+", "", match.group(0)).strip()
        if len(sentence) < 6:
            continue
        if any(sentence[:40] and sentence[:40] in known for known in known_quotes if known):
            continue
        if detect_slot_question(sentence, "dock_find_referent") and any(
            row.get("slot") == "dock_find_referent" for row in rows
        ):
            continue
        unplanned.append({"quote": sentence[:200]})
        if len(unplanned) >= 3:
            break
    audit["unplanned_questions"] = unplanned
    if audit["unexpected_asked"]:
        case["unexpected_ask_count"] = int(case.get("unexpected_ask_count") or 0) + len(
            audit["unexpected_asked"]
        )
        case["last_unexpected_ask"] = copy.deepcopy(audit["unexpected_asked"])
    case["last_question_audit"] = copy.deepcopy(audit)
    return audit
