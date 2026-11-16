# -*- coding: utf-8 -*-
"""通用 subagent 描述。

仓库不再内置场景专属预设；subagent 的默认能力由
``core.runtime.config.SubagentConfig`` 提供，场景可在 spec 里按需声明。
"""
from __future__ import annotations

from core.agent.subagent import SubagentPreset


def presets_for(name: str) -> tuple[SubagentPreset, ...]:
    """没有场景专属预设；保留函数签名给编辑器只读查询。"""
    return ()


def describe(name: str) -> list[dict]:
    return [
        {
            "tool_name": item.tool_name,
            "description": item.description,
            "allowed_tools": list(item.allowed_tools),
            "max_model_calls": item.max_model_calls,
            "token_budget": item.token_budget,
            "timeout_seconds": item.timeout_seconds,
        }
        for item in presets_for(name)
    ]
