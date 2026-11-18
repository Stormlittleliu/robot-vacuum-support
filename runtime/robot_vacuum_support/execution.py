# -*- coding: utf-8 -*-
"""Scene-owned execution entry for the robot-vacuum support product.

The generic instance server should only do transport, session auth and handoff.
This module owns the business turn lifecycle:

    prepare  -> semantic extraction / fact update / snapshot / context / route
              -> direct answer, or a ready worker request body
    finish   -> assistant suggestion + retrieval-source evidence + metrics
    fail     -> typed error bookkeeping without forging a successful turn

State and snapshot remain single-sourced: every function reads/writes the same
``support_state`` object loaded by ``load_state``; the prompt context is built
from the same ``decision_snapshot`` that routing and the answer path use.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any

from . import routing as support_routing
from . import state as support
from .evidence import review as review_evidence
from .interpretation import LlmTurnInterpreter, interpret_turn

try:  # optional on very old/generic artifacts
    from core.observability.turn_usage import TurnUsage
except ImportError:  # pragma: no cover
    TurnUsage = None  # type: ignore


@dataclass
class PreparedTurn:
    """Everything the transport layer needs before/after the worker call."""

    state: dict[str, Any] | None
    request_body: dict[str, Any]
    route: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    direct_answer: str | None = None
    direct_payload: dict[str, Any] | None = None
    assistant_record: dict[str, Any] | None = None
    turn_meter: Any = None
    history: list[dict[str, str]] = field(default_factory=list)


def default_interpreter() -> LlmTurnInterpreter:
    return LlmTurnInterpreter()


def interpret_enabled() -> bool:
    value = os.getenv("RVS_TURN_INTERPRETER", "").strip().lower()
    return value not in {"off", "0", "false", "no"}


def _new_turn_meter():
    return TurnUsage() if TurnUsage is not None else None


def _record_interpretation(meter, bundle: dict | None, route: str, turn: int) -> None:
    if meter is None or not isinstance(bundle, dict):
        return
    trace = bundle.get("trace") or {}
    reused = trace.get("method") == "replay_cache"
    meter.record(
        layer="interpretation",
        usage=trace.get("usage"),
        model=trace.get("model"),
        latency_ms=trace.get("latency_ms"),
        call_id=f"interpretation-{turn}",
        method=trace.get("method"),
        route=route,
        fallback=bool(trace.get("fallback_used")),
        reused=reused,
        model_requests=0 if reused else 1,
        attempts=trace.get("attempts"),
    )


def _record_main_agent(meter, result_usage: dict | None, turn: int) -> None:
    if meter is None:
        return
    main = ((result_usage or {}).get("by_layer") or {}).get("main_agent") or {}
    if not main:
        return
    meter.record(
        layer="main_agent",
        usage={
            "input_tokens": main.get("input_tokens"),
            "output_tokens": main.get("output_tokens"),
            "total_tokens": main.get("total_tokens"),
        },
        model=None,
        latency_ms=main.get("latency_ms"),
        call_id=f"main-agent-{turn}",
        method="react_answer",
        route="semantic",
        model_requests=int(main.get("model_calls") or main.get("requests") or 1),
        aggregated=bool(int(main.get("model_calls") or 0) > 1),
    )


def _turn_metrics(meter, route: dict | None, turn: int) -> dict | None:
    if meter is None:
        return None
    summary = meter.summary()
    summary["route"] = (route or {}).get("route")
    summary["route_reason"] = (route or {}).get("reason")
    summary["turn"] = turn
    return summary


def _usage_with_metrics(result_usage: dict | None, metrics: dict | None) -> dict | None:
    usage = dict(result_usage or {})
    if metrics is not None:
        usage["turn_metrics"] = metrics
        usage["total_tokens_all_layers"] = (metrics.get("total") or {}).get("total_tokens")
        usage["usage_status_all_layers"] = metrics.get("usage_status")
        usage["cost"] = None
        usage["cost_status"] = "unavailable_no_authoritative_unit_price"
    return usage or None


def _review(state: dict[str, Any] | None, answer: str, retrieval: Any) -> Any:
    if not state:
        return retrieval
    public = support.public_view(state)
    case = public.get("active_case") or {}
    review = review_evidence(
        answer,
        retrieval,
        recharge_scope=case.get("topic") == "recharge_failure",
    )
    merged = dict(retrieval or {"status": "not_retrieved", "results": []})
    merged["review"] = review
    return merged


def _interpret(
    prompt: str,
    state: dict[str, Any],
    *,
    interpreter: Any,
    enabled: bool,
    latency_module: Any,
    turn: int,
):
    if not prompt or not enabled or interpreter is None:
        return None
    context = support_routing.interpretation_input(state)
    call_id = f"interpretation-{turn}"
    if latency_module is not None:
        with latency_module.bind_call(call_id, layer="interpretation"):
            with latency_module.span(
                "server.interpretation",
                call_id=call_id,
                method="llm_structured",
            ):
                return interpret_turn(
                    prompt,
                    interpreter=interpreter,
                    context=context,
                )
    return interpret_turn(prompt, interpreter=interpreter, context=context)


def prepare_run(
    store: Any,
    session_id: str,
    body: dict[str, Any],
    *,
    interpreter: Any = None,
    enabled: bool | None = None,
    latency_module: Any = None,
) -> PreparedTurn:
    """Run the deterministic/semantic prelude and produce a worker request body.

    A direct answer is fully persisted here and returned in ``direct_payload``;
    the caller must not call the worker.  Otherwise ``request_body`` carries the
    bounded history + the same snapshot used to build ``support_context``.
    """
    prompt = str(body.get("prompt") or "")
    history = store.history(session_id)
    state = support.load_state(store.load_support_state(session_id))
    request_body = {**body, "history": history}
    next_turn = int(state.get("turn") or 0) + 1
    meter = _new_turn_meter()

    user_record = store.append_message(
        session_id,
        "user",
        prompt,
        turn=next_turn,
        author={"kind": "user", "display_name": "用户"},
    )

    route_decision = support_routing.decide(state, prompt)
    route_name = (route_decision or {}).get("route") or "semantic"
    route_reason = (route_decision or {}).get("reason") or "no_route"
    interpretation = None
    state_version_before = support_routing.state_version(state)
    if route_decision and route_decision.get("bundle") is not None:
        interpretation = route_decision["bundle"]
        if route_name == "replay_cache":
            _record_interpretation(meter, interpretation, route_name, next_turn)
    else:
        interpretation = _interpret(
            prompt,
            state,
            interpreter=interpreter,
            enabled=interpret_enabled() if enabled is None else bool(enabled),
            latency_module=latency_module,
            turn=next_turn,
        )
        if interpretation is not None:
            _record_interpretation(meter, interpretation, route_name, next_turn)

    applied = support.apply_user_turn(
        state,
        prompt,
        (user_record or {}).get("message_id"),
        interpretation=interpretation,
        route=route_name,
        route_reason=route_reason,
    )
    state = applied["state"]
    if (
        route_name == "semantic"
        and isinstance(interpretation, dict)
        and state_version_before
    ):
        support_routing.remember_interpretation(
            state,
            prompt,
            interpretation,
            state_version_value=state_version_before,
        )
    if route_name == "protocol_ack":
        state = support.mark_protocol_ack(state, state.get("turn"))

    # Persist user facts BEFORE the model runs.  A failed/timed-out model must
    # not erase or invent user facts.
    store.save_support_state(session_id, state)
    snapshot = support.decision_snapshot(state)
    state["decision_snapshot"] = snapshot
    request_body["support_snapshot"] = snapshot
    request_body["support_context"] = support.prompt_context(state, snapshot)

    direct = applied.get("direct_answer")
    if route_name == "protocol_ack" and not direct:
        direct = support.protocol_ack_answer(state)
    prepared = PreparedTurn(
        state=state,
        request_body=request_body,
        route=route_decision,
        turn_meter=meter,
        history=history,
    )
    if not direct:
        return prepared

    metrics = _turn_metrics(meter, route_decision, next_turn)
    assistant_record = store.append_message(
        session_id,
        "assistant",
        direct,
        turn=state.get("turn"),
        author={"kind": "assistant", "display_name": "机器人助手"},
    )
    state = support.apply_assistant_turn(
        state,
        direct,
        (assistant_record or {}).get("message_id"),
        turn=state.get("turn"),
        retrieval=None,
    )
    state["last_turn_metrics"] = metrics
    store.save_support_state(session_id, state)
    prepared.state = state
    prepared.metrics = metrics
    prepared.direct_answer = direct
    prepared.assistant_record = assistant_record
    prepared.direct_payload = {
        "answer": direct,
        "direct": True,
        "usage": _usage_with_metrics(None, metrics),
        "support": support.public_view(state),
        "retrieval": None,
    }
    return prepared


def prepare_resume(
    store: Any,
    session_id: str,
    body: dict[str, Any],
    *,
    latency_module: Any = None,
) -> PreparedTurn:
    """Persist the approval event and build the resume request body."""
    state = support.load_state(store.load_support_state(session_id))
    decision = bool(body.get("decision", True))
    store.append_message(
        session_id,
        "event",
        "approval:" + ("approve" if decision else "reject"),
        thread_id=body.get("thread_id"),
    )
    request_body = {**body}
    request_body["support_context"] = support.prompt_context(state)
    return PreparedTurn(state=state, request_body=request_body, turn_meter=_new_turn_meter())


def finish_run(
    store: Any,
    session_id: str,
    prepared: PreparedTurn | None,
    result: dict[str, Any],
    *,
    latency_module: Any = None,
) -> dict[str, Any]:
    """Persist the assistant suggestion, evidence and usage for a real run."""
    answer = str(result.get("answer") or "")
    interrupted = result.get("status") == "interrupted"
    state = prepared.state if prepared is not None else None
    meter = prepared.turn_meter if prepared is not None else None
    route = prepared.route if prepared is not None else None
    assistant_record = store.append_message(
        session_id,
        "assistant",
        answer,
        interrupted=interrupted,
        thread_id=result.get("thread_id"),
        turn=(state or {}).get("turn"),
        author={"kind": "assistant", "display_name": "机器人助手"},
    )
    retrieval = result.get("retrieval")
    metrics = None
    support_payload = None
    if state is not None:
        if meter is not None:
            # Worker meter carries rag_summary calls; main_agent is read from
            # trace run_end exactly once.
            meter.merge(result.get("turn_usage"))
            _record_main_agent(meter, result.get("usage"), int(state.get("turn") or 0))
        retrieval = _review(state, answer, retrieval)
        state = support.apply_assistant_turn(
            state,
            answer,
            (assistant_record or {}).get("message_id"),
            turn=state.get("turn"),
            retrieval=retrieval,
        )
        metrics = _turn_metrics(meter, route, int(state.get("turn") or 0))
        state["last_turn_metrics"] = metrics
        store.save_support_state(session_id, state)
        support_payload = support.public_view(state)
    store.update(
        session_id,
        thread_id=result.get("thread_id"),
        last_status=result.get("status"),
    )
    return {
        "answer": answer,
        "assistant_record": assistant_record,
        "support": support_payload,
        "retrieval": retrieval,
        "metrics": metrics,
        "usage": _usage_with_metrics(result.get("usage"), metrics),
        "observable": {
            "trace_id": result.get("trace_id"),
            "node_events": result.get("node_events") or [],
            "trace": result.get("trace") or [],
            "context": result.get("context") or {},
        },
    }


def fail_run(
    store: Any,
    session_id: str,
    prepared: PreparedTurn | None,
    exc: BaseException,
    *,
    error_message: str | None = None,
    latency_module: Any = None,
) -> dict[str, Any]:
    """Persist retrieval evidence and an explicit error row for a failed turn."""
    state = prepared.state if prepared is not None else None
    retrieval = getattr(exc, "retrieval", None)
    support_payload = None
    if state is not None:
        if retrieval is not None:
            retrieval = _review(state, "", retrieval)
            state["last_retrieval"] = retrieval
            active_id = state.get("active_case_id")
            for case in state.get("cases") or []:
                if case.get("case_id") == active_id:
                    case["last_retrieval"] = retrieval
                    break
            try:
                store.save_support_state(session_id, state)
            except Exception:  # noqa: BLE001 - error path must still emit
                pass
        support_payload = support.public_view(state)
    try:
        store.append_message(
            session_id,
            "error",
            error_message if error_message is not None else str(exc),
        )
    except Exception:  # noqa: BLE001 - error path must still emit
        pass
    try:
        store.update(session_id, last_status="error")
    except Exception:  # noqa: BLE001
        pass
    return {"support": support_payload, "retrieval": retrieval}
