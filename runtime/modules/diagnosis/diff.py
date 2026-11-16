# -*- coding: utf-8 -*-
"""跨 run 对比：同一任务集两次评测，谁退化、谁修好、成本变了多少。

只比较"任务 id 相同"的 trial；身份不一致（任务集/ spec / 模型）先给出警告，
避免把不可比的两份结果放在一起下结论。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from modules.diagnosis.report import Report, TrialRecord

IDENTITY_KEYS = (
    ("taskset_fingerprint", "任务集指纹"),
    ("spec_version", "spec 版本"),
    ("spec_hash", "spec 内容"),
    ("model", "模型"),
)


@dataclass
class Change:
    task_id: str
    workflow: str
    kind: str  # regression / fixed / still_failing
    before: TrialRecord
    after: TrialRecord

    @property
    def before_codes(self) -> list[str]:
        return self.before.diagnosis_codes

    @property
    def after_codes(self) -> list[str]:
        return self.after.diagnosis_codes


@dataclass
class RunDiff:
    before: Report
    after: Report
    changes: list[Change] = field(default_factory=list)
    identity: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        counts = {"regression": 0, "fixed": 0, "still_failing": 0}
        by_workflow: dict[str, dict[str, int]] = defaultdict(
            lambda: {"regression": 0, "fixed": 0, "still_failing": 0}
        )
        for change in self.changes:
            counts[change.kind] += 1
            by_workflow[change.workflow or "（未标注）"][change.kind] += 1
        return {
            **counts,
            "by_workflow": dict(by_workflow),
            "tokens_delta": self.after.summary()["tokens"] - self.before.summary()["tokens"],
            "cost_delta": self.after.summary()["cost"] - self.before.summary()["cost"],
            "duration_delta": (
                self.after.summary()["duration_s"] - self.before.summary()["duration_s"]
            ),
            "identity": list(self.identity),
        }

    def format_text(self) -> str:
        summary = self.summary()
        lines = [
            f"regressions: {summary['regression']}  "
            f"fixed: {summary['fixed']}  "
            f"still failing: {summary['still_failing']}",
            f"tokens Δ {summary['tokens_delta']:+d}  "
            f"cost Δ {summary['cost_delta']:+.4f}  "
            f"time Δ {summary['duration_delta']:+.1f}s",
        ]
        for warning in self.identity:
            lines.append(f"identity warning: {warning}")
        for workflow, bucket in sorted(summary["by_workflow"].items()):
            lines.append(
                f"  workflow {workflow}: "
                f"+{bucket['fixed']} fixed / -{bucket['regression']} regressed / "
                f"{bucket['still_failing']} still failing"
            )
        for change in self.changes:
            if change.kind == "regression":
                cause = ", ".join(change.after_codes) or "unknown"
                node = (
                    change.after.diagnoses[0].get("blame", {}).get("node")
                    if change.after.diagnoses
                    else ""
                )
                lines.append(
                    f"  - regression {change.task_id} @ {node or change.workflow}: {cause}"
                )
            elif change.kind == "fixed":
                lines.append(f"  + fixed {change.task_id}: {change.before.task_id}")
            else:
                cause = ", ".join(change.after_codes) or "unknown"
                lines.append(f"  = still failing {change.task_id}: {cause}")
        return "\n".join(lines)


def diff_reports(before: Report, after: Report) -> RunDiff:
    before_map = {trial.task_id: trial for trial in before.trials}
    after_map = {trial.task_id: trial for trial in after.trials}
    changes: list[Change] = []
    for task_id in sorted(set(before_map) & set(after_map)):
        old = before_map[task_id]
        new = after_map[task_id]
        if old.passed and not new.passed:
            kind = "regression"
        elif not old.passed and new.passed:
            kind = "fixed"
        elif not old.passed and not new.passed:
            kind = "still_failing"
        else:
            continue
        changes.append(Change(
            task_id=task_id,
            workflow=new.workflow or old.workflow,
            kind=kind,
            before=old,
            after=new,
        ))
    identity: list[str] = []
    for key, label in IDENTITY_KEYS:
        old_value = before.meta.get(key)
        new_value = after.meta.get(key)
        if old_value and new_value and old_value != new_value:
            identity.append(f"{label}不一致：{old_value} → {new_value}")
    return RunDiff(before=before, after=after, changes=changes, identity=identity)
