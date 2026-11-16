# -*- coding: utf-8 -*-
"""工具盒：场景"能看见哪些工具"的唯一来源。

工具盒分两类（统一中文叫"工具盒"，不要叫工具池 / 工具箱）：

- ``scenario``：特定场景开发的专用盒，必须声明 ``scenario`` 归属；
- ``project``：项目伴生的通用盒，必须声明 ``project`` 与 ``source`` 归属。

现有 ``office`` / ``coding`` / ``research`` / ``mcp`` / ``rag`` 都是 project
通用盒。一个工具可以属于多个盒，成员关系以这里声明的 ``tools`` 为准；不从
工具名猜场景，也不把 runtime workspace 当归属。场景专用盒只是契约能力，
生产环境不创建业务场景或样例。

spec 只需要写 ``tools: [...]`` 白名单；没有专属工具盒的场景走 ``office``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.observability import latency
from core.paths import SPEC_ROOT, runtime_path
from core.runtime.config import SubagentConfig


class ToolboxCategory(str, Enum):
    """工具盒分类：场景专用 / 项目伴生通用。"""

    SCENARIO = "scenario"
    PROJECT = "project"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value: Any) -> "ToolboxCategory":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for item in cls:
            if item.value == text:
                return item
        raise ValueError(f"未知工具盒分类：{value!r}（可用：scenario、project）")


TOOLBOX_CATEGORY_LABELS = {
    ToolboxCategory.SCENARIO: "场景专用",
    ToolboxCategory.PROJECT: "项目通用",
}

TOOLBOX_CATEGORY_DESCRIPTIONS = {
    ToolboxCategory.SCENARIO: "为单个场景开发、必须声明场景归属的工具盒。",
    ToolboxCategory.PROJECT: "与项目伴生、可跨场景复用的通用工具盒。",
}


@dataclass(frozen=True)
class ToolboxMeta:
    """一个工具盒的只读元数据；工具成员关系也以这里为准。

    ``scenario`` / ``project`` 分开声明，避免把"工具盒是谁的"混在运行时
    路径或工具名里。``source`` 只表示实现来源，不代表归属。
    """

    name: str
    label: str
    description: str
    category: ToolboxCategory
    source: str
    tools: tuple[str, ...] = ()
    scenario: str = ""
    project: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("工具盒 name 不能为空")
        if name != str(self.name) or any(char.isspace() for char in name):
            raise ValueError(f"工具盒 name 不能含空白：{self.name!r}")
        if any(char in name for char in "/\\?#%") or any(ord(char) < 32 for char in name):
            raise ValueError(f"工具盒 name 含保留字符：{self.name!r}")

        category = ToolboxCategory.parse(self.category)
        scenario = str(self.scenario or "").strip()
        project = str(self.project or "").strip()
        source = str(self.source or "").strip()
        if category is ToolboxCategory.SCENARIO:
            if not scenario:
                raise ValueError(f"场景专用工具盒 {name} 必须声明 scenario 归属")
            if project:
                raise ValueError(f"场景专用工具盒 {name} 不能再声明 project 归属")
        else:
            if not project:
                raise ValueError(f"项目通用工具盒 {name} 必须声明 project 归属")
            if scenario:
                raise ValueError(f"项目通用工具盒 {name} 不能再声明 scenario 归属")
        if not source:
            raise ValueError(f"工具盒 {name} 必须声明 source 来源")

        tools: list[str] = []
        for item in self.tools or ():
            text = str(item or "").strip()
            if text and text not in tools:
                tools.append(text)
        if not tools:
            raise ValueError(f"工具盒 {name} 至少声明一个工具")

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "label", str(self.label or name).strip() or name)
        object.__setattr__(self, "description", str(self.description or "").strip())
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "tools", tuple(tools))
        object.__setattr__(self, "scenario", scenario)
        object.__setattr__(self, "project", project)

    @property
    def owner(self) -> str:
        """归属：场景名或项目 id；与 runtime workspace 无关。"""
        return self.scenario if self.category is ToolboxCategory.SCENARIO else self.project

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "category": self.category.value,
            "category_label": TOOLBOX_CATEGORY_LABELS[self.category],
            "owner": self.owner,
            "scenario": self.scenario or None,
            "project": self.project or None,
            "source": self.source,
            "tools": list(self.tools),
            "tool_count": len(self.tools),
        }


_SUBAGENT_TOOL = SubagentConfig().name

# 工具盒元数据：运行时、ResourceCatalog、编辑器 UI 都从这里读，不在别处再抄
# 一份"工具 -> 盒"的映射。工具实现仍在各自模块里。
_TOOLBOXES: dict[str, ToolboxMeta] = {
    meta.name: meta
    for meta in (
        ToolboxMeta(
            name="office",
            label="办公",
            description="笔记、长期记忆、翻译与总结；新建场景的默认工具盒。",
            category=ToolboxCategory.PROJECT,
            project="agent-harness",
            source="modules/runtime/office_tools.py",
            tools=(
                "list_notes",
                "read_note",
                "search_notes",
                "write_note",
                "remember",
                "recall",
                "list_memory",
                "translate",
                "summarize",
            ),
        ),
        ToolboxMeta(
            name="coding",
            label="编码",
            description="工作区文件、命令、任务图与子 Agent。",
            category=ToolboxCategory.PROJECT,
            project="agent-harness",
            source="core/runtime/workspace.py",
            tools=(
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "run_command",
                "create_task",
                "list_tasks",
                "get_task",
                "claim_task",
                "complete_task",
                "fail_task",
                _SUBAGENT_TOOL,
            ),
        ),
        ToolboxMeta(
            name="research",
            label="研究",
            description="资料列表、读取、检索与报告。",
            category=ToolboxCategory.PROJECT,
            project="agent-harness",
            source="modules/runtime/research_tools.py",
            tools=(
                "list_sources",
                "read_source",
                "search_sources",
                "write_report",
            ),
        ),
        ToolboxMeta(
            name="mcp",
            label="MCP 连接器",
            description="memory 与 fetch 外部 MCP 工具；连接后才可用。",
            category=ToolboxCategory.PROJECT,
            project="agent-harness",
            source="core/mcp/client.py",
            tools=(
                "memory_create_entities",
                "memory_create_relations",
                "memory_add_observations",
                "memory_delete_entities",
                "memory_delete_observations",
                "memory_delete_relations",
                "memory_read_graph",
                "memory_search_nodes",
                "memory_open_nodes",
                "fetch_fetch_url",
            ),
        ),
        ToolboxMeta(
            name="rag",
            label="RAG 知识库",
            description="通用 RAG 检索与总结引擎。",
            category=ToolboxCategory.PROJECT,
            project="agent-harness",
            source="core/tools/rag/tools.py",
            tools=("rag_summarize", "rag_retrieve"),
        ),
    )
}


def toolbox_metas(category: Any = None) -> tuple[ToolboxMeta, ...]:
    """列出工具盒元数据；``category`` 可过滤 scenario / project。"""
    if category is None or not str(category).strip():
        return tuple(_TOOLBOXES.values())
    wanted = ToolboxCategory.parse(category)
    return tuple(meta for meta in _TOOLBOXES.values() if meta.category is wanted)


def toolbox_meta(name: str) -> ToolboxMeta | None:
    return _TOOLBOXES.get(str(name or "").strip())


def tool_toolboxes(name: str) -> tuple[str, ...]:
    """反向查一个工具属于哪些盒；未知工具返回空元组，不按名字猜。"""
    text = str(name or "").strip()
    if not text:
        return ()
    return tuple(meta.name for meta in _TOOLBOXES.values() if text in meta.tools)


def toolbox_categories() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "id": category.value,
            "label": TOOLBOX_CATEGORY_LABELS[category],
            "description": TOOLBOX_CATEGORY_DESCRIPTIONS[category],
        }
        for category in ToolboxCategory
    )


def register_toolbox(
    meta: ToolboxMeta,
    *,
    builder: Any = None,
    replace: bool = False,
) -> ToolboxMeta:
    """登记一个工具盒（测试或未来的场景专用盒用）。

    ``builder`` 可选；提供后 ``toolbox(name)`` 会走它，否则运行时仍走 office
    兜底。生产不登记业务场景盒，也不自动创建场景 / 样例。
    """
    if not isinstance(meta, ToolboxMeta):
        raise TypeError("register_toolbox 只接受 ToolboxMeta")
    if meta.name in _TOOLBOXES and not replace:
        raise ValueError(f"工具盒已存在：{meta.name}")
    _TOOLBOXES[meta.name] = meta
    if builder is not None:
        _BUILDERS[meta.name] = builder
    clear_cache()
    return meta


def unregister_toolbox(name: str) -> ToolboxMeta | None:
    """注销工具盒（主要给测试清理；生产不应注销内置 project 盒）。"""
    key = str(name or "").strip()
    meta = _TOOLBOXES.pop(key, None)
    _BUILDERS.pop(key, None)
    clear_cache()
    return meta


@dataclass(frozen=True)
class Toolbox:
    """一个场景的工具集合，外加它需要的运行时与"代码层默认值"。

    ``defaults`` 是 ``Scenario`` 字段的覆盖层（tool_policy / high_risk_tools /
    pii_types / context / timeouts / checkpoint…）。它们属于"实现"，不该写进 spec；
    spec 只描述结构（提示词 / 工具白名单 / 拓扑 / harness 开关）。
    """

    name: str
    tools: tuple = ()
    runtime: Any = None
    middleware: tuple = ()
    defaults: dict = field(default_factory=dict)
    notes: tuple[str, ...] = field(default_factory=tuple)
    category: str = ToolboxCategory.PROJECT.value
    owner: str = "agent-harness"
    label: str = ""
    description: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        """校验分类 / 归属，并约束 ``defaults`` 只能是已知字段名。"""
        from core.agent.contracts import ExecutionSpec, Scenario

        category = ToolboxCategory.parse(self.category)
        owner = str(self.owner or "").strip()
        if not owner:
            raise ValueError(
                f"工具盒 {self.name} 必须声明 owner（"
                f"{'场景' if category is ToolboxCategory.SCENARIO else '项目'}归属）"
            )
        object.__setattr__(self, "category", category.value)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "label", str(self.label or self.name))
        object.__setattr__(self, "description", str(self.description or ""))
        object.__setattr__(self, "source", str(self.source or ""))

        known = set(Scenario.__dataclass_fields__) | set(ExecutionSpec.__dataclass_fields__)
        unknown = sorted(set(self.defaults) - known)
        if unknown:
            raise ValueError(
                f"工具盒 {self.name} 的 defaults 含未知字段：{', '.join(unknown)}"
                f"（可用：{', '.join(sorted(known))}）"
            )

        from core.tools.adapters.langchain import as_spec
        from core.tools.spec import ToolSpec

        if not all(isinstance(tool, ToolSpec) for tool in self.tools):
            object.__setattr__(
                self, "tools", tuple(as_spec(tool) for tool in self.tools)
            )

    def tool_specs(self) -> tuple:
        """框架无关的工具描述（``ToolSpec``）；执行仍回到原工具。"""
        from core.tools.adapters.langchain import as_spec

        policy = self.defaults.get("tool_policy") or {}
        risks = policy.get("risk_levels") or {}
        timeouts = self.defaults.get("tool_timeouts") or {}
        default_timeout = self.defaults.get("tool_timeout_seconds")
        specs = []
        for tool in self.tools:
            name = str(getattr(tool, "name", ""))
            specs.append(as_spec(
                tool,
                risk=str(risks.get(name) or "low"),
                timeout=timeouts.get(name, tool.timeout if tool.timeout is not None else default_timeout),
            ))
        return tuple(specs)


def _box(name: str, tools: tuple, **kwargs: Any) -> Toolbox:
    """用登记过的元数据构造运行时工具盒；归属不在 builder 里另写。"""
    meta = _TOOLBOXES[name]
    return Toolbox(
        name,
        tools,
        category=meta.category.value,
        owner=meta.owner,
        label=meta.label,
        description=meta.description,
        source=meta.source,
        **kwargs,
    )


def _office() -> Toolbox:
    """通用兜底盒：笔记检索 / 读写 / 长期记忆 / 翻译 / 总结。"""
    from modules.runtime.office_tools import MemoryStore, NoteStore, build_office_tools

    store = NoteStore(
        runtime_path("office", "seed_notes"),
        runtime_path("office", "notes"),
    )
    memory = MemoryStore(runtime_path("office", "memory.sqlite"))
    return _box(
        "office",
        tuple(build_office_tools(store, memory)),
        defaults={
            "tool_policy": {
                "default": "allow",
                "risk_levels": {
                    "write_note": "medium",
                    "remember": "medium",
                },
            },
            "tool_timeout_seconds": 30,
            "tool_timeouts": {"translate": 60, "summarize": 60},
        },
    )


def _coding() -> Toolbox:
    from core.agent.middleware import log_before_model, monitor_tool
    from core.agent.subagent import build_subagent_tool
    from core.llm.factory import chat_model
    from core.runtime.config import RuntimeConfig, SubagentConfig
    from core.runtime.task_tools import build_task_tools
    from core.runtime.tasks import TaskStore
    from core.runtime.workspace import Workspace, build_workspace_tools

    workspace = Workspace(runtime_path("coding_workspace"), runtime_path("run_window"))
    task_db = runtime_path("tasks", "agent.sqlite")
    store = TaskStore(task_db)
    subagent = SubagentConfig(
        allowed_tools=["read_file", "glob", "grep", "run_command"],
        max_model_calls=None,
        token_budget=None,
        timeout_seconds=None,
    )
    runtime = RuntimeConfig(
        workspace=workspace.root,
        task_store_path=task_db,
        log_dir=workspace.log_dir,
        task_store=store,
        subagent=subagent,
    )
    tools = build_workspace_tools(workspace) + build_task_tools(store)
    subagent_tool = build_subagent_tool(chat_model, tools, subagent, parent_runtime=runtime)
    middleware = (monitor_tool, log_before_model)
    return _box(
        "coding",
        tuple(tools) + (subagent_tool,),
        runtime=runtime,
        middleware=middleware,
        defaults={
            "middleware": list(middleware),
            "tool_policy": {
                "default": "allow",
                "risk_levels": {
                    "write_file": "medium",
                    "edit_file": "medium",
                    "run_command": "medium",
                    "subagent": "medium",
                },
            },
            "tool_timeout_seconds": 60,
            "tool_timeouts": {"subagent": 300, "run_command": 120},
            "checkpoint_backend": "sqlite",
            "checkpoint_path": "data/runtime/coding_checkpoints.sqlite",
        },
    )


def _research() -> Toolbox:
    from core.agent.middleware import log_before_model, monitor_tool
    from modules.runtime.research_tools import SourceStore, build_research_tools

    store = SourceStore(
        runtime_path("research", "sources"),
        runtime_path("research", "reports"),
    )
    tools = tuple(build_research_tools(store))
    middleware = (monitor_tool, log_before_model)
    return _box(
        "research",
        tools,
        middleware=middleware,
        defaults={
            "middleware": list(middleware),
            "tool_policy": {
                "default": "allow",
                "risk_levels": {"write_report": "medium"},
            },
            "tool_timeout_seconds": 60,
        },
    )


def _mcp() -> Toolbox:
    from core.mcp.client import load_default_tools

    return _box("mcp", tuple(load_default_tools()))


def _rag() -> Toolbox:
    # Importing ``core.tools.rag.tools`` pulls the RAG service/retriever stack
    # (including the embedding stack) at module import time.  Keep the import
    # itself visible so the cold profile can separate it from construction.
    with latency.span("worker.toolbox_import", toolbox="rag"):
        from core.tools.rag.tools import build_rag_tools

    with latency.span("worker.toolbox_construct", toolbox="rag"):
        summarize, retrieve = build_rag_tools()
    return _box(
        "rag",
        (summarize, retrieve),
        defaults={
            "tool_policy": {
                "default": "allow",
                "risk_levels": {"rag_summarize": "low", "rag_retrieve": "low"},
            },
            "tool_timeout_seconds": 60,
        },
    )


_BUILDERS = {
    "office": _office,
    "coding": _coding,
    "research": _research,
    "mcp": _mcp,
    "rag": _rag,
}


def _pool_override(name: str, root: Path | str | None = None) -> str | None:
    """用 ``<root>/<name>/.pool`` 指定工具盒；默认根是全局 SPEC_ROOT。"""
    if not name:
        return None
    base = Path(root) if root is not None else Path(SPEC_ROOT)
    path = base / name / ".pool"
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


@lru_cache(maxsize=None)
def toolbox(name: str | None = None, root: Path | str | None = None) -> Toolbox:
    """按场景名取工具盒；没有专属盒的场景走 office。"""
    base = Path(root) if root is not None else Path(SPEC_ROOT)
    key = _pool_override(str(name or ""), base) or str(name or "")
    builder = _BUILDERS.get(key, _office)
    with latency.span("worker.toolbox_factory", toolbox=key):
        return builder()


def tool_names(name: str | None = None, root: Path | str | None = None) -> list[str]:
    return [str(getattr(tool, "name", "")) for tool in toolbox(name, root).tools]


def clear_cache() -> None:
    """测试用：改了工具配置后重新构建。"""
    toolbox.cache_clear()
