# -*- coding: utf-8 -*-
"""发布门禁配置：每个场景一个 ``gate.yml``。

只保存阈值和基线，不保存一次评测的结论；结论由 publish API 现算。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.paths import SPEC_ROOT

GATE_FILE = "gate.yml"
DEFAULT_MIN_PASS_RATE = 0.8


def gate_path(name: str, root: str | Path = SPEC_ROOT) -> Path:
    return Path(root) / name / GATE_FILE


def load_gate(name: str, root: str | Path = SPEC_ROOT) -> dict[str, Any]:
    path = gate_path(name, root)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        if isinstance(raw, dict):
            data = raw
    try:
        min_pass_rate = float(data.get("min_pass_rate", DEFAULT_MIN_PASS_RATE))
    except (TypeError, ValueError):
        min_pass_rate = DEFAULT_MIN_PASS_RATE
    min_pass_rate = min(1.0, max(0.0, min_pass_rate))
    baseline = str(data.get("baseline_run") or "").strip()
    return {
        "min_pass_rate": min_pass_rate,
        "baseline_run": baseline or None,
    }


def save_gate(
    name: str,
    *,
    min_pass_rate: float = DEFAULT_MIN_PASS_RATE,
    baseline_run: str | None = None,
    root: str | Path = SPEC_ROOT,
) -> dict[str, Any]:
    payload = {
        "min_pass_rate": min(1.0, max(0.0, float(min_pass_rate))),
        "baseline_run": str(baseline_run or "").strip() or None,
    }
    path = gate_path(name, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(path)
    return load_gate(name, root)
