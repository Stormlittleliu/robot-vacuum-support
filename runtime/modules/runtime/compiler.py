# -*- coding: utf-8 -*-
"""spec → 工作流编译器：入参只有 spec + 工具，不依赖已构建的 Scenario。

为什么单独一层：以前 ``build_workflows(spec, scenario)`` 要求先有 Agent 才能编译，
于是"编辑器预览 / MCP 体检 / 候选编译"都被迫构造完整运行时。现在：

- ``compile_spec(spec, tools, model=None)``：纯编译，工具只用于解析 ``get_tool``；
- ``compile_preview(spec)``：连工具对象都不需要，给编辑器与候选用；
- ``build_workflows`` 保留为兼容壳，内部调用本模块。

执行仍发生在编译出的图里；编译本身不调用模型、不读知识库。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from core.observability import latency
from modules.scenario_format.spec import SpecError, SpecState, validate_spec
from modules.scenario_format.spec_check import split_edge
from modules.scenario_format.spec_nodes import NODE_REGISTRY


_NODE_EVENT_SINK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "node_event_sink", default=None
)
_NODE_ACTIVATIONS: ContextVar[dict[str, int] | None] = ContextVar(
    "node_activations", default=None
)
_TOKEN_SINK: ContextVar[Callable[[str], None] | None] = ContextVar(
    "token_sink", default=None
)


@contextmanager
def token_event_sink(sink: Callable[[str], None] | None):
    token = _TOKEN_SINK.set(sink)
    try:
        yield
    finally:
        _TOKEN_SINK.reset(token)


def emit_token(text: str) -> None:
    sink = _TOKEN_SINK.get()
    if sink is not None and text:
        sink(text)


def has_token_sink() -> bool:
    return _TOKEN_SINK.get() is not None


@contextmanager
def node_event_sink(sink: Callable[[dict[str, Any]], None] | None):
    token = _NODE_EVENT_SINK.set(sink)
    counter_token = _NODE_ACTIVATIONS.set({})
    try:
        yield
    finally:
        _NODE_ACTIVATIONS.reset(counter_token)
        _NODE_EVENT_SINK.reset(token)


def _instrumented(node_id: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    def run(state: dict[str, Any], **kwargs: Any) -> Any:
        sink = _NODE_EVENT_SINK.get()
        counter = _NODE_ACTIVATIONS.get()
        activation = None
        if counter is not None:
            activation = counter.get(node_id, 0) + 1
            counter[node_id] = activation
        started = time.time()
        if sink is not None:
            sink({"event": "node_start", "node": node_id, "ts": started, "activation": activation, "input": state})
        try:
            result = fn(state, **kwargs)
        except Exception as exc:
            if sink is not None:
                sink({
                    "event": "node_error",
                    "node": node_id,
                    "ts": time.time(),
                    "activation": activation,
                    "latency_ms": int((time.time() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            raise
        if sink is not None:
            sink({
                "event": "node_end",
                "node": node_id,
                "ts": time.time(),
                "activation": activation,
                "latency_ms": int((time.time() - started) * 1000),
                "input": state,
                "output": result,
            })
        return result

    return run


class _PreviewTool:
    """只提供名字的占位工具；预览阶段不会被调用。"""

    def __init__(self, name: str):
        self.name = name

    def invoke(self, *_args: Any, **_kwargs: Any):
        raise RuntimeError("preview tool 不能被调用")


class ToolView:
    """块执行时需要的"场景视图"：只暴露 ``get_tool`` 与 ``model``。"""

    def __init__(self, tools: list[Any] | None = None, model: Any = None):
        self.model = model
        self._tools = {
            str(getattr(tool, "name", "")): tool
            for tool in (tools or [])
            if getattr(tool, "name", None)
        }

    @property
    def tools(self) -> list[Any]:
        return list(self._tools.values())

    def get_tool(self, name: str):
        tool = self._tools.get(str(name))
        if tool is None:
            raise KeyError(f"工作流引用了未提供的工具：{name}")
        return tool


@dataclass
class CompiledWorkflow:
    name: str
    graph: Any
    nodes: list[str] = field(default_factory=list)
    edges: list[tuple[str, str, str | None]] = field(default_factory=list)

    def invoke(self, state: dict[str, Any], config: dict[str, Any] | None = None):
        """执行编译好的图；``config`` 原样透传（trace/checkpoint 由调用方决定）。"""
        return self.graph.invoke(state, config=config) if config else self.graph.invoke(state)


@dataclass
class CompiledSpec:
    workflows: dict[str, CompiledWorkflow] = field(default_factory=dict)

    @property
    def graphs(self) -> dict[str, Any]:
        return {name: item.graph for name, item in self.workflows.items()}


def _route_of(state: dict[str, Any]) -> str:
    """条件边用的路由值：branch 块把它写进 ``scratch.route``。"""
    scratch = state.get("scratch") or {}
    return str(scratch.get("route") or "")


def compile_spec(
    spec: Any,
    *,
    tools: list[Any] | None = None,
    model: Any = None,
) -> CompiledSpec:
    """把 spec 编译成 ``CompiledSpec``；不构造 Scenario，不调用模型。"""
    tool_names = {
        str(getattr(tool, "name", "")) for tool in (tools or []) if getattr(tool, "name", None)
    }
    errors = validate_spec(spec, tool_names=tool_names or None)
    if errors:
        raise SpecError(errors)

    view = ToolView(tools, model)
    compiled = CompiledSpec()
    with latency.span("worker.graph_compile", workflows=len(spec.workflows)):
        for name, workflow in spec.workflows.items():
            with latency.span(
                "worker.graph_compile.workflow",
                workflow=name,
                nodes=len(workflow.nodes),
                edges=len(workflow.edges),
            ):
                graph = StateGraph(SpecState)
                for node in workflow.nodes:
                    fn = NODE_REGISTRY[node.fn]
                    graph.add_node(node.id, _instrumented(node.id, partial(fn, scenario=view, **node.args)))

                plain: list[tuple[str, str]] = []
                branches: dict[str, dict[str, str]] = {}
                for edge in workflow.edges:
                    source, target, when = split_edge(edge)
                    if when is None:
                        plain.append((source, target))
                    else:
                        branches.setdefault(source, {})[when] = target

                for source, target in plain:
                    if source == START:
                        graph.add_edge(START, target)
                    elif target == END:
                        graph.add_edge(source, END)
                    else:
                        graph.add_edge(source, target)
                for source, mapping in branches.items():
                    graph.add_conditional_edges(source, _route_of, mapping)

                needs_checkpoint = any(node.fn == "approval" for node in workflow.nodes)
                checkpointer = None
                if needs_checkpoint:
                    import sqlite3

                    from langgraph.checkpoint.sqlite import SqliteSaver

                    from core.paths import runtime_path

                    conn = sqlite3.connect(str(runtime_path("approval_checkpoints.sqlite")), check_same_thread=False)
                    checkpointer = SqliteSaver(conn)
                compiled.workflows[name] = CompiledWorkflow(
                    name=name,
                    graph=graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile(),
                    nodes=[node.id for node in workflow.nodes],
                    edges=[split_edge(edge) for edge in workflow.edges],
                )
    return compiled


def compile_preview(spec: Any) -> CompiledSpec:
    """只读编译：工具用名字占位，不需要模型或已构建的场景。"""
    tools = [_PreviewTool(name) for name in spec.tools]
    return compile_spec(spec, tools=tools, model=None)
