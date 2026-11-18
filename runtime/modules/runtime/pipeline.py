# -*- coding: utf-8 -*-
"""中间件流水线：声明式装配 + 顺序不变量。

装配逻辑从 ``framework.py`` 的 if 链搬到这里，解决三个问题：

1. **顺序不靠调用方拖拽**：每个策略有固定 slot，编译时统一排序；
2. **可增删可配参**：spec 场景用 ``policies`` 覆盖默认策略，不用改框架代码；
3. **不变量可校验**：违反"权限最外 / 审批在超时外 / error 在 retry 外"直接报错。

默认策略从 ``Scenario`` 的现有字段推导，所以代码场景的行为与重构前完全一致；
``policies`` 只是叠加在上面的覆盖层（默认开启、显式移除）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
    PIIMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)

from modules.runtime import policy as policy_registry
from modules.runtime.hooks import make_hook_middleware
from modules.runtime.middleware import (
    PlanningPolicyMiddleware,
    TokenBudgetMiddleware,
    ToolPermissionMiddleware,
    ToolTimeoutMiddleware,
)
from modules.runtime.policy_meta import (
    INVARIANTS,
    LAYERS,
    POLICY_DOCS,
    SLOTS,
    PolicyDoc,
    policy_names,
)

# slot / 不变量 / 分层 / 中文说明都在 ``policy_meta``（纯数据，不 import langchain），
# 这里只保留策略构建器与装配逻辑。


class PipelineError(ValueError):
    """策略配置不合法。"""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("；".join(self.errors))


@dataclass(frozen=True)
class PolicySpec:
    name: str
    enabled: bool = True
    args: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    #: 该值来自哪一层：harness_default / scenario_policy / node_override。
    source: str = "scenario_policy"
    #: harness 默认是否启用；用于 effective policy 审计，不改变装配。
    default: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "scenario_policy") -> "PolicySpec":
        return cls(
            name=str(data.get("name") or ""),
            enabled=bool(data.get("enabled", True)),
            args=dict(data.get("args") or {}),
            reason=data.get("reason"),
            source=str(data.get("source") or source),
            default=bool(data.get("default", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "args": self.args,
            "reason": self.reason,
            "source": self.source,
            "default": self.default,
        }


@dataclass(frozen=True)
class Entry:
    name: str  # 展示名，如 permission / pii:email / custom:MonitorToolMiddleware
    policy: str  # 注册名，如 permission / pii / custom
    slot: int
    seq: int
    factory: Callable[[], Any]


def _on_tool_error(exc: Exception, request) -> str:
    """把工具执行异常转成中文 error ToolMessage，让 Agent 有机会换一种方式继续。"""
    tool_name = getattr(request, "tool_call", {}).get("name", "unknown")
    return (
        f"[工具执行异常] 工具 {tool_name} 执行失败（{type(exc).__name__}）。"
        "请不要重复同样的调用，检查参数或改用其他工具。"
    )


# ---------------------------------------------------------------- 策略构建器
#
# 名字 / slot / 中文说明 / 构建器现在统一登记在 ``modules.runtime.policy``；
# 这里只定义"怎么把 Policy 变成 LangChain 中间件"。

register_policy = policy_registry.register_builder


@register_policy("permission")
def _build_permission(scenario: Any, args: dict[str, Any]) -> Any:
    return ToolPermissionMiddleware(scenario.tool_policy)


@register_policy("hooks")
def _build_hooks(scenario: Any, args: dict[str, Any]) -> Any:
    return make_hook_middleware(args["manager"])


@register_policy("planning")
def _build_planning(scenario: Any, args: dict[str, Any]) -> Any:
    return PlanningPolicyMiddleware(scenario.planning_policy)


@register_policy("todo")
def _build_todo(scenario: Any, args: dict[str, Any]) -> Any:
    return TodoListMiddleware()


@register_policy("summarization")
def _build_summarization(scenario: Any, args: dict[str, Any]) -> Any:
    return SummarizationMiddleware(
        model=args.get("model") or scenario.model,
        trigger=scenario.summarization_trigger,
        keep=scenario.summarization_keep,
    )


@register_policy("pii")
def _build_pii(scenario: Any, args: dict[str, Any]) -> Any:
    return PIIMiddleware(
        args["type"],
        strategy="redact",
        apply_to_input=True,
        apply_to_output=False,
        apply_to_tool_results=False,
    )


@register_policy("hitl")
def _build_hitl(scenario: Any, args: dict[str, Any]) -> Any:
    return HumanInTheLoopMiddleware(interrupt_on=scenario.high_risk_tools)


@register_policy("timeout")
def _build_timeout(scenario: Any, args: dict[str, Any]) -> Any:
    timeouts = {}
    for tool in scenario.tools:
        timeout = getattr(tool, "timeout", None)
        if timeout is None:
            timeout = (getattr(tool, "metadata", None) or {}).get("timeout_seconds")
        if timeout is not None:
            timeouts[tool.name] = timeout
    timeouts.update(scenario.tool_timeouts)
    return ToolTimeoutMiddleware(
        default_timeout=scenario.tool_timeout_seconds,
        overrides=timeouts,
    )


@register_policy("error")
def _build_error(scenario: Any, args: dict[str, Any]) -> Any:
    return ToolErrorMiddleware(on_error=_on_tool_error)


@register_policy("retry")
def _build_retry(scenario: Any, args: dict[str, Any]) -> Any:
    return ToolRetryMiddleware(max_retries=1, on_failure="error")


@register_policy("limit")
def _build_limit(scenario: Any, args: dict[str, Any]) -> Any:
    run_limit = args.get("run_limit") or scenario.tool_call_limit
    return ToolCallLimitMiddleware(run_limit=run_limit, exit_behavior="end")


@register_policy("budget")
def _build_budget(scenario: Any, args: dict[str, Any]) -> Any:
    return TokenBudgetMiddleware()


@register_policy("fallback")
def _build_fallback(scenario: Any, args: dict[str, Any]) -> Any:
    return ModelFallbackMiddleware(scenario.fallback_model)


@register_policy("model_retry")
def _build_model_retry(scenario: Any, args: dict[str, Any]) -> Any:
    return ModelRetryMiddleware(max_retries=1)


# ---------------------------------------------------------------- 装配


def available_policies() -> list[str]:
    return policy_registry.names()


#: Harness 默认层。这是唯一默认值定义；场景字段与 spec.policies 只能覆盖它，
#: 真实 workflow 节点只能在最上层覆盖。`default` 是审计用的 harness 默认值，
#: 不随场景变化。
_HARNESS_DEFAULTS: dict[str, dict[str, Any]] = {
    "permission": {"enabled": True},
    "planning": {"enabled": False},
    "todo": {"enabled": False},
    "summarization": {"enabled": False},
    "pii": {"enabled": False},
    "hitl": {"enabled": False},
    "timeout": {"enabled": True},
    "error": {"enabled": True},
    "retry": {"enabled": True},
    "limit": {"enabled": False},
    "budget": {"enabled": False},
    "fallback": {"enabled": False},
    "model_retry": {"enabled": True},
    "hooks": {"enabled": False},
}


def _harness_default_specs() -> list[PolicySpec]:
    return [
        PolicySpec(
            name=name,
            enabled=bool(config.get("enabled")),
            args={},
            source="harness_default",
            default=bool(config.get("enabled")),
        )
        for name, config in _HARNESS_DEFAULTS.items()
    ]


def _apply_layer(
    base: list[PolicySpec],
    overrides: list[Any] | None,
    *,
    source: str,
) -> list[PolicySpec]:
    """按层覆盖；同一个 name 后写覆盖前写，args 浅合并。"""
    merged = {spec.name: spec for spec in base}
    for raw in overrides or []:
        spec = raw if isinstance(raw, PolicySpec) else PolicySpec.from_dict(raw, source=source)
        if not spec.name:
            raise PipelineError(["policies 里有一项缺少 name"])
        if policy_registry.find(spec.name) is None:
            raise PipelineError([
                f"未知策略：{spec.name}（可用：{', '.join(available_policies())}）"
            ])
        current = merged.get(spec.name)
        if current is None:
            raise PipelineError([f"未知策略：{spec.name}"])
        merged[spec.name] = PolicySpec(
            name=spec.name,
            enabled=spec.enabled,
            args={**current.args, **spec.args},
            reason=spec.reason,
            source=source,
            default=current.default,
        )
    return [merged[name] for name in _HARNESS_DEFAULTS if name in merged]


def _scenario_layer(
    scenario: Any,
    hook_manager: Any = None,
    execution: Any = None,
) -> list[PolicySpec]:
    """场景字段 + spec.policies 的覆盖层。

    普通 Agent 不再有节点覆盖；workflow 节点的显式覆盖由 ``node_overrides``
    在更高一层应用。这里始终是 scenario_policy。
    """
    specs: list[PolicySpec] = []
    if getattr(scenario, "planning_policy", None) is not None:
        specs.append(PolicySpec("planning", enabled=True))
    if getattr(scenario, "enable_todo", False):
        specs.append(PolicySpec("todo", enabled=True))
    if getattr(scenario, "enable_summarization", False):
        model = getattr(execution, "model", None) if execution is not None else None
        specs.append(
            PolicySpec(
                "summarization",
                enabled=True,
                args={"model": model} if model is not None else {},
            )
        )
    pii_types = list(getattr(scenario, "pii_types", None) or [])
    if pii_types:
        specs.append(PolicySpec("pii", enabled=True, args={"types": pii_types}))
    if getattr(scenario, "high_risk_tools", None):
        specs.append(PolicySpec("hitl", enabled=True))
    limit = getattr(scenario, "tool_call_limit", None)
    if limit:
        specs.append(PolicySpec("limit", enabled=True, args={"run_limit": limit}))
    from core.observability.budget import current_budget

    inherited = current_budget()
    if getattr(scenario, "token_budget", None) or (
        inherited is not None and inherited.remaining is not None
    ):
        specs.append(PolicySpec("budget", enabled=True))
    if getattr(scenario, "fallback_model", None) is not None:
        specs.append(PolicySpec("fallback", enabled=True))
    if hook_manager is not None and hook_manager.has_hooks():
        specs.append(PolicySpec("hooks", enabled=True, args={"manager": hook_manager}))
    # spec.policies 是场景级显式覆盖，不是节点覆盖。
    specs.extend(
        PolicySpec.from_dict(raw, source="scenario_policy")
        if not isinstance(raw, PolicySpec)
        else PolicySpec(
            name=raw.name,
            enabled=raw.enabled,
            args=dict(raw.args),
            reason=raw.reason,
            source="scenario_policy",
            default=raw.default,
        )
        for raw in (getattr(execution, "policies", None) or [])
    )
    return specs


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return f"<{type(value).__name__}>"


def _policy_rows(specs: list[PolicySpec]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in specs:
        policy = policy_registry.find(spec.name)
        rows.append(
            {
                "name": spec.name,
                "enabled": bool(spec.enabled),
                "value": bool(spec.enabled),
                "source": spec.source,
                "default": bool(spec.default),
                "args": _jsonable(dict(spec.args)),
                "slot": policy.slot if policy is not None else None,
                "label": policy.label if policy is not None else spec.name,
            }
        )
    return rows


def _resolved_specs(
    scenario: Any,
    hook_manager: Any = None,
    overrides: list[Any] | None = None,
    execution: Any = None,
    *,
    node_overrides: list[Any] | None = None,
) -> list[PolicySpec]:
    """唯一的策略解析入口：默认表 + 场景层 + 显式 overrides + 节点覆盖。"""
    if execution is None:
        resolve = getattr(scenario, "resolve_execution", None)
        execution = resolve() if callable(resolve) else None
    if overrides is None and execution is not None:
        overrides = getattr(execution, "policies", None)
    specs = default_policies(scenario, hook_manager, execution)
    specs = _merge(specs, overrides, source="scenario_policy")
    if node_overrides:
        specs = _merge(specs, node_overrides, source="node_override")
    _validate_specs(specs)
    return specs


def effective_policies(
    scenario: Any,
    hook_manager: Any = None,
    execution: Any = None,
    *,
    node_overrides: list[Any] | None = None,
    overrides: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """返回 effective policy（值 / 来源 / harness 默认），不构建中间件。

    优先级：节点显式覆盖（仅真实 workflow 节点）> 场景 policy
    （Scenario 字段 / spec.harness / spec.policies）> harness 默认。
    与 ``build_pipeline`` 共用 ``_resolved_specs``，不是第二套默认表。
    """
    return _policy_rows(
        _resolved_specs(
            scenario,
            hook_manager,
            overrides,
            execution,
            node_overrides=node_overrides,
        )
    )


def default_policies(
    scenario: Any,
    hook_manager: Any = None,
    execution: Any = None,
) -> list[PolicySpec]:
    """兼容入口：返回场景层合并后的 specs（不含节点覆盖）。"""
    if execution is None:
        resolve = getattr(scenario, "resolve_execution", None)
        execution = resolve() if callable(resolve) else None
    return _apply_layer(
        _harness_default_specs(),
        _scenario_layer(scenario, hook_manager, execution),
        source="scenario_policy",
    )


def _merge(
    base: list[PolicySpec],
    overrides: list[Any] | None,
    *,
    source: str = "scenario_policy",
) -> list[PolicySpec]:
    return _apply_layer(base, overrides, source=source)


def _validate_specs(specs: list[PolicySpec]) -> None:
    errors = [
        f"策略 {spec.name} 被关闭但没有写 reason：移除一层守卫必须可审计"
        for spec in specs
        if not spec.enabled
        and spec.source != "harness_default"
        and not (spec.reason or "").strip()
    ]
    if errors:
        raise PipelineError(errors)


def _entries_for_specs(
    specs: list[PolicySpec],
    scenario: Any,
    execution: Any = None,
) -> list[Entry]:
    entries: list[Entry] = []
    seq = 0
    for spec in specs:
        if not spec.enabled:
            continue
        if spec.name == "pii":
            for pii_type in spec.args.get("types") or ():
                entries.append(Entry(
                    name=f"pii:{pii_type}",
                    policy="pii",
                    slot=SLOTS["pii"],
                    seq=seq,
                    factory=partial(
                        policy_registry.get("pii").build, scenario, {"type": pii_type}
                    ),
                ))
                seq += 1
            continue
        entries.append(Entry(
            name=spec.name,
            policy=spec.name,
            slot=SLOTS[spec.name],
            seq=seq,
            factory=partial(
                policy_registry.get(spec.name).build, scenario, dict(spec.args)
            ),
        ))
        seq += 1

    custom = list(getattr(execution, "middleware", None) or [])
    for middleware in custom:
        entries.append(Entry(
            name=f"custom:{type(middleware).__name__}",
            policy="custom",
            slot=SLOTS["custom"],
            seq=seq,
            factory=partial(_keep, middleware),
        ))
        seq += 1

    entries.sort(key=lambda entry: (entry.slot, entry.seq))
    _validate_order(entries)
    return entries


def _entries(
    scenario: Any,
    hook_manager: Any = None,
    overrides: list[Any] | None = None,
    execution: Any = None,
    *,
    node_overrides: list[Any] | None = None,
) -> list[Entry]:
    specs = _resolved_specs(
        scenario,
        hook_manager,
        overrides,
        execution,
        node_overrides=node_overrides,
    )
    return _entries_for_specs(specs, scenario, execution)


def _keep(middleware: Any) -> Any:
    return middleware


def _validate_order(entries: list[Entry]) -> None:
    position: dict[str, int] = {}
    for index, entry in enumerate(entries):
        position.setdefault(entry.policy, index)
    errors: list[str] = []
    for outer, inner, why in INVARIANTS:
        if outer in position and inner in position and position[outer] > position[inner]:
            errors.append(f"策略顺序违反不变量：{outer} 必须在 {inner} 之前（{why}）")
    if errors:
        raise PipelineError(errors)


def build_pipeline(
    scenario: Any,
    hook_manager: Any = None,
    overrides: list[Any] | None = None,
    execution: Any = None,
    *,
    node_overrides: list[Any] | None = None,
) -> list[Any]:
    """编译出最终的中间件列表（由外到内）。"""
    return [
        entry.factory()
        for entry in _entries(
            scenario, hook_manager, overrides, execution, node_overrides=node_overrides
        )
    ]


def build_pipeline_with_policies(
    scenario: Any,
    hook_manager: Any = None,
    overrides: list[Any] | None = None,
    execution: Any = None,
    *,
    node_overrides: list[Any] | None = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """一次解析同时返回真实中间件和它们的 effective policy。

    ``FrameworkAgent`` 用这个入口，保证审计行与真正装配的中间件来自同一份
    resolved specs，而不是先建 pipeline、再从默认表重算一遍。
    """
    specs = _resolved_specs(
        scenario,
        hook_manager,
        overrides,
        execution,
        node_overrides=node_overrides,
    )
    entries = _entries_for_specs(specs, scenario, execution)
    return [entry.factory() for entry in entries], _policy_rows(specs)


def describe_pipeline(
    scenario: Any,
    hook_manager: Any = None,
    overrides: list[Any] | None = None,
    execution: Any = None,
    *,
    node_overrides: list[Any] | None = None,
) -> list[str]:
    """返回装配后的策略名（由外到内），用于测试、UI 与排查。"""
    return [
        entry.name
        for entry in _entries(
            scenario, hook_manager, overrides, execution, node_overrides=node_overrides
        )
    ]


def describe_chain(
    scenario: Any,
    hook_manager: Any = None,
    overrides: list[Any] | None = None,
    execution: Any = None,
    *,
    node_overrides: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """返回装配后的策略明细（由外到内）：中文名、说明、分层、slot。

    与 ``build_pipeline`` 共用同一套 ``_entries``，所以下标与真正的中间件
    列表一一对应；UI 用它给每个实例配上中文说明和悬停提示。
    """
    chain: list[dict[str, Any]] = []
    for order, entry in enumerate(
        _entries(
            scenario, hook_manager, overrides, execution, node_overrides=node_overrides
        ),
        1,
    ):
        doc = POLICY_DOCS.get(entry.policy)
        label = doc.label if doc else entry.name
        desc = doc.desc if doc else ""
        layer = doc.layer if doc else "自定义"
        if entry.policy == "pii":
            pii_type = entry.name.split(":", 1)[-1]
            label = f"敏感信息脱敏 · {pii_type}"
            desc = f"输入里的 {pii_type} 在送模型前替换成占位符"
        if entry.policy == "custom":
            # 自定义中间件的说明由它自己的模块提供（UI 按类名查），这里只给分层。
            label = entry.name.split(":", 1)[-1]
            desc = ""
        chain.append({
            "order": order,
            "name": entry.name,
            "policy": entry.policy,
            "slot": entry.slot,
            "label": label,
            "desc": desc,
            "layer": layer,
        })
    return chain
