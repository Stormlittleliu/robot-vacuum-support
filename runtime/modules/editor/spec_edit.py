# -*- coding: utf-8 -*-
"""场景编辑器背后的纯逻辑：草稿（dict）↔ ``ScenarioSpec``。

Streamlit 只负责摆控件；所有会改结构、会写盘的判断都在这里，所以可以直接
单测：加节点、改块、换参数、校验、存新版本，都不需要跑界面。

草稿就是 ``spec_to_dict`` 的形状（JSON 可序列化），这样"载入已有 spec"和
"界面新建 spec"共用一套代码，不用担心两套模型对不上。
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from modules.scenario_format.edges import END, START
from modules.scenario_format.spec import (
    split_edge,
    SPEC_ROOT,
    ScenarioSpec,
    SpecConflict,
    SpecError,
    SPEC_VERSION,
    load_active_versioned,
    load_spec,
    save_spec,
    spec_from_dict,
    spec_to_dict,
)
from modules.scenario_format.spec_nodes import NODE_REGISTRY, block_params, block_label
from modules.scenario_format.spec_check import Issue, check_spec, messages

from core.paths import runtime_path

DEFAULT_WORKFLOW = "qa"
LAYOUT_ROOT = runtime_path("spec_layout")
NODE_W = 184
NODE_H = 64
COL_GAP = 238
ROW_GAP = 104
# 第一个节点往右挪一点，给 START 留出位置（否则 START 会被画到画布外面）。
ORIGIN = (216, 40)


def new_draft(
    name: str,
    tools: list[str] | None = None,
    description: str = "",
) -> dict[str, Any]:
    """新场景模板：检索 → 生成回答 → 引用校验。

    模板只是"能编译通过的最小结构"，不预设业务；用户按需要加/删节点。
    """
    tools = list(tools or [])
    return {
        "schema_version": SPEC_VERSION,
        "name": name,
        "description": description or "在控制台里搭出来的场景",
        "tools": tools,
        "system_prompt": "你是严谨的助手。回答必须用 [来源id] 标注引用；资料不足就明确说无法确定。",
        "harness": {
            "enable_todo": False,
            "enable_summarization": True,
            "tool_call_limit": 8,
            "token_budget": 20000,
        },
        "policies": None,
        "workflows": {DEFAULT_WORKFLOW: template_workflow(tools)},
    }


def template_workflow(tools: list[str] | None = None) -> dict[str, Any]:
    """默认三节点流程；有工具就把它填进检索节点。"""
    tools = list(tools or [])
    retrieve_args: dict[str, Any] = {"limit": 5}
    if tools:
        retrieve_args["tool"] = tools[0]
    return {
        "nodes": [
            {"id": "search", "fn": "retrieve", "args": retrieve_args},
            {"id": "answer", "fn": "llm_answer", "args": {}},
            {"id": "verify", "fn": "require_citation", "args": {}},
        ],
        "edges": [
            [START, "search"],
            ["search", "answer"],
            ["answer", "verify"],
            ["verify", END],
        ],
    }


def draft_from_spec(spec: ScenarioSpec) -> dict[str, Any]:
    """把已保存的 spec 转成草稿（深拷贝语义，不改调用方对象）。"""
    return spec_to_dict(spec)


def spec_from_draft(draft: dict[str, Any]) -> ScenarioSpec:
    """草稿 -> ``ScenarioSpec``；结构错误统一抛 ``SpecError``（带 Issue）。"""
    try:
        return spec_from_dict(dict(draft))
    except SpecError as exc:
        if not exc.issues:
            exc.issues = [
                Issue(
                    "spec.parse_failed",
                    str(exc),
                    hint="检查字段类型；未知字段会被 schema 拒绝",
                )
            ]
        raise
    except Exception as exc:  # noqa: BLE001 - pydantic 的报错不适合直接上屏
        message = f"{type(exc).__name__}: {exc}"
        raise SpecError([message], [Issue("spec.parse_failed", message)]) from exc


def load_draft(
    name: str,
    root: str | None = None,
    tools: list[str] | None = None,
) -> dict[str, Any]:
    """读该场景的 active spec；没有就返回模板草稿。"""
    return load_draft_versioned(name, root, tools)[0]


def load_draft_versioned(
    name: str,
    root: str | None = None,
    tools: list[str] | None = None,
) -> tuple[dict[str, Any], int | None]:
    """同时返回草稿与它对应的 active 版本号（同一次读指针，避免版本错配）。"""
    spec_root = SPEC_ROOT if root is None else root
    spec, version, _path = load_active_versioned(name, spec_root)
    if spec is None:
        return new_draft(name, tools), None
    return draft_from_spec(spec), version


def save_candidate(
    draft: dict[str, Any],
    root: str | None = None,
) -> str:
    """把草稿另存为候选：只写 ``candidates/``，**不切 active**。

    冲突时给用户的安全出口：不覆盖别处保存的版本，也不丢本地改动。
    """
    name = str(draft.get("name") or "").strip()
    if not name:
        raise SpecError(["草稿缺少场景名"])
    if not name.replace("_", "").isalnum() or name != name.lower():
        raise SpecError([f"场景名不合法：{name}（只允许小写字母、数字、下划线）"])
    directory = (SPEC_ROOT if root is None else Path(root)) / name / "candidates"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = directory / f"candidate-{stamp}-{uuid.uuid4().hex[:6]}.json"
    payload = json.dumps(draft, ensure_ascii=False, indent=2)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8", newline="\n")
    tmp.replace(target)
    return str(target)


def block_defaults(fn: str) -> dict[str, Any]:
    """按块的参数契约生成默认参数；必填项留空，由用户填。"""
    args: dict[str, Any] = {}
    for param in block_params(fn):
        if param.required:
            continue
        if param.default is not None:
            args[param.name] = param.default
    return args


def normalize_args(fn: str, args: dict[str, Any]) -> dict[str, Any]:
    """去掉空值，避免 spec 里出现一堆 ``""`` 和 ``null``。"""
    allowed = {param.name for param in block_params(fn)}
    cleaned: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if allowed and key not in allowed:
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple)) and not value:
            continue
        cleaned[key] = value
    return cleaned


def next_node_id(nodes: list[dict[str, Any]], base: str = "step") -> str:
    """生成不与现有节点冲突的 id。"""
    existing = {str(node.get("id")) for node in nodes}
    index = 1
    while f"{base}{index}" in existing:
        index += 1
    return f"{base}{index}"


def add_node(
    draft: dict[str, Any],
    workflow: str,
    fn: str = "llm_answer",
    node_id: str | None = None,
) -> str:
    """往工作流里加一个节点，返回节点 id。参数按块契约填默认值。"""
    if fn not in NODE_REGISTRY:
        raise SpecError([f"未注册的块：{fn}"])
    if node_id and str(node_id).strip() in {START, END}:
        raise SpecError([f"节点 id 不能是保留名：{node_id}"])
    nodes = draft["workflows"][workflow]["nodes"]
    node_id = node_id or next_node_id(nodes, fn.split("_")[0])
    nodes.append({"id": node_id, "fn": fn, "args": block_defaults(fn)})
    return node_id


def remove_node(draft: dict[str, Any], workflow: str, index: int) -> None:
    """删节点并顺手删掉挂在它身上的边（不做自动重连）。"""
    nodes = draft["workflows"][workflow]["nodes"]
    if not 0 <= index < len(nodes):
        return
    removed = str(nodes[index].get("id"))
    nodes.pop(index)
    edges = draft["workflows"][workflow]["edges"]
    draft["workflows"][workflow]["edges"] = [
        edge for edge in edges
        if removed not in (split_edge(edge)[0], split_edge(edge)[1])
    ]


def rename_node(draft: dict[str, Any], workflow: str, old: str, new: str) -> str:
    """改节点 id，并把引用它的边一起改；返回最终 id。"""
    new = (new or "").strip()
    workflow_draft = draft["workflows"][workflow]
    nodes = workflow_draft["nodes"]
    existing = {str(node.get("id")) for node in nodes}
    if not new or new in {START, END}:
        raise SpecError([f"节点 id 不合法：{new!r}"])
    if new != old and new in existing:
        raise SpecError([f"节点 id 已存在：{new}"])
    for node in nodes:
        if str(node.get("id")) == old:
            node["id"] = new
            break
    workflow_draft["edges"] = [
        [new if source == old else source, new if target == old else target]
        + ([when] if when else [])
        for source, target, when in (
            split_edge(edge) for edge in workflow_draft["edges"]
        )
    ]
    return new


def set_edge(draft: dict[str, Any], workflow: str, source: str, target: str) -> None:
    """加一条边；重复的边不重复加。"""
    edges = draft["workflows"][workflow]["edges"]
    pair = [str(source), str(target)]
    if pair not in edges:
        edges.append(pair)


def add_workflow(draft: dict[str, Any], name: str) -> str:
    """新建一个空工作流（带一个 START 边都没有的节点列表，交给用户连）。"""
    name = (name or "").strip()
    if not name:
        raise SpecError(["工作流名不能为空"])
    if name in draft["workflows"]:
        raise SpecError([f"工作流已存在：{name}"])
    draft["workflows"][name] = template_workflow(draft.get("tools") or [])
    return name


def remove_workflow(draft: dict[str, Any], name: str) -> None:
    if len(draft.get("workflows") or {}) <= 1:
        raise SpecError(["至少保留一个工作流"])
    draft["workflows"].pop(name, None)


#: ``apply_ops`` 支持的语义操作。外部 agent 只能通过它们改结构（不给整份 JSON）。
OP_NAMES = (
    "add_node",
    "remove_node",
    "rename_node",
    "set_arg",
    "set_edge",
    "remove_edge",
    "add_workflow",
    "remove_workflow",
)


def apply_ops(
    draft: dict[str, Any],
    ops: list[Any],
) -> tuple[dict[str, Any], list[Issue], dict[str, str]]:
    """把一批语义 op 应用到草稿：**整批或零**。

    返回 ``(draft, issues, created)``：

    - 任一 op 非法 → 返回**原草稿**与逐条 ``op.*`` Issue（不落盘、不改内存）；
    - ``created`` 把 ``ref`` 映射到新节点 id，方便后续 op 引用。
    """
    issues: list[Issue] = []
    created: dict[str, str] = {}
    working = copy.deepcopy(draft)

    def fail(code: str, message: str, *, loc: str, node_id: str | None = None) -> None:
        issues.append(Issue(code, message, node_id=node_id, loc=loc))

    for position, raw in enumerate(ops or []):
        loc = f"ops[{position}]"
        if not isinstance(raw, dict):
            fail("op.bad_field", f"{loc} 必须是对象", loc=loc)
            continue
        kind = str(raw.get("op") or raw.get("kind") or "").strip()
        workflow_name = str(raw.get("workflow") or "").strip()
        if kind not in OP_NAMES:
            fail("op.unknown", f"{loc} 未知操作：{kind or '（空）'}", loc=loc)
            continue
        if kind == "add_workflow":
            if not workflow_name:
                fail("op.bad_field", f"{loc}.workflow 不能为空", loc=loc)
                continue
            try:
                add_workflow(working, workflow_name)
            except SpecError as exc:
                fail("op.bad_field", f"{loc}：{exc}", loc=loc)
            continue
        if kind == "remove_workflow":
            if workflow_name not in (working.get("workflows") or {}):
                fail("op.target_missing", f"{loc} 工作流不存在：{workflow_name}", loc=loc)
                continue
            try:
                remove_workflow(working, workflow_name)
            except SpecError as exc:
                fail("op.bad_field", f"{loc}：{exc}", loc=loc)
            continue

        workflows = working.get("workflows") or {}
        if workflow_name not in workflows:
            fail("op.target_missing", f"{loc} 工作流不存在：{workflow_name}", loc=loc)
            continue
        nodes = workflows[workflow_name]["nodes"]

        def find(node_id: str) -> int | None:
            for index, node in enumerate(nodes):
                if str(node.get("id")) == node_id:
                    return index
            return None

        def resolve(value: Any) -> str:
            """同批 op 可以引用前面 ``add_node`` 的 ``ref``。"""
            text = str(value or "").strip()
            return created.get(text, text)

        if kind == "add_node":
            fn = str(raw.get("fn") or "").strip()
            if fn not in NODE_REGISTRY:
                fail("op.bad_field", f"{loc}.fn 不是已注册的块：{fn or '（空）'}", loc=loc)
                continue
            node_id = str(raw.get("node_id") or "").strip() or None
            if node_id and find(node_id) is not None:
                fail("op.bad_field", f"{loc}.node_id 已存在：{node_id}", loc=loc, node_id=node_id)
                continue
            try:
                new_id = add_node(working, workflow_name, fn, node_id)
            except SpecError as exc:
                fail("op.bad_field", f"{loc}：{exc}", loc=loc, node_id=node_id)
                continue
            args = raw.get("args")
            if isinstance(args, dict) and args:
                target = next(item for item in nodes if str(item.get("id")) == new_id)
                target["args"].update(normalize_args(fn, args))
            ref = str(raw.get("ref") or "").strip()
            if ref:
                created[ref] = new_id
            continue

        if kind == "remove_node":
            node_id = resolve(raw.get("node_id"))
            index = find(node_id)
            if index is None:
                fail("op.target_missing", f"{loc} 节点不存在：{node_id}", loc=loc, node_id=node_id)
                continue
            remove_node(working, workflow_name, index)
            continue

        if kind == "rename_node":
            old = resolve(raw.get("from"))
            new = str(raw.get("to") or "").strip()
            if find(old) is None:
                fail("op.target_missing", f"{loc} 节点不存在：{old}", loc=loc, node_id=old)
                continue
            try:
                rename_node(working, workflow_name, old, new)
            except SpecError as exc:
                fail("op.bad_field", f"{loc}：{exc}", loc=loc, node_id=old)
            continue

        if kind == "set_arg":
            node_id = resolve(raw.get("node_id"))
            index = find(node_id)
            if index is None:
                fail("op.target_missing", f"{loc} 节点不存在：{node_id}", loc=loc, node_id=node_id)
                continue
            key = str(raw.get("key") or "").strip()
            if not key:
                fail("op.bad_field", f"{loc}.key 不能为空", loc=loc, node_id=node_id)
                continue
            node = nodes[index]
            allowed = {param.name for param in block_params(str(node.get("fn")))}
            if allowed and key not in allowed:
                fail(
                    "op.bad_field",
                    f"{loc}.key 不是 {node.get('fn')} 的参数：{key}"
                    f"（可用：{', '.join(sorted(allowed))}）",
                    loc=loc,
                    node_id=node_id,
                )
                continue
            value = raw.get("value")
            node["args"] = normalize_args(
                str(node.get("fn")), {**node.get("args", {}), key: value}
            )
            continue

        if kind == "set_edge":
            source = resolve(raw.get("from"))
            target = resolve(raw.get("to"))
            when = raw.get("when")
            known = {str(node.get("id")) for node in nodes} | {START, END}
            if source not in known or target not in known:
                fail("op.target_missing", f"{loc} 边引用了不存在的节点：{source}→{target}", loc=loc)
                continue
            edges = workflows[workflow_name]["edges"]
            new_edge: list[Any] = [source, target] + ([str(when)] if when else [])
            if new_edge not in edges:
                edges.append(new_edge)
            continue

        if kind == "remove_edge":
            source = resolve(raw.get("from"))
            target = resolve(raw.get("to"))
            when = raw.get("when")
            edges = workflows[workflow_name]["edges"]
            kept = [
                edge for edge in edges
                if not (
                    split_edge(edge)[0] == source
                    and split_edge(edge)[1] == target
                    and (when is None or split_edge(edge)[2] == str(when))
                )
            ]
            if len(kept) == len(edges):
                fail("op.target_missing", f"{loc} 边不存在：{source}→{target}", loc=loc)
                continue
            workflows[workflow_name]["edges"] = kept
            continue

    if issues:
        return copy.deepcopy(draft), issues, {}
    return working, [], created


def draft_issues(
    draft: dict[str, Any],
    tool_names: set[str] | None = None,
) -> list[Issue]:
    """草稿的全部结构问题，带 code / node_id / loc。

    规则只来自 ``spec_check``；编辑器不再自己重算"必填参数"——``validate_args``
    已经覆盖缺键，这里只补"键在、但值是空串/空列表"这一种，且复用同一个 code。
    """
    try:
        spec = spec_from_draft(draft)
    except SpecError as exc:
        return [
            Issue(
                "spec.parse_failed",
                error,
                hint="检查字段类型；未知字段会被 schema 拒绝",
            )
            for error in exc.errors
        ]
    issues = list(check_spec(spec, tool_names=tool_names))
    for workflow_name, workflow in spec.workflows.items():
        index = {node.id: position for position, node in enumerate(workflow.nodes)}
        for node in workflow.nodes:
            for param in block_params(node.fn):
                if not param.required or param.name not in node.args:
                    continue
                value = node.args.get(param.name)
                if value not in (None, "", []):
                    continue
                issues.append(Issue(
                    "node.arg_invalid",
                    f"workflow[{workflow_name}].{node.id}：{node.fn}.{param.name} 是必填参数",
                    node_id=node.id,
                    workflow=workflow_name,
                    loc=f"workflows.{workflow_name}.nodes[{index.get(node.id, 0)}].args.{param.name}",
                ))
    return issues


def draft_errors(draft: dict[str, Any], tool_names: set[str] | None = None) -> list[str]:
    """兼容壳：只返回 ERROR 的 message（旧 UI / 测试依赖字符串列表）。"""
    return messages(draft_issues(draft, tool_names))


def save_draft(
    draft: dict[str, Any],
    root: str | None = None,
    tool_names: set[str] | None = None,
    *,
    expected_version: int | None = None,
) -> str:
    """校验并写新版本；校验不过直接抛 ``SpecError``，不落盘。

    ``expected_version`` 是保存时依据的 active 版本：``None`` 表示"新建场景"，
    整数表示"我读到的是 vN"；与磁盘不一致时抛 ``SpecConflict``。
    """
    spec = spec_from_draft(draft)
    issues = draft_issues(draft, tool_names)
    errors = messages(issues)
    if errors:
        raise SpecError(errors, issues)
    path = (
        save_spec(spec, expected_version=expected_version)
        if root is None
        else save_spec(spec, root, expected_version=expected_version)
    )
    return str(path)


def versions(name: str, root: str | None = None) -> list[dict[str, Any]]:
    """列出该场景的版本文件，标出哪个是 active。"""
    directory = (SPEC_ROOT if root is None else Path(root)) / name
    if not directory.is_dir():
        return []
    active = None
    pointer = directory / "active.json"
    if pointer.is_file():
        try:
            active = json.loads(pointer.read_text(encoding="utf-8")).get("file")
        except (OSError, ValueError):
            active = None
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("v*.json")):
        rows.append({
            "版本": path.stem,
            "当前": path.name == active,
            "文件": path.name,
        })
    return rows


def load_version(name: str, filename: str, root: str | None = None) -> dict[str, Any]:
    """读某个历史版本成草稿，用于"载入旧版本"。"""
    directory = (SPEC_ROOT if root is None else Path(root)) / name
    spec = load_spec(directory / filename)
    return draft_from_spec(spec)


# ---------------------------------------------------------------- 节点级语义 diff
#
# diff 只回答"改了什么结构"：节点 id、``fn``、``args`` 逐键，边只比
# ``(from, to, when)`` 集合。坐标在 ``LAYOUT_ROOT``（spec 之外），
# ``contract`` / ``contract_shape`` 是保存时按块自动填的派生字段——两者都不是
# "人改了结构"，比进来只会制造假改动。


@dataclass
class SpecDiff:
    """一份结构相对另一份的差异；节点级改动在 ``added/removed/changed``。"""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: node_id -> 变了的字段，例如 ``["fn", "args.limit"]``；
    #: 和 ``published_nodes``（只有摘要）比时是 ``["fn+args"]``。
    changed: dict[str, list[str]] = field(default_factory=dict)
    workflows_added: list[str] = field(default_factory=list)
    workflows_removed: list[str] = field(default_factory=list)
    #: 边只比集合，格式 ``"qa:search->answer"`` / ``"qa:branch->done[yes]"``。
    edges_added: list[str] = field(default_factory=list)
    edges_removed: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.added
            or self.removed
            or self.changed
            or self.workflows_added
            or self.workflows_removed
            or self.edges_added
            or self.edges_removed
        )

    @property
    def changed_count(self) -> int:
        """节点级改动总数（新增 + 删除 + 修改）。"""
        return len(self.added) + len(self.removed) + len(self.changed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": {node_id: list(fields) for node_id, fields in self.changed.items()},
            "workflows_added": list(self.workflows_added),
            "workflows_removed": list(self.workflows_removed),
            "edges_added": list(self.edges_added),
            "edges_removed": list(self.edges_removed),
        }


def node_digest(fn: str, args: dict[str, Any] | None = None) -> str:
    """``fn + args`` 的稳定摘要：``sha256(json sort_keys)[:12]``。

    发布时按节点存这个值，之后拿 active spec 重算就能判断节点改没改，
    不必在 entry.yml 里再存一份结构副本。
    """
    payload = {"fn": str(fn or ""), "args": dict(args or {})}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]


def node_digests(spec: dict[str, Any]) -> dict[str, str]:
    """草稿 -> ``{node_id: 摘要}``，供 ``entry.publish`` 落盘。"""
    return {
        node_id: node_digest(node["fn"], node.get("args") or {})
        for node_id, node in _spec_nodes(spec).items()
    }


def _is_published_map(data: Any) -> bool:
    """``published_nodes`` 是 ``{node_id: 摘要}``；草稿一定带 ``workflows``。"""
    return isinstance(data, dict) and "workflows" not in data


def _spec_nodes(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """草稿里所有工作流的节点，按 node_id 摊平；只留 diff 关心的 ``fn`` / ``args``。"""
    nodes: dict[str, dict[str, Any]] = {}
    for workflow in (data.get("workflows") or {}).values():
        if not isinstance(workflow, dict):
            continue
        for node in workflow.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "")
            if not node_id:
                continue
            nodes[node_id] = {
                "fn": str(node.get("fn") or ""),
                "args": dict(node.get("args") or {}),
            }
    return nodes


def _node_views(data: Any) -> dict[str, dict[str, Any]]:
    """把草稿或已发布摘要统一成 ``node_id -> 视图``。"""
    if not isinstance(data, dict):
        return {}
    if _is_published_map(data):
        return {
            str(node_id): {"digest": str(digest or "")}
            for node_id, digest in data.items()
        }
    return _spec_nodes(data)


def _changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """一个节点改在哪些字段；两边都只有摘要时只能报 ``fn+args``。"""
    if "digest" in old or "digest" in new:
        old_digest = old.get("digest") or node_digest(old.get("fn", ""), old.get("args"))
        new_digest = new.get("digest") or node_digest(new.get("fn", ""), new.get("args"))
        return [] if old_digest == new_digest else ["fn+args"]

    fields: list[str] = []
    if old.get("fn") != new.get("fn"):
        fields.append("fn")
    old_args = dict(old.get("args") or {})
    new_args = dict(new.get("args") or {})
    for key in sorted(set(old_args) | set(new_args)):
        if key not in old_args or key not in new_args or old_args[key] != new_args[key]:
            fields.append(f"args.{key}")
    return fields


def _edge_set(workflow: Any) -> set[tuple[str, str, str | None]]:
    if not isinstance(workflow, dict):
        return set()
    return {split_edge(edge) for edge in (workflow.get("edges") or [])}


def _edge_label(workflow: str, edge: tuple[str, str, str | None]) -> str:
    source, target, when = edge
    return f"{workflow}:{source}->{target}" + (f"[{when}]" if when else "")


def diff_spec(old: dict[str, Any], new: dict[str, Any]) -> SpecDiff:
    """比较两份结构，返回节点级差异。

    ``old`` / ``new`` 可以是草稿 dict，也可以是 ``entry.published_nodes``
    （``{node_id: fn+args 摘要}``）：发布页拿当前 active 直接和发布快照比，
    不用为了做 diff 重建旧 spec。

    - 节点比 ``fn`` 与 ``args``（逐键）；``args`` 新增/删除一个键也算改；
    - 边只比 ``(from, to, when)`` 集合；
    - 不比坐标，忽略 ``contract`` / ``contract_shape``。
    """
    old_nodes = _node_views(old)
    new_nodes = _node_views(new)

    added = sorted(set(new_nodes) - set(old_nodes))
    removed = sorted(set(old_nodes) - set(new_nodes))
    changed: dict[str, list[str]] = {}
    for node_id in sorted(set(old_nodes) & set(new_nodes)):
        fields = _changed_fields(old_nodes[node_id], new_nodes[node_id])
        if fields:
            changed[node_id] = fields

    workflows_added: list[str] = []
    workflows_removed: list[str] = []
    edges_added: list[str] = []
    edges_removed: list[str] = []
    # 和发布摘要比时没有工作流信息，只报节点级差异。
    if not _is_published_map(old) and not _is_published_map(new):
        old_workflows = old.get("workflows") or {}
        new_workflows = new.get("workflows") or {}
        workflows_added = sorted(set(new_workflows) - set(old_workflows))
        workflows_removed = sorted(set(old_workflows) - set(new_workflows))
        for workflow_name in sorted(set(old_workflows) & set(new_workflows)):
            before = _edge_set(old_workflows[workflow_name])
            after = _edge_set(new_workflows[workflow_name])
            edges_added.extend(
                _edge_label(workflow_name, edge)
                for edge in sorted(
                    after - before, key=lambda item: (item[0], item[1], item[2] or "")
                )
            )
            edges_removed.extend(
                _edge_label(workflow_name, edge)
                for edge in sorted(
                    before - after, key=lambda item: (item[0], item[1], item[2] or "")
                )
            )

    return SpecDiff(
        added=added,
        removed=removed,
        changed=changed,
        workflows_added=workflows_added,
        workflows_removed=workflows_removed,
        edges_added=sorted(edges_added),
        edges_removed=sorted(edges_removed),
    )


# ---------------------------------------------------------------- 画布：布局与合并
#
# 画布只负责"拓扑 + 位置"，节点身份与参数仍在草稿里：
# - 位置不进 spec（会被 schema 拒绝），放 data/runtime/spec_layout/<name>.json；
# - 画布回传的状态先过 merge_canvas 才落进草稿，非法节点 / 悬空边直接丢掉。


def _levels(nodes: list[dict], edges: list[list[str]]) -> dict[str, int]:
    """按"从 START 出发的最长路径"给节点分层；有环也能收敛（重复访问返回 0）。"""
    ids = [str(node.get("id")) for node in nodes]
    parents: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in edges:
        source, target, _when = split_edge(edge)
        if source in parents and target in parents:
            parents[target].append(source)
    levels: dict[str, int] = {}

    def depth(node: str, seen: set[str]) -> int:
        if node in levels:
            return levels[node]
        if node in seen:
            return 0
        seen.add(node)
        found = [depth(parent, seen) + 1 for parent in parents.get(node, [])]
        levels[node] = max(found) if found else 0
        return levels[node]

    for node_id in ids:
        depth(node_id, set())
    return levels


def auto_layout(nodes: list[dict], edges: list[list[str]]) -> dict[str, list[int]]:
    """没有历史位置时用的自动布局：按层从左到右，同层从上到下。"""
    levels = _levels(nodes, edges)
    rows: dict[int, int] = {}
    positions: dict[str, list[int]] = {}
    for node in nodes:
        node_id = str(node.get("id"))
        level = levels.get(node_id, 0)
        row = rows.get(level, 0)
        rows[level] = row + 1
        positions[node_id] = [
            ORIGIN[0] + level * COL_GAP,
            ORIGIN[1] + row * ROW_GAP,
        ]
    return positions


def layout_path(name: str, root: str | Path | None = None) -> Path:
    base = LAYOUT_ROOT if root is None else Path(root)
    return base / f"{name}.json"


def load_layout(name: str, root: str | Path | None = None) -> dict[str, dict[str, list[int]]]:
    """读回上次拖动的位置；文件缺失或坏了都返回空。"""
    path = layout_path(name, root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, dict[str, list[int]]] = {}
    for workflow, positions in data.items():
        if not isinstance(positions, dict):
            continue
        cleaned[str(workflow)] = {
            str(node_id): [int(value[0]), int(value[1])]
            for node_id, value in positions.items()
            if isinstance(value, (list, tuple)) and len(value) == 2
        }
    return cleaned


def save_layout(
    name: str,
    workflow: str,
    positions: dict[str, list[int]],
    root: str | Path | None = None,
) -> Path:
    """把某个工作流的节点位置写盘（其余工作流保留）。"""
    path = layout_path(name, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_layout(name, root)
    data[workflow] = {
        str(node_id): [int(value[0]), int(value[1])]
        for node_id, value in positions.items()
    }
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return path


def node_positions(
    name: str,
    workflow: str,
    nodes: list[dict],
    edges: list[list[str]],
    root: str | Path | None = None,
) -> dict[str, list[int]]:
    """历史位置优先，缺的节点用自动布局补。"""
    saved = load_layout(name, root).get(workflow, {})
    auto = auto_layout(nodes, edges)
    return {
        str(node.get("id")): list(saved.get(str(node.get("id"))) or auto.get(str(node.get("id")), list(ORIGIN)))
        for node in nodes
    }


def merge_canvas(
    draft: dict[str, Any],
    workflow: str,
    payload: Any,
    clipboard: dict[str, dict] | None = None,
) -> bool:
    """把画布回传的状态并进草稿；返回是否真的改了结构。

    画布是"不可信输入"：未知块、重复 id、悬空边、空节点列表全部丢掉，
    只接受 ``{nodes: [{id, fn, x, y}], edges: [[from, to]]}``。

    ``clipboard`` 只用于 Ctrl+X 后的粘贴：被剪掉的源节点已不在草稿里，
    但它的参数要从 Python 侧快照恢复，画布本身不传参数。
    """
    if not isinstance(payload, dict):
        return False
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return False
    current = {
        str(node.get("id")): node
        for node in draft["workflows"][workflow]["nodes"]
    }
    merged: list[dict[str, Any]] = []
    used: set[str] = set()
    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "").strip()
        fn = str(item.get("fn") or "")
        if (
            not node_id
            or node_id in used
            or node_id in {START, END}
            or fn not in NODE_REGISTRY
        ):
            continue
        previous = current.get(node_id)
        copy_of = str(item.get("copy_of") or "")
        source = current.get(copy_of) or (clipboard or {}).get(copy_of)
        if previous is not None and str(previous.get("fn")) == fn:
            args = dict(previous.get("args") or {})
        elif source is not None and str(source.get("fn")) == fn:
            # 画布复制/粘贴只传 copy_of，不传参数；参数在 Python 侧从源节点复制，
            # 既保留配置，又不让画布直接改参数。
            args = copy.deepcopy(source.get("args") or {})
        else:
            args = block_defaults(fn)
        merged.append({"id": node_id, "fn": fn, "args": args})
        used.add(node_id)
    if not merged:
        return False

    known = used | {START, END}
    # 画布只画 from→to，不表达条件；条件边原样保留（按 from/to 配对找回 when）。
    conditions: dict[tuple[str, str], str | None] = {}
    for edge in draft["workflows"][workflow]["edges"]:
        source, target, when = split_edge(edge)
        conditions[(source, target)] = when
    raw_edges = payload.get("edges")
    edges: list[list[str]] = []
    for edge in raw_edges if isinstance(raw_edges, list) else []:
        if not isinstance(edge, (list, tuple)) or len(edge) < 2:
            continue
        source, target = str(edge[0]), str(edge[1])
        if source not in known or target not in known:
            continue
        when = edge[2] if len(edge) >= 3 else conditions.get((source, target))
        item = [source, target] if when in (None, "") else [source, target, str(when)]
        if item not in edges:
            edges.append(item)

    changed = (
        merged != draft["workflows"][workflow]["nodes"]
        or edges != draft["workflows"][workflow]["edges"]
    )
    if changed:
        draft["workflows"][workflow]["nodes"] = merged
        draft["workflows"][workflow]["edges"] = edges
    return changed


def bad_node_ids(issues: list[Any], nodes: list[dict] | None = None) -> list[str]:
    """从 Issue 列表里取"这条错属于哪个节点"，给画布标红用。

    只读 ``Issue.node_id``，不再用正则猜。传入旧字符串列表时返回 ``[]``
    （宁可少标，也不误标）。
    """
    bad: list[str] = []
    for issue in issues or ():
        node_id = getattr(issue, "node_id", None)
        if not node_id:
            continue
        text = str(node_id)
        if text not in bad:
            bad.append(text)
    return bad


def canvas_payload(
    name: str,
    workflow: str,
    draft: dict[str, Any],
    errors: list[str] | None = None,
    root: str | Path | None = None,
    selected: str | None = None,
    include_blocks: bool = True,
    selection: list[str] | None = None,
    issues: list[Issue] | None = None,
) -> dict[str, Any]:
    """给前端画布的完整输入：节点（含中文名与坐标）、边、块库、标红节点。

    ``include_blocks=False`` 时前端隐藏自带块库，由外层工作台提供（三栏布局）。
    ``issues`` 优先；``errors`` 只做旧字符串接口的兼容（无 node_id，不会标红）。
    """
    issue_list = list(issues or [])
    issue_list.extend(Issue("legacy.error", str(text)) for text in (errors or []))
    workflow_draft = draft["workflows"][workflow]
    nodes = list(workflow_draft["nodes"])
    edges = []
    for edge in workflow_draft["edges"]:
        source, target, when = split_edge(edge)
        edges.append([source, target] if not when else [source, target, when])
    positions = node_positions(name, workflow, nodes, edges, root)

    canvas_nodes = [
        {
            "id": str(node.get("id")),
            "fn": str(node.get("fn")),
            "label": block_label(str(node.get("fn"))),
            "x": positions[str(node.get("id"))][0],
            "y": positions[str(node.get("id"))][1],
        }
        for node in nodes
    ]
    xs = [item["x"] for item in canvas_nodes] or [0]
    ys = [item["y"] for item in canvas_nodes] or [0]
    saved = load_layout(name, root).get(workflow, {})
    # 保存过就原样还原：START/END 被拖到画布外也不能夹回去，否则松手会"回弹"。
    start_pos = saved.get(START) or [max(0, min(xs) - NODE_W - 40), int(sum(ys) / len(ys))]
    end_pos = saved.get(END) or [max(xs) + NODE_W + 40, int(sum(ys) / len(ys))]
    canvas_nodes.insert(0, {
        "id": START, "fn": "", "label": "START",
        "x": int(start_pos[0]), "y": int(start_pos[1]), "kind": "start",
    })
    canvas_nodes.append({
        "id": END, "fn": "", "label": "END",
        "x": int(end_pos[0]), "y": int(end_pos[1]), "kind": "end",
    })

    return {
        "nodes": canvas_nodes,
        "edges": edges,
        "blocks": [
            {"fn": fn, "label": block_label(fn)}
            for fn in NODE_REGISTRY
        ],
        "show_palette": bool(include_blocks),
        "errors": bad_node_ids(issue_list),
        "selected": selected if selected and selected not in {START, END} else None,
        "selection": [str(item) for item in (selection or []) if str(item)],
        "workflow": workflow,
    }


def absorb_canvas(
    name: str,
    workflow: str,
    draft: dict[str, Any],
    payload: Any,
    root: str | Path | None = None,
    clipboard: dict[str, dict] | None = None,
) -> bool:
    """吸收一次画布回传：合并结构 + 记住位置；返回结构是否变化。"""
    changed = merge_canvas(draft, workflow, payload, clipboard=clipboard)
    if not isinstance(payload, dict):
        return changed
    positions: dict[str, list[int]] = {}
    for item in payload.get("nodes") or []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "")
        if not node_id:
            continue
        try:
            positions[node_id] = [int(item.get("x")), int(item.get("y"))]
        except (TypeError, ValueError):
            continue
    anchors = payload.get("anchors")
    if isinstance(anchors, dict):
        for node_id, value in anchors.items():
            if isinstance(value, (list, tuple)) and len(value) == 2:
                positions[str(node_id)] = [int(value[0]), int(value[1])]
    if positions:
        save_layout(name, workflow, positions, root)
    return changed


def clear_layout(name: str, workflow: str, root: str | Path | None = None) -> None:
    """清掉某工作流的位置缓存，下次渲染回到自动布局。"""
    data = load_layout(name, root)
    if workflow in data:
        data.pop(workflow, None)
        path = layout_path(name, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )

