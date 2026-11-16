# -*- coding: utf-8 -*-
"""Artifact manifest（schema v2）加载与离线校验。

边界：

- 只读文件，不 import 业务运行时，不调用模型，不证明模型/知识库已配置；
- ``checksums`` 通过只说明产物字节完整，不等于 ``runtime`` 可启动；
- schema v1 作为 legacy 允许做 CLI 校验，但绝不算可服务实例。

路径安全：manifest 中的文件路径必须是相对产物根的 POSIX 相对路径，
不允许绝对路径、盘符、``..`` 或符号链接逃逸。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_RUNTIME_IGNORED_PARTS = {"__pycache__"}
_RUNTIME_IGNORED_SUFFIXES = {".pyc", ".pyo"}


class ManifestError(ValueError):
    """manifest 无法读取或 schema 不受支持。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _as_posix(value: Any) -> str:
    return str(value).replace("\\", "/")


def safe_relative_path(value: Any) -> tuple[bool, str]:
    """返回 ``(是否安全, 规范化后的相对路径或错误说明)``。"""
    if not isinstance(value, str) or not value.strip():
        return False, "empty path"
    normalized = _as_posix(value).strip()
    if normalized.startswith("/"):
        return False, f"absolute path is not allowed: {value!r}"
    if _DRIVE_RE.match(normalized):
        return False, f"drive-qualified path is not allowed: {value!r}"
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False, f"path traversal or empty segment is not allowed: {value!r}"
    return True, normalized


def _resolve_inside(root: Path, relative: str) -> tuple[Path | None, str | None]:
    """把安全相对路径解析到 root 内，并做一次真实路径逃逸检查。"""
    target = root / relative
    try:
        resolved_root = root.resolve()
        resolved_target = target.resolve()
    except OSError as exc:  # pragma: no cover - 极端文件系统错误
        return None, f"cannot resolve path {relative!r}: {exc}"
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError:
        return None, f"path escapes artifact root: {relative!r}"
    return target, None


def _manifest_path(root: Path) -> Path:
    return root / "manifest.json"


def load_manifest(root: str | Path) -> dict[str, Any]:
    """读取并粗校验 ``<root>/manifest.json``。

    未知 ``schema_version`` 直接拒绝（抛 :class:`ManifestError`）；
    schema 1 / 2 返回原始 dict，字段级校验交给 :func:`verify_artifact`。
    """
    base = Path(root)
    path = _manifest_path(base)
    if not path.is_file():
        raise ManifestError(f"manifest.json not found under {base}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read manifest.json: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest.json must contain a JSON object")
    schema = data.get("schema_version")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestError(
            f"unsupported schema_version {schema!r}; expected one of "
            f"{list(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    return data


def _iter_runtime_files(root: Path):
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & _RUNTIME_IGNORED_PARTS:
            continue
        if path.suffix.lower() in _RUNTIME_IGNORED_SUFFIXES:
            continue
        yield path


def hash_tree(root: str | Path) -> str:
    """对目录内容做确定性 sha256（跳过 ``__pycache__`` / ``*.pyc``）。

    跳过字节码，使“运行过一次”不会改变 runtime hash；manifest 的
    ``checksums`` 负责逐文件完整性，本函数负责 runtime 子树摘要。
    """
    base = Path(root)
    digest = hashlib.sha256()
    if not base.is_dir():
        return digest.hexdigest()
    for path in _iter_runtime_files(base):
        relative = path.relative_to(base).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def compute_content_hash(checksums: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(checksums, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def verify_artifact(
    root: str | Path, require_service: bool = True
) -> dict[str, Any]:
    """离线校验一个 artifact。

    返回 ``{ok, issues, manifest, warnings, service_ready, checksums_ok, legacy}``。

    - ``require_service=True``：还要求 ``serve.py`` 与 ``product_ui/`` 存在；
    - ``require_service=False``：允许只做 CLI/字节校验，但返回的
      ``service_ready`` 仍为 False，调用方不得据此宣称“可服务/可运行”。

    本函数不 import bundle 内代码、不构建图、不调用模型。
    """
    base = Path(root)
    report: dict[str, Any] = {
        "ok": False,
        "issues": [],
        "warnings": [],
        "manifest": None,
        "service_ready": False,
        "checksums_ok": False,
        "legacy": False,
    }
    issues: list[str] = report["issues"]
    warnings: list[str] = report["warnings"]

    if not base.is_dir():
        issues.append(f"artifact root is not a directory: {base}")
        return report

    try:
        manifest = load_manifest(base)
    except ManifestError as exc:
        issues.append(str(exc))
        return report

    report["manifest"] = manifest
    schema = manifest.get("schema_version")
    report["legacy"] = schema == LEGACY_SCHEMA_VERSION

    checksums_ok = _check_checksums(base, manifest, issues, warnings)
    report["checksums_ok"] = checksums_ok

    if schema == LEGACY_SCHEMA_VERSION:
        _check_legacy(base, manifest, issues, warnings)
        if require_service:
            issues.append(
                "legacy schema_version 1 is not a service-capable artifact: "
                "it has no v2 entry/frontend/runtime contract and no serve.py"
            )
    else:
        _check_v2_cli(base, manifest, issues, warnings)

    server_declared = "serve.py"
    frontend_declared = "product_ui"
    entry = manifest.get("entry") if isinstance(manifest.get("entry"), dict) else {}
    frontend = (
        manifest.get("frontend")
        if isinstance(manifest.get("frontend"), dict)
        else {}
    )
    server_relative = entry.get("server") if entry else None
    frontend_relative = frontend.get("root") if frontend else None

    if isinstance(server_relative, str):
        safe, normalized = safe_relative_path(server_relative)
        if safe:
            server_declared = normalized
    if isinstance(frontend_relative, str):
        safe, normalized = safe_relative_path(frontend_relative)
        if safe:
            frontend_declared = normalized

    server_exists = (base / server_declared).is_file()
    frontend_exists = (base / frontend_declared).is_dir()

    if not server_exists:
        message = f"service entry missing: {server_declared} is not present"
        if require_service:
            issues.append(message + " (next phase)")
        else:
            warnings.append(message + " (CLI check only; artifact is not service-ready)")
    if not frontend_exists:
        message = f"frontend root missing: {frontend_declared}/ is not present"
        if require_service:
            issues.append(message + " (next phase)")
        else:
            warnings.append(message + " (CLI check only; artifact is not service-ready)")

    report["service_ready"] = bool(
        not issues and server_exists and frontend_exists
    )
    report["ok"] = not issues
    return report


def _check_checksums(
    base: Path,
    manifest: dict[str, Any],
    issues: list[str],
    warnings: list[str],
) -> bool:
    schema = manifest.get("schema_version")
    checksums = manifest.get("checksums")
    if not isinstance(checksums, dict) or not checksums:
        issues.append("manifest.checksums is missing or empty")
        return False

    ok = True
    for filename, expected in sorted(checksums.items(), key=lambda item: str(item[0])):
        safe, normalized = safe_relative_path(filename)
        if not safe:
            issues.append(f"unsafe checksums path: {normalized}")
            ok = False
            continue
        if normalized == "manifest.json":
            issues.append("checksums must not include manifest.json (self-reference)")
            ok = False
            continue
        target, error = _resolve_inside(base, normalized)
        if error:
            issues.append(error)
            ok = False
            continue
        assert target is not None
        if not target.is_file():
            issues.append(f"checksum target missing: {normalized}")
            ok = False
            continue
        if not isinstance(expected, str) or _sha256_file(target) != expected:
            issues.append(f"checksum mismatch: {normalized}")
            ok = False

    declared_content_hash = manifest.get("content_hash")
    if isinstance(declared_content_hash, str) and declared_content_hash:
        actual = compute_content_hash(checksums)
        if actual != declared_content_hash:
            issues.append("content_hash mismatch")
            ok = False
    elif schema == SCHEMA_VERSION:
        issues.append("manifest.content_hash is missing")
        ok = False
    else:
        warnings.append("legacy manifest has no content_hash")

    files = manifest.get("files")
    if isinstance(files, list):
        name = manifest.get("name")
        scenario_prefix = name if isinstance(name, str) and name else None
        for filename in files:
            safe, normalized = safe_relative_path(filename)
            if not safe:
                issues.append(f"unsafe files path: {normalized}")
                ok = False
                continue
            target, error = _resolve_inside(base, normalized)
            if error:
                issues.append(error)
                ok = False
                continue
            assert target is not None
            if not target.is_file() and scenario_prefix:
                # 兼容旧 manifest：files 里的 spec 文件名相对 <name>/ 记录。
                fallback, fallback_error = _resolve_inside(
                    base, f"{scenario_prefix}/{normalized}"
                )
                if not fallback_error and fallback is not None and fallback.is_file():
                    target = fallback
            if not target.is_file():
                issues.append(f"declared file missing: {normalized}")
                ok = False
    elif schema == SCHEMA_VERSION:
        warnings.append("manifest.files is missing")
    return ok


def _check_legacy(
    base: Path,
    manifest: dict[str, Any],
    issues: list[str],
    warnings: list[str],
) -> None:
    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append("legacy manifest.name is missing")
        return
    active = base / name / "active.json"
    if not active.is_file():
        issues.append(f"legacy entry missing: {name}/active.json")
    if not (base / "run.py").is_file():
        issues.append("legacy entry missing: run.py")
    if not isinstance(manifest.get("workflows"), list) or not manifest["workflows"]:
        issues.append("legacy manifest.workflows is empty")
    warnings.append("schema_version 1 is legacy; CLI check only")


def _check_v2_cli(
    base: Path,
    manifest: dict[str, Any],
    issues: list[str],
    warnings: list[str],
) -> None:
    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append("manifest.name is missing")
        name = None

    version = manifest.get("version")
    if not isinstance(version, int):
        issues.append("manifest.version must be an integer")

    workflows = manifest.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        issues.append("manifest.workflows is empty")
        workflows = []

    source = manifest.get("source")
    if not isinstance(source, dict):
        issues.append("manifest.source is missing")
    else:
        if not isinstance(source.get("scenario"), str) or not source["scenario"]:
            issues.append("manifest.source.scenario is missing")
        if not isinstance(source.get("version"), int):
            issues.append("manifest.source.version must be an integer")

    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        issues.append("manifest.runtime is missing")
        runtime = {}
    if runtime.get("included") is not True:
        issues.append("manifest.runtime.included must be true")
    python = runtime.get("python")
    if not isinstance(python, str) or not python.strip():
        issues.append("manifest.runtime.python is missing")
    runtime_root, error = _runtime_relative(
        runtime.get("root", "runtime"), "manifest.runtime.root", issues
    )
    if runtime_root is not None and not (base / runtime_root).is_dir():
        issues.append(f"runtime root missing: {runtime_root}/")
    requirements, error = _runtime_relative(
        runtime.get("requirements", "requirements.txt"),
        "manifest.runtime.requirements",
        issues,
    )
    if requirements is not None and not (base / requirements).is_file():
        issues.append(f"runtime requirements missing: {requirements}")
    declared_hash = runtime.get("hash")
    if not isinstance(declared_hash, str) or not declared_hash:
        issues.append("manifest.runtime.hash is missing")
    elif runtime_root is not None:
        actual_hash = hash_tree(base / runtime_root)
        if actual_hash != declared_hash:
            issues.append("runtime hash mismatch")

    entry = manifest.get("entry")
    if not isinstance(entry, dict):
        issues.append("manifest.entry is missing")
        entry = {}
    cli, error = _runtime_relative(entry.get("cli", "run.py"), "manifest.entry.cli", issues)
    if cli is not None and cli != "run.py":
        warnings.append(f"manifest.entry.cli is {cli!r}; contract default is 'run.py'")
    if cli is not None and not (base / cli).is_file():
        issues.append(f"CLI entry missing: {cli}")
    server, error = _runtime_relative(
        entry.get("server", "serve.py"), "manifest.entry.server", issues
    )
    default_workflow = entry.get("default_workflow")
    if not isinstance(default_workflow, str) or not default_workflow:
        issues.append("manifest.entry.default_workflow is missing")
    elif workflows and default_workflow not in workflows:
        issues.append(
            f"manifest.entry.default_workflow {default_workflow!r} is not in workflows"
        )

    frontend = manifest.get("frontend")
    if not isinstance(frontend, dict):
        issues.append("manifest.frontend is missing")
        frontend = {}
    frontend_root, error = _runtime_relative(
        frontend.get("root", "product_ui"), "manifest.frontend.root", issues
    )
    api_version = frontend.get("api_version")
    if api_version != "v1":
        issues.append("manifest.frontend.api_version must be 'v1'")

    if name is not None:
        active_path = base / name / "active.json"
        if not active_path.is_file():
            issues.append(f"entry missing: {name}/active.json")
        else:
            _check_active_pointer(base, name, active_path, issues)


def _runtime_relative(
    value: Any, field: str, issues: list[str]
) -> tuple[str | None, str | None]:
    safe, normalized = safe_relative_path(value)
    if not safe:
        issues.append(f"{field}: {normalized}")
        return None, normalized
    return normalized, None


def _check_active_pointer(
    base: Path, name: str, active_path: Path, issues: list[str]
) -> None:
    try:
        pointer = json.loads(active_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        issues.append(f"invalid {name}/active.json: {exc}")
        return
    if not isinstance(pointer, dict):
        issues.append(f"invalid {name}/active.json: expected object")
        return
    version_file = pointer.get("file")
    if version_file is None:
        return
    safe, normalized = safe_relative_path(version_file)
    if not safe:
        issues.append(f"unsafe active.json file pointer: {normalized}")
        return
    target, error = _resolve_inside(base / name, normalized)
    if error:
        issues.append(error)
        return
    assert target is not None
    if not target.is_file():
        issues.append(f"active spec version missing: {name}/{normalized}")
