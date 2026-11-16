# -*- coding: utf-8 -*-
"""把一次工作流执行记录成 scenario → workflow → node → tool 的步骤树。

不依赖 Inspect / Streamlit；只依赖 LangChain callback 协议，因此单测可以离线跑。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler


@dataclass
class Step:
    """一步：节点或工具。节点是骨架，工具挂在当前节点下。"""

    step_id: str
    kind: str  # node / tool
    name: str
    workflow: str = ""
    node: str = ""
    tool: str = ""
    status: str = "ok"  # ok / error
    duration_ms: int = 0
    error: str = ""
    args: str = ""
    parent: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "name": self.name,
            "workflow": self.workflow,
            "node": self.node,
            "tool": self.tool,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "args": self.args,
            "parent": self.parent,
        }


@dataclass
class _OpenNode:
    step: Step
    started: float
    parent: str = ""


class AttributionRecorder(BaseCallbackHandler):
    """LangChain callback：记录 LangGraph 节点与工具调用。

    - 只有带 ``langgraph_node`` metadata 的 chain 才算节点；
    - 工具调用自动挂到当前节点（栈顶）下；
    - 异常不抛出，只把 status 记成 error，避免评测器自身把运行打断。
    """

    def __init__(self, workflow: str = ""):
        super().__init__()
        self.workflow = workflow
        self.steps: list[Step] = []
        self._node_stack: list[_OpenNode] = []
        self._runs: dict[str, Step] = {}
        self._tools: dict[str, Step] = {}
        self._tool_started: dict[str, float] = {}
        self._seq = 0

    # ---------------------------------------------------------------- 节点
    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        node = (metadata or {}).get("langgraph_node")
        if not node:
            return
        parent = self._node_stack[-1].step.step_id if self._node_stack else ""
        step = Step(
            step_id=self._next_id("node", node),
            kind="node",
            name=str(node),
            workflow=self.workflow,
            node=str(node),
            parent=parent,
        )
        self.steps.append(step)
        self._node_stack.append(_OpenNode(step=step, started=time.perf_counter(), parent=parent))
        if run_id is not None:
            self._runs[str(run_id)] = step

    def on_chain_end(self, outputs: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        step = self._runs.pop(str(run_id), None) if run_id is not None else None
        if step is None:
            return
        step.duration_ms = self._elapsed(step.step_id)
        self._pop_node(step.step_id)

    def on_chain_error(self, error: BaseException, *, run_id: Any = None, **kwargs: Any) -> None:
        step = self._runs.pop(str(run_id), None) if run_id is not None else None
        if step is None:
            return
        step.status = "error"
        step.error = f"{type(error).__name__}: {error}"
        step.duration_ms = self._elapsed(step.step_id)
        self._pop_node(step.step_id)

    # ---------------------------------------------------------------- 工具
    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        name = ""
        if isinstance(serialized, dict):
            name = str(serialized.get("name") or "")
        name = name or "tool"
        node = self._node_stack[-1].step.step_id if self._node_stack else ""
        step = Step(
            step_id=self._next_id("tool", name),
            kind="tool",
            name=name,
            workflow=self.workflow,
            node=self._node_stack[-1].step.name if self._node_stack else "",
            tool=name,
            args=str(input_str or "")[:200],
            parent=node,
        )
        self.steps.append(step)
        self._tool_started[step.step_id] = time.perf_counter()
        if run_id is not None:
            self._tools[str(run_id)] = step

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        step = self._tools.pop(str(run_id), None) if run_id is not None else None
        if step is not None:
            step.duration_ms = self._tool_ms(step.step_id)

    def on_tool_error(self, error: BaseException, *, run_id: Any = None, **kwargs: Any) -> None:
        step = self._tools.pop(str(run_id), None) if run_id is not None else None
        if step is None:
            return
        step.status = "error"
        step.error = f"{type(error).__name__}: {error}"
        step.duration_ms = self._tool_ms(step.step_id)

    # ---------------------------------------------------------------- 汇总
    def summary(self) -> dict[str, Any]:
        """给报告用的最小切片：节点序列、工具序列、错误。"""
        nodes = [step.name for step in self.steps if step.kind == "node"]
        tools = [step.name for step in self.steps if step.kind == "tool"]
        errors = [
            {"step": step.name, "error": step.error}
            for step in self.steps
            if step.status == "error"
        ]
        return {
            "workflow": self.workflow,
            "nodes": nodes,
            "tools": tools,
            "errors": errors,
            "steps": [step.to_dict() for step in self.steps],
        }

    # ---------------------------------------------------------------- 内部
    def _next_id(self, kind: str, name: str) -> str:
        self._seq += 1
        return f"{kind}-{self._seq}-{name}"

    def _elapsed(self, step_id: str) -> int:
        for node in self._node_stack:
            if node.step.step_id == step_id:
                return int((time.perf_counter() - node.started) * 1000)
        for step in self.steps:
            if step.step_id == step_id:
                return step.duration_ms
        return 0

    def _tool_ms(self, step_id: str) -> int:
        started = self._tool_started.pop(step_id, None)
        return int((time.perf_counter() - started) * 1000) if started else 0

    def _pop_node(self, step_id: str) -> None:
        while self._node_stack:
            if self._node_stack[-1].step.step_id == step_id:
                self._node_stack.pop()
                return
            self._node_stack.pop()

    def bind(self, workflow: str) -> "AttributionRecorder":
        self.workflow = workflow
        return self
