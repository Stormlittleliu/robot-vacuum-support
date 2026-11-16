# -*- coding: utf-8 -*-
"""场景库：界面层唯一的"有哪些场景"来源。

仓库默认没有任何保存的场景。``data/specs/<name>/active.json`` 一旦存在，
场景就由 spec 构建；归属状态仍只读 ``ownership.status()``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from core.scenarios.ownership import status
from core.scenarios.spec import SPEC_ROOT, active_spec_path, load_active_spec

KIND_AGENT = "agent"
KIND_ROUTER = "router"


@dataclass(frozen=True)
class ScenarioEntry:
    """场景库里的一条记录。"""

    name: str
    label: str
    icon: str = "💬"
    kind: str = KIND_AGENT
    description: str = ""
    examples: tuple[str, ...] = ()
    has_workflow: bool = False
    supports_runtime: bool = False
    pool: str | None = None
    owner: str | None = None
    editable: bool = False
    from_spec: bool = False
    cases: int = 0
    issue: str | None = None
    legacy: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_router(self) -> bool:
        return self.kind == KIND_ROUTER

    @property
    def title(self) -> str:
        return f"{self.icon} {self.label}"

    @property
    def badge(self) -> str:
        if not self.from_spec:
            return "code"
        return "spec · 可编辑" if self.editable else "spec · 只读"


def _count_cases(path: Path | str | None) -> int:
    if not path:
        return 0
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def _spec_names() -> list[str]:
    directory = Path(SPEC_ROOT)
    if not directory.is_dir():
        return []
    return sorted(
        child.name
        for child in directory.iterdir()
        if child.is_dir() and (child / "active.json").is_file()
    )


def _entry_from_spec(name: str) -> ScenarioEntry:
    spec = load_active_spec(name) if active_spec_path(name) else None
    info = status(name)
    owner = info.get("owner")
    editable = owner is not None and bool(info.get("editable"))
    path = active_spec_path(name)
    if spec is None:
        return ScenarioEntry(
            name=name,
            label=f"场景 {name}",
            icon="🧩",
            owner=owner,
            editable=False,
            from_spec=True,
            issue=f"active spec 读取失败：{path}",
        )
    if spec.cases_path:
        candidate = Path(spec.cases_path)
        cases = _count_cases(candidate if candidate.is_absolute() else Path.cwd() / candidate)
    else:
        cases = _count_cases(Path(SPEC_ROOT) / name / "cases.jsonl")
    return ScenarioEntry(
        name=name,
        label=f"场景 {name}",
        icon="🧩",
        kind=spec.kind,
        description=spec.description,
        has_workflow=bool(spec.workflows),
        owner=owner or "spec",
        editable=True if owner is None else editable,
        from_spec=True,
        cases=cases,
    )


def entries() -> list[ScenarioEntry]:
    """场景库全量：只来自 spec；默认空。"""
    items = [_entry_from_spec(name) for name in _spec_names()]
    items.sort(key=lambda entry: (entry.kind != KIND_ROUTER, entry.label))
    return items


def names() -> list[str]:
    return [entry.name for entry in entries()]


def entry(name: str) -> ScenarioEntry | None:
    return next((item for item in entries() if item.name == name), None)


def label(name: str) -> str:
    found = entry(name)
    return found.title if found else name


def default_name() -> str:
    """默认打开的场景：没有保存的场景就返回空字符串。"""
    items = entries()
    editable = [item for item in items if item.from_spec and item.editable]
    return editable[0].name if editable else ""


def summary() -> dict:
    """给总览 / 场景页用的一行统计。"""
    items = entries()
    return {
        "total": len(items),
        "spec": sum(1 for item in items if item.from_spec),
        "editable": sum(1 for item in items if item.editable),
        "cases": sum(item.cases for item in items),
        "routers": sum(1 for item in items if item.is_router),
        "items": [item.__dict__ | {"title": item.title, "badge": item.badge} for item in items],
    }


def dump_json() -> str:
    """调试用：把场景库打成 JSON（CLI / 测试都能看）。"""
    return json.dumps(summary(), ensure_ascii=False, indent=2)
