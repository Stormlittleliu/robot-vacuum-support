# -*- coding: utf-8 -*-
"""Offline cold/warm startup profiler for the instance worker.

What it measures
----------------
1. Three fresh Python processes (``--runs 3``).  Each process builds the
   scenario once (cold: module cache empty, worker reuse false) and then
   builds it twice more in the same process (warm: module cache + toolbox
   lru_cache warm, same worker process).
2. The same scenario/workflow initialization as ``worker.scenario_resolve``
   and ``worker.workflow_build``: read spec -> build tool box -> import model
   factory -> assemble Scenario -> resolve execution -> compile LangGraph.
   It does **not** invoke the provider, the agent, a tool, or retrieval.
3. One ``-X importtime`` child on the first-import path.  The parser keeps
   self and cumulative times separate and never adds nested cumulative rows.

Boundaries
----------
- No network call, no provider call, no prompt/body/env value is logged.
- ``NO_PROXY`` is sanitized in the child process exactly like
  ``worker._safe_no_proxy`` does; this is a per-process copy, not a system
  proxy change.
- The model factory is constructed with the configured provider, but a missing
  API key gets a non-secret offline placeholder.  Construction alone does not
  make a request.
- One process per session is the worker's isolation model; this profiler uses
  a separate process for every "cold" sample and never shares agent history or
  compiled graphs between them.

Usage
-----
    python scripts/profile_startup.py
    python scripts/profile_startup.py --runs 3 --warm 2
    python scripts/profile_startup.py --skip-importtime
    python scripts/profile_startup.py --no-trace   # instrumentation-overhead control
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
PYTHON = sys.executable
DEFAULT_OUT = ROOT / "outputs" / "startup-profile"
DEFAULT_SCENARIO = "robot_vacuum"
REPORT_PATH = ROOT / "outputs" / "startup-profile-report.json"

_IMPORTTIME_RE = re.compile(r"^import time:\s+(\d+)\s+\|\s+(\d+)\s+\|\s+(.+?)\s*$")

_KEY_MODULES = (
    "core.scenarios.registry",
    "modules.runtime.workflow",
    "core.scenarios.generic",
    "core.scenarios.pools",
    "core.llm.factory",
    "core.tools.rag.tools",
    "core.tools.rag.service",
    "core.tools.rag.retrievers",
    "core.tools.rag.vector_store",
    "langchain_classic.retrievers",
    "langchain_text_splitters",
    "sentence_transformers",
    "transformers",
    "torch",
)


# ------------------------------------------------------------------ helpers


def _hidden_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _round(value: Any, digits: int = 3) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _sanitize_no_proxy() -> None:
    """Same narrow fix as worker._safe_no_proxy; operate on this process only.

    A bracketed IPv6 entry such as ``[::1]`` makes httpx/openai raise
    ``InvalidURL`` while constructing the client.  Keep unbracketed entries.
    """
    for key in list(os.environ):
        if key.upper() != "NO_PROXY":
            continue
        parts = [part.strip() for part in str(os.environ.get(key) or "").split(",")]
        kept = [
            part
            for part in parts
            if part and not (part.startswith("[") and part.endswith("]"))
        ]
        os.environ[key] = ",".join(kept)


def _safe_rmtree(path: Path, root: Path) -> None:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise ValueError(f"refusing to remove unexpected path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _child_env(out_dir: Path, run_id: str, trace: bool) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(RUNTIME)
    env["AH_SPEC_ROOT"] = str(ROOT)
    # A worker owns one session-local data root.  Use a fresh root per fresh
    # process so run 2/3 do not inherit run 1's Chroma files; warm iterations
    # inside the same process intentionally reuse it.
    env["AH_DATA_ROOT"] = str(out_dir / "runtime-data" / run_id)
    env["AH_RUNTIME_ROOT"] = str(out_dir / "runtime-data" / run_id / "runtime")
    env["AH_OUTPUT_ROOT"] = str(out_dir / "runtime-outputs")
    env["AH_MODEL_ROOT"] = str(ROOT / "assets" / "models")
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Construction only; the profiler never invokes the model or a tool.
    env.setdefault("DEEPSEEK_API_KEY", "startup-profile-offline-placeholder")
    env["RVS_LATENCY_TRACE"] = "1" if trace else "0"
    env["RVS_LATENCY_PROCESS"] = "startup-profile"
    env["RVS_LATENCY_DIR"] = str(out_dir / "spans" / run_id)
    return env


def _run_child(
    args: list[str],
    *,
    out_dir: Path,
    run_id: str,
    trace: bool,
    stdout_path: Path,
    stderr_path: Path,
) -> int:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("w", encoding="utf-8") as out_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as err_handle:
        completed = subprocess.run(
            [PYTHON, "-B", *args],
            cwd=str(ROOT),
            env=_child_env(out_dir, run_id, trace),
            stdout=out_handle,
            stderr=err_handle,
            text=True,
            timeout=180,
            **_hidden_kwargs(),
        )
    return int(completed.returncode)


# ------------------------------------------------------------------ child


def _bootstrap_child(args: argparse.Namespace) -> int:
    """Measure the eager worker-module import without building a scenario."""
    _sanitize_no_proxy()
    from core.observability import latency

    started = time.perf_counter()
    with latency.span("worker.bootstrap_import"):
        import modules.instances.runtime.execution  # noqa: F401
    result = {
        "schema_version": "startup-profile-bootstrap/v1",
        "generated_at": _now(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "pid": os.getpid(),
        "bootstrap_import_ms": _round((time.perf_counter() - started) * 1000.0),
        "provider_invoked": False,
    }
    Path(args.result_file).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


def _child_main(args: argparse.Namespace) -> int:
    _sanitize_no_proxy()
    import builtins as _builtins

    from core.observability import latency

    boot: list[dict[str, Any]] = []

    def first_import(module_name: str):
        started = time.perf_counter()
        # Use __import__ (not importlib.import_module): the latter hides the
        # top-level module row from ``-X importtime``.  We only want the cold
        # first-import marker here; the actual symbol import follows normally.
        _builtins.__import__(module_name)
        boot.append(
            {
                "module": module_name,
                "cold": True,
                "import_ms": _round((time.perf_counter() - started) * 1000.0),
            }
        )

    first_import("core.scenarios.registry")
    first_import("modules.runtime.workflow")
    from core.scenarios.registry import get_scenario
    from modules.runtime.workflow import WorkflowRuntime

    iterations: list[dict[str, Any]] = []
    total = int(args.warm) + 1
    for index in range(total):
        cold = index == 0
        with latency.bind_context(cold=cold, iteration=index):
            started = time.perf_counter()
            with latency.span("worker.scenario_resolve", scenario=args.scenario):
                scenario = get_scenario(args.scenario)
                with latency.span(
                    "worker.scenario_resolve_execution", scenario=args.scenario
                ):
                    scenario.resolve_execution()
            resolve_done = time.perf_counter()
            with latency.span("worker.workflow_build", scenario=args.scenario):
                runtime = WorkflowRuntime(scenario)
                workflows = runtime.list_workflows()
            workflow_done = time.perf_counter()
        iterations.append(
            {
                "iteration": index,
                "cold": cold,
                "worker_reuse": not cold,
                "scenario_resolve_ms": _round((resolve_done - started) * 1000.0),
                "workflow_build_ms": _round((workflow_done - resolve_done) * 1000.0),
                "total_init_ms": _round((workflow_done - started) * 1000.0),
                "workflows": workflows,
            }
        )
        del runtime, scenario

    result = {
        "schema_version": "startup-profile-child/v1",
        "generated_at": _now(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "pid": os.getpid(),
        "scenario": args.scenario,
        "boot_imports": boot,
        "iterations": iterations,
        "provider_invoked": False,
        "tool_invoked": False,
    }
    Path(args.result_file).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


# ------------------------------------------------------------------ span analysis


def _spans(span_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(span_dir.glob("spans_*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def _resolve_spans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (row for row in rows if row.get("stage") == "worker.scenario_resolve"),
        key=lambda row: row.get("start_mono_ns") or 0,
    )


def _workflow_spans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (row for row in rows if row.get("stage") == "worker.workflow_build"),
        key=lambda row: row.get("start_mono_ns") or 0,
    )


def _child_spans(rows: list[dict[str, Any]], parent_id: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("parent_span_id") == parent_id]


def _stage_ms(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {
        str(row.get("stage")): _round(row.get("duration_ms")) or 0.0
        for row in rows
    }


def _tree(rows: list[dict[str, Any]], root_ids: set[str]) -> list[dict[str, Any]]:
    """Flatten a parent/child span tree, keeping nested rows visibly nested."""
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        parent = str(row.get("parent_span_id") or "")
        by_parent.setdefault(parent, []).append(row)
    out: list[dict[str, Any]] = []

    def walk(parent_id: str, depth: int) -> None:
        for row in sorted(
            by_parent.get(parent_id, []),
            key=lambda item: item.get("start_mono_ns") or 0,
        ):
            out.append(
                {
                    "depth": depth,
                    "stage": row.get("stage"),
                    "duration_ms": _round(row.get("duration_ms")),
                    "status": row.get("status"),
                }
            )
            walk(str(row.get("span_id")), depth + 1)

    for root_id in sorted(root_ids):
        walk(root_id, 0)
    return out


def _analyse_run(
    run_id: str,
    result: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolves = _resolve_spans(rows)
    workflows = _workflow_spans(rows)
    out: list[dict[str, Any]] = []
    for iteration in result.get("iterations") or []:
        index = int(iteration["iteration"])
        resolve = resolves[index] if index < len(resolves) else None
        workflow = workflows[index] if index < len(workflows) else None
        phases: dict[str, float] = {}
        graph_compile_ms = None
        workflow_tree: list[dict[str, Any]] = []
        if resolve is not None:
            children = _child_spans(rows, str(resolve.get("span_id")))
            phases = _stage_ms(children)
            workflow_tree = _tree(rows, {str(resolve.get("span_id"))})
        if workflow is not None:
            workflow_children = _child_spans(rows, str(workflow.get("span_id")))
            for child in workflow_children:
                if child.get("stage") == "worker.graph_compile":
                    graph_compile_ms = _round(child.get("duration_ms"))
        out.append(
            {
                "run": run_id,
                "iteration": index,
                "cold": bool(iteration.get("cold")),
                "worker_reuse": bool(iteration.get("worker_reuse")),
                "scenario_resolve_ms": iteration.get("scenario_resolve_ms"),
                "workflow_build_ms": iteration.get("workflow_build_ms"),
                "total_init_ms": iteration.get("total_init_ms"),
                "scenario_resolve_phases_ms": phases,
                "scenario_resolve_tree": workflow_tree,
                "graph_compile_ms": graph_compile_ms,
            }
        )
    return out


# ------------------------------------------------------------------ importtime


def _parse_importtime(text: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _IMPORTTIME_RE.match(line)
        if not match:
            continue
        rows.append(
            {
                "self_us": int(match.group(1)),
                "cumulative_us": int(match.group(2)),
                "name": match.group(3).strip(),
            }
        )

    # The same module can appear more than once.  Keep the largest row per name
    # for both views; never add nested cumulative rows together.
    by_self: dict[str, dict[str, Any]] = {}
    by_cumulative: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row["name"]
        if name not in by_self or row["self_us"] > by_self[name]["self_us"]:
            by_self[name] = row
        if (
            name not in by_cumulative
            or row["cumulative_us"] > by_cumulative[name]["cumulative_us"]
        ):
            by_cumulative[name] = row

    def pick(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "module": item["name"],
            "self_ms": _round(item["self_us"] / 1000.0),
            "cumulative_ms": _round(item["cumulative_us"] / 1000.0),
        }

    top_self = [pick(row) for row in sorted(by_self.values(), key=lambda r: -r["self_us"])[:20]]
    top_cumulative = [
        pick(row)
        for row in sorted(by_cumulative.values(), key=lambda r: -r["cumulative_us"])[:20]
    ]
    key_modules = [
        pick(by_cumulative[name]) for name in _KEY_MODULES if name in by_cumulative
    ]
    return {
        "row_count": len(rows),
        "top_self": top_self,
        "top_cumulative": top_cumulative,
        "key_modules": key_modules,
        "note": "self and cumulative are separate views; nested cumulative rows are not additive",
    }


# ------------------------------------------------------------------ parent


def _stats(values: list[float | None]) -> dict[str, Any]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"n": 0, "median": None, "min": None, "max": None}
    return {
        "n": len(clean),
        "median": _round(statistics.median(clean)),
        "min": _round(min(clean)),
        "max": _round(max(clean)),
    }


def _summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    cold = [row for row in records if row["cold"]]
    warm = [row for row in records if not row["cold"]]
    phase_names = sorted(
        {
            stage
            for row in records
            for stage in (row.get("scenario_resolve_phases_ms") or {})
        }
    )
    phase_stats = {
        stage: _stats(
            [
                (row.get("scenario_resolve_phases_ms") or {}).get(stage)
                for row in records
            ]
        )
        for stage in phase_names
    }
    phase_stats_cold = {
        stage: _stats(
            [
                (row.get("scenario_resolve_phases_ms") or {}).get(stage)
                for row in cold
            ]
        )
        for stage in phase_names
    }
    phase_stats_warm = {
        stage: _stats(
            [
                (row.get("scenario_resolve_phases_ms") or {}).get(stage)
                for row in warm
            ]
        )
        for stage in phase_names
    }
    return {
        "cold": {
            "n": len(cold),
            "scenario_resolve_ms": _stats([row["scenario_resolve_ms"] for row in cold]),
            "workflow_build_ms": _stats([row["workflow_build_ms"] for row in cold]),
            "graph_compile_ms": _stats([row.get("graph_compile_ms") for row in cold]),
            "total_init_ms": _stats([row["total_init_ms"] for row in cold]),
        },
        "warm_same_process": {
            "n": len(warm),
            "scenario_resolve_ms": _stats([row["scenario_resolve_ms"] for row in warm]),
            "workflow_build_ms": _stats([row["workflow_build_ms"] for row in warm]),
            "graph_compile_ms": _stats([row.get("graph_compile_ms") for row in warm]),
            "total_init_ms": _stats([row["total_init_ms"] for row in warm]),
        },
        "phase_stats": phase_stats,
        "phase_stats_cold": phase_stats_cold,
        "phase_stats_warm": phase_stats_warm,
        "note": "n=3 cold / n=6 warm process samples; descriptive only, not a distribution or significance claim",
    }


def _run_parent(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    report_path = Path(args.report).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    spans_root = out_dir / "spans"
    runs_root = out_dir / "runs"
    spans_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    runs: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index in range(1, int(args.runs) + 1):
        run_id = f"run{index}"
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        span_dir = spans_root / run_id
        _safe_rmtree(span_dir, out_dir)
        result_file = run_dir / "child-result.json"
        code = _run_child(
            [
                str(Path(__file__).resolve()),
                "--child",
                "--scenario",
                args.scenario,
                "--warm",
                str(args.warm),
                "--result-file",
                str(result_file),
            ],
            out_dir=out_dir,
            run_id=run_id,
            trace=not args.no_trace,
            stdout_path=run_dir / "child-stdout.log",
            stderr_path=run_dir / "child-stderr.log",
        )
        if code != 0 or not result_file.is_file():
            tail = ""
            stderr_path = run_dir / "child-stderr.log"
            if stderr_path.is_file():
                tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            failures.append({"run": run_id, "exit_code": code, "stderr_tail": tail})
            runs.append({"run": run_id, "ok": False, "exit_code": code})
            continue
        result = json.loads(result_file.read_text(encoding="utf-8"))
        rows = _spans(span_dir) if not args.no_trace else []
        analysed = _analyse_run(run_id, result, rows)
        runs.append(
            {
                "run": run_id,
                "ok": True,
                "pid": result.get("pid"),
                "python": result.get("python"),
                "boot_imports": result.get("boot_imports"),
                "iterations": result.get("iterations"),
            }
        )
        records.extend(analysed)

    bootstrap = None
    if not args.skip_importtime:
        boot_dir = runs_root / "bootstrap"
        boot_dir.mkdir(parents=True, exist_ok=True)
        boot_result = boot_dir / "child-result.json"
        code = _run_child(
            [
                str(Path(__file__).resolve()),
                "--bootstrap-child",
                "--scenario",
                args.scenario,
                "--result-file",
                str(boot_result),
            ],
            out_dir=out_dir,
            run_id="bootstrap",
            trace=False,
            stdout_path=boot_dir / "child-stdout.log",
            stderr_path=boot_dir / "child-stderr.log",
        )
        if code == 0 and boot_result.is_file():
            bootstrap = json.loads(boot_result.read_text(encoding="utf-8"))
        else:
            failures.append(
                {"run": "bootstrap", "exit_code": code, "stderr_tail": "bootstrap child failed"}
            )

    importtime_profile = None
    if not args.skip_importtime:
        im_dir = runs_root / "importtime"
        im_dir.mkdir(parents=True, exist_ok=True)
        im_span_dir = spans_root / "importtime"
        _safe_rmtree(im_span_dir, out_dir)
        im_result = im_dir / "child-result.json"
        stderr_path = im_dir / "importtime-stderr.txt"
        code = _run_child(
            [
                "-X",
                "importtime",
                str(Path(__file__).resolve()),
                "--child",
                "--scenario",
                args.scenario,
                "--warm",
                "0",
                "--result-file",
                str(im_result),
            ],
            out_dir=out_dir,
            run_id="importtime",
            trace=False,
            stdout_path=im_dir / "child-stdout.log",
            stderr_path=stderr_path,
        )
        if code == 0 and stderr_path.is_file():
            text = stderr_path.read_text(encoding="utf-8", errors="replace")
            importtime_profile = _parse_importtime(text)
            importtime_profile["raw_file"] = str(stderr_path)
            importtime_profile["exit_code"] = code
        else:
            failures.append(
                {"run": "importtime", "exit_code": code, "stderr_tail": "importtime child failed"}
            )

    summary = _summarise(records)
    summary.update(
        {
            "schema_version": "startup-profile-summary/v1",
            "generated_at": _now(),
            "environment": {
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "cwd": str(ROOT),
                "scenario": args.scenario,
                "cold_definition": "first scenario build in a fresh Python process; module cache empty, worker not reused",
                "warm_definition": "second/third scenario build in the same process; module cache + toolbox lru_cache warm, worker reused",
                "os_file_cache_control": "not dropped; process order is the only OS cache proxy",
                "provider_invoked": False,
                "tool_invoked": False,
                "llm_invoked": False,
            },
            "records": records,
            "runs": runs,
            "worker_bootstrap_import": bootstrap,
            "failures": failures,
        }
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    report = {
        "schema_version": "startup-profile-report/v1",
        "generated_at": _now(),
        "status": "complete" if not failures else "complete_with_failures",
        "question": "把 worker.scenario_resolve 的冷启动拆成真实子阶段，并判断是否存在可安全消除的加载浪费。",
        "progress": [
            "已完成 3 个 fresh process 的离线 cold 初始化 + 每个进程 2 次同进程 warm 重复；未调用 provider/LLM/tool。",
            "已用 -X importtime 对首次导入路径做只读 profile；self 与 cumulative 分列，未把嵌套 cumulative 相加。",
            "已在本轮允许文件内加真实子 span；未改 prompt/budget/state/tool schema/tool name。",
        ],
        "summary": summary,
        "worker_bootstrap_import": bootstrap,
        "importtime": importtime_profile,
        "artifacts": {
            "summary": str(out_dir / "summary.json"),
            "runs": str(runs_root),
            "spans": str(spans_root),
            "importtime_raw": (importtime_profile or {}).get("raw_file"),
        },
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"summary": str(out_dir / "summary.json"), "report": str(report_path)}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help="internal child mode")
    parser.add_argument("--bootstrap-child", action="store_true", help="internal bootstrap child mode")
    parser.add_argument("--result-file", default="", help="internal child result path")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warm", type=int, default=2)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument("--skip-importtime", action="store_true")
    parser.add_argument("--no-trace", action="store_true", help="trace-off control run")
    args = parser.parse_args(argv)
    if args.child or args.bootstrap_child:
        if not args.result_file:
            parser.error("child mode requires --result-file")
        if args.bootstrap_child:
            return _bootstrap_child(args)
        return _child_main(args)
    if args.runs < 1 or args.warm < 0:
        parser.error("--runs must be >= 1 and --warm must be >= 0")
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
