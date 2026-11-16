# -*- coding: utf-8 -*-
"""统一 context MCP server：scene / library 的宽度与深度感受野。

启动：

    python -m core.mcp.context_server

工具：

- ``context(scope, target, width=0, depth=0)``：读目标及其感受野；
- ``find(scope, query, kind="", scenario="")``：只做发现，不展开。

所有工具返回 JSON 字符串；错误返回 ``{"ok": false, "code": ..., "error": ...}``。
``context`` 和 ``find`` 都是只读的，不会写 spec、不会调用模型。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import json
from mcp.server.fastmcp import FastMCP

from modules.workspace.service import ContextService

mcp = FastMCP("context")


@lru_cache(maxsize=1)
def _service() -> ContextService:
    return ContextService()


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(exc: BaseException, *, code: str = "error", **extra: Any) -> str:
    payload = {
        "ok": False,
        "code": getattr(exc, "code", code),
        "error": f"{type(exc).__name__}: {exc}",
    }
    payload.update(getattr(exc, "extra", {}) or {})
    payload.update(extra)
    return _dumps(payload)


@mcp.tool()
def context(
    scope: str,
    target: str,
    width: int = 0,
    depth: int = 0,
    scenario: str = "",
    selection: str = "",
    max_items: int = 40,
    max_chars: int = 16000,
) -> str:
    """读取目标的感受野。scope=scene|library；width/depth 0..3。

    width=0 只返回 focus；depth=0 只返回目标表层。scene 的 target 形如
    ``scene://<scenario>/node/<node>``；library 的 target 形如
    ``ah://tool/search_notes``。target=selection 时需显式给 selection URI。
    """
    try:
        return _dumps(_service().context(
            scope,
            target,
            width=width,
            depth=depth,
            scenario=scenario or None,
            selection=selection or None,
            max_items=max_items,
            max_chars=max_chars,
        ))
    except Exception as exc:  # noqa: BLE001 - MCP 工具返回结构化错误
        return _error(exc)


@mcp.tool()
def find(
    scope: str,
    query: str,
    kind: str = "",
    scenario: str = "",
    limit: int = 10,
) -> str:
    """发现候选：scene 需要 scenario；library 可选 kind。

    返回的是候选摘要，不带感受野；选中候选后再调 ``context``。
    """
    try:
        return _dumps(_service().find(
            scope,
            query,
            kind=kind,
            scenario=scenario or None,
            limit=limit,
        ))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


if __name__ == "__main__":
    mcp.run(transport="stdio")
