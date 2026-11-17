# -*- coding: utf-8 -*-
"""Structured model/turn failure classification.

The product must not turn a provider failure into an HTTP-200 empty answer with a
completed run.  This module is the single classification point used by the
ReAct node, trace writer, worker and instance server:

* provider authentication / permission errors -> ``model_auth_failed``
* rate limits, timeouts and provider outages -> explicit transient codes
* other execution failures -> ``run_error``

Only safe, user-actionable text is exposed.  Raw provider messages (which may
contain key fragments or request ids) never become the SSE/error-record text.
"""
from __future__ import annotations

import re
from typing import Any

from core.observability.redact import redact_text

AUTH_MESSAGE = (
    "模型凭据无效或未获授权。请检查实例 environment_file 中的模型 API Key"
    "（DEEPSEEK_API_KEY），更新后重试本轮；本轮不会记为完成。"
)
RATE_LIMIT_MESSAGE = "模型服务暂时限流，请稍后重试；本轮不会记为完成。"
TIMEOUT_MESSAGE = "模型请求超时，请稍后重试；本轮不会记为完成。"
UNAVAILABLE_MESSAGE = "模型服务暂时不可用，请稍后重试；本轮不会记为完成。"
MODEL_MESSAGE = "模型调用失败，请稍后重试；若持续失败，请检查实例模型配置。"
EMPTY_ANSWER_MESSAGE = "模型没有返回可用回答，请重试；若持续失败，请检查模型配置。"
CITATION_MESSAGE = "模型回答缺少可核验的来源引用，本轮未完成；请重试。"
RUN_MESSAGE = "本轮执行失败，请重试；若持续失败，请检查服务日志。"


class TurnFailure(RuntimeError):
    """A safe, typed failure that can cross node -> worker -> SSE boundaries."""

    default_code = "run_error"
    default_message = RUN_MESSAGE

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        *,
        retryable: bool = True,
        requires_configuration: bool = False,
        http_status: int | None = None,
    ):
        self.code = str(code or self.default_code)
        self.user_message = str(message or self.default_message)
        self.retryable = bool(retryable)
        self.requires_configuration = bool(requires_configuration)
        self.http_status = http_status
        super().__init__(self.user_message)


class ModelFailure(TurnFailure):
    default_code = "model_error"
    default_message = MODEL_MESSAGE


class ModelOutputFailure(TurnFailure):
    default_code = "model_empty_answer"
    default_message = EMPTY_ANSWER_MESSAGE


class AnswerValidationFailure(TurnFailure):
    default_code = "answer_missing_citation"
    default_message = CITATION_MESSAGE


def _chain(exc: BaseException):
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "http_status", "status"):
        value = getattr(exc, attr, None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    try:
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        pass
    match = re.search(r"(?:error code|http|status)\D{0,8}(\d{3})", str(exc), re.I)
    return int(match.group(1)) if match else None


def _looks_like_model_error(exc: BaseException) -> bool:
    for item in _chain(exc):
        name = type(item).__name__.lower()
        text = str(item).lower()
        if _status_code(item) is not None:
            return True
        if any(
            marker in name
            for marker in (
                "openai",
                "langchain",
                "authentication",
                "permission",
                "ratelimit",
                "rate_limit",
                "timeout",
                "connection",
                "apiconnection",
                "provider",
                "model",
            )
        ):
            return True
        if any(
            marker in text
            for marker in (
                "error code:",
                "api key",
                "authentication",
                "unauthorized",
                "rate limit",
                "openai",
                "deepseek",
                "model",
                "timed out",
                "timeout",
                "connection",
            )
        ):
            return True
    return False


def _auth_error(exc: BaseException) -> bool:
    for item in _chain(exc):
        status = _status_code(item)
        name = type(item).__name__.lower()
        text = str(item).lower()
        if status in {401, 403}:
            return True
        if any(
            marker in name
            for marker in (
                "authentication",
                "permissiondenied",
                "permission_denied",
                "unauthorized",
            )
        ):
            return True
        if any(
            marker in text
            for marker in (
                "authentication fails",
                "invalid api key",
                "incorrect api key",
                "unauthorized",
                "permission denied",
                "invalid_api_key",
            )
        ):
            return True
    return False


def classify_model_exception(exc: BaseException) -> ModelFailure:
    """Return a typed, safe model failure; never expose the raw provider text."""
    if isinstance(exc, ModelFailure):
        return exc
    status = None
    for item in _chain(exc):
        status = status or _status_code(item)
    text = " ".join(str(item) for item in _chain(exc)).lower()
    if _auth_error(exc):
        return ModelFailure(
            "model_auth_failed",
            AUTH_MESSAGE,
            retryable=False,
            requires_configuration=True,
            http_status=status or 401,
        )
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return ModelFailure(
            "model_rate_limited",
            RATE_LIMIT_MESSAGE,
            retryable=True,
            http_status=status or 429,
        )
    if status == 408 or "timed out" in text or "timeout" in text:
        return ModelFailure(
            "model_timeout",
            TIMEOUT_MESSAGE,
            retryable=True,
            http_status=status or 408,
        )
    if (status is not None and 500 <= status < 600) or any(
        marker in text
        for marker in ("connection error", "apiconnectionerror", "service unavailable", "bad gateway")
    ):
        return ModelFailure(
            "model_unavailable",
            UNAVAILABLE_MESSAGE,
            retryable=True,
            http_status=status,
        )
    return ModelFailure(
        "model_error",
        MODEL_MESSAGE,
        retryable=True,
        http_status=status,
    )


def as_turn_failure(exc: BaseException) -> TurnFailure:
    """Classify an exception raised inside a turn into a safe failure object."""
    if isinstance(exc, TurnFailure):
        return exc
    if _looks_like_model_error(exc):
        return classify_model_exception(exc)
    return TurnFailure("run_error", RUN_MESSAGE, retryable=True)


def safe_exception_summary(exc: BaseException) -> str:
    """Short log/trace summary without raw provider text or headers."""
    if isinstance(exc, TurnFailure):
        suffix = f" (HTTP {exc.http_status})" if exc.http_status else ""
        return f"{type(exc).__name__}: {exc.code}{suffix}"
    if _looks_like_model_error(exc):
        failure = classify_model_exception(exc)
        return safe_exception_summary(failure)
    text = redact_text(str(exc)).strip()
    return f"{type(exc).__name__}: {text[:160]}" if text else type(exc).__name__


def safe_error_message(exc: BaseException) -> str:
    """User-facing message; never includes raw provider details."""
    if isinstance(exc, TurnFailure):
        return exc.user_message
    if _looks_like_model_error(exc):
        return classify_model_exception(exc).user_message
    return RUN_MESSAGE


def failure_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or ("run_error" if not _looks_like_model_error(exc) else "model_error"))


def failure_retryable(exc: BaseException) -> bool:
    return bool(getattr(exc, "retryable", True))


def failure_requires_configuration(exc: BaseException) -> bool:
    return bool(getattr(exc, "requires_configuration", False))


def provider_error_summary(exc: BaseException) -> str:
    """Trace-level provider summary: code and status only, never the raw body."""
    return safe_exception_summary(exc)
