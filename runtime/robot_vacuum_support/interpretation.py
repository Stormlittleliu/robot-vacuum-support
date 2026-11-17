# -*- coding: utf-8 -*-
"""Typed turn interpretation for the robot-vacuum support flow.

The semantic layer proposes a small, auditable work-context object.  It is
**not** allowed to create facts on its own: ``state.py`` validates quotes,
scope, provenance and idempotency before anything is written.  The fallback is
deliberately conservative and exists so a missing/failed model never turns an
ambiguous sentence into a confident fact.

This module only depends on the standard library.  The optional LLM adapter is
imported lazily so tests and offline checks do not need credentials.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from .actions import ACTION_IDS, claim_rejection
from .diagnostic_scope import (
    normalize_proposal_observations,
    normalize_retractions,
)

INTERPRETATION_SCHEMA_VERSION = "turn-interpretation/v1"
TRACE_SCHEMA_VERSION = "turn-interpretation-trace/v1"

DOMAINS = ("robot_vacuum_after_sales", "other", "unknown")
TOPICS = (
    "recharge_failure",
    "cleaning",
    "maintenance",
    "navigation",
    "noise",
    "battery",
    "app",
    "purchase",
    "other",
    "unknown",
)
OBJECTS = (
    "robot",
    "charging_dock",
    "charging_contacts",
    "return_sensor",
    "water_tank",
    "filter",
    "dustbin",
    "brush",
    "battery",
    "app",
    "floor",
    "unknown",
)
EXPERIENCERS = ("robot", "user", "ambiguous", "unknown")
STAGES = (
    "first_report",
    "diagnosing",
    "tried",
    "awaiting_result",
    "need_info",
    "resolved",
    "closing",
    "topic_switch",
    "unknown",
)
FAMILIARITY = ("unknown", "novice", "experienced")
CLOSURE_SIGNALS = (
    "none",
    "explicit_success",
    "weak_positive",
    "partial_success",
    "failure",
    "silence",
)
#: Re-exported from the shared action catalogue; kept as a module name so
#: existing imports keep working without a second action enum.
__all__ = ["ACTION_IDS"]
NEGATION_SCOPES = ("none", "explicit", "all_pending", "unknown")
OUTCOMES = ("success", "failed", "unknown")
INFORMATION_SOURCES = ("user_message", "conversation_context")
SAFETY_STATES = ("none", "possible")

#: Ambiguity families the policy knows how to resolve with one cheap question.
AMBIGUITY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "dock_find_referent": ("robot_cannot_return", "user_cannot_find_dock"),
}

_RECHARGE_WORDS = (
    "回充",
    "充电",
    "充电座",
    "充电桩",
    "触点",
    "电量",
    "充不上",
    "充不进",
    "不充电",
    "回不去",
    "回不了",
)
_USER_FIND_DOCK = re.compile(
    r"(?:我|自己).{0,12}(?:找不到|不知道.{0,6}放哪|搬(?:完)?家|挪走|丢了|忘了放哪)|"
    r"找不到(?:充电座|基座).{0,8}(?:放哪|位置)|"
    r"(?:充电座|基座).{0,8}(?:被|让).{0,4}(?:挪|搬|移)"
)
_ROBOT_RETURN = re.compile(
    r"(?:机器人|机器|它).{0,14}(?:回不去|回不了|回不到|找不到(?:充电座|基座|座)|对不上|打转|转圈)|"
    r"(?:在|到).{0,8}(?:充电座|基座|座).{0,10}(?:打转|对不上|转圈|找不到)|"
    r"回不去(?:了)?|回不了(?:充|去)?"
)
_FIND_DOCK = re.compile(r"找不到.{0,4}(?:充电座|基座|座)")
_SAFETY = re.compile(r"冒烟|焦味|烧焦|发烫|过热|漏电|触电|起火|着火")
_NOVICE = re.compile(r"不懂|小白|一步步|不会(?:排查|弄|操作)|没排查过|教教我|手把手")
_EXPERIENCED = re.compile(r"我很熟|我熟悉|经常(?:自己)?(?:修|拆|清理)|熟练|自己修过")
_WANTS_LIST = re.compile(r"完整.{0,4}(?:清单|步骤)|所有.{0,4}步骤|都告诉我|全部步骤")
_TRIED_CUE = re.compile(r"我已经|我试过|试过|做了|清理过|擦过|重启过|检查过|还是不行|仍然不行")
_EXPLICIT_SUCCESS = re.compile(
    r"充(?:上|进)(?:了|电)|能(?:自己)?回充|回充正常了?|自己回去了|已经能.{0,6}(?:回充|充电)|"
    r"恢复正常|恢复(?:了|正常)|修好了|已经解决"
)
_WEAK_POSITIVE = re.compile(r"好了|正常了|没事了|可以了|谢谢|辛苦了|^行$|^ok$", re.I)
_PARTIAL = re.compile(
    r"手动.{0,8}(?:能|可以)|放(?:回|上).{0,8}(?:能|可以)充|"
    r"能充.{0,8}自动.{0,6}(?:不行|不能)|自动.{0,6}(?:还是)?不行"
)
_FAILURE = re.compile(r"失败|不行|不回充|回不去|回不了|充不上|充不进|不能充电|无法充电|没反应|没用")
_WEAK_CLAUSE_NEGATION = re.compile(r"没|没有|未|不能|无法|不是")

_OBJECT_CUES = (
    ("charging_dock", re.compile(r"充电座|充电桩|基座")),
    ("charging_contacts", re.compile(r"充电触点|触点|金属片")),
    ("return_sensor", re.compile(r"回充传感器|传感器|红外")),
    ("water_tank", re.compile(r"水箱")),
    ("filter", re.compile(r"滤网|过滤网")),
    ("dustbin", re.compile(r"尘盒|集尘")),
    ("brush", re.compile(r"滚刷|边刷")),
    ("battery", re.compile(r"电池|电量|续航")),
    ("app", re.compile(r"APP|app|wifi|WI-FI|联网")),
    ("robot", re.compile(r"机器人|机器|扫地机|扫拖")),
    ("floor", re.compile(r"地板|地面")),
)


def _topic(text: str) -> str:
    value = str(text or "")
    if any(word in value for word in _RECHARGE_WORDS):
        return "recharge_failure"
    if re.search(r"扫不干净|清扫不干净|拖不干净|吸力|打扫不干净", value):
        return "cleaning"
    if re.search(r"滤网|滚刷|边刷|水箱|保养|维护|清洗", value):
        return "maintenance"
    if re.search(r"迷路|建图|地图|乱跑|定位|找不到路", value):
        return "navigation"
    if re.search(r"噪音|很响|异响|声音大", value):
        return "noise"
    if re.search(r"续航|掉电|耗电快|电池不耐用", value):
        return "battery"
    if re.search(r"wifi|WI-FI|联网|配网|连接不上|APP|app", value):
        return "app"
    if re.search(r"选购|推荐|买哪|价格|多少钱|型号推荐", value):
        return "purchase"
    return "other"


def _closure(text: str) -> str:
    value = str(text or "")
    if _PARTIAL.search(value):
        return "partial_success"
    explicit = _EXPLICIT_SUCCESS.search(value)
    if explicit:
        clause = value[max(0, explicit.start() - 8) : explicit.end() + 4]
        if not _WEAK_CLAUSE_NEGATION.search(clause):
            return "explicit_success"
    if _FAILURE.search(value):
        return "failure"
    if _WEAK_POSITIVE.search(value):
        return "weak_positive"
    return "none"


def fallback_interpretation(text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Conservative deterministic proposal used when no model is available.

    It does not invent executed actions or outcomes.  The reducer still applies
    its existing scoped phrase adapters, so legacy sessions keep working.
    """
    value = str(text or "")
    topic = _topic(value)
    objects = [name for name, pattern in _OBJECT_CUES if pattern.search(value)]
    closure = _closure(value)
    user_find = bool(_USER_FIND_DOCK.search(value))
    robot_return = bool(_ROBOT_RETURN.search(value))
    ambiguity: list[dict[str, Any]] = []
    if _FIND_DOCK.search(value) and not user_find and not robot_return:
        ambiguity.append(
            {
                "id": "dock_find_referent",
                "candidates": list(AMBIGUITY_CANDIDATES["dock_find_referent"]),
                "status": "open",
                "basis_quote": "找不到充电座",
            }
        )
    if user_find:
        experiencer = "user"
    elif robot_return:
        experiencer = "robot"
    elif ambiguity:
        experiencer = "ambiguous"
    else:
        experiencer = "unknown"

    stage = "first_report"
    if closure in {"explicit_success", "weak_positive", "partial_success"}:
        stage = "resolved" if closure == "explicit_success" else "closing"
    elif _TRIED_CUE.search(value):
        stage = "tried"
    elif ambiguity:
        stage = "need_info"

    familiarity = "unknown"
    if _NOVICE.search(value):
        familiarity = "novice"
    elif _EXPERIENCED.search(value):
        familiarity = "experienced"

    unknowns: list[str] = []
    if not _TRIED_CUE.search(value) and topic == "recharge_failure":
        unknowns.append("用户是否已尝试过相关步骤")
    if ambiguity:
        unknowns.append("“找不到充电座”的经验者")

    return {
        "schema_version": INTERPRETATION_SCHEMA_VERSION,
        "domain": "robot_vacuum_after_sales" if topic != "other" else "other",
        "topic": topic,
        "objects": objects or ["unknown"],
        "experiencer": experiencer,
        "observable_symptom": {
            "text": value[:160],
            "observed_by_user": bool(value),
            "safety": "possible" if _SAFETY.search(value) else "none",
        },
        "ambiguity": ambiguity,
        "executed_actions": [],
        "not_tried_actions": [],
        "capability_observations": [],
        "observation_retractions": [],
        "negation_scope": "none",
        "negation_quote": "",
        "assistant_suggestions": [],
        "interaction_stage": stage,
        "familiarity": familiarity,
        "closure_signal": closure,
        "information_sources": ["user_message"],
        "unknowns": unknowns,
        "topic_switch": False,
        "wants_full_checklist": bool(_WANTS_LIST.search(value)),
    }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _text(value: Any, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _enum(value: Any, allowed: tuple[str, ...], default: str, field: str, errors: list[str]) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in allowed:
        return candidate
    if candidate:
        errors.append(f"{field}:{candidate!r} not allowed")
    return default


def normalize_interpretation(
    payload: Any,
    user_text: str,
    *,
    base: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate an interpretation proposal against the current user message.

    Invalid action claims are dropped rather than repaired.  A model quote must
    be an exact substring of the user's message; this is the provenance check
    that stops a plausible but invented "I already did X" from becoming state.
    """
    errors: list[str] = []
    fallback = dict(base or fallback_interpretation(user_text))
    if not isinstance(payload, dict):
        return fallback, ["payload is not an object"]

    result = dict(fallback)
    result["schema_version"] = INTERPRETATION_SCHEMA_VERSION
    result["domain"] = _enum(
        payload.get("domain", fallback["domain"]), DOMAINS, fallback["domain"], "domain", errors
    )
    result["topic"] = _enum(
        payload.get("topic", fallback["topic"]), TOPICS, fallback["topic"], "topic", errors
    )
    result["experiencer"] = _enum(
        payload.get("experiencer", fallback["experiencer"]),
        EXPERIENCERS,
        fallback["experiencer"],
        "experiencer",
        errors,
    )
    result["interaction_stage"] = _enum(
        payload.get("interaction_stage", fallback["interaction_stage"]),
        STAGES,
        fallback["interaction_stage"],
        "interaction_stage",
        errors,
    )
    result["familiarity"] = _enum(
        payload.get("familiarity", fallback["familiarity"]),
        FAMILIARITY,
        fallback["familiarity"],
        "familiarity",
        errors,
    )
    result["closure_signal"] = _enum(
        payload.get("closure_signal", fallback["closure_signal"]),
        CLOSURE_SIGNALS,
        fallback["closure_signal"],
        "closure_signal",
        errors,
    )

    objects: list[str] = []
    for item in _as_list(payload.get("objects", fallback["objects"])):
        name = str(item or "").strip().lower()
        if name in OBJECTS and name not in objects:
            objects.append(name)
        elif name:
            errors.append(f"objects:{name!r} not allowed")
    if not objects:
        errors.append("objects empty; fallback used")
        objects = list(fallback["objects"])
    result["objects"] = objects

    symptom = payload.get("observable_symptom", fallback["observable_symptom"])
    if isinstance(symptom, dict):
        symptom_text = _text(symptom.get("text") or symptom.get("summary") or "", 200)
        safety = _enum(
            symptom.get("safety", "none"),
            SAFETY_STATES,
            "none",
            "observable_symptom.safety",
            errors,
        )
    else:
        symptom_text = _text(symptom, 200)
        safety = "possible" if _SAFETY.search(symptom_text) else "none"
    result["observable_symptom"] = {
        "text": symptom_text or _text(fallback["observable_symptom"].get("text"), 200),
        "observed_by_user": True,
        "safety": safety,
    }

    ambiguity: list[dict[str, Any]] = []
    for item in _as_list(payload.get("ambiguity", [])):
        if not isinstance(item, dict):
            errors.append("ambiguity entry is not an object")
            continue
        amb_id = str(item.get("id") or "").strip()
        if amb_id not in AMBIGUITY_CANDIDATES:
            errors.append(f"ambiguity id {amb_id!r} not allowed")
            continue
        candidates = [
            str(candidate)
            for candidate in _as_list(item.get("candidates"))
            if str(candidate) in AMBIGUITY_CANDIDATES[amb_id]
        ]
        if len(candidates) < 2:
            errors.append(f"ambiguity {amb_id!r} needs at least two known candidates")
            continue
        status = str(item.get("status") or "open").strip().lower()
        if status not in {"open", "resolved", "deferred"}:
            errors.append(f"ambiguity {amb_id!r} invalid status")
            status = "open"
        basis = _text(item.get("basis_quote"), 80)
        if basis and basis not in str(user_text or ""):
            errors.append(f"ambiguity {amb_id!r} basis quote not in user message")
            basis = ""
        resolution = str(item.get("resolution") or "").strip()
        if resolution and resolution not in candidates:
            errors.append(f"ambiguity {amb_id!r} resolution not a candidate")
            resolution = ""
        ambiguity.append(
            {
                "id": amb_id,
                "candidates": candidates,
                "status": status,
                "basis_quote": basis,
                "resolution": resolution or None,
            }
        )
    result["ambiguity"] = ambiguity

    executed: list[dict[str, Any]] = []
    for item in _as_list(payload.get("executed_actions", [])):
        if not isinstance(item, dict):
            errors.append("executed_actions entry is not an object")
            continue
        action_id = str(item.get("action_id") or "").strip()
        quote = _text(item.get("evidence_quote"), 120)
        rejection = claim_rejection(action_id, quote, user_text, require_execution=True)
        if rejection:
            if rejection == "quote_not_in_current_message":
                errors.append(f"executed action {action_id!r} quote not in user message")
            else:
                errors.append(f"executed action {action_id!r} rejected: {rejection}")
            continue
        source = str(item.get("source") or "user_message").strip().lower()
        if source not in INFORMATION_SOURCES or source == "assistant_suggestion":
            errors.append(f"executed action {action_id!r} has non-user source")
            continue
        outcome = str(item.get("outcome") or "unknown").strip().lower()
        if outcome not in OUTCOMES:
            outcome = "unknown"
            errors.append(f"executed action {action_id!r} invalid outcome")
        executed.append(
            {"action_id": action_id, "outcome": outcome, "evidence_quote": quote}
        )
    result["executed_actions"] = executed
    not_tried: list[dict[str, Any]] = []
    for item in _as_list(payload.get("not_tried_actions", [])):
        if not isinstance(item, dict):
            errors.append("not_tried_actions entry is not an object")
            continue
        action_id = str(item.get("action_id") or "").strip()
        quote = _text(item.get("evidence_quote"), 120)
        rejection = claim_rejection(action_id, quote, user_text)
        if rejection:
            if rejection == "quote_not_in_current_message":
                errors.append(f"not-tried action {action_id!r} quote not in user message")
            else:
                errors.append(f"not-tried action {action_id!r} rejected: {rejection}")
            continue
        not_tried.append({"action_id": action_id, "evidence_quote": quote})
    result["not_tried_actions"] = not_tried

    observations, observation_errors = normalize_proposal_observations(
        payload.get("capability_observations"), user_text
    )
    retractions, retraction_errors = normalize_retractions(
        payload.get("observation_retractions"), user_text
    )
    result["capability_observations"] = observations
    result["observation_retractions"] = retractions
    errors.extend(observation_errors)
    errors.extend(retraction_errors)

    negation_scope = str(payload.get("negation_scope", "none") or "none").strip().lower()
    if negation_scope not in NEGATION_SCOPES:
        errors.append(f"negation_scope:{negation_scope!r} not allowed")
        negation_scope = "none"
    negation_quote = _text(payload.get("negation_quote"), 120)
    if negation_scope in {"explicit", "all_pending"}:
        if not negation_quote or negation_quote not in str(user_text or ""):
            errors.append(f"negation_scope {negation_scope!r} quote not in user message")
            negation_scope = "none"
            negation_quote = ""
    result["negation_scope"] = negation_scope
    result["negation_quote"] = negation_quote

    # A user turn can never establish an assistant suggestion; that is written
    # by ``apply_assistant_turn`` from the assistant's own message.
    result["assistant_suggestions"] = []

    sources: list[str] = []
    for item in _as_list(payload.get("information_sources", ["user_message"])):
        source = str(item or "").strip().lower()
        if source in INFORMATION_SOURCES and source not in sources:
            sources.append(source)
        elif source:
            errors.append(f"information_sources:{source!r} not allowed")
    if not sources:
        sources = ["user_message"]
    result["information_sources"] = sources

    result["unknowns"] = [_text(item, 100) for item in _as_list(payload.get("unknowns")) if _text(item, 100)]
    result["topic_switch"] = bool(payload.get("topic_switch", fallback.get("topic_switch", False)))
    result["wants_full_checklist"] = bool(
        payload.get("wants_full_checklist", fallback.get("wants_full_checklist", False))
    )
    return result, errors


def _parse_json_text(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("interpretation JSON is not an object")
    return payload


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def _response_usage(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict):
        token_usage = metadata.get("token_usage") or metadata.get("usage")
        if isinstance(token_usage, dict) and token_usage:
            return {
                "input_tokens": token_usage.get("prompt_tokens", token_usage.get("input_tokens")),
                "output_tokens": token_usage.get("completion_tokens", token_usage.get("output_tokens")),
                "total_tokens": token_usage.get("total_tokens"),
            }
    return None


_INTERPRETER_SYSTEM = """你是售后对话的“语境抽取器”，只输出 JSON，不做回答。
规则：
1. 只记录用户消息明确表达的内容；不要推测未说出的操作、结果或熟悉度。
2. 用户说“我做了/我试过/已经…”才算 executed；助手建议不是用户执行。
3. evidence_quote 必须是用户消息中的原文子串，否则该条不要输出。
4. “找不到充电座/回不去”若不能确定是机器人回不到座，还是用户找不到实体座，
   在 ambiguity 输出 dock_find_referent，两个候选 robot_cannot_return / user_cannot_find_dock。
5. 不确定的字段用 unknown/[]；不要为了填满而编造。
6. closure_signal 只有明确“能回充/充上电/恢复”才是 explicit_success；“好了/谢谢”是 weak_positive。
7. 动作身份：换“新的”充电座是 replace_dock；挪动原来的充电座是 move_dock；
   把机器人放回/对准充电座是 reseat_dock。不要用“换座”猜 replace/move/reseat。
8. 用户说“都没试过/都没做/一个都没试”且没有明确动作对象时，negation_scope=all_pending，
   evidence_quote 用该原句；有明确动作对象时用 explicit + not_tried_actions。reducer 才负责绑定上下文。
9. “服务重启/后台重启/会话重启”不是 reset_robot；只有重启机器人/机器才算。
10. 能力观察只记录用户明确说的结果，capability 只能是 manual_charging（手动放回/放到座后充电）、
    automatic_return（机器人自动/自己回充）、dock_power（充电座供电/指示灯）；result 只能是 works/fails/unknown。
    evidence_quote 用当前消息中对应能力所在子句。手动放回能充不能写成 automatic_return=works；
    自动回充的原文也不能写成 manual_charging=works。
11. 条件句/疑问（如果、是不是、能不能…）、引用别人/资料的说法不是用户观察，不要写成 works；
    “擦了触点/清了障”是动作，不是充电能力结果。用户撤回某观察时用 observation_retractions。
返回字段：
{schema}
"""

_INTERPRETER_SCHEMA = {
    "domain": "robot_vacuum_after_sales|other|unknown",
    "topic": "recharge_failure|cleaning|maintenance|navigation|noise|battery|app|purchase|other|unknown",
    "objects": ["robot|charging_dock|charging_contacts|return_sensor|water_tank|filter|dustbin|brush|battery|app|floor|unknown"],
    "experiencer": "robot|user|ambiguous|unknown",
    "observable_symptom": {"text": "用户可观察现象", "safety": "none|possible"},
    "ambiguity": [
        {
            "id": "dock_find_referent",
            "candidates": ["robot_cannot_return", "user_cannot_find_dock"],
            "status": "open|resolved|deferred",
            "basis_quote": "用户原文子串",
            "resolution": "候选之一或 null",
        }
    ],
    "executed_actions": [
        {
            "action_id": "|".join(ACTION_IDS),
            "outcome": "success|failed|unknown",
            "evidence_quote": "用户原文子串",
            "source": "user_message",
        }
    ],
    "not_tried_actions": [
        {"action_id": "|".join(ACTION_IDS), "evidence_quote": "用户原文子串"}
    ],
    "capability_observations": [
        {
            "capability": "manual_charging|automatic_return|dock_power",
            "result": "works|fails|unknown",
            "evidence_quote": "当前用户原文中对应能力子句",
            "source": "user_message",
        }
    ],
    "observation_retractions": [
        {"capability": "manual_charging|automatic_return|dock_power", "evidence_quote": "用户原文子串"}
    ],
    "negation_scope": "none|explicit|all_pending|unknown",
    "negation_quote": "用户原文子串或空",
    "interaction_stage": "first_report|diagnosing|tried|awaiting_result|need_info|resolved|closing|topic_switch|unknown",
    "familiarity": "unknown|novice|experienced",
    "closure_signal": "none|explicit_success|weak_positive|partial_success|failure|silence",
    "information_sources": ["user_message"],
    "unknowns": ["仍未确认的信息"],
    "topic_switch": False,
    "wants_full_checklist": False,
}


def _sanitize_no_proxy() -> None:
    """Remove bracketed IPv6 NO_PROXY entries before httpx builds a client.

    The host may export ``NO_PROXY=...,[::1]``; httpx/openai then raises
    ``InvalidURL: Invalid port: ':1]'`` before any network call.  The worker
    already sanitises its own child env; the in-server interpreter call must do
    the same or it silently falls back and never records a real model trace.
    """
    for name in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(name)
        if value is None:
            continue
        kept = [
            part.strip()
            for part in value.split(",")
            if part.strip() and not (part.strip().startswith("[") and part.strip().endswith("]"))
        ]
        os.environ[name] = ",".join(kept)


class LlmTurnInterpreter:
    """Small structured-output adapter around the existing chat model.

    It is intentionally one call (with at most one transport retry).  Usage and
    latency are returned to the caller for an auditable trace; absence of usage
    is reported as ``unavailable`` rather than estimated.
    """

    def __init__(self, model: Any = None):
        self._model = model

    def _default_model(self) -> Any:
        from core.llm.factory import chat_model  # lazy: needs instance env

        return chat_model

    def interpret(self, text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        _sanitize_no_proxy()
        model = self._model or self._default_model()
        known: dict[str, Any] = {}
        if isinstance(context, dict):
            if "needed_facts" in context or "pending_question" in context:
                # Compressed input from the decision snapshot: only the facts
                # needed to disambiguate this turn and the one pending question.
                known = {
                    "topic": context.get("topic"),
                    "issue": context.get("issue"),
                    "needed_facts": context.get("needed_facts"),
                    "pending_question": context.get("pending_question"),
                    "snapshot_id": context.get("snapshot_id"),
                }
            else:
                active = context.get("active_case") or {}
                known = {
                    "topic": active.get("topic"),
                    "issue": active.get("issue"),
                    "tried": [
                        item.get("action_id")
                        for item in active.get("tried_actions") or []
                        if isinstance(item, dict)
                    ],
                }
        messages = [
            SystemMessage(
                content=_INTERPRETER_SYSTEM.format(
                    schema=json.dumps(_INTERPRETER_SCHEMA, ensure_ascii=False)
                )
            ),
            HumanMessage(
                content="本会话已知（只作消歧，不得写成新事实）："
                + json.dumps(known, ensure_ascii=False)
                + "\n用户本轮消息："
                + str(text)
            ),
        ]
        attempts = 0
        retry_reason = None
        try:
            attempts += 1
            try:
                response = model.bind(response_format={"type": "json_object"}).invoke(messages)
            except Exception as exc:  # transport/provider lacks JSON mode; one retry
                retry_reason = f"{type(exc).__name__}: {str(exc)[:120]}"
                attempts += 1
                response = model.invoke(messages)
            payload = _parse_json_text(_response_text(response))
            return {
                "payload": payload,
                "usage": _response_usage(response),
                "model": str(
                    getattr(model, "model_name", None)
                    or getattr(model, "model", None)
                    or type(model).__name__
                ),
                "attempts": attempts,
                "retry_reason": retry_reason,
            }
        except Exception as exc:  # noqa: BLE001 - caller records fallback
            from core.llm.errors import as_turn_failure

            raise as_turn_failure(exc) from exc


def _trace(
    *,
    method: str,
    latency_ms: int,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
    attempts: int = 1,
    retry_reason: str | None = None,
    errors: list[str] | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "method": method,
        "model": model,
        "latency_ms": latency_ms,
        "usage": usage,
        "usage_status": "reported" if usage else "unavailable",
        "attempts": attempts,
        "retry_reason": retry_reason,
        "status": "ok" if not fallback_reason else "fallback",
        "fallback_used": bool(fallback_reason),
        "fallback_reason": fallback_reason,
        "validation_errors": list(errors or []),
    }


def interpret_turn(
    text: str,
    *,
    interpreter: Callable[[str, dict[str, Any] | None], dict[str, Any]] | Any | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ``{interpretation, trace}`` for one user turn.

    With no interpreter, or on any model/validation failure, a conservative
    deterministic proposal is returned.  A model proposal is accepted only after
    schema/provenance validation; invalid action claims are dropped and logged.
    """
    started = time.perf_counter()
    fallback = fallback_interpretation(text, context)
    if interpreter is None:
        return {
            "interpretation": fallback,
            "trace": _trace(
                method="fallback",
                latency_ms=int((time.perf_counter() - started) * 1000),
                fallback_reason="interpreter_not_configured",
            ),
        }
    try:
        call = getattr(interpreter, "interpret", interpreter)
        raw = call(text, context)
        payload = raw.get("payload") if isinstance(raw, dict) and "payload" in raw else raw
        normalized, errors = normalize_interpretation(payload, text, base=fallback)
        method = "llm_structured" if not errors else "llm_structured_partial"
        return {
            "interpretation": normalized,
            "trace": _trace(
                method=method,
                latency_ms=int((time.perf_counter() - started) * 1000),
                model=(raw or {}).get("model") if isinstance(raw, dict) else None,
                usage=(raw or {}).get("usage") if isinstance(raw, dict) else None,
                attempts=int((raw or {}).get("attempts", 1)) if isinstance(raw, dict) else 1,
                retry_reason=(raw or {}).get("retry_reason") if isinstance(raw, dict) else None,
                errors=errors,
            ),
        }
    except Exception as exc:  # noqa: BLE001 - fallback is the product contract
        from core.llm.errors import safe_exception_summary

        return {
            "interpretation": fallback,
            "trace": _trace(
                method="fallback",
                latency_ms=int((time.perf_counter() - started) * 1000),
                fallback_reason=safe_exception_summary(exc),
            ),
        }
