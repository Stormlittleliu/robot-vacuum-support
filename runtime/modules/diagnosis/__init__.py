# -*- coding: utf-8 -*-
"""诊断：场景内失败定位、报告、跨 run 对比、修复建议。

不定义 Score；不直接写 active spec。
"""
from modules.diagnosis.attribution import AttributionRecorder, Step
from modules.diagnosis.diagnosis import Diagnosis, diagnose_task
from modules.diagnosis.diff import Change, RunDiff, diff_reports
from modules.diagnosis.report import Report, TrialRecord, latest_log, latest_logs
from modules.diagnosis.repair import (
    Remedy,
    RepairResult,
    apply_remedies,
    plan_remedies,
    verify_repair,
    write_candidate,
)

__all__ = [
    "AttributionRecorder",
    "Change",
    "Diagnosis",
    "Remedy",
    "Report",
    "RepairResult",
    "RunDiff",
    "Step",
    "TrialRecord",
    "apply_remedies",
    "diagnose_task",
    "diff_reports",
    "latest_log",
    "latest_logs",
    "plan_remedies",
    "verify_repair",
    "write_candidate",
]
