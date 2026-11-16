# -*- coding: utf-8 -*-
"""FastAPI instance product API (v1) for one artifact + instance config.

Every route is registered under both ``/api/...`` and ``/api/v1/...``.  Startup
never builds a model or scenario: a missing model credential keeps the product
UI reachable and reports a degraded readiness instead of a false "ready".
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

try:
    from .store import (
        SessionBindingError,
        SessionEnded,
        SessionNotFound,
        SessionStore,
        valid_session_id,
    )
    from .worker import (
        ApprovalGone,
        SessionBusy,
        SessionCapacity,
        SessionManager,
    )
except ImportError:  # artifact runtime is flat (server/store/worker on sys.path)
    from store import (  # type: ignore
        SessionBindingError,
        SessionEnded,
        SessionNotFound,
        SessionStore,
        valid_session_id,
    )
    from worker import (  # type: ignore
        ApprovalGone,
        SessionBusy,
        SessionCapacity,
        SessionManager,
    )

try:  # project-owned business policy; generic artifacts may not ship it
    from robot_vacuum_support import (
        apply_assistant_turn as support_apply_assistant_turn,
        apply_user_turn as support_apply_user_turn,
        decision_snapshot as support_decision_snapshot,
        handoff_summary as support_handoff_summary,
        interpret_turn as support_interpret_turn,
        LlmTurnInterpreter as SupportTurnInterpreter,
        load_state as support_load_state,
        mark_protocol_ack as support_mark_protocol_ack,
        prompt_context as support_prompt_context,
        protocol_ack_answer as support_protocol_ack_answer,
        public_view as support_public_view,
    )
    from robot_vacuum_support import routing as support_routing
    from robot_vacuum_support.evidence import review as support_review_evidence

    SUPPORT_ENABLED = True
    _SUPPORT_INTERPRETER = SupportTurnInterpreter()
except ImportError:  # pragma: no cover - generic runtime fallback
    SUPPORT_ENABLED = False
    _SUPPORT_INTERPRETER = None
    support_routing = None  # type: ignore

try:
    from core.observability.turn_usage import TurnUsage
except ImportError:  # pragma: no cover - very old artifact
    TurnUsage = None  # type: ignore

try:
    from core.observability import latency
except ImportError:  # pragma: no cover - very old artifact
    latency = None  # type: ignore


def _interpret_enabled() -> bool:
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
        # The trace may contain several main-agent model calls (e.g. a tool
        # round).  Keep one combined usage row for this turn, but carry the
        # true request count so routing/accounting is not understated.
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

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class CreateSession(BaseModel):
    workflow: str | None = None


class MessageRequest(BaseModel):
    prompt: str = Field(min_length=1)


class ResumeRequest(BaseModel):
    thread_id: str
    decision: bool = True


def _error(status_code: int, code: str, message: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _map_exception(exc: Exception) -> None:
    if isinstance(exc, SessionNotFound):
        _error(404, "session_not_found", str(exc))
    if isinstance(exc, SessionBindingError):
        _error(409, "binding_conflict", str(exc))
    if isinstance(exc, SessionEnded):
        _error(409, "session_ended", str(exc))
    if isinstance(exc, SessionBusy):
        _error(409, "session_busy", str(exc))
    if isinstance(exc, SessionCapacity):
        _error(409, "session_capacity", str(exc))
    if isinstance(exc, ApprovalGone):
        _error(410, "approval_gone", str(exc))
    if isinstance(exc, ValueError):
        _error(422, "invalid_request", str(exc))
    _error(500, "internal_error", str(exc))


def reserve_session(manager, store, session_id: str, *, resume: bool = False):
    """Atomically reserve the existing worker busy lock for one SSE run.

    Kept as a module function so the duplicate-request contract can be tested
    without starting a model or an HTTP server.
    """
    try:
        store.require_active(session_id)
        if resume:
            session = manager.peek(session_id)
            if session is None or not session.pending_thread_id:
                raise ApprovalGone("原审批运行已不在当前会话进程，请重新发送消息")
        else:
            session = manager.get(session_id)
    except Exception as exc:  # noqa: BLE001
        _map_exception(exc)
        raise
    if not session.busy.acquire(blocking=False):
        _error(409, "session_busy", "该会话正在执行，请等待本次完成或先停止")
    return session


def _identity(config) -> dict:
    return {
        "instance_id": config.instance_id,
        "artifact_id": config.artifact_id,
        "scenario": config.scenario,
        "version": config.version,
        "name": config.name,
    }


def _ui_root(config) -> Path:
    product = Path(config.product_ui_root)
    if (product / "index.html").is_file():
        return product
    fallback = Path(config.runtime_root) / "web"
    return fallback


def _health(config) -> dict:
    required = {
        "manifest": (Path(config.artifact_root) / "manifest.json").is_file(),
        "runtime": Path(config.runtime_root).is_dir(),
        "spec": (Path(config.artifact_root) / config.scenario / "active.json").is_file(),
        "frontend": (_ui_root(config) / "index.html").is_file(),
    }
    try:
        Path(getattr(config, "sessions_root", Path(config.storage_root))).mkdir(
            parents=True, exist_ok=True
        )
        required["storage"] = True
    except OSError:
        required["storage"] = False
    credentials = (
        config.credential_names() if hasattr(config, "credential_names") else []
    )
    bindings = getattr(config, "asset_bindings", {}) or {}
    model_binding = bindings.get("models") if isinstance(bindings, dict) else None
    required_kinds = (
        list(config.required_assets())
        if hasattr(config, "required_assets")
        else []
    )
    # Only an instance that actually needs the shared model directory is asked
    # for it; constant/minimal tools must not be blocked by a missing model dir.
    if model_binding or "models" in required_kinds:
        required["model_assets"] = Path(config.model_root).is_dir()
    missing_bindings = (
        list(config.missing_bindings())
        if hasattr(config, "missing_bindings")
        else []
    )
    for key in missing_bindings:
        required[f"asset_{key}"] = False
    missing_required = (
        list(config.missing_required_assets())
        if hasattr(config, "missing_required_assets")
        else []
    )
    for key in missing_required:
        required[f"asset_{key}"] = False
        if key not in missing_bindings:
            missing_bindings.append(key)
    missing = [name for name, present in required.items() if not present]
    needs_configuration = list(missing_bindings)
    if not credentials:
        needs_configuration.append("model_credentials")
        missing.append("model_credentials")
    ready = not missing
    status = "ok" if ready else (
        "needs_configuration" if missing_bindings else "degraded"
    )
    model_root = Path(config.model_root)
    return {
        "status": status,
        "ready": ready,
        "identity": _identity(config),
        "required_assets": required,
        "needs_configuration": needs_configuration,
        "model": {
            "configured": bool(credentials),
            "credential_names": credentials,
            "root": str(model_root),
            "root_present": model_root.is_dir(),
        },
        "missing": missing,
        "issues": list(getattr(config, "issues", []) or []),
    }


def create_app(config) -> FastAPI:
    mimetypes.init()
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    store = SessionStore(config)
    manager = SessionManager(config, store)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        manager.close_all()

    app = FastAPI(title=f"{config.name} · instance", version="1.0", lifespan=lifespan)
    app.state.instance_config = config
    app.state.store = store
    app.state.manager = manager

    def dual(path: str, methods: list[str]):
        def decorate(func):
            for prefix in ("/api", "/api/v1"):
                app.api_route(prefix + path, methods=methods)(func)
            return func

        return decorate

    # ------------------------------------------------------------ identity
    @dual("/instance", ["GET"])
    def instance_info():
        health = _health(config)
        port = int(getattr(config, "port", 0) or 0)
        frontend_url = (
            f"http://{config.host}:{port}/" if port else "/"
        )
        return {
            **_identity(config),
            "instance_id": config.instance_id,
            "artifact_id": config.artifact_id,
            "scenario": config.scenario,
            "version": config.version,
            "workflows": list(config.workflows),
            "default_workflow": config.default_workflow,
            "api_version": "v1",
            "frontend_url": frontend_url,
            "status": health["status"],
            "readiness": health,
        }

    @dual("/health", ["GET"])
    def health():
        return _health(config)

    # ------------------------------------------------------------ sessions
    @dual("/sessions", ["GET"])
    def list_sessions():
        return {"sessions": store.list()}

    @dual("/sessions", ["POST"])
    def create_session(body: CreateSession | None = None):
        workflow = (body.workflow if body else None) or config.default_workflow
        workflows = list(config.workflows)
        if workflow and workflows and workflow not in workflows:
            _error(422, "unknown_workflow", f"未知工作流：{workflow}")
        metadata = store.create(workflow=workflow)
        return {**metadata, "workspace": str(store.workspace_path(metadata["session_id"]))}

    @dual("/sessions/{session_id}", ["GET"])
    def get_session(session_id: str):
        try:
            metadata = store.get(session_id)
            transcript = store.transcript(session_id)
        except Exception as exc:  # noqa: BLE001 - mapped to HTTP semantics
            _map_exception(exc)
        payload = {
            **metadata,
            "session_id": session_id,
            "transcript": transcript,
            "workspace": str(store.workspace_path(session_id)),
        }
        if SUPPORT_ENABLED:
            state = support_load_state(store.load_support_state(session_id))
            payload["support"] = support_public_view(state)
        return payload

    @dual("/sessions/{session_id}/support", ["GET"])
    def get_support(session_id: str):
        try:
            store.get(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        if not SUPPORT_ENABLED:
            return {"enabled": False, "support": None}
        state = support_load_state(store.load_support_state(session_id))
        return {"enabled": True, "support": support_public_view(state)}

    @dual("/sessions/{session_id}/handoff-summary", ["GET", "POST"])
    def handoff_summary(session_id: str):
        """Read-only local handoff brief.  It is never sent externally.

        GET and POST return the same projection.  Ended/resolved sessions stay
        viewable; the route only reads state and transcript and never mutates
        either.
        """
        try:
            store.get(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        if not SUPPORT_ENABLED:
            _error(503, "support_unavailable", "当前产品未配置售后状态模块")
        state = support_load_state(store.load_support_state(session_id))
        messages = store.transcript(session_id)
        return support_handoff_summary(state, session_id, messages)

    @dual("/sessions/{session_id}", ["DELETE"])
    def end_session(session_id: str):
        try:
            manager.end(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        return {"ok": True, "status": "ended", "session_id": session_id}

    @dual("/sessions/{session_id}/stop", ["POST"])
    def stop_session(session_id: str):
        try:
            manager.stop(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        return {"ok": True, "status": "active", "session_id": session_id}

    @dual("/sessions/{session_id}/reopen", ["POST"])
    def reopen_session(session_id: str):
        try:
            manager.reopen(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        return {"ok": True, "status": "active", "session_id": session_id}

    # ------------------------------------------------------------ messages
    def _reserve(session_id: str, *, resume: bool = False):
        return reserve_session(manager, store, session_id, resume=resume)

    def _review(state, answer: str, retrieval):
        if not SUPPORT_ENABLED or not state:
            return retrieval
        public = support_public_view(state)
        case = public.get("active_case") or {}
        review = support_review_evidence(
            answer,
            retrieval,
            recharge_scope=case.get("topic") == "recharge_failure",
        )
        merged = dict(retrieval or {"status": "not_retrieved", "results": []})
        merged["review"] = review
        return merged

    def _sse(session_id: str, body: dict, action: str, reservation, latency_ctx: dict | None = None, request_span=None):
        updates: queue.Queue = queue.Queue()
        ctx = dict(latency_ctx or {})

        def execute():
            handed_to_manager = False
            run_prompt = str(body.get("prompt") or "")
            support_state = None
            support_payload = None
            retrieval = None
            turn_meter = _new_turn_meter()
            route_decision = None
            ctx_cm = latency.bind_context(**ctx) if latency is not None else None
            if ctx_cm is not None:
                ctx_cm.__enter__()
            try:
                if latency is not None:
                    with latency.span("server.history_state"):
                        history = store.history(session_id)
                        request_body = {**body, "history": history}
                        if SUPPORT_ENABLED:
                            support_state = support_load_state(store.load_support_state(session_id))
                else:
                    history = store.history(session_id)
                    request_body = {**body, "history": history}
                    if SUPPORT_ENABLED:
                        support_state = support_load_state(store.load_support_state(session_id))

                # Transcript is server-authoritative and written once:
                # - run: one user message now, then assistant/error;
                # - resume: one approval event, never an empty/duplicate user.
                if action == "resume":
                    decision = bool(body.get("decision", True))
                    store.append_message(
                        session_id,
                        "event",
                        "approval:" + ("approve" if decision else "reject"),
                        thread_id=body.get("thread_id"),
                    )
                    if support_state is not None:
                        request_body["support_context"] = support_prompt_context(support_state)
                else:
                    next_turn = int((support_state or {}).get("turn") or 0) + 1
                    user_record = store.append_message(
                        session_id, "user", run_prompt, turn=next_turn
                    )
                    if support_state is not None:
                        interpretation = None
                        state_version_before = None
                        if support_routing is not None:
                            # Route evidence is recorded before any model call.
                            state_version_before = support_routing.state_version(support_state)
                            route_decision = support_routing.decide(support_state, run_prompt)
                        route_name = (route_decision or {}).get("route") or "semantic"
                        route_reason = (route_decision or {}).get("reason") or "no_route"
                        if route_decision and route_decision.get("bundle") is not None:
                            # Deterministic slot answer / protocol ack / replay.
                            interpretation = route_decision["bundle"]
                            if route_name == "replay_cache":
                                _record_interpretation(
                                    turn_meter, interpretation, route_name, next_turn
                                )
                        elif run_prompt and _interpret_enabled() and _SUPPORT_INTERPRETER is not None:
                            # One semantic call, only when routing says it is
                            # needed.  Its input is the compressed decision
                            # snapshot (needed facts + pending question), not a
                            # replay of the whole session.
                            call_id = f"interpretation-{next_turn}"
                            if latency is not None:
                                with latency.bind_call(call_id, layer="interpretation"):
                                    with latency.span(
                                        "server.interpretation",
                                        call_id=call_id,
                                        method="llm_structured",
                                    ):
                                        interpretation = support_interpret_turn(
                                            run_prompt,
                                            interpreter=_SUPPORT_INTERPRETER,
                                            context=(
                                                support_routing.interpretation_input(support_state)
                                                if support_routing is not None
                                                else support_public_view(support_state)
                                            ),
                                        )
                            else:
                                interpretation = support_interpret_turn(
                                    run_prompt,
                                    interpreter=_SUPPORT_INTERPRETER,
                                    context=(
                                        support_routing.interpretation_input(support_state)
                                        if support_routing is not None
                                        else support_public_view(support_state)
                                    ),
                                )
                            _record_interpretation(
                                turn_meter, interpretation, route_name, next_turn
                            )
                        applied = support_apply_user_turn(
                            support_state,
                            run_prompt,
                            (user_record or {}).get("message_id"),
                            interpretation=interpretation,
                            route=route_name,
                            route_reason=route_reason,
                        )
                        support_state = applied["state"]
                        if (
                            route_name == "semantic"
                            and isinstance(interpretation, dict)
                            and state_version_before
                            and support_routing is not None
                        ):
                            support_routing.remember_interpretation(
                                support_state,
                                run_prompt,
                                interpretation,
                                state_version_value=state_version_before,
                            )
                        if route_name == "protocol_ack":
                            # No model call, no new question, no fake resolution.
                            support_state = support_mark_protocol_ack(
                                support_state, support_state.get("turn")
                            )
                        # Persist user facts BEFORE the model runs.  A failed or
                        # timed-out model must not erase or invent user facts.
                        store.save_support_state(session_id, support_state)
                        snapshot = support_decision_snapshot(support_state)
                        support_state["decision_snapshot"] = snapshot
                        request_body["support_snapshot"] = snapshot
                        request_body["support_context"] = support_prompt_context(
                            support_state, snapshot
                        )
                        direct = applied.get("direct_answer")
                        if route_name == "protocol_ack" and not direct:
                            direct = support_protocol_ack_answer(support_state)
                        if direct:
                            metrics = _turn_metrics(turn_meter, route_decision, next_turn)
                            assistant_record = store.append_message(
                                session_id,
                                "assistant",
                                direct,
                                turn=support_state.get("turn"),
                            )
                            support_state = support_apply_assistant_turn(
                                support_state,
                                direct,
                                (assistant_record or {}).get("message_id"),
                                turn=support_state.get("turn"),
                                retrieval=None,
                            )
                            support_state["last_turn_metrics"] = metrics
                            store.save_support_state(session_id, support_state)
                            support_payload = support_public_view(support_state)
                            updates.put(
                                {"type": "workspace", "path": str(store.workspace_path(session_id))}
                            )
                            updates.put(
                                {
                                    "type": "answer",
                                    "answer": direct,
                                    "direct": True,
                                    "usage": _usage_with_metrics(None, metrics),
                                    "support": support_payload,
                                    "retrieval": None,
                                }
                            )
                            return

                def on_event(event):
                    if event.get("type") == "answer_delta":
                        updates.put({"type": "answer_delta", "delta": event.get("delta", "")})

                handed_to_manager = True
                if action == "resume":
                    if latency is not None:
                        with latency.span("server.worker_run", action=action) as run_span:
                            request_body["_rvs_latency"] = {
                                **ctx,
                                "worker_parent_span_id": getattr(run_span, "span_id", None),
                            }
                            result = manager.resume(
                                session_id, request_body, event_sink=on_event, reservation=reservation
                            )
                    else:
                        result = manager.resume(
                            session_id, request_body, event_sink=on_event, reservation=reservation
                        )
                else:
                    if latency is not None:
                        with latency.span("server.worker_run", action=action) as run_span:
                            request_body["_rvs_latency"] = {
                                **ctx,
                                "worker_parent_span_id": getattr(run_span, "span_id", None),
                            }
                            result = manager.run(
                                session_id, request_body, event_sink=on_event, reservation=reservation
                            )
                    else:
                        result = manager.run(
                            session_id, request_body, event_sink=on_event, reservation=reservation
                        )
                answer = str(result.get("answer") or "")
                interrupted = result.get("status") == "interrupted"
                if latency is not None:
                    with latency.span("server.persist_assistant"):
                        assistant_record = store.append_message(
                            session_id,
                            "assistant",
                            answer,
                            interrupted=interrupted,
                            thread_id=result.get("thread_id"),
                            turn=(support_state or {}).get("turn"),
                        )
                else:
                    assistant_record = store.append_message(
                        session_id,
                        "assistant",
                        answer,
                        interrupted=interrupted,
                        thread_id=result.get("thread_id"),
                        turn=(support_state or {}).get("turn"),
                    )
                if support_state is not None:
                    if latency is not None:
                        with latency.span("server.postprocess"):
                            if turn_meter is not None:
                                turn_meter.merge(result.get("turn_usage"))
                                _record_main_agent(turn_meter, result.get("usage"), int(support_state.get("turn") or 0))
                            retrieval = _review(support_state, answer, result.get("retrieval"))
                            support_state = support_apply_assistant_turn(
                                support_state,
                                answer,
                                (assistant_record or {}).get("message_id"),
                                turn=support_state.get("turn"),
                                retrieval=retrieval,
                            )
                            metrics = _turn_metrics(
                                turn_meter, route_decision, int(support_state.get("turn") or 0)
                            )
                            support_state["last_turn_metrics"] = metrics
                    else:
                        if turn_meter is not None:
                            # Worker meter carries rag_summary calls; main_agent is
                            # read from trace run_end (not from budget) exactly once.
                            turn_meter.merge(result.get("turn_usage"))
                            _record_main_agent(turn_meter, result.get("usage"), int(support_state.get("turn") or 0))
                        retrieval = _review(support_state, answer, result.get("retrieval"))
                        support_state = support_apply_assistant_turn(
                            support_state,
                            answer,
                            (assistant_record or {}).get("message_id"),
                            turn=support_state.get("turn"),
                            retrieval=retrieval,
                        )
                        metrics = _turn_metrics(
                            turn_meter, route_decision, int(support_state.get("turn") or 0)
                        )
                        support_state["last_turn_metrics"] = metrics
                    if latency is not None:
                        with latency.span("server.persist_support_state"):
                            store.save_support_state(session_id, support_state)
                    else:
                        store.save_support_state(session_id, support_state)
                    support_payload = support_public_view(support_state)
                else:
                    metrics = None
                store.update(
                    session_id,
                    thread_id=result.get("thread_id"),
                    last_status=result.get("status"),
                )
                observable = {
                    "trace_id": result.get("trace_id"),
                    "node_events": result.get("node_events") or [],
                    "trace": result.get("trace") or [],
                    "context": result.get("context") or {},
                }
                updates.put({"type": "workspace", "path": str(store.workspace_path(session_id))})
                usage_payload = _usage_with_metrics(result.get("usage"), metrics)
                if latency is not None:
                    latency.point(
                        "server.answer_enqueue",
                        answer_chars=len(answer),
                        interrupted=interrupted,
                    )
                if interrupted:
                    updates.put(
                        {
                            "type": "interrupt",
                            "answer": answer,
                            "thread_id": result.get("thread_id"),
                            "interrupt": result.get("interrupt"),
                            "usage": usage_payload,
                            "support": support_payload,
                            "retrieval": retrieval,
                            **observable,
                        }
                    )
                else:
                    updates.put(
                        {
                            "type": "answer",
                            "answer": answer,
                            "thread_id": result.get("thread_id"),
                            "usage": usage_payload,
                            "support": support_payload,
                            "retrieval": retrieval,
                            **observable,
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - delivered through SSE
                # Keep the user/approval evidence and add an explicit error row.
                if SUPPORT_ENABLED and support_state is not None:
                    retrieval = getattr(exc, "retrieval", retrieval)
                    if retrieval is not None:
                        retrieval = _review(support_state, "", retrieval)
                        support_state["last_retrieval"] = retrieval
                        active_id = support_state.get("active_case_id")
                        for case in support_state.get("cases") or []:
                            if case.get("case_id") == active_id:
                                case["last_retrieval"] = retrieval
                                break
                        try:
                            store.save_support_state(session_id, support_state)
                        except Exception:
                            pass
                    support_payload = support_public_view(support_state)
                try:
                    store.append_message(session_id, "error", str(exc))
                except Exception:
                    pass
                updates.put(
                    {
                        "type": "error",
                        "error": str(exc),
                        "code": "approval_gone"
                        if isinstance(exc, ApprovalGone)
                        else "run_error",
                        "support": support_payload,
                        "retrieval": retrieval,
                    }
                )
            finally:
                if not handed_to_manager:
                    # No manager call took ownership of the reservation.
                    reservation.busy.release()
                if ctx_cm is not None:
                    ctx_cm.__exit__(None, None, None)
                updates.put({"type": "done"})

        threading.Thread(target=execute, daemon=True).start()

        def events():
            if latency is not None:
                first = True
                try:
                    while True:
                        event = updates.get()
                        if first:
                            first = False
                            latency.point(
                                "server.sse_first_byte",
                                context=ctx,
                                event_type=event.get("type"),
                            )
                        yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
                        if event["type"] == "done":
                            latency.point("server.sse_done", context=ctx)
                            break
                finally:
                    if request_span is not None:
                        request_span.end(status="ok")
            else:
                while True:
                    event = updates.get()
                    yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
                    if event["type"] == "done":
                        break

        headers = {}
        if latency is not None and ctx.get("request_id"):
            headers["X-RVS-Request-Id"] = str(ctx["request_id"])
            headers["X-RVS-Cold"] = "1" if ctx.get("cold") else "0"
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    @dual("/sessions/{session_id}/messages", ["POST"])
    def post_message(session_id: str, body: MessageRequest, request: Request):
        if latency is None:
            reservation = _reserve(session_id)
            return _sse(session_id, {"prompt": body.prompt}, "run", reservation)
        request_id = request.headers.get("X-RVS-Request-Id") or latency.new_id("req")
        turn_id = request.headers.get("X-RVS-Turn-Id") or latency.new_id("turn")
        cold = manager.peek(session_id) is None
        ctx = {
            "request_id": request_id,
            "turn_id": turn_id,
            "cold": cold,
            "process_role": "server",
        }
        with latency.bind_context(**ctx):
            latency.point("server.receive", path="/messages", cold=cold)
            with latency.span("server.reserve", cold=cold):
                reservation = _reserve(session_id)
            request_span = latency.open_span("server.request", cold=cold)
            return _sse(
                session_id,
                {"prompt": body.prompt},
                "run",
                reservation,
                latency_ctx=ctx,
                request_span=request_span,
            )

    @dual("/sessions/{session_id}/resume", ["POST"])
    def resume_message(session_id: str, body: ResumeRequest):
        reservation = _reserve(session_id, resume=True)
        return _sse(
            session_id,
            {"thread_id": body.thread_id, "decision": body.decision},
            "resume",
            reservation,
        )

    # ------------------------------------------------------------ files
    @dual("/sessions/{session_id}/files", ["GET"])
    def list_files(session_id: str):
        try:
            files = store.workspace_files(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        return {"files": files, "workspace": str(store.workspace_path(session_id))}

    @dual("/sessions/{session_id}/files", ["POST"])
    def upload_file(session_id: str, file: UploadFile = File(...)):
        try:
            store.get(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        filename = file.filename or ""
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or Path(filename).name != filename
        ):
            _error(422, "invalid_filename", "无效文件名")
        root = store.workspace_path(session_id).resolve()
        target = (root / "uploads" / filename).resolve()
        if not target.is_relative_to(root):
            _error(422, "path_escape", "文件路径超出工作区")
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            _error(413, "file_too_large", "单个文件不能超过 10 MB")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as output:
                output.write(data)
        except FileExistsError:
            _error(409, "file_exists", "同名文件已存在，请重命名后上传")
        return {"path": "uploads/" + filename, "workspace": str(root)}

    @dual("/sessions/{session_id}/file", ["GET"])
    def download_file(session_id: str, path: str):
        try:
            target = store.resolve_workspace_file(session_id, path)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, FileNotFoundError):
                _error(404, "file_not_found", str(exc))
            _map_exception(exc)
        return FileResponse(target, filename=target.name)

    # ------------------------------------------------------------ product UI
    @app.get("/")
    def index():
        root = _ui_root(config)
        target = root / "index.html"
        if not target.is_file():
            _error(503, "frontend_missing", "实例前端缺失")
        return FileResponse(target, headers={"Cache-Control": "no-store"})

    @app.get("/{path:path}")
    def static_file(path: str):
        root = _ui_root(config).resolve()
        target = (root / path).resolve()
        if target.is_file() and target.is_relative_to(root):
            return FileResponse(target, headers={"Cache-Control": "no-store"})
        return index()

    return app


def main() -> None:
    import argparse

    from modules.instances.runtime.bootstrap import (
        configure_environment,
        load_instance_config,
        resolve_instance_config,
    )

    parser = argparse.ArgumentParser(description="Agent Harness instance runtime")
    parser.add_argument("--instance-config")
    parser.add_argument("--instance-root")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    config = load_instance_config(
        resolve_instance_config(
            instance_config=args.instance_config, instance_root=args.instance_root
        )
    )
    if args.host:
        config.host = args.host
    if args.port is not None:
        config.port = args.port
    configure_environment(config)
    import uvicorn

    uvicorn.run(create_app(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
