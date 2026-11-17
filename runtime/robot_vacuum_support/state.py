# -*- coding: utf-8 -*-
"""Deterministic, server-authoritative after-sales state for recharge failures.

Design rules (deliberately narrow):

* Only the *user's* message may create a user fact (model, attempted step,
  outcome, resolution).  An assistant answer only creates a *suggestion*.
* A negated step ("没清理触点") is never recorded as tried.
* A bare "不行" is not a global failure; it is linked to a clear recent
  suggestion, otherwise the product asks which step the user means.
* "还是不行" cannot mark an assistant suggestion as tried unless the suggestion
  is unambiguous.  Ambiguity produces a necessary clarification instead.
* Each resolved case stays in ``cases``; a later recharge failure opens a new
  case instead of erasing history.
* Writes are made by the server through ``SessionStore`` while the existing
  worker busy lock is held; the worker never writes this file.

Schema version 1 is intentionally small and JSON-only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from typing import Any

from . import diagnostic_scope as diagnostic
from . import questions as question_lifecycle
from .actions import (
    ACTION_BY_ID,
    ACTION_CATALOG,
    ACTION_IDS,
    ACTION_PATTERNS,
    claim_rejection,
    clause_scope_at,
    display_action_label,
    scoped_clauses,
)
from .interpretation import (
    AMBIGUITY_CANDIDATES,
    INTERPRETATION_SCHEMA_VERSION,
    interpret_turn,
    normalize_interpretation,
)

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSIONS = (1,)
FACT_STATUS_EFFECTIVE = "effective"
FACT_STATUS_RETRACTED = "retracted"
FACT_STATUS_SUPERSEDED = "superseded"
MAX_FACT_AUDIT = 80
MAX_GENERALIZED_NEGATION_ACTIONS = 3

#: Shared canonical catalogue (imported for backwards-compatible module-level
#: access; interpretation.py consumes the same list).
_ACTION_PATTERNS = ACTION_PATTERNS

#: Keywords that put a turn in the recharge scope.  This is intentionally not a
#: generic "fault" detector: other faults must not contaminate recharge facts.
_RECHARGE_WORDS = (
    "回充",
    "充电",
    "充电座",
    "充电桩",
    "充电触点",
    "电量",
    "充不上",
    "充不进",
    "充不了",
    "不充电",
    "没充电",
    "回不去",
    "回不了",
)
_RECHARGE_FAILURE = re.compile(
    r"充不上|充不进|充不了|无法充电|不能充电|不回充|回充失败|充电失败|"
    r"不充电|没充电|充电没反应|充电没有反应|充不进电|充不上电|"
    r"回不了充|回不到充电座|找不到充电座|"
    r"回不去(?:了)?|回不来(?:了)?|回不了(?:去|家)"
)
#: Other explicit topics.  When one is present without a recharge follow-up we
#: leave the recharge case alone and do not inject its facts into the answer.
_OTHER_TOPICS = {
    "cleaning": r"扫不干净|清扫不干净|拖不干净|吸力|打扫不干净|清扫效果",
    "navigation": r"迷路|建图|地图|乱跑|找不到路|定位",
    "noise": r"噪音|很响|异响|声音大",
    "battery": r"续航|掉电|耗电快|电池不耐用",
    "app": r"wifi|WI-FI|联网|配网|连接不上|APP|app",
    "maintenance": r"保养|维护|滤网|滚刷|边刷|水箱|清洗",
    "purchase": r"选购|推荐|买哪|价格|多少钱|型号推荐",
}
_FOLLOWUP = re.compile(
    r"还是不行|仍然不行|依然不行|还是不能|还是没|仍然没|依然没|"
    r"充上了|充上电|充进了|恢复了|正常了|好了|解决了|可以充|能充了|"
    r"刚才试|试过什么|做过什么|之前试|下一步|接下来|怎么办|"
    r"型号|机型|没试|没有试|还没试|没清理|没有清理|没擦|没有擦|"
    r"擦了|清理了|清洁了|重启了|检查了|试了|用了"
)
#: Explicit success only.  Weak positive signals ("好了/谢谢/正常了") are
#: deliberately separate: they can close the conversation politely but must not
#: be promoted to a resolved user fact.
_SUCCESS = re.compile(
    r"充(?:上|进)(?:了|电)|充上电了|充好了|已经充上|能充上|可以充|能充电了|能充了|"
    r"能(?:自己)?回充|回充正常了?|可以(?:正常)?回充|正常回充|"
    r"自己回去了|已经能.{0,6}(?:回充|充电)|"
    r"恢复正常|恢复(?:了|正常)|解决了|修好了|已经解决"
)
_WEAK_POSITIVE = re.compile(r"好了|正常了|没事了|可以了|谢谢|辛苦了|^行$|^ok$", re.I)
_PARTIAL_SUCCESS = re.compile(
    r"手动.{0,8}(?:能|可以)|放(?:回|上).{0,8}(?:能|可以)充|"
    r"能充.{0,8}自动.{0,6}(?:不行|不能)|自动.{0,6}(?:还是)?不行"
)
_CORRECTION = re.compile(r"说错了|说错|收回|改口|更正|纠正|不是.{0,6}是")
_FAILURE = re.compile(
    r"还是不行|仍然不行|依然不行|还是不能|还是没|仍然没|依然没|"
    r"没反应|没有反应|不好使|不管用|没用|失败|"
    r"充不上|充不进|充不了|无法充电|不能充电|不充电|没充电|"
    r"回充失败|充电失败|不能(?:回充|充电)|"
    r"不能.{0,6}(?:正常)?回充|无法.{0,6}(?:正常)?回充|"
    r"不能.{0,6}(?:正常)?充电|无法.{0,6}(?:正常)?充电|"
    r"找不到充电座|回不到充电座|回不了充|回不去(?:了)?|回不来(?:了)?|不充了|"
    r"没恢复|没有恢复|还没恢复|未恢复|没有好|还没好|没好"
)
#: Only these phrases are ambiguous *follow-ups*.  A first-turn report such as
#: "回充失败怎么办" must not be answered as "你说的还是不行是指哪一步".
_AMBIGUOUS_FOLLOWUP = re.compile(
    r"还是不行|仍然不行|依然不行|还是不能|还是没|仍然没|依然没|"
    r"没反应|没有反应|不好使|不管用|不行"
)
#: A bare feedback turn, not a new symptom report.  "还是不行" / "现在没反应"
#: may ask which prior step is meant; "灯不亮，放上去没反应" is a new symptom
#: and must go back through diagnosis/agent instead of reusing an old question.
_BARE_AMBIGUOUS_FOLLOWUP = re.compile(
    r"^(?:那|那么|这|这个|它|机器人|现在|目前|还是|仍然|依然|怎么|怎么办|咋办|"
    r"呢|啊|呀|哦|嗯|，|,|。|！|!|\s)*"
    r"(?:还是不行|仍然不行|依然不行|还是不能|还是没(?:有)?(?:反应|用|好)?|"
    r"仍然没(?:有)?(?:反应|用|好)?|依然没(?:有)?(?:反应|用|好)?|"
    r"没反应|没有反应|不好使|不管用|不行)"
    r"(?:怎么办|咋办|呢|啊|呀|哦|嗯|了|，|,|。|！|!|\s)*$"
)
#: Narrow non-fault intent adapter: an explicit how-to/configuration request
#: about auto-return is not a fault report unless the same turn also carries an
#: explicit failure/safety signal.  This is a guard, not the understanding layer.
_SETTINGS_INTENT = re.compile(
    r"(?:怎么|如何|怎样|在哪|哪里|哪儿).{0,12}(?:设置|开启|关闭|打开|配置|调整|使用)|"
    r"(?:设置|开启|关闭|打开|配置|调整).{0,12}(?:自动回充|回充|在哪|哪里|哪儿)"
)
#: A bare "不能" is deliberately not a failure marker.  A result negation such
#: as "没擦干净/没弄好" still means the user did try, so it must not become a
#: "not tried" fact.
_NEGATED_ACTION = re.compile(
    r"没(?:有)?(?:试|试过|清理|擦|清洁|检查|重启|换|弄|动|做)(?!干净|好|完|到位|彻底)|"
    r"不是(?!没|没有)(?:换|挪|搬|擦|清|检查|重启|动|做)?|"
    r"还没(?:试|清理|擦|清洁|检查|重启)|未(?:试|处理|清理)|"
    r"不打算(?:试|清理|擦)|不想(?:试|清理|擦)|没用过|没清(?!楚|干净)"
)
#: A correction can explicitly withdraw an execution claim without using the
#: short verb list above ("其实我没有把它放回充电座").  This is still a general
#: negation-of-action rule: the action mention must be in the same clause, after
#: the negation, and must not be a result complement ("没清理干净" means the user
#: did try).  It is only consulted when a correction cue is present.
_CORRECTION_NEGATION = re.compile(
    r"(?:没|没有|未|还没|从没).{0,10}"
    r"(?:放回|对准|归位|按|试|试过|用|打开|关闭|搬|挪|换|清理|擦|清洁|检查|重启)"
    r"(?!干净|好|完|到位|彻底)"
)
#: Mention patterns used only for not-tried extraction.  ``check_auto_return``
#: is a settings identity in the execution catalogue, but a correction such as
#: "自动回充也还没再试" is still a not-tried statement about that step family.
_NEGATED_ACTION_PATTERNS = dict(ACTION_PATTERNS)
_NEGATED_ACTION_PATTERNS["check_auto_return"] = re.compile(
    r"自动回充|自动返回|回充设置|地图.{0,8}(?:设置|位置|改动|修改)"
)
#: Generalized negation ("都没试过/一个都没做").  With no explicit action object
#: the reducer binds it to the last-asked actions or pending suggestions; it
#: never negates all historical tried actions.
_GENERALIZED_NEGATION = re.compile(
    r"(?:都|全|全部|全都|一个|一样|什么|啥|哪个|任何).{0,3}(?:没|没有|未|还没|从没).{0,3}"
    r"(?:试|试过|做|做过|弄|动|排查)|"
    r"(?:没|没有|未|还没|从没).{0,3}(?:试过|做过).{0,3}(?:任何|所有|这些|那些|全部)"
)
_EXECUTED = re.compile(
    r"我|已经|刚刚|刚才|了|过|做完|弄完|搞完|试了|试过|执行|按说明|按你"
)
_TRIED_QUESTION = re.compile(
    r"刚才(?:试过|做过|用了|清理|擦)(?:什么|哪些|哪几|啥|的?步骤)|"
    r"之前(?:试过|做过)(?:什么|哪些|哪几|啥|的?步骤)|"
    r"我(?:都)?试过(?:什么|哪些|哪几|啥)|"
    r"已经试过(?:什么|哪些|哪几)|试过哪些|试过什么|做过哪些|做过什么"
)
#: Negation anywhere in the same clause suppresses a success claim.  This is
#: intentionally conservative: when unsure we keep the case open and ask.
_CLAUSE_NEGATION = re.compile(r"没|没有|还没|未|不能|无法|不|别|勿")
_HYPOTHETICAL = re.compile(
    r"如果|要是|假如|万一|是否|能否|能不能|可不可以|会不会|是不是|"
    r"怎么才能|何时|吗|嘛|的话"
)
_CLAUSE_SPLIT = re.compile(r"[，。；！？,.;!?]|但(?:是)?|不过|可是|只是|然而")
_SAFETY_SYMPTOM = re.compile(r"冒烟|焦味|烧焦|发烫|过热|漏电|触电|起火|着火")
_DOCK_CLEAR_ROBOT = re.compile(
    r"(?:机器人|机器|它).{0,14}(?:回不去|回不了|回不到|找不到(?:充电座|基座|座)|对不上|打转|转圈)|"
    r"(?:在|到).{0,8}(?:充电座|基座|座).{0,10}(?:打转|对不上|转圈)|"
    r"回不去(?:了)?|回不了(?:充|去)?"
)
_USER_FIND_DOCK = re.compile(
    r"(?:我|自己).{0,12}(?:找不到|不知道.{0,6}放哪|搬(?:完)?家|挪走|丢了|忘了放哪)|"
    r"找不到(?:充电座|基座).{0,8}(?:放哪|位置)|"
    r"(?:充电座|基座).{0,8}(?:被|让).{0,4}(?:挪|搬|移)"
)
_FULL_CHECKLIST_REQUEST = re.compile(r"完整.{0,4}(?:清单|步骤)|所有.{0,4}步骤|都告诉我|全部步骤")
_DOCK_AMBIGUITY_QUESTION = (
    "先确认一下：是机器人回不到充电座（例如在座前打转、对不上），"
    "还是您找不到充电座的位置？"
)
_DOCK_PLACE_STEP = {
    "action_id": "place_dock",
    "label": "确认充电座位置",
    "detail": "先确认充电座是否还在原来的固定位置；如果被挪动，先放回开阔、有电源的位置，再让机器人重新回充。",
    "kind": "place_dock",
}
_SAFETY_STEP = {
    "action_id": "safety_check",
    "label": "先做安全检查",
    "detail": "检测到可能的安全风险：不要继续常规排查；具体安全处理只能依据本轮实际检索到的资料说明，资料不足时明确说明并建议联系官方售后。",
    "kind": "safety",
}


def _reported_outcome(text: str) -> str | None:
    """Return the last explicit outcome in clause order, with closure grading.

    Negated or hypothetical clauses cannot close a case: "还没有恢复正常",
    "还不能正常回充", "如果恢复了..." stay unresolved.  When success and
    failure both appear, the *last explicit* clause wins, matching "充不上，
    但现在充上了" and "充上了，但又不充了".

    Weak positive signals ("好了", "谢谢", "正常了") never override a failure
    and are returned separately so the caller does not mark the case resolved.
    """
    outcome: str | None = None
    weak_seen = False
    for part in scoped_clauses(str(text or "")):
        if part.get("blocked"):
            # A hypothetical/other-source clause governs its result clause
            # ("如果朋友家…手动放上去能充"): it cannot close or partially
            # resolve the current user's case.
            continue
        clause = str(part.get("text") or "")
        if not clause.strip():
            continue
        hypothetical = bool(_HYPOTHETICAL.search(clause))
        if hypothetical:
            continue
        if _PARTIAL_SUCCESS.search(clause):
            outcome = "partial_success"
            continue
        success = bool(_SUCCESS.search(clause))
        failure = bool(_FAILURE.search(clause))
        weak = bool(_WEAK_POSITIVE.search(clause))
        if success and _CLAUSE_NEGATION.search(clause):
            success = False
        if success and failure:
            success_match = _SUCCESS.search(clause)
            failure_match = _FAILURE.search(clause)
            if failure_match and success_match and failure_match.start() > success_match.start():
                success = False
            else:
                failure = False
        if success:
            outcome = "explicit_success"
        elif failure:
            outcome = "failure"
        elif weak:
            weak_seen = True
            if outcome is None:
                outcome = "weak_positive"
    if outcome is None and weak_seen:
        return "weak_positive"
    return outcome


def outcome_is_failure(text: str) -> bool:
    """True for an explicit failure/partial signal; weak positives are not."""
    return _reported_outcome(text) in {"failure", "partial_success"}


def _semantic_recharge_fault(proposal: dict[str, Any], text: str, recharge: bool) -> bool:
    """Whether a validated semantic proposal is evidence of a recharge fault.

    The reducer still owns facts.  This only decides *activation*: a semantic
    ``topic=recharge_failure`` is enough when the same proposal carries
    structural fault evidence (explicit closure, an open referent ambiguity, a
    diagnosis-stage turn, or user execution facts).  A settings/how-to question
    has none of those and therefore stays out of the fault-case flow.  The
    existing stable symptom adapters are the last fallback, never the main
    understanding layer.
    """
    if not recharge or str(proposal.get("topic") or "") != "recharge_failure":
        return False
    closure = str(proposal.get("closure_signal") or "none")
    if closure in {"failure", "partial_success"}:
        return True
    if _SETTINGS_INTENT.search(str(text or "")):
        # A how-to/configuration request is not a fault report merely because
        # the semantic layer attached a referent ambiguity.  Explicit failure or
        # safety above still wins.
        return False
    for item in proposal.get("ambiguity") or []:
        if isinstance(item, dict) and item.get("id"):
            # Any validated referent record (open, deferred or resolved) means
            # the interpreter saw a return/dock fault context, not a settings
            # how-to.  Open/deferred rows remain askable; resolved rows are
            # still activation evidence.
            return True
    if str(proposal.get("interaction_stage") or "") in {
        "tried",
        "diagnosing",
        "awaiting_result",
    }:
        return True
    if (
        proposal.get("executed_actions")
        or proposal.get("not_tried_actions")
        or proposal.get("capability_observations")
    ):
        return True
    symptom = str((proposal.get("observable_symptom") or {}).get("text") or "")
    if str(proposal.get("domain") or "") == "robot_vacuum_after_sales" and (
        outcome_is_failure(symptom) or bool(_POWER_SYMPTOM.search(symptom))
    ):
        return True
    return False


def _has_prior_feedback_context(case: dict[str, Any], turn: int) -> bool:
    """True only when a case already has a previous fact/suggestion turn.

    A case created on the current turn has no prior work context, so a first
    symptom such as "放上去没反应" is never answered as if it were feedback on
    an earlier suggestion.
    """
    try:
        source_turn = int(case.get("_source_turn") or 0)
    except (TypeError, ValueError):
        source_turn = 0
    if source_turn >= turn:
        return False
    if (
        _effective_tried(case)
        or _effective_not_tried(case)
        or case.get("suggested_actions")
        or case.get("feedback_events")
        or case.get("clarification_history")
    ):
        return True
    stage = str((case.get("work_context") or {}).get("interaction_stage") or "")
    return stage in {"tried", "diagnosing", "awaiting_result", "need_info"}


def _is_bare_ambiguous_feedback(text: str) -> bool:
    return bool(_BARE_AMBIGUOUS_FOLLOWUP.match(str(text or "").strip()))


_MODEL_PATTERNS = (
    re.compile(
        r"(?:型号|机型)\s*(?:是|为|:)?\s*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9 _.\-]{1,31})"
    ),
    re.compile(
        r"(?:我家|家里|我买的|用的是|机器是|机器人是)\s*(?:的)?\s*(?:是|为)?\s*"
        r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9 _.\-]{1,31})"
    ),
    re.compile(r"(?:^|[\s，,。])([A-Za-z]{2,}[ \-]?\d{1,4}[A-Za-z0-9\-]*)"),
)
_POWER_SYMPTOM = re.compile(r"指示灯不亮|灯不亮|没电|不通电|插座|电源没|没有电")
_MODEL_REJECT = (
    "充电",
    "回充",
    "扫地机器人",
    "扫拖机器人",
    "机器人",
    "充电座",
    "触点",
    "怎么办",
    "不行",
    "故障",
    "清理",
    "检查",
)


def _now(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(value)))


def _excerpt(text: str, limit: int = 160) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:limit]


# ---------------------------------------------------------------------------
# Authoritative fact rows: effective / retracted / superseded + audit
# ---------------------------------------------------------------------------
def _fact_status(row: dict[str, Any]) -> str:
    """Old rows without a status are effective (lazy schema compatibility)."""
    status = str(row.get("fact_status") or FACT_STATUS_EFFECTIVE)
    return status if status in {
        FACT_STATUS_EFFECTIVE,
        FACT_STATUS_RETRACTED,
        FACT_STATUS_SUPERSEDED,
    } else FACT_STATUS_EFFECTIVE


def _effective_rows(case: dict[str, Any] | None, field: str) -> list[dict[str, Any]]:
    if not isinstance(case, dict):
        return []
    return [row for row in case.get(field) or [] if _fact_status(row) == FACT_STATUS_EFFECTIVE]


def _effective_tried(case: dict[str, Any] | None) -> list[dict[str, Any]]:
    return _effective_rows(case, "tried_actions")


def _effective_not_tried(case: dict[str, Any] | None) -> list[dict[str, Any]]:
    return _effective_rows(case, "not_tried_actions")


def _effective_action_ids(case: dict[str, Any] | None, field: str) -> set[str]:
    return {str(row.get("action_id")) for row in _effective_rows(case, field)}


def _fact_evidence_id(payload: dict[str, Any]) -> str:
    key = "|".join(
        str(payload.get(name) or "")
        for name in (
            "kind",
            "action_id",
            "message_id",
            "turn",
            "reason",
            "text",
            "binding_source",
        )
    )
    return "fact-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _append_fact_audit(
    case: dict[str, Any],
    *,
    kind: str,
    action_id: str | None,
    message_id: str | None,
    turn: int,
    text: str,
    reason: str,
    replaced_fact: dict[str, Any] | None,
    now: float,
    binding_source: str | None = None,
) -> bool:
    """Append one retraction/supersede record, idempotently.

    The audit deliberately keeps the original quote, turn, message id, reason
    and the replaced fact so a later reader can reconstruct why an old tried
    row is no longer authoritative.
    """
    payload = {
        "kind": kind,
        "action_id": action_id,
        "message_id": message_id,
        "turn": turn,
        "text": _excerpt(text),
        "reason": reason,
        "binding_source": binding_source,
    }
    evidence_id = _fact_evidence_id(payload)
    audit = case.setdefault("fact_audit", [])
    if any(item.get("evidence_id") == evidence_id for item in audit):
        return False
    audit.append(
        {
            "evidence_id": evidence_id,
            **payload,
            "replaced_fact": copy.deepcopy(replaced_fact) if replaced_fact else None,
            "at": now,
        }
    )
    del audit[:-MAX_FACT_AUDIT]
    return True


def _retract_tried_for_action(
    case: dict[str, Any],
    action_id: str,
    *,
    message_id: str | None,
    turn: int,
    text: str,
    reason: str,
    now: float,
) -> int:
    """Withdraw every effective tried row for one action, keeping the audit."""
    changed = 0
    for row in _effective_tried(case):
        if row.get("action_id") != action_id:
            continue
        row["fact_status"] = FACT_STATUS_RETRACTED
        row["retracted_turn"] = turn
        row["retraction_reason"] = reason
        row["retracted_at"] = now
        row["updated_at"] = now
        _append_fact_audit(
            case,
            kind="retract_tried",
            action_id=action_id,
            message_id=message_id,
            turn=turn,
            text=text,
            reason=reason,
            replaced_fact=row,
            now=now,
        )
        changed += 1
    for item in case.get("suggested_actions") or []:
        if item.get("action_id") == action_id and str(item.get("status") or "").startswith("confirmed"):
            item["status"] = "suggested"
            item["confirmation_withdrawn_turn"] = turn
            item.pop("confirmed_message_id", None)
            item.pop("confirmed_turn", None)
            item["updated_at"] = now
    return changed


def _supersede_not_tried_for_action(
    case: dict[str, Any],
    action_id: str,
    *,
    message_id: str | None,
    turn: int,
    text: str,
    reason: str,
    now: float,
) -> int:
    """Revoke an old effective not-tried row when the user later did the action."""
    changed = 0
    for row in _effective_not_tried(case):
        if row.get("action_id") != action_id:
            continue
        row["fact_status"] = FACT_STATUS_SUPERSEDED
        row["superseded_turn"] = turn
        row["supersede_reason"] = reason
        row["superseded_at"] = now
        row["updated_at"] = now
        _append_fact_audit(
            case,
            kind="supersede_not_tried",
            action_id=action_id,
            message_id=message_id,
            turn=turn,
            text=text,
            reason=reason,
            replaced_fact=row,
            now=now,
        )
        changed += 1
    return changed


def _record_negation_ambiguity(
    case: dict[str, Any],
    *,
    status: str,
    turn: int,
    text: str,
    message_id: str | None,
    candidates: list[str],
    now: float,
    reason: str,
) -> dict[str, Any]:
    rows = case.setdefault("negation_ambiguity", [])
    key = (status, turn, message_id, tuple(candidates))
    for row in rows:
        if (row.get("status"), row.get("turn"), row.get("message_id"), tuple(row.get("candidates") or [])) == key:
            return row
    row = {
        "status": status,
        "turn": turn,
        "message_id": message_id,
        "text": _excerpt(text),
        "candidates": list(candidates),
        "reason": reason,
        "at": now,
    }
    rows.append(row)
    del rows[:-20]
    return row


def _record_fact_binding(
    case: dict[str, Any],
    *,
    action_ids: list[str],
    source: str,
    turn: int,
    text: str,
    message_id: str | None,
    now: float,
) -> None:
    case["last_fact_binding"] = {
        "action_ids": list(action_ids),
        "source": source,
        "turn": turn,
        "text": _excerpt(text),
        "message_id": message_id,
        "at": now,
    }


def _last_asked_candidates(case: dict[str, Any], turn: int) -> tuple[list[str], str]:
    """Context-linked referents for a generalized negation; never all history."""
    last_asked = case.get("last_asked_actions") or {}
    ids = [str(item) for item in last_asked.get("action_ids") or [] if item in ACTION_BY_ID]
    if ids and int(last_asked.get("turn") or -1) == turn - 1:
        return ids, "last_asked_actions"
    pending = [str(item.get("action_id")) for item in _pending_suggestions(case)]
    pending = [item for item in pending if item in ACTION_BY_ID]
    if pending:
        return pending, "pending_suggestions"
    return [], "none"



def empty_state(now: float | None = None) -> dict[str, Any]:
    stamp = _now(now)
    return {
        "schema_version": SCHEMA_VERSION,
        "turn": 0,
        "active_case_id": None,
        "cases": [],
        "open_tasks": [],
        "pending_confirmation": None,
        "last_interpretation": None,
        "last_retrieval": None,
        "last_turn_scope": None,
        "last_decision": None,
        "decision_snapshot": None,
        "last_turn_metrics": None,
        "last_question_audit": None,
        "interpretation_cache": [],
        "updated_at": stamp,
    }


def _valid_case(case: Any) -> bool:
    return (
        isinstance(case, dict)
        and isinstance(case.get("case_id"), str)
        and isinstance(case.get("tried_actions"), list)
        and isinstance(case.get("suggested_actions"), list)
    )


def _migrate_case_facts(case: dict[str, Any], source_version: Any, now: float) -> None:
    """Lazy compatibility for pre-contract rows; never rewrite an old outcome."""
    legacy = "fact_contract_version" not in case
    case.setdefault("fact_audit", [])
    case.setdefault("negation_ambiguity", [])
    case.setdefault(
        "last_asked_actions", {"turn": None, "action_ids": [], "source": "none"}
    )
    for field in ("tried_actions", "not_tried_actions"):
        for row in case.get(field) or []:
            if not isinstance(row, dict):
                continue
            row.setdefault("fact_status", FACT_STATUS_EFFECTIVE)
            if legacy and field == "tried_actions" and row.get("outcome") == "success":
                row.setdefault("provenance", "legacy_pre_contract_outcome_preserved")
    if not legacy:
        return
    case["fact_contract_version"] = 1
    case["fact_contract_origin"] = (
        "legacy_v1" if source_version == 1 else "legacy_pre_action_state_contract"
    )
    # One effective row per action.  Older duplicates are evidence, not current
    # facts.  The audit records the exact replaced row.
    for field, status, kind in (
        ("tried_actions", FACT_STATUS_RETRACTED, "retract_tried"),
        ("not_tried_actions", FACT_STATUS_SUPERSEDED, "supersede_not_tried"),
    ):
        rows = [row for row in case.get(field) or [] if isinstance(row, dict)]
        effective = [row for row in rows if _fact_status(row) == FACT_STATUS_EFFECTIVE]
        by_action: dict[str, list[dict[str, Any]]] = {}
        for row in effective:
            by_action.setdefault(str(row.get("action_id")), []).append(row)
        for action_id, group in by_action.items():
            if len(group) <= 1:
                continue
            group.sort(
                key=lambda item: (
                    int(item.get("source_turn") or 0),
                    float(item.get("updated_at") or 0),
                )
            )
            for old in group[:-1]:
                old["fact_status"] = status
                old["superseded_turn"] = int(group[-1].get("source_turn") or 0)
                old["supersede_reason"] = "legacy_duplicate_fact"
                old["updated_at"] = now
                _append_fact_audit(
                    case,
                    kind=kind,
                    action_id=action_id,
                    message_id=old.get("source_message_id"),
                    turn=int(old.get("source_turn") or 0),
                    text=str(old.get("evidence") or ""),
                    reason="legacy_duplicate_fact",
                    replaced_fact=old,
                    now=now,
                    binding_source="load_state",
                )
    tried = {
        str(row.get("action_id")): row
        for row in _effective_tried(case)
    }
    not_tried = {
        str(row.get("action_id")): row
        for row in _effective_not_tried(case)
    }
    for action_id in sorted(set(tried) & set(not_tried)):
        tried_row = tried[action_id]
        not_tried_row = not_tried[action_id]
        tried_turn = int(tried_row.get("source_turn") or 0)
        not_tried_turn = int(not_tried_row.get("source_turn") or 0)
        if not_tried_turn >= tried_turn:
            tried_row["fact_status"] = FACT_STATUS_RETRACTED
            tried_row["retracted_turn"] = not_tried_turn
            tried_row["retraction_reason"] = "legacy_conflict_later_not_tried"
            tried_row["updated_at"] = now
            _append_fact_audit(
                case,
                kind="retract_tried",
                action_id=action_id,
                message_id=not_tried_row.get("source_message_id"),
                turn=not_tried_turn,
                text=str(not_tried_row.get("evidence") or ""),
                reason="legacy_conflict_later_not_tried",
                replaced_fact=tried_row,
                now=now,
                binding_source="load_state",
            )
        else:
            not_tried_row["fact_status"] = FACT_STATUS_SUPERSEDED
            not_tried_row["superseded_turn"] = tried_turn
            not_tried_row["supersede_reason"] = "legacy_conflict_later_tried"
            not_tried_row["updated_at"] = now
            _append_fact_audit(
                case,
                kind="supersede_not_tried",
                action_id=action_id,
                message_id=tried_row.get("source_message_id"),
                turn=tried_turn,
                text=str(tried_row.get("evidence") or ""),
                reason="legacy_conflict_later_tried",
                replaced_fact=not_tried_row,
                now=now,
                binding_source="load_state",
            )
        case.setdefault("legacy_fact_reconciliation", []).append(
            {
                "action_id": action_id,
                "kept": "not_tried" if not_tried_turn >= tried_turn else "tried",
                "tried_turn": tried_turn,
                "not_tried_turn": not_tried_turn,
                "reason": "legacy_conflict_resolved_by_later_evidence",
                "at": now,
            }
        )


def load_state(raw: Any, now: float | None = None) -> dict[str, Any]:
    """Validate persisted JSON; unknown/corrupt shapes become a fresh state.

    A missing file (legacy session) is handled by the caller and also returns a
    fresh state, so a pre-state transcript keeps working without migration.
    """
    if not isinstance(raw, dict):
        return empty_state(now)
    source_version = raw.get("schema_version")
    if source_version != SCHEMA_VERSION and source_version not in LEGACY_SCHEMA_VERSIONS:
        # Do not silently reinterpret a future schema.  Starting clean is safer
        # than writing mixed fields; the old file remains as a backup.
        return empty_state(now)
    state = copy.deepcopy(raw)
    state["schema_version"] = SCHEMA_VERSION
    try:
        state["turn"] = int(state.get("turn") or 0)
    except (TypeError, ValueError):
        state["turn"] = 0
    cases = [case for case in state.get("cases") or [] if _valid_case(case)]
    state["cases"] = cases
    for case in cases:
        # Lazy migration: add typed fields without rewriting or deleting any
        # user fact.  The old case rows are kept verbatim.
        case.setdefault("work_context", None)
        case.setdefault("clarification_history", [])
        case.setdefault("feedback_events", [])
        case.setdefault("closure_evidence", None)
        case.setdefault("weak_confirmation_turn", None)
        case.setdefault("questions", [])
        diagnostic.migrate_case(case)
        _migrate_case_facts(case, source_version, _now(now))
    active_id = state.get("active_case_id")
    if not any(case.get("case_id") == active_id for case in cases):
        state["active_case_id"] = None
    state.setdefault("open_tasks", [])
    if source_version == 1 and not state["open_tasks"]:
        existing_ids = {task.get("case_id") for task in state["open_tasks"]}
        for case in cases:
            if case.get("status") == "active" and case.get("case_id") not in existing_ids:
                state["open_tasks"].append(
                    {
                        "task_id": case.get("case_id"),
                        "case_id": case.get("case_id"),
                        "topic": case.get("topic") or "recharge_failure",
                        "status": "open",
                        "created_turn": case.get("_source_turn"),
                        "last_confirmed_turn": None,
                        "last_prompted_turn": None,
                        "source": "lazy_migration_v1",
                    }
                )
        state["migration"] = {
            "from": 1,
            "to": SCHEMA_VERSION,
            "at": _now(now),
            "kept_case_ids": [case.get("case_id") for case in cases],
            "note": "typed context added lazily; existing user facts preserved",
        }
    state.setdefault("pending_confirmation", None)
    state.setdefault("last_interpretation", None)
    state.setdefault("last_retrieval", None)
    state.setdefault("last_turn_scope", None)
    state.setdefault("last_decision", None)
    state.setdefault("decision_snapshot", None)
    state.setdefault("last_turn_metrics", None)
    state.setdefault("last_question_audit", None)
    state.setdefault("interpretation_cache", [])
    for case in cases:
        # Reconcile typed facts into question records without inventing an
        # answer: only a resolved ambiguity / explicit outcome is a fact.
        question_lifecycle.reconcile_questions(
            case, turn=int(case.get("_source_turn") or state.get("turn") or 0), now=_now(now)
        )
    state.setdefault("updated_at", _now(now))
    return state


def _active_case(state: dict[str, Any]) -> dict[str, Any] | None:
    active_id = state.get("active_case_id")
    for case in state.get("cases") or []:
        if case.get("case_id") == active_id:
            return case
    return None


def _find_case(state: dict[str, Any], case_id: str) -> dict[str, Any] | None:
    for case in state.get("cases") or []:
        if case.get("case_id") == case_id:
            return case
    return None


def _new_case(
    text: str,
    message_id: str | None,
    turn: int,
    now: float,
    *,
    work_context: dict[str, Any] | None = None,
    interpretation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case_id = "case-" + uuid.uuid4().hex[:10]
    issue = _derive_issue(text)
    return {
        "case_id": case_id,
        "topic": "recharge_failure",
        "issue": {"text": issue, "reported_text": _excerpt(text)},
        "status": "active",
        "stage": "diagnose",
        "model": {
            "value": None,
            "source_message_id": None,
            "source_turn": None,
            "updated_at": None,
        },
        "work_context": copy.deepcopy(work_context) if work_context else None,
        "tried_actions": [],
        "not_tried_actions": [],
        "fact_audit": [],
        "fact_contract_version": 1,
        "observations": [],
        "observation_audit": [],
        "last_observation_result": None,
        "last_asked_actions": {"turn": None, "action_ids": [], "source": "none"},
        "negation_ambiguity": [],
        "suggested_actions": [],
        "feedback_events": [],
        "clarification_history": [],
        "questions": [],
        "closure_evidence": None,
        "weak_confirmation_turn": None,
        "next_step": None,
        "clarification": None,
        "sources": [],
        "last_retrieval": None,
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
        "resolution": None,
        "_source_message_id": message_id,
        "_source_turn": turn,
        "_interpretation_schema": (interpretation or {}).get("schema_version"),
    }


def _derive_issue(text: str) -> str:
    value = _excerpt(text, 80)
    value = re.sub(r"(?:我家|家里|我买的|用的是|机器是|机器人是).*", "", value).strip(" ，,。")
    return value or "回充/充电失败"


_RECHARGE_TOPIC_DISMISSED = re.compile(r"(?:先)?(?:不说|不谈|不聊|别聊).{0,8}(?:回充|充电)")


def is_recharge_related(text: str) -> bool:
    value = str(text or "")
    if _RECHARGE_TOPIC_DISMISSED.search(value):
        return False
    if any(word in value for word in _RECHARGE_WORDS):
        return True
    return bool(_RECHARGE_FAILURE.search(value))


def _other_topics(text: str) -> list[str]:
    value = str(text or "")
    return [name for name, pattern in _OTHER_TOPICS.items() if re.search(pattern, value, re.I)]


def _is_tried_question(text: str) -> bool:
    return bool(_TRIED_QUESTION.search(str(text or "")))


def _add_source(case: dict[str, Any], role: str, message_id: str | None, turn: int, text: str, now: float) -> None:
    if not message_id:
        return
    sources = case.setdefault("sources", [])
    if any(item.get("message_id") == message_id for item in sources):
        return
    sources.append(
        {
            "role": role,
            "message_id": message_id,
            "turn": turn,
            "excerpt": _excerpt(text),
            "at": now,
        }
    )
    del sources[:-20]


def _action_from_text(text: str) -> list[str]:
    value = str(text or "")
    result: list[str] = []
    for action_id, pattern in _ACTION_PATTERNS.items():
        for match in pattern.finditer(value):
            if clause_scope_at(value, match.start()):
                continue
            if (
                claim_rejection(
                    action_id, match.group(0), value, require_execution=True
                )
                is not None
            ):
                continue
            if action_id not in result:
                result.append(action_id)
            break
    return result


def _clause_at(value: str, position: int) -> str:
    start = 0
    for boundary in _CLAUSE_SPLIT.finditer(value):
        if boundary.start() >= position:
            return value[start:boundary.start()]
        start = boundary.end()
    return value[start:]


def _negated_action_ids(text: str) -> set[str]:
    """Negation is scoped to the clause containing the action.

    "没清理触点，清理了充电座周围" must mark only the contact step as
    negated; a fixed character window would incorrectly swallow both actions.
    A correction may withdraw a previous assertion using a longer phrase
    ("其实我没有把它放回充电座"); that path is still clause-scoped and never
    treats a result complement ("没清理干净") as "not tried".
    """
    value = str(text or "")
    correction = bool(_CORRECTION.search(value))
    if not (
        _NEGATED_ACTION.search(value)
        or (correction and _CORRECTION_NEGATION.search(value))
    ):
        return set()
    result: set[str] = set()
    for action_id, pattern in _NEGATED_ACTION_PATTERNS.items():
        for match in pattern.finditer(value):
            if clause_scope_at(value, match.start()):
                continue
            clause = _clause_at(value, match.start())
            if not (
                _NEGATED_ACTION.search(clause)
                or (correction and _CORRECTION_NEGATION.search(clause))
            ):
                continue
            if claim_rejection(action_id, match.group(0), value) is not None:
                continue
            result.add(action_id)
    return result


def _outcome_after_position(text: str, position: int) -> str | None:
    """Outcome explicitly reported after an action mention, not before it.

    "回充失败，已经清理了充电座周围" reports the *initial* failure and an
    execution with no result; it must stay ``unknown`` until the user says the
    cleanup did or did not help.
    """
    value = str(text or "")
    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in _CLAUSE_SPLIT.finditer(value):
        spans.append((start, boundary.start()))
        start = boundary.end()
    spans.append((start, len(value)))
    outcome: str | None = None
    for clause_start, clause_end in spans:
        if clause_end <= position:
            continue
        chunk = value[position:clause_end] if clause_start <= position < clause_end else value[clause_start:clause_end]
        if not chunk.strip():
            continue
        local = _reported_outcome(chunk)
        if local in {"explicit_success", "failure", "partial_success"}:
            outcome = local
        elif local == "weak_positive" and outcome is None:
            outcome = "weak_positive"
    return outcome


def _outcome_for_action(text: str, action_id: str) -> str | None:
    pattern = _ACTION_PATTERNS.get(action_id)
    if pattern is None:
        return None
    positions = [match.start() for match in pattern.finditer(str(text or ""))]
    if not positions:
        return None
    if clause_scope_at(str(text or ""), positions[-1]):
        return None
    return _outcome_after_position(text, positions[-1])


def _semantic_outcomes(proposal: dict[str, Any]) -> dict[str, str]:
    """Compact validated ``executed_actions`` outcomes by action id.

    The proposal has already passed quote/scope validation in
    ``normalize_interpretation``.  This map is the reducer's preferred outcome
    source; the action regex is only a fallback when no semantic outcome exists.
    A same-turn success/failed contradiction stays conservative (unknown).
    """
    values: dict[str, set[str]] = {}
    for item in proposal.get("executed_actions") or []:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or "")
        if action_id not in ACTION_BY_ID:
            continue
        outcome = str(item.get("outcome") or "unknown").strip().lower()
        if outcome not in {"success", "failed", "unknown"}:
            outcome = "unknown"
        values.setdefault(action_id, set()).add(outcome)
    result: dict[str, str] = {}
    for action_id, seen in values.items():
        if "success" in seen and "failed" in seen:
            result[action_id] = "unknown"
        elif "failed" in seen:
            result[action_id] = "failed"
        elif "success" in seen:
            result[action_id] = "success"
        else:
            result[action_id] = "unknown"
    return result


def _extract_model(text: str) -> str | None:
    value = str(text or "")
    for pattern in _MODEL_PATTERNS:
        for match in pattern.finditer(value):
            candidate = match.group(1).strip(" ，,。：:;；")
            if not (2 <= len(candidate) <= 32):
                continue
            if not re.search(r"[A-Za-z]", candidate) or not re.search(r"\d", candidate):
                continue
            if any(word in candidate for word in _MODEL_REJECT):
                continue
            return candidate
    return None


def _latest_primary_suggestion(case: dict[str, Any]) -> dict[str, Any] | None:
    suggestions = case.get("suggested_actions") or []
    for item in reversed(suggestions):
        if item.get("primary") and item.get("status") == "suggested":
            return item
    pending = [item for item in suggestions if item.get("status") == "suggested"]
    return pending[-1] if len(pending) == 1 else None


def _pending_suggestions(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in case.get("suggested_actions") or [] if item.get("status") == "suggested"]


def _record_attempt(
    case: dict[str, Any],
    action_id: str,
    outcome: str,
    message_id: str | None,
    turn: int,
    evidence: str,
    now: float,
) -> bool:
    """Record one user-confirmed attempt, idempotently by effective action id.

    A later execution revokes an old effective ``not_tried`` row for the same
    action (with audit).  Repeated reports never duplicate a row; a stricter
    outcome upgrades the current effective row in place.
    """
    if action_id not in ACTION_BY_ID:
        return False
    normalized = outcome if outcome in {"failed", "success", "unknown"} else "unknown"
    _supersede_not_tried_for_action(
        case,
        action_id,
        message_id=message_id,
        turn=turn,
        text=evidence,
        reason="later_user_execution",
        now=now,
    )
    tried = case.setdefault("tried_actions", [])
    existing = next(
        (
            item
            for item in tried
            if item.get("action_id") == action_id
            and _fact_status(item) == FACT_STATUS_EFFECTIVE
        ),
        None,
    )
    if existing is not None:
        if normalized in {"failed", "success"} and existing.get("outcome") != normalized:
            existing["previous_outcome"] = existing.get("outcome")
            existing["outcome"] = normalized
            existing["evidence"] = _excerpt(evidence)
            existing["source_message_id"] = message_id
            existing["source_turn"] = turn
            existing["updated_at"] = now
        return False
    tried.append(
        {
            "action_id": action_id,
            "label": ACTION_BY_ID[action_id]["label"],
            "user_executed": True,
            "outcome": normalized,
            "evidence": _excerpt(evidence),
            "source_message_id": message_id,
            "source_turn": turn,
            "fact_status": FACT_STATUS_EFFECTIVE,
            "provenance": "user_message",
            "updated_at": now,
        }
    )
    return True


def _mark_suggestion(case: dict[str, Any], action_id: str, status: str, message_id: str | None, turn: int, now: float) -> None:
    for item in reversed(case.get("suggested_actions") or []):
        if item.get("action_id") == action_id and item.get("status") == "suggested":
            item["status"] = status
            item["confirmed_message_id"] = message_id
            item["confirmed_turn"] = turn
            item["updated_at"] = now
            break


def _add_not_tried(
    case: dict[str, Any],
    action_id: str,
    message_id: str | None,
    turn: int,
    text: str,
    now: float,
    *,
    reason: str = "user_negation",
    binding_source: str | None = None,
) -> bool:
    """Assert one effective not-tried fact and retract a contradicting tried row."""
    if action_id not in ACTION_BY_ID:
        return False
    changed = bool(
        _retract_tried_for_action(
            case,
            action_id,
            message_id=message_id,
            turn=turn,
            text=text,
            reason=reason,
            now=now,
        )
    )
    rows = case.setdefault("not_tried_actions", [])
    existing = next(
        (
            item
            for item in rows
            if item.get("action_id") == action_id
            and _fact_status(item) == FACT_STATUS_EFFECTIVE
        ),
        None,
    )
    if existing is not None:
        if existing.get("source_message_id") != message_id:
            existing["evidence"] = _excerpt(text)
            existing["source_message_id"] = message_id
            existing["source_turn"] = turn
            existing["updated_at"] = now
        question_lifecycle.reconcile_questions(case, turn=turn, now=now)
        return changed
    rows.append(
        {
            "action_id": action_id,
            "label": ACTION_BY_ID[action_id]["label"],
            "source_message_id": message_id,
            "source_turn": turn,
            "evidence": _excerpt(text),
            "fact_status": FACT_STATUS_EFFECTIVE,
            "reason": reason,
            "binding_source": binding_source,
            "provenance": "user_message",
            "updated_at": now,
        }
    )
    question_lifecycle.reconcile_questions(case, turn=turn, now=now)
    return True


def _default_work_context() -> dict[str, Any]:
    return {
        "domain": "unknown",
        "topic": "unknown",
        "objects": [],
        "experiencer": "unknown",
        "observable_symptom": {"text": "", "observed_by_user": False, "safety": "none"},
        "ambiguity": [],
        "familiarity": "unknown",
        "interaction_stage": "unknown",
        "wants_full_checklist": False,
        "unknowns": [],
        "information_sources": ["user_message"],
        "source": None,
    }


def _merge_work_context(
    case: dict[str, Any],
    interpretation: dict[str, Any],
    *,
    text: str,
    message_id: str | None,
    turn: int,
    now: float,
    method: str = "fallback",
) -> dict[str, Any]:
    """Merge a validated proposal into typed context without erasing facts.

    ``resolved`` ambiguity stays resolved; a later fallback turn cannot reopen
    it.  User-confirmed fields remain sourced to the user message that set them.
    """
    context = copy.deepcopy(case.get("work_context") or _default_work_context())
    for field in ("domain", "topic", "experiencer", "familiarity", "interaction_stage"):
        value = str(interpretation.get(field) or "unknown")
        if value != "unknown" or not context.get(field):
            context[field] = value
    objects = list(context.get("objects") or [])
    for item in interpretation.get("objects") or []:
        if item != "unknown" and item not in objects:
            objects.append(item)
    context["objects"] = objects
    symptom = interpretation.get("observable_symptom") or {}
    if isinstance(symptom, dict) and (symptom.get("text") or context.get("observable_symptom", {}).get("text")):
        current_symptom = dict(context.get("observable_symptom") or {})
        current_symptom.update(
            {
                "text": symptom.get("text") or current_symptom.get("text") or "",
                "observed_by_user": True,
                "safety": symptom.get("safety") or current_symptom.get("safety") or "none",
            }
        )
        context["observable_symptom"] = current_symptom
    context["wants_full_checklist"] = bool(
        interpretation.get("wants_full_checklist", context.get("wants_full_checklist", False))
    )
    existing_ambiguity = {
        item.get("id"): item for item in context.get("ambiguity") or [] if isinstance(item, dict)
    }
    for item in interpretation.get("ambiguity") or []:
        if not isinstance(item, dict) or item.get("id") not in AMBIGUITY_CANDIDATES:
            continue
        incoming = copy.deepcopy(item)
        current = existing_ambiguity.get(incoming.get("id"))
        if current:
            if current.get("status") == "resolved":
                continue
            if incoming.get("status") == "open" and current.get("status") == "deferred":
                continue
        existing_ambiguity[incoming["id"]] = incoming
    context["ambiguity"] = list(existing_ambiguity.values())
    unknowns = list(context.get("unknowns") or [])
    for item in interpretation.get("unknowns") or []:
        if item and item not in unknowns:
            unknowns.append(item)
    context["unknowns"] = unknowns[:12]
    context["information_sources"] = list(interpretation.get("information_sources") or ["user_message"])
    context["source"] = {
        "message_id": message_id,
        "turn": turn,
        "method": method,
        "at": now,
    }
    case["work_context"] = context
    case["updated_at"] = now
    return context


def _record_feedback(
    case: dict[str, Any],
    outcome: str,
    text: str,
    message_id: str | None,
    turn: int,
    now: float,
) -> None:
    events = case.setdefault("feedback_events", [])
    if any(
        item.get("message_id") == message_id and item.get("outcome") == outcome
        for item in events
    ):
        return
    events.append(
        {
            "outcome": outcome,
            "text": _excerpt(text),
            "source_message_id": message_id,
            "source_turn": turn,
            "at": now,
        }
    )
    del events[:-20]


def _ensure_open_task(state: dict[str, Any], case: dict[str, Any], turn: int) -> dict[str, Any]:
    tasks = state.setdefault("open_tasks", [])
    for task in tasks:
        if task.get("case_id") == case.get("case_id"):
            return task
    task = {
        "task_id": case.get("case_id"),
        "case_id": case.get("case_id"),
        "topic": case.get("topic") or "recharge_failure",
        "status": "open",
        "created_turn": turn,
        "last_confirmed_turn": None,
        "last_prompted_turn": None,
        "source": "user_message",
    }
    tasks.append(task)
    return task


def _mark_open_task(state: dict[str, Any], case_id: str | None, status: str, turn: int | None = None) -> None:
    for task in state.get("open_tasks") or []:
        if task.get("case_id") == case_id:
            task["status"] = status
            if status == "resolved" and turn is not None:
                task["last_confirmed_turn"] = turn
            return


def _queue_old_task_confirmation(state: dict[str, Any], case: dict[str, Any], turn: int) -> None:
    task = _ensure_open_task(state, case, turn)
    if task.get("status") != "open":
        return
    if task.get("last_prompted_turn") is not None:
        return
    state["pending_confirmation"] = {
        "case_id": case.get("case_id"),
        "topic": case.get("topic") or "recharge_failure",
        "created_turn": turn,
        "policy": "answer_new_topic_first_then_at_most_one_light_check",
    }


def _defer_case_questions(
    state: dict[str, Any], case: dict[str, Any], turn: int, reason: str
) -> None:
    question_lifecycle.defer_pending_questions(case, turn=turn, reason=reason)
    _ = state  # explicit: question state is case-scoped


def _cancel_pending_questions(case: dict[str, Any], turn: int, reason: str) -> None:
    for row in case.get("questions") or []:
        if row.get("status") == "pending":
            row["status"] = "cancelled"
            row["cancelled_turn"] = turn
            row["cancel_reason"] = reason


def _sync_question_for_next_step(case: dict[str, Any], turn: int, now: float) -> None:
    """Create at most one pending question from the reducer's next step.

    Only real questions are recorded (clarify/confirm); an action suggestion is
    not a question and must not become a repeat-ask loop.
    """
    next_step = case.get("next_step") or {}
    kind = next_step.get("kind")
    if kind == "confirm":
        action_id = str(next_step.get("action_id") or "")
        if action_id in {"confirm", "soft_close", "handoff"}:
            slot = "weak_closure_result"
        elif action_id:
            slot = f"step_outcome:{action_id}"
        else:
            slot = "tried_step"
        question_lifecycle.ensure_question(
            case,
            slot=slot,
            source="confirm",
            question_text=str(next_step.get("detail") or ""),
            turn=turn,
            evidence={"turn": turn, "reason": "reducer_next_step"},
            now=now,
        )
    elif kind == "clarify":
        action_id = str(next_step.get("action_id") or "")
        if action_id == "clarify" and "哪一步" in str(next_step.get("detail") or ""):
            question_lifecycle.ensure_question(
                case,
                slot="tried_step",
                source="clarify",
                question_text=str(next_step.get("detail") or ""),
                turn=turn,
                evidence={"turn": turn, "reason": "reducer_next_step"},
                now=now,
            )
    elif kind == "observe":
        capability = str(next_step.get("capability") or "")
        if capability:
            question_lifecycle.ensure_question(
                case,
                slot=f"capability_observation:{capability}",
                source="capability_observation",
                question_text=str(next_step.get("detail") or ""),
                turn=turn,
                evidence={
                    "turn": turn,
                    "reason": next_step.get("reason") or "capability_observation",
                },
                now=now,
            )


def classify_slot_answer(state: dict[str, Any], text: str) -> dict[str, Any] | None:
    """Return a deterministic answer to the current slot, when unambiguous.

    This is used only for explicit, schema-verifiable answers (the binary dock
    referent, explicit weak-closure result).  Ambiguity/new symptoms continue to
    the semantic interpreter.
    """
    case = _active_case(state)
    if case is None or case.get("status") != "active":
        return None
    value = str(text or "")
    slots = {
        row.get("slot")
        for row in case.get("questions") or []
        if row.get("status") in {"pending", "deferred"}
    }
    if "dock_find_referent" in slots:
        user_find = bool(_USER_FIND_DOCK.search(value))
        robot_return = bool(_DOCK_CLEAR_ROBOT.search(value))
        if user_find != robot_return:
            return {
                "slot": "dock_find_referent",
                "value": "user_cannot_find_dock" if user_find else "robot_cannot_return",
                "reason": "unambiguous_binary_slot_answer",
            }
    if "weak_closure_result" in slots:
        outcome = _reported_outcome(value)
        if outcome == "explicit_success":
            return {
                "slot": "weak_closure_result",
                "value": "explicit_success",
                "reason": "explicit_success_slot_answer",
            }
        if outcome in {"failure", "partial_success"}:
            return {
                "slot": "weak_closure_result",
                "value": outcome,
                "reason": "explicit_failure_slot_answer",
            }
    return None


def protocol_ack_answer(state: dict[str, Any]) -> str:
    """Deterministic, model-free reply to a pure thanks/greeting.

    It must not claim resolution; the weak positive signal stays a weak signal.
    """
    case = _active_case(state)
    if case is None:
        return "不客气。如果你还有扫地/扫拖机器人的具体问题，可以继续描述现象。"
    return (
        "不客气。刚才的回充问题我这边还没有确认解决；"
        "如果之后能正常自动回充，或还是回不去，再把结果告诉我就行。"
    )


def mark_protocol_ack(state: dict[str, Any], turn: int | None = None) -> dict[str, Any]:
    """Low-cost protocol route: no new question, no resolution claim."""
    state = load_state(state)
    case = _active_case(state)
    if case is None:
        return state
    case["stage"] = "closing"
    case["clarification"] = None
    case["next_step"] = {
        "action_id": "soft_close",
        "label": "礼貌收束",
        "detail": "用户只是致谢/寒暄；可以礼貌结束，但不宣称已解决。",
        "kind": "soft_close",
    }
    question_lifecycle.defer_pending_questions(
        case,
        turn=int(turn if turn is not None else state.get("turn") or 0),
        reason="protocol_ack_no_new_question",
    )
    state["decision_snapshot"] = decision_snapshot(state)
    return state


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _snapshot_id(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return "snap-" + digest[:12]


def _integration_context_input(state: dict[str, Any]) -> dict[str, Any]:
    """Compact interpreter input: needed facts + pending question only.

    The full transcript is never replayed.  This is the evidence that prompt
    compression is not silently dropping confirmed user facts.
    """
    case = _active_case(state)
    if case is None:
        return {"topic": "unknown", "needed_facts": [], "pending_question": None}
    turn = int(state.get("turn") or 0) + 1
    question = question_lifecycle.current_question(case, turn)
    if question is None:
        # Still expose an unanswered/deferred question to the interpreter as a
        # needed fact, even when the frequency policy says "do not ask now".
        for row in reversed(case.get("questions") or []):
            if row.get("status") in {"pending", "deferred"}:
                question = row
                break
    context = case.get("work_context") or {}
    return {
        "topic": context.get("topic") or case.get("topic") or "unknown",
        "issue": (case.get("issue") or {}).get("text"),
        "needed_facts": {
            "experiencer": context.get("experiencer") or "unknown",
            "tried": [
                [item.get("action_id"), item.get("outcome")]
                for item in _effective_tried(case)
            ],
            "not_tried": [item.get("action_id") for item in _effective_not_tried(case)],
            "capabilities": [
                [item.get("capability"), item.get("result")]
                for item in diagnostic.observations_summary(case)
            ],
            "ambiguity": [
                [item.get("id"), item.get("status"), item.get("resolution")]
                for item in context.get("ambiguity") or []
            ],
            "closure": (case.get("closure_evidence") or {}).get("level"),
        },
        "pending_question": (
            {"slot": question.get("slot"), "text": question.get("question_text")}
            if question
            else None
        ),
    }


def decision_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """One decision snapshot shared by prompt context and answer generation.

    It exposes only the current askable question, confirmed facts and a short
    deferred-task summary.  Old clarifications are not re-injected; answered
    scope is explicit so the model cannot "forget" that it already asked.
    """
    state = load_state(state)
    turn = int(state.get("turn") or 0)
    scope = str(state.get("last_turn_scope") or "recharge")
    active = _active_case(state)
    current = active if (active is not None and scope != "other") else None
    summary = question_lifecycle.question_summary(current, turn + 1)
    if current is not None:
        question_lifecycle.reconcile_questions(
            current, turn=turn + 1
        )
        summary = question_lifecycle.question_summary(current, turn + 1)
    confirmed: dict[str, Any] = {
        "model": ((current or {}).get("model") or {}).get("value"),
        "tried": [
            {
                "action_id": item.get("action_id"),
                "label": item.get("label"),
                "outcome": item.get("outcome"),
                "source_turn": item.get("source_turn"),
                "source_message_id": item.get("source_message_id"),
            }
            for item in _effective_tried(current)
        ],
        "not_tried": [
            {"action_id": item.get("action_id"), "label": item.get("label")}
            for item in _effective_not_tried(current)
        ],
        "closure": copy.deepcopy((current or {}).get("closure_evidence")),
        "observations": copy.deepcopy(diagnostic.observations_summary(current)),
        "work_context": copy.deepcopy((current or {}).get("work_context")),
    }
    deferred_tasks: list[dict[str, Any]] = []
    for task in state.get("open_tasks") or []:
        if task.get("status") not in {"open", "deferred"}:
            continue
        if current is not None and task.get("case_id") == current.get("case_id"):
            continue
        reminder_allowed = False
        pending = state.get("pending_confirmation") or {}
        if pending.get("case_id") == task.get("case_id"):
            reminder_allowed = (
                task.get("last_prompted_turn") is None
                and pending.get("created_turn") == turn
            )
        deferred_tasks.append(
            {
                "case_id": task.get("case_id"),
                "topic": task.get("topic"),
                "status": task.get("status"),
                "reminder_allowed": reminder_allowed,
                "last_prompted_turn": task.get("last_prompted_turn"),
            }
        )
    if current is None and active is not None and not deferred_tasks:
        deferred_tasks.append(
            {
                "case_id": active.get("case_id"),
                "topic": active.get("topic"),
                "status": next(
                    (
                        task.get("status")
                        for task in state.get("open_tasks") or []
                        if task.get("case_id") == active.get("case_id")
                    ),
                    "open",
                ),
                "reminder_allowed": False,
                "last_prompted_turn": None,
            }
        )
    payload = {
        "schema_version": "support-decision-snapshot/v1",
        "snapshot_id": None,
        "turn": turn,
        "last_turn_scope": scope,
        "active_case_id": (current or {}).get("case_id"),
        "case_status": (current or {}).get("status"),
        "topic": ((current or {}).get("work_context") or {}).get("topic")
        or (current or {}).get("topic"),
        "available_scope": ["new_topic"] if current is None else ["active_case"],
        "confirmed_facts": confirmed,
        "current_question": summary.get("current_question"),
        "answered_scope": summary.get("answered_scope"),
        "deferred_scope": summary.get("deferred_scope"),
        "blocked_unanswered": summary.get("blocked_unanswered"),
        "must_not_ask": summary.get("must_not_ask"),
        "deferred_tasks": deferred_tasks,
        "route": copy.deepcopy(state.get("last_decision")),
        "interpreter_input": _integration_context_input(state),
        "diagnostic_scope": copy.deepcopy(diagnostic.derive(current)),
    }
    payload["snapshot_id"] = _snapshot_id(payload)
    return payload


def _clarification_asked(case: dict[str, Any], ambiguity_id: str) -> bool:
    return any(
        item.get("ambiguity_id") == ambiguity_id and item.get("asked_turn") is not None
        for item in case.get("clarification_history") or []
    )


def _mark_clarification_answered(case: dict[str, Any], ambiguity_id: str, turn: int) -> None:
    for item in case.get("clarification_history") or []:
        if item.get("ambiguity_id") == ambiguity_id and item.get("answered_turn") is None:
            item["answered_turn"] = turn
            item["status"] = "answered"


def _ambiguity_item(case: dict[str, Any], ambiguity_id: str) -> dict[str, Any] | None:
    for item in ((case.get("work_context") or {}).get("ambiguity") or []):
        if item.get("id") == ambiguity_id:
            return item
    return None


def _evidence(text: str, message_id: str | None, turn: int) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "turn": turn,
        "quote": _excerpt(text, 120),
    }


def _resolve_ambiguity_from_text(
    case: dict[str, Any], text: str, turn: int, message_id: str | None = None
) -> str | None:
    """Resolve the one dock-referent ambiguity from a clear user follow-up.

    An earlier answer can be corrected; the old answer is kept in
    ``answer_history`` by the question record.  Without an explicit correction a
    resolved ambiguity is stable and is never reopened by a fallback turn.
    """
    item = _ambiguity_item(case, "dock_find_referent")
    if item is None:
        return None
    value = str(text or "")
    new_resolution = None
    if _USER_FIND_DOCK.search(value) and not _DOCK_CLEAR_ROBOT.search(value):
        new_resolution = "user_cannot_find_dock"
    elif _DOCK_CLEAR_ROBOT.search(value) and not _USER_FIND_DOCK.search(value):
        new_resolution = "robot_cannot_return"
    if item.get("status") == "resolved":
        old = item.get("resolution")
        if new_resolution and new_resolution != old and _CORRECTION.search(value):
            question_lifecycle.reopen_question(
                case,
                "dock_find_referent",
                turn=turn,
                reason="user_correction_with_new_evidence",
                evidence=_evidence(value, message_id, turn),
                question_text=_DOCK_AMBIGUITY_QUESTION,
            )
        else:
            question_lifecycle.reconcile_questions(case, turn=turn)
            return old
    if not new_resolution:
        return None
    item["status"] = "resolved"
    item["resolution"] = new_resolution
    item["resolved_turn"] = turn
    context = case.setdefault("work_context", _default_work_context())
    context["experiencer"] = "user" if new_resolution == "user_cannot_find_dock" else "robot"
    case["clarification"] = None
    _mark_clarification_answered(case, "dock_find_referent", turn)
    question_lifecycle.answer_question(
        case,
        "dock_find_referent",
        turn=turn,
        value=new_resolution,
        evidence=_evidence(value, message_id, turn),
        reason="explicit_user_answer",
    )
    return new_resolution


def _apply_ambiguity(
    case: dict[str, Any],
    text: str,
    message_id: str | None,
    turn: int,
    now: float,
) -> str | None:
    """Ask one binary dock clarification only when it changes the next step.

    The question record is the authority: if it was already asked and is still
    unanswered, do not loop every turn; defer it with a reason.  A deferred
    question may be re-asked once after a gap and only with an explicit reason.
    """
    resolution = _resolve_ambiguity_from_text(case, text, turn, message_id)
    if resolution:
        item = _ambiguity_item(case, "dock_find_referent")
        if item:
            item["status"] = "resolved"
            item["resolution"] = resolution
        return None
    context = case.get("work_context") or _default_work_context()
    case["work_context"] = context
    item = _ambiguity_item(case, "dock_find_referent")
    if item is None or item.get("status") == "resolved":
        return None
    row = question_lifecycle.find_question(case, "dock_find_referent")
    if row is None:
        row = question_lifecycle.ensure_question(
            case,
            slot="dock_find_referent",
            source="ambiguity",
            question_text=_DOCK_AMBIGUITY_QUESTION,
            turn=turn,
            evidence=_evidence(text, message_id, turn),
            now=now,
        )
    return_to_topic = bool(_RECHARGE_FAILURE.search(str(text or ""))) or is_recharge_related(str(text or ""))
    if (
        row.get("status") == "deferred"
        and int(row.get("ask_count") or 0) > 0
        and int(row.get("ask_count") or 0) < question_lifecycle.MAX_ASKS
        and turn - int(row.get("last_asked_turn") or 0) >= question_lifecycle.REASK_MIN_GAP
        and return_to_topic
    ):
        # One reasoned re-ask: a deferred question is not an answered one.  The
        # record keeps the reason and the user evidence for this re-ask.
        question_lifecycle.reopen_question(
            case,
            "dock_find_referent",
            turn=turn,
            reason="unanswered_reask_after_gap",
            evidence=_evidence(text, message_id, turn),
            question_text=_DOCK_AMBIGUITY_QUESTION,
            now=now,
        )
        row = question_lifecycle.find_question(case, "dock_find_referent") or row
    can_ask, reason = question_lifecycle.can_ask(row, turn)
    if not can_ask:
        question_lifecycle.defer_question(
            case,
            "dock_find_referent",
            turn=turn,
            reason=reason,
            now=now,
        )
        item["status"] = "deferred"
        case["clarification"] = None
        case["stage"] = "diagnose"
        case["next_step"] = dict(_DOCK_PLACE_STEP)
        return None
    if int(row.get("ask_count") or 0) > 0:
        question_lifecycle.reopen_question(
            case,
            "dock_find_referent",
            turn=turn,
            reason="unanswered_reask_after_gap",
            evidence=_evidence(text, message_id, turn),
            question_text=_DOCK_AMBIGUITY_QUESTION,
            now=now,
        )
    item["status"] = "open"
    context.setdefault("ambiguity", [])
    case.setdefault("clarification_history", []).append(
        {
            "ambiguity_id": "dock_find_referent",
            "question": _DOCK_AMBIGUITY_QUESTION,
            "asked_turn": turn,
            "source_message_id": message_id,
            "asked_at": now,
            "status": "asked",
        }
    )
    case["clarification"] = _DOCK_AMBIGUITY_QUESTION
    case["stage"] = "need_info"
    case["next_step"] = {
        "action_id": "clarify",
        "label": "确认是哪种“找不到”",
        "detail": _DOCK_AMBIGUITY_QUESTION,
        "kind": "clarify",
    }
    return _DOCK_AMBIGUITY_QUESTION


def _diagnosis_next_step(
    case: dict[str, Any],
    text: str,
    *,
    interpretation: dict[str, Any] | None = None,
    turn: int | None = None,
) -> dict[str, Any]:
    """Progressive diagnosis: one lowest-cost necessary next step.

    The default catalogue order is kept as a fallback, but a clear observable
    symptom or an explicitly user-side problem can move a different low-cost
    step ahead.  No repair advice is invented here.
    """
    context = case.get("work_context") or {}
    symptom = str((context.get("observable_symptom") or {}).get("text") or text or "")
    if (context.get("observable_symptom") or {}).get("safety") == "possible" or _SAFETY_SYMPTOM.search(
        str(text or "")
    ):
        return dict(_SAFETY_STEP)
    item = _ambiguity_item(case, "dock_find_referent")
    if item is not None and item.get("status") == "resolved" and item.get("resolution") == "user_cannot_find_dock":
        return dict(_DOCK_PLACE_STEP)
    if context.get("experiencer") == "user":
        return dict(_DOCK_PLACE_STEP)
    tried_ids = {entry.get("action_id") for entry in _effective_tried(case)}
    preferred: list[str] = []
    if _POWER_SYMPTOM.search(str(text or "")):
        preferred.append("check_power")
    if re.search(r"触点.{0,6}(?:脏|黑|氧化|灰)|(?:脏|黑|氧化|灰).{0,6}触点", symptom):
        preferred.append("clean_contacts")
    if re.search(r"传感器.{0,6}(?:脏|灰|挡)|(?:脏|灰|挡).{0,6}传感器", symptom):
        preferred.append("clean_sensors")
    if re.search(r"打转|转圈|对不上|对准", symptom):
        preferred.extend(["clean_sensors", "reseat_dock"])
    for action_id in preferred:
        if action_id not in tried_ids:
            action = ACTION_BY_ID.get(action_id)
            if action:
                return {
                    "action_id": action["id"],
                    "label": action["label"],
                    "detail": action["detail"],
                    "kind": "action",
                }
    generic = _next_step_for(case, text, turn=turn)
    capability_step = _capability_observation_step(case, turn)
    if capability_step is not None:
        scope = diagnostic.derive(case) or {}
        goal = str(scope.get("task_goal") or "")
        has_non_goal_observation = any(
            str(row.get("capability")) != goal
            for row in scope.get("verified_capabilities") or []
        )
        if not has_non_goal_observation:
            # A bare initial failure observation is not enough to replace the
            # established step-outcome confirm / action flow.
            return generic
        if generic.get("kind") == "confirm":
            return capability_step
        if str(scope.get("task_goal_status")) == "unknown":
            # The current task goal has not been measured yet; a generic next
            # action would not resolve that missing observation.
            return capability_step
        if capability_step.get("capability") == "dock_power" and str(
            scope.get("task_goal_status")
        ) == "fails":
            known = {
                str(row.get("capability")): str(row.get("result"))
                for row in scope.get("verified_capabilities") or []
            }
            relevant = {
                capability: result
                for capability, result in known.items()
                if capability in {"automatic_return", "manual_charging"}
            }
            if relevant and all(result == "fails" for result in relevant.values()):
                # Both return and manual-charging observations already failed;
                # do not recycle a manual measurement, ask for the supply
                # observation instead.
                return capability_step
    return generic


def _negated_next_step(case: dict[str, Any], negated_ids: set[str]) -> bool:
    tried_ids = {item.get("action_id") for item in _effective_tried(case)}
    for action_id in negated_ids:
        candidate = ACTION_BY_ID.get(action_id)
        # Observation-only identities (replace/move, high-risk reset) are never
        # promoted to an automatic next step.
        if candidate and candidate.get("auto_next", True) and candidate["id"] not in tried_ids:
            case["next_step"] = {
                "action_id": candidate["id"],
                "label": candidate["label"],
                "detail": "你刚才说还没做这一步；先按资料尝试后再反馈结果。" + candidate["detail"],
                "kind": "action",
            }
            return True
    return False


def _capability_observation_step(case: dict[str, Any], turn: int | None) -> dict[str, Any] | None:
    scope = diagnostic.derive(case)
    step = diagnostic.next_observation(scope, case, turn)
    if not step:
        return None
    return {
        "action_id": f"observe_{step['capability']}",
        "capability": step["capability"],
        "reason": step.get("reason"),
        "label": step["label"],
        "detail": step["detail"],
        "kind": "observe",
    }


def _measurement_known_for_action(
    case: dict[str, Any], action: dict[str, Any], turn: int | None
) -> bool:
    capability = diagnostic.ACTION_CAPABILITY.get(str(action.get("action_id") or ""))
    observation = diagnostic.effective_by_capability(case).get(capability or "")
    if not observation:
        return False
    action_turn = int(action.get("source_turn") or 0)
    observation_turn = int(observation.get("turn") or 0)
    # A new intervention in the current turn needs a fresh observation; an older
    # fact must not silently answer the new measurement.
    if turn is not None and action_turn == int(turn) and observation_turn < action_turn:
        return False
    return True


def _compute_next_step(case: dict[str, Any], turn: int | None = None) -> dict[str, Any]:
    tried = _effective_tried(case)
    for item in tried:
        if item.get("outcome") != "unknown":
            continue
        if _measurement_known_for_action(case, item, turn):
            continue
        return {
            "action_id": item["action_id"],
            "label": "确认步骤结果",
            "detail": f"你已做过「{item['label']}」，现在能正常充上电吗？",
            "kind": "confirm",
            "reason": (
                "after_new_intervention"
                if turn is not None and int(item.get("source_turn") or 0) == int(turn)
                else "unknown_step_outcome"
            ),
        }
    tried_ids = {item.get("action_id") for item in tried}
    for action in ACTION_CATALOG:
        if not action.get("auto_next", True):
            continue
        if action["id"] not in tried_ids:
            return {
                "action_id": action["id"],
                "label": action["label"],
                "detail": action["detail"],
                "kind": "action",
            }
    return {
        "action_id": "handoff",
        "label": "整理转人工摘要",
        "detail": "已按资料完成可尝试的步骤仍未恢复，可整理摘要后联系官方售后检测。",
        "kind": "handoff",
    }


def _next_step_for(case: dict[str, Any], text: str, turn: int | None = None) -> dict[str, Any]:
    next_step = _compute_next_step(case, turn)
    tried_ids = {item.get("action_id") for item in _effective_tried(case)}
    # If the user already reports a power symptom, checking the dock supply is
    # the honest next step even though cleaning contacts is earlier in the
    # generic order.  This keeps the displayed next step consistent with the
    # answer instead of recommending a step that cannot explain "no light".
    if (
        next_step.get("kind") == "action"
        and _POWER_SYMPTOM.search(str(text or ""))
        and "check_power" not in tried_ids
    ):
        action = ACTION_BY_ID["check_power"]
        return {
            "action_id": action["id"],
            "label": action["label"],
            "detail": action["detail"],
            "kind": "action",
        }
    return next_step


def _set_suggestion(case: dict[str, Any], message_id: str | None, turn: int, now: float) -> None:
    next_step = case.get("next_step") or {}
    action_id = next_step.get("action_id")
    if not action_id or action_id == "handoff" or action_id not in ACTION_BY_ID:
        return
    suggestions = case.setdefault("suggested_actions", [])
    # Keep the list bounded; only unresolved/latest suggestions matter for
    # linking a later "还是不行".
    suggestions[:] = [item for item in suggestions if item.get("status") == "suggested"][-3:]
    existing = next((item for item in suggestions if item.get("action_id") == action_id), None)
    if existing is not None:
        existing.update(
            {
                "primary": True,
                "status": "suggested",
                "source_message_id": message_id,
                "source_turn": turn,
                "updated_at": now,
            }
        )
        return
    suggestions.append(
        {
            "action_id": action_id,
            "label": ACTION_BY_ID[action_id]["label"],
            "primary": True,
            "status": "suggested",
            "source_message_id": message_id,
            "source_turn": turn,
            "updated_at": now,
        }
    )


def _make_public_case(case: dict[str, Any] | None) -> dict[str, Any] | None:
    if case is None:
        return None
    tried = [
        {
            "action_id": item.get("action_id"),
            "label": display_action_label(
                str(item.get("action_id") or ""),
                item.get("label"),
                item.get("evidence"),
            ),
            "outcome": item.get("outcome"),
            "user_executed": bool(item.get("user_executed")),
            "source_message_id": item.get("source_message_id"),
            "source_turn": item.get("source_turn"),
            "evidence": item.get("evidence"),
        }
        for item in _effective_tried(case)
    ]
    suggestions = [
        {
            "action_id": item.get("action_id"),
            "label": item.get("label"),
            "status": item.get("status"),
            "source_message_id": item.get("source_message_id"),
            "source_turn": item.get("source_turn"),
        }
        for item in case.get("suggested_actions") or []
    ]
    next_step = case.get("next_step")
    return {
        "case_id": case.get("case_id"),
        "topic": case.get("topic"),
        "issue": (case.get("issue") or {}).get("text"),
        "status": case.get("status"),
        "stage": case.get("stage"),
        "work_context": copy.deepcopy(case.get("work_context")),
        "feedback_events": copy.deepcopy(case.get("feedback_events") or []),
        "clarification_history": copy.deepcopy(case.get("clarification_history") or []),
        "questions": copy.deepcopy(case.get("questions") or []),
        "closure_evidence": copy.deepcopy(case.get("closure_evidence")),
        "model": (case.get("model") or {}).get("value"),
        "model_source_turn": (case.get("model") or {}).get("source_turn"),
        "tried_actions": tried,
        "not_tried_actions": [
            {
                "action_id": item.get("action_id"),
                "label": display_action_label(
                    str(item.get("action_id") or ""),
                    item.get("label"),
                    item.get("evidence"),
                ),
                "source_turn": item.get("source_turn"),
            }
            for item in _effective_not_tried(case)
        ],
        "suggested_actions": suggestions,
        "fact_audit": copy.deepcopy(case.get("fact_audit") or []),
        "observations": copy.deepcopy(diagnostic.observations_summary(case)),
        "diagnostic_scope": copy.deepcopy(diagnostic.derive(case)),
        "negation_ambiguity": copy.deepcopy(case.get("negation_ambiguity") or []),
        "last_asked_actions": copy.deepcopy(case.get("last_asked_actions")),
        "next_step": copy.deepcopy(next_step),
        "clarification": case.get("clarification"),
        "sources": copy.deepcopy(case.get("sources") or []),
        "last_retrieval": copy.deepcopy(case.get("last_retrieval")),
        "created_at": case.get("created_at"),
        "updated_at": case.get("updated_at"),
        "resolved_at": case.get("resolved_at"),
        "resolution": case.get("resolution"),
    }


def public_view(state: dict[str, Any]) -> dict[str, Any]:
    """Fields safe for the product UI / SSE; never includes internal prompts."""
    active = _active_case(state)
    return {
        "schema_version": SCHEMA_VERSION,
        "turn": state.get("turn"),
        "active_case": _make_public_case(active),
        "cases": [
            {
                "case_id": case.get("case_id"),
                "issue": (case.get("issue") or {}).get("text"),
                "status": case.get("status"),
                "stage": case.get("stage"),
                "created_at": case.get("created_at"),
                "resolved_at": case.get("resolved_at"),
            }
            for case in state.get("cases") or []
        ],
        "open_tasks": copy.deepcopy(state.get("open_tasks") or []),
        "pending_confirmation": copy.deepcopy(state.get("pending_confirmation")),
        "last_interpretation": copy.deepcopy(state.get("last_interpretation")),
        "last_retrieval": copy.deepcopy(state.get("last_retrieval")),
        "last_turn_scope": state.get("last_turn_scope"),
        "last_decision": copy.deepcopy(state.get("last_decision")),
        "decision_snapshot": copy.deepcopy(state.get("decision_snapshot")),
        "last_question_audit": copy.deepcopy(state.get("last_question_audit")),
        "last_turn_metrics": copy.deepcopy(state.get("last_turn_metrics")),
        "updated_at": state.get("updated_at"),
        "updated_at_iso": _iso(state.get("updated_at")),
    }


def prompt_context(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> str:
    """Compact state block built from the *same* decision snapshot.

    Dynamic question text lives here, never in the system prompt.  The block
    exposes only the current askable question, confirmed facts, answered scope
    and a short deferred-task summary; old clarifications are not re-injected.
    """
    state = load_state(state)
    snapshot = snapshot or decision_snapshot(state)
    scope = snapshot.get("last_turn_scope")
    current_case_id = snapshot.get("active_case_id")
    case = _active_case(state) if current_case_id else None
    lines = [
        "【服务端售后状态｜同一 decision snapshot；事实只来自用户消息，助手建议不得当成用户已做】",
        "决策快照：{}；轮次：{}；本轮范围：{}；路由：{}（{}）".format(
            snapshot.get("snapshot_id"),
            snapshot.get("turn"),
            scope,
            (snapshot.get("route") or {}).get("route") or "unknown",
            (snapshot.get("route") or {}).get("reason") or "unknown",
        ),
    ]
    if case is not None:
        lines.append(
            f"当前案例：{case.get('case_id')}；问题：{(case.get('issue') or {}).get('text')}；阶段：{case.get('stage')}"
        )
        context = case.get("work_context") or {}
        if context:
            symptom = context.get("observable_symptom") or {}
            source = context.get("source") or {}
            lines.append("【工作语境｜服务端 typed context，来源仅用户消息；unknown 不得补默认值】")
            lines.append(
                "领域/主题：{domain} / {topic}；对象：{objects}；经验者：{experiencer}；用户熟悉度：{familiarity}；交互阶段：{stage}".format(
                    domain=context.get("domain") or "unknown",
                    topic=context.get("topic") or "unknown",
                    objects="、".join(context.get("objects") or []) or "unknown",
                    experiencer=context.get("experiencer") or "unknown",
                    familiarity=context.get("familiarity") or "unknown",
                    stage=context.get("interaction_stage") or "unknown",
                )
            )
            if symptom.get("text"):
                lines.append(
                    "用户观察到的现象：{}；安全风险：{}（现象用用户原话，不先替用户下诊断）".format(
                        symptom.get("text"), symptom.get("safety") or "none"
                    )
                )
            if source.get("message_id"):
                lines.append(
                    f"语境来源：用户第{source.get('turn')}轮消息 {source.get('message_id')}；抽取方式：{source.get('method') or 'unknown'}"
                )
        model = (case.get("model") or {}).get("value")
        if model:
            lines.append(
                f"用户提供的型号：{model}（用户第{(case.get('model') or {}).get('source_turn')}轮；不要猜测其它型号）"
            )
        else:
            lines.append("用户提供的型号：未提供（型号不是必问项，只在确有必要时问一次）")
        scope_block = diagnostic.render_prompt_block(snapshot.get("diagnostic_scope"))
        if scope_block:
            lines.append(scope_block)
        tried = _effective_tried(case)
        if tried:
            lines.append("用户已确认执行过的步骤（不得说成未做，也不得重复建议已失败步骤）：")
            for item in tried:
                outcome = {"failed": "失败", "success": "成功/恢复", "unknown": "已做但结果未知"}.get(
                    item.get("outcome"), "结果未知"
                )
                lines.append(
                    "- {}：{}（用户第{}轮，消息 {}）".format(
                        display_action_label(
                            str(item.get("action_id") or ""),
                            item.get("label"),
                            item.get("evidence"),
                        ),
                        outcome,
                        item.get("source_turn"),
                        item.get("source_message_id"),
                    )
                )
        else:
            lines.append("用户已确认执行过的步骤：暂无")
        not_tried = _effective_not_tried(case)
        if not_tried:
            lines.append(
                "用户明确表示没试过的步骤（不能标为已试）："
                + "、".join(
                    display_action_label(
                        str(item.get("action_id") or ""),
                        item.get("label"),
                        item.get("evidence"),
                    )
                    for item in not_tried
                )
            )
        suggestions = [item for item in case.get("suggested_actions") or [] if item.get("status") == "suggested"]
        if suggestions:
            lines.append("助手最近建议（仅建议，用户尚未确认执行）：")
            for item in suggestions[-2:]:
                lines.append(f"- {item.get('label')}（助手第{item.get('source_turn')}轮）")
        answered = snapshot.get("answered_scope") or []
        if answered:
            lines.append("【已回答范围｜不得再问，也不得把回答改回待确认】")
            for item in answered:
                quote = ((item.get("answer_evidence") or {}).get("quote") or "")[:60]
                lines.append(
                    "- {}={}（第{}轮{}）".format(
                        item.get("slot"),
                        item.get("answer_value"),
                        item.get("resolved_turn"),
                        f"，证据：“{quote}”" if quote else "",
                    )
                )
        current = snapshot.get("current_question")
        if current:
            lines.append("【本轮唯一可问问题｜只问这一个，不要附加已答问题】")
            lines.append(
                "问题记录 {}：slot={}；问法：{}；已问次数：{}；理由：{}".format(
                    current.get("question_id"),
                    current.get("slot"),
                    current.get("question_text"),
                    current.get("ask_count"),
                    current.get("ask_reason"),
                )
            )
        else:
            lines.append(
                "【本轮没有需要新增的澄清问题】不要重复 history 中已回答/已暂缓的问题；"
                "不要自行补问；只回答本轮用户消息并推进一个下一步。"
            )
        blocked = snapshot.get("blocked_unanswered") or []
        if blocked:
            lines.append(
                "【仍未回答但已达重问上限】"
                + "；".join(f"{item.get('slot')}（已问{item.get('ask_count')}次）" for item in blocked)
                + "。这些仍不是已答事实；不要宣称已问出结果。"
            )
        next_step = case.get("next_step") or {}
        if next_step:
            lines.append(f"本轮下一步：{next_step.get('label')}（{next_step.get('detail')}）")
        next_kind = next_step.get("kind")
        if next_kind == "safety":
            lines.append(
                "安全优先：先停止使用并说明风险；安全处理只能引用本轮实际检索到的资料，"
                "资料不足时建议联系官方售后；不要编造维修步骤，不要建议恢复出厂、拆机或打磨触点。"
            )
        if context.get("wants_full_checklist"):
            lines.append(
                "用户明确要完整清单：可以给有序多项，但标明顺序、观察点和停止条件；"
                "仍要把助手建议与用户已执行分开。"
            )
        if context.get("familiarity") == "novice":
            lines.append("用户自述不熟悉：不要要求用户懂术语；先给一个最低成本、可观察的动作，等反馈再分支。")
        if (case.get("closure_evidence") or {}).get("level") == "weak_positive":
            lines.append(
                "弱正向信号：可以礼貌收束，但不得说“已解决/已修好”；如仍不清楚结果，最多轻确认一次。"
            )
        retrieval = case.get("last_retrieval")
        if isinstance(retrieval, dict) and retrieval.get("results"):
            names = []
            for result in retrieval["results"][:4]:
                name = str(result.get("source") or "")
                if name and name not in names:
                    names.append(name)
            if names:
                lines.append("本轮检索证据来源：" + "、".join(names))
        sources = case.get("sources") or []
        if sources:
            lines.append("最近用户来源摘录：")
            for source in sources[-3:]:
                lines.append(f"- 第{source.get('turn')}轮：{str(source.get('excerpt') or '')[:80]}")
    else:
        lines.append("【本轮范围｜新主题优先】先完整回答用户当前的新主题；旧任务只作摘要，不打断新问题。")
    deferred = snapshot.get("deferred_tasks") or []
    if deferred:
        lines.append("【暂缓旧任务｜不每轮提醒，不重复具体旧问题】")
        for task in deferred:
            lines.append(
                "- {topic}（{case_id}，status={status}，last_prompted_turn={last}）".format(
                    topic=task.get("topic"),
                    case_id=task.get("case_id"),
                    status=task.get("status"),
                    last=task.get("last_prompted_turn"),
                )
            )
            if task.get("reminder_allowed"):
                lines.append(
                    "  本轮允许最多一次轻量确认“之前的回充问题现在解决了吗”；"
                    "不要重复已经回答过的具体澄清问题，不要宣称旧问题已解决。"
                )
            else:
                lines.append("  本轮不要提醒、不要追问该旧任务。")
    lines.append(
        "回答要求：用中文；只引用本轮检索到的资料文件名；用户问“刚才试过什么”时只按上面的已试列表回答；"
        "不要声称用户做了未确认的操作；不要重复建议已失败步骤；资料不足或检索失败时明确说明并给出可恢复的下一步。"
        "渐进诊断：按上面的工作语境选一个最低成本的必要澄清/安全检查；说明观察点，等用户反馈后再分支；"
        "不要默认用户会排查，也不要把完整清单当唯一答案。"
    )
    lines.append(
        "安全边界：清障、清触点、清传感器是不同步骤，不得合并成用户已做；不要主动建议恢复出厂、清空地图、拆机或打磨充电触点等高风险操作；"
        "更换适配器只能在资料支持时提及，并提醒联系官方售后或专业检测。"
    )
    return "\n".join(lines)


def _changed(state: dict[str, Any], now: float) -> dict[str, Any]:
    state["updated_at"] = now
    return state


def _direct_answer(
    state: dict[str, Any],
    case: dict[str, Any],
    text: str,
    message_id: str | None,
    turn: int,
    now: float,
) -> str | None:
    if _is_tried_question(text):
        answer = query_tried_answer(state)
        case["stage"] = "diagnose"
        _add_source(case, "user", message_id, turn, text, now)
        return answer
    if case.get("clarification"):
        # The clarification was already asked; do not ask it in a loop.  The
        # next turn's deterministic update clears it if it can link the step.
        return None
    return None


def query_tried_answer(state: dict[str, Any]) -> str:
    case = _active_case(state)
    if case is None:
        return "目前这个会话没有记录中的回充故障步骤。你可以先描述具体现象，我再按记录帮你梳理。"
    tried = _effective_tried(case)
    if not tried:
        answer = (
            "根据服务端记录，这个回充故障案例里还没有你确认执行过的步骤。"
            "我之前给出的只是建议，不代表你已经做了。你可以先按“清洁充电触点”试一次，再告诉我结果。"
        )
        not_tried = _effective_not_tried(case)
        if not_tried:
            answer += "\n你明确说过没做过的步骤：" + "、".join(
                display_action_label(
                    str(item.get("action_id") or ""),
                    item.get("label"),
                    item.get("evidence"),
                )
                for item in not_tried
            ) + "（不会当成已试）。"
        return answer
    lines = ["根据服务端记录，你已经确认执行过："]
    for item in tried:
        outcome = {"failed": "结果仍未恢复", "success": "已恢复", "unknown": "结果未说明"}.get(
            item.get("outcome"), "结果未说明"
        )
        lines.append(
            "- {}：{}（第{}轮）".format(
                display_action_label(
                    str(item.get("action_id") or ""),
                    item.get("label"),
                    item.get("evidence"),
                ),
                outcome,
                item.get("source_turn"),
            )
        )
    not_tried = _effective_not_tried(case)
    if not_tried:
        lines.append(
            "另外，你明确说过没做过："
            + "、".join(
                display_action_label(
                    str(item.get("action_id") or ""),
                    item.get("label"),
                    item.get("evidence"),
                )
                for item in not_tried
            )
            + "（不会当成已试）。"
        )
    next_step = case.get("next_step") or {}
    if next_step:
        lines.append(f"下一步：{next_step.get('label')}。{next_step.get('detail')}")
    return "\n".join(lines)


def _coerce_interpretation(
    value: str, interpretation: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a validated interpretation plus its trace.

    The server passes a full ``{interpretation, trace}`` bundle; tests may pass
    a raw proposal.  Both are re-validated here against the actual user text.
    """
    if isinstance(interpretation, dict) and "interpretation" in interpretation:
        proposal = interpretation.get("interpretation")
        trace = copy.deepcopy(interpretation.get("trace") or {})
    else:
        proposal = interpretation
        trace = {"method": "provided", "usage_status": "unavailable", "fallback_used": False}
    if proposal is None:
        bundle = interpret_turn(value)
        return bundle["interpretation"], bundle["trace"]
    normalized, errors = normalize_interpretation(proposal, value)
    trace.setdefault("method", "provided")
    trace["validation_errors"] = list(trace.get("validation_errors") or []) + errors
    return normalized, trace


def apply_user_turn(
    state: dict[str, Any],
    text: str,
    message_id: str | None = None,
    *,
    interpretation: dict[str, Any] | None = None,
    now: float | None = None,
    route: str | None = None,
    route_reason: str | None = None,
) -> dict[str, Any]:
    """Apply one user message and add one shared decision snapshot.

    The reducer body never sees ``route``; routing is a cost/latency decision
    recorded for evidence only.  The snapshot is rebuilt after the reducer so
    prompt context and answer generation read exactly the same object.
    """
    result = _apply_user_turn_impl(
        state, text, message_id, interpretation=interpretation, now=now
    )
    result_state = result["state"]
    scope = result.get("scope")
    if scope not in {"duplicate", "empty"}:
        stamp = _now(now)
        result_state["last_turn_scope"] = scope
        result_state["last_decision"] = {
            "turn": result_state.get("turn"),
            "route": route or "reducer",
            "reason": route_reason or "no_route_override",
            "scope": scope,
            "interpretation_method": str(
                ((result_state.get("last_interpretation") or {}).get("trace") or {}).get("method")
                or "none"
            ),
            "at": stamp,
        }
        snapshot = decision_snapshot(result_state)
        result_state["decision_snapshot"] = snapshot
        result_state["last_decision"]["snapshot_id"] = snapshot.get("snapshot_id")
    result["state"] = result_state
    return result


def _apply_user_turn_impl(
    state: dict[str, Any],
    text: str,
    message_id: str | None = None,
    *,
    interpretation: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply one user message and return ``{state, changed, direct_answer}``.

    ``interpretation`` is a model/extractor proposal, not a fact.  The reducer
    validates it and remains authoritative for scope, provenance, negation and
    idempotency.  This function never reads assistant text.
    """
    stamp = _now(now)
    if (
        isinstance(state, dict)
        and message_id
        and state.get("last_user_message_id") == message_id
    ):
        # Duplicate replay must not run load_state/reconcile side effects; the
        # persisted bytes and question rows stay exactly as they were.
        return {
            "state": copy.deepcopy(state),
            "changed": False,
            "direct_answer": None,
            "scope": "duplicate",
        }
    state = load_state(state, stamp)
    state["last_user_message_id"] = message_id
    value = str(text or "").strip()
    if not value:
        state["turn"] = int(state.get("turn") or 0) + 1
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "empty"}
    state["turn"] = int(state.get("turn") or 0) + 1
    turn = int(state["turn"])
    proposal, interpretation_trace = _coerce_interpretation(value, interpretation)
    semantic_outcomes = _semantic_outcomes(proposal)
    state["last_interpretation"] = {
        "schema_version": INTERPRETATION_SCHEMA_VERSION,
        "turn": turn,
        "message_id": message_id,
        "text_excerpt": _excerpt(value),
        "interpretation": copy.deepcopy(proposal),
        "trace": copy.deepcopy(interpretation_trace),
    }
    topic = str(proposal.get("topic") or "unknown")
    recharge = is_recharge_related(value) or topic == "recharge_failure"
    safety_signal = (
        str((proposal.get("observable_symptom") or {}).get("safety") or "none") == "possible"
        or bool(_SAFETY_SYMPTOM.search(value))
    )
    failure_signal = (
        bool(_RECHARGE_FAILURE.search(value))
        or (topic == "recharge_failure" and outcome_is_failure(value))
        or (recharge and safety_signal)
    )
    semantic_failure = _semantic_recharge_fault(proposal, value, recharge)
    if semantic_failure and not failure_signal:
        failure_signal = True
    weak_signal = _reported_outcome(value) == "weak_positive"
    other_topics = _other_topics(value)
    if topic not in {"recharge_failure", "unknown", "other"} and topic not in other_topics:
        other_topics.append(topic)
    active = _active_case(state)
    dismissed = bool(_RECHARGE_TOPIC_DISMISSED.search(value))

    # A resolved case is never resurrected: a later recharge failure is a new
    # case, and its old rows remain in ``cases``.
    if active is not None and active.get("status") == "resolved":
        if failure_signal and recharge:
            active = None
        else:
            return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "other"}

    if dismissed and active is not None:
        _mark_open_task(state, active.get("case_id"), "deferred", turn)
        state["pending_confirmation"] = None
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "other"}

    # Explicit non-recharge topic with no recharge/follow-up signal: keep the
    # old task, answer the new topic first, and queue at most one light
    # confirmation for the old task.
    if active is not None and other_topics and not recharge and not failure_signal:
        _ensure_open_task(state, active, turn)
        # New topic has priority: defer old questions instead of re-asking them
        # every turn.  At most one light old-task reminder is allowed, once.
        _defer_case_questions(state, active, turn, "topic_switch:new_topic_priority")
        _queue_old_task_confirmation(state, active, turn)
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "other"}

    if active is None:
        if not failure_signal or not recharge:
            return {
                "state": _changed(state, stamp),
                "changed": True,
                "direct_answer": None,
                "scope": "recharge" if recharge else "other",
            }
        active = _new_case(value, message_id, turn, stamp, interpretation=proposal)
        active["activation_evidence"] = {
            "kind": "semantic_recharge_failure"
            if semantic_failure and not (
                _RECHARGE_FAILURE.search(value)
                or (topic == "recharge_failure" and outcome_is_failure(value))
            )
            else "explicit_failure_or_safety",
            "text": _excerpt(value),
            "semantic_topic": proposal.get("topic"),
            "semantic_closure": proposal.get("closure_signal"),
            "semantic_stage": proposal.get("interaction_stage"),
            "ambiguity_ids": [
                item.get("id")
                for item in proposal.get("ambiguity") or []
                if isinstance(item, dict)
            ],
            "source_turn": turn,
        }
        state["cases"].append(active)
        state["active_case_id"] = active["case_id"]
        _ensure_open_task(state, active, turn)

    if not (
        recharge
        or _FOLLOWUP.search(value)
        or failure_signal
        or weak_signal
        or _is_tried_question(value)
        or _CORRECTION.search(value)
        or _GENERALIZED_NEGATION.search(value)
    ):
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "other"}

    _add_source(active, "user", message_id, turn, value, stamp)
    _merge_work_context(
        active,
        proposal,
        text=value,
        message_id=message_id,
        turn=turn,
        now=stamp,
        method=str((interpretation_trace or {}).get("method") or "fallback"),
    )
    diagnostic.apply_turn(
        active,
        value,
        message_id,
        turn,
        stamp,
        proposal=proposal,
    )

    if _is_tried_question(value):
        direct = _direct_answer(state, active, value, message_id, turn, stamp)
        state["active_case_id"] = active["case_id"]
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": direct, "scope": "recharge"}

    model = _extract_model(value)
    if model and model != (active.get("model") or {}).get("value"):
        active["model"] = {
            "value": model,
            "source_message_id": message_id,
            "source_turn": turn,
            "updated_at": stamp,
        }

    explicit_ids = list(_action_from_text(value))
    proposal_executed_ids: list[str] = []
    for item in proposal.get("executed_actions") or []:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or "")
        quote = str(item.get("evidence_quote") or "")
        if (
            claim_rejection(action_id, quote, value, require_execution=True)
            is not None
        ):
            continue
        if _NEGATED_ACTION.search(quote) and not _CORRECTION.search(value):
            continue
        if action_id not in proposal_executed_ids:
            proposal_executed_ids.append(action_id)
        if action_id not in explicit_ids:
            explicit_ids.append(action_id)
    negated_ids = set(_negated_action_ids(value))
    for item in proposal.get("not_tried_actions") or []:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or "")
        quote = str(item.get("evidence_quote") or "")
        if claim_rejection(action_id, quote, value) is None:
            negated_ids.add(action_id)

    # A correction that cannot be tied to a clear action negation must not
    # silently keep the old fact as if the correction had been understood.
    # Record the uncertainty without inventing either a retraction or a new
    # tried fact; a later explicit turn can resolve it.
    if _CORRECTION.search(value) and not negated_ids and not explicit_ids:
        _append_fact_audit(
            active,
            kind="correction_without_retraction",
            action_id=None,
            message_id=message_id,
            turn=turn,
            text=value,
            reason="no_clear_action_negation_evidence",
            replaced_fact=None,
            now=stamp,
        )
        active["correction_unresolved"] = {
            "text": _excerpt(value),
            "message_id": message_id,
            "turn": turn,
            "reason": "no_clear_action_negation_evidence",
        }

    # A semantic proposal that says the same action was both executed and not
    # tried is internally contradictory.  Record the ambiguity instead of
    # forcing either fact.
    conflicting = set(proposal_executed_ids) & negated_ids
    if conflicting:
        _record_negation_ambiguity(
            active,
            status="conflicting_claims",
            turn=turn,
            text=value,
            message_id=message_id,
            candidates=sorted(conflicting),
            now=stamp,
            reason="same_turn_executed_and_not_tried",
        )
        for action_id in conflicting:
            negated_ids.discard(action_id)
            if action_id in explicit_ids:
                explicit_ids.remove(action_id)

    binding_source: str | None = None
    generalized = bool(_GENERALIZED_NEGATION.search(value)) or str(
        proposal.get("negation_scope") or "none"
    ) == "all_pending"
    if generalized and not negated_ids:
        candidates, source = _last_asked_candidates(active, turn)
        if not candidates:
            _record_negation_ambiguity(
                active,
                status="needs_referent",
                turn=turn,
                text=value,
                message_id=message_id,
                candidates=[],
                now=stamp,
                reason="generalized_negation_without_referent",
            )
            clarification = (
                "你说“都没试过”，但我这里没有上一轮明确问过的步骤可以对应。"
                "请告诉我具体是哪些步骤没做，我不会替你猜或否定其它已做过的记录。"
            )
            active["clarification"] = clarification
            active["stage"] = "need_info"
            active["next_step"] = {
                "action_id": "clarify",
                "label": "确认没试过的步骤",
                "detail": clarification,
                "kind": "clarify",
            }
            _sync_question_for_next_step(active, turn, stamp)
            return {
                "state": _changed(state, stamp),
                "changed": True,
                "direct_answer": clarification,
                "scope": "recharge",
            }
        if len(candidates) > MAX_GENERALIZED_NEGATION_ACTIONS:
            labels = "、".join(ACTION_BY_ID[item]["label"] for item in candidates)
            _record_negation_ambiguity(
                active,
                status="too_many",
                turn=turn,
                text=value,
                message_id=message_id,
                candidates=candidates,
                now=stamp,
                reason="generalized_negation_too_many_referents",
            )
            clarification = (
                f"你刚才提到的候选步骤有：{labels}。数量较多，请确认具体是哪些没试过；"
                "我先不改记录，避免把没提到的历史步骤一起否定。"
            )
            active["clarification"] = clarification
            active["stage"] = "need_info"
            active["next_step"] = {
                "action_id": "clarify",
                "label": "确认没试过的步骤",
                "detail": clarification,
                "kind": "clarify",
            }
            _sync_question_for_next_step(active, turn, stamp)
            return {
                "state": _changed(state, stamp),
                "changed": True,
                "direct_answer": clarification,
                "scope": "recharge",
            }
        success_ids = [
            str(row.get("action_id"))
            for row in _effective_tried(active)
            if row.get("action_id") in candidates and row.get("outcome") == "success"
        ]
        if success_ids:
            _record_negation_ambiguity(
                active,
                status="conflicting_success",
                turn=turn,
                text=value,
                message_id=message_id,
                candidates=success_ids,
                now=stamp,
                reason="generalized_negation_conflicts_with_success",
            )
            labels = "、".join(ACTION_BY_ID[item]["label"] for item in success_ids)
            clarification = (
                f"你之前明确说过「{labels}」已经成功过，现在又说都没试过；这两条记录冲突。"
                "请确认哪条为准，我先不改记录。"
            )
            active["clarification"] = clarification
            active["stage"] = "need_info"
            active["next_step"] = {
                "action_id": "clarify",
                "label": "确认冲突的事实",
                "detail": clarification,
                "kind": "clarify",
            }
            _sync_question_for_next_step(active, turn, stamp)
            return {
                "state": _changed(state, stamp),
                "changed": True,
                "direct_answer": clarification,
                "scope": "recharge",
            }
        negated_ids = set(candidates)
        binding_source = source
        _record_fact_binding(
            active,
            action_ids=candidates,
            source=source,
            turn=turn,
            text=value,
            message_id=message_id,
            now=stamp,
        )

    # An imperative suggestion ("清理充电座") is not evidence that the user did
    # it.  Require a first-person/past marker before creating a user fact.
    executed = bool(_EXECUTED.search(value))
    confirmed_ids = [
        action_id for action_id in explicit_ids if action_id not in negated_ids and executed
    ]
    negation_reason = (
        "user_correction_not_tried"
        if _CORRECTION.search(value)
        else (
            "generalized_negation_bound"
            if binding_source
            else "user_negation"
        )
    )
    for action_id in negated_ids:
        _add_not_tried(
            active,
            action_id,
            message_id,
            turn,
            value,
            stamp,
            reason=negation_reason,
            binding_source=binding_source,
        )

    outcome = _reported_outcome(value)
    success = outcome == "explicit_success"
    failure = outcome == "failure"
    weak = outcome == "weak_positive"
    partial = outcome == "partial_success"
    scope = diagnostic.derive(active)
    # A validated automatic-return observation can establish closure even when
    # the surface phrase ("自己找到充电座并开始充电") is not in the legacy
    # success regex.  The observation is still the user's own current-turn fact.
    if (
        not success
        and isinstance(scope, dict)
        and str(scope.get("task_goal_status")) == "works"
        and any(
            row.get("turn") == turn
            and row.get("capability") == scope.get("task_goal")
            and row.get("result") == "works"
            and str(row.get("fact_status") or "effective") == "effective"
            for row in active.get("observations") or []
        )
    ):
        success = True
        failure = False
        weak = False
        partial = False
    scope_limited_success = False
    # A semantic closure_signal=explicit_success is not allowed to bypass the
    # capability scope.  Manual placement/charging is a different capability
    # from automatic return; only an automatic-return success may close an
    # automatic-return case.
    if success:
        if not diagnostic.success_in_scope(scope, value):
            success = False
            partial = True
            scope_limited_success = True
    # "还是不行"/"不行" without any recharge/action follow-up is not enough.
    if failure and not (recharge or confirmed_ids or _FOLLOWUP.search(value)):
        failure = False

    # Persist user-executed facts before any clarification/early return.  The
    # later branch updates are idempotent and can only tighten the outcome.
    if confirmed_ids:
        for action_id in confirmed_ids:
            if success:
                pre_outcome = "success"
            elif failure:
                # Prefer the validated semantic outcome.  The legacy regex is
                # only a fallback; recomputing over the whole message could
                # match the question "算试过重新对准吗" and lose a real failed
                # outcome reported before it.
                semantic = semantic_outcomes.get(action_id)
                if semantic in {"success", "failed"}:
                    pre_outcome = semantic
                else:
                    local = _outcome_for_action(value, action_id)
                    if local in {"failure", "partial_success"}:
                        pre_outcome = "failed"
                    elif local == "explicit_success":
                        pre_outcome = "success"
                    else:
                        pre_outcome = "unknown"
            else:
                semantic = semantic_outcomes.get(action_id)
                pre_outcome = semantic if semantic in {"success", "failed"} else "unknown"
            _record_attempt(active, action_id, pre_outcome, message_id, turn, value, stamp)
            if pre_outcome in {"success", "failed"}:
                _mark_suggestion(
                    active,
                    action_id,
                    "confirmed_success" if pre_outcome == "success" else "confirmed_failed",
                    message_id,
                    turn,
                    stamp,
                )

    if success:
        if confirmed_ids:
            for action_id in confirmed_ids:
                _record_attempt(active, action_id, "success", message_id, turn, value, stamp)
                _mark_suggestion(active, action_id, "confirmed_success", message_id, turn, stamp)
        # No confirmed action in this turn: the case may be resolved, but the
        # latest assistant suggestion must NOT be promoted to a user-executed
        # fact.  Execution and effect are independent dimensions.
        _record_feedback(active, "explicit_success", value, message_id, turn, stamp)
        active["closure_evidence"] = {
            "level": "explicit_success",
            "text": _excerpt(value),
            "source_message_id": message_id,
            "source_turn": turn,
        }
        active["status"] = "resolved"
        active["stage"] = "resolved"
        active["resolved_at"] = stamp
        active["resolution"] = {"text": _excerpt(value), "source_message_id": message_id, "source_turn": turn}
        active["next_step"] = None
        active["clarification"] = None
        question_lifecycle.answer_question(
            active,
            "weak_closure_result",
            turn=turn,
            value="explicit_success",
            evidence=_evidence(value, message_id, turn),
            reason="explicit_user_success",
            now=stamp,
        )
        _cancel_pending_questions(active, turn, "case_resolved")
        _mark_open_task(state, active.get("case_id"), "resolved", turn)
        state["pending_confirmation"] = None
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "recharge"}

    # One binary dock clarification, asked only once and only when it changes
    # the next step.  User facts above are already persisted.
    ambiguity_direct = _apply_ambiguity(active, value, message_id, turn, stamp)
    if ambiguity_direct:
        if failure:
            _record_feedback(active, "failure", value, message_id, turn, stamp)
        return {
            "state": _changed(state, stamp),
            "changed": True,
            "direct_answer": ambiguity_direct,
            "scope": "recharge",
        }

    if failure:
        # An initial "回充失败" is the reported symptom, not feedback on an
        # action.  Only a later failure (or a failure tied to a step) is a
        # feedback event.
        is_initial_report = (
            turn == int(active.get("_source_turn") or 0)
            and not _effective_tried(active)
            and not active.get("suggested_actions")
        )
        if not is_initial_report:
            _record_feedback(active, "failure", value, message_id, turn, stamp)
        linked: dict[str, Any] | None = None
        if confirmed_ids:
            for action_id in confirmed_ids:
                semantic = semantic_outcomes.get(action_id)
                if semantic in {"success", "failed"}:
                    action_outcome = semantic
                else:
                    local = _outcome_for_action(value, action_id)
                    action_outcome = (
                        "failed" if local in {"failure", "partial_success"} else "unknown"
                    )
                _record_attempt(active, action_id, action_outcome, message_id, turn, value, stamp)
                if action_outcome == "failed":
                    _mark_suggestion(active, action_id, "confirmed_failed", message_id, turn, stamp)
            active["clarification"] = None
        else:
            pending = _pending_suggestions(active)
            clear_pending = [item for item in pending if item.get("action_id") not in negated_ids]
            if len(clear_pending) == 1:
                linked = clear_pending[0]
            elif not pending and negated_ids:
                # User explicitly said a step was not tried; do not mark it.
                linked = None
            elif len(clear_pending) > 1:
                labels = "、".join(item.get("label", "") for item in clear_pending)
                active["clarification"] = f"你说的“还是不行”是指哪一步之后？刚才建议了：{labels}。请告诉我你实际试了哪一个。"
                active["stage"] = "need_info"
                active["next_step"] = {
                    "action_id": "clarify",
                    "label": "确认已试步骤",
                    "detail": active["clarification"],
                    "kind": "clarify",
                }
                _sync_question_for_next_step(active, turn, stamp)
                return {"state": _changed(state, stamp), "changed": True, "direct_answer": active["clarification"], "scope": "recharge"}
            if linked is not None:
                _record_attempt(active, linked["action_id"], "failed", message_id, turn, value, stamp)
                _mark_suggestion(active, linked["action_id"], "confirmed_failed", message_id, turn, stamp)
                active["clarification"] = None
            elif (
                not confirmed_ids
                and not negated_ids
                and _AMBIGUOUS_FOLLOWUP.search(value)
                and _is_bare_ambiguous_feedback(value)
                and _has_prior_feedback_context(active, turn)
                and ((
                    _ambiguity_item(active, "dock_find_referent") or {}
                ).get("status") != "deferred")
            ):
                active["clarification"] = (
                    "你说的“还是不行”是指哪一步之后？请告诉我你刚才实际试了哪个操作"
                    "（例如清洁充电触点、检查充电座电源），我才能判断下一步。"
                )
                active["stage"] = "need_info"
                active["next_step"] = {
                    "action_id": "clarify",
                    "label": "确认已试步骤",
                    "detail": active["clarification"],
                    "kind": "clarify",
                }
                _sync_question_for_next_step(active, turn, stamp)
                return {"state": _changed(state, stamp), "changed": True, "direct_answer": active["clarification"], "scope": "recharge"}
        active["stage"] = "diagnose"
        active["status"] = "active"
        if isinstance(scope, dict) and str(scope.get("task_goal_status")) == "fails" and any(
            str(row.get("capability")) != str(scope.get("task_goal"))
            and str(row.get("result")) == "works"
            for row in scope.get("verified_capabilities") or []
        ):
            # Local capability works + task-goal failure is a partial success,
            # not a global resolution.  Keep both scopes visible.
            active["closure_evidence"] = {
                "level": "partial_success",
                "text": _excerpt(value),
                "source_message_id": message_id,
                "source_turn": turn,
                "scope_reason": "local_capability_works_but_task_goal_fails",
            }
        elif (active.get("closure_evidence") or {}).get("level") == "partial_success":
            # A later failure with no remaining local positive observation must
            # not keep a stale partial-success closure from an earlier turn.
            active["closure_evidence"] = None
        active["next_step"] = _diagnosis_next_step(active, value, interpretation=proposal, turn=turn)
        # A not-tried step pointed at by the message is the next useful action
        # only if the generic next step is still earlier in the catalogue.
        if negated_ids and not confirmed_ids:
            _negated_next_step(active, negated_ids)
        _sync_question_for_next_step(active, turn, stamp)
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "recharge"}

    if partial:
        _record_feedback(active, "partial_success", value, message_id, turn, stamp)
        active["closure_evidence"] = {
            "level": "partial_success",
            "text": _excerpt(value),
            "source_message_id": message_id,
            "source_turn": turn,
            "scope_reason": (
                "manual_observation_does_not_cover_automatic_return"
                if scope_limited_success
                else "local_effect_does_not_cover_global_case"
            ),
        }
        active["status"] = "active"
        active["stage"] = "diagnose"
        active["next_step"] = _diagnosis_next_step(active, value, interpretation=proposal, turn=turn)
        _sync_question_for_next_step(active, turn, stamp)
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "recharge"}

    if weak:
        _record_feedback(active, "weak_positive", value, message_id, turn, stamp)
        active["closure_evidence"] = {
            "level": "weak_positive",
            "text": _excerpt(value),
            "source_message_id": message_id,
            "source_turn": turn,
        }
        active["status"] = "active"
        active["stage"] = "closing"
        question_lifecycle.ensure_question(
            active,
            slot="weak_closure_result",
            source="weak_closure",
            question_text="你刚才说“好了”——现在能正常自动回充了吗？",
            turn=turn,
            evidence=_evidence(value, message_id, turn),
            now=stamp,
        )
        if active.get("weak_confirmation_turn") is None:
            active["weak_confirmation_turn"] = turn
            active["clarification"] = "你刚才说“好了”——现在能正常自动回充了吗？"
            active["next_step"] = {
                "action_id": "confirm",
                "label": "确认是否真正恢复",
                "detail": active["clarification"],
                "kind": "confirm",
            }
        else:
            active["next_step"] = {
                "action_id": "soft_close",
                "label": "礼貌收束",
                "detail": "可以礼貌结束对话，但不要宣称已经解决；用户回来时可继续。",
                "kind": "soft_close",
            }
        _sync_question_for_next_step(active, turn, stamp)
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "recharge"}

    # A plain statement of an attempted step without an outcome.
    if confirmed_ids:
        for action_id in confirmed_ids:
            _record_attempt(active, action_id, "unknown", message_id, turn, value, stamp)
        active["stage"] = "diagnose"
        active["next_step"] = _diagnosis_next_step(active, value, interpretation=proposal, turn=turn)
        if negated_ids and not confirmed_ids:
            _negated_next_step(active, negated_ids)
        _sync_question_for_next_step(active, turn, stamp)
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "recharge"}

    # A question about what was tried is answered from evidence in the caller.
    if _is_tried_question(value):
        direct = query_tried_answer(state)
        active["stage"] = "diagnose"
        return {"state": _changed(state, stamp), "changed": True, "direct_answer": direct, "scope": "recharge"}

    active["next_step"] = _diagnosis_next_step(active, value, interpretation=proposal, turn=turn)
    if negated_ids and not confirmed_ids:
        _negated_next_step(active, negated_ids)
    active["stage"] = "diagnose"
    active["status"] = "active"
    _sync_question_for_next_step(active, turn, stamp)
    return {"state": _changed(state, stamp), "changed": True, "direct_answer": None, "scope": "recharge"}


def apply_assistant_turn(
    state: dict[str, Any],
    answer: str,
    message_id: str | None = None,
    *,
    turn: int | None = None,
    retrieval: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Record assistant advice/evidence; never promote it to a user fact."""
    stamp = _now(now)
    state = load_state(state, stamp)
    case = _active_case(state)
    case_turn = int(turn if turn is not None else state.get("turn") or 0)
    pending = state.get("pending_confirmation") or {}
    if pending.get("case_id") and pending.get("created_turn") == case_turn:
        # This assistant answer belongs to the new topic, not to the old
        # recharge case.  Do not contaminate old tried/suggested/source rows.
        task = next(
            (item for item in state.get("open_tasks") or [] if item.get("case_id") == pending.get("case_id")),
            None,
        )
        observed = bool(
            answer
            and "回充" in str(answer)
            and re.search(r"解决|恢复|好(?:了)?吗|怎么样|是否", str(answer))
        )
        if task is not None:
            task["last_prompted_turn"] = case_turn
            task["confirmation_observed"] = observed
        state["pending_confirmation"] = None
        if retrieval:
            state["last_retrieval"] = copy.deepcopy(retrieval)
        return _changed(state, stamp)
    if case is None or case.get("status") == "resolved":
        if retrieval:
            state["last_retrieval"] = copy.deepcopy(retrieval)
        return _changed(state, stamp)
    if retrieval:
        case["last_retrieval"] = copy.deepcopy(retrieval)
        state["last_retrieval"] = copy.deepcopy(retrieval)
    if message_id:
        _add_source(case, "assistant", message_id, case_turn, answer, stamp)
    if answer:
        # Keep the next step as the single primary suggestion.  The assistant
        # answer may mention several actions; only the state's next step is
        # eligible for a later "还是不行" link.
        _set_suggestion(case, message_id, case_turn, stamp)
        asked_ids: list[str] = []
        next_step = case.get("next_step") or {}
        if next_step.get("kind") == "action" and next_step.get("action_id") in ACTION_BY_ID:
            asked_ids.append(str(next_step.get("action_id")))
        for row in case.get("questions") or []:
            slot = str(row.get("slot") or "")
            if (
                slot.startswith("step_outcome:")
                and row.get("status") in {"pending", "deferred"}
            ):
                action_id = slot.split(":", 1)[1]
                if action_id in ACTION_BY_ID and action_id not in asked_ids:
                    asked_ids.append(action_id)
        case["last_asked_actions"] = {
            "turn": case_turn,
            "action_ids": asked_ids,
            "source": "assistant_next_step_or_question",
        }
        if (case.get("next_step") or {}).get("kind") == "clarify":
            case["stage"] = "need_info"
        elif (case.get("next_step") or {}).get("kind") == "confirm":
            case["stage"] = "awaiting_result"
        elif case.get("stage") != "resolved":
            case["stage"] = "diagnose"
        audit = question_lifecycle.audit_assistant_answer(
            case, answer, turn=case_turn, now=stamp
        )
        state["last_question_audit"] = copy.deepcopy(audit)
        decision = state.get("last_decision")
        if isinstance(decision, dict) and decision.get("turn") == case_turn:
            decision["actual_question_audit"] = copy.deepcopy(audit)
    case["updated_at"] = stamp
    return _changed(state, stamp)


def _handoff_ref(turn: Any, message_id: Any) -> str:
    pieces: list[str] = []
    if turn is not None:
        pieces.append(f"用户第{turn}轮")
    if message_id:
        pieces.append(str(message_id))
    return "，".join(pieces) or "无轮次记录"


def _handoff_retrieval_lines(retrieval: Any) -> list[str]:
    """List document sources only when the retrieval record is genuinely bound."""
    if not isinstance(retrieval, dict) or not retrieval:
        return ["- 无检索记录；不列资料文件名。"]
    status = str(retrieval.get("status") or "unknown")
    results = retrieval.get("results") or []
    if status != "ok" or not results:
        return [f"- 无可用检索证据（status={status}）；不把未绑定文件名写成来源。"]
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        chunk = str(item.get("chunk_id") or "")
        if not source or (source, chunk) in seen:
            continue
        seen.add((source, chunk))
        snippet = re.sub(r"\s+", " ", str(item.get("snippet") or ""))[:100]
        lines.append(f"- {source}（chunk={chunk or '-'}）：{snippet}")
    return lines or ["- 无可用检索证据；不列资料文件名。"]


def _handoff_transcript_lines(state: dict[str, Any], messages: list[dict[str, Any]]) -> list[str]:
    """Transcript-grounded Q&A summary; never forges domain facts."""
    users = [item for item in messages if str(item.get("role")) == "user"]
    assistants = [item for item in messages if str(item.get("role")) == "assistant"]
    lines = ["【当前问题（用户原话）】"]
    if not users:
        lines.append("- 当前会话还没有用户消息；没有可整理的转人工摘要，也未生成故障状态。")
        return lines
    last_user = users[-1]
    lines.append(
        "- {}：{}".format(
            _handoff_ref(last_user.get("turn"), last_user.get("message_id")),
            _excerpt(str(last_user.get("content") or ""), 300),
        )
    )
    lines.append("【最近回答（原文摘录，仅说明对话内容，不是领域状态）】")
    if assistants:
        last_assistant = assistants[-1]
        lines.append(
            "- 助手第{}轮（{}）：{}".format(
                last_assistant.get("turn"),
                last_assistant.get("message_id") or "-",
                _excerpt(str(last_assistant.get("content") or ""), 300),
            )
        )
    else:
        lines.append("- 尚无回答。")
    lines.append("【结构化售后状态】")
    lines.append(
        "- 本次会话没有形成回充故障案例；不把这次知识问答当成设备故障，"
        "不生成已试步骤、结果、原因或结案结论。"
    )
    lines.append("【未知 / 需人工确认】")
    lines.append(
        "- 如需售后处理，请人工确认具体设备型号、可观察现象和用户实际做过的步骤；"
        "本摘要不推断这些事实。"
    )
    lines.append("【本次回答的来源（仅真实检索记录）】")
    lines.extend(_handoff_retrieval_lines(state.get("last_retrieval")))
    return lines


def _handoff_case_lines(case: dict[str, Any]) -> list[str]:
    issue = case.get("issue") or {}
    lines = [
        "【当前问题（用户原话）】",
        f"- 记录问题：{issue.get('text') or '未记录'}",
    ]
    if issue.get("reported_text"):
        lines.append(f"- 用户原话：{issue.get('reported_text')}")
    lines.append(
        "- 来源：{}".format(_handoff_ref(case.get("_source_turn"), case.get("_source_message_id")))
    )
    lines.append(
        "- 案例：{}；状态：{}；阶段：{}".format(
            case.get("case_id"), case.get("status"), case.get("stage")
        )
    )
    context = case.get("work_context") or {}
    if context:
        lines.append(
            "【工作语境】主题={}；对象={}；经验者={}；熟悉度={}；歧义={}".format(
                context.get("topic") or "unknown",
                "、".join(context.get("objects") or []) or "unknown",
                context.get("experiencer") or "unknown",
                context.get("familiarity") or "unknown",
                "、".join(
                    "{}={}".format(item.get("id"), item.get("status"))
                    for item in context.get("ambiguity") or []
                )
                or "无",
            )
        )
    model = (case.get("model") or {}).get("value")
    if model:
        lines.append(
            f"用户提供的型号：{model}（来源：用户第{(case.get('model') or {}).get('source_turn')}轮）"
        )
    else:
        lines.append("用户提供的型号：未提供（不要推断）")

    tried = _effective_tried(case)
    lines.append("【用户已执行的动作与结果（仅用户确认）】")
    if tried:
        for item in tried:
            outcome = {
                "failed": "失败/未恢复",
                "success": "成功/恢复",
                "unknown": "结果未说明",
            }.get(item.get("outcome"), "结果未说明")
            lines.append(
                "- {}：{}；用户原话“{}”；{}".format(
                    display_action_label(
                        str(item.get("action_id") or ""),
                        item.get("label"),
                        item.get("evidence"),
                    ),
                    outcome,
                    _excerpt(str(item.get("evidence") or ""), 120),
                    _handoff_ref(item.get("source_turn"), item.get("source_message_id")),
                )
            )
    else:
        lines.append("- 暂无用户确认执行的动作；助手建议不算已做。")

    not_tried = _effective_not_tried(case)
    lines.append("【用户明确没试过】")
    if not_tried:
        for item in not_tried:
            lines.append(
                "- {}（{}）".format(
                    display_action_label(
                        str(item.get("action_id") or ""),
                        item.get("label"),
                        item.get("evidence"),
                    ),
                    _handoff_ref(item.get("source_turn"), item.get("source_message_id")),
                )
            )
    else:
        lines.append("- 暂无用户明确否定的步骤。")

    observations = diagnostic.observations_summary(case)
    lines.append("【用户观察（按能力范围，不能跨范围外推）】")
    if observations:
        for item in observations:
            lines.append(
                "- {}：{}；用户原话“{}”；{}".format(
                    diagnostic.CAPABILITY_LABELS.get(
                        str(item.get("capability")), str(item.get("capability"))
                    ),
                    "成功" if item.get("result") == "works" else "失败" if item.get("result") == "fails" else "结果未说明",
                    _excerpt(str(item.get("quote") or ""), 120),
                    _handoff_ref(item.get("turn"), item.get("message_id")),
                )
            )
    else:
        lines.append("- 暂无用户直接观察。")

    lines.append("【助手建议（未确认执行，不是事实）】")
    suggestions = [
        item
        for item in case.get("suggested_actions") or []
        if isinstance(item, dict) and item.get("status") == "suggested"
    ]
    if suggestions:
        for item in suggestions[-3:]:
            lines.append(
                "- {}（助手第{}轮）".format(item.get("label"), item.get("source_turn"))
            )
    else:
        lines.append("- 暂无待确认的助手建议。")

    scope = diagnostic.derive(case) or {}
    lines.append("【未知项 / 下一步】")
    next_step = case.get("next_step") or {}
    if next_step:
        lines.append(f"- 下一步：{next_step.get('label')}（{next_step.get('detail')}）")
    unresolved = [
        diagnostic.CAPABILITY_LABELS.get(str(item), str(item))
        for item in scope.get("unverified_capabilities") or []
    ]
    if unresolved:
        lines.append("- 仍未验证：" + "、".join(unresolved))
    context_unknowns = [str(item) for item in context.get("unknowns") or [] if str(item)]
    if context_unknowns:
        lines.append("- 语境未知：" + "、".join(context_unknowns[:6]))
    for item in (scope.get("still_needed") or [])[:3]:
        lines.append(f"- 仍需观察：{item}")
    root_cause = scope.get("root_cause") or {}
    candidates = root_cause.get("candidates") or []
    if candidates:
        lines.append(
            "- 诊断假设（不是事实）："
            + "、".join(
                "{}（{}）".format(item.get("label"), item.get("status"))
                for item in candidates
            )
        )
    if not (next_step or unresolved or context_unknowns or candidates):
        lines.append("- 暂无更多可确认的未知项。")

    if case.get("status") == "resolved":
        resolution = case.get("resolution") or {}
        lines.append("【结案】")
        lines.append(
            "- 用户报告恢复（{}）；原因仍未确认，不代表所有潜在问题已排除。".format(
                _handoff_ref(
                    resolution.get("source_turn"),
                    resolution.get("source_message_id"),
                )
            )
        )

    lines.append("【来源（仅真实绑定记录）】")
    sources = case.get("sources") or []
    if sources:
        lines.append("对话记录：")
        for item in sources[-6:]:
            lines.append(
                "- 第{}轮 {}：{}".format(
                    item.get("turn"),
                    item.get("message_id") or "-",
                    _excerpt(str(item.get("excerpt") or ""), 120),
                )
            )
    else:
        lines.append("对话记录：暂无。")
    lines.append("知识文档（仅本轮检索成功时列出）：")
    lines.extend(_handoff_retrieval_lines(case.get("last_retrieval")))
    return lines


def handoff_summary(
    state: dict[str, Any],
    session_id: str = "",
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministic local handoff brief.  It is never sent externally.

    This is a read-only projection.  It uses only the persisted support state and
    the actual session transcript; it never mutates either and never calls a
    model.  A non-recharge session gets a transcript-grounded Q&A summary with an
    explicit uncertainty boundary instead of being treated as a fault case.
    """
    state = load_state(state)
    generated_at = _iso(_now())
    transcript = [
        item
        for item in (messages or [])
        if isinstance(item, dict) and item.get("content")
    ]
    case = _active_case(state)
    lines = [
        "【转人工摘要｜仅在本地整理，未自动发送】",
        f"会话：{session_id or '-'}",
    ]
    if case is None:
        mode = "transcript_only"
        lines.extend(_handoff_transcript_lines(state, transcript))
        sources = copy.deepcopy(transcript[-6:])
        retrieval = copy.deepcopy(state.get("last_retrieval"))
        case_id = None
        updated_at = state.get("updated_at")
    else:
        mode = "structured_case"
        lines.extend(_handoff_case_lines(case))
        sources = copy.deepcopy(case.get("sources") or [])
        retrieval = copy.deepcopy(case.get("last_retrieval"))
        case_id = case.get("case_id")
        updated_at = case.get("updated_at")
    lines.append("边界：本地通用资料不代表官方授权售后结论；未执行任何设备动作；未生成外部工单。")
    return {
        "schema_version": "support-handoff/v1",
        "summary": "\n".join(lines),
        "mode": mode,
        "case_id": case_id,
        "sources": sources,
        "retrieval": retrieval,
        "notice": "仅在本地整理，未发送给任何外部售后系统。",
        "generated_at": generated_at,
        "updated_at": updated_at,
    }

