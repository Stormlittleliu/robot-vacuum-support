# -*- coding: utf-8 -*-
"""编辑器自己的文档模型。

不是控制台草稿的搬运：前端拥有编辑器文档（节点/边/参数/位置），
保存时才投影成 WorkflowSpec。visual-only / observe 对象之后也放这一层，
由投影器过滤，不进入执行图。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.paths import runtime_path
from modules.editor import spec_edit
from modules.scenario_format.edges import END, START
from modules.scenario_format.modes import infer_scene_mode
from modules.scenario_format.spec import SpecConflict, SpecError, save_spec
from modules.scenario_format.spec_nodes import (
    NODE_REGISTRY,
    block_label,
    block_ports,
    block_params,
)


def _param_spec(param: Any) -> dict[str, Any]:
    return {
        "name": param.name,
        "type": param.type,
        "default": param.default,
        "required": bool(param.required),
        "choices": list(param.choices or ()),
        "minimum": param.minimum,
        "maximum": param.maximum,
        "help": param.help,
    }


def _port_spec(port: Any) -> dict[str, Any]:
    return {
        "name": port.name,
        "type": port.type,
        "required": bool(port.required),
        "default": port.default,
        "help": port.help,
    }


def blocks() -> list[dict[str, Any]]:
    """编辑器块库：参数和端口都由块契约生成，不在 UI 里手抄。"""
    out: list[dict[str, Any]] = []
    for fn in sorted(NODE_REGISTRY):
        args = spec_edit.block_defaults(fn)
        ports = block_ports(fn, args)
        out.append(
            {
                "fn": fn,
                "label": block_label(fn),
                "params": [_param_spec(item) for item in block_params(fn)],
                "inputs": [_port_spec(item) for item in ports.inputs],
                "outputs": [_port_spec(item) for item in ports.outputs],
            }
        )
    return out


def _node(
    node: dict[str, Any],
    position: list[int],
    errors: set[str],
) -> dict[str, Any]:
    node_id = str(node.get("id"))
    fn = str(node.get("fn") or "")
    args = dict(node.get("args") or {})
    ports = block_ports(fn, args) if fn in NODE_REGISTRY else None
    return {
        "id": node_id,
        "kind": "block",
        "block": fn,
        "title": block_label(fn) if fn else node_id,
        "position": {"x": int(position[0]), "y": int(position[1])},
        "params": args,
        "inputs": [_port_spec(item) for item in ports.inputs] if ports else [],
        "outputs": [_port_spec(item) for item in ports.outputs] if ports else [],
        "error": node_id in errors,
    }


def visual_path(name: str, workflow: str) -> Path:
    return runtime_path("editor_canvas", f"{name}--{workflow}.json")


def load_visual(name: str, workflow: str) -> dict[str, Any]:
    path = visual_path(name, workflow)
    if not path.is_file():
        return {"nodes": [], "edges": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"nodes": [], "edges": []}
    return {
        "nodes": list(data.get("nodes") or []),
        "edges": list(data.get("edges") or []),
    }


def save_visual(name: str, workflow: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    path = visual_path(name, workflow)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scenario": name,
        "workflow": workflow,
        "nodes": nodes,
        "edges": edges,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    tmp.replace(path)


def resolve_ports(fn: str, params: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """参数改变后重建端口；编辑器不用等保存就知道端口增删。"""
    if fn not in NODE_REGISTRY:
        raise SpecError([f"未知块：{fn}"])
    args = spec_edit.normalize_args(fn, dict(params or {}))
    ports = block_ports(fn, args)
    return {
        "inputs": [_port_spec(item) for item in ports.inputs],
        "outputs": [_port_spec(item) for item in ports.outputs],
    }


def load_document(name: str, workflow: str | None = None) -> dict[str, Any]:
    draft, version = spec_edit.load_draft_versioned(name)
    workflows = list((draft.get("workflows") or {}).keys())
    if not workflows:
        raise SpecError(["场景没有工作流"])
    chosen = workflow if workflow in workflows else workflows[0]
    flow = draft["workflows"][chosen]
    edges_raw = [spec_edit.split_edge(edge) for edge in flow.get("edges") or []]
    issues = spec_edit.draft_issues(draft)
    errors = set(spec_edit.bad_node_ids(issues))
    positions = spec_edit.node_positions(name, chosen, flow.get("nodes") or [], edges_raw)
    xs = [item[0] for item in positions.values()] or [0]
    ys = [item[1] for item in positions.values()] or [0]

    nodes: list[dict[str, Any]] = [
        {
            "id": START,
            "kind": "start",
            "block": "",
            "title": "START",
            "position": {"x": min(xs) - 220, "y": int(sum(ys) / len(ys))},
            "params": {},
            "inputs": [],
            "outputs": [],
            "error": False,
        }
    ]
    for node in flow.get("nodes") or []:
        nodes.append(_node(node, positions[str(node["id"])], errors))
    nodes.append(
        {
            "id": END,
            "kind": "end",
            "block": "",
            "title": "END",
            "position": {"x": max(xs) + 220, "y": int(sum(ys) / len(ys))},
            "params": {},
            "inputs": [],
            "outputs": [],
            "error": False,
        }
    )

    edges = [
        {
            "id": f"{source}->{target}",
            "source": source,
            "target": target,
            "condition": when or "",
        }
        for source, target, when in edges_raw
    ]

    return {
        "scenario": name,
        "workflow": chosen,
        "workflows": workflows,
        "mode": infer_scene_mode(draft),
        "revision": version,
        "limits": {key: (draft.get("harness") or {}).get(key) for key in ("token_budget", "tool_call_limit")},
        "nodes": nodes,
        "edges": edges,
        "blocks": blocks(),
        "inputs": list(flow.get("inputs") or [{"name": "input", "type": "text", "required": True, "title": "用户输入"}]),
        "outputs": list(flow.get("outputs") or [{"name": "answer", "type": "text", "title": "回答"}]),
        "issues": [str(item) for item in issues],
        "visual_nodes": load_visual(name, chosen)["nodes"],
        "observe_edges": load_visual(name, chosen)["edges"],
    }


def _apply_document(draft: dict[str, Any], workflow: str, document: dict[str, Any]) -> dict[str, list[int]]:
    if "limits" in document:
        limits = document["limits"]
        if not isinstance(limits, dict) or set(limits) - {"token_budget", "tool_call_limit"}:
            raise SpecError(["执行限制字段无效"])
        for value in limits.values():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise SpecError(["执行上限必须是正整数；关闭限制使用 null"])
        draft["harness"] = {**(draft.get("harness") or {}), **limits}
    raw_nodes = document.get("nodes") or []
    nodes: list[dict[str, Any]] = []
    positions: dict[str, list[int]] = {}
    used: set[str] = set()
    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "")
        kind = str(item.get("kind") or "block")
        if kind in {"start", "end"}:
            point = item.get("position") or {}
            positions[node_id] = [int(point.get("x", 0)), int(point.get("y", 0))]
            continue
        fn = str(item.get("block") or "")
        if not node_id or node_id in used or node_id in {START, END} or fn not in NODE_REGISTRY:
            continue
        used.add(node_id)
        args = spec_edit.normalize_args(fn, dict(item.get("params") or {}))
        nodes.append({"id": node_id, "fn": fn, "args": args})
        point = item.get("position") or {}
        positions[node_id] = [int(point.get("x", 0)), int(point.get("y", 0))]

    if not nodes:
        raise SpecError(["文档里没有可执行节点"])

    known = used | {START, END}
    edges: list[list[str]] = []
    for item in document.get("edges") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        target = str(item.get("target") or "")
        if source not in known or target not in known:
            continue
        condition = str(item.get("condition") or "").strip()
        edge = [source, target] if not condition else [source, target, condition]
        if edge not in edges:
            edges.append(edge)

    if workflow not in (draft.get("workflows") or {}):
        raise SpecError([f"未知工作流：{workflow}"])
    draft["workflows"][workflow]["nodes"] = nodes
    draft["workflows"][workflow]["edges"] = edges
    if "inputs" in document:
        draft["workflows"][workflow]["inputs"] = list(document.get("inputs") or [])
    if "outputs" in document:
        draft["workflows"][workflow]["outputs"] = list(document.get("outputs") or [])
    return positions


def _project_document(name: str, document: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """把不可信的编辑器文档投影进草稿；不落盘，供校验和运行共用。"""
    workflow = str(document.get("workflow") or "")
    if not workflow:
        raise SpecError(["文档缺少 workflow"])
    draft, _version = spec_edit.load_draft_versioned(name)
    _apply_document(draft, workflow, document)
    return draft, workflow


def _issue_messages(issues: list[Any]) -> list[str]:
    messages: list[str] = []
    for issue in issues:
        if isinstance(issue, str):
            messages.append(issue)
            continue
        message = getattr(issue, "message", None)
        if message:
            messages.append(str(message))
        else:
            messages.append(str(issue))
    return messages


def validate_document(name: str, document: dict[str, Any]) -> dict[str, Any]:
    """只校验当前编辑文档，不写 spec / 布局，也不调用模型工具。"""
    try:
        draft, workflow = _project_document(name, document)
        issues = _issue_messages(spec_edit.draft_errors(draft))
    except SpecError as exc:
        issues = _issue_messages(list(exc.issues or [])) or [str(exc)]
        workflow = str(document.get("workflow") or "")
        draft, _version = spec_edit.load_draft_versioned(name)
    version = spec_edit.load_draft_versioned(name)[1]
    return {
        "scenario": name,
        "workflow": workflow,
        "valid": not issues,
        "issues": issues,
        "revision": version,
    }


def save_document(
    name: str,
    document: dict[str, Any],
    expected_version: int | None = None,
    coalesce: bool = False,
) -> dict[str, Any]:
    """把编辑器文档投影成 spec，再走统一校验 / 版本化保存。"""
    workflow = str(document.get("workflow") or "")
    if not workflow:
        raise SpecError(["文档缺少 workflow"])

    draft, version = spec_edit.load_draft_versioned(name)
    if expected_version is not None and version != expected_version:
        raise SpecConflict(version, reason="stale")

    positions = _apply_document(draft, workflow, document)
    issues = spec_edit.draft_errors(draft)
    if issues:
        raise SpecError(issues)
    spec = spec_edit.spec_from_draft(draft)
    if coalesce:
        # 已发布的场景不能覆盖历史版本；发布快照必须保持不变。
        from modules.editor.entry import load_entry
        if load_entry(name).enabled:
            coalesce = False
    save_spec(spec, expected_version=version, coalesce=coalesce)
    if positions:
        spec_edit.save_layout(name, workflow, positions)
    save_visual(
        name,
        workflow,
        list(document.get("visual_nodes") or []),
        list(document.get("observe_edges") or []),
    )
    return load_document(name, workflow)


def build_scenario_from_document(name: str, document: dict[str, Any]) -> Any:
    """用当前编辑文档在内存里构建 Scenario，不写 spec。"""
    draft, _workflow = _project_document(name, document)
    issues = spec_edit.draft_errors(draft)
    if issues:
        raise SpecError(issues)
    spec = spec_edit.spec_from_draft(draft)

    from core.llm.factory import chat_model
    from core.paths import SPEC_ROOT
    from core.prompts.blocks import load_book
    from core.scenarios import pools
    from core.scenarios.schemas import resolve as resolve_schema
    from core.scenarios.spec import SpecError as RuntimeSpecError, build_from_spec, resolve_cases_path

    box = pools.toolbox(name)
    available = {str(getattr(tool, "name", "")) for tool in box.tools}
    missing = [tool for tool in spec.tools if tool not in available]
    if missing:
        raise RuntimeSpecError([f"工具不在工具盒：{', '.join(missing)}"])
    book = load_book(Path(SPEC_ROOT) / name / "prompt.yml")
    return build_from_spec(
        spec,
        tools=list(box.tools),
        model=chat_model,
        cases_path=resolve_cases_path(spec, SPEC_ROOT),
        checkpoint_backend="sqlite",
        checkpoint_path=f"data/runtime/spec_{name}_checkpoints.sqlite",
        runtime=box.runtime,
        response_format=resolve_schema(spec.response_format),
        defaults=box.defaults,
        prompt_book=book,
    )


def document_json(name: str, workflow: str | None = None) -> dict[str, Any]:
    return load_document(name, workflow)


def _context_tool(
    name: str,
    origin: str,
    source_hint: str,
    catalog: Any = None,
) -> dict[str, Any]:
    """只读工具元数据；目录没有就明确未知，不补描述或 schema。"""
    unknown = {
        "name": name,
        "label": name,
        "description": "",
        "description_state": "unknown",
        "input_schema": {},
        "schema_state": "unknown",
        "source": f"{source_hint}（目录未登记）" if source_hint else "未知",
        "state": "unknown",
        "trust": "unknown",
        "origin": origin,
        "uri": "",
        "metadata": {},
        "toolboxes": [],
        "toolbox_state": "unknown",
    }
    try:
        if catalog is None:
            from modules.resources import ResourceCatalog

            catalog = ResourceCatalog()
        descriptor = catalog.get(f"ah://tool/{name}")
    except Exception:  # noqa: BLE001 - 目录缺失/非法名不影响只读说明
        return unknown
    schema = descriptor.get("input_schema") if isinstance(descriptor, dict) else None
    schema_known = bool(
        isinstance(schema, dict)
        and schema.get("properties")
        and (descriptor.get("metadata") or {}).get("schema_available", True)
    )
    description = str(descriptor.get("description") or "")
    metadata = descriptor.get("metadata") or {}
    raw_boxes = metadata.get("toolboxes")
    if not isinstance(raw_boxes, list):
        raw_boxes = metadata.get("pools") or []
    toolboxes = [str(item) for item in raw_boxes if str(item or "").strip()]
    return {
        "name": name,
        "label": str(descriptor.get("label") or name),
        "description": description,
        "description_state": "known" if description else "unknown",
        "input_schema": schema if isinstance(schema, dict) else {},
        "schema_state": "known" if schema_known else "unknown",
        "source": str(descriptor.get("source") or "ResourceCatalog"),
        "state": "known" if description or schema_known else "unknown",
        "trust": "declared" if description or schema_known else "unknown",
        "origin": origin,
        "uri": str(descriptor.get("uri") or ""),
        "metadata": metadata,
        "toolboxes": toolboxes,
        "toolbox_state": "known" if toolboxes else "unassigned",
    }


def _policy_entries(draft: dict[str, Any]) -> list[dict[str, Any]]:
    from modules.runtime.policy_meta import POLICY_DOCS

    source_file = f"spec:{draft.get('name') or ''}"
    entries: list[dict[str, Any]] = []

    def add(name: str, origin: str, args: dict[str, Any] | None = None) -> None:
        if any(item["name"] == name for item in entries):
            return
        doc = POLICY_DOCS.get(name)
        known = doc is not None
        entries.append(
            {
                "name": name,
                "label": doc.label if doc else name,
                "desc": doc.desc if doc else "POLICY_DOCS 未登记该策略，说明未知",
                "layer": doc.layer if doc else "未知",
                "origin": origin,
                "state": "declared" if known else "unknown",
                "trust": "declared" if known else "unknown",
                "source": source_file,
                "args": args or {},
                # 编辑器没有加载运行时装配；声明不等于实际触发。
                "assembly": "unobserved",
                "observed": "unobserved",
                "observed_reason": "编辑器未构建运行时，也未收到该策略的 trace 证据",
            }
        )

    policies = draft.get("policies")
    if isinstance(policies, list):
        for item in policies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                add(name, "spec.policies", dict(item.get("args") or {}))
    harness = draft.get("harness") or {}
    for key, name in (
        ("enable_todo", "todo"),
        ("enable_summarization", "summarization"),
        ("tool_call_limit", "limit"),
        ("token_budget", "budget"),
    ):
        value = harness.get(key)
        if value not in (None, False, 0, ""):
            add(name, f"spec.harness.{key}", {"value": value})
    return entries


def context_config(
    name: str,
    workflow: str | None = None,
    react_loop_id: str | None = None,
) -> dict[str, Any]:
    """上下文配置只读元数据：不构建场景、不加载模型/工具实例。"""
    from modules.runtime.policy_meta import POLICY_DOCS

    draft, version = spec_edit.load_draft_versioned(name)
    workflows = list((draft.get("workflows") or {}).keys())
    if not workflows:
        raise SpecError(["场景没有工作流"])
    chosen = workflow if workflow in workflows else workflows[0]
    flow = draft["workflows"][chosen]
    loops = [
        {
            "id": str(node.get("id") or ""),
            "fn": str(node.get("fn") or ""),
            "prompt": str((node.get("args") or {}).get("prompt") or ""),
            "tools": [str(item) for item in ((node.get("args") or {}).get("tools") or [])],
        }
        for node in flow.get("nodes") or []
        if isinstance(node, dict) and node.get("fn") == "react_loop"
    ]
    target_id = react_loop_id if react_loop_id in {item["id"] for item in loops} else None
    if target_id is None and len(loops) == 1:
        target_id = loops[0]["id"]

    inherited = str(draft.get("system_prompt") or "")
    inherited_payload = {
        "value": inherited,
        "source": "spec.system_prompt" if inherited else "builtin_default",
        "state": "declared" if inherited else "builtin_default",
        "trust": "declared" if inherited else "builtin",
        "editable": False,
    }

    tool_names: list[str] = []
    for item in draft.get("tools") or []:
        text = str(item or "").strip()
        if text and text not in tool_names:
            tool_names.append(text)
    subagents: list[dict[str, Any]] = []
    try:
        from core.scenarios import subagents as scenario_subagents

        subagents = scenario_subagents.describe(name)
    except Exception:  # noqa: BLE001
        subagents = []
    subagent_tools = [str(item.get("tool_name") or "") for item in subagents if item.get("tool_name")]

    # 工具盒元数据与工具归属都来自 ResourceCatalog；这里只做只读投影。
    catalog = None
    toolboxes: list[dict[str, Any]] = []
    toolbox_categories: list[dict[str, str]] = []
    try:
        from modules.resources import ResourceCatalog

        catalog = ResourceCatalog()
        toolboxes = catalog.toolboxes()
        toolbox_categories = catalog.toolbox_categories()
    except Exception:  # noqa: BLE001 - 目录不可用不应拖垮 prompt / 工具白名单
        toolboxes = []
        toolbox_categories = []

    tool_pool = [
        _context_tool(tool, "spec.tools", "spec.tools", catalog)
        for tool in tool_names
    ]
    for tool in subagent_tools:
        if tool not in tool_names:
            tool_pool.append(
                _context_tool(tool, "subagent", "core.scenarios.subagents", catalog)
            )

    target_node = next((node for node in flow.get("nodes") or [] if str(node.get("id")) == target_id), None)
    target_args = (target_node or {}).get("args") or {}
    node_prompt = str(target_args.get("prompt") or "")

    return {
        "scenario": name,
        "workflow": chosen,
        "workflows": workflows,
        "revision": version,
        "mode": infer_scene_mode(draft),
        "react_loops": loops,
        "target_react_loop_id": target_id,
        "target_prompt": {
            "node_id": target_id,
            "value": node_prompt,
            "state": "declared" if node_prompt else "empty",
            "source": f"spec.workflows.{chosen}.nodes[{target_id}].args.prompt" if target_id else "unknown",
            "trust": "declared" if target_id else "unknown",
        },
        "system_prompt": inherited_payload,
        "effective_system_prompt_source": (
            "节点 prompt" if node_prompt else ("场景 system_prompt" if inherited else "运行时内置默认")
        ),
        "inputs": list(flow.get("inputs") or []),
        "outputs": list(flow.get("outputs") or []),
        "tools": [str(item) for item in draft.get("tools") or []],
        "tool_pool": tool_pool,
        "toolboxes": toolboxes,
        "toolbox_categories": toolbox_categories,
        "subagents": subagents,
        "harness": {
            "config": dict(draft.get("harness") or {}),
            "source": "spec.harness",
            "state": "declared",
            "trust": "declared",
        },
        "policies": _policy_entries(draft),
        "policy_docs": [
            {"name": doc.key, "label": doc.label, "desc": doc.desc, "layer": doc.layer}
            for doc in POLICY_DOCS.values()
        ],
        "policy_assembly_state": "unobserved",
        "policy_assembly_reason": "只为展示读取 spec_edit 投影与 POLICY_DOCS；未调用 build_scenario_from_document，装配/触发均未观测",
        "notes": [
            "模型看到的是工具描述与参数 schema；工具执行时收到的是实际参数。",
            "上下文由运行时按轮次装配；默认不会继承全部历史，具体以本次运行时间线为准。",
        ],
    }
