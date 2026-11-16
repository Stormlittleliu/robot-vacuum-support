# -*- coding: utf-8 -*-
"""跨模块契约：Agent、执行面、模型端口、工具端口。

这里是物理实现位置；旧的 ``core.agent.contracts`` / ``core.llm.port`` /
``core.tools.spec`` 是兼容 shim。
"""
from modules.contracts.agent import ExecutionSpec, Scenario
from modules.contracts.model import ModelMessage, ModelPort, ModelReply, as_port, port_for
from modules.contracts.tool import ToolResult, ToolSpec

__all__ = [
    "ExecutionSpec",
    "ModelMessage",
    "ModelPort",
    "ModelReply",
    "Scenario",
    "ToolResult",
    "ToolSpec",
    "as_port",
    "port_for",
]
