# -*- coding: utf-8 -*-
"""场景写通道 MCP server：读 / 体检 / 提议 / 落盘。

与 ``resource_server`` 的分工：

- ``resource_server`` 是**只读**资源目录（工具 / 工作流 / 场景 / 块）；
- 本 server 是**唯一写入口**：只写 ``data/specs/<scenario>/v*.json``，且必须
  经过 ``spec_edit.apply_ops`` → ``spec_check`` → ``save_spec(expected_version=...)``。

刻意不提供：整份 spec 覆盖（``spec_write``）、新建场景、运行/命令执行。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from core.paths import SPEC_ROOT
from modules.editor import spec_edit
from modules.editor.ownership import OwnershipError, assert_spec_owned, load_state
from modules.scenario_format.spec import (
    SpecConflict,
    SpecError,
    load_active_versioned,
    save_spec,
    spec_to_dict,
)
from modules.scenario_format.spec_check import Issue, Severity, check_spec, messages
from modules.scenario_format.spec_nodes import block_catalog as _block_catalog

mcp = FastMCP("spec")

NAME_RE = re.compile(r"^[a-z0-9_]+$")
#: 单次 apply 的语义操作上限；超过直接报 payload_too_large，避免一次改半张图。
MAX_OPS = 64


def _root() -> Path:
    """每次现读 ``AH_SPEC_ROOT``，测试/外部进程可用临时目录。"""
    value = os.environ.get("AH_SPEC_ROOT", "").strip()
    return Path(value) if value else Path(SPEC_ROOT)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(code: str, message: str, **extra: Any) -> str:
    return _dumps({"ok": False, "code": code, "error": message, **extra})


def _check_name(scenario: str) -> str:
    name = str(scenario or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(f"场景名不合法：{scenario!r}（只允许小写字母、数字、下划线）")
    return name


def _issues(spec, *, only_error: bool = False, tool_names: set[str] | None = None) -> list[dict[str, Any]]:
    items = check_spec(spec, tool_names=tool_names)
    if only_error:
        items = [issue for issue in items if issue.severity is Severity.ERROR]
    return [issue.to_dict() for issue in items]


def _ops(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, str):
        if not raw.strip():
            return []
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raise ValueError("ops 必须是数组")
    return raw


@mcp.tool()
def spec_schema() -> str:
    """返回 ScenarioSpec / NodeSpec 的 JSON schema 与当前 SPEC_VERSION。"""
    from core.scenarios.spec import NodeSpec, ScenarioSpec
    from core.scenarios.spec_check import SPEC_VERSION

    return _dumps({
        "ok": True,
        "spec_version": SPEC_VERSION,
        "scenario": ScenarioSpec.model_json_schema(),
        "node": NodeSpec.model_json_schema(),
        "ops": list(spec_edit.OP_NAMES),
    })


@mcp.tool()
def spec_block_catalog(fn: str = "") -> str:
    """返回块库的完整明细（参数契约 + 静态端口）；``fn`` 非空只返回该块。

    只读：不写盘、不构建场景、不调模型。``fn`` 为空返回 ``blocks`` 数组，
    给了 ``fn`` 返回 ``block`` 单块；未知 ``fn`` 返回 ``{"ok": false, "code": "not_found"}``。
    """
    try:
        key = str(fn or "").strip()
        blocks = _block_catalog()
        if not key:
            return _dumps({"ok": True, "count": len(blocks), "blocks": blocks})
        for block in blocks:
            if block["fn"] == key:
                return _dumps({"ok": True, "block": block})
        return _error("not_found", f"未注册的块：{key}", fn=key)
    except Exception as exc:  # noqa: BLE001 - MCP 工具返回结构化错误
        return _error("error", f"{type(exc).__name__}: {exc}")


@mcp.tool()
def spec_read(
    scenario: str,
    workflow: str = "",
    nodes_offset: int = 0,
    nodes_limit: int = 200,
) -> str:
    """读当前 active spec + 版本 + 校验问题（不写盘）。"""
    try:
        name = _check_name(scenario)
        root = _root()
        try:
            spec, version, _path = load_active_versioned(name, root)
        except SpecError as exc:
            return _error("spec_corrupt", str(exc))
        if spec is None:
            return _error("not_found", f"没有 active spec：{root / name / 'active.json'}")
        payload = spec_to_dict(spec)
        if workflow:
            workflows = payload.get("workflows") or {}
            if workflow not in workflows:
                return _error("not_found", f"工作流不存在：{workflow}")
            chosen = {workflow: workflows[workflow]}
            nodes = chosen[workflow].get("nodes") or []
            start = max(0, int(nodes_offset or 0))
            limit = max(1, int(nodes_limit or 200))
            chosen[workflow]["nodes"] = nodes[start:start + limit]
            payload["workflows"] = chosen
        state = load_state(name, root)
        return _dumps({
            "ok": True,
            "owner": (state.owner if state else "spec"),
            "version": version,
            "spec": payload,
            "issues": _issues(spec, tool_names=set(spec.tools)),
            # P1-10 之后才有值；现在显式返回 null，不留"无人填的字段"。
            "diff_since_published": None,
        })
    except Exception as exc:  # noqa: BLE001 - MCP 工具返回结构化错误
        return _error("error", f"{type(exc).__name__}: {exc}")


@mcp.tool()
def spec_check(scenario: str, ops: list[dict[str, Any]] | None = None) -> str:
    """体检当前 active；给了 ops 就校候选（仍然不写盘）。"""
    try:
        name = _check_name(scenario)
        root = _root()
        spec, current, _path = load_active_versioned(name, root)
        if spec is None:
            return _error("not_found", f"没有 active spec：{name}")
        if ops is None:
            issues = _issues(spec, tool_names=set(spec.tools))
            return _dumps({
                "ok": not any(i["severity"] == "error" for i in issues),
                "version": current,
                "issues": issues,
                "drift": [],
            })
        draft = spec_edit.draft_from_spec(spec)
        candidate, op_issues, created = spec_edit.apply_ops(draft, _ops(ops))
        if op_issues:
            return _dumps({"ok": False, "code": "bad_op", "issues": [i.to_dict() for i in op_issues]})
        try:
            candidate_spec = spec_edit.spec_from_draft(candidate)
        except SpecError as exc:
            return _dumps({"ok": False, "code": "validation_failed", "issues": [i.to_dict() for i in exc.issues]})
        issues = _issues(candidate_spec, tool_names=set(candidate_spec.tools))
        return _dumps({
            "ok": not any(i["severity"] == "error" for i in issues),
            "version": current,
            "issues": issues,
            "created": created,
        })
    except Exception as exc:  # noqa: BLE001
        return _error("error", f"{type(exc).__name__}: {exc}")


@mcp.tool()
def spec_propose(scenario: str, ops: list[dict[str, Any]]) -> str:
    """把 ops 应用到候选草稿并返回结果；**不写盘**。"""
    try:
        name = _check_name(scenario)
        root = _root()
        spec, current, _path = load_active_versioned(name, root)
        if spec is None:
            return _error("not_found", f"没有 active spec：{name}")
        draft = spec_edit.draft_from_spec(spec)
        candidate, op_issues, created = spec_edit.apply_ops(draft, _ops(ops))
        if op_issues:
            return _dumps({
                "ok": False,
                "code": "bad_op",
                "issues": [i.to_dict() for i in op_issues],
                "node_id_map": {},
            })
        try:
            candidate_spec = spec_edit.spec_from_draft(candidate)
        except SpecError as exc:
            return _dumps({
                "ok": False,
                "code": "validation_failed",
                "issues": [i.to_dict() for i in exc.issues],
                "candidate": candidate,
                "node_id_map": created,
            })
        return _dumps({
            "ok": True,
            "version": current,
            "candidate": candidate,
            "issues": _issues(candidate_spec, tool_names=set(candidate_spec.tools)),
            "node_id_map": created,
            "diff": None,  # P1-10 补
        })
    except Exception as exc:  # noqa: BLE001
        return _error("error", f"{type(exc).__name__}: {exc}")


@mcp.tool()
def spec_apply(scenario: str, ops: list[dict[str, Any]], expected_version: int | None) -> str:
    """校验并落盘一个新版本；``expected_version`` 必须来自最近的 ``spec_read``。"""
    try:
        name = _check_name(scenario)
        root = _root()
        # 先判存在：对不存在的场景连 state.json 都不该创建（写盘零）。
        if not (root / name / "active.json").is_file():
            return _error("not_found", f"没有 active spec：{name}（P0 不支持新建场景）")
        try:
            assert_spec_owned(name, root)
        except OwnershipError as exc:
            return _error("ownership", str(exc))
        try:
            spec, current, _path = load_active_versioned(name, root)
        except SpecError as exc:
            return _error("spec_corrupt", str(exc))
        if spec is None:
            return _error("not_found", f"没有 active spec：{name}（P0 不支持新建场景）")
        draft = spec_edit.draft_from_spec(spec)
        raw_ops = _ops(ops)
        if not raw_ops:
            return _dumps({
                "ok": False,
                "code": "bad_op",
                "issues": [
                    Issue(
                        "op.unknown",
                        "ops 不能为空：apply 必须至少包含一个语义操作",
                        loc="ops",
                    ).to_dict()
                ],
            })
        if len(raw_ops) > MAX_OPS:
            return _error("payload_too_large", f"单次 ops 上限 {MAX_OPS} 条，收到 {len(raw_ops)}")
        candidate, op_issues, created = spec_edit.apply_ops(draft, raw_ops)
        if op_issues:
            return _dumps({
                "ok": False,
                "code": "bad_op",
                "issues": [i.to_dict() for i in op_issues],
            })
        try:
            candidate_spec = spec_edit.spec_from_draft(candidate)
        except SpecError as exc:
            return _dumps({
                "ok": False,
                "code": "validation_failed",
                "issues": [i.to_dict() for i in exc.issues],
            })
        errors = messages(check_spec(candidate_spec, tool_names=set(candidate_spec.tools)))
        if errors:
            return _dumps({
                "ok": False,
                "code": "validation_failed",
                "issues": _issues(
                    candidate_spec, only_error=True, tool_names=set(candidate_spec.tools)
                ),
            })
        try:
            path = save_spec(candidate_spec, root=root, expected_version=expected_version)
        except SpecConflict as exc:
            return _dumps({
                "ok": False,
                "code": "conflict",
                "current_version": exc.current_version,
                "reason": exc.reason,
                "changed_nodes": exc.changed_nodes,
            })
        return _dumps({
            "ok": True,
            "version": int(path.stem[1:]),
            "file": path.name,
            "created": created,
            "issues": _issues(candidate_spec, tool_names=set(candidate_spec.tools)),
        })
    except Exception as exc:  # noqa: BLE001
        return _error("error", f"{type(exc).__name__}: {exc}")
