# -*- coding: utf-8 -*-
"""Execution adapter for the self-contained instance runtime.

Only runtime/scenario abstractions are used.  It never imports the editor HTTP
server or the old chat runtime.  Approval is resumed only inside the same live
worker process; a restart loses the pending runtime and returns ``ResumeGone``
instead of rebuilding an approval from current config.
"""
from __future__ import annotations

import uuid
from typing import Any

from core.scenarios.registry import get_scenario
from core.observability import latency
from core.observability.turn_usage import TurnUsage, turn_usage_scope
from core.tools.rag.evidence import begin_collection, end_collection
from modules.runtime.framework import FrameworkAgent
from modules.runtime.workflow import WorkflowRuntime


class ResumeGone(RuntimeError):
    """The original approval run no longer exists in this worker."""


def _require_model_config() -> None:
    """Fail with an explicit configuration error instead of a provider traceback."""
    import os

    if os.environ.get("AH_INSTANCE_MODEL_REQUIRED") == "1":
        names = os.environ.get("AH_INSTANCE_MODEL_CREDENTIALS", "")
        raise RuntimeError(
            "模型凭据未配置：请在实例 environment_file 中配置模型 API Key"
            + (f"（已识别：{names}）" if names else "")
            + "。产品界面和 health 仍可访问，但发送消息需要模型配置。"
        )


_direct_agent: FrameworkAgent | None = None
_direct_thread_id: str | None = None
_direct_history_primed = False
_pending: dict[str, WorkflowRuntime] = {}


def reset() -> None:
    global _direct_agent, _direct_thread_id, _direct_history_primed
    _direct_agent = None
    _direct_thread_id = None
    _direct_history_primed = False
    _pending.clear()


def _prompt(body: dict, include_history: bool = True) -> str:
    prompt = str(body.get("prompt") or "").strip()
    history = body.get("history") or []
    support = str(body.get("support_context") or "").strip()
    if include_history and history:
        lines: list[str] = []
        for item in list(history)[-12:]:
            role = "用户" if item.get("role") == "user" else "助手"
            text = str(item.get("content") or "").strip()
            if text:
                lines.append(f"{role}: {text}")
        if lines:
            prompt = "历史对话：\n" + "\n".join(lines) + "\n\n当前用户：" + prompt
    if support:
        # The server-authoritative state is passed as an opaque block; this
        # adapter does not interpret any business field.
        prompt = support + "\n\n" + prompt
    return prompt


def _final_answer(messages: list[Any]) -> str:
    for message in reversed(messages or []):
        role = getattr(message, "type", None)
        if role is None and isinstance(message, dict):
            role = message.get("role")
        if role not in {"ai", "assistant"}:
            continue
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        if content:
            return str(content)
    return ""


def _usage(budget: Any, trace_summary: dict | None = None) -> dict | None:
    if budget is None and not trace_summary:
        return None
    summary = trace_summary or {}
    events = [item for item in summary.get("model_events") or [] if isinstance(item, dict)]
    main_events = [item for item in events if item.get("node") != "tools"]
    tool_events = [item for item in events if item.get("node") == "tools"]
    if events:
        main_prompt = sum(int(item.get("prompt_tokens") or 0) for item in main_events)
        main_completion = sum(int(item.get("completion_tokens") or 0) for item in main_events)
        main_total = sum(int(item.get("total_tokens") or 0) for item in main_events)
        tool_total = sum(int(item.get("total_tokens") or 0) for item in tool_events)
    else:
        main_prompt = summary.get("prompt_tokens")
        main_completion = summary.get("completion_tokens")
        main_total = summary.get("total_tokens")
        tool_total = None
    main_layer = {
        "requests": len(main_events) if events else summary.get("model_calls"),
        "model_calls": len(main_events) if events else summary.get("model_calls"),
        "input_tokens": main_prompt,
        "output_tokens": main_completion,
        "total_tokens": main_total,
        "usage_status": "reported" if main_total is not None else "unavailable",
        "latency_ms": summary.get("latency_ms"),
        "source": "trace_run_end_excluding_tool_model_calls",
        "tool_model_calls_in_trace": len(tool_events),
        "tool_model_tokens_in_trace": tool_total,
    }
    return {
        "total_tokens": (summary.get("total_tokens") if summary else None)
        if budget is None
        else budget.total_tokens,
        "token_limit": getattr(budget, "max_tokens", None),
        "by_layer": {"main_agent": main_layer} if summary else {},
        "trace_total_tokens": summary.get("total_tokens"),
        "trace_tool_model_tokens": tool_total,
        "note": "trace_total_tokens includes tool-internal model calls; main_agent excludes node='tools' to avoid double counting with the rag_summary meter",
    }


def _observability(trace_id: str | None, node_events: list | None) -> dict:
    """Shared trace/context payload read from *this worker's* AH_OUTPUT_ROOT.

    ``trace_read`` / ``context_view`` are the same read-only projections used by
    the editor and console, but the worker never imports the editor server and
    never reads the repository's outputs tree.
    """
    events: list[dict] = []
    context: dict = {}
    try:
        from core.observability.context_view import build_context_view
        from core.observability.trace_read import read_trace_events

        events = read_trace_events(trace_id) if trace_id else []
        context = build_context_view(events)
    except Exception:  # noqa: BLE001 - observability must not break the answer
        events = []
        try:
            from core.observability.context_view import build_context_view

            context = build_context_view([])
        except Exception:  # noqa: BLE001
            context = {"available": False, "turns": [], "segments": [], "contexts": []}
    return {
        "trace_id": trace_id,
        "node_events": list(node_events or []),
        "trace": events,
        "context": context,
    }


def _answer(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result or "")
    answer = str(result.get("answer") or result.get("tool_result") or "")
    if not answer:
        answer = _final_answer(result.get("messages", []))
    return answer


def run(name: str, body: dict, event_sink=None, token_sink=None) -> dict:
    """Run one turn and attach this turn's real retrieval evidence.

    Retrieval evidence is collected even when the tool/model raises, then
    attached to the exception so the server can persist an explicit error
    state instead of a fake empty-success result.
    """
    token = begin_collection()
    meter = TurnUsage()
    try:
        with turn_usage_scope(meter):
            result = _run_impl(name, body, event_sink=event_sink, token_sink=token_sink)
    except Exception as exc:  # noqa: BLE001 - attach evidence, then re-raise
        retrieval = end_collection(token)
        try:
            setattr(exc, "retrieval", retrieval)
            setattr(exc, "turn_usage", meter.summary())
        except Exception:  # noqa: BLE001
            pass
        raise
    retrieval = end_collection(token)
    result["retrieval"] = retrieval
    result["turn_usage"] = meter.summary()
    return result


def _run_impl(name: str, body: dict, event_sink=None, token_sink=None) -> dict:
    """Run one turn; stream deltas/nodes through the sinks."""
    _require_model_config()
    global _direct_agent, _direct_thread_id, _direct_history_primed
    prompt = _prompt(body)
    with latency.span("worker.scenario_resolve", scenario=name):
        scenario = get_scenario(name)
        with latency.span("worker.scenario_resolve_execution", scenario=name):
            execution = scenario.resolve_execution()
    if not execution.workflow_factory:
        if _direct_agent is None:
            with latency.span("worker.agent_build", scenario=name):
                _direct_agent = FrameworkAgent(scenario)
        # A fresh UUID per worker/reset; stop/reopen keeps the worker alive but a
        # crashed worker naturally starts a new thread.
        if _direct_thread_id is None:
            _direct_thread_id = f"instance-{uuid.uuid4().hex}"
            _direct_history_primed = False
        checkpoint = bool(getattr(execution, "checkpoint", False))
        turn_prompt = _prompt(
            body, include_history=not checkpoint or not _direct_history_primed
        )
        thread_id = _direct_thread_id
        answer = ""
        first_delta = False
        try:
            with latency.span("worker.answer_stream", thread_id=thread_id):
                for delta in _direct_agent.execute_stream(turn_prompt, thread_id=thread_id):
                    if not first_delta and delta:
                        first_delta = True
                        latency.point(
                            "worker.first_delta",
                            chars=len(delta),
                            note="first visible answer chunk; product path yields a completed AI message",
                        )
                    answer += delta
                    if token_sink is not None:
                        token_sink(delta)
        except Exception:
            # A half-written checkpoint must not poison the next turn.
            _direct_thread_id = None
            _direct_history_primed = False
            raise
        _direct_history_primed = True
        interrupted = _direct_agent.last_interrupt is not None
        return {
            "status": "interrupted" if interrupted else "completed",
            "answer": answer,
            "thread_id": thread_id,
            "interrupt": _direct_agent.last_interrupt if interrupted else None,
            "usage": _usage(
                getattr(_direct_agent, "last_budget", None),
                getattr(_direct_agent, "last_usage_summary", None),
            ),
            **_observability(getattr(_direct_agent, "last_trace_id", None), []),
        }

    with latency.span("worker.workflow_build", scenario=name):
        runtime = WorkflowRuntime(scenario)
        workflows = runtime.list_workflows()
        workflow = body.get("workflow") or (workflows[0] if workflows else None)
    if not workflow:
        raise RuntimeError("场景没有可运行的工作流")
    with latency.span("worker.workflow_run", workflow=workflow):
        result = runtime.run(
            workflow,
            prompt,
            event_sink=lambda event: event_sink(event) if event_sink else None,
            token_sink=token_sink,
        )
    payload = {
        "status": "interrupted" if runtime.last_interrupt is not None else "completed",
        "answer": _answer(result),
        "thread_id": runtime.thread_id,
        "interrupt": runtime.last_interrupt if runtime.last_interrupt is not None else None,
        "usage": _usage(runtime.run_budget, runtime.last_usage_summary),
        **_observability(runtime.last_trace_id, runtime.last_node_events),
    }
    if runtime.last_interrupt is not None and runtime.thread_id:
        _pending[runtime.thread_id] = runtime
    return payload


def resume(name: str, body: dict, event_sink=None, token_sink=None) -> dict:
    """Resume approval in the same live worker; otherwise raise ResumeGone."""
    _require_model_config()
    global _direct_agent
    thread_id = str(body.get("thread_id") or "")
    decision = bool(body.get("decision", True))
    runtime = _pending.get(thread_id)
    if runtime is not None:
        result = runtime.resume(
            decision,
            event_sink=lambda event: event_sink(event) if event_sink else None,
            token_sink=token_sink,
        )
        if runtime.last_interrupt is None:
            _pending.pop(thread_id, None)
        return {
            "status": "interrupted" if runtime.last_interrupt is not None else "completed",
            "answer": _answer(result),
            "thread_id": runtime.thread_id,
            "interrupt": runtime.last_interrupt if runtime.last_interrupt is not None else None,
            "usage": _usage(runtime.run_budget, runtime.last_usage_summary),
            **_observability(runtime.last_trace_id, runtime.last_node_events),
        }

    if (
        _direct_agent is not None
        and _direct_thread_id is not None
        and thread_id == _direct_thread_id
        and _direct_agent.last_interrupt is not None
    ):
        result = _direct_agent.resume(
            _direct_thread_id,
            [{"type": "approve" if decision else "reject"}],
        )
        interrupted = _direct_agent.last_interrupt is not None
        return {
            "status": "interrupted" if interrupted else "completed",
            "answer": _final_answer(result.get("messages", [])),
            "thread_id": _direct_thread_id,
            "interrupt": _direct_agent.last_interrupt if interrupted else None,
            "usage": _usage(
                getattr(_direct_agent, "last_budget", None),
                getattr(_direct_agent, "last_usage_summary", None),
            ),
            **_observability(getattr(_direct_agent, "last_trace_id", None), []),
        }

    raise ResumeGone("原审批运行已不在当前会话进程（可能已停止或重启）。请重新发送消息。")
