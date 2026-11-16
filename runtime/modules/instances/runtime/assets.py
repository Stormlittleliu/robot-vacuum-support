# -*- coding: utf-8 -*-
"""统一资产解析：显式 binding > manifest.assets > artifact/data/<kind>。

This module is deliberately standard-library only: ``bootstrap`` imports it
before the artifact runtime is added to ``sys.path``.  It never reads the
editor checkout's DATA_ROOT and never writes into the artifact.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

ASSET_KINDS = ("knowledge", "models", "external", "rag_index")

_CATEGORY_ALIASES = {
    "knowledge": "knowledge",
    "model": "models",
    "models": "models",
    "external": "external",
    "assets": "external",
    "rag_index": "rag_index",
    "rag-index": "rag_index",
}

_PERSIST_RE = re.compile(
    r"(?m)^\s*persist_directory\s*:\s*['\"]?([^'\"\r\n#]+)"
)

#: Directories/files that must never be copied from an asset tree.
_ASSET_IGNORE_NAMES = {
    "__pycache__",
    "node_modules",
    ".git",
    ".pytest_cache",
    "checkpoints",
    "logs",
    "sessions",
}
_ASSET_IGNORE_SUFFIXES = (".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx")


def normalize_category(value: Any) -> str | None:
    key = str(value or "").strip().lower().replace("-", "_")
    return _CATEGORY_ALIASES.get(key)


def _first_binding(value: Any) -> str | None:
    if isinstance(value, (list, tuple)):
        value = next(
            (item for item in value if isinstance(item, str) and item.strip()),
            None,
        )
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _manifest_asset_entries(manifest: dict[str, Any]) -> Iterable[tuple[str, str]]:
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        return []
    entries: list[tuple[str, str]] = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        category = normalize_category(item.get("category"))
        raw = item.get("path") or item.get("source")
        if category and isinstance(raw, str) and raw.strip():
            entries.append((category, raw.strip()))
    return entries


def resolve_asset_sources(
    artifact_root: str | Path,
    bindings: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
) -> dict[str, Path]:
    """Resolve one source per asset kind, in priority order.

    Explicit ``asset_bindings`` win; then ``manifest.assets`` packed by the
    source project (project-relative paths); last the legacy
    ``artifact/data/<kind>`` layout.
    """
    root = Path(artifact_root)
    manifest = manifest if isinstance(manifest, dict) else {}
    bindings = bindings if isinstance(bindings, dict) else {}
    resolved: dict[str, Path] = {}

    for kind in ASSET_KINDS:
        value = _first_binding(bindings.get(kind))
        if value is None and kind == "models":
            value = _first_binding(bindings.get("model"))
        if value is not None:
            resolved[kind] = _resolve(root, value)

    for category, raw in _manifest_asset_entries(manifest):
        if category in resolved or category not in ASSET_KINDS:
            continue
        path = Path(raw)
        if path.is_absolute():
            continue
        resolved[category] = (root / path).resolve()

    for kind in ASSET_KINDS:
        if kind in resolved:
            continue
        legacy = root / "data" / kind
        if legacy.exists():
            resolved[kind] = legacy.resolve()

    return resolved


def read_persist_directory(artifact_root: str | Path) -> str:
    """Read ``persist_directory`` from the artifact's runtime/config/chroma.yml."""
    root = Path(artifact_root)
    for candidate in (
        root / "runtime" / "config" / "chroma.yml",
        root / "config" / "chroma.yml",
    ):
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        match = _PERSIST_RE.search(text)
        if match:
            return match.group(1).strip()
    return ""


def project_runtime_path(
    persist_directory: str,
    *,
    data_root: str | Path,
    runtime_root: str | Path,
) -> Path | None:
    """Project a config ``data/...`` path onto the session's writable roots.

    Only safe relative ``data/...`` paths are accepted; absolute paths and
    ``..`` escapes are rejected so an asset config can never write outside the
    session root.
    """
    value = str(persist_directory or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return None
    parts = [part for part in value.split("/") if part]
    if not parts or parts[0] != "data" or any(part in {".", ".."} for part in parts):
        return None
    suffix = parts[1:]
    if not suffix:
        return Path(data_root).resolve()
    if suffix[0] == "runtime":
        return Path(runtime_root).joinpath(*suffix[1:]).resolve()
    return Path(data_root).joinpath(*suffix).resolve()


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    return (
        lower == ".env"
        or lower.startswith(".env.")
        or "secret" in lower
        or "credential" in lower
        or lower.endswith(_ASSET_IGNORE_SUFFIXES)
    )


def _ignore_asset(directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name.lower() in _ASSET_IGNORE_NAMES}
    ignored.update(name for name in names if _is_secret_name(name))
    return ignored


def _copy_tree_filtered(source: Path, target: Path) -> int:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=_ignore_asset,
    )
    return sum(1 for path in target.rglob("*") if path.is_file())


def _sqlite_backup(source: Path, target: Path) -> bool:
    """Copy a live/read-only Chroma SQLite file consistently."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        uri = source.resolve().as_uri() + "?mode=ro"
        src = sqlite3.connect(uri, uri=True)
        try:
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return True
    except (OSError, sqlite3.Error):
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        return False


def copy_rag_index(source: Path, target: Path) -> bool:
    """Copy a Chroma index into a writable private directory.

    ``chroma.sqlite3`` uses SQLite backup for a consistent snapshot; segment
    directories are copied as-is with secret/checkpoint/log files filtered.
    """
    if not source.exists():
        return False
    if source.is_file():
        target.mkdir(parents=True, exist_ok=True)
        destination = target / source.name
        if source.name == "chroma.sqlite3" and _sqlite_backup(source, destination):
            return True
        shutil.copy2(source, destination)
        return True
    target.mkdir(parents=True, exist_ok=True)
    database = source / "chroma.sqlite3"
    if database.is_file():
        if not _sqlite_backup(database, target / "chroma.sqlite3"):
            shutil.copy2(database, target / "chroma.sqlite3")
    for child in sorted(source.iterdir()):
        if child.name == "chroma.sqlite3" or _is_secret_name(child.name):
            continue
        if child.name.lower() in _ASSET_IGNORE_NAMES:
            continue
        destination = target / child.name
        if child.is_dir():
            _copy_tree_filtered(child, destination)
        elif child.is_file():
            shutil.copy2(child, destination)
    return True


def _collection_count(database: Path) -> int | None:
    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "embeddings" not in tables:
                return 0 if "collections" in tables else None
            return int(connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None


def rag_index_ready(index: str | Path | None) -> bool:
    """A rag index is ready only when it exists and has at least one embedding."""
    if index is None:
        return False
    path = Path(index)
    if path.is_file():
        database = path if path.name == "chroma.sqlite3" else None
    else:
        database = path / "chroma.sqlite3"
    if database is None or not database.is_file():
        return False
    count = _collection_count(database)
    if count is None:
        # Unknown schema: fall back to a non-empty directory, never fake ready.
        return False
    return count > 0


def required_asset_kinds(manifest: dict[str, Any] | None) -> list[str]:
    """Machine-readable required asset kinds declared by the artifact manifest."""
    manifest = manifest if isinstance(manifest, dict) else {}
    raw = manifest.get("required_assets")
    kinds: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("kind") or value.get("category")
        kind = normalize_category(value)
        if kind and kind not in kinds:
            kinds.append(kind)

    if isinstance(raw, dict):
        for key in raw:
            add(key)
    elif isinstance(raw, list):
        for item in raw:
            add(item)
    elif isinstance(raw, str):
        add(raw)
    return kinds


def required_assets_manifest(
    *, tools: Iterable[str], rag: dict[str, Any], chroma: dict[str, Any], agent: dict[str, Any]
) -> list[dict[str, Any]]:
    """Known RAG rules only; unknown tool dependencies are never invented."""
    tools = set(str(item) for item in tools or [])
    required: list[dict[str, Any]] = []

    def add(kind: str, reason: str) -> None:
        kind = normalize_category(kind) or kind
        for item in required:
            if item["kind"] == kind:
                if reason not in item["reasons"]:
                    item["reasons"].append(reason)
                return
        required.append({"kind": kind, "required": True, "reasons": [reason]})

    if "rag_summarize" in tools:
        add("knowledge", "rag_summarize")
        add("rag_index", "rag_summarize")
        provider = str(rag.get("embedding_provider") or "").strip().lower()
        if provider == "local":
            add("models", "local embedding model")
    external_data = str(agent.get("external_data_path") or "").strip()
    if external_data:
        add("external", "external_data_path")
    return required
