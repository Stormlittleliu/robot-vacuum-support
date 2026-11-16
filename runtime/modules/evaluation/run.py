# -*- coding: utf-8 -*-
"""M0：把场景工作流包成 Inspect Solver，跑任务并记录归属。

CLI:
    python -m core.evaluation.run --scenario <name> --workflow <workflow>
    python -m core.evaluation.run --limit 2 --log-dir outputs/evaluation

Inspect 的 model 只是占位（``mockllm/model``）；真实模型由 ``model_factory`` 提供，
这样评测不依赖 Inspect 的 provider 配置。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from inspect_ai import Task, eval  # noqa: E402
from inspect_ai.scorer import includes  # noqa: E402
from inspect_ai.solver import TaskState, solver  # noqa: E402

from modules.diagnosis.attribution import AttributionRecorder  # noqa: E402
from modules.evaluation.tasks import DEFAULT_TASKS, EvalTask  # noqa: E402
from modules.evaluation.tasksets import TASKSETS, get_taskset  # noqa: E402
from core.scenarios import pools  # noqa: E402
from core.scenarios.compiler import compile_spec  # noqa: E402
from modules.scenario_format.spec import active_version, load_active_spec  # noqa: E402

DEFAULT_LOG_DIR = ROOT / "outputs" / "evaluation"


@dataclass
class EvalOutcome:
    answer: str
    recorder: AttributionRecorder
    error: str = ""


class WorkflowRunner:
    """把 ``scenario@version`` 的一条 workflow 跑一次，并记录归属。"""

    def __init__(
        self,
        scenario: str = "",
        workflow: str | None = None,
        model_factory: Callable[[str], Any] | None = None,
        tools_factory: Callable[[], list[Any]] | None = None,
        spec: Any = None,
    ):
        self.scenario = scenario
        self.workflow = workflow
        self.spec = spec if spec is not None else load_active_spec(scenario)
        if workflow is not None and workflow not in self.spec.workflows:
            raise KeyError(f"{scenario} 没有工作流 {workflow}")
        self.model_factory = model_factory or self._default_model
        self.tools_factory = tools_factory or (lambda: list(pools.toolbox(scenario).tools))

    @staticmethod
    def _default_model(query: str):
        from core.llm.factory import chat_model

        return chat_model

    def run(self, query: str, workflow: str | None = None) -> EvalOutcome:
        chosen = workflow or self.workflow
        if not chosen:
            raise ValueError("WorkflowRunner 需要 workflow（构造参数或 run 参数）")
        if chosen not in self.spec.workflows:
            raise KeyError(f"{self.scenario} 没有工作流 {chosen}")
        model = self.model_factory(query)
        compiled = compile_spec(
            self.spec,
            tools=self.tools_factory(),
            model=model,
        ).workflows[chosen]
        recorder = AttributionRecorder(workflow=chosen)
        try:
            state = compiled.graph.invoke(
                {"input": query},
                config={"callbacks": [recorder]},
            )
        except Exception as exc:  # noqa: BLE001 - 失败要进诊断，而不是把评测跑挂
            return EvalOutcome(
                answer="",
                recorder=recorder,
                error=f"{type(exc).__name__}: {exc}",
            )
        answer = str(state.get("answer") or state.get("tool_result") or "")
        return EvalOutcome(answer=answer, recorder=recorder)


_RUNNERS: dict[str, WorkflowRunner] = {}


def _register_runner(runner: WorkflowRunner) -> str:
    runner_id = uuid.uuid4().hex
    _RUNNERS[runner_id] = runner
    return runner_id


@solver
def scenario_solver(runner_id: str):
    """Inspect Solver 工厂：按 sample 上登记的 runner 跑一次场景工作流。

    必须是模块级、且返回 async solve 的工厂：Inspect 的 ``@solver`` 需要
    从工厂参数里提取 registry params，才能把 plan 写进日志。
    """

    async def solve(state: TaskState, generate: Any) -> TaskState:
        runner = _RUNNERS.get(runner_id)
        if runner is None:
            raise RuntimeError(f"找不到 runner：{runner_id}")
        task = (state.metadata or {}).get("task") or {}
        outcome = runner.run(state.input_text, workflow=str(task.get("workflow") or "") or None)
        state.output.completion = outcome.answer
        state.metadata["attribution"] = outcome.recorder.summary()
        state.metadata["run_error"] = outcome.error
        state.metadata["scenario"] = runner.scenario
        state.metadata["workflow"] = outcome.recorder.workflow
        return state

    return solve


def _spec_identity(runner: WorkflowRunner) -> tuple[int, str]:
    payload = (
        runner.spec.model_dump(mode="json")
        if hasattr(runner.spec, "model_dump")
        else runner.spec
    )
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    try:
        version = int(active_version(runner.scenario))
    except Exception:  # noqa: BLE001 - 没有版本目录时不影响评测
        version = 0
    return version, digest


def build_inspect_task(
    runner: WorkflowRunner,
    tasks: list[EvalTask] | tuple[EvalTask, ...] = DEFAULT_TASKS,
    name: str | None = None,
    version: str = "0",
    taskset: Any = None,
) -> Task:
    runner_id = _register_runner(runner)
    samples = [task.to_sample() for task in tasks]
    workflows = sorted({task.workflow for task in tasks})
    spec_version, spec_hash = _spec_identity(runner)
    return Task(
        dataset=samples,
        solver=scenario_solver(runner_id),
        scorer=includes(),
        name=name or f"{runner.scenario}.tasks",
        version=version,
        metadata={
            "scenario": runner.scenario,
            "workflows": workflows,
            "spec_version": spec_version,
            "spec_hash": spec_hash,
            "taskset": getattr(taskset, "name", ""),
            "taskset_version": getattr(taskset, "version", ""),
            "taskset_fingerprint": getattr(taskset, "fingerprint", ""),
        },
    )


def run_eval(
    runner: WorkflowRunner,
    tasks: list[EvalTask] | tuple[EvalTask, ...] = DEFAULT_TASKS,
    limit: int = 0,
    log_dir: str | Path | None = None,
    taskset: Any = None,
) -> list[Any]:
    selected = list(tasks)[:limit] if limit else list(tasks)
    task = build_inspect_task(runner, selected, taskset=taskset)
    return eval(
        task,
        model="mockllm/model",
        log_dir=str(log_dir or DEFAULT_LOG_DIR),
        display="none",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M0 场景评测（Inspect 底座）")
    parser.add_argument("--scenario", default="")
    parser.add_argument("--taskset", default="workspace_smoke", choices=sorted(TASKSETS))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--list", action="store_true", help="只列任务，不跑")
    parser.add_argument("--probes", action="store_true", help="只跑守卫探针（权限/超时/审批）")
    args = parser.parse_args(argv)

    if args.probes:
        from core.evaluation.probes import run_probes

        logs = run_probes(args.log_dir)
        for log in logs:
            print(f"log: {log.location}  status={log.status}")
            for sample in log.samples or []:
                score = next(iter((sample.scores or {}).values()), None)
                print(f"  {sample.id}\tvalue={getattr(score, 'value', None)}")
        return 0

    if args.list:
        for name, taskset in sorted(TASKSETS.items()):
            print(f"{name}\t{taskset.version}/{taskset.fingerprint}\t{taskset.description}")
            for task in taskset.tasks:
                print(f"  {task.id}\t{task.scenario}/{task.workflow}\t{task.input}")
        return 0

    taskset = get_taskset(args.taskset)
    runner = WorkflowRunner(scenario=args.scenario)
    logs = run_eval(
        runner,
        taskset.tasks,
        limit=args.limit,
        log_dir=args.log_dir,
        taskset=taskset,
    )
    for log in logs:
        print(f"log: {log.location}  status={log.status}")
        for sample in log.samples or []:
            score = next(iter((sample.scores or {}).values()), None)
            value = getattr(score, "value", None) if score else None
            print(f"  {sample.id}\tvalue={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
