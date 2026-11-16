# -*- coding: utf-8 -*-
"""Scene 模式契约：workflow 与 agent 两种，不做第三种用户模式。"""
from __future__ import annotations

from typing import Any

from modules.scenario_format.edges import END, START


def infer_scene_mode(draft: dict[str, Any]) -> str:
    """新场景显式声明；旧场景按结构推断。"""
    explicit = str(draft.get("mode") or "").strip()
    if explicit in {"agent", "workflow"}:
        return explicit
    workflows = draft.get("workflows") or {}
    if len(workflows) != 1:
        return "workflow"
    flow = next(iter(workflows.values()))
    nodes = [node for node in (flow.get("nodes") or []) if isinstance(node, dict)]
    block_nodes = [
        node for node in nodes
        if str(node.get("fn") or "") not in {"", START, END}
    ]
    if len(block_nodes) != 1 or str(block_nodes[0].get("fn")) != "react_loop":
        return "workflow"
    edges = [tuple(edge) if isinstance(edge, (list, tuple)) else edge for edge in (flow.get("edges") or [])]
    pairs = set()
    for edge in edges:
        if isinstance(edge, dict):
            pairs.add((str(edge.get("from")), str(edge.get("to"))))
        elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
            pairs.add((str(edge[0]), str(edge[1])))
    return "agent" if {(START, str(block_nodes[0].get("id"))), (str(block_nodes[0].get("id")), END)} <= pairs else "workflow"


def agent_contract_issues(draft: dict[str, Any]) -> list[str]:
    """agent scene 只允许 START → react_loop → END。"""
    if infer_scene_mode(draft) != "agent":
        return []
    workflows = draft.get("workflows") or {}
    if len(workflows) != 1:
        return ["agent scene 只能有一个 workflow"]
    flow = next(iter(workflows.values()))
    block_nodes = [
        node for node in (flow.get("nodes") or [])
        if isinstance(node, dict) and str(node.get("fn") or "") not in {"", START, END}
    ]
    if len(block_nodes) != 1 or str(block_nodes[0].get("fn")) != "react_loop":
        return ["agent scene 只允许一个 react_loop 节点"]
    return []
