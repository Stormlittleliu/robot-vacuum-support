# -*- coding: utf-8 -*-
"""兼容 shim：上下文视图纯投影已移至 ``core.observability.context_view``。

旧路径继续导出公共常量与 ``build_context_view``，保证编辑器 API / 既有测试
无需改 import；新代码（尤其 Console）直接使用公共 observability 模块。
JSON 数据形状、观测语义与前端契约均未改变。
"""
from __future__ import annotations

from core.observability.context_view import (
    LEGACY_CONTEXT_ID,
    UNKNOWN_CONTEXT_ID,
    build_context_view,
)

__all__ = ["LEGACY_CONTEXT_ID", "UNKNOWN_CONTEXT_ID", "build_context_view"]
