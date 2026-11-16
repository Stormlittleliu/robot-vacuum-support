# -*- coding: utf-8 -*-
"""结构化输出 schema 注册表：spec 里只写名字，不写代码。

仓库默认不注册任何业务 schema；需要时在这里登记通用 schema，或由调用方
给 ``build_from_spec`` 直接传 ``response_format``。
"""
from __future__ import annotations

from typing import Any

SCHEMAS: dict[str, Any] = {}


def names() -> list[str]:
    return sorted(SCHEMAS)


def resolve(name: str | None) -> Any:
    """按名字取 schema；没有就返回 None（当成"不用结构化输出"）。"""
    if not name:
        return None
    return SCHEMAS.get(str(name))
