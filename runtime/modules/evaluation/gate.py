# -*- coding: utf-8 -*-
"""发布门禁：把跨 run diff 变成"能不能发布"的结论。

门禁只回答"候选相对基线有没有退化"；它不重新评分（评分在 Inspect），
也不自动改 spec（纠错在 repair）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from modules.diagnosis.diff import RunDiff, diff_reports
from modules.diagnosis.report import Report


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    diff: RunDiff | None = None
    thresholds: dict[str, Any] = field(default_factory=dict)

    def format_text(self) -> str:
        if self.ok:
            return "评测门禁通过"
        return "评测门禁未通过：" + "；".join(self.reasons or ["未知原因"])


def evaluate_gate(
    before: Report,
    after: Report,
    *,
    max_regressions: int = 0,
    max_cost_delta: float | None = None,
    max_duration_delta_s: float | None = None,
    require_identity: bool = True,
) -> GateResult:
    diff = diff_reports(before, after)
    summary = diff.summary()
    reasons: list[str] = []

    if require_identity and diff.identity:
        reasons.extend(diff.identity)
    if summary["regression"] > max_regressions:
        reasons.append(
            f"退化任务 {summary['regression']} 条 > 允许 {max_regressions}"
        )
    if max_cost_delta is not None and summary["cost_delta"] > max_cost_delta:
        reasons.append(
            f"成本增加 {summary['cost_delta']:+.4f} > 允许 {max_cost_delta:+.4f}"
        )
    if max_duration_delta_s is not None and summary["duration_delta"] > max_duration_delta_s:
        reasons.append(
            f"时延增加 {summary['duration_delta']:+.1f}s > 允许 {max_duration_delta_s:+.1f}s"
        )

    return GateResult(
        ok=not reasons,
        reasons=reasons,
        diff=diff,
        thresholds={
            "max_regressions": max_regressions,
            "max_cost_delta": max_cost_delta,
            "max_duration_delta_s": max_duration_delta_s,
            "require_identity": require_identity,
        },
    )


def gate_logs(
    before_path: str,
    after_path: str,
    **kwargs: Any,
) -> GateResult:
    from inspect_ai.log import read_eval_log

    before = Report.from_log(read_eval_log(str(before_path)))
    after = Report.from_log(read_eval_log(str(after_path)))
    return evaluate_gate(before, after, **kwargs)
