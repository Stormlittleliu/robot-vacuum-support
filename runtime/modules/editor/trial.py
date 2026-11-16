# -*- coding: utf-8 -*-
"""Editor-only temporary trials on the shared instance session runtime.

An editor run must not build a ``WorkflowRuntime`` inside the 8600 process and
must not let tools write into the shared source ``DATA_ROOT``.  This module
prepares a throw-away trial root and drives the *same* ``SessionStore`` +
``SessionManager`` pair used by instance products.

Boundaries:

- the current editor document is projected into a private, read-only-by-
  convention spec snapshot under ``<trial>/specs``; the source spec / active
  pointer / revision are never written;
- secrets are never copied; only prompt / pool / cases style text is copied;
- worker data, checkpoints, workspace and logs live below ``<trial>``;
- ``runtime_root=PROJECT_ROOT`` is a development-only trial mode.  Formal
  artifacts keep using their own bundled runtime and self-contained checks;
- trials never touch the instance registry or a formal session API.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.paths import DATA_ROOT, MODEL_ROOT, PROJECT_ROOT, SPEC_ROOT
from core.paths import runtime_path
from modules.editor import document as editor_document
from modules.editor import spec_edit
from modules.instances.runtime.bootstrap import credential_keys_for_root, read_env_file
from modules.instances.runtime.store import SessionStore
from modules.instances.runtime.worker import ApprovalGone, SessionManager
from modules.scenario_format.spec import (
    active_spec_path,
    active_version,
    load_spec,
    save_spec,
)

TRIAL_ROLE = "editor_trial"
_TRIAL_ID_PREFIX = "trial-"
_COPY_FILES = ("prompt.yml", ".pool", "cases.jsonl", "entry.yml", "gate.yml")
_SEED_DIRS = (
    "knowledge",
    "external",
    "rag_index",
)
_SEED_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.sqlite", "*.sqlite-wal", "*.sqlite-shm", ".env", "*.secret", "*credential*"
)


class TrialError(RuntimeError):
    """Base error for editor trials."""


class TrialPreparationError(TrialError):
    """The current document cannot be projected into a trial spec."""

    def __init__(self, issues: list[str]):
        self.issues = list(issues)
        super().__init__("；".join(self.issues) or "试跑快照准备失败")


class TrialApprovalGone(TrialError):
    """The original trial runtime no longer exists; rerun instead of rebuilding."""


class TrialWorkerUnavailable(TrialError):
    """The shared runtime worker could not be started from this trial mode."""


@dataclass
class TrialConfig:
    """Attribute-style config consumed by ``SessionStore`` / ``SessionManager``.

    It deliberately mirrors the attributes the instance runtime reads, without
    reusing a formal ``InstanceConfig`` (which would imply registry ownership).
    """

    instance_id: str
    artifact_id: str
    artifact_root: Path
    scenario: str
    workflow: str | None
    storage_root: Path
    sessions_root: Path
    runtime_root: Path
    model_root: Path
    data_root: Path
    runtime_data_root: Path
    workspace_root: Path
    log_root: Path
    asset_bindings: dict[str, Any] = field(default_factory=dict)
    workflows: tuple[str, ...] = ()
    env_values: dict[str, str] = field(default_factory=dict)
    environment_file: Path | None = None
    credential_keys: tuple[str, ...] = ()
    role: str = TRIAL_ROLE
    ephemeral: bool = True

    @property
    def spec_root(self) -> Path:
        return self.artifact_root

    @property
    def default_workflow(self) -> str | None:
        return self.workflow or (self.workflows[0] if self.workflows else None)

    def credential_names(self) -> list[str]:
        """Names (never values) of provider keys explicitly loaded for this trial."""
        return sorted(
            key for key in self.credential_keys
            if str(self.env_values.get(key, "")).strip()
        )


def new_trial_id() -> str:
    return f"{_TRIAL_ID_PREFIX}{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _trial_log(message: str) -> None:
    """Append trial lifecycle lines to ``data/runtime/logs/editor-trial-instance*.log``."""
    try:
        path = runtime_path("logs", f"editor-trial-instance{os.getpid()}.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_now()} {message}\n")
    except OSError:
        pass


def _copy_optional_text(scenario: str, destination: Path) -> None:
    source = SPEC_ROOT / scenario
    for filename in _COPY_FILES:
        path = source / filename
        if path.is_file():
            shutil.copy2(path, destination / filename)


def _copy_active_snapshot(scenario: str, spec_root: Path) -> None:
    """Fallback when no document was supplied: copy the active spec bytes only."""
    source = SPEC_ROOT / scenario
    pointer = source / "active.json"
    if not pointer.is_file():
        raise TrialPreparationError([f"场景没有可复制的 active spec：{pointer}"])
    active = json.loads(pointer.read_text(encoding="utf-8"))
    version_file = str(active.get("file") or "")
    if not version_file or not (source / version_file).is_file():
        raise TrialPreparationError([f"active 指针损坏：{pointer}"])
    target = spec_root / scenario
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pointer, target / "active.json")
    shutil.copy2(source / version_file, target / version_file)
    _copy_optional_text(scenario, target)


def _resolve_trial_environment() -> tuple[Path | None, dict[str, str]]:
    """Read one explicitly chosen dev .env, without parent search or copying.

    ``AH_EDITOR_ENV_FILE`` wins.  Otherwise only ``PROJECT_ROOT/.env`` is read,
    and only when it exists.  Parsing is the shared stdlib ``read_env_file``;
    nothing is written to the trial root or to global ``os.environ``.
    """
    explicit = os.environ.get("AH_EDITOR_ENV_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path, read_env_file(path)
    default = PROJECT_ROOT / ".env"
    if default.is_file():
        return default, read_env_file(default)
    return None, {}


class TrialHandle:
    """One editor run (plus its only valid approval resume) on a shared session manager."""

    def __init__(
        self,
        scenario: str,
        document: dict[str, Any] | None,
        workflow: str | None = None,
        *,
        run_id: str | None = None,
        trial_id: str | None = None,
        manager: Any | None = None,
        store: SessionStore | None = None,
        seed_data: bool = True,
    ):
        self.scenario = str(scenario)
        self.document = document
        self.run_id = run_id
        self.trial_id = trial_id or new_trial_id()
        self.root = runtime_path("editor_trials", self.trial_id)
        self.spec_root = self.root / "specs"
        self.workflow = str(workflow or (document or {}).get("workflow") or "") or None
        self.thread_id: str | None = None
        self.last_trace_id: str | None = None
        self.last_interrupt: Any = None
        self.last_node_events: list[dict[str, Any]] = []
        self.last_trace: list[dict[str, Any]] = []
        self.last_context: dict[str, Any] | None = None
        self.usage: dict[str, Any] | None = None
        self.run_budget: Any = None
        self.answer = ""
        self.status = "idle"
        self._collected_events: list[dict[str, Any]] = []
        self._released = False
        self._closed = False
        self._ran = False
        self.environment_file, self.env_values = _resolve_trial_environment()

        self._prepare_root()
        try:
            self._prepare_spec()
            self.config = self._build_config()
            self.store = store or SessionStore(self.config)
            metadata = self.store.create(self.workflow)
            self.session_id = str(metadata["session_id"])
            if seed_data:
                self._seed_session_data()
            self.manager = manager or SessionManager(self.config, self.store)
            self._write_trial_metadata()
            _trial_log(
                f"prepared trial_id={self.trial_id} scenario={self.scenario} "
                f"workflow={self.workflow} session={self.session_id} root={self.root}"
            )
        except Exception as exc:
            if hasattr(self, "store") and hasattr(self, "session_id"):
                self._release_quietly()
            # Keep the trial identity on preparation failures so the editor run
            # record can still point at the evidence root.
            try:
                exc.trial_id = self.trial_id  # type: ignore[attr-defined]
                exc.trial_root = str(self.root)  # type: ignore[attr-defined]
            except Exception:
                pass
            raise

    # ------------------------------------------------------------ preparation
    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        self.spec_root.mkdir(parents=True, exist_ok=True)
        for directory in ("sessions", "data", "runtime", "workspace", "logs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def _prepare_spec(self) -> None:
        if self.document is None:
            _copy_active_snapshot(self.scenario, self.spec_root)
            return
        # Untrusted editor input: project it with the same code used by save /
        # validate, then write only into the trial spec root.
        draft, workflow = editor_document._project_document(self.scenario, self.document)
        issues = spec_edit.draft_errors(draft)
        if issues:
            raise TrialPreparationError(issues)
        spec = spec_edit.spec_from_draft(draft)
        self.workflow = self.workflow or workflow
        target = save_spec(spec, root=self.spec_root, expected_version=None)
        _trial_log(
            f"spec snapshot trial_id={self.trial_id} scenario={self.scenario} "
            f"file={target.name} workflows={sorted(spec.workflows)}"
        )
        _copy_optional_text(self.scenario, target.parent)

    def _build_config(self) -> TrialConfig:
        spec = load_spec(active_spec_path(self.scenario, self.spec_root))
        workflows = tuple(sorted(spec.workflows))
        if self.workflow and self.workflow not in spec.workflows:
            raise TrialPreparationError([f"未知工作流：{self.workflow}"])
        artifact_id = f"trial-{uuid.uuid4().hex}"
        credential_keys, _from_rag = credential_keys_for_root(PROJECT_ROOT)
        return TrialConfig(
            instance_id=f"editor-{self.trial_id}",
            artifact_id=artifact_id,
            artifact_root=self.spec_root,
            scenario=self.scenario,
            workflow=self.workflow or (workflows[0] if workflows else None),
            storage_root=self.root,
            sessions_root=self.root / "sessions",
            # Development-only: the shared worker currently imports project
            # sources; formal artifacts never use this shortcut.
            runtime_root=PROJECT_ROOT,
            model_root=MODEL_ROOT,
            data_root=self.root / "data",
            runtime_data_root=self.root / "runtime",
            workspace_root=self.root / "workspace",
            log_root=self.root / "logs",
            asset_bindings={},
            workflows=workflows,
            env_values=dict(self.env_values),
            environment_file=self.environment_file,
            credential_keys=tuple(credential_keys),
            role=TRIAL_ROLE,
            ephemeral=True,
        )

    def _seed_session_data(self) -> None:
        """Copy only whitelisted seed data into this session's private data root."""
        target = self.store.data_path(self.session_id)
        target.mkdir(parents=True, exist_ok=True)
        for name in _SEED_DIRS:
            source = DATA_ROOT / name
            if not source.is_dir():
                continue
            try:
                shutil.copytree(source, target / name, dirs_exist_ok=True, ignore=_SEED_IGNORE)
            except OSError:
                # A missing optional seed must not make the trial unusable.
                continue
        (target / ".seeded").touch()

    def _write_trial_metadata(self) -> None:
        source_active = active_version(self.scenario, SPEC_ROOT)
        payload = {
            "schema_version": 1,
            "trial_id": self.trial_id,
            "role": TRIAL_ROLE,
            "ephemeral": True,
            "instance_id": self.config.instance_id,
            "artifact_id": self.config.artifact_id,
            "scenario": self.scenario,
            "workflow": self.workflow,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "model": {
                # Names only: never environment values or the .env path.
                "credential_names": self.config.credential_names(),
            },
            "created_at": _now(),
            "source": {
                "spec_root": str(SPEC_ROOT),
                "active_version": source_active,
                "document_hash": _digest_json(self.document) if self.document is not None else None,
            },
            "paths": {
                "trial_root": str(self.root),
                "spec_root": str(self.spec_root),
                "sessions_root": str(self.config.sessions_root),
                "data_root": str(self.config.data_root),
                "model_root": str(self.config.model_root),
                "runtime_root": str(self.config.runtime_root),
            },
        }
        path = self.root / "trial.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------ run/resume
    def _event_sink(self, event_sink=None, token_sink=None):
        def adapter(item: Any) -> None:
            if not isinstance(item, dict):
                return
            kind = item.get("type")
            if kind == "answer_delta":
                delta = item.get("delta")
                if delta not in (None, "") and token_sink is not None:
                    token_sink(str(delta))
                return
            if kind == "node" and isinstance(item.get("event"), dict):
                event = item["event"]
            elif item.get("event") is not None or item.get("node") is not None:
                event = item
            else:
                return
            self._collected_events.append(event)
            if event_sink is not None:
                event_sink(event)

        return adapter

    def _apply_result(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            result = {"answer": str(result or ""), "status": "completed"}
        self.thread_id = str(result.get("thread_id") or self.thread_id or "") or None
        self.last_trace_id = result.get("trace_id") or self.last_trace_id
        interrupt = result.get("interrupt")
        status = str(result.get("status") or "completed")
        self.status = "interrupted" if status == "interrupted" or interrupt is not None else "completed"
        self.last_interrupt = interrupt if self.status == "interrupted" else None
        payload_events = result.get("node_events")
        if isinstance(payload_events, list) and payload_events:
            self.last_node_events = [item for item in payload_events if isinstance(item, dict)]
        else:
            self.last_node_events = list(self._collected_events)
        payload_trace = result.get("trace")
        self.last_trace = list(payload_trace) if isinstance(payload_trace, list) else []
        payload_context = result.get("context")
        self.last_context = payload_context if isinstance(payload_context, dict) else None
        usage = result.get("usage")
        self.usage = dict(usage) if isinstance(usage, dict) else None
        if self.usage is not None:
            self.run_budget = SimpleNamespace(
                total_tokens=self.usage.get("total_tokens"),
                max_tokens=self.usage.get("token_limit"),
            )
        else:
            self.run_budget = None
        self.answer = str(result.get("answer") or result.get("tool_result") or "")
        if not self.answer:
            self.answer = _final_answer(result.get("messages") or [])
        return result

    def run(self, workflow: str | None = None, prompt: str = "", event_sink=None, token_sink=None) -> dict[str, Any]:
        if self._closed:
            raise TrialError("试跑已结束，请重新发起新的试跑。")
        if self._ran:
            raise TrialError("一次试跑只能执行一次；请发起新的试跑。")
        self._ran = True
        self.workflow = str(workflow or self.workflow or self.config.default_workflow or "")
        _trial_log(f"run start trial_id={self.trial_id} workflow={self.workflow} session={self.session_id}")
        try:
            result = self.manager.run(
                self.session_id,
                {"prompt": str(prompt or ""), "workflow": self.workflow},
                event_sink=self._event_sink(event_sink, token_sink),
            )
        except ApprovalGone as exc:
            self._release_quietly()
            _trial_log(f"run approval gone trial_id={self.trial_id}: {exc}")
            raise TrialApprovalGone(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - release the worker and keep evidence on failure
            self._release_quietly()
            _trial_log(f"run error trial_id={self.trial_id} {type(exc).__name__}: {exc}")
            if _looks_like_worker_start_failure(exc):
                raise TrialWorkerUnavailable(_worker_gap_message(exc)) from exc
            raise
        payload = self._apply_result(result)
        if self.status != "interrupted":
            self.release()
        _trial_log(
            f"run end trial_id={self.trial_id} status={self.status} "
            f"thread={self.thread_id} trace={self.last_trace_id}"
        )
        return payload

    def resume(
        self,
        decision: Any = True,
        *,
        thread_id: str | None = None,
        event_sink=None,
        token_sink=None,
    ) -> dict[str, Any]:
        if self._closed:
            raise TrialApprovalGone("试跑已结束，请重新发起测试。")
        if not self._ran or not self.thread_id or self.last_interrupt is None:
            raise TrialApprovalGone("原审批运行已不在当前试跑，请重新发起测试。")
        if thread_id and str(thread_id) != self.thread_id:
            raise TrialApprovalGone("审批请求与发起它的试跑不一致，请重新发起测试。")
        _trial_log(f"resume start trial_id={self.trial_id} thread={self.thread_id} session={self.session_id}")
        try:
            result = self.manager.resume(
                self.session_id,
                {"thread_id": self.thread_id, "decision": decision},
                event_sink=self._event_sink(event_sink, token_sink),
            )
        except ApprovalGone as exc:
            self.last_interrupt = None
            self._release_quietly()
            _trial_log(f"resume approval gone trial_id={self.trial_id}: {exc}")
            raise TrialApprovalGone(str(exc)) from exc
        except Exception as exc:
            self._release_quietly()
            _trial_log(f"resume error trial_id={self.trial_id} {type(exc).__name__}: {exc}")
            raise
        payload = self._apply_result(result)
        if self.status != "interrupted":
            self.release()
        _trial_log(
            f"resume end trial_id={self.trial_id} status={self.status} "
            f"thread={self.thread_id} trace={self.last_trace_id}"
        )
        return payload

    # ------------------------------------------------------------ lifecycle
    def release(self) -> None:
        """Stop the worker but keep the trial root / session / logs as evidence."""
        if self._released:
            return
        self._released = True
        try:
            self.manager.release(self.session_id)
        except Exception:  # noqa: BLE001 - cleanup must not hide the run error
            pass
        _trial_log(f"worker released trial_id={self.trial_id} session={self.session_id}")

    def _release_quietly(self) -> None:
        self.release()

    def close(self) -> None:
        if self._closed:
            return
        self.release()
        self._closed = True

    @property
    def trial_root(self) -> str:
        return str(self.root)


def _final_answer(messages: list[Any]) -> str:
    for message in reversed(messages or []):
        role = getattr(message, "type", None)
        if role is None and isinstance(message, dict):
            role = message.get("role")
        if role not in {"ai", "assistant"}:
            continue
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        if content:
            return str(content)
    return ""


def _looks_like_worker_start_failure(exc: Exception) -> bool:
    text = str(exc)
    return any(
        marker in text
        for marker in ("会话运行进程已退出", "会话运行进程意外退出", "读取通道已断开", "No module named")
    )


def _worker_gap_message(exc: Exception) -> str:
    return (
        f"{exc}。editor_trial 子进程未能启动：共享 runtime worker 以 "
        "`python -B -m modules.instances.runtime.worker` 启动，PYTHONPATH 指向 "
        "PROJECT_ROOT；请检查 trial 根目录、worker.log 与模块导入错误。"
    )
