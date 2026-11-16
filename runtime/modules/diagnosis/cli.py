# -*- coding: utf-8 -*-
"""评测 CLI：report（三层汇总）、repair（诊断 → 修复 → 候选 spec）、diff（跨 run）。

    python -m core.evaluation.cli report --log outputs/evaluation/xxx.eval
    python -m core.evaluation.cli report --log-dir outputs/evaluation
    python -m core.evaluation.cli repair --log-dir outputs/evaluation --candidate
    python -m core.evaluation.cli diff --log-dir outputs/evaluation

repair 默认只打印方案；``--candidate`` 才把机械修复写成候选 spec（不切 active）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from inspect_ai.log import read_eval_log  # noqa: E402

from core.evaluation.gate import gate_logs  # noqa: E402
from modules.diagnosis.diff import diff_reports  # noqa: E402
from modules.diagnosis.repair import (  # noqa: E402
    apply_remedies,
    plan_remedies,
    verify_repair,
    write_candidate,
)
from modules.diagnosis.report import Report, latest_log, latest_logs  # noqa: E402
from modules.scenario_format.spec import load_active_spec  # noqa: E402


def load_report(path: str | Path) -> Report:
    return Report.from_log(read_eval_log(str(path)))


def _failed_diagnoses(report: Report) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for trial in report.trials:
        if not trial.passed:
            items.extend(trial.diagnoses)
    return items


def plan_from_report(report: Report) -> list[Any]:
    """按 workflow 分组，生成修复方案（同一 workflow 的重复诊断去重）。"""
    remedies: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for trial in report.trials:
        if trial.passed or not trial.diagnoses:
            continue
        spec = load_active_spec(trial.scenario).model_dump(mode="json")
        for remedy in plan_remedies(spec, trial.workflow, trial.diagnoses):
            key = (trial.workflow, remedy.code)
            if key in seen:
                continue
            seen.add(key)
            remedies.append(remedy)
    return remedies


def repair(
    report: Report,
    *,
    write: bool = False,
    root: str | None = None,
) -> tuple[list[Any], list[str], list[Any], Any]:
    """返回 (remedies, 写出的候选路径, 校验问题, 修复结果)。"""
    remedies = plan_from_report(report)
    if not remedies:
        return [], [], [], None
    scenario = next((trial.scenario for trial in report.trials if not trial.passed), "")
    spec = load_active_spec(scenario).model_dump(mode="json")
    result = apply_remedies(spec, remedies, tool_names=set(spec.get("tools") or []))
    paths: list[str] = []
    if write and result.ok and result.applied:
        paths.append(write_candidate(result.draft, root=root))
    return remedies, paths, list(result.issues), result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测报告 / 纠错 / 跨 run 对比 / 门禁")
    parser.add_argument("command", choices=["report", "repair", "diff", "gate"])
    parser.add_argument("--log", default="", help="指定 .eval 文件；默认取 log-dir 最新")
    parser.add_argument("--before", default="", help="diff 的基线 .eval")
    parser.add_argument("--after", default="", help="diff 的候选 .eval")
    parser.add_argument("--log-dir", default=str(ROOT / "outputs" / "evaluation"))
    parser.add_argument("--candidate", action="store_true", help="把机械修复写成候选 spec")
    parser.add_argument("--verify", action="store_true", help="用修复后的 spec 复跑原失败任务")
    parser.add_argument("--root", default=None, help="候选写入的 spec 根目录（测试用）")
    parser.add_argument("--max-regressions", type=int, default=0, help="gate 允许的退化任务数")
    parser.add_argument("--max-cost-delta", type=float, default=None, help="gate 允许的成本增量")
    parser.add_argument(
        "--max-duration-delta",
        type=float,
        default=None,
        help="gate 允许的时延增量（秒）",
    )
    parser.add_argument(
        "--allow-identity-mismatch",
        action="store_true",
        help="gate 忽略任务集/spec/模型身份不一致",
    )
    args = parser.parse_args(argv)

    if args.command == "gate":
        if args.before and args.after:
            paths = [Path(args.before), Path(args.after)]
        else:
            paths = latest_logs(args.log_dir, 2)
        result = gate_logs(
            str(paths[0]),
            str(paths[1]),
            max_regressions=args.max_regressions,
            max_cost_delta=args.max_cost_delta,
            max_duration_delta_s=args.max_duration_delta,
            require_identity=not args.allow_identity_mismatch,
        )
        if result.diff is not None:
            print(result.diff.format_text())
        print(result.format_text())
        return 0 if result.ok else 1

    if args.command == "diff":
        if args.before and args.after:
            paths = [Path(args.before), Path(args.after)]
        else:
            paths = latest_logs(args.log_dir, 2)
        print(f"before: {paths[0]}")
        print(f"after:  {paths[1]}")
        diff = diff_reports(load_report(paths[0]), load_report(paths[1]))
        print(diff.format_text())
        return 0

    path = Path(args.log) if args.log else latest_log(args.log_dir)
    report = load_report(path)
    print(f"log: {path}")
    print(report.format_text())
    if args.command == "repair":
        remedies, paths, issues, result = repair(report, write=args.candidate, root=args.root)
        print("remedies:")
        for remedy in remedies:
            flag = "manual" if remedy.manual else "auto"
            print(f"  [{flag}/{remedy.confidence}] {remedy.code}: {remedy.title}")
            print(f"    {remedy.detail}")
            for op in remedy.ops:
                print(f"    op: {op}")
        for issue in issues:
            print(f"  issue: {issue}")
        for candidate in paths:
            print(f"candidate: {candidate}")
        if args.verify and result is not None and result.ok:
            failed = [trial for trial in report.trials if not trial.passed][:1]
            if failed:
                trial = failed[0]
                results = verify_repair(
                    result.draft,
                    trial.workflow,
                    [trial.task],
                    scenario=trial.scenario,
                )
                for item in results:
                    print(
                        f"verify {item['task_id']}: "
                        f"{'fixed' if item['passed'] else 'still failing'} — {item['reason']}"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
