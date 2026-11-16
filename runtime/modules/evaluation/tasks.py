# -*- coding: utf-8 -*-
"""评测任务定义：只描述"输入 + 可机器判定的成功条件"。

默认任务不绑定任何业务场景；scenario / workflow 由调用方显式指定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from inspect_ai.dataset import Sample

REFUSAL_HINTS = ("无法确定", "没有找到", "资料不足", "无法回答", "没有相关")


@dataclass(frozen=True)
class EvalTask:
    id: str
    input: str
    scenario: str = ""
    workflow: str = ""
    expect_contains: tuple[str, ...] = ()
    expect_any: tuple[str, ...] = ()
    expect_refusal: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_sample(self) -> Sample:
        payload = {
            "id": self.id,
            "scenario": self.scenario,
            "workflow": self.workflow,
            "expect_contains": list(self.expect_contains),
            "expect_any": list(self.expect_any),
            "expect_refusal": self.expect_refusal,
            "metadata": dict(self.metadata),
        }
        return Sample(
            input=self.input,
            target=self.scoring_targets(),
            id=self.id,
            metadata={"task": payload},
        )

    def scoring_targets(self) -> list[str]:
        """交给 Inspect 的判分目标：硬条件优先，其次任一关键词。

        评分完全由 Inspect 的 ``includes`` scorer 完成；这里只把"什么算命中"
        转成 target 列表，不在内部重写打分器。
        """
        return scoring_targets({
            "expect_contains": list(self.expect_contains),
            "expect_any": list(self.expect_any),
            "expect_refusal": self.expect_refusal,
        })


def scoring_targets(task: dict[str, Any]) -> list[str]:
    """任务 payload → Inspect target 列表；``includes`` 命中任一即通过。"""
    if task.get("expect_refusal"):
        return list(REFUSAL_HINTS)
    return list(task.get("expect_contains") or task.get("expect_any") or [])


def check_answer(task: dict[str, Any], answer: str) -> tuple[bool, str]:
    """与 Inspect ``includes`` 同语义：命中任一 target 即通过。

    只用于诊断/修复后的复跑校验；正式评分以 Inspect 的 Score 为准。
    """
    text = str(answer or "")
    targets = scoring_targets(task)
    hit = next((item for item in targets if item in text), "")
    if hit:
        return True, f"命中判分目标：{hit}"
    return False, "没有命中任一判分目标"


DEFAULT_TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        id="generic-eval-1",
        input="Agent 评测要看什么指标？",
        scenario="demo_flow",
        workflow="qa",
        expect_contains=("[",),
        expect_any=("pass", "token", "成本", "时延"),
    ),
    EvalTask(
        id="generic-eval-2",
        input="火星上有地铁吗？",
        scenario="demo_flow",
        workflow="qa",
        expect_refusal=True,
    ),
    EvalTask(
        id="generic-eval-3",
        input="编码 Agent 有哪些基础工具？",
        scenario="demo_flow",
        workflow="qa",
        expect_contains=("[",),
        expect_any=("read", "write", "grep", "run_command"),
    ),
)
