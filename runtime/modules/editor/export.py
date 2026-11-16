# -*- coding: utf-8 -*-
"""把 active spec 导出成可直接运行的 agent bundle。

实现已迁移到 :mod:`modules.artifacts.builder`；本模块保留原函数签名
``export_bundle(name, *, root=EXPORT_ROOT) -> {bundle, zip, manifest}`` 与
``EXPORT_ROOT`` 常量，避免 CLI / 旧调用方依赖运行中的编辑器。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT, SPEC_ROOT
from modules.artifacts.builder import build_artifact
from modules.editor.ownership import OwnershipError, assert_spec_owned

EXPORT_ROOT = PROJECT_ROOT / "exports"


def export_bundle(name: str, *, root: str | Path = EXPORT_ROOT) -> dict[str, Any]:
    """构建 schema v2 artifact；源 spec 只读，产物 id 唯一且不覆盖。"""
    try:
        assert_spec_owned(name, root=SPEC_ROOT)
    except OwnershipError as exc:
        raise ValueError("不能从已失效的 spec 导出代码归属场景：" + str(exc)) from exc
    return build_artifact(name, root=root)
