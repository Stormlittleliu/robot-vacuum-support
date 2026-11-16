# -*- coding: utf-8 -*-
"""边的最小解析：``[from, to]`` / ``[from, to, when]`` / ``{from, to, when}``。

单独一层是为了避免"第二份实现"：``spec_check`` 用它做校验，
``resources.providers`` 用它做只读描述，两边不再各写一份。
"""
from __future__ import annotations

from typing import Any

#: 与 LangGraph 常量同值；放在这里让"只校验结构"的模块不必 import langgraph。
START = "__start__"
END = "__end__"


def split_edge(edge: Any) -> tuple[str, str, str | None]:
    """把一条边拆成 ``(source, target, when)``；非法形状按无条件处理。"""
    if isinstance(edge, dict):
        return str(edge.get("from") or ""), str(edge.get("to") or ""), edge.get("when")
    if isinstance(edge, (list, tuple)) and len(edge) >= 2:
        when = edge[2] if len(edge) >= 3 else None
        return str(edge[0]), str(edge[1]), None if when in (None, "") else str(when)
    return "", "", None


def edge_pair(edge: Any) -> tuple[str, str]:
    source, target, _ = split_edge(edge)
    return source, target
