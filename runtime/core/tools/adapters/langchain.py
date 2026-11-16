# -*- coding: utf-8 -*-
"""LangChain 工具 <-> ``ToolSpec`` 的 adapter。

``as_spec`` 让现有工具获得框架无关的描述与统一返回值；``to_langchain`` 让
``ToolSpec`` 仍然能回到当前运行时。两边都只做形状转换，不复制工具实现。
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from langchain_core.tools import StructuredTool

from core.tools.spec import EMPTY_SCHEMA, ToolResult, ToolSpec


def _schema_from_tool(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return dict(EMPTY_SCHEMA)
    if isinstance(schema, dict):
        return dict(schema)
    model_json_schema = getattr(schema, "model_json_schema", None)
    if callable(model_json_schema):
        try:
            return dict(model_json_schema())
        except Exception:  # noqa: BLE001 - 旧式 schema 可能没有 model_json_schema
            return dict(EMPTY_SCHEMA)
    return dict(EMPTY_SCHEMA)


def _content(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def as_spec(tool: Any, *, risk: str = "low", timeout: float | None = None) -> ToolSpec:
    """把一个工具包成 ``ToolSpec``；已经是 spec 时只覆盖策略字段。"""
    if isinstance(tool, ToolSpec):
        if risk == "low" and timeout is None:
            return tool
        return replace(tool, risk=risk, timeout=timeout)

    name = str(getattr(tool, "name", "") or "")
    description = str(getattr(tool, "description", "") or "")
    if timeout is None:
        timeout = (getattr(tool, "metadata", None) or {}).get("timeout_seconds")

    def _invoke(args: dict[str, Any]) -> ToolResult:
        message = tool.invoke({
            "name": name,
            "args": dict(args or {}),
            "id": f"spec-{uuid.uuid4().hex}",
            "type": "tool_call",
        })
        status = "error" if str(getattr(message, "status", "ok")) == "error" else "ok"
        content = _content(message)
        return ToolResult(
            content=content,
            artifact=getattr(message, "artifact", None),
            status=status,
            error=content if status == "error" else None,
        )

    return ToolSpec(
        name=name,
        description=description,
        input_schema=_schema_from_tool(tool),
        risk=risk,
        timeout=timeout,
        runner=_invoke,
    )


def to_langchain(spec: ToolSpec) -> StructuredTool:
    """把 ``ToolSpec`` 包回 LangChain 工具；schema 原样透传。"""

    def _run(**kwargs: Any) -> str:
        return spec(kwargs).content

    return StructuredTool.from_function(
        func=_run,
        name=spec.name,
        description=spec.description,
        args_schema=spec.input_schema,
    )
