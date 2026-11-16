# -*- coding: utf-8 -*-
"""评测的数据形状：输入规格 / 运行结果 / 评分输入。

三分法来自 Inspect AI 的 ``Sample`` vs ``EvalSample``：**Task 不含结果**，
结果只在 ``SampleResult``；scorer 只吃 ``SampleState``。这样"事后重评"才有对象，
而不是只能重跑 agent。

这里只放纯 dataclass / 转换，不 import 任何模型或框架。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Sample:
    """输入规格：跑之前就确定的东西。"""

    id: str
    input: str
    target: dict[str, Any] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    case_id: str = ""
    variant: str = ""
    spec_version: int | None = None


@dataclass
class SampleResult:
    """一次运行的结果侧。"""

    sample_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    output: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    status: str = ""
    duration_ms: int | None = None
    error: str | None = None


@dataclass
class SampleState:
    """scorer 的唯一输入：从 run 目录或内存结果重建。"""

    input: str
    target: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_run(
        cls,
        sample: Sample,
        result: SampleResult,
        scores: dict[str, dict[str, Any]] | None = None,
    ) -> "SampleState":
        return cls(
            input=sample.input,
            target=dict(sample.target),
            output=result.output,
            tool_calls=list(result.tool_calls),
            messages=list(result.messages),
            status=result.status,
            metadata={**dict(sample.metadata), "sample_id": sample.id},
            scores=dict(scores or {}),
        )

    @classmethod
    def from_line(cls, line: dict[str, Any]) -> "SampleState":
        """从 ``lines.jsonl`` 的一行重建；旧行没有 ``state`` 时返回空输入。"""
        state = line.get("state") if isinstance(line.get("state"), dict) else {}
        scores = {
            str(score.get("name")): score
            for score in line.get("scores") or []
            if isinstance(score, dict) and score.get("name")
        }
        return cls(
            input=str(state.get("input") or ""),
            target=dict(state.get("target") or {}),
            output=str(state.get("output") or line.get("answer") or ""),
            tool_calls=list(state.get("tool_calls") or []),
            messages=list(state.get("messages") or []),
            status=str(line.get("status") or ""),
            metadata={"case_id": line.get("case_id"), "trial": line.get("trial")},
            scores=scores,
        )
