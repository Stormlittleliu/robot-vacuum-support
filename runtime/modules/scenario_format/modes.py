# -*- coding: utf-8 -*-
"""Scene 模式契约：workflow 与 agent 两种，不做第三种用户模式。

这里只做**纯结构投影**：把一份 spec 草稿判定为 workflow 或 agent，并在旧
Agent 场景可以无损表达时给出原生 ``mode=agent`` 配置。它不构建 Scenario、
不加载模型、不读写磁盘；真正的执行装配仍在 ``spec.build_from_spec`` /
``modules.runtime.framework``。

旧场景的唯一可迁移形态（无损）：

    START -> react_loop -> END

且只有一个 workflow、没有额外/条件边、``stop_when=no_tool_calls``。节点
prompt / 工具子集 / 正数 max_steps 会被投影到原生 Agent 字段；其它真实图或
非默认停止语义（例如 ``answer_has_citation``）继续留在 workflow。
"""
from __future__ import annotations

from typing import Any

from modules.scenario_format.edges import END, START, split_edge

#: react_loop 的默认停止语义；只有这一种能无损投影到普通 Agent。
ORDINARY_STOP = "no_tool_calls"


def _explicit_mode(draft: dict[str, Any]) -> str | None:
    explicit = str(draft.get("mode") or "").strip()
    return explicit if explicit in {"agent", "workflow"} else None


def _workflows(draft: dict[str, Any]) -> dict[str, Any]:
    raw = draft.get("workflows")
    return raw if isinstance(raw, dict) else {}


def _block_nodes(flow: Any) -> list[dict[str, Any]]:
    if not isinstance(flow, dict):
        return []
    nodes = [node for node in (flow.get("nodes") or []) if isinstance(node, dict)]
    return [
        node
        for node in nodes
        if str(node.get("fn") or "") not in {"", START, END}
    ]


def _edge_pairs(flow: Any) -> list[tuple[str, str, str | None]]:
    if not isinstance(flow, dict):
        return []
    return [split_edge(edge) for edge in (flow.get("edges") or [])]


def _exact_agent_structure(
    draft: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """返回唯一可无损迁移的 legacy Agent 结构；不满足时返回 None。"""
    workflows = _workflows(draft)
    if len(workflows) != 1:
        return None
    name, flow = next(iter(workflows.items()))
    if not isinstance(flow, dict):
        return None
    nodes = _block_nodes(flow)
    if len(nodes) != 1 or str(nodes[0].get("fn")) != "react_loop":
        return None
    node = nodes[0]
    node_id = str(node.get("id") or "")
    if not node_id:
        return None
    args = node.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    if str(args.get("stop_when") or ORDINARY_STOP) != ORDINARY_STOP:
        return None

    edges = _edge_pairs(flow)
    # 必须恰好两条无条件边；多一条、条件边、或重复边都不是普通 Agent。
    if len(edges) != 2:
        return None
    if any(when is not None for _, _, when in edges):
        return None
    pairs = {(source, target) for source, target, _ in edges}
    if pairs != {(START, node_id), (node_id, END)}:
        return None
    return str(name), flow, node


def _agent_structure_error(draft: dict[str, Any]) -> str | None:
    """显式 agent 时的可展示原因；结构合法返回 None。"""
    workflows = _workflows(draft)
    if not workflows:
        return None
    if len(workflows) != 1:
        return "agent scene 只能有一个 workflow"
    _name, flow = next(iter(workflows.items()))
    nodes = _block_nodes(flow)
    if len(nodes) != 1 or str(nodes[0].get("fn")) != "react_loop":
        return "agent scene 只允许一个 react_loop 节点"
    args = nodes[0].get("args") if isinstance(nodes[0], dict) else {}
    args = args if isinstance(args, dict) else {}
    stop_when = str(args.get("stop_when") or ORDINARY_STOP)
    if stop_when != ORDINARY_STOP:
        return (
            f"agent scene 不支持 stop_when={stop_when!r}；"
            "带额外停止语义的图必须保持 workflow 模式"
        )
    edges = _edge_pairs(flow)
    node_id = str(nodes[0].get("id") or "")
    pairs = {(source, target) for source, target, _ in edges}
    if (
        any(when is not None for _, _, when in edges)
        or len(edges) != 2
        or pairs != {(START, node_id), (node_id, END)}
    ):
        return "agent scene 只允许 START → react_loop → END 直连，不能有额外/条件边"
    return None


def infer_scene_mode(draft: dict[str, Any]) -> str:
    """新场景显式声明；旧场景按结构推断。

    - 显式 ``agent`` / ``workflow`` 优先；
    - 没有图（例如原生 Agent 的 ``workflows={}``）推断为 ``agent``；
    - 只有一个可无损迁移的 ``START → react_loop → END`` 推断为 ``agent``；
    - 其余真实图（含非默认停止语义）推断为 ``workflow``。
    """
    explicit = _explicit_mode(draft)
    if explicit is not None:
        return explicit
    workflows = _workflows(draft)
    if not workflows:
        return "agent"
    return "agent" if _exact_agent_structure(draft) is not None else "workflow"


def agent_contract_issues(draft: dict[str, Any]) -> list[str]:
    """agent scene 的结构约束；显式 agent 与图冲突时给出明确原因。"""
    explicit = _explicit_mode(draft)
    if explicit == "workflow":
        return []
    if explicit == "agent":
        error = _agent_structure_error(draft)
        return [error] if error else []
    # 没有显式 mode 时，非精确结构已经被推断为 workflow，不必再报错。
    return []
