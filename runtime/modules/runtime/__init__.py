# -*- coding: utf-8 -*-
"""运行时：LangChain / LangGraph 适配面。

只做公开 API 的惰性导出，避免 ``spec_check -> policy_meta -> runtime`` 这类
只读校验路径被编译器/framework 的导入环拖住。
"""
from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "CompiledSpec": "modules.runtime.compiler",
    "CompiledWorkflow": "modules.runtime.compiler",
    "FrameworkAgent": "modules.runtime.framework",
    "WorkflowRuntime": "modules.runtime.workflow",
    "build_pipeline": "modules.runtime.pipeline",
    "compile_preview": "modules.runtime.compiler",
    "compile_spec": "modules.runtime.compiler",
}

_SUBMODULES = {
    "checkpoint",
    "compiler",
    "framework",
    "hooks",
    "middleware",
    "pipeline",
    "policy",
    "policy_meta",
    "subagent",
    "workflow",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _SUBMODULES:
        module = importlib.import_module(f"modules.runtime.{name}")
        globals()[name] = module
        return module
    target = _EXPORTS.get(name)
    if target:
        value = getattr(importlib.import_module(target), name)
        globals()[name] = value
        return value
    raise AttributeError(name)
