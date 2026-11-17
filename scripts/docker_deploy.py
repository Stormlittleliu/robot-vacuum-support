# -*- coding: utf-8 -*-
"""Minimal Docker deployment preflight/smoke helper (stdlib only).

The deployment stays deliberately small: one backend, one nginx frontend, and
one shared access token per instance.  Secrets are read only from the explicit
``--env-file`` (default ``.env.docker``), which is gitignored and
dockerignored.  This script never prints token values.

Examples:
  python scripts/docker_deploy.py preflight --env-file .env.docker
  python scripts/docker_deploy.py up --env-file .env.docker --project rvssmoke --public-port 18080
  python scripts/docker_deploy.py smoke --env-file .env.docker --base-url http://127.0.0.1:18080
  python scripts/docker_deploy.py down --env-file .env.docker --project rvssmoke
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / ".env.docker"
ASSETS = {
    "knowledge": ROOT / "assets" / "knowledge",
    "models": ROOT / "assets" / "models" / "bge-small-zh-v1.5",
    "rag_index": ROOT / "assets" / "rag_index",
}
REQUIRED_KEYS = ("RVS_ADMIN_TOKEN", "RVS_ACCESS_TOKEN")
PLACEHOLDERS = {
    "changeme",
    "change-me",
    "placeholder",
    "replace-me",
    "dummy",
    "test",
    "token",
}


def read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE dotenv lines without echoing values."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _run(command: list[str], *, timeout: int = 30, env: dict[str, str] | None = None):
    return subprocess.run(
        command,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _compose(project: str, env_file: Path, *args: str, env: dict[str, str] | None = None):
    command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(env_file),
        *args,
    ]
    return _run(command, timeout=300, env=env)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _check_assets() -> dict[str, bool]:
    knowledge = ASSETS["knowledge"]
    models = ASSETS["models"]
    index = ASSETS["rag_index"]
    knowledge_ok = knowledge.is_dir() and any(
        child.is_file() and child.suffix.lower() in {".txt", ".md", ".pdf"}
        for child in knowledge.rglob("*")
    )
    model_ok = models.is_dir() and (models / "config.json").is_file() and any(
        (models / name).is_file()
        for name in ("model.safetensors", "pytorch_model.bin")
    )
    index_ok = index.is_dir() and (index / "chroma.sqlite3").is_file() and (
        (index / "chroma.sqlite3").stat().st_size > 0
    )
    return {"knowledge": knowledge_ok, "models": model_ok, "rag_index": index_ok}


def _check_ignore_files() -> dict[str, bool]:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return {
        "gitignore_env": any(line.strip() in {".env", ".env.*"} for line in gitignore),
        "dockerignore_env": any(
            line.strip() in {".env", ".env.*"} for line in dockerignore
        ),
    }


def preflight(env_file: Path, *, project: str, port: int) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    if not env_file.is_file():
        return {"ok": False, "error": f"env file not found: {env_file}", "checks": checks}
    values = read_env_file(env_file)
    secrets = [values.get(key, "") for key in REQUIRED_KEYS]
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    weak = [
        key
        for key in REQUIRED_KEYS
        if values.get(key) and (
            len(values[key]) < 24 or values[key].lower() in PLACEHOLDERS
        )
    ]
    duplicate = bool(values.get(REQUIRED_KEYS[0])) and values.get(
        REQUIRED_KEYS[0]
    ) == values.get(REQUIRED_KEYS[1])
    checks["tokens_present"] = not missing
    checks["tokens_distinct"] = not duplicate
    checks["tokens_not_placeholders"] = not weak
    if os.name != "nt":
        mode = env_file.stat().st_mode & 0o777
        checks["env_file_private"] = not bool(mode & 0o077)
    else:
        checks["env_file_private"] = True

    ignores = _check_ignore_files()
    checks.update(ignores)
    assets = _check_assets()
    checks.update({f"asset_{key}": value for key, value in assets.items()})
    checks["port_free"] = _port_free(port)

    docker = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
    checks["docker_daemon"] = docker.returncode == 0
    if docker.returncode == 0:
        probe_env = os.environ.copy()
        probe_env["PUBLIC_PORT"] = str(port)
        config = _compose(
            project, env_file, "config", "--quiet", env=probe_env
        )
        checks["compose_config"] = config.returncode == 0
        if config.returncode != 0:
            checks["compose_error"] = _scrub(
                (config.stderr or "").strip()[:400], secrets
            )
    else:
        checks["compose_config"] = False

    failed = [name for name, value in checks.items() if value is False]
    return {
        "ok": not failed,
        "env_file": str(env_file),
        "project": project,
        "port": port,
        "checks": checks,
        "failed": failed,
    }


def _http(
    url: str,
    *,
    method: str = "GET",
    token: str = "",
    body: dict[str, Any] | None = None,
    accept: str = "application/json",
    timeout: float = 30,
) -> tuple[int, dict[str, str], str]:
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            {key.lower(): value for key, value in exc.headers.items()},
            exc.read().decode("utf-8", errors="replace"),
        )


def _wait_for(url: str, *, timeout: float = 120.0) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            status, _, body = _http(url, timeout=5)
            last = f"HTTP {status}"
            if status == 200:
                return True, last
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = type(exc).__name__
        time.sleep(2)
    return False, last


def smoke(env_file: Path, *, project: str, base_url: str) -> dict[str, Any]:
    values = read_env_file(env_file)
    admin = values.get("RVS_ADMIN_TOKEN", "")
    access = values.get("RVS_ACCESS_TOKEN", "")
    secrets = [admin, access]
    checks: dict[str, Any] = {}
    if not admin or not access:
        return {"ok": False, "error": "tokens missing from env file", "checks": checks}

    up, detail = _wait_for(base_url.rstrip("/") + "/healthz")
    checks["frontend_healthz"] = up
    if not up:
        return {"ok": False, "error": f"frontend unreachable: {detail}", "checks": checks}

    status, _, body = _http(base_url + "/api/health/live")
    checks["backend_live"] = status == 200 and '"alive"' in body
    status, _, body = _http(base_url + "/api/health/ready")
    checks["backend_ready"] = status == 200
    if status != 200:
        try:
            payload = json.loads(body)
            checks["ready_status"] = payload.get("status")
            checks["ready_missing"] = payload.get("missing")
            checks["ready_needs_configuration"] = payload.get("needs_configuration")
        except ValueError:
            pass

    status, _, _ = _http(base_url + "/api/sessions")
    checks["sessions_unauthorized"] = status == 401
    status, _, _ = _http(base_url + "/api/sessions", token=access)
    checks["sessions_authorized"] = status == 200

    status, _, body = _http(
        base_url + "/api/sessions", method="POST", token=access, body={}
    )
    checks["session_create"] = status in {200, 201}
    session_id = ""
    if checks["session_create"]:
        try:
            session_id = str(json.loads(body).get("session_id") or "")
        except ValueError:
            pass
    checks["session_id_present"] = bool(session_id)
    if session_id:
        status, _, _ = _http(
            f"{base_url}/api/sessions/{session_id}/handoff/request",
            method="POST",
            token=access,
            body={"request_id": f"deploy-smoke-{session_id}"},
        )
        checks["handoff_request"] = status == 200
        # With a waiting ticket, a user message is queued and answered by the
        # handoff SSE stream without calling the model.
        status, headers, body = _http(
            f"{base_url}/api/sessions/{session_id}/messages",
            method="POST",
            token=access,
            body={"prompt": "deploy smoke, do not call the model"},
            accept="text/event-stream",
            timeout=60,
        )
        checks["handoff_sse"] = (
            status == 200
            and "text/event-stream" in headers.get("content-type", "")
            and '"type": "handoff"' in body
            and '"type": "done"' in body
        )

    status, _, _ = _http(base_url + "/api/admin/handoff", token=admin)
    checks["admin_via_proxy"] = status == 200
    status, _, _ = _http(base_url + "/api/admin/handoff", token=access)
    checks["admin_rejects_user_token"] = status == 401
    status, _, _ = _http(base_url + "/admin.html")
    checks["admin_page"] = status == 200

    logs = _run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(env_file),
            "logs",
            "--no-color",
            "--tail",
            "500",
            "backend",
            "frontend",
        ],
        timeout=60,
    )
    log_text = (logs.stdout or "") + (logs.stderr or "")
    checks["logs_available"] = logs.returncode == 0
    checks["logs_no_token"] = logs.returncode != 0 or not any(
        secret and secret in log_text for secret in secrets
    )
    checks["logs_no_provider_value"] = logs.returncode != 0 or not any(
        value and value in log_text
        for key, value in values.items()
        if key.endswith("_API_KEY")
    )

    failed = [name for name, value in checks.items() if value is False]
    return {
        "ok": not failed,
        "base_url": base_url,
        "project": project,
        "checks": checks,
        "failed": failed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Docker deploy preflight/smoke helper")
    parser.add_argument(
        "command", choices=("preflight", "up", "down", "smoke"), help="action"
    )
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--project", default="robot-vacuum-support")
    parser.add_argument("--public-port", type=int, default=8080)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    env_file = Path(args.env_file).expanduser().resolve()
    if args.command in {"preflight", "up"}:
        result = preflight(env_file, project=args.project, port=args.public_port)
        if not args.skip_preflight or args.command == "preflight":
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result["ok"]:
                return 1
        if args.command == "preflight":
            return 0

    if args.command == "up":
        env = os.environ.copy()
        env["PUBLIC_PORT"] = str(args.public_port)
        outcome = _compose(
            args.project,
            env_file,
            "up",
            "-d",
            "--build",
            env=env,
        )
        if outcome.returncode != 0:
            print(
                "compose up failed; see docker output above (no secrets echoed)",
                file=sys.stderr,
            )
            return outcome.returncode
        print(json.dumps({"ok": True, "action": "up", "project": args.project}))
        return 0

    if args.command == "down":
        # Never pass -v: the named data volume is the persistent session store.
        outcome = _compose(args.project, env_file, "down")
        if outcome.returncode != 0:
            print("compose down failed", file=sys.stderr)
            return outcome.returncode
        print(json.dumps({"ok": True, "action": "down", "project": args.project}))
        return 0

    base_url = args.base_url or f"http://127.0.0.1:{args.public_port}"
    result = smoke(env_file, project=args.project, base_url=base_url)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
