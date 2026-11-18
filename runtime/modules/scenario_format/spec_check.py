# -*- coding: utf-8 -*-
"""结构规则的唯一出口：spec / workflow / 端口 / 图 的静态校验。

为什么单独一层：规则要被三种消费者共用——构建（`build_workflows`）、发布
（`entry.validate`）、编辑器（`draft_issues` / 画布标红）。它们需要的不是同一句话，
而是同一套判定：`Issue(code, severity, node_id, loc, message)`。

边界（刻意的）：

- 只做静态校验：不执行节点、不加载知识库、不构建 Agent；
- **不 import `core.scenarios.spec`**（`spec.py` 反向 import 本模块）。需要
  `ScenarioSpec`/`WorkflowSpec` 类型时走 `TYPE_CHECKING`；
- `split_edge` / `edge_pair` 定义在这里，`spec.py` 只做 re-export，避免循环依赖；
- 规则文案与迁移前逐字一致（页面、测试、历史报告都依赖这些句子），`code` 是新增维度。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Iterable

from core.agent.policy_meta import policy_names
from core.prompts.blocks import PromptBook, validate_book
from modules.scenario_format.edges import END, START, edge_pair, split_edge
from modules.scenario_format.modes import VALID_SCENE_MODES
from modules.scenario_format.spec_nodes import (
    NODE_REGISTRY,
    ResolvedPorts,
    block_ports,
    branch_route_values,
    contract_fingerprint,
    contract_shape,
    types_compatible,
    validate_args,
    validate_tool_refs,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from core.scenarios.spec import ScenarioSpec, WorkflowSpec

RESERVED_ARGS = {"scenario", "state", "config", "runtime"}

#: 可读的最低 schema 版本；比它低说明文件太旧。
MIN_SCHEMA_VERSION = 1
#: 当前写出的 schema 版本。2 = 节点带块契约指纹。
SPEC_VERSION = 2


class Severity(str, Enum):
    """问题级别。P0 只有 ERROR；WARNING 由 advisory 通道单独消费。"""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    """一条可展示、可定位、可被外部 agent 消费的校验问题。"""

    code: str
    message: str
    severity: Severity = Severity.ERROR
    node_id: str | None = None
    workflow: str | None = None
    loc: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "node_id": self.node_id,
            "workflow": self.workflow,
            "loc": self.loc,
            "hint": self.hint,
        }


def _walk(seed: str, adjacency: dict[str, set[str]]) -> set[str]:
    seen = {seed}
    stack = [seed]
    while stack:
        current = stack.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def check_port_flow(
    prefix: str,
    nodes: list[Any],
    edges: list[Any],
    *,
    resolve: Callable[[str, dict[str, Any]], ResolvedPorts] = block_ports,
    seed: dict[str, str] | None = None,
) -> list[Issue]:
    """按节点端口做数据流校验：下游要的输入，上游到底产出了没有、类型对不对。

    静态检查：只看声明和拓扑，不执行节点。从 START 的种子（``input:text``）出发，
    求每个节点"执行前 state 里有哪些槽位"的不动点；循环图也能收敛。
    只校验 ``required=True`` 的输入；可选输入缺失不算错。
    """
    if not nodes:
        return []
    seed_types = dict(seed or {"input": "text"})
    by_id = {node.id: node for node in nodes}
    predecessors: dict[str, list[str]] = {node.id: [] for node in nodes}
    for edge in edges:
        source, target, _ = split_edge(edge)
        if source in by_id and target in by_id:
            predecessors[target].append(source)

    after: dict[str, dict[str, str]] = {node.id: {} for node in nodes}
    for _ in range(len(nodes) + 2):
        changed = False
        for node in nodes:
            before = dict(seed_types)
            for source in predecessors.get(node.id, ()):
                before.update(after[source])
            result = dict(before)
            for port in resolve(node.fn, node.args).outputs:
                result[port.name] = port.type
            if result != after[node.id]:
                after[node.id] = result
                changed = True
        if not changed:
            break

    issues: list[Issue] = []
    for node in nodes:
        before = dict(seed_types)
        for source in predecessors.get(node.id, ()):
            before.update(after[source])
        for port in resolve(node.fn, node.args).inputs:
            if port.name in before:
                if not types_compatible(before[port.name], port.type):
                    issues.append(Issue(
                        code="port.type_mismatch",
                        message=(
                            f"{prefix}.{node.id}：输入 {port.name} 需要 {port.type}，"
                            f"上游给的是 {before[port.name]}"
                        ),
                        node_id=node.id,
                        loc=f"{node.id}.inputs.{port.name}",
                    ))
            elif port.required:
                issues.append(Issue(
                    code="port.missing_input",
                    message=(
                        f"{prefix}.{node.id}：缺少输入 {port.name}"
                        f"（{node.fn} 需要，上游节点没有产出）"
                    ),
                    node_id=node.id,
                    loc=f"{node.id}.inputs.{port.name}",
                ))
    return issues


def check_workflow(
    name: str,
    workflow: "WorkflowSpec",
    tool_names: set[str] | None = None,
) -> list[Issue]:
    """校验一张工作流：节点、边、branch、可达性、端口。"""
    issues: list[Issue] = []
    prefix = f"workflow[{name}]"
    node_ids = [node.id for node in workflow.nodes]
    if not node_ids:
        return [Issue("workflow.empty", f"{prefix} 没有节点", workflow=name)]

    index = {node.id: position for position, node in enumerate(workflow.nodes)}
    if len(set(node_ids)) != len(node_ids):
        duplicates = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
        issues.extend(
            Issue(
                "node.duplicate_id",
                f"{prefix} 节点 id 重复：{node_id}",
                node_id=node_id,
                workflow=name,
                loc=f"workflows.{name}.nodes[{index.get(node_id, 0)}]",
            )
            for node_id in duplicates
        )

    for position, node in enumerate(workflow.nodes):
        loc = f"workflows.{name}.nodes[{position}]"
        if not node.id.strip():
            issues.append(Issue(
                "node.empty_id", f"{prefix} 存在空节点 id", workflow=name, loc=loc
            ))
        if node.id in {START, END}:
            issues.append(Issue(
                "node.reserved_id",
                f"{prefix}.{node.id} 使用了保留节点 id（START/END 由编译器占用）",
                node_id=node.id,
                workflow=name,
                loc=loc,
            ))
        if node.fn not in NODE_REGISTRY:
            issues.append(Issue(
                "node.unregistered_block",
                f"{prefix}.{node.id} 引用了未注册的块：{node.fn}",
                node_id=node.id,
                workflow=name,
                loc=loc,
            ))
        elif (
            node.contract
            and node.contract_shape == contract_shape(node.fn)
            and node.contract != contract_fingerprint(node.fn)
        ):
            # 骨架一致但指纹对不上 = 手改过指纹（真正的契约变化由 check_drift 判断）
            issues.append(Issue(
                "node.contract_forged",
                f"{prefix}.{node.id} 的块契约指纹与当前实现不一致（手改或旧版本）",
                node_id=node.id,
                workflow=name,
                loc=loc,
                hint="重新保存该 spec 会按当前块契约重写指纹",
            ))
        reserved = sorted(RESERVED_ARGS & set(node.args))
        if reserved:
            issues.append(Issue(
                "node.reserved_arg",
                f"{prefix}.{node.id} 的参数名被保留：{', '.join(reserved)}",
                node_id=node.id,
                workflow=name,
                loc=loc,
            ))
        for error in validate_args(node.fn, node.args):
            issues.append(Issue(
                "node.arg_invalid",
                f"{prefix}.{node.id}：{error}",
                node_id=node.id,
                workflow=name,
                loc=loc,
            ))
        if tool_names is not None:
            for error in validate_tool_refs(node.fn, node.args, tool_names):
                issues.append(Issue(
                    "node.tool_ref_invalid",
                    f"{prefix}.{node.id}：{error}",
                    node_id=node.id,
                    workflow=name,
                    loc=loc,
                ))

    known = set(node_ids) | {START, END}
    adjacency: dict[str, set[str]] = {}
    reverse: dict[str, set[str]] = {}
    conditional_sources: set[str] = set()
    plain_sources: set[str] = set()
    conditional_labels: dict[str, set[str]] = {}
    for position, edge in enumerate(workflow.edges):
        source, target, when = split_edge(edge)
        loc = f"workflows.{name}.edges[{position}]"
        if source not in known:
            issues.append(Issue(
                "edge.source_missing", f"{prefix} 边起点不存在：{source}", workflow=name, loc=loc
            ))
            continue
        if target not in known:
            issues.append(Issue(
                "edge.target_missing", f"{prefix} 边终点不存在：{target}", workflow=name, loc=loc
            ))
            continue
        if when is not None:
            if not str(when).strip():
                issues.append(Issue(
                    "edge.empty_condition",
                    f"{prefix}.{source} 存在空的条件标签",
                    node_id=source if source in index else None,
                    workflow=name,
                    loc=loc,
                ))
            conditional_sources.add(source)
            conditional_labels.setdefault(source, set()).add(str(when))
        else:
            plain_sources.add(source)
        adjacency.setdefault(source, set()).add(target)
        reverse.setdefault(target, set()).add(source)

    for source in sorted(conditional_sources & plain_sources):
        issues.append(Issue(
            "edge.mixed_condition",
            f"{prefix}.{source} 同时有条件和无条件出边：一个节点要么固定去一处，要么按分支走",
            node_id=source if source in index else None,
            workflow=name,
            loc=f"workflows.{name}.nodes[{index.get(source, 0)}]",
        ))

    for node in workflow.nodes:
        if node.fn != "branch":
            continue
        routes, route_errors = branch_route_values(node.args)
        for error in route_errors:
            issues.append(Issue(
                "branch.route_invalid",
                f"{prefix}.{node.id}：{error}",
                node_id=node.id,
                workflow=name,
                loc=f"workflows.{name}.nodes[{index.get(node.id, 0)}]",
            ))
        labels = conditional_labels.get(node.id, set())
        if not labels:
            issues.append(Issue(
                "branch.no_conditional_edge",
                f"{prefix}.{node.id} 是 branch，但没有条件出边",
                node_id=node.id,
                workflow=name,
                loc=f"workflows.{name}.nodes[{index.get(node.id, 0)}]",
            ))
            continue
        missing = sorted(routes - labels)
        if missing:
            issues.append(Issue(
                "branch.route_uncovered",
                f"{prefix}.{node.id} 可能写出 route={missing}，但没有对应条件边",
                node_id=node.id,
                workflow=name,
                loc=f"workflows.{name}.nodes[{index.get(node.id, 0)}]",
            ))
        extra = sorted(labels - routes)
        if extra:
            issues.append(Issue(
                "branch.label_extra",
                f"{prefix}.{node.id} 的条件边标签 {extra} 不是 branch 的输出",
                node_id=node.id,
                workflow=name,
                loc=f"workflows.{name}.nodes[{index.get(node.id, 0)}]",
            ))

    if not adjacency.get(START):
        issues.append(Issue("graph.no_start_edge", f"{prefix} 缺少从 START 出发的边", workflow=name))
    if not reverse.get(END):
        issues.append(Issue("graph.no_end_edge", f"{prefix} 缺少指向 END 的边", workflow=name))

    reachable = _walk(START, adjacency)
    for node_id in node_ids:
        if node_id not in reachable:
            issues.append(Issue(
                "graph.unreachable",
                f"{prefix}.{node_id} 从 START 不可达",
                node_id=node_id,
                workflow=name,
                loc=f"workflows.{name}.nodes[{index.get(node_id, 0)}]",
            ))
    finishing = _walk(END, reverse)
    for node_id in node_ids:
        if node_id not in finishing:
            issues.append(Issue(
                "graph.dead_end",
                f"{prefix}.{node_id} 走不到 END",
                node_id=node_id,
                workflow=name,
                loc=f"workflows.{name}.nodes[{index.get(node_id, 0)}]",
            ))
    issues.extend(check_port_flow(prefix, workflow.nodes, workflow.edges))
    return issues


def _compare_contract(old: dict, new: dict) -> str:
    """比较两份契约骨架：``ok`` / ``safe`` / ``breaking``。"""
    changed = False
    old_p = {row[0]: row for row in old.get("p", [])}
    new_p = {row[0]: row for row in new.get("p", [])}
    for name in old_p:
        if name not in new_p:  # 参数消失：旧 spec 的 args 会变非法
            return "breaking"
    for name, row in new_p.items():
        previous = old_p.get(name)
        if previous is None:
            if row[2] and not row[3]:
                return "breaking"
            changed = True
            continue
        if row[1] != previous[1]:
            return "breaking"

    old_i = {row[0]: row for row in old.get("i", [])}
    new_i = {row[0]: row for row in new.get("i", [])}
    for name in old_i:
        if name not in new_i:
            return "breaking"
    for name, row in new_i.items():
        if name not in old_i:
            if row[2]:
                return "breaking"
            changed = True

    old_o = {row[0]: row for row in old.get("o", [])}
    new_o = {row[0]: row for row in new.get("o", [])}
    if set(old_o) != set(new_o):
        return "breaking"
    for name, row in new_o.items():
        previous = old_o[name]
        if row[1] == previous[1]:
            continue
        if row[1] == "any" and previous[1] != "any":  # 变宽
            changed = True
            continue
        return "breaking"  # 变窄 / 换类型
    return "safe" if changed else "ok"


def check_drift(spec) -> list[Issue]:
    """块契约漂移：旧 spec 里记录的契约 vs 当前块实现。

    - 旧 spec（没有 ``contract_shape``）只提示，不阻断；
    - 端口/参数的变化分方向：变宽 / 新增可选 = safe，变窄 / 删除 / 必填新增 = breaking；
    - legacy 版本（schema < 当前）整体跳过：它们还没有契约记录，不制造噪声。
    """
    if spec.schema_version < SPEC_VERSION:
        return []
    issues: list[Issue] = []
    for workflow_name, workflow in spec.workflows.items():
        for node in workflow.nodes:
            if node.fn not in NODE_REGISTRY:
                continue
            if not node.contract_shape:
                issues.append(Issue(
                    "node.contract_unknown",
                    f"workflow[{workflow_name}].{node.id} 没有块契约记录，无法判断漂移",
                    severity=Severity.WARNING,
                    node_id=node.id,
                    workflow=workflow_name,
                ))
                continue
            state = _compare_contract(node.contract_shape, contract_shape(node.fn))
            if state == "ok":
                continue
            breaking = state == "breaking"
            issues.append(Issue(
                "node.contract_drift_breaking" if breaking else "node.contract_drift_safe",
                f"workflow[{workflow_name}].{node.id} 的块契约已变化（{state}）",
                severity=Severity.ERROR if breaking else Severity.WARNING,
                node_id=node.id,
                workflow=workflow_name,
                hint="重新保存该 spec 会按当前块契约重写指纹；breaking 需先核对参数与端口",
            ))
    return issues


def check_spec(
    spec: "ScenarioSpec",
    tool_names: set[str] | None = None,
    prompt_book: PromptBook | None = None,
) -> list[Issue]:
    """校验一份 spec；返回全部 Issue（含 WARNING）。调用方按 severity 过滤。"""
    issues: list[Issue] = []
    if spec.schema_version > SPEC_VERSION or spec.schema_version < MIN_SCHEMA_VERSION:
        issues.append(Issue(
            "spec.schema_version_invalid",
            f"schema_version 必须是 {SPEC_VERSION}，收到 {spec.schema_version}",
        ))
    elif spec.schema_version < SPEC_VERSION:
        # 旧版本只提示，不阻断：旧 spec 必须还能打开、还能构建。
        issues.append(Issue(
            "spec.schema_version_legacy",
            f"schema_version {spec.schema_version} 是旧版本，当前为 {SPEC_VERSION}",
            severity=Severity.WARNING,
        ))
    if not spec.name.strip():
        issues.append(Issue("spec.name_empty", "scenario.name 不能为空"))
    declared_mode = str(spec.mode or "").strip()
    if declared_mode and declared_mode not in VALID_SCENE_MODES:
        issues.append(Issue(
            "spec.mode_invalid",
            f"mode 必须是 agent 或 workflow，收到 {spec.mode!r}",
        ))
    elif declared_mode == "workflow" and not spec.workflows:
        # workflow 模式的执行入口是 workflow_factory；没有图就没有可执行路径，
        # 不能让 Scenario/ExecutionSpec 的默认 agent 静默接管。
        issues.append(Issue(
            "spec.mode_without_workflow",
            "mode=workflow 但没有 workflow 图",
        ))
    # 允许没有 workflow：纯 ReAct 场景（general / coding / router…）只有一个 agent 循环。
    if spec.prompt is not None and spec.prompt.blocks and prompt_book is not None:
        for error in validate_book(list(spec.prompt.blocks), prompt_book):
            issues.append(Issue("prompt.block_invalid", error))
    known_policies = set(policy_names())
    if spec.response_format:
        from core.scenarios.schemas import SCHEMAS

        if spec.response_format not in SCHEMAS:
            issues.append(Issue(
                "spec.response_format_unknown",
                f"未知的 response_format：{spec.response_format}"
                f"（可用：{', '.join(sorted(SCHEMAS))}）",
            ))
    for policy in spec.policies or []:
        if policy.name not in known_policies:
            issues.append(Issue(
                "policy.unknown",
                f"未知策略：{policy.name}（可用：{', '.join(sorted(known_policies))}）",
            ))
        if not policy.enabled and not (policy.reason or "").strip():
            issues.append(Issue(
                "policy.reason_missing",
                f"策略 {policy.name} 被关闭但没有写 reason：移除守卫必须可审计",
            ))
    for name, workflow in spec.workflows.items():
        issues.extend(check_workflow(name, workflow, tool_names))
    # 契约漂移是 spec 级判断：breaking 走 ERROR（阻断构建/发布），safe 走 WARNING。
    issues.extend(check_drift(spec))
    return issues


def messages(issues: Iterable[Issue], *, only_error: bool = True) -> list[str]:
    """把 Issue 投影成旧接口需要的字符串列表。"""
    return [
        issue.message
        for issue in issues
        if (issue.severity is Severity.ERROR) or not only_error
    ]
