# -*- coding: utf-8 -*-
"""通用工作流运行时：只执行 Scenario 提供的 LangGraph。

场景层（scenarios/<name>/graph.py）负责挑工具、定义节点和边；
本文件不知道任何业务工作流名，也不 import rag/business 工具。
"""
from __future__ import annotations

import time
import uuid
from modules.runtime.limits import engine_recursion_limit, token_limit
from core.observability.budget import RunBudget, budget_scope, current_budget

from langgraph.types import Command

from core.observability.decisions import root_tracer_scope
from core.observability.tracer import TraceCallbackHandler
from modules.runtime.compiler import node_event_sink, token_event_sink


class WorkflowRuntime:
    def __init__(self, scenario):
        self.scenario = scenario
        resolve = getattr(scenario, "resolve_execution", None)
        execution = resolve() if callable(resolve) else None
        factory = (
            execution.workflow_factory if execution is not None
            else getattr(scenario, "workflow_factory", None)
        )
        self.graphs = factory(scenario) if factory else {}
        self.last_trace_id: str | None = None
        self.last_node_events: list[dict] = []
        self.thread_id: str | None = None
        self.last_name: str | None = None
        self.last_interrupt: object | None = None
        self.run_budget = None
        self.last_usage_summary: dict | None = None

    def list_workflows(self) -> list[str]:
        return sorted(self.graphs)

    def run(self, name: str, input_text: str, event_sink=None, token_sink=None):
        if name not in self.graphs:
            available = ", ".join(self.list_workflows()) or "（无）"
            raise FileNotFoundError(f"工作流不存在：{name}（可用：{available}）")
        self.run_budget = RunBudget(token_limit(self.scenario), parent=current_budget())
        tracer = TraceCallbackHandler(
            budget=self.run_budget,
            trace_id=f"run-{uuid.uuid4().hex}",
            thread_id=f"workflow-{uuid.uuid4().hex}",
            scenario=getattr(self.scenario, "name", None),
        )
        self.last_trace_id = tracer.trace_id
        self.thread_id = f"workflow-{uuid.uuid4().hex}"
        self.last_name = name
        self.last_interrupt = None
        self.last_node_events = []

        def capture(event):
            self.last_node_events.append(event)
            if event_sink is not None:
                event_sink(event)

        try:
            with root_tracer_scope(tracer), budget_scope(self.run_budget), node_event_sink(capture), token_event_sink(token_sink):
                result = self.graphs[name].invoke(
                    {"input": input_text},
                    config={
                        "callbacks": [tracer],
                    "recursion_limit": engine_recursion_limit(),
                        "configurable": {"thread_id": self.thread_id},
                    },
                )
                if isinstance(result, dict) and "__interrupt__" in result:
                    self.last_interrupt = result["__interrupt__"]
                return result
        finally:
            self.last_usage_summary = {
                "model_calls": tracer.model_calls,
                "tool_calls": tracer.tool_calls,
                "prompt_tokens": tracer.prompt_tokens,
                "completion_tokens": tracer.completion_tokens,
                "total_tokens": tracer.total_tokens,
                "cost_est": tracer.cost_est,
                "latency_ms": int((time.time() - tracer.started_at) * 1000),
                "budget_total_tokens": self.run_budget.total_tokens if self.run_budget else None,
                "model_events": list(tracer.model_events),
            }
            tracer.finalize_if_needed()



    def resume(self, decision: object, event_sink=None, token_sink=None):
        """恢复被 approval 中断的同一个 thread。"""
        if not self.last_name or not self.thread_id:
            raise RuntimeError("没有可恢复的 workflow run")
        tracer = TraceCallbackHandler(
            budget=self.run_budget,
            trace_id=self.last_trace_id or f"run-{uuid.uuid4().hex}",
            thread_id=self.thread_id,
            scenario=getattr(self.scenario, "name", None),
        )
        self.last_node_events = []

        def capture(event):
            self.last_node_events.append(event)
            if event_sink is not None:
                event_sink(event)

        with root_tracer_scope(tracer), budget_scope(self.run_budget), node_event_sink(capture), token_event_sink(token_sink):
            result = self.graphs[self.last_name].invoke(
                Command(resume=decision),
                config={
                    "callbacks": [tracer],
                    "recursion_limit": engine_recursion_limit(),
                    "configurable": {"thread_id": self.thread_id},
                },
            )
        if isinstance(result, dict) and "__interrupt__" in result:
            self.last_interrupt = result["__interrupt__"]
        else:
            self.last_interrupt = None
        tracer.finalize_if_needed()
        return result
