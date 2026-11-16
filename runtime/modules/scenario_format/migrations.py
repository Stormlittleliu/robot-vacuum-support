# -*- coding: utf-8 -*-
"""块改名的追加式迁移表。

改 ``register_node`` 的名字等于让所有旧 spec 失效（``NODE_REGISTRY`` 只认当前名字）。
这张表把"改名"从 breaking 降成自动迁移：

- **只追加**；删条目 = 让旧 spec 失效；
- 真名唯一，不做别名歧义索引（``fn`` 就是唯一身份）；
- 每个条目必须有 ``since``（从哪个 schema 版本开始改名），供人判断能否回退；
- 表里没有的旧 ``fn`` 原样保留，随后由 ``check_spec`` 报 ``node.unregistered_block``。
"""
from __future__ import annotations

from typing import Any

from modules.scenario_format.spec_nodes import NODE_REGISTRY

#: ``old_fn -> (new_fn, since)``
RENAMES: dict[str, tuple[str, str]] = {}


def apply_renames(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """就地改写 ``data`` 里的旧块名；返回 ``(data, 失败项)``。

    目标块未注册时**不抛**：保留旧名并记一条失败项，坏 spec 仍然能打开、
    能编辑，由校验器给出可定位的错误。
    """
    failed: list[str] = []
    if not isinstance(data, dict):
        return data, failed
    workflows = data.get("workflows")
    if not isinstance(workflows, dict):
        return data, failed
    for workflow_name, workflow in workflows.items():
        if not isinstance(workflow, dict):
            continue
        for node in workflow.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            old = str(node.get("fn") or "")
            target = RENAMES.get(old)
            if target is None:
                continue
            new, _since = target
            if new not in NODE_REGISTRY:
                failed.append(
                    f"workflow[{workflow_name}].{node.get('id')}：迁移目标块未注册：{new}"
                )
                continue
            node["fn"] = new
    return data, failed
