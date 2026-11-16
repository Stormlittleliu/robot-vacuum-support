# -*- coding: utf-8 -*-
"""spec 驱动的场景定义：受约束的伪低代码。

它只做三件事：

1. 校验一份 spec（节点/边/函数引用是否成立）；
2. 把 spec 编译成 LangGraph（``StateGraph``）；
3. 版本化读写 spec（``data/specs/<scenario>/vN.json`` + ``active.json``）。

它**不**做的事（这是刻意的）：

- 不把现有代码场景序列化成 spec；
- 不追求任意图 -> spec -> 图的完备往返；
- 删节点后不自动重连，悬空边直接报错。

所以 spec 是"新增场景的一种方式"，不是"所有场景的格式"。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.paths import SPEC_ROOT as _SPEC_ROOT
from core.prompts.blocks import PromptBook, validate_book
from core.prompts.assemble import context_from_scenario, get_assembled
from modules.scenario_format.edges import END, START
from modules.scenario_format.spec_check import (
    Issue,
    SPEC_VERSION,
    check_port_flow,
    check_spec,
    edge_pair,
    messages,
    split_edge,
)
from modules.scenario_format.spec_nodes import (
    NODE_REGISTRY,
    ResolvedPorts,
    block_ports,
    contract_fingerprint,
    contract_shape,
)

SPEC_ROOT = _SPEC_ROOT
ACTIVE_FILE = "active.json"


class SpecError(ValueError):
    """spec 不合法；``errors`` 是逐条可展示的原因，``issues`` 是可定位的版本。"""

    def __init__(self, errors: list[str], issues: list[Issue] | None = None):
        self.errors = list(errors)
        self.issues = list(issues or [])
        super().__init__("；".join(self.errors))


class SpecState(TypedDict, total=False):
    """spec 场景只有一个通用 state，节点只能读写这几个键。"""

    input: str
    tool_result: str
    answer: str
    scratch: dict


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    fn: str
    args: dict[str, Any] = Field(default_factory=dict)
    # 块契约指纹 + 骨架：由 save_spec 统一填充，人和 agent 都不手写。
    contract: str = ""
    contract_shape: dict[str, Any] | None = None


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["text", "number", "boolean", "json"] = "text"
    required: bool = False
    default: Any = None
    title: str = ""
    description: str = ""


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["text", "number", "boolean", "json"] = "text"
    title: str = ""
    description: str = ""


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[NodeSpec] = Field(default_factory=list)
    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    # 边：["from","to"] 或 ["from","to","when"]。带第三项的是条件边，
    # 由 branch 块写的 scratch.route 决定走哪条；旧 spec（两项）照旧能读。
    edges: list[Any] = Field(default_factory=list)


def split_edge(edge: Any) -> tuple[str, str, str | None]:
    """把一条边拆成 ``(source, target, when)``；非法形状按无条件处理。"""
    if isinstance(edge, dict):
        return str(edge.get("from") or ""), str(edge.get("to") or ""), edge.get("when")
    if isinstance(edge, (list, tuple)) and len(edge) >= 2:
        when = edge[2] if len(edge) >= 3 else None
        return str(edge[0]), str(edge[1]), None if when in (None, "") else str(when)
    return "", "", None


def edge_pair(edge: Any) -> tuple[str, str]:
    source, target, _ = split_edge(edge)
    return source, target


class HarnessSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable_todo: bool = False
    enable_summarization: bool = True
    tool_call_limit: int | None = None
    token_budget: int | None = None


class PolicySpecModel(BaseModel):
    """策略覆盖项：默认开启、显式移除、参数可配。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class PromptSpec(BaseModel):
    """系统提示词的分块引用：块本身在 ``data/specs/<name>/prompt.yml``。"""

    model_config = ConfigDict(extra="forbid")

    blocks: list[str] = Field(default_factory=list)
    overrides: dict[str, str] = Field(default_factory=dict)


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SPEC_VERSION
    name: str
    description: str = ""
    # 场景类型：agent / router（router 靠 planning 规则分流，界面上多一个路由配置面板）
    kind: str = "agent"
    tools: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    # 分块提示词：给了 blocks 就按块拼接（覆盖 system_prompt）；否则用 system_prompt 原文。
    prompt: PromptSpec | None = None
    # 行为用例文件（相对仓库根）；留空则找 data/specs/<name>/cases.jsonl。
    # 仿造现有场景时指向原来的 cases，保证两边是同一把尺子。
    cases_path: str | None = None
    # 结构化输出：schemas.py 里登记的名字（如 "ReportSchema"），空表示不用。
    response_format: str | None = None
    harness: HarnessSpec = Field(default_factory=HarnessSpec)
    policies: list[PolicySpecModel] | None = None
    workflows: dict[str, WorkflowSpec] = Field(default_factory=dict)
    # 候选 spec 的来源（pattern id / run ids / mining id）；active spec 通常为空。
    # 这是可选的，旧 spec 不需要补字段。
    provenance: dict[str, Any] | None = None


# ---------------------------------------------------------------- 校验


def validate_port_flow(
    prefix: str,
    nodes: list[Any],
    edges: list[Any],
    *,
    resolve: Callable[[str, dict[str, Any]], ResolvedPorts] = block_ports,
    seed: dict[str, str] | None = None,
) -> list[str]:
    """兼容壳：返回 Issue 的 message 列表（旧测试与调用方依赖 ``list[str]``）。"""
    return messages(check_port_flow(prefix, nodes, edges, resolve=resolve, seed=seed))


def validate_spec(
    spec: ScenarioSpec,
    tool_names: set[str] | None = None,
    prompt_book: PromptBook | None = None,
) -> list[str]:
    """兼容壳：只返回 ERROR 的 message。

    WARNING（旧版本、契约漂移提示等）不阻断构建；需要它们的调用方
    直接用 :func:`core.scenarios.spec_check.check_spec` 并按 severity 过滤。
    """
    return messages(check_spec(spec, tool_names=tool_names, prompt_book=prompt_book))


# ---------------------------------------------------------------- 编译


def build_workflows(spec: ScenarioSpec, scenario: Any) -> dict[str, Any]:
    """兼容壳：把 spec + Scenario 交给 ``compiler.compile_spec``，返回编译好的图。

    编译本身不再要求 Scenario；这里只是把 ``scenario.tools / model`` 抽出来传入。
    """
    from core.scenarios.compiler import compile_spec

    model = getattr(scenario, "model", None)
    if model is None:
        resolve = getattr(scenario, "resolve_execution", None)
        execution = resolve() if callable(resolve) else None
        model = getattr(execution, "model", None) if execution is not None else None
    compiled = compile_spec(
        spec,
        tools=list(getattr(scenario, "tools", None) or []),
        model=model,
    )
    return compiled.graphs


def build_from_spec(
    spec: ScenarioSpec,
    *,
    tools: list[Any],
    model: Any,
    cases_path: Path | None = None,
    checkpoint_backend: str = "memory",
    checkpoint_path: str = "data/runtime/spec_checkpoints.sqlite",
    runtime: Any = None,
    response_format: Any = None,
    defaults: dict[str, Any] | None = None,
    prompt_book: PromptBook | None = None,
) -> Any:
    """用 spec 组装一个 ``Scenario``；工具按 ``spec.tools`` 白名单过滤。

    ``defaults`` 是代码层给的覆盖层（tool_policy / high_risk_tools / pii_types /
    context / timeouts / middleware / checkpoint…）。spec 控制结构，defaults 控制
    实现细节；两者冲突时以 spec 为准（结构必须由数据决定）。
    """
    from core.agent.contracts import ExecutionSpec, Scenario

    errors = validate_spec(spec)
    if errors:
        raise SpecError(errors)

    by_name = {getattr(tool, "name", ""): tool for tool in tools}
    missing = [name for name in spec.tools if name not in by_name]
    if missing:
        raise SpecError([f"场景缺少工具：{', '.join(missing)}"])
    # 允许没有工具：白板 / 结构化输出这类场景只有一个 agent 循环。
    selected = [by_name[name] for name in spec.tools]

    # 工具白名单确定后再校验一次：块里引用的 tool_list 必须真的可用。
    errors = validate_spec(spec, tool_names={tool.name for tool in selected})
    if errors:
        raise SpecError(errors)

    harness = spec.harness
    has_react_loop = any(
        node.fn == "react_loop"
        for workflow in spec.workflows.values()
        for node in workflow.nodes
    )
    options: dict[str, Any] = dict(defaults or {})
    # defaults 里属于执行面的键（model / middleware / checkpoint…）收进 ExecutionSpec；
    # 其余才是 Scenario 的结构 / harness 字段。
    execution_overrides = {
        key: options.pop(key)
        for key in list(options)
        if key in ExecutionSpec.__dataclass_fields__
    }
    options.update({
        "name": spec.name,
        "description": spec.description,
        "system_prompt": spec.system_prompt,
        "tools": selected,
        "kind": spec.kind,
        "cases_path": cases_path,
        "enable_todo": harness.enable_todo,
        "enable_summarization": harness.enable_summarization,
        # react_loop 自己持有 max_steps；外层再加一道 tool_call_limit 会把
        # 复杂任务的预算截在更低的值上，且错误看起来像“循环坏了”。
        "tool_call_limit": None if has_react_loop else harness.tool_call_limit,
        "token_budget": harness.token_budget,
        "runtime": runtime,
    })
    options["execution"] = ExecutionSpec(
        model=execution_overrides.pop("model", model),
        middleware=list(execution_overrides.pop("middleware", None) or []),
        response_format=execution_overrides.pop("response_format", response_format),
        workflow_factory=(
            (lambda scenario: build_workflows(spec, scenario)) if spec.workflows else None
        ),
        checkpoint=bool(execution_overrides.pop("checkpoint", True)),
        checkpoint_backend=str(
            execution_overrides.pop("checkpoint_backend", checkpoint_backend)
        ),
        checkpoint_path=str(execution_overrides.pop("checkpoint_path", checkpoint_path)),
        policies=(
            [policy.model_dump() for policy in spec.policies]
            if spec.policies is not None
            else execution_overrides.pop("policies", None)
        ),
    )
    scenario = Scenario(**options)
    # 分块提示词：块能引用 {tools} / {workflows}，所以要等 Scenario 建好再拼。
    if spec.prompt is not None and spec.prompt.blocks:
        book = prompt_book or PromptBook()
        block_errors = validate_book(list(spec.prompt.blocks), book)
        if block_errors:
            raise SpecError(block_errors)
        context = context_from_scenario(scenario, workflows=list(spec.workflows))
        text = get_assembled(
            list(spec.prompt.blocks), book, context, dict(spec.prompt.overrides)
        )
        if text.strip():
            scenario.system_prompt = text
    return scenario


def resolve_cases_path(spec: ScenarioSpec, root: Path | str = SPEC_ROOT) -> Path | None:
    """行为用例路径：spec 显式指定优先，否则找 spec 目录下的 cases.jsonl。"""
    if spec.cases_path:
        candidate = Path(spec.cases_path)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate if candidate.is_file() else None
    fallback = Path(root) / spec.name / "cases.jsonl"
    return fallback if fallback.is_file() else None


# ---------------------------------------------------------------- 读写

#: 哨兵：不做基版本检查（仅兼容旧调用方；`core/` 内由 AST 测试禁止裸调用）。
ANY_VERSION = object()

#: claim 超过这个秒数视为残留，可被下一个写者接管。
CLAIM_STALE_SECONDS = 600

#: 连续保存合并窗口：同一 active 版本在这个时间内再次保存时，
#: 编辑器可以要求覆盖当前版本，而不是继续堆 vN。
COALESCE_WINDOW_SECONDS = 120


class SpecConflict(RuntimeError):
    """写入冲突：基版本已过期，或另一个写者正持有 claim。"""

    def __init__(
        self,
        current_version: int | None,
        changed_nodes: list[str] | None = None,
        reason: str = "stale",
    ):
        self.current_version = current_version
        self.changed_nodes = list(changed_nodes or [])
        self.reason = reason
        if reason == "claim_busy":
            message = "另一个写者正在保存这个场景，请重新读取后再试"
        else:
            message = f"场景已被更新到 v{current_version}，请基于最新版本重试"
        super().__init__(message)


def spec_to_dict(spec: ScenarioSpec) -> dict[str, Any]:
    return spec.model_dump(mode="json")


def spec_from_dict(data: dict[str, Any]) -> ScenarioSpec:
    # 改写旧块名（追加式迁移表）；表为空时是零成本。
    from core.scenarios.migrations import apply_renames

    payload, _rename_failures = apply_renames(dict(data))
    try:
        return ScenarioSpec.model_validate(payload)
    except ValidationError as exc:
        raise SpecError([str(exc)]) from exc


def _version_numbers(directory: Path) -> list[int]:
    return [
        int(path.stem[1:])
        for path in directory.glob("v*.json")
        if path.stem[1:].isdigit()
    ]


def _active_pointer(name: str, root: Path | str = SPEC_ROOT) -> dict[str, Any] | None:
    """只读一次 ``active.json``，返回指针内容；不存在/坏了返回 ``None``。"""
    pointer = Path(root) / name / ACTIVE_FILE
    if not pointer.is_file():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def active_version(name: str, root: Path | str = SPEC_ROOT) -> int | None:
    """当前 active 的版本号；没有指针返回 ``None``。

    版本号取自**指针里的 ``version``**，而不是从文件名反推：指针指向的文件若是
    悬空的，版本号仍然存在，``save_spec(expected_version=None)`` 才不会把
    损坏的 active 当成"新建场景"。
    """
    data = _active_pointer(name, root)
    if data is None:
        return None
    try:
        return int(data.get("version"))
    except (TypeError, ValueError):
        return None


def load_active_versioned(
    name: str,
    root: Path | str = SPEC_ROOT,
) -> tuple[ScenarioSpec | None, int | None, Path | None]:
    """一次读 ``active.json``，返回**同源**的 ``(spec, version, path)``。

    分开调 ``load_active_spec`` 与 ``active_version`` 会在两次读之间被另一个写者
    插入，导致"v3 的内容 + v4 的版本号"；调用方原样带回该版本号就会通过 CAS，
    静默覆盖 v4。所有需要"内容 + 版本"的地方都必须用本函数。
    """
    directory = Path(root) / name
    data = _active_pointer(name, root)
    if data is None:
        return None, None, None
    try:
        version = int(data.get("version"))
    except (TypeError, ValueError):
        version = None
    filename = str(data.get("file") or "")
    path = directory / filename
    if not filename or not path.is_file():
        raise SpecError([
            f"active 指针损坏：{directory / ACTIVE_FILE} 指向不存在的 {filename or '（空）'}"
        ])
    return load_spec(path), version, path


def _claim_is_stale(claim: Path) -> bool:
    try:
        return (time.time() - claim.stat().st_mtime) > CLAIM_STALE_SECONDS
    except OSError:
        return False


def _claim_version(
    directory: Path,
    start: int,
    *,
    expected_version: int | None,
    name: str,
    root: Path | str,
) -> tuple[int, Path]:
    """从 ``start`` 起找一个未被占用、未被写过的版本号并抢占。

    - claim 存在且未过期 → ``SpecConflict(reason="claim_busy")``，**不跳号**
      （跳号会让两个同基版本写者都成功，正是 CAS 要防的）；
    - ``v{n}.json`` 存在且无 claim → 上一次写崩了留下的孤儿版本，跳过；
    - 抢占成功后再读一次 active，仍等于 expected 才继续。
    """
    version = start
    for _ in range(64):
        claim = directory / f"v{version}.claim"
        if claim.exists():
            if _claim_is_stale(claim):
                claim.unlink(missing_ok=True)
                continue
            raise SpecConflict(active_version(name, root), reason="claim_busy")
        if (directory / f"v{version}.json").exists():
            version += 1
            continue
        try:
            handle = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SpecConflict(active_version(name, root), reason="claim_busy") from None
        os.close(handle)
        if active_version(name, root) != expected_version:
            claim.unlink(missing_ok=True)
            raise SpecConflict(active_version(name, root), reason="stale")
        return version, claim
    raise SpecConflict(active_version(name, root), reason="claim_busy")


def save_spec(
    spec: ScenarioSpec,
    root: Path | str = SPEC_ROOT,
    make_active: bool = True,
    *,
    expected_version: int | None | object,
    coalesce: bool = False,
) -> Path:
    """原子写一个新版本；``expected_version`` 是 CAS 的基版本。

    - ``expected_version=None``：断言"当前没有 active 版本"（新建场景首版）；
    - ``expected_version=int``：与当前 active 不等 → :class:`SpecConflict`；
    - ``expected_version=ANY_VERSION``：不做检查（仅供旧调用方兜底）。

    写入顺序：抢占 claim → 原子写 ``v{n}.json`` → 原子切 ``active.json``。
    claim 泄漏只浪费一个版本号，不产生半截 spec。
    """
    directory = Path(root) / spec.name
    directory.mkdir(parents=True, exist_ok=True)

    # 连续保存合并：只覆盖刚写过的 active 版本，不新建 vN。
    # 仅由编辑器显式请求；默认行为仍然是每次保存递增版本。
    if coalesce and isinstance(expected_version, int):
        current = active_version(spec.name, root)
        if current != expected_version:
            raise SpecConflict(current, reason="stale")
        target = directory / f"v{expected_version}.json"
        recent = target.is_file() and (time.time() - target.stat().st_mtime) <= COALESCE_WINDOW_SECONDS
        if recent:
            stored = spec.model_copy(deep=True)
            stored.schema_version = SPEC_VERSION
            for workflow in stored.workflows.values():
                for node in workflow.nodes:
                    if node.fn in NODE_REGISTRY:
                        node.contract = contract_fingerprint(node.fn)
                        node.contract_shape = contract_shape(node.fn)
            payload = json.dumps(spec_to_dict(stored), ensure_ascii=False, indent=2)
            tmp = directory / f"v{expected_version}.json.tmp"
            tmp.write_text(payload, encoding="utf-8", newline="\n")
            tmp.replace(target)
            return target

    if expected_version is ANY_VERSION:
        version = (max(_version_numbers(directory)) + 1) if _version_numbers(directory) else 1
        claim: Path | None = None
    else:
        current = active_version(spec.name, root)
        if current != expected_version:
            # 冲突提示复用编辑器的节点级 diff（added/removed/changed 都算"不同"）。
            # spec_edit 顶层 import 本模块，所以只能在函数内 import，避免循环。
            changed: list[str] = []
            try:
                from core.scenarios.spec_edit import diff_spec

                disk_spec, _disk_version, _disk_path = load_active_versioned(spec.name, root)
                if disk_spec is not None:
                    diff = diff_spec(spec_to_dict(disk_spec), spec_to_dict(spec))
                    changed = sorted(set(diff.added) | set(diff.removed) | set(diff.changed))
            except Exception:  # noqa: BLE001 - 冲突提示读不出来不该盖住冲突本身
                changed = []
            raise SpecConflict(current, changed, reason="stale")
        start = 1 if expected_version is None else int(expected_version) + 1
        version, claim = _claim_version(
            directory,
            start,
            expected_version=expected_version,  # type: ignore[arg-type]
            name=spec.name,
            root=root,
        )

    target = directory / f"v{version}.json"
    # 版本号与块契约由写入方统一填充：agent / 人都不手写这两个字段。
    stored = spec.model_copy(deep=True)
    stored.schema_version = SPEC_VERSION
    for workflow in stored.workflows.values():
        for node in workflow.nodes:
            if node.fn in NODE_REGISTRY:
                node.contract = contract_fingerprint(node.fn)
                node.contract_shape = contract_shape(node.fn)
    payload = json.dumps(spec_to_dict(stored), ensure_ascii=False, indent=2)
    tmp = directory / f"v{version}.json.tmp"
    try:
        # 固定 LF：spec 是给人和 git 看的文本，不该因为平台不同产生假 diff。
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        tmp.replace(target)
        if make_active:
            pointer = {"version": version, "file": target.name}
            active_tmp = directory / f"{ACTIVE_FILE}.tmp"
            active_tmp.write_text(
                json.dumps(pointer, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
            active_tmp.replace(directory / ACTIVE_FILE)
    finally:
        if claim is not None:
            claim.unlink(missing_ok=True)
    return target


def activate_version(name: str, version: int, root: Path | str = _SPEC_ROOT) -> ScenarioSpec:
    """把已存在的 ``vN.json`` 切回 active；不改文件内容，不新建版本。"""
    directory = Path(root) / name
    path = directory / f"v{int(version)}.json"
    if not path.is_file():
        raise SpecError([f"版本不存在：v{version}"])
    spec = load_spec(path)
    errors = validate_spec(spec)
    if errors:
        raise SpecError(errors)
    pointer = {"version": int(version), "file": path.name}
    tmp = directory / f"{ACTIVE_FILE}.tmp"
    tmp.write_text(
        json.dumps(pointer, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(directory / ACTIVE_FILE)
    return spec


def load_spec(path: Path | str) -> ScenarioSpec:
    return spec_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def active_spec_path(name: str, root: Path | str = SPEC_ROOT) -> Path | None:
    directory = Path(root) / name
    pointer = directory / ACTIVE_FILE
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    candidate = directory / str(data.get("file") or "")
    return candidate if candidate.is_file() else None


def load_active_spec(name: str, root: Path | str = SPEC_ROOT) -> ScenarioSpec | None:
    path = active_spec_path(name, root)
    return load_spec(path) if path is not None else None
