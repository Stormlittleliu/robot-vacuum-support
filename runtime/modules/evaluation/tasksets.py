# -*- coding: utf-8 -*-
"""任务集：按场景/工作流组织，带内容指纹，便于复跑与回归比较。

任务只描述输入与可机器判定的成功条件；不绑定模型实现。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from modules.evaluation.tasks import DEFAULT_TASKS, EvalTask


@dataclass(frozen=True)
class TaskSet:
    name: str
    version: str
    tasks: tuple[EvalTask, ...]
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fingerprint(self) -> str:
        payload = [
            {
                "id": task.id,
                "input": task.input,
                "scenario": task.scenario,
                "workflow": task.workflow,
                "expect_contains": list(task.expect_contains),
                "expect_any": list(task.expect_any),
                "expect_refusal": task.expect_refusal,
            }
            for task in self.tasks
        ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def by_workflow(self) -> dict[str, list[EvalTask]]:
        grouped: dict[str, list[EvalTask]] = {}
        for task in self.tasks:
            grouped.setdefault(task.workflow, []).append(task)
        return grouped

    def samples(self) -> list:
        return [task.to_sample() for task in self.tasks]

    @classmethod
    def from_source(
        cls,
        name: str,
        version: str,
        source: object,
        description: str = "",
        tags: tuple[str, ...] = (),
    ) -> "TaskSet":
        """从 ``TaskSource`` 加载任务集；外部数据只产出 Task。"""
        loader = getattr(source, "load", None)
        if not callable(loader):
            raise TypeError("source 必须实现 load() -> list[EvalTask]")
        tasks = tuple(loader())
        if not tasks:
            raise ValueError(f"任务源 {name} 没有任务")
        return cls(name=name, version=version, tasks=tasks, description=description, tags=tags)


STUDY_TASK = EvalTask(
    id="generic-eval-4",
    scenario="demo_flow",
    input="整理 Agent 评测的学习笔记",
    workflow="qa",
    expect_contains=("[",),
    expect_any=("评测", "pass", "token", "成本"),
)

CROSS_TASK = EvalTask(
    id="generic-eval-5",
    scenario="demo_flow",
    input="What should agent evaluation measure?",
    workflow="qa",
    expect_contains=("[",),
    expect_any=("pass", "token", "评测", "成本"),
)

WORKSPACE_SMOKE = TaskSet(
    name="generic_smoke",
    version="1",
    tasks=DEFAULT_TASKS,
    description="通用最小冒烟集；场景与工作流由调用方指定",
    tags=("smoke",),
)

WORKSPACE_FULL = TaskSet(
    name="generic_full",
    version="1",
    tasks=DEFAULT_TASKS + (STUDY_TASK, CROSS_TASK),
    description="通用多工作流任务集",
    tags=("multiflow",),
)

TASKSETS: dict[str, TaskSet] = {
    item.name: item for item in (WORKSPACE_SMOKE, WORKSPACE_FULL)
}


def get_taskset(name: str) -> TaskSet:
    if name not in TASKSETS:
        raise KeyError(f"未知任务集：{name}（可用：{', '.join(sorted(TASKSETS))}）")
    return TASKSETS[name]
