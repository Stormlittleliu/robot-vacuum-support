# -*- coding: utf-8 -*-
"""Instance bootstrap: parse the independent instance config before importing runtime.

This module intentionally uses only the standard library.  The generated
``serve.py`` imports it *before* adding ``artifact/runtime`` to ``sys.path`` and
before importing ``server`` so ``core.paths`` sees the instance roots.

Secrets are never copied.  Only the explicitly configured ``environment_file``
is read; there is no ``dotenv`` parent-directory search.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .assets import (
        ASSET_KINDS,
        rag_index_ready,
        required_asset_kinds,
        resolve_asset_sources,
    )
except ImportError:  # flat artifact layout
    from assets import (  # type: ignore
        ASSET_KINDS,
        rag_index_ready,
        required_asset_kinds,
        resolve_asset_sources,
    )

INSTANCE_SCHEMA_VERSION = 1
REQUIRED_FIELDS = (
    "schema_version",
    "instance_id",
    "artifact_id",
    "artifact_root",
    "name",
    "scenario",
    "version",
    "workflow",
    "host",
    "port",
    "storage_root",
    "environment_file",
    "asset_bindings",
)

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class InstanceConfigError(ValueError):
    """instance.json / artifact manifest 不满足契约。"""


@dataclass
class InstanceConfig:
    instance_id: str
    artifact_id: str
    artifact_root: Path
    name: str
    scenario: str
    version: int
    workflow: str | None
    host: str
    port: int
    storage_root: Path
    environment_file: Path | None
    asset_bindings: dict[str, Any]
    instance_dir: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    env_values: dict[str, str] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def runtime_root(self) -> Path:
        runtime = self.manifest.get("runtime")
        relative = "runtime"
        if isinstance(runtime, dict) and isinstance(runtime.get("root"), str):
            relative = runtime["root"]
        return (self.artifact_root / relative).resolve()

    @property
    def spec_root(self) -> Path:
        return self.artifact_root

    @property
    def _writable_base(self) -> Path:
        """Instance writable base.

        The registry phase creates ``<instance>/data`` and points
        ``storage_root`` at that data directory while workspace/sessions/logs
        sit beside it.  Hand-written instance configs may instead treat
        ``storage_root`` as the instance base.  Support both without ever
        writing into the artifact.
        """
        if (
            self.storage_root.name == "data"
            and (self.storage_root.parent / "sessions").is_dir()
            and not (self.storage_root / "sessions").is_dir()
        ):
            return self.storage_root.parent
        return self.storage_root

    @property
    def data_root(self) -> Path:
        if self._writable_base == self.storage_root:
            return self.storage_root / "data"
        return self.storage_root

    @property
    def runtime_data_root(self) -> Path:
        return self._writable_base / "runtime"

    @property
    def sessions_root(self) -> Path:
        return self._writable_base / "sessions"

    @property
    def workspace_root(self) -> Path:
        return self._writable_base / "workspace"

    @property
    def log_root(self) -> Path:
        return self._writable_base / "logs"

    @property
    def output_root(self) -> Path:
        """Writable outputs root (logs / traces / checkpoints), never the artifact."""
        return self._writable_base / "outputs"

    def binding_path(self, key: str) -> Path | None:
        """Resolve one ``asset_bindings`` entry without touching editor DATA_ROOT."""
        bindings = self.asset_bindings if isinstance(self.asset_bindings, dict) else {}
        value = bindings.get(key)
        if isinstance(value, (list, tuple)):
            value = next(
                (item for item in value if isinstance(item, str) and item.strip()),
                None,
            )
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            path = self.artifact_root / path
        return path.resolve()

    def missing_bindings(self) -> list[str]:
        """Configured-but-missing asset bindings; empty means no declared gap."""
        missing: list[str] = []
        for key in ASSET_KINDS:
            path = self.binding_path(key)
            if path is not None and not path.exists():
                missing.append(key)
        return missing

    def asset_sources(self) -> dict[str, Path]:
        """Explicit binding > manifest.assets > artifact/data/<kind>."""
        return resolve_asset_sources(
            self.artifact_root,
            self.asset_bindings if isinstance(self.asset_bindings, dict) else {},
            self.manifest,
        )

    def required_assets(self) -> list[str]:
        """Machine-readable asset kinds declared by the artifact manifest."""
        return required_asset_kinds(self.manifest)

    def missing_required_assets(self) -> list[str]:
        """Required assets that do not resolve to a usable source.

        ``rag_index`` must also be initialized (at least one embedding); an
        empty directory is a declared gap, never a fake ready.
        """
        sources = self.asset_sources()
        missing: list[str] = []
        for kind in self.required_assets():
            path = sources.get(kind)
            if kind == "models":
                path = self.model_root
                if path is None or not path.is_dir():
                    missing.append(kind)
                continue
            if path is None or not path.exists():
                missing.append(kind)
                continue
            if kind == "rag_index" and not rag_index_ready(path):
                missing.append(kind)
        return missing

    @property
    def product_ui_root(self) -> Path:
        frontend = self.manifest.get("frontend")
        relative = "product_ui"
        if isinstance(frontend, dict) and isinstance(frontend.get("root"), str):
            relative = frontend["root"]
        return (self.artifact_root / relative).resolve()

    @property
    def model_root(self) -> Path:
        """Read-only shared model directory (never copied per session)."""
        return self.asset_sources().get(
            "models", (self.artifact_root / "data" / "models").resolve()
        )

    @property
    def workflows(self) -> list[str]:
        values = self.manifest.get("workflows")
        return [str(item) for item in values] if isinstance(values, list) else []

    @property
    def default_workflow(self) -> str | None:
        if self.workflow:
            return self.workflow
        entry = self.manifest.get("entry")
        if isinstance(entry, dict) and isinstance(entry.get("default_workflow"), str):
            return entry["default_workflow"]
        return self.workflows[0] if self.workflows else None

    def credential_names(self) -> list[str]:
        """Configured credential names, restricted to the provider's required key.

        ``runtime/config/rag.yml`` decides the provider (deepseek / dashscope).
        An unrelated ``FOO_API_KEY`` in ``environment_file`` must never make the
        instance look configured.  Only names are returned, never values.
        """
        keys, _known_provider = credential_keys_for_root(self.artifact_root)
        return sorted(
            key for key in keys if str(self.env_values.get(key, "")).strip()
        )


_PROVIDER_CREDENTIAL_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_KNOWN_CREDENTIAL_KEYS = tuple(dict.fromkeys(_PROVIDER_CREDENTIAL_KEYS.values()))


def _read_rag_providers(root: Path) -> list[str]:
    """Read chat/embedding providers from ``config/rag.yml`` with stdlib only."""
    for candidate in (root / "runtime" / "config" / "rag.yml", root / "config" / "rag.yml"):
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        providers: list[str] = []
        for line in text.splitlines():
            match = re.match(
                r"\s*(?:chat_provider|embedding_provider)\s*:\s*['\"]?([A-Za-z0-9_-]+)",
                line,
            )
            if match:
                provider = match.group(1).lower()
                if provider not in providers:
                    providers.append(provider)
        if providers:
            return providers
    return []


def credential_keys_for_root(root: str | Path) -> tuple[tuple[str, ...], bool]:
    """Return provider-required credential names for an artifact/project root.

    The second value says whether a provider was actually read from
    ``config/rag.yml`` (in ``<root>/runtime/config`` or ``<root>/config``).  A
    missing config keeps the legacy known-provider fallback so old artifacts do
    not lose an explicitly configured key, but arbitrary ``FOO_API_KEY`` values
    are still ignored.
    """
    providers = _read_rag_providers(Path(root))
    keys: list[str] = []
    for provider in providers:
        key = _PROVIDER_CREDENTIAL_KEYS.get(provider)
        if key and key not in keys:
            keys.append(key)
    if keys:
        return tuple(keys), True
    return _KNOWN_CREDENTIAL_KEYS, False


def _absolute(base: Path, value: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a small .env file without dotenv and without parent search."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _load_manifest(artifact_root: Path, expected_artifact_id: str) -> dict[str, Any]:
    path = artifact_root / "manifest.json"
    if not path.is_file():
        raise InstanceConfigError(f"artifact manifest missing: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstanceConfigError(f"cannot read artifact manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise InstanceConfigError("artifact manifest must be an object")
    if manifest.get("schema_version") != 2:
        raise InstanceConfigError(
            f"artifact schema_version must be 2, got {manifest.get('schema_version')!r}"
        )
    artifact_id = str(manifest.get("artifact_id") or "")
    if artifact_id != expected_artifact_id:
        raise InstanceConfigError(
            f"artifact_id mismatch: instance={expected_artifact_id!r} manifest={artifact_id!r}"
        )
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("included") is not True:
        raise InstanceConfigError("artifact manifest.runtime.included must be true")
    return manifest


def load_instance_config(path: str | Path) -> InstanceConfig:
    """Load ``instances/<id>/instance.json`` and the referenced v2 manifest."""
    config_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstanceConfigError(f"cannot read instance config: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstanceConfigError("instance config must be a JSON object")
    if payload.get("schema_version") != INSTANCE_SCHEMA_VERSION:
        raise InstanceConfigError("instance schema_version must be 1")
    missing = [name for name in REQUIRED_FIELDS if name not in payload]
    if missing:
        raise InstanceConfigError("instance config missing: " + ", ".join(missing))

    instance_dir = config_path.parent
    instance_id = str(payload.get("instance_id") or "")
    if not _ID_RE.fullmatch(instance_id):
        raise InstanceConfigError("invalid instance_id")
    artifact_id = str(payload.get("artifact_id") or "")
    artifact_root = _absolute(instance_dir, str(payload.get("artifact_root") or ""))
    storage_root = _absolute(instance_dir, str(payload.get("storage_root") or ""))
    manifest = _load_manifest(artifact_root, artifact_id)

    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise InstanceConfigError("instance version must be an integer")
    port = payload.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        raise InstanceConfigError("instance port must be an integer")

    env_value = payload.get("environment_file")
    environment_file: Path | None = None
    if isinstance(env_value, str) and env_value.strip():
        environment_file = _absolute(instance_dir, env_value)

    bindings = payload.get("asset_bindings")
    if not isinstance(bindings, dict):
        raise InstanceConfigError("instance asset_bindings must be an object")

    config = InstanceConfig(
        instance_id=instance_id,
        artifact_id=artifact_id,
        artifact_root=artifact_root,
        name=str(payload.get("name") or ""),
        scenario=str(payload.get("scenario") or ""),
        version=version,
        workflow=str(payload["workflow"]) if payload.get("workflow") else None,
        host=str(payload.get("host") or "127.0.0.1"),
        port=port,
        storage_root=storage_root,
        environment_file=environment_file,
        asset_bindings=bindings,
        instance_dir=instance_dir,
        manifest=manifest,
        env_values=read_env_file(environment_file) if environment_file is not None else {},
    )

    if not config.runtime_root.is_dir():
        config.issues.append(f"runtime root missing: {config.runtime_root}")
    if not config.product_ui_root.is_dir():
        config.issues.append("product_ui missing; development fallback runtime/web will be used")
    if not (artifact_root / config.scenario / "active.json").is_file():
        config.issues.append(f"scenario spec missing: {config.scenario}/active.json")
    if config.workflow and config.workflows and config.workflow not in config.workflows:
        config.issues.append(f"configured workflow {config.workflow!r} is not in artifact workflows")
    if environment_file is not None and not environment_file.is_file():
        config.issues.append(f"environment_file missing: {environment_file}")
    return config


def configure_environment(config: InstanceConfig) -> None:
    """Apply instance-scoped paths/secrets.  Must run before importing ``server``."""
    # Only the explicitly configured file, never a parent .env or dotenv search.
    for key, value in config.env_values.items():
        os.environ[key] = value
    # python-dotenv honours this flag; core.llm.factory's load_dotenv() must not
    # climb from the artifact/runtime directory into the source repository.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    credentials = config.credential_names()
    os.environ["AH_INSTANCE_MODEL_CREDENTIALS"] = ",".join(credentials)
    os.environ["AH_INSTANCE_MODEL_REQUIRED"] = "0" if credentials else "1"
    os.environ["AH_DATA_ROOT"] = str(config.data_root)
    os.environ["AH_RUNTIME_ROOT"] = str(config.runtime_data_root)
    os.environ["AH_SPEC_ROOT"] = str(config.spec_root)
    os.environ["AH_MODEL_ROOT"] = str(config.model_root)
    os.environ["AH_OUTPUT_ROOT"] = str(config.output_root)
    os.environ["AH_INSTANCE_ID"] = config.instance_id
    os.environ["AH_ARTIFACT_ID"] = config.artifact_id
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for directory in (
        config.storage_root,
        config.data_root,
        config.runtime_data_root,
        config.sessions_root,
        config.workspace_root,
        config.log_root,
        config.output_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    runtime_root = str(config.runtime_root)
    if runtime_root not in sys.path:
        sys.path.insert(0, runtime_root)


def resolve_instance_config(
    *, instance_config: str | None = None, instance_root: str | None = None
) -> Path:
    if instance_config:
        return Path(instance_config).expanduser().resolve()
    if instance_root:
        root = Path(instance_root).expanduser().resolve()
        direct = root / "instance.json"
        if direct.is_file():
            return direct
        if (root / "instances").is_dir():
            matches = sorted((root / "instances").glob("*/instance.json"))
            if len(matches) == 1:
                return matches[0]
        raise InstanceConfigError(f"instance.json not found under {root}")
    default = Path.cwd() / "instance.json"
    if default.is_file():
        return default
    raise InstanceConfigError("--instance-config or --instance-root is required")
