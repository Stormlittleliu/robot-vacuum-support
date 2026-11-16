# -*- coding: utf-8 -*-
"""守卫探针（L3）：权限 / 超时 / 审批是否还生效。

这些不是业务任务，而是"Harness 不变量"：用脚本化模型触发一次工具调用，
看中间件是否按预期拦截。评分仍由 Inspect 的 ``includes`` 完成。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from modules.contracts.tool import ToolResult, ToolSpec
from modules.evaluation.tasks import EvalTask

PROBE_KIND = "kind"


class _ToolCallModel(GenericFakeChatModel):
    """先调工具、再给最终回答；``bind_tools`` 必须可调用。"""

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self


def _scenario(kind: str, calls: list[dict[str, Any]], timeout: float = 0.05):
    from core.agent.contracts import ExecutionSpec, Scenario

    def runner(args: dict[str, Any]) -> ToolResult:
        calls.append(dict(args))
        if kind == "timeout":
            time.sleep(0.2)
        return ToolResult(content="ok")

    tool = ToolSpec(
        name="reset_device",
        description="重置设备（探针用）",
        input_schema={"type": "object", "properties": {}},
        runner=runner,
    )
    model = _ToolCallModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "reset_device", "args": {}, "id": "1"}]),
        AIMessage(content="完成"),
    ]))
    high_risk = {"reset_device": True} if kind == "approval" else {}
    policy = {"default": "deny"} if kind == "permission" else {"default": "allow"}
    return Scenario(
        name=f"probe_{kind}",
        description=f"guard probe: {kind}",
        system_prompt="你是探针场景。",
        tools=[tool],
        tool_policy=policy,
        high_risk_tools=high_risk,
        enable_todo=False,
        enable_summarization=False,
        tool_call_limit=3,
        execution=ExecutionSpec(
            model=model,
            checkpoint=True,
            checkpoint_backend="memory",
        ),
        **({"tool_timeout_seconds": timeout} if kind == "timeout" else {}),
    )


def run_probe(kind: str, instruction: str = "重置设备") -> tuple[str, dict[str, Any]]:
    """跑一条探针，返回（可判分的结论, 证据）。"""
    from core.agent.framework import FrameworkAgent

    calls: list[dict[str, Any]] = []
    scenario = _scenario(kind, calls)
    state = FrameworkAgent(scenario).invoke(instruction, thread_id=f"probe-{kind}")
    texts = "\n".join(str(getattr(item, "content", "")) for item in state.get("messages") or [])

    if kind == "permission":
        answer = "工具被拒绝，未执行" if not calls else "工具执行了"
    elif kind == "timeout":
        answer = "工具超时" if "工具超时" in texts else "未超时"
    elif kind == "approval":
        answer = "等待审批" if state.get("__interrupt__") else "未触发审批"
    else:
        raise ValueError(f"未知探针：{kind}")
    return answer, {"kind": kind, "calls": calls, "interrupt": bool(state.get("__interrupt__"))}


PROBE_TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        id="probe-permission",
        input="重置设备",
        scenario="probe",
        workflow="invariant",
        expect_contains=("工具被拒绝",),
        metadata={PROBE_KIND: "permission"},
    ),
    EvalTask(
        id="probe-timeout",
        input="重置设备",
        scenario="probe",
        workflow="invariant",
        expect_contains=("工具超时",),
        metadata={PROBE_KIND: "timeout"},
    ),
    EvalTask(
        id="probe-approval",
        input="重置设备",
        scenario="probe",
        workflow="invariant",
        expect_contains=("等待审批",),
        metadata={PROBE_KIND: "approval"},
    ),
)


from inspect_ai.solver import TaskState, solver


@solver
def probe_solver():
    async def solve(state: TaskState, generate: Any) -> TaskState:
        task = (state.metadata or {}).get("task") or {}
        kind = str((task.get("metadata") or {}).get(PROBE_KIND) or "")
        answer, evidence = run_probe(kind, state.input_text)
        state.output.completion = answer
        state.metadata["probe"] = evidence
        state.metadata["workflow"] = "invariant"
        state.metadata["scenario"] = "probe"
        return state

    return solve


def build_probe_task():
    """Inspect Task：三条守卫探针，评分仍用 JSON 目标 + includes。"""
    from inspect_ai import Task
    from inspect_ai.scorer import includes

    return Task(
        dataset=[task.to_sample() for task in PROBE_TASKS],
        solver=probe_solver(),
        scorer=includes(),
        name="guards",
        version="0",
        metadata={"kind": "invariant", "workflow": "invariant"},
    )


def run_probes(log_dir: str | Path | None = None) -> list[Any]:
    from inspect_ai import eval

    root = Path(__file__).resolve().parents[2]
    return eval(
        build_probe_task(),
        model="mockllm/model",
        log_dir=str(log_dir or root / "outputs" / "evaluation"),
        display="none",
    )
