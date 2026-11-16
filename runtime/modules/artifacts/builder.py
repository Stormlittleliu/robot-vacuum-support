# -*- coding: utf-8 -*-
"""Artifact 构建器：把 active spec 导出为 schema v2 的独立产物。

``modules.editor.export.export_bundle`` 只做归属门禁并委托到这里，
因此 CLI / 测试可以直接构建产物，不依赖运行中的编辑器进程。

产物根布局（与旧 export 布局兼容）::

    <bundle>/manifest.json
    <bundle>/run.py
    <bundle>/README.md
    <bundle>/requirements.txt
    <bundle>/<name>/active.json ...
    <bundle>/runtime/core, modules, config ...
    <bundle>/serve.py               # stdlib entry, imports modules.instances.runtime.*
    <bundle>/product_ui/            # 默认来自 modules/instances/runtime/web

``serve.py`` 与 ``product_ui`` 在计算 checksums / runtime hash 之前生成；缺少入口或
前端时 ``verify_artifact(require_service=True)`` 明确失败，不会伪造 service 能力。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path
from typing import Any

import yaml

from core.paths import PROJECT_ROOT, SPEC_ROOT
from modules.artifacts.manifest import (
    SCHEMA_VERSION,
    compute_content_hash,
    hash_tree,
)
from modules.artifacts.service_entry import write_service_entry
from modules.editor.ownership import OwnershipError, assert_spec_owned
from modules.scenario_format.spec import active_version, load_spec, resolve_cases_path

EXPORT_ROOT = PROJECT_ROOT / "exports"

#: 场景 config 只复制运行配置文本；脚本、缓存和凭据文件一律不进 bundle。
_CONFIG_SUFFIXES = {".yml", ".yaml", ".json"}

#: 只排除 Web/评测 UI 直接依赖；FastAPI/uvicorn 属于后续 product host 运行依赖。
_EXCLUDED_REQUIREMENTS = {"streamlit", "inspect-ai", "mockllm", "textual", "rich"}

#: product host / runtime 不需要的 UI 应用代码。
_EXCLUDED_RUNTIME_DIRS = (
    "modules/editor/web/",
    "modules/chat/",
)
_EXCLUDED_RUNTIME_FILES = {
    "modules/editor/server.py",
    "modules/editor/run_store.py",
}


def _copy_if_exists(source: Path, target: Path, files: list[str], name: str) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    files.append(name)


def _is_secret_file(filename: str) -> bool:
    """凭据文件不导出；约定文件名含 secret / credential 或 .env。"""
    lower = filename.lower()
    return (
        lower == ".env"
        or lower.startswith(".env.")
        or "secret" in lower
        or "credential" in lower
    )


def _tool_pool_name(name: str) -> str:
    """当前场景使用的工具池名；``.pool`` 覆盖优先，其次场景库的静态映射。"""
    pool_file = SPEC_ROOT / name / ".pool"
    if pool_file.is_file():
        try:
            value = pool_file.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            return value
    try:
        from core.scenarios.library import entry

        found = entry(name)
        if found is not None and found.pool:
            return found.pool
    except Exception:  # noqa: BLE001 - 场景库不可用时不阻断导出，回退到同名池
        pass
    return name


def _copy_scenario_config(
    name: str, bundle: Path, runtime: Path, files: list[str]
) -> str:
    """复制工具池运行所需的场景 config（仅文本配置，排除凭据）。"""
    pool = _tool_pool_name(name)
    source_dir = PROJECT_ROOT / "core" / "scenarios" / pool / "config"
    if not source_dir.is_dir():
        return pool
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file():
            continue
        if "__pycache__" in source.parts or "node_modules" in source.parts:
            continue
        if source.suffix.lower() not in _CONFIG_SUFFIXES:
            continue
        if _is_secret_file(source.name):
            continue
        target = runtime / "core" / "scenarios" / pool / "config" / source.relative_to(source_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files.append(target.relative_to(bundle).as_posix())
    return pool


def _copy_cases(
    spec: Any,
    destination: Path,
    bundle: Path,
    files: list[str],
) -> str | None:
    """把 resolve_cases_path 找到的用例拷进场景目录，返回运行时可解析的相对路径。"""
    source = resolve_cases_path(spec, SPEC_ROOT)
    if source is None and getattr(spec, "cases_path", None):
        # resolve_cases_path 对相对路径按 cwd 解析；导出器可能从别处调用，
        # 再按仓库根兜底一次（仍只使用 spec 明确定义的路径）。
        fallback = Path(str(spec.cases_path))
        if not fallback.is_absolute():
            fallback = PROJECT_ROOT / fallback
        if fallback.is_file():
            source = fallback
    if source is None or not source.is_file():
        return None
    target = destination / "cases.jsonl"
    shutil.copy2(source, target)
    files.append(target.relative_to(bundle).as_posix())
    # run.py 会把 cwd 固定到 <bundle>/runtime；这个相对路径从那里解析。
    return Path(os.path.relpath(target, start=bundle / "runtime")).as_posix()


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _dependencies_not_included(spec: Any, pool: str) -> dict[str, list[str]]:
    """显式列出 bundle 没有复制、运行时需由目标环境提供的依赖。"""
    deps: dict[str, list[str]] = {"knowledge": [], "model": [], "external": []}
    rag = _read_yaml(PROJECT_ROOT / "config" / "rag.yml")
    chroma = _read_yaml(PROJECT_ROOT / "config" / "chroma.yml")

    # Chat API credentials are an instance/environment concern.  They stay in
    # the human-readable dependency list, but never become required_assets
    # (which describe resolvable local files/directories only).
    chat = str(rag.get("chat_model_name") or "").strip()
    if chat:
        provider = str(rag.get("chat_provider") or "").strip()
        base_url = str(rag.get("chat_base_url") or "").strip()
        label = f"{provider} {chat}".strip() if provider else chat
        if base_url:
            label += f" at {base_url}"
        deps["external"].append(f"{label}: chat model API (network + credentials)")

    tools = set(getattr(spec, "tools", None) or [])
    if "rag_summarize" in tools:
        data_path = str(chroma.get("data_path") or "").strip()
        if data_path:
            deps["knowledge"].append(f"{data_path}: RAG source documents (not copied)")
        persist = str(chroma.get("persist_directory") or "").strip()
        if persist:
            deps["knowledge"].append(
                f"{persist}: RAG index (not copied; rebuild from source documents)"
            )
        embedding = str(rag.get("embedding_model_name") or "").strip()
        if embedding:
            deps["model"].append(f"{embedding}: local embedding model (not copied)")

    agent_conf = _read_yaml(
        PROJECT_ROOT / "core" / "scenarios" / pool / "config" / "agent.yml"
    )
    external_data = str(agent_conf.get("external_data_path") or "").strip()
    if external_data:
        deps["external"].append(f"{external_data}: scenario external data (not copied)")

    return {key: _dedupe(value) for key, value in deps.items()}


def _required_assets_for(spec: Any, pool: str) -> list[dict[str, Any]]:
    """Machine-readable required assets, reusing the known RAG rules."""
    from modules.instances.runtime.assets import required_assets_manifest

    return required_assets_manifest(
        tools=getattr(spec, "tools", None) or [],
        rag=_read_yaml(PROJECT_ROOT / "config" / "rag.yml"),
        chroma=_read_yaml(PROJECT_ROOT / "config" / "chroma.yml"),
        agent=_read_yaml(
            PROJECT_ROOT / "core" / "scenarios" / pool / "config" / "agent.yml"
        ),
    )


def _dependencies_markdown(deps: dict[str, list[str]]) -> str:
    titles = (
        ("knowledge", "Knowledge"),
        ("model", "Model"),
        ("external", "External services and data"),
    )
    lines: list[str] = []
    for key, title in titles:
        items = deps.get(key) or []
        if not items:
            continue
        lines.append(f"### {title}")
        lines.extend(f"- `{item}`" for item in items)
        lines.append("")
    return "\n".join(lines).rstrip() if lines else "No extra dependencies declared."


def _runner_source(name: str, workflow: str) -> str:
    return f'''# -*- coding: utf-8 -*-
"""Auto-generated by Agent Harness artifact builder. Run inside the harness environment."""
from __future__ import annotations

import sys

# The artifact is read-only program source: never write bytecode next to it.
sys.dont_write_bytecode = True

import argparse
import json
import os
import tempfile
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
HARNESS = BUNDLE / "runtime"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))


def _writable_root(explicit: str | None) -> Path:
    """Writable data root lives outside the artifact.

    Priority: ``--data-root`` > ``AH_DATA_ROOT`` > a fresh external temp dir.
    The bundled program directory is never used as the data root.
    """
    value = explicit or os.environ.get("AH_DATA_ROOT", "")
    if value.strip():
        root = Path(value).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        return root.resolve()
    return Path(tempfile.mkdtemp(prefix="ah-{name}-run-")).resolve()


def _prepare_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    os.environ["AH_SPEC_ROOT"] = str(BUNDLE)
    os.environ["AH_DATA_ROOT"] = str(root)
    os.environ["AH_RUNTIME_ROOT"] = str(root / "runtime")
    os.environ.setdefault("AH_OUTPUT_ROOT", str(root / "outputs"))
    # Relative writes (if any) go to the writable root, not the artifact.
    os.chdir(root)


def answer_of(result):
    if not isinstance(result, dict):
        return str(result)
    text = result.get("answer") or result.get("tool_result") or ""
    if text:
        return str(text)
    messages = result.get("messages") or []
    if messages:
        return str(getattr(messages[-1], "content", messages[-1]))
    return json.dumps(result, ensure_ascii=False, default=str)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run exported artifact {name}")
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--check", action="store_true", help="Verify bundled files without calling the model")
    parser.add_argument("--workflow", default="{workflow}")
    parser.add_argument("--data-root", help="Writable data root outside the artifact (default: external temp dir)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.check:
        from modules.artifacts.manifest import verify_artifact
        report = verify_artifact(BUNDLE, require_service=False)
        payload = {{
            "ok": report["ok"],
            "artifact_id": (report.get("manifest") or {{}}).get("artifact_id"),
            "workflows": (report.get("manifest") or {{}}).get("workflows", []),
            "issues": report["issues"],
            "checksums_only": True,
            "service_ready": report["service_ready"],
        }}
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if report["ok"] else 1
    if not args.prompt:
        parser.error("provide a prompt or use --check")
    _prepare_root(_writable_root(args.data_root))
    from core.scenarios.registry import get_scenario
    from modules.runtime.workflow import WorkflowRuntime

    scenario = get_scenario("{name}")
    runtime = WorkflowRuntime(scenario)
    result = runtime.run(args.workflow, args.prompt)
    answer = answer_of(result)
    if args.json:
        print(json.dumps({{"answer": answer, "trace_id": runtime.last_trace_id, "workflow": args.workflow}}, ensure_ascii=False))
    else:
        print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _is_runtime_source_excluded(relative: Path) -> bool:
    posix = relative.as_posix()
    parts = set(relative.parts)
    if parts & {"__pycache__", "node_modules"}:
        return True
    if relative.suffix.lower() in {".pyc", ".pyo"}:
        return True
    if posix in _EXCLUDED_RUNTIME_FILES:
        return True
    for prefix in _EXCLUDED_RUNTIME_DIRS:
        if posix.startswith(prefix):
            return True
    return False


def _copy_runtime_sources(runtime: Path) -> list[str]:
    """复制运行所需 Python 源码，排除 editor/chat 的 UI 应用代码。

    内核仍会 import 的 editor domain 纯模块（ownership / entry / spec_edit 等）
    必须保留，否则独立运行无法启动。
    """
    excluded: list[str] = []
    for package in ("core", "modules"):
        for source in sorted((PROJECT_ROOT / package).rglob("*.py")):
            relative = source.relative_to(PROJECT_ROOT)
            if relative.parts[:2] == ("modules", "instances"):
                # 只带自包含 runtime 包及其 package __init__；registry / launcher /
                # cli 属于管理面，不进产物。runtime 子包由 _copy_instance_runtime 复制。
                if relative.as_posix() != "modules/instances/__init__.py":
                    continue
            if _is_runtime_source_excluded(relative):
                if "__pycache__" not in source.parts:
                    excluded.append(relative.as_posix())
                continue
            target = runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for filename in ("rag.yml", "rag_prompts.yml", "chroma.yml", "observability.yml"):
        source = PROJECT_ROOT / "config" / filename
        if source.exists():
            target = runtime / "config" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return excluded


def _copy_instance_runtime(runtime: Path) -> list[str]:
    """把 ``modules/instances/runtime`` 作为真正的 Python 包复制。

    产物内路径是 ``runtime/modules/instances/runtime/``，因此 serve/worker 都用
    ``modules.instances.runtime.*`` 导入；不会退化成 flat ``import server``。
    """
    package = PROJECT_ROOT / "modules" / "instances"
    copied: list[str] = []
    init = package / "__init__.py"
    if init.is_file():
        target = runtime / "modules" / "instances" / "__init__.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(init, target)
        copied.append("modules/instances/__init__.py")
    source_dir = package / "runtime"
    if source_dir.is_dir():
        target = runtime / "modules" / "instances" / "runtime"
        shutil.copytree(
            source_dir,
            target,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"),
        )
        copied.append("modules/instances/runtime/")
    return copied


def _copy_product_ui(bundle: Path, source: str | Path | None = None) -> list[str]:
    """复制产品前端；默认取 ``modules/instances/runtime/web``。

    ``source`` 是给未来自定义前端预留的参数；相对路径按仓库根解析。
    """
    default = PROJECT_ROOT / "modules" / "instances" / "runtime" / "web"
    candidate = Path(source).expanduser() if source is not None else default
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    if not candidate.is_dir():
        return []
    target = bundle / "product_ui"
    shutil.copytree(
        candidate,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"),
    )
    return ["product_ui/"]


def _build_requirements() -> list[str]:
    requirements = ["# Runtime dependencies pinned to the exporting environment."]
    for line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if "==" not in line or line.lstrip().startswith("#"):
            continue
        package, declared = line.strip().split("==", 1)
        if package in _EXCLUDED_REQUIREMENTS:
            continue
        try:
            resolved = installed_version(package)
        except PackageNotFoundError:
            resolved = declared
        requirements.append(f"{package}=={resolved}")
    return requirements


def _readme(name: str, version: int, workflow: str, dependencies_markdown: str) -> str:
    return f"""# {name} v{version}

Auto-exported from Agent Harness editor as a standalone instance.

This bundle is a **copy**, not a live editor document. It contains the saved spec,
the runtime Python sources, `run.py`, and a dependency manifest. You can move it out of
the original repository and edit its files directly.

```powershell
python -m pip install -r requirements.txt
python run.py "your question" --workflow {workflow}
python run.py "your question" --json
python run.py --check
```

Python 3.10+ required.

## Required local data and services (not included)

The bundle does not copy the dependencies below. They must be supplied in the target
environment before the scenario can run. Paths are logical `data/...` paths; when
running this bundle they resolve under the writable root selected by `--data-root`,
`AH_DATA_ROOT`, or a fresh external temp directory (never inside the artifact),
e.g. `data/knowledge` -> `<writable-data-root>/knowledge`.

{dependencies_markdown}

`python run.py --check` only verifies the SHA-256 checksums in `manifest.json`.
It does **not** build the scenario, load the knowledge base or embedding model,
validate external data, call the model, or prove that the scenario is runnable.
A passing check is not a build or execution guarantee.

## Service / frontend status

This artifact follows manifest `schema_version: 2` and ships a real service entry:
`serve.py` boots `modules.instances.runtime.bootstrap` / `server` from
`runtime/` and serves `product_ui/`. Instance config and writable storage live
outside the artifact; the artifact tree itself stays read-only at runtime.

`python run.py --check` verifies checksums only. It does **not** prove that model
credentials, local knowledge, embedding models, or external services are ready.
Keep the instance `environment_file` and asset bindings outside the artifact;
`GET /api/health` reports `needs_configuration` rather than pretending to be ready.

Model credentials, local knowledge, embedding models, and external services are not
copied. Configure them in the target environment, for example in
`runtime/config/rag.yml`, `runtime/config/chroma.yml`, and the scenario config under
`runtime/core/scenarios/<pool>/config/`.

## Editing this instance

- The saved spec is in `{name}/` (`active.json` points at the version file).
- Runtime sources are in `runtime/`; edit them if the bundled code must change.
- `run.py --check` verifies the manifest checksums. After a deliberate edit it will
  report the changed files; that is an integrity warning, not an execution blocker.
- The exported spec's `cases_path` points at the bundled `{name}/cases.jsonl`; the
  original repository spec is not modified.

## Converting the repository project instead

The original repository scene may be spec-owned. Do not edit `core/scenarios/{name}/`
until you explicitly run:

```powershell
python -m core.scenarios.eject {name} --reason "why this becomes code-owned"
```

That is a separate, one-way repository action. Exporting this bundle does not eject or
modify the source scene.
"""


def build_artifact(
    name: str,
    *,
    root: str | Path = EXPORT_ROOT,
    product_ui_source: str | Path | None = None,
) -> dict[str, Any]:
    """构建一个 schema v2 artifact，返回 ``{bundle, zip, manifest}``（兼容旧返回）。"""
    try:
        assert_spec_owned(name, root=SPEC_ROOT)
    except OwnershipError as exc:
        raise ValueError("不能从已失效的 spec 导出代码归属场景：" + str(exc)) from exc
    version = active_version(name)
    if version is None:
        raise FileNotFoundError(f"{name} 没有 active.json")
    active_path = SPEC_ROOT / name / "active.json"
    pointer = json.loads(active_path.read_text(encoding="utf-8"))
    version_file = str(pointer.get("file") or f"v{version}.json")
    spec = load_spec(SPEC_ROOT / name / version_file)
    workflows = list(spec.workflows)
    if not workflows:
        raise ValueError(f"{name} 没有 workflow")

    output_root = Path(root)
    output_root.mkdir(parents=True, exist_ok=True)
    bundle = _create_bundle_dir(output_root, name, version)
    artifact_id = bundle.name
    destination = bundle / name
    destination.mkdir(parents=True, exist_ok=True)

    files: list[str] = []
    _copy_if_exists(
        active_path, destination / "active.json", files, f"{name}/active.json"
    )
    _copy_if_exists(
        SPEC_ROOT / name / version_file,
        destination / version_file,
        files,
        f"{name}/{version_file}",
    )
    for filename in ("prompt.yml", "entry.yml", "gate.yml", ".pool"):
        _copy_if_exists(
            SPEC_ROOT / name / filename,
            destination / filename,
            files,
            f"{name}/{filename}",
        )

    # The artifact is read-only at runtime.  Scenario construction calls
    # ``ensure_spec_editable(..., track_code=False)`` which writes/rewrites
    # ``state.json`` when it is missing or still carries a code hash.  Ship a
    # settled spec-owned state so runtime never needs to write into the bundle.
    source_state = _read_yaml(SPEC_ROOT / name / "state.json")
    exported_state = {
        "name": name,
        "owner": "spec",
        "code_hash": None,
    }
    for key in ("spec_file", "ejected_at", "reason"):
        if source_state.get(key) is not None:
            exported_state[key] = source_state[key]
    (destination / "state.json").write_text(
        json.dumps(exported_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    files.append(f"{name}/state.json")

    # 行为用例按 resolve_cases_path 定位，复制后进行可移植的 cases_path 改写。
    # 只改包内副本，源 spec 字节保持不变。
    cases_relative = _copy_cases(spec, destination, bundle, files)
    if cases_relative is not None:
        exported_spec_path = destination / version_file
        exported_spec = json.loads(exported_spec_path.read_text(encoding="utf-8"))
        exported_spec["cases_path"] = cases_relative
        exported_spec_path.write_text(
            json.dumps(exported_spec, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    runtime = bundle / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    excluded_sources = _copy_runtime_sources(runtime)
    included_instances = _copy_instance_runtime(runtime)
    product_ui = _copy_product_ui(bundle, product_ui_source)

    # 工具池实现会在包内 runtime 下读取自己的场景 config；只复制文本配置。
    pool = _copy_scenario_config(name, bundle, runtime, files)

    requirements = _build_requirements()
    (bundle / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")

    dependencies = _dependencies_not_included(spec, pool)
    required_assets = _required_assets_for(spec, pool)
    dependencies_markdown = _dependencies_markdown(dependencies)
    (bundle / "run.py").write_text(_runner_source(name, workflows[0]), encoding="utf-8")
    (bundle / "README.md").write_text(
        _readme(name, version, workflows[0], dependencies_markdown), encoding="utf-8"
    )
    # 所有生成文件必须在 checksums / runtime hash 之前落地；serve.py 不是空壳，
    # 由 write_service_entry 真正生成。
    write_service_entry(bundle)

    checksums = {}
    for artifact_file in bundle.rglob("*"):
        if not artifact_file.is_file() or artifact_file.name == "manifest.json":
            continue
        relative = artifact_file.relative_to(bundle).as_posix()
        checksums[relative] = hashlib.sha256(artifact_file.read_bytes()).hexdigest()
    content_hash = compute_content_hash(checksums)
    runtime_hash = hash_tree(runtime)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "name": name,
        "version": version,
        "workflows": workflows,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entry": {
            "cli": "run.py",
            "server": "serve.py",
            "default_workflow": workflows[0],
        },
        "frontend": {"root": "product_ui", "api_version": "v1"},
        "runtime": {
            "included": True,
            "python": ">=3.10",
            "root": "runtime",
            "requirements": "requirements.txt",
            "hash": runtime_hash,
            "excluded_app_code": sorted(set(excluded_sources)),
            "included_instances_runtime": included_instances,
        },
        "source": {"scenario": name, "version": version},
        "dependencies_not_included": dependencies,
        "required_assets": required_assets,
        "files": files,
        "checksums": checksums,
        "content_hash": content_hash,
        "check_scope": "checksums_only",
        # 兼容旧消费者的字段；新代码读 entry/runtime/frontend。
        "runtime_included": True,
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    zip_path = output_root / f"{artifact_id}.zip"
    if zip_path.exists():
        raise FileExistsError(f"artifact zip already exists: {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in bundle.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(bundle))
    return {"bundle": str(bundle), "zip": str(zip_path), "manifest": manifest}


def _create_bundle_dir(output_root: Path, name: str, version: int) -> Path:
    """固定 artifact id，绝不覆盖已有产物。"""
    for _ in range(20):
        artifact_id = f"{name}-v{version}-{uuid.uuid4().hex[:10]}"
        bundle = output_root / artifact_id
        if bundle.exists() or (output_root / f"{artifact_id}.zip").exists():
            continue
        try:
            bundle.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return bundle
    raise FileExistsError("could not allocate a unique artifact id")
