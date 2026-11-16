# -*- coding: utf-8 -*-
"""评测输出路径。

旧质量层的 ``data/evals`` / benchmark 路径已移除；新评测只向 ``outputs/`` 落盘。
"""
from __future__ import annotations

from core.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
OUTPUTS_DIR = ROOT / "outputs"
EVALUATION_OUT = OUTPUTS_DIR / "evaluation"
RUNS_OUT = OUTPUTS_DIR / "runs"

__all__ = ["EVALUATION_OUT", "OUTPUTS_DIR", "ROOT", "RUNS_OUT"]
