# -*- coding: utf-8 -*-
"""中间件 decision 的轻量观测 sink。

只在仓库自有中间件真正做出决定时调用；不代理 LangChain pipeline，也不根据
"中间件已装配"推断"已触发"。root tracer 放在 ContextVar 里，子 Agent 用
``copy_context().run`` 启动线程时自然继承；没有 root tracer 时直接返回 False，
由调用方保持未观测，而不是编造事件。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from core.observability.budget import context_scope, current_budget

_root_tracer = ContextVar("observability_root_tracer", default=None)


def current_root_tracer():
    return _root_tracer.get()


@contextmanager
def root_tracer_scope(tracer):
    """绑定当前 run 的 root tracer；嵌套 run 不覆盖已有 root。"""
    if _root_tracer.get() is not None:
        yield _root_tracer.get()
        return
    token = _root_tracer.set(tracer)
    try:
        yield tracer
    finally:
        _root_tracer.reset(token)


def record_middleware_decision(middleware: str, decision: str, **fields: Any) -> bool:
    """把真实 decision 写到 root tracer；未绑定或写入失败时明确返回 False。"""
    tracer = _root_tracer.get()
    if tracer is None:
        return False
    budget = current_budget() or getattr(tracer, "budget", None)
    scope = context_scope(budget)
    try:
        tracer.record_decision(middleware=middleware, decision=decision, **scope, **fields)
    except Exception:
        return False
    return True
