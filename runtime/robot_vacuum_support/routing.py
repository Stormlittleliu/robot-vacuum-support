# -*- coding: utf-8 -*-
"""Cost-aware turn routing for the support slice.

Only three low-risk routes are deterministic: a replay of an already validated
interpretation with the same message and state version, a pure protocol/thanks
turn with no fact update, and an explicit schema-verifiable answer to the one
open slot.  Everything else stays on the semantic interpreter; keywords are not
used as a substitute for semantic understanding.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from typing import Any

from . import state as support
from .interpretation import INTERPRETATION_SCHEMA_VERSION, TRACE_SCHEMA_VERSION, fallback_interpretation

CACHE_SCHEMA_VERSION = "support-interpretation-cache/v1"
MAX_CACHE_ENTRIES = 8

_PROTOCOL_ONLY = re.compile(
    r"^(?:谢谢|多谢|感谢|辛苦了|好的|好|嗯嗯?|ok|收到|明白|知道了)[。！!,.，、\s]*$", re.I
)


def is_pure_protocol(text: str) -> bool:
    return bool(_PROTOCOL_ONLY.match(str(text or "").strip()))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def state_version(state: dict[str, Any]) -> str:
    """Stable identity of the facts a cached interpretation may depend on."""
    case = None
    active_id = state.get("active_case_id")
    for item in state.get("cases") or []:
        if item.get("case_id") == active_id:
            case = item
            break
    compact = {
        # Fact-only version: a replay after a failed model run may have advanced
        # the turn counter but not the user facts.  Turn numbers are therefore
        # deliberately excluded; the message text itself is part of the key.
        "active_case_id": active_id,
        "case_count": len(state.get("cases") or []),
        "last_turn_scope": state.get("last_turn_scope"),
        "case": None
        if case is None
        else {
            "status": case.get("status"),
            "tried": [
                [i.get("action_id"), i.get("outcome")]
                for i in case.get("tried_actions") or []
                if str(i.get("fact_status") or "effective") == "effective"
            ],
            "not_tried": [
                i.get("action_id")
                for i in case.get("not_tried_actions") or []
                if str(i.get("fact_status") or "effective") == "effective"
            ],
            "negation_ambiguity": [
                [i.get("status"), i.get("turn"), i.get("candidates")]
                for i in case.get("negation_ambiguity") or []
            ],
            "ambiguity": [
                [i.get("id"), i.get("status"), i.get("resolution")]
                for i in (case.get("work_context") or {}).get("ambiguity") or []
            ],
            "questions": [
                [i.get("slot"), i.get("status"), i.get("ask_count")]
                for i in case.get("questions") or []
            ],
            "closure": (case.get("closure_evidence") or {}).get("level"),
        },
    }
    return _digest(_canonical(compact))[:16]


def cache_key(text: str, state_version_value: str, model: str | None) -> str:
    return _digest(
        _canonical(
            {
                "text": str(text or "").strip(),
                "state_version": state_version_value,
                "model": model or "unknown",
            }
        )
    )[:20]


def lookup_interpretation(
    state: dict[str, Any], text: str, *, model: str | None = None
) -> dict[str, Any] | None:
    version = state_version(state)
    key = cache_key(text, version, model)
    for entry in state.get("interpretation_cache") or []:
        if entry.get("key") != key:
            continue
        if entry.get("state_version") != version:
            continue
        entry["reuse_count"] = int(entry.get("reuse_count") or 0) + 1
        entry["last_reused_at"] = time.time()
        original_trace = copy.deepcopy(entry.get("trace") or {})
        trace = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "method": "replay_cache",
            "model": original_trace.get("model"),
            "latency_ms": 0,
            "usage": None,
            "usage_status": "not_called",
            "attempts": 0,
            "retry_reason": None,
            "status": "ok",
            "fallback_used": False,
            "fallback_reason": None,
            "validation_errors": [],
            "cache_hit": True,
            "reused_from_method": original_trace.get("method"),
            "cache_key": key,
            "state_version": version,
            "reuse_count": entry.get("reuse_count"),
        }
        return {
            "interpretation": copy.deepcopy(entry.get("interpretation")),
            "trace": trace,
        }
    return None


def remember_interpretation(
    state: dict[str, Any],
    text: str,
    bundle: dict[str, Any],
    *,
    state_version_value: str,
    model: str | None = None,
) -> bool:
    """Persist only a validated semantic interpretation for same-state replay."""
    trace = (bundle or {}).get("trace") or {}
    if trace.get("method") not in {"llm_structured", "llm_structured_partial"}:
        return False
    if trace.get("fallback_used") or trace.get("validation_errors"):
        return False
    interpretation = (bundle or {}).get("interpretation")
    if not isinstance(interpretation, dict):
        return False
    key = cache_key(text, state_version_value, model)
    entries = state.setdefault("interpretation_cache", [])
    entries[:] = [item for item in entries if item.get("key") != key]
    entries.append(
        {
            "schema_version": CACHE_SCHEMA_VERSION,
            "key": key,
            "text_digest": _digest(str(text or "").strip()),
            "state_version": state_version_value,
            "model": model,
            "method": trace.get("method"),
            "usage": copy.deepcopy(trace.get("usage")),
            "interpretation": copy.deepcopy(interpretation),
            "trace": copy.deepcopy(trace),
            "created_at": time.time(),
            "reuse_count": 0,
        }
    )
    del entries[:-MAX_CACHE_ENTRIES]
    return True


def deterministic_bundle(
    text: str,
    *,
    method: str,
    slot: str | None = None,
    value: Any = None,
    reason: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    interpretation = fallback_interpretation(text)
    if slot == "dock_find_referent" and value in {"robot_cannot_return", "user_cannot_find_dock"}:
        candidates = ["robot_cannot_return", "user_cannot_find_dock"]
        interpretation["ambiguity"] = [
            {
                "id": "dock_find_referent",
                "candidates": candidates,
                "status": "resolved",
                "basis_quote": str(text or "")[:80],
                "resolution": value,
            }
        ]
        interpretation["experiencer"] = "robot" if value == "robot_cannot_return" else "user"
    return {
        "interpretation": interpretation,
        "trace": {
            "schema_version": TRACE_SCHEMA_VERSION,
            "method": method,
            "model": None,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "usage": None,
            "usage_status": "not_called",
            "attempts": 0,
            "retry_reason": None,
            "status": "ok",
            "fallback_used": False,
            "fallback_reason": None,
            "validation_errors": [],
            "route_reason": reason,
            "deterministic_slot": slot,
            "deterministic_value": value,
        },
    }


def decide(
    state: dict[str, Any], text: str, *, model: str | None = None
) -> dict[str, Any]:
    """Return ``{route, reason, bundle?}`` before any model call."""
    value = str(text or "").strip()
    if not value:
        return {"route": "empty", "reason": "empty_message"}
    if is_pure_protocol(value):
        return {
            "route": "protocol_ack",
            "reason": "pure_protocol_no_fact_update",
            "bundle": deterministic_bundle(
                value, method="deterministic_protocol_ack", reason="pure_protocol_no_fact_update"
            ),
        }
    slot_answer = support.classify_slot_answer(state, value)
    if slot_answer:
        return {
            "route": "deterministic_slot_answer",
            "reason": slot_answer.get("reason"),
            "slot": slot_answer.get("slot"),
            "value": slot_answer.get("value"),
            "bundle": deterministic_bundle(
                value,
                method="deterministic_slot_answer",
                slot=slot_answer.get("slot"),
                value=slot_answer.get("value"),
                reason=str(slot_answer.get("reason")),
            ),
        }
    cached = lookup_interpretation(state, value, model=model)
    if cached is not None:
        return {
            "route": "replay_cache",
            "reason": "same_message_and_state_version",
            "bundle": cached,
        }
    return {"route": "semantic", "reason": "ambiguous_new_information_or_correction"}


def interpretation_input(state: dict[str, Any]) -> dict[str, Any]:
    """Compressed interpreter input: needed facts + the one pending question."""
    snapshot = support.decision_snapshot(state)
    interpreter_input = snapshot.get("interpreter_input") or {}
    return {
        "topic": interpreter_input.get("topic"),
        "issue": interpreter_input.get("issue"),
        "needed_facts": interpreter_input.get("needed_facts"),
        "pending_question": interpreter_input.get("pending_question"),
        "snapshot_id": snapshot.get("snapshot_id"),
    }
