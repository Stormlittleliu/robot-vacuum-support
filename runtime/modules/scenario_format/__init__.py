# -*- coding: utf-8 -*-
"""场景格式：spec schema、Issue、块契约、migration。

不包含 LangGraph 执行、编辑器写入或 UI。
"""
from modules.scenario_format.migrations import RENAMES, apply_renames
from modules.scenario_format.spec import (
    NodeSpec,
    ScenarioSpec,
    SpecConflict,
    SpecError,
    WorkflowSpec,
    active_version,
    load_active_spec,
    load_active_versioned,
    spec_from_dict,
    spec_to_dict,
)
from modules.scenario_format.spec_check import Issue, Severity, check_spec
from modules.scenario_format.spec_nodes import (
    ParamSpec,
    PortSpec,
    block_catalog,
    block_desc,
    block_label,
    block_params,
    block_ports,
    registered_nodes,
)

__all__ = [
    "Issue",
    "NodeSpec",
    "ParamSpec",
    "PortSpec",
    "RENAMES",
    "ScenarioSpec",
    "Severity",
    "SpecConflict",
    "SpecError",
    "WorkflowSpec",
    "active_version",
    "apply_renames",
    "block_catalog",
    "block_desc",
    "block_label",
    "block_params",
    "block_ports",
    "check_spec",
    "load_active_spec",
    "load_active_versioned",
    "registered_nodes",
    "spec_from_dict",
    "spec_to_dict",
]
