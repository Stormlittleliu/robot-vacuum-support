"""Harness 与场景之间的契约。

`agent/` 只依赖这个文件，不依赖 `scenarios/<name>/` 的任何具体实现；
场景层负责实现/组装它。这样依赖方向是：

    scenarios → agent.contracts ← agent
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExecutionSpec:
    """一次运行需要的"活对象"：模型、输出 schema、工作流工厂、checkpoint。

    与 ``Scenario`` 的结构数据分开，是为了让"这个场景是什么"不必和
    "它跑在哪个框架上"绑死。``Scenario.execution`` 为空时，仍从旧字段推导
    （见 ``Scenario.resolve_execution``），所以代码场景可以渐进迁移。
    """

    model: Any = None
    #: 可选：框架无关的模型端口；只给 model 时由 adapter 自动包一层。
    model_port: Any = None
    middleware: list[Any] = field(default_factory=list)
    response_format: Any = None
    workflow_factory: Any = None
    checkpoint: bool = True
    checkpoint_backend: str = "memory"
    checkpoint_path: str = "data/runtime/agent_checkpoints.sqlite"
    policies: list[Any] | None = None


@dataclass
class Scenario:
    name: str
    description: str
    system_prompt: str = ""
    tools: list[Any] = field(default_factory=list)
    # 场景类型：agent（普通 Agent）/ router（总路由，靠 planning 规则分流）。
    # 只影响 UI 分组与"路由配置"面板，不影响运行时。
    kind: str = "agent"
    context: dict[str, Any] = field(default_factory=dict)
    cases_path: Path | None = None
    # Harness 能力（可选）
    high_risk_tools: dict[str, Any] = field(default_factory=dict)
    fallback_model: Any = None
    enable_todo: bool = True
    enable_summarization: bool = True
    summarization_trigger: tuple = ("messages", 30)
    summarization_keep: tuple = ("messages", 12)
    tool_call_limit: int | None = None
    token_budget: int | None = None
    pii_types: list[str] = field(default_factory=list)
    # 工具权限与超时
    tool_policy: dict[str, Any] = field(default_factory=dict)
    tool_timeout_seconds: float = 30.0
    tool_timeouts: dict[str, float] = field(default_factory=dict)
    # 可选：确定性 planning policy（core.agent.middleware.PlanningPolicy）；
    # 命中规则时在第一次模型调用前注入工具调用，否则 abstain。
    planning_policy: Any = None
    # 可选运行时：hooks / 工作区 / 任务库，由 core.runtime.RuntimeConfig 提供
    runtime: Any = None
    # 活对象集合：模型、中间件、输出 schema、工作流工厂、checkpoint。
    execution: ExecutionSpec | None = None

    def resolve_execution(self) -> ExecutionSpec:
        """返回这次运行的执行面；没有显式 execution 时用一份空默认。"""
        return self.execution if self.execution is not None else ExecutionSpec()

    def get_tool(self, name: str):
        """按名字取场景注册的工具；能力层评测/调试用。"""
        for tool in self.tools:
            if getattr(tool, "name", None) == name:
                return tool
        raise KeyError(f"场景 {self.name} 没有注册工具：{name}")
