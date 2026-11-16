# -*- coding: utf-8 -*-
"""评测报告：把 Inspect 的逐条结果汇总成 workflow / node / tool 三层。

Inspect 负责"跑与存"；这里负责"看完之后知道下一步改哪"。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspect_ai.scorer import value_to_float

from modules.diagnosis.diagnosis import diagnose_task
SCORER_NAME = "includes"
_TO_FLOAT = value_to_float()


@dataclass
class TrialRecord:
    task_id: str
    workflow: str
    passed: bool
    scenario: str = ""
    answer: str = ""
    task: dict[str, Any] = field(default_factory=dict)
    attribution: dict[str, Any] = field(default_factory=dict)
    diagnoses: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    tokens: int = 0
    cost: float = 0.0
    duration_s: float = 0.0

    @property
    def diagnosis_codes(self) -> list[str]:
        return [str(item.get("code") or "") for item in self.diagnoses]

    @property
    def blame_nodes(self) -> list[str]:
        return [str(item.get("blame", {}).get("node") or "") for item in self.diagnoses]

    @property
    def blame_tools(self) -> list[str]:
        return [str(item.get("blame", {}).get("tool") or "") for item in self.diagnoses]


@dataclass
class Report:
    trials: list[TrialRecord] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_log(cls, log: Any) -> "Report":
        trials: list[TrialRecord] = []
        for sample in log.samples or []:
            metadata = sample.metadata or {}
            task = metadata.get("task") or {}
            score = (sample.scores or {}).get(SCORER_NAME)
            if score is None and sample.scores:
                score = next(iter(sample.scores.values()))
            passed = bool(score is not None and _TO_FLOAT(score.value) >= 1.0)
            answer = ""
            output = getattr(sample, "output", None)
            if output is not None:
                answer = str(getattr(output, "completion", "") or "")
            attribution = metadata.get("attribution") or {}
            run_error = str(metadata.get("run_error") or "")
            diagnoses = diagnose_task(
                task,
                answer,
                passed,
                attribution,
                run_error=run_error,
            )
            usage = sample.model_usage or {}
            tokens = sum(
                int((item.get("total_tokens", 0) if isinstance(item, dict) else getattr(item, "total_tokens", 0)) or 0)
                for item in usage.values()
            )
            cost = sum(
                float((item.get("total_cost", 0) if isinstance(item, dict) else getattr(item, "total_cost", 0)) or 0)
                for item in usage.values()
            )
            trials.append(TrialRecord(
                task_id=str(sample.id),
                workflow=str(metadata.get("workflow") or task.get("workflow") or ""),
                scenario=str(metadata.get("scenario") or task.get("scenario") or ""),
                passed=passed,
                answer=answer,
                task=dict(task),
                attribution={
                    "nodes": attribution.get("nodes") or [],
                    "tools": attribution.get("tools") or [],
                    "errors": attribution.get("errors") or [],
                    "steps": attribution.get("steps") or [],
                },
                diagnoses=[item.to_dict() for item in diagnoses],
                error=run_error,
                tokens=tokens,
                cost=cost,
                duration_s=float(getattr(sample, "total_time", 0) or 0),
            ))
        meta = dict(getattr(log.eval, "metadata", None) or {})
        meta.update({
            "task": getattr(log.eval, "task", ""),
            "model": getattr(log.eval, "model", ""),
            "created": str(getattr(log.eval, "created", "")),
            "eval_id": getattr(log.eval, "eval_id", ""),
        })
        return cls(trials=trials, meta=meta)

    def summary(self) -> dict[str, Any]:
        total = len(self.trials)
        passed = sum(1 for item in self.trials if item.passed)
        by_workflow: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
        failure_codes: Counter[str] = Counter()
        advisory_codes: Counter[str] = Counter()
        by_node: dict[str, int] = Counter()
        by_tool: dict[str, int] = Counter()
        first_failing: Counter[str] = Counter()
        manual = 0
        for trial in self.trials:
            bucket = by_workflow[trial.workflow or "（未标注）"]
            bucket["total"] += 1
            bucket["passed"] += int(trial.passed)
            for diagnosis in trial.diagnoses:
                code = str(diagnosis.get("code") or "")
                if trial.passed:
                    advisory_codes[code] += 1
                    continue
                failure_codes[code] += 1
                blame = diagnosis.get("blame") or {}
                if blame.get("node"):
                    by_node[str(blame["node"])] += 1
                if blame.get("tool"):
                    by_tool[str(blame["tool"])] += 1
                if diagnosis.get("manual"):
                    manual += 1
            if not trial.passed and trial.diagnoses:
                first = trial.diagnoses[0].get("blame") or {}
                first_failing[str(first.get("node") or "（未知）")] += 1
        return {
            "total": total,
            "passed": passed,
            "pass_rate": (passed / total) if total else 0.0,
            "by_workflow": dict(by_workflow),
            "failure_codes": dict(failure_codes),
            "advisory_codes": dict(advisory_codes),
            "by_node": dict(by_node),
            "by_tool": dict(by_tool),
            "first_failing": dict(first_failing),
            "manual": manual,
            "tokens": sum(item.tokens for item in self.trials),
            "cost": sum(item.cost for item in self.trials),
            "duration_s": sum(item.duration_s for item in self.trials),
        }

    def format_text(self) -> str:
        summary = self.summary()
        lines = [
            f"tasks: {summary['passed']}/{summary['total']} passed"
            f"（{summary['pass_rate']:.1%}）",
        ]
        for workflow, bucket in sorted(summary["by_workflow"].items()):
            lines.append(
                f"  workflow {workflow}: {bucket['passed']}/{bucket['total']}"
            )
        if summary["failure_codes"]:
            lines.append("failure codes:")
            for code, count in sorted(summary["failure_codes"].items(), key=lambda item: -item[1]):
                lines.append(f"  {code}: {count}")
        if summary["advisory_codes"]:
            lines.append("advisories（通过但有质量提示）:")
            for code, count in sorted(summary["advisory_codes"].items(), key=lambda item: -item[1]):
                lines.append(f"  {code}: {count}")
        if summary["by_node"]:
            lines.append("blamed nodes:")
            for node, count in sorted(summary["by_node"].items(), key=lambda item: -item[1]):
                lines.append(f"  {node}: {count}")
        if summary["by_tool"]:
            lines.append("blamed tools:")
            for tool, count in sorted(summary["by_tool"].items(), key=lambda item: -item[1]):
                lines.append(f"  {tool}: {count}")
        if summary["manual"]:
            lines.append(f"需要人工判断：{summary['manual']} 条")
        for trial in self.trials:
            if trial.passed:
                continue
            codes = ", ".join(str(item.get("code")) for item in trial.diagnoses) or "unknown"
            node = (trial.diagnoses[0].get("blame", {}).get("node") if trial.diagnoses else "")
            lines.append(f"  - {trial.task_id}: {codes} @ {node or 'workflow'}")
        return "\n".join(lines)


def latest_log(log_dir: str | Path) -> Path:
    directory = Path(log_dir)
    logs = sorted(directory.glob("*.eval"), key=lambda path: path.stat().st_mtime)
    if not logs:
        raise FileNotFoundError(f"{directory} 里没有 .eval 日志")
    return logs[-1]


def latest_logs(log_dir: str | Path, count: int = 2) -> list[Path]:
    directory = Path(log_dir)
    logs = sorted(directory.glob("*.eval"), key=lambda path: path.stat().st_mtime)
    if len(logs) < count:
        raise FileNotFoundError(f"{directory} 里不足 {count} 份 .eval 日志")
    return logs[-count:]
