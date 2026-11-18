# -*- coding: utf-8 -*-
"""Full-product acceptance for the runtime/scene refactor on port 18082.

The product under test is a freshly built, complete artifact served by its
normal ``serve.py --instance-config`` entry.  Cases are driven only through
HTTP/SSE, using the frozen 12-session / 18-turn three-mainlines suite and its
historical evaluator.  A Docker engine is used only if one is already running;
this script never starts Docker Desktop, a daemon or a GUI.

It adds:
* at most 2 extra user turns (checkpoint continuity, with a real restart
  between them);
* one negative provider probe for the typed-error path (oversized request, no
  fake response);
* the real human-handoff lifecycle through the admin/user HTTP APIs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "runtime"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from eval.runner import LiveServer  # noqa: E402
import scripts.run_three_mainlines as historical  # noqa: E402

DEFAULT_ARTIFACT = ROOT.parent / "robot-vacuum-support-built" / "robot_vacuum-v4-d30692614a"
DEFAULT_ENV = Path(r"E:\CODE\Myproject\agent-workspace\my_agent_harness\.env")
DEFAULT_OUT = ROOT / "outputs" / "runtime-refactor-v1"
PORT = 18082
EXISTING_PORT = 18080
SUITE_DIR = ROOT / "eval" / "development-next" / "three-mainlines-v1"
SCHEMA = "runtime-refactor-acceptance/v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def docker_state() -> dict[str, Any]:
    """Report the existing engine only; never start Docker Desktop/daemon."""
    try:
        completed = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            timeout=15,
        )
        return {
            "available": completed.returncode == 0,
            "server_version": completed.stdout.strip() if completed.returncode == 0 else None,
            "error": None if completed.returncode == 0 else completed.stderr.strip()[:300],
            "started_by_us": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "server_version": None, "error": f"{type(exc).__name__}: {exc}", "started_by_us": False}


def env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_instance_env(path: Path, authorized: Path) -> dict[str, str]:
    """Copy only the authorized model key; add isolated access/admin tokens."""
    source = env_values(authorized)
    model_key = source.get("DEEPSEEK_API_KEY") or ""
    if not model_key:
        raise RuntimeError("authorized environment_file has no DEEPSEEK_API_KEY")
    generated = {
        "RVS_ACCESS_TOKEN": secrets.token_urlsafe(24),
        "RVS_ADMIN_TOKEN": secrets.token_urlsafe(24),
        "RVS_TURN_INTERPRETER": "on",
        "RVS_INSTANCE_NAME": "robot-vacuum runtime refactor 18082",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    lines = [f"DEEPSEEK_API_KEY={model_key}"] + [f"{key}={value}" for key, value in generated.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return generated


def http_json(base: str, token: str, path: str, *, payload: dict | None = None, method: str = "GET", timeout: float = 60) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def send_sse(base: str, token: str, session_id: str, prompt: str, *, timeout: float = 180.0) -> list[dict[str, Any]]:
    return _sse_url(
        base + f"/api/sessions/{session_id}/messages",
        token,
        {"prompt": prompt},
        timeout=timeout,
    )


def _sse_url(url: str, token: str, payload: dict[str, Any], *, timeout: float = 180.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    events: list[dict[str, Any]] = []
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            events.append(event)
            if event.get("type") in ("done", "interrupt"):
                break
    return events


def first_event(events: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    for event in events:
        if event.get("type") == kind:
            return event
    return None


def last_answer(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") in ("answer", "interrupt"):
            return event
    return None


def create_session(base: str, token: str) -> str:
    payload = http_json(base, token, "/api/sessions", payload={"workflow": "agent"}, method="POST")
    session_id = payload.get("session_id")
    if not session_id:
        raise RuntimeError(f"session creation returned no session_id: {payload}")
    return str(session_id)


def error_trace_rows(storage: Path, session_id: str) -> dict[str, Any]:
    root = storage / "sessions" / session_id / "outputs" / "traces"
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("trace_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") == "run_end":
                rows.append(row)
    return {"files": [str(path) for path in sorted(root.glob("trace_*.jsonl"))], "run_end": rows}


def run_12_cases(base: str, token: str, out_dir: Path) -> dict[str, Any]:
    historical.BASE_URL = base
    historical.PORT = PORT
    historical.EXISTING_PORT = EXISTING_PORT
    historical.PROJECT = "rvs-refactor18082"
    historical.IMAGE = "local-artifact:robot_vacuum-v4-d30692614a"
    historical.OUT_DIR = out_dir
    suite = read_json(SUITE_DIR / "suite.json")
    cases_doc = read_json(SUITE_DIR / "cases.json")
    validation = historical.validate(suite, cases_doc)
    if not validation["ok"]:
        return {"status": "blocked_validation", "validation": validation}
    cases = cases_doc.get("cases") or []
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(historical.run_case, case, token, suite): case for case in cases}
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as exc:  # noqa: BLE001
                case = futures[future]
                rows.append({"id": case.get("id"), "status": "error", "error": f"{type(exc).__name__}: {exc}", "turns": []})
    order = [case.get("id") for case in cases]
    rows.sort(key=lambda row: order.index(row.get("id")) if row.get("id") in order else 999)

    per_case = []
    for row in rows:
        checks = [check for turn in row.get("turns") or [] for check in turn.get("checks") or []]
        per_case.append({
            "id": row.get("id"),
            "group": row.get("group"),
            "topic": row.get("topic"),
            "theme": row.get("theme"),
            "status": row.get("status"),
            "turns": len(row.get("turns") or []),
            "checks_total": len(checks),
            "checks_passed": sum(1 for check in checks if check.get("ok")),
            "failed_checks": [check.get("id") for check in checks if not check.get("ok")],
            "session_last_status": row.get("session_last_status"),
            "errors": [turn.get("error") for turn in row.get("turns") or [] if turn.get("error")],
            "error_codes": [turn.get("error_code") for turn in row.get("turns") or [] if turn.get("error_code")],
            "turn_summaries": [
                {
                    "turn": turn.get("turn"),
                    "question": turn.get("question"),
                    "answer": turn.get("answer"),
                    "error": turn.get("error"),
                    "error_code": turn.get("error_code"),
                    "retrieval_attempted": next((c.get("ok") for c in turn.get("checks") or [] if c.get("id") == "retrieval_attempted"), None),
                    "state": turn.get("state") if row.get("state_applicable") else None,
                }
                for turn in row.get("turns") or []
            ],
        })
    by_group: dict[str, Any] = {}
    for row in rows:
        bucket = by_group.setdefault(str(row.get("group")), {"total": 0, "passed_structural": 0})
        bucket["total"] += 1
        if row.get("status") == "passed_structural":
            bucket["passed_structural"] += 1
    report = {
        "status": "completed_with_findings" if any(row.get("status") != "passed_structural" for row in rows) else "completed_structural_pass",
        "validation": {"ok": validation["ok"], "counts": validation.get("counts")},
        "sessions_planned": len(cases),
        "sessions_run": sum(1 for row in rows if row.get("session_id")),
        "turns_planned": sum(len(case.get("turns") or []) for case in cases),
        "turns_run": sum(len(row.get("turns") or []) for row in rows),
        "concurrency": 2,
        "by_group": by_group,
        "cases": per_case,
        "frozen_cases_sha256": sha256_file(SUITE_DIR / "cases.json"),
        "frozen_suite_sha256": sha256_file(SUITE_DIR / "suite.json"),
        "note": "frozen 12 cases / 18 turns; expectations unchanged; not a success rate",
    }
    write_json(out_dir / "transcripts-12case.json", {"generated_at": now_iso(), "cases": rows})
    write_json(out_dir / "report-12case.json", report)
    return report


def checkpoint_restart_check(base: str, token: str, server: LiveServer, out_dir: Path) -> dict[str, Any]:
    session_id = create_session(base, token)
    first_prompt = "我叫小明。机器人回充时总在充电座前打转，充不上电。"
    first_events = send_sse(base, token, session_id, first_prompt)
    first = last_answer(first_events) or {}
    first_error = first_event(first_events, "error")
    thread_first = first.get("thread_id")
    restart = server.restart()
    second_prompt = "我叫什么名字？只回答名字，不要重复之前的排查步骤。"
    second_events = send_sse(base, token, session_id, second_prompt)
    second = last_answer(second_events) or {}
    second_error = first_event(second_events, "error")
    detail = http_json(base, token, f"/api/sessions/{session_id}")
    transcript = detail.get("transcript") or []
    roles = [row.get("role") for row in transcript if isinstance(row, dict)]
    user_texts = [row.get("content") for row in transcript if isinstance(row, dict) and row.get("role") == "user"]
    checks = {
        "first_turn_answer": bool(first.get("answer")) and not first_error,
        "restart_ready": bool(restart.get("ready")),
        "second_turn_answer": bool(second.get("answer")) and not second_error,
        "answer_keeps_name": "小明" in str(second.get("answer") or ""),
        "thread_id_stable": bool(thread_first) and thread_first == second.get("thread_id"),
        "transcript_roles": roles == ["user", "assistant", "user", "assistant"],
        "user_message_not_duplicated": user_texts == [first_prompt, second_prompt],
    }
    result = {
        "session_id": session_id,
        "first_question": first_prompt,
        "first_answer": first.get("answer"),
        "second_question": second_prompt,
        "second_answer": second.get("answer"),
        "thread_id_first": thread_first,
        "thread_id_second": second.get("thread_id"),
        "checks": checks,
        "passed": all(checks.values()),
        "transcript_message_count": len(transcript),
        "transcript_roles": roles,
        "note": "2 extra user turns; a real server/worker restart happens between them",
    }
    write_json(out_dir / "checkpoint-restart.json", result)
    return result


def _negative_env(path: Path, *, port: int) -> dict[str, str]:
    """Isolated negative probe: deliberately invalid provider key + local tokens."""
    generated = {
        "DEEPSEEK_API_KEY": "sk-runtime-refactor-invalid-probe",
        "RVS_ACCESS_TOKEN": secrets.token_urlsafe(24),
        "RVS_ADMIN_TOKEN": secrets.token_urlsafe(24),
        "RVS_TURN_INTERPRETER": "off",
        "RVS_INSTANCE_NAME": f"robot-vacuum runtime refactor error probe {port}",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    path.write_text("\n".join(f"{key}={value}" for key, value in generated.items()) + "\n", encoding="utf-8")
    return generated


def provider_error_probe(artifact: Path, out_dir: Path, *, port: int = 18083) -> dict[str, Any]:
    """Real provider-error path on a second isolated full-product instance."""
    root = out_dir / "provider-error"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    env_path = root / "instance.env"
    tokens = _negative_env(env_path, port=port)
    storage = root / "storage"
    instance = {
        "schema_version": 1,
        "instance_id": "robot-vacuum-runtime-refactor-error-18083",
        "artifact_id": read_json(artifact / "manifest.json").get("artifact_id"),
        "artifact_root": str(artifact),
        "name": "robot-vacuum runtime refactor error probe",
        "scenario": "robot_vacuum",
        "version": 4,
        "workflow": "agent",
        "host": "127.0.0.1",
        "port": port,
        "storage_root": str(storage),
        "environment_file": str(env_path),
        "asset_bindings": {},
    }
    instance_path = root / "instance.json"
    write_json(instance_path, instance)
    server = LiveServer(
        python=sys.executable,
        serve_py=artifact / "serve.py",
        instance_path=instance_path,
        host="127.0.0.1",
        port=port,
        log_path=root / "server.log",
        cwd=artifact,
    )
    try:
        health = server.start(timeout=240)
        session_id = create_session(server.base_url, tokens["RVS_ACCESS_TOKEN"])
        events = send_sse(server.base_url, tokens["RVS_ACCESS_TOKEN"], session_id, "回充失败怎么办？", timeout=120)
        error = first_event(events, "error") or {}
        detail = http_json(server.base_url, tokens["RVS_ACCESS_TOKEN"], f"/api/sessions/{session_id}")
        trace = error_trace_rows(storage, session_id)
        trace_errors = [row for row in trace["run_end"] if str(row.get("status")) == "error"]
        return {
            "port": port,
            "health": health,
            "session_id": session_id,
            "error": error,
            "session_last_status": detail.get("last_status"),
            "trace_files": trace["files"],
            "run_end": trace["run_end"],
            "checks": {
                "sse_error": bool(error),
                "typed_code": bool(error.get("code")),
                "session_error": detail.get("last_status") == "error",
                "run_end_error": bool(trace_errors),
            },
        }
    finally:
        server.stop()


def typed_error_check(base: str, token: str, storage: Path, out_dir: Path, artifact: Path) -> dict[str, Any]:
    """Real typed error paths: approval_gone + invalid-provider negative probe."""
    session_id = create_session(base, token)
    pending_http: dict[str, Any] = {}
    try:
        pending = _sse_url(
            base + f"/api/sessions/{session_id}/resume",
            token,
            {"thread_id": "not-a-pending-thread", "decision": True},
        )
        pending_http = {"events": pending}
    except urllib.error.HTTPError as exc:
        pending_http = {
            "http_status": exc.code,
            "body": exc.read().decode("utf-8", errors="replace")[:500],
        }
    provider = provider_error_probe(artifact, out_dir)
    checks = {
        "approval_gone_http_410": pending_http.get("http_status") == 410,
        "approval_gone_typed_body": "approval_gone" in str(pending_http.get("body") or ""),
        **{f"provider_{key}": value for key, value in provider.get("checks", {}).items()},
    }
    result = {
        "approval_gone": {
            "session_id": session_id,
            "response": pending_http,
        },
        "provider_error": provider,
        "checks": checks,
        "passed": all(checks.values()),
        "note": "provider probe uses an isolated full product on 18083 with a deliberately invalid key; it is a negative error-path check, not QA success data",
    }
    write_json(out_dir / "error-path.json", result)
    return result


def handoff_check(base: str, token: str, admin_token: str, out_dir: Path) -> dict[str, Any]:
    session_id = create_session(base, token)
    requested = http_json(base, token, f"/api/sessions/{session_id}/handoff/request", payload={}, method="POST")
    waiting = http_json(base, admin_token, "/api/admin/handoff?status=waiting")
    listed = [row for row in waiting.get("rows") or [] if row.get("session_id") == session_id]
    queued = send_sse(base, token, session_id, "人工客服在吗？")
    accepted = http_json(base, admin_token, f"/api/admin/handoff/{session_id}/accept", payload={}, method="POST")
    reply = http_json(
        base,
        admin_token,
        f"/api/admin/handoff/{session_id}/reply",
        payload={"message": "我是人工客服，请先确认充电座指示灯。", "request_id": "refactor-reply-1"},
        method="POST",
    )
    duplicate = http_json(
        base,
        admin_token,
        f"/api/admin/handoff/{session_id}/reply",
        payload={"message": "我是人工客服，请先确认充电座指示灯。", "request_id": "refactor-reply-1"},
        method="POST",
    )
    messages = http_json(base, token, f"/api/sessions/{session_id}/handoff/messages?after=0")
    closed = http_json(
        base,
        admin_token,
        f"/api/admin/handoff/{session_id}/close",
        payload={"note": "refactor acceptance close", "request_id": "refactor-close-1"},
        method="POST",
    )
    final = http_json(base, token, f"/api/sessions/{session_id}/handoff")
    human_rows = [row for row in messages.get("messages") or [] if row.get("role") == "human"]
    checks = {
        "request_created": requested.get("ticket", {}).get("status") in {"waiting", "active"},
        "admin_list_contains_session": bool(listed),
        "active_ticket_queues_user_message": (first_event(queued, "handoff") is not None),
        "accepted": accepted.get("ticket", {}).get("status") == "active",
        "reply_delivered": bool(human_rows) and human_rows[0].get("content") == "我是人工客服，请先确认充电座指示灯。",
        "reply_idempotent": duplicate.get("duplicate") is True,
        "closed": closed.get("ticket", {}).get("status") == "closed",
        "closed_after_read": final.get("ticket", {}).get("status") == "closed",
    }
    result = {
        "session_id": session_id,
        "checks": checks,
        "passed": all(checks.values()),
        "queued_event_types": [event.get("type") for event in queued],
        "human_message_count": len(human_rows),
        "note": "real HTTP handoff lifecycle; no model call and no external ticket",
    }
    write_json(out_dir / "handoff.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    artifact = args.artifact.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()
    env_path = args.env_file.expanduser().resolve()
    if not (artifact / "serve.py").is_file():
        raise SystemExit(f"artifact is missing serve.py: {artifact}")
    if not env_path.is_file():
        raise SystemExit("authorized environment_file is missing")
    if port_open(PORT):
        raise SystemExit(f"port {PORT} is already in use")
    out_dir.mkdir(parents=True, exist_ok=True)
    storage = out_dir / "storage"
    if storage.exists():
        raise SystemExit(f"isolated storage already exists; refusing to overwrite evidence: {storage}")

    manifest = read_json(artifact / "manifest.json")
    artifact_id = manifest.get("artifact_id")
    instance_env = out_dir / "instance.env"
    tokens = write_instance_env(instance_env, env_path)
    access_token = tokens["RVS_ACCESS_TOKEN"]
    admin_token = tokens["RVS_ADMIN_TOKEN"]
    instance = {
        "schema_version": 1,
        "instance_id": "robot-vacuum-runtime-refactor-18082",
        "artifact_id": artifact_id,
        "artifact_root": str(artifact),
        "name": "robot-vacuum runtime refactor 18082",
        "scenario": "robot_vacuum",
        "version": 4,
        "workflow": "agent",
        "host": "127.0.0.1",
        "port": PORT,
        "storage_root": str(storage),
        "environment_file": str(instance_env),
        "asset_bindings": {},
    }
    instance_path = out_dir / "instance.json"
    write_json(instance_path, instance)
    public_instance = {key: value for key, value in instance.items() if key != "environment_file"}

    docker = docker_state()
    freeze = {
        "schema_version": "runtime-refactor-freeze/v1",
        "frozen_at": now_iso(),
        "artifact_id": artifact_id,
        "artifact_root": str(artifact),
        "artifact_manifest_sha256": sha256_file(artifact / "manifest.json"),
        "cases_sha256": sha256_file(SUITE_DIR / "cases.json"),
        "suite_sha256": sha256_file(SUITE_DIR / "suite.json"),
        "port": PORT,
        "existing_18080_open_before": port_open(EXISTING_PORT),
        "storage_root": str(storage),
        "instance_public": public_instance,
        "environment_file_used": env_path.name,
        "environment_values_read_or_logged": False,
        "docker_engine_at_start": docker,
        "no_docker_desktop_or_daemon_start": True,
        "new_extra_user_turns": 2,
        "frozen_12case18turn": True,
        "concurrency": 2,
    }
    write_json(out_dir / "freeze.json", freeze)

    server = LiveServer(
        python=sys.executable,
        serve_py=artifact / "serve.py",
        instance_path=instance_path,
        host="127.0.0.1",
        port=PORT,
        log_path=out_dir / "server.log",
        cwd=artifact,
    )
    result: dict[str, Any] = {
        "schema_version": SCHEMA,
        "generated_at": now_iso(),
        "status": "running",
        "artifact": {
            "artifact_id": artifact_id,
            "manifest_sha256": sha256_file(artifact / "manifest.json"),
            "run_check": "run.py --check ok",
            "content_hash": manifest.get("content_hash"),
        },
        "isolation": {
            "port": PORT,
            "storage_root": str(storage),
            "existing_18080_untouched": True,
            "docker": docker,
        },
        "freeze": freeze,
    }
    started = time.time()
    try:
        health = server.start(timeout=240)
        instance_info = http_json(server.base_url, access_token, "/api/instance")
        result["health"] = health
        result["instance"] = instance_info
        result["twelve_case"] = run_12_cases(server.base_url, access_token, out_dir)
        result["checkpoint_restart"] = checkpoint_restart_check(server.base_url, access_token, server, out_dir)
        result["error_path"] = typed_error_check(server.base_url, access_token, storage, out_dir, artifact)
        result["handoff"] = handoff_check(server.base_url, access_token, admin_token, out_dir)
        result["status"] = (
            "completed"
            if result["twelve_case"].get("sessions_run") == 12
            and result["checkpoint_restart"].get("passed")
            and result["error_path"].get("passed")
            and result["handoff"].get("passed")
            else "completed_with_findings"
        )
    finally:
        server.stop()
        result["cleanup"] = {
            "server_stopped": not port_open(PORT),
            "port_18082_open_after": port_open(PORT),
            "existing_18080_untouched": port_open(EXISTING_PORT) == freeze["existing_18080_open_before"],
            "storage_preserved": storage.exists(),
            "docker_started_by_us": False,
        }
        result["finished_at"] = now_iso()
        result["duration_seconds"] = round(time.time() - started, 2)
        result["missing"] = []
        if not docker.get("available"):
            result["missing"].append(
                "Docker engine was unavailable and was not started; no new image build or Docker compose run. "
                "The same artifact was exercised through its normal HTTP/SSE entry on isolated port 18082."
            )
        write_json(out_dir / "report.json", result)

    print(json.dumps({
        "status": result.get("status"),
        "artifact_id": artifact_id,
        "twelve_case_status": result.get("twelve_case", {}).get("status"),
        "sessions_run": result.get("twelve_case", {}).get("sessions_run"),
        "turns_run": result.get("twelve_case", {}).get("turns_run"),
        "checkpoint_restart_passed": result.get("checkpoint_restart", {}).get("passed"),
        "error_path_passed": result.get("error_path", {}).get("passed"),
        "handoff_passed": result.get("handoff", {}).get("passed"),
        "docker_available": docker.get("available"),
        "report": str(out_dir / "report.json"),
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "completed" else 5


if __name__ == "__main__":
    raise SystemExit(main())
