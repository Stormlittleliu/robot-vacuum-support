# -*- coding: utf-8 -*-
"""外部源工程：从完整 v2 artifact fork，再用工程自身内容 rebuild 新 artifact。

边界：

- fork 只读取并校验完整 v2 artifact，复制可编辑源码（spec / runtime /
  requirements / product_ui），不复制旧 manifest、实例状态、运行数据、密钥或缓存；
- build-project 只使用源工程自己的 spec / runtime / 前端，不回源仓库按场景名取货；
- 输出目录、工程引用目录都做真实路径边界检查，拒绝 symlink 逃逸和输出递归进工程；
- 不删除任何已存在目录，artifact id 由 UUID 分配，旧产物与旧实例不受影响；
- 静态校验 ``verify_artifact`` 只证明字节完整，不等于运行就绪；运行就绪仍需实例验证。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from modules.artifacts.manifest import (
    SCHEMA_VERSION,
    compute_content_hash,
    hash_tree,
    safe_relative_path,
    verify_artifact,
)
from modules.artifacts.service_entry import write_service_entry

PROJECT_SCHEMA_VERSION = 1
PROJECT_FILE = "project.json"

_CACHE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".git",
    ".hg",
    ".svn",
    ".idea",
}
_VENV_DIR_NAMES = {"venv", ".venv", "env", ".env"}
#: 运行状态目录：无论出现在根、runtime 还是声明的 asset 里都不复制。
_RUNTIME_STATE_DIR_NAMES = {
    "outputs",
    "logs",
    "sessions",
    "checkpoints",
    "workspace",
}
_ARTIFACT_ROOT_STATE_DIRS = _RUNTIME_STATE_DIR_NAMES | {"data", "instances"}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_SECRET_NAME_PARTS = ("secret", "credential")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SCENARIO_RE = re.compile(r"^[^/\\:]+$")
_ASSET_CATEGORIES = {
    "knowledge": "knowledge",
    "model": "models",
    "models": "models",
    "external": "external",
    "assets": "external",
    "rag_index": "rag_index",
    "rag-index": "rag_index",
}


class ProjectError(ValueError):
    """源工程或输出边界不满足契约。"""


# ---------------------------------------------------------------------------
# 路径与排除规则
# ---------------------------------------------------------------------------
def _as_posix(value: Any) -> str:
    return str(value).replace("\\", "/")


def _safe_relative(value: Any, field: str, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str):
        raise ProjectError(f"{field}: path must be a string")
    normalized = _as_posix(value).strip()
    if allow_dot and normalized == ".":
        return "."
    safe, detail = safe_relative_path(normalized)
    if not safe:
        raise ProjectError(f"{field}: {detail}")
    return detail


def _assert_inside(root: Path, target: Path, field: str) -> Path:
    try:
        resolved_root = root.resolve()
        resolved_target = target.resolve()
        resolved_target.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ProjectError(f"{field} escapes {root}: {target}") from exc
    return resolved_target


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    if lower == ".env" or lower.startswith(".env"):
        return True
    if lower in {"id_rsa", "id_dsa", "id_ed25519"}:
        return True
    if any(part in lower for part in _SECRET_NAME_PARTS):
        return True
    return Path(lower).suffix in _SECRET_SUFFIXES


def _is_excluded(relative: Path, *, is_dir: bool, scope: str) -> bool:
    """按源内相对路径判断是否排除；scope = artifact/spec/runtime/frontend/assets。"""
    parts = relative.parts
    if not parts:
        return False
    if any(part in _CACHE_DIR_NAMES for part in parts):
        return True
    if is_dir and any(part in _VENV_DIR_NAMES for part in parts):
        return True
    if is_dir and any(part in _RUNTIME_STATE_DIR_NAMES for part in parts):
        return True
    if scope == "artifact":
        if parts[0] in _ARTIFACT_ROOT_STATE_DIRS:
            return True
        if relative.as_posix() in {"manifest.json", "serve.py", "run.py", "README.md"}:
            return True
    if scope == "runtime" and parts[0] in _ARTIFACT_ROOT_STATE_DIRS:
        return True
    if scope == "assets" and len(parts) > 1 and parts[0] == "data" and parts[1] in {
        "runtime",
        "specs",
    }:
        return True
    if is_dir:
        return False
    if _is_secret_name(relative.name):
        return True
    if relative.suffix.lower() in {".pyc", ".pyo"}:
        return True
    if relative.name == "instance.json":
        return True
    return False


def _read_json(path: Path, field: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectError(f"cannot read {field}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError(f"{field} must contain a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


# ---------------------------------------------------------------------------
# 复制（所有路径先做真实路径边界检查）
# ---------------------------------------------------------------------------
def _copy_file(source: Path, destination: Path, *, base: Path, field: str) -> None:
    if source.is_symlink():
        raise ProjectError(f"{field}: symlink is not allowed: {source}")
    _assert_inside(base, source, field)
    if not source.is_file():
        raise ProjectError(f"{field}: file is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_tree(
    source_root: Path,
    destination_root: Path,
    *,
    base: Path,
    scope: str,
) -> int:
    """复制目录内容，跳过缓存/运行状态/密钥；任何 symlink 都拒绝。"""
    if source_root.is_symlink():
        raise ProjectError(f"{scope}: symlink root is not allowed: {source_root}")
    _assert_inside(base, source_root, scope)
    if not source_root.is_dir():
        raise ProjectError(f"{scope}: directory is missing: {source_root}")

    copied = 0
    for current, dirnames, filenames in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for dirname in dirnames:
            candidate = current_path / dirname
            relative = candidate.relative_to(source_root)
            if _is_excluded(relative, is_dir=True, scope=scope):
                continue
            if candidate.is_symlink():
                raise ProjectError(f"{scope}: symlink is not allowed: {candidate}")
            _assert_inside(base, candidate, scope)
            kept.append(dirname)
        dirnames[:] = kept

        target_dir = destination_root / current_path.relative_to(source_root)
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            candidate = current_path / filename
            relative = candidate.relative_to(source_root)
            if _is_excluded(relative, is_dir=False, scope=scope):
                continue
            if candidate.is_symlink():
                raise ProjectError(f"{scope}: symlink is not allowed: {candidate}")
            _assert_inside(base, candidate, scope)
            shutil.copy2(candidate, target_dir / filename)
            copied += 1
    return copied


# ---------------------------------------------------------------------------
# project.json
# ---------------------------------------------------------------------------
def _scenario_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectError(f"{field} is missing")
    name = value.strip()
    if name in {".", ".."} or not _SCENARIO_RE.fullmatch(name):
        raise ProjectError(f"{field} is not a safe scenario name: {value!r}")
    return name


def _source_path(
    project_root: Path,
    value: Any,
    field: str,
    *,
    allow_dot: bool = False,
) -> tuple[Path, str]:
    relative = _safe_relative(value, field, allow_dot=allow_dot)
    target = project_root if relative == "." else project_root / relative
    _assert_inside(project_root, target, field)
    return target, relative


def _asset_entries(value: Any) -> list[tuple[str, str]]:
    """把 asset_source 归一化为 [(category, relative_path), ...]。"""
    entries: list[tuple[str, str]] = []

    def add(category: Any, item: Any) -> None:
        key = str(category or "external").strip().lower()
        category_name = _ASSET_CATEGORIES.get(key, key)
        if isinstance(item, dict):
            if item.get("category"):
                nested_key = str(item["category"]).strip().lower()
                category_name = _ASSET_CATEGORIES.get(nested_key, nested_key)
            item = item.get("path")
        if isinstance(item, (list, tuple)):
            for nested in item:
                add(category_name, nested)
            return
        if not isinstance(item, str) or not item.strip():
            raise ProjectError("asset_source entries must be non-empty paths")
        entries.append((category_name, item.strip()))

    if value is None:
        return entries
    if isinstance(value, str):
        add("external", value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            add("external", item)
    elif isinstance(value, dict):
        for category, item in value.items():
            add(category, item)
    else:
        raise ProjectError("asset_source must be a path, list, or object")
    return entries


def _binding_entries(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProjectError("asset_bindings must be an object")
    result: dict[str, list[str]] = {}
    for key, item in value.items():
        category = _ASSET_CATEGORIES.get(str(key).strip().lower(), str(key).strip())
        values = item if isinstance(item, (list, tuple)) else [item]
        cleaned: list[str] = []
        for entry in values:
            if not isinstance(entry, str) or not entry.strip():
                raise ProjectError(f"asset_bindings.{key} must contain non-empty strings")
            cleaned.append(entry.strip())
        result[category] = cleaned
    return result


def _load_project(project_root: Path) -> dict[str, Any]:
    path = project_root / PROJECT_FILE
    if not path.is_file():
        raise ProjectError(f"{PROJECT_FILE} not found under {project_root}")
    data = _read_json(path, PROJECT_FILE)
    schema = data.get("schema", data.get("schema_version"))
    if schema != PROJECT_SCHEMA_VERSION:
        raise ProjectError(
            f"{PROJECT_FILE} schema must be {PROJECT_SCHEMA_VERSION}, got {schema!r}"
        )
    scenario = _scenario_name(data.get("scenario"), "project.scenario")
    spec_root, spec_relative = _source_path(
        project_root, data.get("spec_root", "."), "project.spec_root", allow_dot=True
    )
    runtime, runtime_relative = _source_path(
        project_root, data.get("runtime_source", "runtime"), "project.runtime_source"
    )
    frontend, frontend_relative = _source_path(
        project_root,
        data.get("frontend_source", "product_ui"),
        "project.frontend_source",
    )
    if runtime_relative == "." or frontend_relative == ".":
        raise ProjectError("runtime_source / frontend_source must be project subdirectories")

    assets: list[tuple[str, str, Path]] = []
    seen_assets: set[str] = set()
    for category, raw in _asset_entries(data.get("asset_source")):
        relative = _safe_relative(raw, "asset_source")
        target = project_root / relative
        _assert_inside(project_root, target, "asset_source")
        if target.resolve() == project_root.resolve():
            raise ProjectError("asset_source must not be the project root")
        first = Path(relative).parts[0]
        if first in {
            "runtime",
            "product_ui",
            scenario,
            PROJECT_FILE,
            "manifest.json",
            "serve.py",
            "run.py",
            "requirements.txt",
            "README.md",
        }:
            raise ProjectError(
                f"asset_source must not overlap package roots: {relative!r}"
            )
        for source_root, source_name in (
            (runtime, "runtime_source"),
            (frontend, "frontend_source"),
        ):
            try:
                target.resolve().relative_to(source_root.resolve())
            except ValueError:
                continue
            raise ProjectError(
                f"asset_source {relative!r} must not be inside {source_name}"
            )
        if spec_relative != ".":
            try:
                target.resolve().relative_to(spec_root.resolve())
            except ValueError:
                pass
            else:
                raise ProjectError(
                    f"asset_source {relative!r} must not be inside spec_root"
                )
        if relative not in seen_assets:
            seen_assets.add(relative)
            assets.append((category, relative, target))

    return {
        "data": data,
        "name": str(data.get("name") or scenario),
        "scenario": scenario,
        "spec_root": spec_root,
        "spec_root_relative": spec_relative,
        "runtime_source": runtime,
        "runtime_source_relative": runtime_relative,
        "frontend_source": frontend,
        "frontend_source_relative": frontend_relative,
        "assets": assets,
        "asset_bindings": _binding_entries(data.get("asset_bindings")),
        "dependencies": data.get("dependencies_not_included")
        if isinstance(data.get("dependencies_not_included"), dict)
        else {},
        "needs_configuration": data.get("needs_configuration")
        if isinstance(data.get("needs_configuration"), list)
        else [],
        "required_assets": data.get("required_assets")
        if isinstance(data.get("required_assets"), list)
        else [],
        "include_assets": bool(data.get("include_assets")),
    }


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------
def fork_artifact(
    artifact_root: str | Path, project_dir: str | Path
) -> dict[str, Any]:
    """把完整 v2 artifact fork 成可编辑源工程（不覆盖已存在目录）。"""
    artifact = Path(artifact_root).expanduser()
    if not artifact.is_dir():
        raise ProjectError(f"artifact root is not a directory: {artifact}")
    artifact_resolved = artifact.resolve()

    destination = Path(project_dir).expanduser()
    if destination.exists():
        raise FileExistsError(f"project directory already exists: {destination}")
    destination_resolved = destination.resolve()
    try:
        destination_resolved.relative_to(artifact_resolved)
    except ValueError:
        pass
    else:
        raise ProjectError("project directory must be outside the source artifact")

    report = verify_artifact(artifact, require_service=True)
    manifest = report.get("manifest")
    if not isinstance(manifest, dict):
        raise ProjectError(
            "cannot fork: artifact manifest is missing or invalid: "
            + "; ".join(report.get("issues") or ["unknown manifest error"])
        )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ProjectError("fork requires a complete schema_version 2 artifact")
    if not report.get("ok") or not report.get("service_ready"):
        raise ProjectError(
            "cannot fork an incomplete/unverified v2 artifact: "
            + "; ".join(report.get("issues") or ["not service-ready"])
        )

    name = _scenario_name(manifest.get("name"), "manifest.name")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    scenario = name
    source_scenario = source.get("scenario") if isinstance(source.get("scenario"), str) else name
    entry = manifest.get("entry") if isinstance(manifest.get("entry"), dict) else {}
    workflows = manifest.get("workflows") if isinstance(manifest.get("workflows"), list) else []
    workflows = [str(item) for item in workflows if str(item).strip()]
    default_workflow = str(entry.get("default_workflow") or (workflows[0] if workflows else ""))
    if not default_workflow:
        raise ProjectError("cannot fork: manifest has no default workflow")
    if default_workflow not in workflows:
        raise ProjectError(
            f"cannot fork: default workflow {default_workflow!r} is not in manifest.workflows"
        )

    frontend = manifest.get("frontend") if isinstance(manifest.get("frontend"), dict) else {}
    frontend_relative = frontend.get("root", "product_ui")
    frontend_root, _ = _source_path(
        artifact, frontend_relative, "manifest.frontend.root"
    )
    spec_source, _ = _source_path(artifact, name, "manifest.name")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)

    _copy_tree(spec_source, destination / name, base=artifact, scope="spec")
    _copy_tree(
        artifact / "runtime", destination / "runtime", base=artifact, scope="runtime"
    )
    requirements = artifact / "requirements.txt"
    if requirements.is_file():
        _copy_file(
            requirements,
            destination / "requirements.txt",
            base=artifact,
            field="requirements.txt",
        )
    _copy_tree(
        frontend_root, destination / "product_ui", base=artifact, scope="frontend"
    )

    # Packed manifest.assets stay usable after fork: preserve their relative
    # path and declare them as asset_source so rebuild can include them again.
    manifest_assets = manifest.get("assets") if isinstance(manifest.get("assets"), list) else []
    asset_source: dict[str, list[str]] = {}
    for item in manifest_assets:
        if not isinstance(item, dict):
            continue
        category = _ASSET_CATEGORIES.get(str(item.get("category") or "").strip().lower())
        raw_path = item.get("path") or item.get("source")
        if not category or not isinstance(raw_path, str) or not raw_path.strip():
            continue
        try:
            relative = _safe_relative(raw_path, "manifest.assets.path")
        except ProjectError:
            continue
        source = artifact / relative
        if not source.exists():
            continue
        try:
            _assert_inside(artifact, source, "manifest.assets")
        except ProjectError:
            continue
        target = destination / relative
        if source.is_file():
            _copy_file(source, target, base=artifact, field=f"manifest.assets:{relative}")
        else:
            _copy_tree(source, target, base=artifact, scope="assets")
        asset_source.setdefault(category, []).append(relative)

    dependencies = (
        manifest.get("dependencies_not_included")
        if isinstance(manifest.get("dependencies_not_included"), dict)
        else {"knowledge": [], "model": [], "external": []}
    )
    needs = manifest.get("needs_configuration")
    if not isinstance(needs, list):
        needs = []
    for values in dependencies.values():
        if isinstance(values, list):
            needs.extend(str(item) for item in values)

    project = {
        "schema": PROJECT_SCHEMA_VERSION,
        "schema_version": PROJECT_SCHEMA_VERSION,
        "name": name,
        "scenario": scenario,
        "project_name": destination.name,
        "spec_root": ".",
        "runtime_source": "runtime",
        "frontend_source": "product_ui",
        "default_workflow": default_workflow,
        "source_artifact_id": str(manifest.get("artifact_id") or artifact.name),
        "source_content_hash": manifest.get("content_hash"),
        "source_schema_version": manifest.get("schema_version"),
        "source_scenario": source_scenario,
        "source_version": manifest.get("version"),
        "dependencies_not_included": {
            str(key): [str(item) for item in (values or [])]
            for key, values in dependencies.items()
        },
        "needs_configuration": _dedupe(needs),
        "required_assets": manifest.get("required_assets")
        if isinstance(manifest.get("required_assets"), list)
        else [],
        "asset_bindings": (
            manifest.get("asset_bindings")
            if isinstance(manifest.get("asset_bindings"), dict)
            else {}
        ),
        "asset_source": asset_source,
        "include_assets": bool(asset_source),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_json(destination / PROJECT_FILE, project)
    return {
        "project": str(destination),
        "project_json": str(destination / PROJECT_FILE),
        "source_artifact_id": project["source_artifact_id"],
        "source_content_hash": project["source_content_hash"],
        "scenario": scenario,
        "default_workflow": default_workflow,
    }


# ---------------------------------------------------------------------------
# build-project
# ---------------------------------------------------------------------------
def _active_version(spec_dir: Path) -> tuple[int, str, Path, dict[str, Any]]:
    pointer_path = spec_dir / "active.json"
    if not pointer_path.is_file():
        raise ProjectError(f"active spec pointer missing: {pointer_path}")
    pointer = _read_json(pointer_path, "spec active.json")
    raw_file = pointer.get("file")
    if not isinstance(raw_file, str) or not raw_file.strip():
        raise ProjectError("spec active.json has no file pointer")
    version_file = _safe_relative(raw_file, "spec active.json file")
    version_path = spec_dir / version_file
    _assert_inside(spec_dir, version_path, "spec active.json file")
    if not version_path.is_file():
        raise ProjectError(f"active spec version is missing: {version_path}")

    payload = _read_json(version_path, f"spec {version_file}")
    version = pointer.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        match = re.fullmatch(r"v(\d+)\.json", version_file)
        if match:
            version = int(match.group(1))
        else:
            payload_version = payload.get("version")
            if isinstance(payload_version, int) and not isinstance(payload_version, bool):
                version = payload_version
            else:
                raise ProjectError(
                    "spec active.json.version is missing and cannot be inferred"
                )
    if version < 1:
        raise ProjectError("spec version must be >= 1")
    return version, version_file, version_path, payload


def _workflows_of(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("workflows")
    if isinstance(raw, list):
        values = [str(item) for item in raw if str(item).strip()]
    elif isinstance(raw, dict):
        values = [str(item) for item in raw if str(item).strip()]
    else:
        values = []
    if not values:
        raise ProjectError("active spec has no workflows")
    return values


def _find_cases(
    payload: dict[str, Any], spec_dir: Path, project_root: Path
) -> Path | None:
    cases_path = payload.get("cases_path")
    if isinstance(cases_path, str) and cases_path.strip():
        candidate = Path(cases_path)
        if candidate.is_absolute():
            _assert_inside(project_root, candidate, "spec cases_path")
            if candidate.is_file():
                return candidate
        else:
            # artifact 内 cases_path 是相对 bundle/runtime 的；源工程里同时尝试
            # 工程根、runtime 根和 spec 目录，只有落在工程内才接受。
            for base in (project_root, project_root / "runtime", spec_dir):
                candidate_path = base / candidate
                try:
                    candidate_path.resolve().relative_to(project_root.resolve())
                except ValueError:
                    continue
                if candidate_path.is_file():
                    return candidate_path
    fallback = spec_dir / "cases.jsonl"
    return fallback if fallback.is_file() else None


def _copy_spec(
    *,
    project_root: Path,
    spec_dir: Path,
    scenario: str,
    version_file: str,
    version_path: Path,
    payload: dict[str, Any],
    bundle: Path,
    files: list[str],
) -> None:
    destination = bundle / scenario
    destination.mkdir(parents=True, exist_ok=True)
    _copy_file(
        spec_dir / "active.json",
        destination / "active.json",
        base=spec_dir,
        field=f"{scenario}/active.json",
    )
    _copy_file(
        version_path,
        destination / version_file,
        base=spec_dir,
        field=f"{scenario}/{version_file}",
    )
    files.extend([f"{scenario}/active.json", f"{scenario}/{version_file}"])
    for filename in ("prompt.yml", "entry.yml", "gate.yml", ".pool"):
        source = spec_dir / filename
        if not source.is_file():
            continue
        _copy_file(
            source,
            destination / filename,
            base=spec_dir,
            field=f"{scenario}/{filename}",
        )
        files.append(f"{scenario}/{filename}")

    # artifact 在运行时是只读的；写入 settled spec-owned state，避免运行时
    # 因 missing/code-hash 状态而尝试回写产物。布局与 builder 保持一致。
    source_state: dict[str, Any] = {}
    state_path = spec_dir / "state.json"
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                source_state = loaded
        except (OSError, ValueError):
            source_state = {}
    exported_state = {"name": scenario, "owner": "spec", "code_hash": None}
    for key in ("spec_file", "ejected_at", "reason"):
        if source_state.get(key) is not None:
            exported_state[key] = source_state[key]
    _write_json(destination / "state.json", exported_state)
    files.append(f"{scenario}/state.json")

    cases = _find_cases(payload, spec_dir, project_root)
    if cases is None:
        return
    target = destination / "cases.jsonl"
    _copy_file(cases, target, base=project_root, field=f"{scenario}/cases.jsonl")
    files.append(f"{scenario}/cases.jsonl")
    cases_relative = Path(os.path.relpath(target, start=bundle / "runtime")).as_posix()
    exported = json.loads((destination / version_file).read_text(encoding="utf-8"))
    exported["cases_path"] = cases_relative
    _write_json(destination / version_file, exported)


def _create_bundle_dir(output_root: Path, scenario: str, version: int) -> Path:
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", scenario).strip("-") or "artifact"
    for _ in range(20):
        artifact_id = f"{prefix}-v{version}-{uuid.uuid4().hex[:10]}"
        bundle = output_root / artifact_id
        if bundle.exists() or (output_root / f"{artifact_id}.zip").exists():
            continue
        try:
            bundle.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return bundle
    raise FileExistsError("could not allocate a unique artifact id")


def _copy_requirements(project_root: Path, bundle: Path) -> None:
    source = project_root / "requirements.txt"
    if source.is_file():
        _copy_file(
            source, bundle / "requirements.txt", base=project_root, field="requirements.txt"
        )
    else:
        (bundle / "requirements.txt").write_text(
            "# No requirements.txt was declared by the source project.\n",
            encoding="utf-8",
        )


def _runner_source(scenario: str, workflow: str) -> str:
    # 复用现 builder 的生成模板，保证包布局/CLI 行为一致；不在本模块改 builder。
    from modules.artifacts.builder import _runner_source as builder_runner_source

    return builder_runner_source(scenario, workflow)


def _readme(
    scenario: str,
    version: int,
    workflow: str,
    dependencies_markdown: str,
) -> str:
    return f"""# {scenario} v{version}

Built from an editable **source project**; this directory is a build artifact.

- Scene/spec version: `v{version}`
- Default workflow: `{workflow}`
- Artifact build id: see `manifest.json` (`artifact_id`)

```powershell
python run.py "your question" --workflow {workflow}
python run.py "your question" --json
python run.py --check
```

`python run.py --check` verifies the SHA-256 checksums and the v2 manifest only.
It does **not** prove the runtime is ready: instance validation
(`serve.py --instance-config <instances/id/instance.json>`) is still required.

## Dependencies not included

{dependencies_markdown}

Credentials, sessions, checkpoints, logs, writable data roots and virtualenvs are
never copied from the source project or the artifact it was forked from.
"""


def _dependencies_markdown(dependencies: dict[str, list[str]], needs: list[str]) -> str:
    lines: list[str] = []
    titles = (
        ("knowledge", "Knowledge"),
        ("model", "Model"),
        ("external", "External services and data"),
    )
    for key, title in titles:
        items = dependencies.get(key) or []
        if not items:
            continue
        lines.append(f"### {title}")
        lines.extend(f"- `{item}`" for item in items)
        lines.append("")
    if needs:
        lines.append("### needs_configuration")
        lines.extend(f"- `{item}`" for item in needs)
    return "\n".join(lines).rstrip() if lines else "No extra dependencies declared."


def _merge_dependencies(
    data: dict[str, Any],
    *,
    assets: list[tuple[str, str, Path]],
    include_assets: bool,
    bindings: dict[str, list[str]],
) -> tuple[dict[str, list[str]], list[str]]:
    raw = data.get("dependencies_not_included")
    dependencies: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for key, values in raw.items():
            if isinstance(values, list):
                dependencies[str(key)] = [str(item) for item in values]
            elif values:
                dependencies[str(key)] = [str(values)]
    for key in ("knowledge", "model", "external"):
        dependencies.setdefault(key, [])

    needs = [str(item) for item in data.get("needs_configuration") or []]
    # 源工程继承的依赖即使没有单独的 needs_configuration 字段，也要保留缺口清单。
    for values in dependencies.values():
        needs.extend(values)
    for category, relative, target in assets:
        if not target.exists():
            needs.append(f"asset_source:{category}:{relative} (declared but missing)")
            dependencies.setdefault(category, []).append(
                f"{relative}: declared asset_source (missing)"
            )
        elif include_assets:
            # 已显式打包的只读资源不是配置缺口；只记录在 manifest.assets。
            continue
        else:
            message = (
                f"asset_source:{category}:{relative} "
                "(not included; rebuild with --include-assets)"
            )
            needs.append(message)
            dependencies.setdefault(category, []).append(
                f"{relative}: declared asset_source (not included)"
            )
    for category, values in bindings.items():
        for value in values:
            needs.append(f"asset_binding:{category}:{value} (external, not packaged)")
            dependencies.setdefault("external", []).append(
                f"{value}: explicit asset binding (not packaged)"
            )
    return (
        {key: _dedupe(values) for key, values in dependencies.items()},
        _dedupe(needs),
    )


def _copy_assets(
    *,
    project_root: Path,
    bundle: Path,
    assets: list[tuple[str, str, Path]],
    include_assets: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not include_assets:
        return records
    for category, relative, source in assets:
        if not source.exists():
            continue
        destination = bundle / relative
        # Declared assets preserve their project-relative path under the artifact.
        if source.is_file():
            _copy_file(source, destination, base=project_root, field=f"asset {relative}")
            count = 1
        else:
            count = _copy_tree(
                source, destination, base=project_root, scope="assets"
            )
        records.append(
            {
                "category": category,
                "source": relative,
                "path": destination.relative_to(bundle).as_posix(),
                "files": count,
            }
        )
    return records


def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return {}
    return data if isinstance(data, dict) else {}


def _required_assets_for_project(
    project: dict[str, Any], scenario: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Preserve a source project's declaration, else apply the known RAG rules."""
    declared = project.get("required_assets") or []
    if declared:
        return list(declared)
    from modules.instances.runtime.assets import required_assets_manifest

    runtime = project["runtime_source"]
    rag = _read_config(runtime / "config" / "rag.yml") or _read_config(
        project["spec_root"] / "config" / "rag.yml"
    )
    chroma = _read_config(runtime / "config" / "chroma.yml")
    pool = scenario
    pool_file = project["spec_root"] / scenario / ".pool"
    try:
        if pool_file.is_file():
            pool = pool_file.read_text(encoding="utf-8").strip() or scenario
    except OSError:
        pool = scenario
    agent = _read_config(runtime / "core" / "scenarios" / pool / "config" / "agent.yml")
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    return required_assets_manifest(tools=tools, rag=rag, chroma=chroma, agent=agent)


def build_project(
    project_dir: str | Path,
    out: str | Path,
    *,
    include_assets: bool | None = None,
) -> dict[str, Any]:
    """用源工程自身内容构建新的 v2 artifact；绝不覆盖旧产物。

    ``include_assets=None`` follows the source project's declaration (legacy
    behaviour).  Passing ``False`` explicitly lets a container build the
    runtime without copying mounted knowledge/model/index trees; passing
    ``True`` keeps the existing ``--include-assets`` CLI path.
    """
    project_root = Path(project_dir).expanduser()
    if not project_root.is_dir():
        raise ProjectError(f"project directory is not a directory: {project_root}")
    project_root = project_root.resolve()
    project = _load_project(project_root)
    if include_assets is None:
        include_assets = bool(project.get("include_assets"))
    else:
        include_assets = bool(include_assets)

    output_root = Path(out).expanduser()
    output_resolved = output_root.resolve()
    try:
        output_resolved.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise ProjectError("output directory must not be inside the source project")
    if output_resolved == project_root:
        raise ProjectError("output directory must not be the source project")
    output_root.mkdir(parents=True, exist_ok=True)
    output_resolved = output_root.resolve()
    try:
        output_resolved.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise ProjectError("output directory must not be inside the source project")

    scenario = project["scenario"]
    spec_dir = project["spec_root"] / scenario
    if not spec_dir.is_dir():
        raise ProjectError(f"scenario spec directory is missing: {spec_dir}")
    _assert_inside(project["spec_root"], spec_dir, "spec scenario directory")
    version, version_file, version_path, payload = _active_version(spec_dir)
    workflows = _workflows_of(payload)
    default_workflow = project["data"].get("default_workflow") or workflows[0]
    if not isinstance(default_workflow, str) or default_workflow not in workflows:
        raise ProjectError(
            f"project.default_workflow {default_workflow!r} is not in spec workflows"
        )

    runtime_source = project["runtime_source"]
    frontend_source = project["frontend_source"]
    if not runtime_source.is_dir():
        raise ProjectError(f"runtime_source is not a directory: {runtime_source}")
    if not frontend_source.is_dir():
        raise ProjectError(f"frontend_source is not a directory: {frontend_source}")

    bundle = _create_bundle_dir(output_resolved, scenario, version)
    try:
        runtime = bundle / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        _copy_tree(runtime_source, runtime, base=project_root, scope="runtime")

        files: list[str] = []
        _copy_spec(
            project_root=project_root,
            spec_dir=spec_dir,
            scenario=scenario,
            version_file=version_file,
            version_path=version_path,
            payload=payload,
            bundle=bundle,
            files=files,
        )
        _copy_tree(
            frontend_source,
            bundle / "product_ui",
            base=project_root,
            scope="frontend",
        )
        _copy_requirements(project_root, bundle)
        files.extend(["run.py", "requirements.txt", "serve.py"])

        (bundle / "run.py").write_text(
            _runner_source(scenario, default_workflow), encoding="utf-8"
        )
        write_service_entry(bundle)

        dependencies, needs = _merge_dependencies(
            {
                "dependencies_not_included": project["dependencies"],
                "needs_configuration": project["needs_configuration"],
            },
            assets=project["assets"],
            include_assets=include_assets,
            bindings=project["asset_bindings"],
        )

        # A source project may carry its own README (deployment notes, asset
        # locations, rebuild steps).  Keep that text in the rebuilt artifact;
        # only fall back to the generated English template when absent.
        project_readme = project_root / "README.md"
        if project_readme.is_file():
            _copy_file(
                project_readme,
                bundle / "README.md",
                base=project_root,
                field="README.md",
            )
        else:
            (bundle / "README.md").write_text(
                _readme(
                    scenario,
                    version,
                    default_workflow,
                    _dependencies_markdown(dependencies, needs),
                ),
                encoding="utf-8",
            )

        assets = _copy_assets(
            project_root=project_root,
            bundle=bundle,
            assets=project["assets"],
            include_assets=include_assets,
        )
        required_assets = _required_assets_for_project(project, scenario, payload)

        checksums: dict[str, str] = {}
        for artifact_file in bundle.rglob("*"):
            if not artifact_file.is_file() or artifact_file.name == "manifest.json":
                continue
            relative = artifact_file.relative_to(bundle).as_posix()
            checksums[relative] = hashlib.sha256(artifact_file.read_bytes()).hexdigest()
        content_hash = compute_content_hash(checksums)
        runtime_hash = hash_tree(runtime)
        included_instances = (
            ["modules/instances/runtime/"]
            if (runtime / "modules" / "instances" / "runtime").is_dir()
            else []
        )

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "artifact_id": bundle.name,
            "name": scenario,
            "version": version,
            "workflows": workflows,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entry": {
                "cli": "run.py",
                "server": "serve.py",
                "default_workflow": default_workflow,
            },
            "frontend": {"root": "product_ui", "api_version": "v1"},
            "runtime": {
                "included": True,
                "python": ">=3.10",
                "root": "runtime",
                "requirements": "requirements.txt",
                "hash": runtime_hash,
                "excluded_app_code": [],
                "included_instances_runtime": included_instances,
            },
            "source": {
                "scenario": scenario,
                "version": version,
                "project_schema": PROJECT_SCHEMA_VERSION,
            },
            "dependencies_not_included": dependencies,
            "needs_configuration": needs,
            "required_assets": required_assets,
            "files": files,
            "assets": assets,
            "asset_bindings": project["asset_bindings"],
            "project": {
                "schema": PROJECT_SCHEMA_VERSION,
                "name": project["name"],
                "scenario": scenario,
                "spec_root": project["spec_root_relative"],
                "runtime_source": project["runtime_source_relative"],
                "frontend_source": project["frontend_source_relative"],
                "source_artifact_id": project["data"].get("source_artifact_id"),
                "source_content_hash": project["data"].get("source_content_hash"),
                "include_assets": bool(include_assets),
            },
            "checksums": checksums,
            "content_hash": content_hash,
            "check_scope": "checksums_only",
            "runtime_included": True,
        }
        _write_json(bundle / "manifest.json", manifest)
    except Exception:
        # Do not delete anything; the caller can inspect the partial build.
        raise

    verification = verify_artifact(bundle, require_service=True)
    if not verification.get("ok"):
        raise ProjectError(
            "built artifact failed static verification: "
            + "; ".join(verification.get("issues") or ["unknown verification error"])
        )

    zip_path = output_resolved / f"{bundle.name}.zip"
    if zip_path.exists():
        raise FileExistsError(f"artifact zip already exists: {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in bundle.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(bundle))
    return {
        "bundle": str(bundle),
        "zip": str(zip_path),
        "manifest": manifest,
        "verification": verification,
        "needs_configuration": needs,
        "assets": assets,
    }


__all__ = [
    "PROJECT_FILE",
    "PROJECT_SCHEMA_VERSION",
    "ProjectError",
    "build_project",
    "fork_artifact",
]
