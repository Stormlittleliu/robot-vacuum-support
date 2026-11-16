# -*- coding: utf-8 -*-
"""从 trace 的 context_snapshot 构建按 context_id 分账的上下文视图。

公共只读投影：Editor 与 Console 共用这一份纯函数；``modules.editor.context_view``
只是兼容 shim。JSON 形状与观测语义以本模块为准。

父 / 子 Agent 的 snapshot 不再混成一条 ``previous`` 序列：每个 context
独立维护上一轮可见 segment，因此子 Agent 的差异不会被误报成父上下文压缩。
``removed`` 只表示"这一轮不再可见"；``compacted`` 只有在 trace 中存在明确的
``context_compacted`` 事件时才为 true，并同时给出该事件本身。

Canonical schema（GUI 只读这些字段，旧字段仅作 fallback）：

top-level
    available: bool
    turns / segments / contexts / tool_calls / middleware_decisions: list
    middleware_observation: dict
    compaction_count / compaction_event_count: int

turn
    sequence: int          # trace 事件流中的真实全局位置（跨 context）
    context_step: int      # 仅该 context 分支内的第几次 snapshot，用于显示 tN
    step: int              # == context_step，兼容旧 GUI
    context_id / parent_context_id / context_label / origin_tool
    run_id / parent_run_id / node / model / tokens_est / messages
    read / appended / removed / moved_out
    compacted: bool
    compaction_event: dict | None    # 精确的 context_compacted 事件，无证据即 null
    compaction_events: list

context
    context_id / parent_context_id / context_label / origin_tool
    legacy: bool / unknown: bool / parent_conflict: bool

tool_call
    sequence / run_id / tool / tool_call_id / context_id / parent_context_id
    context_label / origin_tool / input / input_kind / output / output_kind
    status / latency_ms / node / scope

middleware_decision
    sequence / policy（POLICY_DOCS key，可能为 null）/ middleware / decision
    reason 以及后端实际写入的其余条件字段（tool / risk / total_tokens …）
"""
from __future__ import annotations

import json
import re
from typing import Any

from modules.runtime.policy_meta import POLICY_DOCS

LEGACY_CONTEXT_ID = "legacy"
UNKNOWN_CONTEXT_ID = "unknown"

# middleware class name → POLICY_DOCS key。前端也用同一组别名兜底，
# 避免把 ToolPermissionMiddleware 直接当成 permission 显示成英文类名。
_MIDDLEWARE_POLICY_KEYS = {
    "toolpermissionmiddleware": "permission",
    "toolpermission": "permission",
    "tooltimeoutmiddleware": "timeout",
    "tooltimeout": "timeout",
    "humanintheloopmiddleware": "hitl",
    "humanintheloop": "hitl",
    "hitlmiddleware": "hitl",
    "tokenbudgetmiddleware": "budget",
    "tokenbudget": "budget",
    "summarizationmiddleware": "summarization",
    "piimiddleware": "pii",
    "pii": "pii",
    "modelretrymiddleware": "model_retry",
    "modelretry": "model_retry",
}


def _policy_key(middleware: Any) -> str | None:
    """把真实 middleware class name 映射到 POLICY_DOCS 的 key。"""
    if not middleware:
        return None
    compact = re.sub(r"[^a-z0-9]", "", str(middleware).lower())
    if compact in _MIDDLEWARE_POLICY_KEYS:
        return _MIDDLEWARE_POLICY_KEYS[compact]
    if compact.endswith("middleware"):
        compact = compact[: -len("middleware")]
    if compact in _MIDDLEWARE_POLICY_KEYS:
        return _MIDDLEWARE_POLICY_KEYS[compact]
    for key in POLICY_DOCS:
        if compact in {key.replace("_", ""), key}:
            return key
    return None


def _summary(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = text.replace("\r", "")
    return text[:limit] + ("…" if len(text) > limit else "")


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _ordered(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保持原始顺序；有 ts 时按 ts，没有 ts 时不拿全局 step 打乱交错分支。"""
    indexed = list(enumerate(events))
    indexed.sort(key=lambda pair: (str(pair[1].get("ts") or ""), pair[0]))
    return [event for _, event in indexed]


def _identity(event: dict[str, Any]) -> tuple[str, str | None, bool, bool]:
    """返回 (context_id, parent_context_id, legacy, unknown)。

    legacy 只用于缺少 context_id 的旧 snapshot；显式 unknown 也不编造父子关系。
    """
    if "context_id" not in event or event.get("context_id") in (None, ""):
        return LEGACY_CONTEXT_ID, None, True, False
    context_id = str(event.get("context_id"))
    if context_id == UNKNOWN_CONTEXT_ID:
        return UNKNOWN_CONTEXT_ID, None, False, True
    parent = event.get("parent_context_id")
    parent_id = str(parent) if parent not in (None, "", UNKNOWN_CONTEXT_ID) else None
    return context_id, parent_id, False, False


def _new_context(context_id: str, parent_id: str | None, legacy: bool, unknown: bool) -> dict[str, Any]:
    return {
        "context_id": context_id,
        "parent_context_id": parent_id,
        "context_label": None,
        "origin_tool": None,
        "legacy": legacy,
        "unknown": unknown,
        "steps": 0,
        "appended": [],
        "removed": [],
        "tokens_est": 0,
        "model_calls": 0,
        "compacted": False,
        "parent_conflict": False,
    }


def _build_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def ensure(key: str, event: dict[str, Any]) -> dict[str, Any]:
        if key not in calls:
            context_id, parent_id, legacy, unknown = _identity(event)
            calls[key] = {
                "run_id": event.get("run_id"),
                "tool": event.get("tool") or "unknown",
                "tool_call_id": event.get("tool_call_id"),
                "context_id": context_id,
                "parent_context_id": parent_id,
                "context_label": event.get("context_label"),
                "origin_tool": event.get("origin_tool"),
                "legacy": legacy,
                "unknown": unknown,
                "input": "",
                "input_kind": "tool_arguments",
                "output": "",
                "output_kind": "tool_result",
                "status": "running",
                "latency_ms": None,
                "node": event.get("node"),
                "scope": context_id,
            }
            order.append(key)
        return calls[key]

    for sequence, event in enumerate(_ordered(events), start=1):
        name = event.get("event")
        if name not in {"tool_start", "tool_end", "tool_error"}:
            continue
        run_id = event.get("run_id")
        key = str(run_id) if run_id else f"tool-event-{sequence}"
        call = ensure(key, event)
        call.setdefault("sequence", sequence)
        call["context_label"] = event.get("context_label") or call.get("context_label")
        call["origin_tool"] = event.get("origin_tool") or call.get("origin_tool")
        call["tool"] = event.get("tool") or call["tool"]
        call["tool_call_id"] = event.get("tool_call_id") or call.get("tool_call_id")
        call["node"] = event.get("node") or call.get("node")
        if name == "tool_start":
            # 只展示工具的调用参数，不把它写成"工具看到了聊天历史"。
            call["input"] = _summary(
                event.get("input_args")
                if event.get("input_args") is not None
                else event.get("input")
            )
            call["input_kind"] = event.get("input_kind") or "tool_arguments"
            call["status"] = event.get("status") or "running"
        else:
            if name == "tool_error":
                raw_output = event.get("output_preview") or event.get("error")
                call["output_kind"] = "tool_error"
                call["status"] = "error"
            else:
                raw_output = (
                    event.get("output_preview")
                    if event.get("output_preview") is not None
                    else event.get("output", event.get("result"))
                )
                call["output_kind"] = event.get("output_kind") or "tool_result"
                call["status"] = event.get("status") or "success"
            call["output"] = _summary(raw_output)
            call["latency_ms"] = event.get("latency_ms")
    return [calls[key] for key in order]


def build_context_view(events: list[dict[str, Any]] | None) -> dict[str, Any]:
    ordered = _ordered(list(events or []))
    snapshots = [
        (sequence, event)
        for sequence, event in enumerate(ordered, start=1)
        if event.get("event") == "context_snapshot"
    ]

    segments: dict[tuple[str, str], dict[str, Any]] = {}
    turns: list[dict[str, Any]] = []
    context_state: dict[str, dict[str, Any]] = {}
    previous: dict[str, list[str]] = {}
    counters: dict[str, int] = {}

    for sequence, snapshot in snapshots:
        context_id, parent_id, legacy, unknown = _identity(snapshot)
        state = context_state.setdefault(
            context_id, _new_context(context_id, parent_id, legacy, unknown)
        )
        if parent_id and state["parent_context_id"] is None:
            state["parent_context_id"] = parent_id
        elif parent_id and state["parent_context_id"] != parent_id:
            # 同一个 context_id 出现互相矛盾的父关系时不猜，显式标冲突。
            state["parent_conflict"] = True
        if snapshot.get("context_label"):
            state["context_label"] = state["context_label"] or snapshot.get("context_label")
        if snapshot.get("origin_tool"):
            state["origin_tool"] = state["origin_tool"] or snapshot.get("origin_tool")

        counters[context_id] = counters.get(context_id, 0) + 1
        context_step = counters[context_id]
        current = [item for item in (snapshot.get("segments") or []) if isinstance(item, dict)]
        ids = [str(item.get("id")) for item in current if item.get("id")]
        for item in current:
            segment_id = str(item.get("id") or "")
            if segment_id:
                key = (context_id, segment_id)
                if key not in segments:
                    segments[key] = {
                        **item,
                        "context_id": context_id,
                        "step": context_step,
                        "node": snapshot.get("node"),
                    }
        current_set = set(ids)
        before = previous.get(context_id, [])
        appended = [segment_id for segment_id in ids if segment_id not in before]
        removed = [segment_id for segment_id in before if segment_id not in current_set]
        tokens_est = int(snapshot.get("tokens_est") or sum(
            int(item.get("tokens_est") or 0) for item in current
        ))
        turns.append({
            # sequence 是 trace 事件流里的真实全局位置；step/context_step 只属于
            # 当前 context 分支，GUI 不能用它跨分支重排。
            "sequence": sequence,
            "context_step": context_step,
            "step": context_step,
            "trace_step": snapshot.get("step"),
            "context_id": context_id,
            "parent_context_id": parent_id,
            "context_label": snapshot.get("context_label"),
            "origin_tool": snapshot.get("origin_tool"),
            "run_id": snapshot.get("run_id"),
            "parent_run_id": snapshot.get("parent_run_id"),
            "node": snapshot.get("node") or "",
            "model": snapshot.get("model") or "",
            "tokens_est": tokens_est,
            "messages": len(current),
            "read": current,
            "appended": appended,
            "removed": removed,
            # 兼容旧字段；语义是"移出模型可见范围"，不是压缩证明。
            "moved_out": removed,
            "compacted": False,
            "compaction_event": None,
            "compaction_events": [],
        })
        state["steps"] = context_step
        state["model_calls"] += 1
        state["tokens_est"] += tokens_est
        state["appended"].extend(appended)
        state["removed"].extend(removed)
        previous[context_id] = ids

    # 只有明确的 middleware 压缩事件才能把 compacted 置 true；同时把事件本身
    # 挂到 turn（compaction_event），GUI 不再靠 bool 或 removed 猜压缩。
    compaction_events = [
        (sequence, event)
        for sequence, event in enumerate(ordered, start=1)
        if event.get("event") == "context_compacted"
    ]
    for sequence, event in compaction_events:
        context_id, _, _, _ = _identity(event)
        state = context_state.get(context_id)
        if state is None:
            continue
        state["compacted"] = True
        candidates = [turn for turn in turns if turn["context_id"] == context_id]
        matched: list[dict[str, Any]] = []
        run_id = event.get("run_id")
        step = event.get("step")
        if run_id:
            matched = [turn for turn in candidates if turn.get("run_id") == run_id]
        if not matched and step is not None:
            matched = [
                turn for turn in candidates
                if step in (turn.get("context_step"), turn.get("trace_step"))
            ]
        if not matched and not run_id and step is None and candidates:
            # 事件没有 target 时只能归到该 context 的最后一相，不扩散到其他 context。
            matched = [candidates[-1]]
        event_row = {**event, "sequence": sequence}
        for turn in matched:
            turn["compacted"] = True
            if turn["compaction_event"] is None:
                turn["compaction_event"] = event_row
            turn["compaction_events"].append(event_row)

    tool_calls = _build_tool_calls(ordered)
    contexts: list[dict[str, Any]] = []
    for context_id, state in context_state.items():
        appended = _unique(state["appended"])
        removed = _unique(state["removed"])
        contexts.append({
            "context_id": context_id,
            "parent_context_id": state["parent_context_id"],
            "context_label": state["context_label"],
            "origin_tool": state["origin_tool"],
            "legacy": state["legacy"],
            "unknown": state["unknown"],
            "parent_conflict": state["parent_conflict"],
            "steps": state["steps"],
            "appended": appended,
            "removed": removed,
            "moved_out": removed,
            "appended_count": len(appended),
            "removed_count": len(removed),
            "tokens_est": state["tokens_est"],
            "model_calls": state["model_calls"],
            "tool_calls": sum(1 for call in tool_calls if call["context_id"] == context_id),
            "compacted": state["compacted"],
        })

    decisions = []
    for sequence, event in enumerate(ordered, start=1):
        if event.get("event") != "middleware_decision":
            continue
        row = {
            key: value
            for key, value in event.items()
            if key not in {"trace_id", "thread_id", "scenario", "event"}
        }
        row["sequence"] = sequence
        # canonical：policy 是 POLICY_DOCS key；middleware 保留真实类名。
        row["policy"] = _policy_key(event.get("middleware")) or event.get("policy")
        decisions.append(row)

    def middleware_status(name: str) -> dict[str, Any]:
        rows = [row for row in decisions if row.get("middleware") == name]
        return {
            "instrumented": True,
            "observed": bool(rows),
            "decision_count": len(rows),
            "decisions": _unique([str(row.get("decision")) for row in rows if row.get("decision")]),
        }

    middleware_observation = {
        "TokenBudgetMiddleware": middleware_status("TokenBudgetMiddleware"),
        "ToolPermissionMiddleware": middleware_status("ToolPermissionMiddleware"),
        "other": {
            "instrumented": False,
            "observed": False,
            "reason": "未接入真实 decision 事件；不能根据已装配推断已触发",
        },
    }

    return {
        "available": bool(turns),
        "turns": turns,
        "segments": list(segments.values()),
        "contexts": contexts,
        "tool_calls": tool_calls,
        "total_tokens_est": sum(turn["tokens_est"] for turn in turns),
        "compaction_count": sum(1 for turn in turns if turn["compacted"]),
        "compaction_event_count": len(compaction_events),
        "middleware_decisions": decisions,
        "middleware_observation": middleware_observation,
    }
