# -*- coding: utf-8 -*-
"""Targeted regression for the runtime/scene refactor.

These are component checks only.  Full-product acceptance is run separately
through the normal HTTP/SSE entry.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.contracts.agent import ExecutionSpec, Scenario
from modules.runtime import pipeline
from modules.runtime.framework import check_stop_when
from modules.scenario_format.spec import ScenarioSpec, build_from_spec
from robot_vacuum_support import execution as support_execution
from robot_vacuum_support import state as support_state


ROOT = Path(__file__).resolve().parents[1]


class _Tool:
    def __init__(self, name: str):
        self.name = name
        self.args = {}


def _frozen_spec() -> ScenarioSpec:
    return ScenarioSpec(**json.loads((ROOT / "robot_vacuum" / "v4.json").read_text(encoding="utf-8")))


def test_agent_spec_resolves_native_react_agent_not_wrapper_graph():
    spec = _frozen_spec()
    scenario = build_from_spec(
        spec,
        tools=[_Tool("rag_summarize"), _Tool("rag_retrieve")],
        model=object(),
    )
    assert scenario.mode == "agent"
    assert scenario.execution.workflow_factory is None
    assert scenario.execution.stop_when == "answer_has_citation"
    assert [tool.name for tool in scenario.tools] == ["rag_summarize", "rag_retrieve"]
    # The single react_loop node is the Agent config, not a workflow node.
    assert scenario.tool_call_limit is None
    assert scenario.enable_summarization is True


def test_workflow_spec_keeps_real_graph():
    spec = ScenarioSpec(
        name="wf",
        description="wf",
        tools=["rag_summarize"],
        workflows={
            "flow": {
                "nodes": [
                    {"id": "a", "fn": "react_loop", "args": {"tools": ["rag_summarize"]}},
                    {"id": "b", "fn": "require_citation"},
                ],
                "edges": [["__start__", "a"], ["a", "b"], ["b", "__end__"]],
            }
        },
    )
    scenario = build_from_spec(spec, tools=[_Tool("rag_summarize")], model=object())
    assert scenario.mode == "workflow"
    assert callable(scenario.execution.workflow_factory)


def test_react_loop_node_uses_framework_agent_not_create_agent(monkeypatch):
    calls: list[dict] = []

    class _FakeAgent:
        def __init__(self, scenario, **kwargs):
            calls.append(kwargs)
            self.last_answer = "清理充电触点后再试。[故障排除.txt]"
            self.last_stop_error = None

        def execute_stream(self, text, thread_id="default"):
            yield "清理充电触点后再试。[故障排除.txt]"

    from modules.scenario_format import spec_nodes

    monkeypatch.setattr("modules.runtime.framework.FrameworkAgent", _FakeAgent)

    class _View:
        name = "node-view"
        model = object()
        system_prompt = "base"
        tools = [_Tool("rag_summarize")]
        enable_todo = False
        enable_summarization = True
        summarization_trigger = ("messages", 30)
        summarization_keep = ("messages", 12)
        pii_types = []
        high_risk_tools = {}
        tool_policy = {}
        tool_timeouts = {}
        tool_timeout_seconds = 30
        fallback_model = None
        planning_policy = None
        token_budget = None
        runtime = None

        @staticmethod
        def resolve_execution():
            return ExecutionSpec(model=_View.model, checkpoint=False)

        @staticmethod
        def get_tool(name):
            return _Tool(name)

    result = spec_nodes.react_loop(
        {"input": "回充失败"},
        scenario=_View(),
        tools=["rag_summarize"],
        prompt="node prompt",
        max_steps=3,
        stop_when="answer_has_citation",
        _node_id="agent",
    )
    assert result["answer"].startswith("清理充电触点")
    assert calls and calls[0]["system_prompt"] == "node prompt"
    assert calls[0]["parent_trace_id"] is None
    overrides = {item["name"]: item for item in calls[0]["node_overrides"]}
    assert overrides["limit"]["enabled"] is True
    assert overrides["limit"]["args"]["run_limit"] == 3


def test_effective_policy_priority_and_source():
    scenario = Scenario(name="p", description="p", enable_summarization=True)
    execution = ExecutionSpec(model=object())
    rows = {row["name"]: row for row in pipeline.effective_policies(scenario, execution=execution)}
    assert rows["permission"]["source"] == "harness_default"
    assert rows["summarization"]["enabled"] is True
    assert rows["summarization"]["source"] == "scenario_policy"
    # Scenario-level explicit policy disables, node-level explicit override wins.
    execution.policies = [
        {"name": "summarization", "enabled": False, "reason": "scenario test"}
    ]
    rows = {
        row["name"]: row
        for row in pipeline.effective_policies(scenario, execution=execution)
    }
    assert rows["summarization"]["enabled"] is False
    assert rows["summarization"]["source"] == "scenario_policy"
    rows = {
        row["name"]: row
        for row in pipeline.effective_policies(
            scenario,
            execution=execution,
            node_overrides=[{"name": "summarization", "enabled": True}],
        )
    }
    assert rows["summarization"]["enabled"] is True
    assert rows["summarization"]["source"] == "node_override"


def test_history_not_duplicated_into_populated_checkpoint():
    from modules.instances.runtime import execution as worker_execution

    assert worker_execution._history_mode(checkpoint=True, has_checkpoint=True) is False
    assert worker_execution._history_mode(checkpoint=True, has_checkpoint=False) is True
    assert worker_execution._history_mode(checkpoint=False, has_checkpoint=True) is True


def test_stop_when_is_preserved_without_turning_it_into_provider_error():
    assert check_stop_when("answer_has_citation", "有来源 [故障排除.txt]") is None
    assert check_stop_when("answer_has_citation", "没有引用") == "回答缺少 [来源] 引用"
    assert check_stop_when("no_tool_calls", "没有引用") is None


class _FakeStore:
    def __init__(self):
        self.state = None
        self.messages = []
        self.updates = []

    def history(self, session_id, limit=24):
        return []

    def load_support_state(self, session_id):
        return self.state

    def save_support_state(self, session_id, state):
        self.state = state
        return state

    def append_message(self, session_id, role, content, **extra):
        record = {
            "role": role,
            "content": content,
            "message_id": f"m-{len(self.messages) + 1}",
            **extra,
        }
        self.messages.append(record)
        return record

    def update(self, session_id, **fields):
        self.updates.append(fields)
        return fields


def test_support_execution_prepare_finish_uses_one_snapshot():
    store = _FakeStore()
    prepared = support_execution.prepare_run(
        store,
        "s-1",
        {"prompt": "回充失败，充电座前打转"},
        enabled=False,
    )
    assert prepared.direct_payload is None
    assert prepared.request_body["support_context"]
    assert prepared.request_body["support_snapshot"]["snapshot_id"] == (
        prepared.state.get("decision_snapshot") or {}
    ).get("snapshot_id")
    assert prepared.state["active_case_id"]

    finished = support_execution.finish_run(
        store,
        "s-1",
        prepared,
        {
            "answer": "先清理充电座周围障碍物。[故障排除.txt]",
            "status": "completed",
            "thread_id": "session-s-1",
            "retrieval": {
                "status": "ok",
                "results": [{"source": "故障排除.txt", "snippet": "回充 充电座"}],
            },
            "usage": {"by_layer": {"main_agent": {"model_calls": 1}}},
            "trace_id": "t-1",
        },
    )
    assert finished["answer"].startswith("先清理")
    assert finished["support"]["active_case"]["suggested_actions"]
    assert store.state["last_turn_metrics"] is not None
    assert store.updates[-1]["last_status"] == "completed"


def test_support_execution_protocol_ack_is_direct_and_persisted():
    store = _FakeStore()
    prepared = support_execution.prepare_run(
        store,
        "s-2",
        {"prompt": "谢谢"},
        enabled=False,
    )
    assert prepared.direct_payload is not None
    assert prepared.direct_payload["direct"] is True
    assert prepared.direct_answer
    assert store.messages[-1]["role"] == "assistant"
    assert store.state["turn"] == 1
