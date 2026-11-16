# -*- coding: utf-8 -*-
"""LangChain 模型 <-> ``ModelPort`` 的 adapter。"""
from __future__ import annotations

from typing import Any

from core.llm.port import ModelReply


def _text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = []
    for call in getattr(message, "tool_calls", None) or []:
        if isinstance(call, dict):
            calls.append({"name": call.get("name", ""), "args": call.get("args", {})})
        else:
            calls.append({"name": getattr(call, "name", ""), "args": getattr(call, "args", {})})
    return calls


def _usage(message: Any) -> dict[str, Any]:
    usage = getattr(message, "usage_metadata", None)
    return dict(usage) if isinstance(usage, dict) else {}


class LangChainModelPort:
    """把当前运行时的 chat model 包成 ``ModelPort``。"""

    def __init__(self, model: Any):
        self.model = model

    def stream(self, messages: list[Any]):
        """逐块产出文本；模型不支持 stream 时自行抛错，由调用方回退 invoke。"""
        for chunk in self.model.stream(messages):
            text = _text(chunk)
            if text:
                yield text

    def invoke(self, messages: list[Any], *, schema: Any = None) -> ModelReply:
        if schema is not None:
            structured = self.model.with_structured_output(schema)
            reply = structured.invoke(messages)
            return ModelReply(
                text=_text(reply) if not isinstance(reply, dict) else "",
                structured=reply,
                raw=reply,
            )
        reply = self.model.invoke(messages)
        return ModelReply(
            text=_text(reply),
            tool_calls=_tool_calls(reply),
            usage=_usage(reply),
            raw=reply,
        )
