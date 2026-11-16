# -*- coding: utf-8 -*-
"""工具契约：``ToolSpec`` / ``ToolResult``（框架无关）。

为什么单独一层：工具现在是一个 LangChain ``@tool`` 对象，同时承担 schema、执行、
artifact、错误四种职责；换框架时改动量与工具数成正比。``ToolSpec`` 只描述"工具是
什么 + 怎么调"，LangChain 只是它的一个 adapter。

边界（第一步刻意很小）：

- 不重写任何工具实现，``as_spec`` 只是包一层；
- 不做 registry / 插件发现；
- ``risk`` / ``timeout`` 只作为声明字段，执行仍由原有中间件负责。

``invoke`` 同时接受两种入参：普通参数 dict，以及 LangChain 的 ToolCall dict
（``{"name","args","id","type":"tool_call"}``）。这样图谱/评测里既有的
``get_tool(name).invoke(tool_call)`` 不需要改写就能兼容 ``ToolSpec``。

本模块**不 import langchain**；adapter 在 ``core/tools/adapters/langchain.py``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
Runner = Callable[[dict[str, Any]], "ToolResult"]


@dataclass(frozen=True)
class ToolResult:
    """一次工具执行的结果；不依赖 ``ToolMessage``。"""

    content: str = ""
    artifact: Any = None
    status: str = "ok"          # ok | error
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "artifact": self.artifact,
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True)
class ToolSpec:
    """工具的框架无关描述；``invoke`` 是唯一执行入口。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: dict(EMPTY_SCHEMA))
    risk: str = "low"
    timeout: float | None = None
    runner: Runner | None = None

    def invoke(self, payload: dict[str, Any] | None = None) -> ToolResult:
        """执行工具；兼容普通参数 dict 与 ToolCall dict。"""
        if self.runner is None:
            raise TypeError(f"工具 {self.name} 没有绑定 runner")
        data = dict(payload or {})
        if data.get("type") == "tool_call" and isinstance(data.get("args"), dict):
            data = dict(data["args"])
        return self.runner(data)

    def __call__(self, args: dict[str, Any] | None = None) -> ToolResult:
        return self.invoke(args)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk": self.risk,
            "timeout": self.timeout,
        }
