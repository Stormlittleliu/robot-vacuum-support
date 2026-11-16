# -*- coding: utf-8 -*-
"""评测入口的公共类型与 M0 组装。

评测底座是 Inspect AI；本包只负责：
- 把场景工作流包成 Solver；
- 记录 scenario → workflow → node → tool 的归属；
- 用 outcome scorer 判分。
"""
from core.evaluation.attribution import AttributionRecorder, Step
from core.evaluation.tasks import DEFAULT_TASKS, EvalTask

__all__ = ["AttributionRecorder", "DEFAULT_TASKS", "EvalTask", "Step"]
