# -*- coding: utf-8 -*-
"""把模型输入消息投影成可追踪的上下文片段。"""
from __future__ import annotations

import hashlib
from typing import Any


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


def segments_from_messages(messages: list[Any] | None) -> list[dict[str, Any]]:
    """只保留可观察摘要，不复制完整上下文正文。"""
    segments: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for message in messages or []:
        role = str(getattr(message, "type", "") or "unknown")
        kind = {
            "system": "system",
            "human": "user",
            "ai": "assistant",
            "tool": "tool",
        }.get(role, role)
        text = _text(getattr(message, "content", ""))
        tool_calls = [
            str((call or {}).get("name") or "")
            for call in (getattr(message, "tool_calls", None) or [])
            if isinstance(call, dict)
        ]
        tool_call_id = str(getattr(message, "tool_call_id", "") or "")
        tool_name = str(getattr(message, "name", "") or "")
        digest = hashlib.sha1(
            f"{kind}|{text}|{tool_call_id}|{','.join(tool_calls)}".encode("utf-8")
        ).hexdigest()[:12]
        base = f"ctx-{digest}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        segment_id = base if count == 0 else f"{base}-{count + 1}"
        segments.append({
            "id": segment_id,
            "kind": kind,
            "role": role,
            "chars": len(text),
            "tokens_est": max(1, (len(text) + 3) // 4),
            "preview": text[:120].replace("\n", " "),
            "tool_calls": [name for name in tool_calls if name],
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
        })
    return segments
