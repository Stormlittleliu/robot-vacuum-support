# -*- coding: utf-8 -*-
"""Session worker supervision: process, busy/lifecycle locks, capacity reclaim.

This is the instance-runtime version of the chat session design, with no import
of ``modules.chat``.  A worker gets a session-local data/workspace root while
scenario specs are read from the explicit artifact root.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    from .assets import (
        ASSET_KINDS,
        copy_rag_index,
        project_runtime_path,
        read_persist_directory,
        resolve_asset_sources,
    )
    from .store import SessionEnded, SessionNotFound, valid_session_id
except ImportError:  # artifact/runtime is a flat top-level directory
    from assets import (  # type: ignore
        ASSET_KINDS,
        copy_rag_index,
        project_runtime_path,
        read_persist_directory,
        resolve_asset_sources,
    )
    from store import SessionEnded, SessionNotFound, valid_session_id  # type: ignore

try:
    from core.observability import latency
except ImportError:  # pragma: no cover - very old artifact
    latency = None  # type: ignore

MAX_WORKERS = 16


class SessionBusy(RuntimeError):
    """The session already has an in-flight run or lifecycle transition."""


class SessionCapacity(RuntimeError):
    """All 16 workers are running/pending; idle reclaim cannot help."""


class ApprovalGone(RuntimeError):
    """The pending approval runtime is not in this live worker."""


def terminate_process(process) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
        )
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _safe_no_proxy(value: str) -> str:
    """Drop bracketed IPv6 no_proxy entries that break httpx URL parsing.

    The host can export ``NO_PROXY=...[::1]``; httpx/openai then raises
    ``InvalidURL: Invalid port: ':1]'`` before any network call.  Keep the
    unbracketed ``::1`` entry and all ordinary hosts, only remove the broken
    bracketed form.
    """
    parts = [part.strip() for part in str(value or "").split(",")]
    kept = [part for part in parts if part and not (part.startswith("[") and part.endswith("]"))]
    return ",".join(kept)


def _worker_env(config, session_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("NO_PROXY", "no_proxy"):
        if name in env:
            env[name] = _safe_no_proxy(env[name])
    # Apply this trial/instance's explicitly parsed values first; the AH_*
    # isolation roots below always win so two trials can never share config.
    values = getattr(config, "env_values", None) or {}
    if isinstance(values, dict):
        env.update({str(key): str(value) for key, value in values.items()})
    env.update(
        AH_DATA_ROOT=str(session_root / "data"),
        AH_RUNTIME_ROOT=str(session_root / "workspace"),
        AH_OUTPUT_ROOT=str(session_root / "outputs"),
        AH_SPEC_ROOT=str(config.artifact_root),
        AH_MODEL_ROOT=str(config.model_root),
        AH_INSTANCE_ID=str(config.instance_id),
        AH_ARTIFACT_ID=str(config.artifact_id),
        PYTHONIOENCODING="utf-8",
        # Stop python-dotenv / core.llm.factory load_dotenv() from walking up
        # into a parent source checkout and merging its .env.
        PYTHON_DOTENV_DISABLED="1",
    )
    if env.get("RVS_LATENCY_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}:
        env["RVS_LATENCY_PROCESS"] = "worker"
    # Do not let the child import sources from the editor repository.
    env["PYTHONPATH"] = str(config.runtime_root)
    credentials = (
        config.credential_names() if hasattr(config, "credential_names") else []
    )
    env["AH_INSTANCE_MODEL_CREDENTIALS"] = ",".join(credentials)
    env["AH_INSTANCE_MODEL_REQUIRED"] = "0" if credentials else "1"
    return env


#: Seed copy is allowed to duplicate data, never credentials or caches.
_SEED_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "node_modules",
    ".env",
    ".env.*",
    "*secret*",
    "*credential*",
    "*.pem",
    "*.key",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
)

def _required_names(config) -> set[str]:
    try:
        return set(config.required_assets())
    except (AttributeError, TypeError):
        return set()


def _seed_one(source: Path, target: Path) -> bool:
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=_SEED_IGNORE)
        return True
    if source.is_file() and not _is_secret_name(source.name):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return True
    return False


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    return (
        lower == ".env"
        or lower.startswith(".env.")
        or "secret" in lower
        or "credential" in lower
        or lower.endswith((".pem", ".key"))
    )


def _seed_session_data(config, session_root: Path) -> dict[str, Any]:
    """Copy declared read-only seed assets into the session's own data tree.

    Never reads or falls back to the editor repository's DATA_ROOT.  Bindings
    ``knowledge`` / ``models`` / ``external`` / ``rag_index`` are resolved
    against the artifact root (or absolute paths), not the harness checkout.
    Missing dependencies are recorded for health, not silently reported ready.
    """
    marker = session_root / "data" / ".seeded"
    status_path = session_root / "seed_status.json"
    if marker.exists():
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"seeded": True, "missing": []}
    source_root = Path(config.artifact_root) / "data"
    target_root = session_root / "data"
    runtime_root = session_root / "workspace"
    target_root.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    shared: list[str] = []
    missing: list[str] = []

    # Legacy artifact data tree (if present) is read-only input, copied once.
    # Asset categories are resolved below, never blindly copied from here.
    if source_root.is_dir():
        for child in sorted(source_root.iterdir()):
            if child.name in ASSET_KINDS:
                continue
            if _seed_one(child, target_root / child.name):
                copied.append(child.name)

    bindings = config.asset_bindings if isinstance(config.asset_bindings, dict) else {}
    manifest = getattr(config, "manifest", {}) or {}
    sources = resolve_asset_sources(config.artifact_root, bindings, manifest)

    for key in ASSET_KINDS:
        source = sources.get(key)
        if source is None or not source.exists():
            explicit = key in bindings or (key == "models" and "model" in bindings)
            if key in _required_names(config) or explicit:
                missing.append(key)
            continue
        if key == "models":
            # The model directory is shared read-only through AH_MODEL_ROOT.
            shared.append("models")
            continue
        if key == "rag_index":
            persist = read_persist_directory(config.artifact_root)
            target = project_runtime_path(
                persist,
                data_root=target_root,
                runtime_root=runtime_root,
            )
            if target is None:
                missing.append("rag_index")
                continue
            try:
                if copy_rag_index(source, target):
                    copied.append("rag_index")
                else:
                    missing.append("rag_index")
            except OSError:
                missing.append("rag_index")
            continue
        target_name = "knowledge" if key == "knowledge" else "external"
        if key == "external" and source.is_file():
            target = target_root / target_name / source.name
        else:
            target = target_root / target_name
        if _seed_one(source, target):
            copied.append(target_name)
        else:
            missing.append(key)

    result = {
        "seeded": True,
        "copied": sorted(set(copied)),
        "shared": sorted(set(shared)),
        "missing": sorted(set(missing)),
        "source": "artifact",
    }
    status_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return result


class SessionProcess:
    def __init__(self, config, store, session_id: str):
        metadata = store.require_active(session_id)
        if metadata.get("status") == "ended":
            raise SessionEnded("会话已结束，请先重新打开")
        self.config = config
        self.store = store
        self.session_id = session_id
        self.scenario = str(metadata.get("scenario") or config.scenario)
        self.root = store.session_root(session_id)
        self.root.mkdir(parents=True, exist_ok=True)
        if latency is not None:
            with latency.span("worker.seed_assets", session_id=session_id):
                _seed_session_data(config, self.root)
        else:
            _seed_session_data(config, self.root)
        self.busy = threading.Lock()
        self.closing = False
        self.pending_thread_id: str | None = None
        self.running = False
        self.last_used = time.time()
        self.log = (self.root / "worker.log").open("a", encoding="utf-8")
        command = [sys.executable, "-B", "-m", "modules.instances.runtime.worker"]
        try:
            if latency is not None:
                with latency.span("worker.spawn", session_id=session_id):
                    self.process = subprocess.Popen(
                        command,
                        cwd=str(config.runtime_root),
                        env=_worker_env(config, self.root),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=self.log,
                        text=True,
                        encoding="utf-8",
                        bufsize=1,
                        start_new_session=os.name != "nt",
                    )
            else:
                self.process = subprocess.Popen(
                    command,
                    cwd=str(config.runtime_root),
                    env=_worker_env(config, self.root),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=self.log,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                    start_new_session=os.name != "nt",
                )
        except Exception:
            self.log.close()
            raise

    def request(self, body: dict, action: str = "run", event_sink=None):
        if self.closing or self.process.poll() is not None:
            raise RuntimeError("会话运行进程已退出，请重新打开会话")
        self.last_used = time.time()
        self.running = True
        try:
            try:
                if latency is not None:
                    with latency.span("server.worker_ipc_send", action=action):
                        self.process.stdin.write(
                            json.dumps(
                                {"scenario": self.scenario, "body": body, "action": action},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        self.process.stdin.flush()
                else:
                    self.process.stdin.write(
                        json.dumps(
                            {"scenario": self.scenario, "body": body, "action": action},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError("会话运行进程已退出，请重新打开会话") from exc

            replies: queue.Queue = queue.Queue()

            def read():
                while True:
                    try:
                        line = self.process.stdout.readline()
                    except (OSError, ValueError):
                        replies.put({"error": "会话运行进程的读取通道已断开"})
                        return
                    if not line:
                        replies.put({"error": "会话运行进程意外退出"})
                        return
                    try:
                        payload = json.loads(line)
                    except (TypeError, ValueError):
                        replies.put({"error": "会话运行进程返回了无法解析的响应"})
                        return
                    if not isinstance(payload, dict):
                        replies.put({"error": "会话运行进程返回了非法响应"})
                        return
                    replies.put(payload)
                    if "event" not in payload:
                        return

            threading.Thread(target=read, daemon=True).start()
            configured = os.environ.get("AH_INSTANCE_RUN_TIMEOUT_SECONDS", "").strip()
            duration = float(configured or 0)
            deadline = time.monotonic() + duration if duration > 0 else None
            wait_span = latency.open_span("server.worker_wait", action=action) if latency is not None else None
            wait_status = "ok"
            try:
                while True:
                    try:
                        timeout = max(0.01, deadline - time.monotonic()) if deadline else None
                        payload = replies.get(timeout=timeout)
                    except queue.Empty:
                        wait_status = "timeout"
                        terminate_process(self.process)
                        raise RuntimeError("本次执行超过配置的运行超时，会话进程已停止")
                    if "event" in payload:
                        if event_sink is not None:
                            event_sink(payload["event"])
                        continue
                    if "error" in payload:
                        wait_status = "error"
                        code = payload.get("code")
                        try:
                            from core.llm.errors import TurnFailure

                            error = TurnFailure(
                                code or "run_error",
                                str(payload.get("error") or ""),
                                retryable=bool(payload.get("retryable", True)),
                                requires_configuration=bool(
                                    payload.get("requires_configuration", False)
                                ),
                                http_status=payload.get("http_status"),
                            )
                        except Exception:  # noqa: BLE001 - very old artifact
                            error = RuntimeError(payload["error"])
                            if code:
                                error.code = code  # type: ignore[attr-defined]
                        raise error
                    result = payload.get("result") or {}
                    self.pending_thread_id = (
                        str(result.get("thread_id") or "")
                        if result.get("status") == "interrupted"
                        else None
                    )
                    return result
            finally:
                if wait_span is not None:
                    wait_span.end(status=wait_status)
        finally:
            self.running = False
            self.last_used = time.time()

    def stop(self) -> None:
        self.closing = True
        self.pending_thread_id = None
        terminate_process(self.process)
        try:
            self.log.close()
        except OSError:
            pass


class SessionManager:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self._sessions: dict[str, SessionProcess] = {}
        self._closing: set[str] = set()
        self._lock = threading.RLock()
        self._lifecycle_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------ locks
    def _lifecycle_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            lock = self._lifecycle_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._lifecycle_locks[session_id] = lock
            return lock

    @contextmanager
    def _lifecycle(self, session_id: str):
        lock = self._lifecycle_lock(session_id)
        if not lock.acquire(blocking=False):
            raise SessionBusy("该会话正在执行生命周期操作，请稍后重试")
        try:
            yield
        finally:
            lock.release()

    # ------------------------------------------------------------ cleanup
    def _close_locked(self, session_id: str, session: SessionProcess) -> None:
        self._sessions.pop(session_id, None)
        self._closing.add(session_id)
        try:
            session.stop()
        finally:
            self._closing.discard(session_id)

    def _cleanup_exited_locked(self) -> None:
        for session_id, session in list(self._sessions.items()):
            if session.process.poll() is not None:
                self._sessions.pop(session_id, None)
                try:
                    session.log.close()
                except OSError:
                    pass

    def _reclaim_locked(self) -> bool:
        candidates: list[tuple[float, str, SessionProcess]] = []
        for session_id, session in list(self._sessions.items()):
            if session.closing or session.pending_thread_id:
                continue
            if session.running:
                continue
            if not session.busy.acquire(blocking=False):
                continue
            candidates.append((session.last_used, session_id, session))
        if not candidates:
            return False
        candidates.sort(key=lambda item: item[0])
        _, session_id, victim = candidates[0]
        for _, _, other in candidates[1:]:
            other.busy.release()
        try:
            self._close_locked(session_id, victim)
        finally:
            victim.busy.release()
        return True

    def live_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    # ------------------------------------------------------------ spawn
    def get(self, session_id: str) -> SessionProcess:
        if not valid_session_id(session_id):
            raise SessionNotFound("无效会话标识")
        with self._lifecycle(session_id):
            with self._lock:
                self._cleanup_exited_locked()
                if session_id in self._closing:
                    raise SessionBusy("会话正在释放，请稍后重试")
                session = self._sessions.get(session_id)
                if session is not None:
                    session.last_used = time.time()
                    return session
                metadata = self.store.get(session_id)
                if metadata.get("status") == "ended":
                    raise SessionEnded("会话已结束，请先重新打开")
                if len(self._sessions) >= MAX_WORKERS:
                    if not self._reclaim_locked():
                        raise SessionCapacity("会话进程已达上限，且没有可回收的空闲会话")
                session = SessionProcess(self.config, self.store, session_id)
                self._sessions[session_id] = session
                return session

    def peek(self, session_id: str) -> SessionProcess | None:
        with self._lock:
            if session_id in self._closing:
                return None
            session = self._sessions.get(session_id)
            if session is None or session.closing or session.process.poll() is not None:
                return None
            return session

    # ------------------------------------------------------------ requests
    def run(self, session_id: str, body: dict, event_sink=None, reservation=None):
        session = reservation or self.get(session_id)
        if reservation is not None and session.session_id != session_id:
            reservation.busy.release()
            raise SessionBusy("会话锁预留不匹配")
        if reservation is None and not session.busy.acquire(blocking=False):
            raise SessionBusy("该会话正在执行，请等待本次完成或先停止")
        try:
            metadata = self.store.require_active(session_id)
            if metadata.get("status") == "ended":
                raise SessionEnded("会话已结束，请先重新打开")
            return session.request(body, action="run", event_sink=event_sink)
        finally:
            session.busy.release()
            session.last_used = time.time()

    def resume(self, session_id: str, body: dict, event_sink=None, reservation=None):
        session = reservation or self.peek(session_id)
        if session is None or not session.pending_thread_id:
            if reservation is not None:
                reservation.busy.release()
            raise ApprovalGone("原审批运行已不在当前会话进程，请重新发送消息")
        if reservation is None and not session.busy.acquire(blocking=False):
            raise SessionBusy("该会话正在执行，请等待本次完成或先停止")
        try:
            self.store.require_active(session_id)
            thread_id = str(body.get("thread_id") or "")
            if thread_id != session.pending_thread_id:
                raise ApprovalGone("该审批已失效，请重新发送消息")
            return session.request(body, action="resume", event_sink=event_sink)
        except RuntimeError as exc:
            if getattr(exc, "code", None) == "approval_gone":
                raise ApprovalGone(str(exc)) from exc
            raise
        finally:
            session.busy.release()
            session.last_used = time.time()

    # ------------------------------------------------------------ lifecycle
    def release(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return
            self._closing.add(session_id)
        try:
            session.stop()
        finally:
            with self._lock:
                self._closing.discard(session_id)

    def stop(self, session_id: str) -> dict:
        with self._lifecycle(session_id):
            self.store.require_active(session_id)
            self.release(session_id)
            return self.store.mark(session_id, "active", thread_id=None)

    def end(self, session_id: str) -> dict:
        with self._lifecycle(session_id):
            self.store.require_active(session_id)
            self.release(session_id)
            return self.store.mark(session_id, "ended", thread_id=None)

    def reopen(self, session_id: str) -> dict:
        with self._lifecycle(session_id):
            metadata = self.store.get(session_id)
            session = self.peek(session_id)
            if session is not None:
                if session.pending_thread_id:
                    raise SessionBusy("会话正在等待审批；请先停止或结束会话后再重开")
                if session.running or not session.busy.acquire(blocking=False):
                    raise SessionBusy("会话正在执行；请先停止或结束会话后再重开")
                session.busy.release()
                if metadata.get("status") == "ended":
                    self.release(session_id)
            return self.store.mark(session_id, "active", thread_id=None)

    def close_all(self) -> None:
        for session_id in list(self._sessions):
            self.release(session_id)


# ---------------------------------------------------------------- child entry


def _execution_module():
    from . import execution  # package mode: modules.instances.runtime.execution

    return execution


def main() -> int:
    """Worker stdin/stdout protocol (also the ``python -m worker`` entry)."""
    import contextlib

    if latency is not None:
        latency.point("worker.process_start", pid=os.getpid())
    protocol = sys.stdout
    if latency is not None:
        # Eager top-level imports (execution -> workflow -> compiler/langgraph)
        # happen before the first request; keep them as a separate worker-phase
        # span instead of hiding them inside the first scenario_resolve.
        with latency.span("worker.bootstrap_import"):
            with contextlib.redirect_stdout(sys.stderr):
                execution = _execution_module()
    else:
        with contextlib.redirect_stdout(sys.stderr):
            execution = _execution_module()
    if latency is not None:
        latency.point("worker.ready", pid=os.getpid())

    def emit(event):
        protocol.write(json.dumps({"event": event}, ensure_ascii=False, default=str) + "\n")
        protocol.flush()

    for line in sys.stdin:
        try:
            request = json.loads(line)
            action = request.get("action")
            body = request.get("body") or {}
            ids = body.get("_rvs_latency") or {}
            if latency is not None:
                with latency.bind_context(
                    request_id=ids.get("request_id"),
                    turn_id=ids.get("turn_id"),
                    trace_id=ids.get("trace_id"),
                    cold=ids.get("cold"),
                    worker_parent_span_id=ids.get("worker_parent_span_id"),
                ):
                    latency.point("worker.request_received", action=action)
                    with latency.span("worker.execution", action=action):
                        with contextlib.redirect_stdout(sys.stderr):
                            if action == "health":
                                result = {"pid": os.getpid(), "worker": "instance"}
                            elif action == "resume":
                                result = execution.resume(
                                    request["scenario"],
                                    body,
                                    event_sink=lambda event: emit({"type": "node", "event": event}),
                                    token_sink=lambda delta: emit({"type": "answer_delta", "delta": delta}),
                                )
                            else:
                                result = execution.run(
                                    request["scenario"],
                                    body,
                                    event_sink=lambda event: emit({"type": "node", "event": event}),
                                    token_sink=lambda delta: emit({"type": "answer_delta", "delta": delta}),
                                )
                    with latency.span("worker.result_emit"):
                        protocol.write(json.dumps({"result": result}, ensure_ascii=False, default=str) + "\n")
                        protocol.flush()
            else:
                with contextlib.redirect_stdout(sys.stderr):
                    if action == "health":
                        result = {"pid": os.getpid(), "worker": "instance"}
                    elif action == "resume":
                        result = execution.resume(
                            request["scenario"],
                            body,
                            event_sink=lambda event: emit({"type": "node", "event": event}),
                            token_sink=lambda delta: emit({"type": "answer_delta", "delta": delta}),
                        )
                    else:
                        result = execution.run(
                            request["scenario"],
                            body,
                            event_sink=lambda event: emit({"type": "node", "event": event}),
                            token_sink=lambda delta: emit({"type": "answer_delta", "delta": delta}),
                        )
                protocol.write(json.dumps({"result": result}, ensure_ascii=False, default=str) + "\n")
                protocol.flush()
        except Exception as exc:  # noqa: BLE001 - one failed request must not kill the worker
            try:
                from core.llm.errors import (
                    failure_code,
                    failure_requires_configuration,
                    failure_retryable,
                    safe_error_message,
                )

                payload = {
                    "error": safe_error_message(exc),
                    "retryable": failure_retryable(exc),
                    "requires_configuration": failure_requires_configuration(exc),
                }
                code = getattr(exc, "code", None) or failure_code(exc)
                http_status = getattr(exc, "http_status", None)
                if http_status:
                    payload["http_status"] = http_status
            except Exception:  # noqa: BLE001 - very old artifact
                payload = {"error": str(exc)}
                code = getattr(exc, "code", None)
            if exc.__class__.__name__ == "ResumeGone":
                code = "approval_gone"
            if code:
                payload["code"] = str(code)
            protocol.write(json.dumps(payload, ensure_ascii=False) + "\n")
            protocol.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
