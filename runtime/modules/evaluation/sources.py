# -*- coding: utf-8 -*-
"""任务源适配：外部数据只负责产出 ``EvalTask``，执行仍走统一 runner。

公开 benchmark 的数据正文不进仓库；本模块只定义"怎么读"，具体源由使用方提供。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from modules.evaluation.tasks import EvalTask


class TaskSource(Protocol):
    def load(self) -> list[EvalTask]: ...


@dataclass(frozen=True)
class JsonlTaskSource:
    """一行一个任务：``id / input / workflow / expect_*``。

    这是最小适配面：BFCL / RAG / MultiHop 等源只需把字段映射到这些键，
    不必再写一套 runner。
    """

    path: str | Path
    scenario: str = ""
    default_workflow: str = ""

    def load(self) -> list[EvalTask]:
        file = Path(self.path)
        if not file.is_file():
            raise FileNotFoundError(f"任务文件不存在：{file}")
        tasks: list[EvalTask] = []
        for lineno, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError as exc:
                raise ValueError(f"{file}:{lineno} 不是合法 JSON：{exc}") from exc
            if not payload.get("input"):
                raise ValueError(f"{file}:{lineno} 缺少 input")
            tasks.append(EvalTask(
                id=str(payload.get("id") or f"line-{lineno}"),
                input=str(payload["input"]),
                scenario=str(payload.get("scenario") or self.scenario),
                workflow=str(payload.get("workflow") or self.default_workflow),
                expect_contains=tuple(payload.get("expect_contains") or ()),
                expect_any=tuple(payload.get("expect_any") or ()),
                expect_refusal=bool(payload.get("expect_refusal") or False),
                metadata=dict(payload.get("metadata") or {}),
            ))
        return tasks
