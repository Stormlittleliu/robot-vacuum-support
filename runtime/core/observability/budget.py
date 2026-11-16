# -*- coding: utf-8 -*-
"""单次 run 的 token 预算。

trace callback 在所有模型调用（含 RAG 工具内部的总结调用）结束后累加；
TokenBudgetMiddleware 在每次模型调用前检查，超限就结束 run。
"""
from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

_current_budget = ContextVar("agent_run_budget", default=None)
_context_hint = ContextVar("agent_context_hint", default=None)


def current_budget():
    return _current_budget.get()


@contextmanager
def budget_scope(budget):
    token = _current_budget.set(budget)
    try:
        yield budget
    finally:
        _current_budget.reset(token)


@contextmanager
def context_hint(context_label: str | None = None, origin_tool: str | None = None):
    """给随后新建的 RunBudget 打上来源标签。

    subagent 在自己的线程里创建 child budget 时使用；它只影响新 budget 的
    描述字段，不改变 token 统计或父子聚合。
    """
    token = _context_hint.set({"context_label": context_label, "origin_tool": origin_tool})
    try:
        yield
    finally:
        _context_hint.reset(token)


def current_context_hint() -> dict[str, Any]:
    return dict(_context_hint.get() or {})


def context_scope(budget: RunBudget | None) -> dict[str, Any]:
    """把 budget 投影成 trace 可写的上下文身份；拿不到就明确标 unknown。"""
    if budget is None:
        return {
            "context_id": "unknown",
            "parent_context_id": "unknown",
            "context_label": None,
            "origin_tool": None,
        }
    parent = getattr(budget, "parent", None)
    return {
        "context_id": getattr(budget, "context_id", None) or "unknown",
        "parent_context_id": getattr(parent, "context_id", None) or "unknown",
        "context_label": getattr(budget, "context_label", None),
        "origin_tool": getattr(budget, "origin_tool", None),
    }


@dataclass
class RunBudget:
    max_tokens: int | None = None
    total_tokens: int = 0
    parent: RunBudget | None = None
    context_label: str | None = None
    origin_tool: str | None = None
    #: 同一 ledger 在 resume 时复用，所以 id 不随一次新的 invoke 改变。
    context_id: str = field(default_factory=lambda: uuid.uuid4().hex, init=False)
    _events: set[str] = field(default_factory=set, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        # 让 subagent 的 context_hint 只影响随后新建的 ledger，不追溯已有 budget。
        hint = _context_hint.get() or {}
        if self.context_label is None:
            self.context_label = hint.get("context_label")
        if self.origin_tool is None:
            self.origin_tool = hint.get("origin_tool")

    def add(self, tokens: int, event_id: str | None = None) -> None:
        if tokens <= 0:
            return
        with self._lock:
            if event_id is not None:
                if event_id in self._events:
                    return
                self._events.add(event_id)
            self.total_tokens += int(tokens)
        if self.parent is not None:
            self.parent.add(tokens, event_id)

    @property
    def exceeded(self) -> bool:
        return bool(self.max_tokens and self.total_tokens >= self.max_tokens) or bool(self.parent and self.parent.exceeded)

    @property
    def remaining(self) -> int | None:
        own = max(self.max_tokens - self.total_tokens, 0) if self.max_tokens else None
        inherited = self.parent.remaining if self.parent is not None else None
        limits = [value for value in (own, inherited) if value is not None]
        return min(limits) if limits else None
