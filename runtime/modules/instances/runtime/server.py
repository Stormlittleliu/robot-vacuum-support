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
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

try:
    from .security import (
        ACCESS_TOKEN_ENV,
        ADMIN_TOKEN_ENV,
        access_auth_mode,
        access_token_from_request,
        admin_auth_mode,
        allowed_origins_from_env,
        bearer_token,
        configured_token,
        constant_time_equals,
        loopback_binding,
        origin_allowed,
        static_hosting_enabled,
    )
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
    from .handoff import (
        DEFAULT_OPERATOR,
        HandoffConflict,
        HandoffCorrupt,
        HandoffNotFound,
        HumanHandoffStore,
        admin_ticket_view,
        queued_for_admin,
        ticket_view,
    )
except ImportError:  # artifact runtime is flat (server/store/worker on sys.path)
    from security import (  # type: ignore
        ACCESS_TOKEN_ENV,
        ADMIN_TOKEN_ENV,
        access_auth_mode,
        access_token_from_request,
        admin_auth_mode,
        allowed_origins_from_env,
        bearer_token,
        configured_token,
        constant_time_equals,
        loopback_binding,
        origin_allowed,
        static_hosting_enabled,
    )
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
    from handoff import (  # type: ignore
        DEFAULT_OPERATOR,
        HandoffConflict,
        HandoffCorrupt,
        HandoffNotFound,
        HumanHandoffStore,
        admin_ticket_view,
        queued_for_admin,
        ticket_view,
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


def _failure_code(exc: BaseException) -> str:
    try:
        from core.llm.errors import failure_code

        return failure_code(exc)
    except Exception:  # noqa: BLE001 - very old artifact
        return "run_error"


def _failure_message(exc: BaseException) -> str:
    try:
        from core.llm.errors import safe_error_message

        return safe_error_message(exc)
    except Exception:  # noqa: BLE001 - very old artifact
        return "本轮执行失败，请重试。"


def _failure_retryable(exc: BaseException) -> bool:
    try:
        from core.llm.errors import failure_retryable

        return failure_retryable(exc)
    except Exception:  # noqa: BLE001
        return True


def _failure_requires_configuration(exc: BaseException) -> bool:
    try:
        from core.llm.errors import failure_requires_configuration

        return failure_requires_configuration(exc)
    except Exception:  # noqa: BLE001
        return False


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


class HandoffRequestBody(BaseModel):
    request_id: str | None = None


class AdminReplyBody(BaseModel):
    message: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class AdminCloseBody(BaseModel):
    note: str = ""
    request_id: str | None = None


class ApiModel(BaseModel):
    """Response-model base that keeps forward-compatible extra fields."""

    model_config = ConfigDict(extra="allow")


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class HealthInfo(ApiModel):
    status: str
    ready: bool
    required_assets: dict[str, bool] = Field(default_factory=dict)
    needs_configuration: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class InstanceInfo(ApiModel):
    instance_id: str
    scenario: str
    version: int
    name: str
    workflows: list[str] = Field(default_factory=list)
    default_workflow: str | None = None
    api_version: str = "v1"
    status: str = "degraded"
    readiness: HealthInfo | None = None


class SessionSummary(ApiModel):
    session_id: str
    status: str | None = None
    workflow: str | None = None
    updated_at: float | int | str | None = None


class SessionListResponse(ApiModel):
    sessions: list[SessionSummary] = Field(default_factory=list)


class SessionDetail(SessionSummary):
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    workspace: str | None = None
    handoff: dict[str, Any] | None = None
    support: dict[str, Any] | None = None


class CreatedSession(SessionSummary):
    workspace: str | None = None


class HandoffStatus(ApiModel):
    enabled: bool | None = True
    status: str
    ticket: dict[str, Any] | None = None
    can_request: bool | None = None


class HandoffRequestResult(ApiModel):
    ok: bool
    ticket: dict[str, Any] | None = None
    created: bool | None = None
    duplicate: bool | None = None


class AdminListResult(ApiModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    filter: str = "all"
    admin: dict[str, Any] = Field(default_factory=dict)


class AdminDetailResult(ApiModel):
    ticket: dict[str, Any] | None = None
    session: dict[str, Any] = Field(default_factory=dict)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    handoff: dict[str, Any] | None = None
    queued_messages: list[dict[str, Any]] = Field(default_factory=list)
    admin: dict[str, Any] = Field(default_factory=dict)


class AdminActionResult(ApiModel):
    ok: bool
    ticket: dict[str, Any] | None = None
    changed: bool | None = None
    duplicate: bool | None = None


class SimpleOk(ApiModel):
    ok: bool
    status: str | None = None
    session_id: str | None = None


class FileListResult(ApiModel):
    files: list[dict[str, Any]] = Field(default_factory=list)
    workspace: str | None = None


class UploadResult(ApiModel):
    path: str
    workspace: str


def _error(status_code: int, code: str, message: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


API_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "请求不合法"},
    401: {"model": ErrorResponse, "description": "缺少或无效的 Bearer token"},
    403: {"model": ErrorResponse, "description": "Origin/认证/权限拒绝"},
    404: {"model": ErrorResponse, "description": "资源不存在"},
    409: {"model": ErrorResponse, "description": "状态冲突或重复请求"},
    413: {"model": ErrorResponse, "description": "文件过大"},
    415: {"model": ErrorResponse, "description": "需要 application/json"},
    422: {"model": ErrorResponse, "description": "参数校验失败"},
    500: {"model": ErrorResponse, "description": "服务内部错误"},
    503: {"model": ErrorResponse, "description": "能力未配置或不可用"},
}


def _loopback_binding(host: str | None) -> bool:
    return loopback_binding(host)


def _admin_enabled(config) -> bool:
    """Admin surface exists on loopback, or on any bind when a token is set.

    The check deliberately uses the configured bind host.  ``Host`` and
    ``X-Forwarded-*`` never turn a remote bind into loopback mode.
    """
    return bool(configured_token(ADMIN_TOKEN_ENV)) or _loopback_binding(
        getattr(config, "host", None)
    )


def _same_origin(request: Request) -> bool:
    return origin_allowed(request, allowed_origins_from_env())


def _json_write(request: Request) -> bool:
    content_type = str(request.headers.get("content-type") or "")
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def _require_write(request: Request) -> None:
    if not _same_origin(request):
        _error(403, "origin_rejected", "写入请求 Origin 不在允许列表")
    if not _json_write(request):
        _error(415, "json_required", "写入请求必须使用 application/json")


def _admin_has_valid_token(request: Request) -> bool:
    expected = configured_token(ADMIN_TOKEN_ENV)
    if not expected:
        return False
    return constant_time_equals(
        bearer_token(request.headers.get("authorization")), expected
    )


def _access_has_valid_token(request: Request) -> bool:
    expected = configured_token(ACCESS_TOKEN_ENV)
    if not expected:
        return True
    return constant_time_equals(access_token_from_request(request), expected)


def _auth_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _map_handoff_exception(exc: Exception) -> None:
    if isinstance(exc, HandoffNotFound):
        _error(404, "handoff_not_found", str(exc))
    if isinstance(exc, HandoffConflict):
        _error(409, "handoff_conflict", str(exc))
    if isinstance(exc, HandoffCorrupt):
        _error(500, "handoff_corrupt", str(exc))
    if isinstance(exc, SessionNotFound):
        _error(404, "session_not_found", str(exc))
    if isinstance(exc, SessionEnded):
        _error(409, "session_ended", str(exc))
    _error(500, "handoff_error", str(exc))


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
    # Public health is an intentionally safe summary: no filesystem paths, no
    # credential names/values and no raw config issues (they can carry paths).
    return {
        "status": status,
        "ready": ready,
        "identity": _identity(config),
        "required_assets": {name: bool(value) for name, value in required.items()},
        "needs_configuration": sorted(set(needs_configuration)),
        "model": {
            "configured": bool(credentials),
            "root_present": model_root.is_dir(),
        },
        "missing": sorted(set(missing)),
        "deployment": {
            "single_tenant": True,
            "admin_auth": admin_auth_mode(),
            "user_auth": access_auth_mode(),
            "static_ui": static_hosting_enabled(),
        },
    }


def _liveness(config) -> dict:
    return {
        "status": "alive",
        "ready": False,
        "instance_id": config.instance_id,
        "pid": os.getpid(),
    }


def _readiness(config) -> dict:
    return _health(config)


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

    app = FastAPI(
        title=f"{config.name} · 单租户产品接口",
        description=(
            "同一套接口同时注册在 /api 与 /api/v1 下，保留既有兼容路径。"
            "公开 health/identity 只返回安全摘要；会话与人工管理接口按部署环境"
            "要求 Bearer token。适用于单租户部署，不提供多用户隔离。"
        ),
        version="1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=[
            {"name": "健康与身份", "description": "公开的安全摘要与存活/就绪检查"},
            {"name": "会话", "description": "会话创建、消息 SSE、停止/重开/结束"},
            {"name": "用户转人工", "description": "用户侧申请人工与轮询人工回复"},
            {"name": "人工后台", "description": "单租户人工坐席管理接口（需管理 token）"},
            {"name": "文件", "description": "会话工作区文件上传、列表与下载"},
        ],
        lifespan=lifespan,
    )
    app.state.instance_config = config
    app.state.store = store
    app.state.manager = manager
    handoff = HumanHandoffStore(store)
    app.state.handoff = handoff

    allowed_origins = allowed_origins_from_env()
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-RVS-Request-Id",
                "X-RVS-Turn-Id",
            ],
            expose_headers=["X-RVS-Request-Id", "X-RVS-Cold", "X-RVS-Handoff"],
            max_age=600,
        )

    def _is_admin_api(path: str) -> bool:
        return path in {"/api/admin", "/api/v1/admin"} or path.startswith(
            ("/api/admin/", "/api/v1/admin/")
        )

    @app.middleware("http")
    async def security_guard(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        if method == "OPTIONS":
            return await call_next(request)

        is_admin_api = _is_admin_api(path)
        is_admin_page = path in {"/admin", "/admin.html"}
        is_public_api = path in {
            "/api/health",
            "/api/health/live",
            "/api/health/ready",
            "/api/instance",
            "/api/openapi.json",
            "/api/docs",
            "/api/docs/oauth2-redirect",
            "/api/redoc",
            "/api/v1/health",
            "/api/v1/health/live",
            "/api/v1/health/ready",
            "/api/v1/instance",
        }
        is_api = path.startswith("/api/")

        if is_admin_page:
            if not _admin_enabled(config):
                return _auth_error(
                    403,
                    "admin_local_only",
                    "管理后台仅在本机 loopback 绑定时可用；远程部署请配置 RVS_ADMIN_TOKEN",
                )
            return await call_next(request)

        if is_admin_api:
            if not _admin_enabled(config):
                return _auth_error(
                    403,
                    "admin_local_only",
                    "管理后台仅在本机 loopback 绑定时可用；远程部署请配置 RVS_ADMIN_TOKEN",
                )
            if configured_token(ADMIN_TOKEN_ENV) and not _admin_has_valid_token(request):
                return _auth_error(
                    401,
                    "admin_unauthorized",
                    "需要有效的人工后台 Bearer token",
                )
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                if not _same_origin(request):
                    return _auth_error(403, "origin_rejected", "管理写入 Origin 不在允许列表")
                if not _json_write(request):
                    return _auth_error(415, "json_required", "管理写入必须使用 application/json")
            return await call_next(request)

        if is_api and not is_public_api:
            if not _access_has_valid_token(request):
                return _auth_error(
                    401,
                    "access_unauthorized",
                    "需要有效的访问 Bearer token（或同名 Cookie）",
                )
            if method in {"POST", "PUT", "PATCH", "DELETE"} and not _same_origin(request):
                return _auth_error(403, "origin_rejected", "写入请求 Origin 不在允许列表")
        return await call_next(request)

    def dual(
        path: str,
        methods: list[str],
        *,
        tags: list[str] | None = None,
        summary: str | None = None,
        response_model: type[BaseModel] | None = None,
        responses: dict[int | str, dict[str, Any]] | None = None,
        status_code: int = 200,
    ):
        def decorate(func):
            for prefix in ("/api", "/api/v1"):
                version = "v1" if prefix == "/api/v1" else "v0"
                for method in methods:
                    route_kwargs: dict[str, Any] = {
                        "methods": [method],
                        "tags": tags,
                        "summary": summary,
                        "status_code": status_code,
                        "responses": responses or API_ERROR_RESPONSES,
                        "operation_id": f"{func.__name__}_{version}_{method.lower()}",
                    }
                    if response_model is not None:
                        route_kwargs["response_model"] = response_model
                    app.api_route(prefix + path, **route_kwargs)(func)
            return func

        return decorate

    # ------------------------------------------------------------ identity
    @dual(
        "/instance",
        ["GET"],
        tags=["健康与身份"],
        summary="公开实例身份与就绪摘要",
        response_model=InstanceInfo,
        responses={200: {"model": InstanceInfo}},
    )
    def instance_info():
        health = _health(config)
        return {
            **_identity(config),
            "instance_id": config.instance_id,
            "artifact_id": config.artifact_id,
            "scenario": config.scenario,
            "version": config.version,
            "workflows": list(config.workflows),
            "default_workflow": config.default_workflow,
            "api_version": "v1",
            # Frontend is served by the same origin (nginx) in a separated
            # deployment; never advertise an internal bind address here.
            "frontend_url": "/",
            "status": health["status"],
            "auth": {
                "single_tenant": True,
                "admin": admin_auth_mode(),
                "user": access_auth_mode(),
            },
            "readiness": health,
        }

    @dual(
        "/health",
        ["GET"],
        tags=["健康与身份"],
        summary="健康/就绪摘要（公开，安全字段）",
        response_model=HealthInfo,
        responses={200: {"model": HealthInfo}},
    )
    def health():
        return _health(config)

    @dual(
        "/health/live",
        ["GET"],
        tags=["健康与身份"],
        summary="Liveness：进程存活",
        response_model=HealthInfo,
        responses={200: {"model": HealthInfo}},
    )
    def health_live():
        return _liveness(config)

    @dual(
        "/health/ready",
        ["GET"],
        tags=["健康与身份"],
        summary="Readiness：模型/资产/前端是否就绪",
        response_model=HealthInfo,
        responses={
            200: {"model": HealthInfo, "description": "已就绪"},
            503: {"model": HealthInfo, "description": "降级或缺少配置，可启动但不可完整问答"},
        },
    )
    def health_ready():
        payload = _readiness(config)
        return JSONResponse(
            status_code=200 if payload.get("ready") else 503,
            content=payload,
        )

    # ------------------------------------------------------------ sessions
    @dual(
        "/sessions",
        ["GET"],
        tags=["会话"],
        summary="列出会话（单租户共享访问）",
        response_model=SessionListResponse,
    )
    def list_sessions():
        return {"sessions": store.list()}

    @dual(
        "/sessions",
        ["POST"],
        tags=["会话"],
        summary="创建会话",
        response_model=CreatedSession,
    )
    def create_session(body: CreateSession | None = None):
        workflow = (body.workflow if body else None) or config.default_workflow
        workflows = list(config.workflows)
        if workflow and workflows and workflow not in workflows:
            _error(422, "unknown_workflow", f"未知工作流：{workflow}")
        metadata = store.create(workflow=workflow)
        return {**metadata, "workspace": str(store.workspace_path(metadata["session_id"]))}

    @dual(
        "/sessions/{session_id}",
        ["GET"],
        tags=["会话"],
        summary="读取会话、转录与售后摘要",
        response_model=SessionDetail,
    )
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
            "handoff": handoff.public_status(session_id),
        }
        if SUPPORT_ENABLED:
            state = support_load_state(store.load_support_state(session_id))
            payload["support"] = support_public_view(state)
        return payload

    @dual(
        "/sessions/{session_id}/support",
        ["GET"],
        tags=["会话"],
        summary="读取会话售后状态（只读投影）",
    )
    def get_support(session_id: str):
        try:
            store.get(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        if not SUPPORT_ENABLED:
            return {"enabled": False, "support": None}
        state = support_load_state(store.load_support_state(session_id))
        return {"enabled": True, "support": support_public_view(state)}

    @dual(
        "/sessions/{session_id}/handoff-summary",
        ["GET", "POST"],
        tags=["用户转人工"],
        summary="只读转人工摘要（GET/POST 等价，不生成外部工单）",
    )
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

    # ------------------------------------------------ human handoff (user side)
    def _handoff_sse(ticket: dict, message: dict):
        def events():
            yield "data: " + json.dumps(
                {
                    "type": "handoff",
                    "ticket": ticket_view(ticket),
                    "message": message,
                },
                ensure_ascii=False,
                default=str,
            ) + "\n\n"
            yield "data: " + json.dumps({"type": "done"}, ensure_ascii=False) + "\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-RVS-Handoff": "1"},
        )

    @dual(
        "/sessions/{session_id}/handoff",
        ["GET"],
        tags=["用户转人工"],
        summary="读取当前人工工单状态",
        response_model=HandoffStatus,
    )
    def get_handoff(session_id: str):
        try:
            store.get(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        try:
            return handoff.public_status(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_handoff_exception(exc)

    @dual(
        "/sessions/{session_id}/handoff/request",
        ["POST"],
        tags=["用户转人工"],
        summary="申请人工（幂等；机器人忙时 409）",
        response_model=HandoffRequestResult,
    )
    def request_handoff(
        session_id: str,
        request: Request,
        body: HandoffRequestBody | None = None,
    ):
        """Idempotently put this session in the human queue.

        The request is serialised with the chat entry point by the same
        per-session gate; an in-flight agent run returns 409 instead of being
        silently raced.  A closed ticket may be followed by a fresh request.
        """
        _require_write(request)
        try:
            store.require_active(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        try:
            with handoff.gate(session_id):
                session = manager.peek(session_id)
                busy = bool(
                    session is not None
                    and (
                        session.closing
                        or session.running
                        or session.pending_thread_id
                        or session.busy.locked()
                    )
                )
                if busy:
                    _error(409, "session_busy", "机器人正在处理当前消息，请稍后再申请人工")
                result = handoff.request(
                    session_id,
                    request_id=(body.request_id if body else None),
                )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _map_handoff_exception(exc)
        return {
            "ok": True,
            "ticket": ticket_view(result["ticket"]),
            "created": result["created"],
            "duplicate": result["duplicate"],
        }

    @dual(
        "/sessions/{session_id}/handoff/messages",
        ["GET"],
        tags=["用户转人工"],
        summary="轮询人工客服回复（after 游标）",
    )
    def handoff_messages(session_id: str, after: int = 0):
        try:
            store.get(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        try:
            payload = handoff.human_messages(session_id, after)
        except Exception as exc:  # noqa: BLE001
            _map_handoff_exception(exc)
        return payload

    # ------------------------------------------------ local single-agent admin
    def _admin_ticket_for(session_id: str) -> dict:
        try:
            ticket = handoff.current(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_handoff_exception(exc)
        if ticket is None:
            _error(404, "handoff_not_found", "该会话没有人工工单")
        return ticket

    def _handoff_snapshot(session_id: str, transcript: list[dict]) -> dict | None:
        if not SUPPORT_ENABLED:
            return None
        state = support_load_state(store.load_support_state(session_id))
        return support_handoff_summary(state, session_id, transcript)

    def _admin_row(metadata: dict) -> dict | None:
        session_id = str(metadata.get("session_id") or "")
        try:
            ticket = handoff.current(session_id)
        except HandoffCorrupt:
            return {
                "session_id": session_id,
                "session_status": metadata.get("status"),
                "workflow": metadata.get("workflow"),
                "ticket": None,
                "ticket_status": "corrupt",
                "updated_at": metadata.get("updated_at"),
                "last_user_excerpt": "",
                "handoff_excerpt": "人工工单文件损坏，需人工检查",
                "queued_count": 0,
            }
        if ticket is None:
            return None
        transcript = store.transcript(session_id)
        users = [item for item in transcript if str(item.get("role")) == "user"]
        last_user = users[-1] if users else {}
        snapshot = _handoff_snapshot(session_id, transcript)
        queued = queued_for_admin(ticket)
        return {
            "session_id": session_id,
            "session_status": metadata.get("status"),
            "workflow": metadata.get("workflow"),
            "ticket": admin_ticket_view(ticket),
            "ticket_id": ticket.get("ticket_id"),
            "ticket_status": ticket.get("status"),
            "updated_at": ticket.get("updated_at") or metadata.get("updated_at"),
            "last_user_excerpt": str(last_user.get("content") or "")[:300],
            "last_user_at": last_user.get("at"),
            "handoff_excerpt": (snapshot or {}).get("summary", "")[:700],
            "queued_count": sum(1 for item in queued if not item.get("handled")),
        }

    @dual(
        "/admin/handoff",
        ["GET"],
        tags=["人工后台"],
        summary="人工工单列表（需 RVS_ADMIN_TOKEN，未配置时仅 loopback）",
        response_model=AdminListResult,
    )
    def admin_list_handoff(status: str = "all"):
        if status not in {"all", "waiting", "active", "closed"}:
            _error(422, "invalid_status", "status 只能是 all/waiting/active/closed")
        rows = []
        for metadata in store.list():
            row = _admin_row(metadata)
            if row is None:
                continue
            if status != "all" and row.get("ticket_status") != status:
                continue
            rows.append(row)
        rows.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
        counts = {"waiting": 0, "active": 0, "closed": 0}
        for metadata in store.list():
            try:
                ticket = handoff.current(str(metadata.get("session_id") or ""))
            except HandoffCorrupt:
                continue
            if ticket and ticket.get("status") in counts:
                counts[ticket["status"]] += 1
        return {
            "rows": rows[:200],
            "counts": counts,
            "filter": status,
            "admin": {
                "mode": "local_single_agent",
                "operator": DEFAULT_OPERATOR,
                "auth": admin_auth_mode(),
                "single_tenant": True,
            },
        }

    @dual(
        "/admin/handoff/{session_id}",
        ["GET"],
        tags=["人工后台"],
        summary="人工工单详情与会话转录",
        response_model=AdminDetailResult,
    )
    def admin_get_handoff(session_id: str):
        try:
            metadata = store.get(session_id)
            transcript = store.transcript(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        ticket = _admin_ticket_for(session_id)
        return {
            "ticket": admin_ticket_view(ticket),
            "session": {**metadata, "session_id": session_id},
            "transcript": transcript,
            "handoff": _handoff_snapshot(session_id, transcript),
            "queued_messages": queued_for_admin(ticket),
            "admin": {
                "mode": "local_single_agent",
                "operator": DEFAULT_OPERATOR,
                "auth": admin_auth_mode(),
                "single_tenant": True,
            },
        }

    @dual(
        "/admin/handoff/{session_id}/accept",
        ["POST"],
        tags=["人工后台"],
        summary="接手工单",
        response_model=AdminActionResult,
    )
    def admin_accept_handoff(session_id: str):
        try:
            store.get(session_id)
            result = handoff.accept(session_id)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _map_handoff_exception(exc)
        return {
            "ok": True,
            "ticket": admin_ticket_view(result["ticket"]),
            "changed": result["changed"],
        }

    @dual(
        "/admin/handoff/{session_id}/reply",
        ["POST"],
        tags=["人工后台"],
        summary="人工回复（request_id 幂等）",
        response_model=AdminActionResult,
    )
    def admin_reply_handoff(session_id: str, body: AdminReplyBody):
        try:
            store.get(session_id)
            result = handoff.reply(
                session_id,
                body.message,
                request_id=body.request_id,
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _map_handoff_exception(exc)
        return {
            "ok": True,
            "ticket": admin_ticket_view(result["ticket"]),
            "message": result["message"],
            "duplicate": result["duplicate"],
        }

    @dual(
        "/admin/handoff/{session_id}/close",
        ["POST"],
        tags=["人工后台"],
        summary="结案并恢复机器人（request_id 幂等）",
        response_model=AdminActionResult,
    )
    def admin_close_handoff(session_id: str, body: AdminCloseBody | None = None):
        try:
            store.get(session_id)
            result = handoff.close(
                session_id,
                note=(body.note if body else ""),
                request_id=(body.request_id if body else None),
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _map_handoff_exception(exc)
        return {
            "ok": True,
            "ticket": admin_ticket_view(result["ticket"]),
            "changed": result["changed"],
            "duplicate": result["duplicate"],
        }

    @dual(
        "/sessions/{session_id}",
        ["DELETE"],
        tags=["会话"],
        summary="结束会话（保留历史与工单）",
        response_model=SimpleOk,
    )
    def end_session(session_id: str):
        try:
            manager.end(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        return {"ok": True, "status": "ended", "session_id": session_id}

    @dual(
        "/sessions/{session_id}/stop",
        ["POST"],
        tags=["会话"],
        summary="停止当前运行",
        response_model=SimpleOk,
    )
    def stop_session(session_id: str):
        try:
            manager.stop(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        return {"ok": True, "status": "active", "session_id": session_id}

    @dual(
        "/sessions/{session_id}/reopen",
        ["POST"],
        tags=["会话"],
        summary="重新打开已结束会话",
        response_model=SimpleOk,
    )
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
                        session_id,
                        "user",
                        run_prompt,
                        turn=next_turn,
                        author={"kind": "user", "display_name": "用户"},
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
                                author={"kind": "assistant", "display_name": "机器人助手"},
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
                            author={"kind": "assistant", "display_name": "机器人助手"},
                        )
                else:
                    assistant_record = store.append_message(
                        session_id,
                        "assistant",
                        answer,
                        interrupted=interrupted,
                        thread_id=result.get("thread_id"),
                        turn=(support_state or {}).get("turn"),
                        author={"kind": "assistant", "display_name": "机器人助手"},
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
                # Provider/model text is reduced to a safe, typed message; never
                # echo request headers or credential fragments into SSE/logs.
                error_message = (
                    str(exc)
                    if isinstance(exc, ApprovalGone)
                    else _failure_message(exc)
                )
                error_code = (
                    "approval_gone"
                    if isinstance(exc, ApprovalGone)
                    else _failure_code(exc)
                )
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
                    store.append_message(session_id, "error", error_message)
                except Exception:
                    pass
                try:
                    # Session metadata is the product's run record: an
                    # explicitly failed turn must not leave last_status=completed.
                    store.update(session_id, last_status="error")
                except Exception:
                    pass
                updates.put(
                    {
                        "type": "error",
                        "error": error_message,
                        "code": error_code,
                        "retryable": _failure_retryable(exc),
                        "requires_configuration": _failure_requires_configuration(exc),
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

    def _handoff_or_reserve(session_id: str, prompt: str):
        """Atomically choose human queue vs. agent run for one user message.

        The gate is shared with ``request_handoff``.  It is held only while the
        routing decision and reservation happen, never for the whole LLM run.
        """
        try:
            with handoff.gate(session_id):
                ticket = handoff.current(session_id)
                if ticket and ticket.get("status") in {"waiting", "active"}:
                    routed = handoff.queue_user_message(session_id, prompt)
                    if routed is not None:
                        return "handoff", routed
                return "run", _reserve(session_id)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _map_handoff_exception(exc)

    @dual(
        "/sessions/{session_id}/messages",
        ["POST"],
        tags=["会话"],
        summary="发送消息，返回 SSE 事件流（保持兼容）",
        responses={
            200: {
                "description": "text/event-stream：answer_delta / answer / handoff / interrupt / error / done",
                "content": {"text/event-stream": {}},
            },
            **API_ERROR_RESPONSES,
        },
    )
    def post_message(session_id: str, body: MessageRequest, request: Request):
        if latency is None:
            kind, value = _handoff_or_reserve(session_id, body.prompt)
            if kind == "handoff":
                return _handoff_sse(value["ticket"], value["message"])
            return _sse(session_id, {"prompt": body.prompt}, "run", value)
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
                kind, value = _handoff_or_reserve(session_id, body.prompt)
            if kind == "handoff":
                return _handoff_sse(value["ticket"], value["message"])
            request_span = latency.open_span("server.request", cold=cold)
            return _sse(
                session_id,
                {"prompt": body.prompt},
                "run",
                value,
                latency_ctx=ctx,
                request_span=request_span,
            )

    @dual(
        "/sessions/{session_id}/resume",
        ["POST"],
        tags=["会话"],
        summary="恢复审批中断，返回 SSE 事件流",
        responses={
            200: {
                "description": "text/event-stream：answer / interrupt / error / done",
                "content": {"text/event-stream": {}},
            },
            **API_ERROR_RESPONSES,
        },
    )
    def resume_message(session_id: str, body: ResumeRequest):
        reservation = _reserve(session_id, resume=True)
        return _sse(
            session_id,
            {"thread_id": body.thread_id, "decision": body.decision},
            "resume",
            reservation,
        )

    # ------------------------------------------------------------ files
    @dual(
        "/sessions/{session_id}/files",
        ["GET"],
        tags=["文件"],
        summary="列出会话工作区文件",
        response_model=FileListResult,
    )
    def list_files(session_id: str):
        try:
            files = store.workspace_files(session_id)
        except Exception as exc:  # noqa: BLE001
            _map_exception(exc)
        return {"files": files, "workspace": str(store.workspace_path(session_id))}

    @dual(
        "/sessions/{session_id}/files",
        ["POST"],
        tags=["文件"],
        summary="上传文件（multipart，最大 10 MB）",
        response_model=UploadResult,
    )
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
    # A separated docker deployment sets RVS_SERVE_STATIC=false and nginx
    # serves product_ui.  The default keeps the historical same-process UI.
    if static_hosting_enabled():

        @app.get("/admin")
        @app.get("/admin.html", include_in_schema=False)
        def admin_page():
            root = _ui_root(config)
            target = root / "admin.html"
            if not target.is_file():
                _error(404, "admin_missing", "后台页面缺失")
            return FileResponse(target, headers={"Cache-Control": "no-store"})

        @app.get("/", include_in_schema=False)
        def index():
            root = _ui_root(config)
            target = root / "index.html"
            if not target.is_file():
                _error(503, "frontend_missing", "实例前端缺失")
            return FileResponse(target, headers={"Cache-Control": "no-store"})

        @app.get("/{path:path}", include_in_schema=False)
        def static_file(path: str):
            root = _ui_root(config).resolve()
            target = (root / path).resolve()
            # Only /admin and /admin.html may serve the admin page; a path
            # variant must not bypass the admin guard.
            if target.name.lower() == "admin.html":
                _error(404, "not_found", "页面不存在")
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

    # The session store, manager and handoff file locks are process-local.
    # Keep exactly one worker so two processes cannot diverge on session state.
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        workers=1,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
