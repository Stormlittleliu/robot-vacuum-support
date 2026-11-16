# -*- coding: utf-8 -*-
"""评测接口（Protocol）：Task / Run / Scorer / Log。

只定义形状，不含实现；实现分别在 ``sample.py``、``records.py``、``scorers.py``。
旧口径（``pass^k`` / ``overall`` / ``behavior/judge/outcome``）**不进入这里**，
它们只作为 legacy 视图存在。

第二轮才落 ``legacy_log.py`` / ``rescore`` / ``recompute_metrics``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, runtime_checkable

from modules.evaluation.records import Score
from modules.evaluation.sample import Sample, SampleResult, SampleState


@dataclass(frozen=True)
class Task:
    """一条评测输入；与 ``Sample`` 同形，保留独立名字给外部调用方。"""

    id: str
    input: str
    target: dict[str, Any] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    variant: str = ""


@dataclass(frozen=True)
class Run:
    """一次批量跑的运行头（``run.json`` 的稳定部分）。"""

    run_id: str
    identity: str = ""
    kind: str = ""
    status: str = ""
    started_at: str = ""
    finished_at: str = ""
    task_count: int = 0
    scorers: tuple[str, ...] = ()
    headline: dict[str, Any] | None = None
    spec_ref: dict[str, Any] | None = None


@runtime_checkable
class Scorer(Protocol):
    name: str
    version: str
    data_type: str
    requires_model: bool
    metrics: tuple[str, ...]

    def __call__(self, state: SampleState, target: dict[str, Any]) -> Score | None: ...


@runtime_checkable
class Log(Protocol):
    """目录即日志：只读契约，不规定存储实现。"""

    def header(self) -> Run: ...

    def tasks(self) -> Iterator[Sample]: ...

    def results(self) -> Iterator[SampleResult]: ...

    def state(self, task_id: str, trial: int) -> SampleState: ...
