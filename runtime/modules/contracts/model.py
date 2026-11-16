# -*- coding: utf-8 -*-
"""模型端口：``ModelPort`` / ``ModelReply``（框架无关）。

为什么单独一层：现在"调一次模型"散落在 spec 块、RAG 服务、judge、agent_eval 里，
每处都直接摸 LangChain 的 message 形状。换框架时这些调用点要逐个改；而且
"文本 / tool_calls / usage / 结构化输出"没有统一形状，评测层只能猜。

``ModelPort`` 只描述"给一组消息，返回一个 reply"；LangChain 只是它的 adapter。

本模块**不 import langchain**；adapter 在 ``core/llm/adapters/langchain_chat.py``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ModelMessage:
    """端口层的消息形状；``role`` 用 human / ai / system / tool。"""

    role: str
    content: str
    name: str = ""
    tool_call_id: str = ""


@dataclass
class ModelReply:
    """一次模型调用的结果。"""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    structured: Any = None
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": list(self.tool_calls),
            "usage": dict(self.usage),
            "structured": self.structured,
        }


@runtime_checkable
class ModelPort(Protocol):
    def invoke(
        self,
        messages: list[Any],
        *,
        schema: Any = None,
    ) -> ModelReply: ...


def as_port(model: Any) -> ModelPort:
    """把当前运行时模型包成 ``ModelPort``；已是端口则原样返回。

    不用 ``isinstance(..., ModelPort)`` 判断：LangChain 模型也有 ``invoke``，
    会被 runtime_checkable Protocol 误认成端口。这里用"有没有 bind"区分
    （chat model 支持 bind，端口不需要）。
    """
    if hasattr(model, "invoke") and not hasattr(model, "bind"):
        return model
    from core.llm.adapters.langchain_chat import LangChainModelPort

    return LangChainModelPort(model)


def port_for(scenario: Any) -> ModelPort | None:
    """从 Scenario / ExecutionSpec / ToolView 取模型端口。

    优先 ``execution.model_port``；否则把 ``execution.model`` 包一层；
    再退回旧的 ``scenario.model``（图谱里的 ToolView 仍用这个字段）。
    """
    resolve = getattr(scenario, "resolve_execution", None)
    execution = resolve() if callable(resolve) else None
    port = getattr(execution, "model_port", None) if execution is not None else None
    if port is not None:
        return as_port(port)
    model = getattr(execution, "model", None) if execution is not None else None
    if model is None:
        model = getattr(scenario, "model", None)
    return as_port(model) if model is not None else None
