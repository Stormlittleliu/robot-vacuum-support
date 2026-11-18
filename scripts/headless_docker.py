# -*- coding: utf-8 -*-
"""Headless WSL Docker helper for one isolated compose project.

This script only talks to an already-running Docker Engine inside one WSL
distro.  It never starts/stops Docker Desktop or the daemon, never changes the
global PATH/DOCKER_HOST/docker context, never edits WSL defaults, and never
prunes images/volumes/containers.

Every Docker call runs inside the pinned distro with an explicit
``--project-name``, ``--env-file`` and image/volume values loaded from that env
file.  Tokens and provider keys are read from the env file but are never
printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DISTRO = os.environ.get("RVS_WSL_DISTRO", "DockerEngine")
DEFAULT_PROJECT = os.environ.get("RVS_PROJECT", "rvs-p1")
DEFAULT_ENV = ROOT / "outputs" / "p1-headless-migration-v1" / "runtime.env"
COMPOSE_FILES = ("docker-compose.yml", "deploy/compose.headless.yml")
REQUIRED_KEYS = (
    "PUBLIC_PORT",
    "RVS_BACKEND_IMAGE",
    "RVS_DATA_VOLUME",
    "RVS_ASSETS_HOST_ROOT",
    "RVS_INSTANCE_ID",
    "RVS_ADMIN_TOKEN",
    "RVS_ACCESS_TOKEN",
)


# ----------------------------------------------------------------- utilities
def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def wsl_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive
    if len(drive) == 2 and drive[1] == ":":
        rest = str(resolved)[3:].replace("\\", "/")
        return "/mnt/{}/{}".format(drive[0].lower(), rest)
    return str(resolved).replace("\\", "/")


def clean_env() -> Dict[str, str]:
    env = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
        env.pop(key, None)
    return env


def wsl_run(
    distro: str,
    cwd: str,
    argv: List[str],
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    command = ["wsl.exe", "-d", distro, "--cd", cwd, "--"] + list(argv)
    return subprocess.run(
        command,
        cwd=str(ROOT),
        env=clean_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def compose(
    distro: str,
    project: str,
    env_file: Path,
    args: List[str],
    *,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        wsl_path(env_file),
    ]
    for name in COMPOSE_FILES:
        command.extend(["-f", wsl_path(ROOT / name)])
    command.extend(args)
    return wsl_run(distro, wsl_path(ROOT), command, timeout=timeout)


def docker(distro: str, args: List[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return wsl_run(distro, wsl_path(ROOT), ["docker"] + args, timeout=timeout)


def _fail(message: str, result: subprocess.CompletedProcess) -> None:
    print(message, file=sys.stderr)
    tail = ((result.stderr or "") + (result.stdout or "")).strip()
    if tail:
        print(tail[-2000:], file=sys.stderr)


def _git_short() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return ""


def _port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _registry_base_path(distro: str) -> str:
    """Read the WSL distro BasePath from HKCU read-only (no mutation)."""
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as root:
            index = 0
            while True:
                try:
                    child = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, child) as entry:
                        name = str(winreg.QueryValueEx(entry, "DistributionName")[0])
                        if name == distro:
                            return str(winreg.QueryValueEx(entry, "BasePath")[0])
                except OSError:
                    continue
    except OSError:
        return ""
    return ""


def _json_docker_info(distro: str) -> Dict[str, Any]:
    result = docker(distro, ["info", "--format", "{{json .}}"], timeout=30)
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {}


# -------------------------------------------------------------------- checks
def command_check(args: argparse.Namespace) -> int:
    info = _json_docker_info(args.distro)
    if not info:
        print(json.dumps({"ok": False, "error": "docker info failed via distro"}, indent=2))
        return 1
    base = _registry_base_path(args.distro)
    vhd = ""
    if base:
        candidate = Path(base.replace("\\\\?\\", "")) / "ext4.vhdx"
        vhd = str(candidate)
    context = docker(args.distro, ["context", "show"], timeout=20)
    env_probe = wsl_run(
        args.distro,
        "/",
        ["sh", "-lc", "printenv DOCKER_HOST || true; ss -ltn 2>/dev/null | grep -E ':(2375|2376)\\b' || true"],
        timeout=20,
    )
    payload = {
        "ok": info.get("ServerVersion") is not None,
        "distro": args.distro,
        "distro_base_path": base,
        "vhd": vhd,
        "vhd_exists": bool(vhd) and Path(vhd).is_file(),
        "vhd_bytes": Path(vhd).stat().st_size if vhd and Path(vhd).is_file() else None,
        "server_version": info.get("ServerVersion"),
        "storage_driver": info.get("Driver"),
        "docker_root_dir": info.get("DockerRootDir"),
        "containers": info.get("Containers"),
        "images": info.get("Images"),
        "context": (context.stdout or "").strip(),
        "docker_host_probe": (env_probe.stdout or "").strip(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def command_preflight(args: argparse.Namespace) -> int:
    checks: Dict[str, Any] = {}
    failures: List[str] = []
    env_file = Path(args.env_file).expanduser().resolve()
    checks["env_file_exists"] = env_file.is_file()
    if not checks["env_file_exists"]:
        print(json.dumps({"ok": False, "failed": ["env_file_exists"]}, indent=2))
        return 1
    values = read_env_file(env_file)
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    checks["required_keys"] = not missing
    if missing:
        failures.append("required_keys")
    admin = values.get("RVS_ADMIN_TOKEN", "")
    access = values.get("RVS_ACCESS_TOKEN", "")
    checks["tokens_distinct"] = bool(admin) and bool(access) and admin != access
    checks["tokens_strong"] = min(len(admin), len(access)) >= 24
    if not checks["tokens_distinct"]:
        failures.append("tokens_distinct")
    if not checks["tokens_strong"]:
        failures.append("tokens_strong")

    image = values.get("RVS_BACKEND_IMAGE", "")
    volume = values.get("RVS_DATA_VOLUME", "")
    assets = values.get("RVS_ASSETS_HOST_ROOT", "")
    port = int(values.get("PUBLIC_PORT", "0") or "0")
    checks["image_explicit"] = bool(image) and not image.endswith(":smoke")
    checks["image_project_scoped"] = image.startswith(args.project + "/")
    checks["image_commit_tag"] = bool(_git_short()) and image.endswith(":" + _git_short())
    checks["volume_project_scoped"] = volume == args.project + "_rvs-data"
    checks["assets_linux_absolute"] = assets.startswith("/") and ":" not in assets and "\\" not in assets
    checks["port_valid"] = 0 < port < 65536
    for key in ("image_explicit", "image_project_scoped", "volume_project_scoped", "assets_linux_absolute", "port_valid"):
        if not checks[key]:
            failures.append(key)

    probe = wsl_run(
        args.distro,
        "/",
        [
            "sh",
            "-c",
            "test -d {0}/knowledge && test -d {0}/models/bge-small-zh-v1.5 && test -d {0}/rag_index".format(assets),
        ],
        timeout=20,
    ) if checks["assets_linux_absolute"] else None
    checks["assets_present_in_wsl"] = bool(probe and probe.returncode == 0)
    if not checks["assets_present_in_wsl"]:
        failures.append("assets_present_in_wsl")

    checks["port_free"] = _port_free(port) if checks["port_valid"] else False
    if not checks["port_free"]:
        failures.append("port_free")

    info = _json_docker_info(args.distro)
    checks["docker_daemon"] = bool(info)
    checks["docker_root_dir"] = info.get("DockerRootDir")
    if not checks["docker_daemon"]:
        failures.append("docker_daemon")
    if info:
        cfg = compose(args.distro, args.project, env_file, ["config", "--quiet"], timeout=60)
        checks["compose_config"] = cfg.returncode == 0
        if cfg.returncode != 0:
            failures.append("compose_config")

    payload = {
        "ok": not failures,
        "project": args.project,
        "distro": args.distro,
        "env_file": str(env_file),
        "port": port,
        "image": image,
        "volume": volume,
        "assets_root": assets,
        "checks": checks,
        "failed": failures,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


# ------------------------------------------------------------------ lifecycle
def command_build(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser().resolve()
    values = read_env_file(env_file)
    image = values.get("RVS_BACKEND_IMAGE", "")
    if not image.startswith(args.project + "/") or image.endswith(":smoke"):
        print("refusing to build an image outside the project namespace", file=sys.stderr)
        return 2
    command = [
        "wsl.exe",
        "-d",
        args.distro,
        "--cd",
        wsl_path(ROOT),
        "--",
        "docker",
        "compose",
        "--project-name",
        args.project,
        "--env-file",
        wsl_path(env_file),
    ]
    for name in COMPOSE_FILES:
        command.extend(["-f", wsl_path(ROOT / name)])
    command.extend(["build", "--progress", "plain", "backend"])
    # Stream BuildKit/pip output instead of buffering it, so a caller can tail
    # the log while a large CPU-wheel download is in progress.
    result = subprocess.run(command, cwd=str(ROOT), env=clean_env(), timeout=args.timeout)
    if result.returncode != 0:
        print("compose build failed", file=sys.stderr)
        return result.returncode
    print(json.dumps({"ok": True, "action": "build", "image": image}, ensure_ascii=False))
    return 0


def command_up(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser().resolve()
    values = read_env_file(env_file)
    image = values.get("RVS_BACKEND_IMAGE", "")
    inspect = docker(args.distro, ["image", "inspect", image], timeout=30)
    if inspect.returncode != 0:
        print("image not built yet; refusing to pull an implicit tag: {}".format(image), file=sys.stderr)
        return 2
    result = compose(
        args.distro,
        args.project,
        env_file,
        ["up", "-d", "--no-build"],
        timeout=args.timeout,
    )
    if result.returncode != 0:
        _fail("compose up failed", result)
        return result.returncode
    print(json.dumps({"ok": True, "action": "up", "project": args.project, "image": image}))
    return 0


def command_restart(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser().resolve()
    result = compose(
        args.distro,
        args.project,
        env_file,
        ["restart", "backend"],
        timeout=max(args.timeout, 120),
    )
    if result.returncode != 0:
        _fail("compose restart failed", result)
        return result.returncode
    print(json.dumps({"ok": True, "action": "restart", "service": "backend"}))
    return 0


def command_down(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser().resolve()
    # Never pass -v: the named data volume is the persistent session store.
    result = compose(args.distro, args.project, env_file, ["down"], timeout=args.timeout)
    if result.returncode != 0:
        _fail("compose down failed", result)
        return result.returncode
    print(json.dumps({"ok": True, "action": "down", "project": args.project, "volume_preserved": True}))
    return 0


def command_status(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser().resolve()
    values = read_env_file(env_file)
    ps = compose(args.distro, args.project, env_file, ["ps", "--format", "json"], timeout=60)
    rows: List[Dict[str, Any]] = []
    for line in (ps.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    inspect = docker(
        args.distro,
        [
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project={}".format(args.project),
            "--format",
            "{{json .}}",
        ],
        timeout=30,
    )
    inspect_rows: List[Dict[str, Any]] = []
    for line in (inspect.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            inspect_rows.append(json.loads(line))
        except ValueError:
            continue
    volumes = docker(
        args.distro,
        [
            "volume",
            "ls",
            "--filter",
            "label=com.docker.compose.project={}".format(args.project),
            "--format",
            "{{.Name}}",
        ],
        timeout=30,
    )
    payload = {
        "ok": ps.returncode == 0,
        "project": args.project,
        "image": values.get("RVS_BACKEND_IMAGE"),
        "volume": values.get("RVS_DATA_VOLUME"),
        "compose_ps": rows,
        "containers": inspect_rows,
        "volumes": [line for line in (volumes.stdout or "").splitlines() if line.strip()],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def command_logs(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser().resolve()
    result = compose(
        args.distro,
        args.project,
        env_file,
        ["logs", "--no-color", "--tail", str(args.tail), "backend", "frontend"],
        timeout=120,
    )
    print((result.stdout or "") + (result.stderr or ""))
    return result.returncode


# ---------------------------------------------------------------------- HTTP
def _http(
    url: str,
    *,
    method: str = "GET",
    token: str = "",
    body: Optional[Dict[str, Any]] = None,
    accept: str = "application/json",
    timeout: float = 30,
) -> Any:
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, dict((k.lower(), v) for k, v in response.headers.items()), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, dict((k.lower(), v) for k, v in exc.headers.items()), raw


def _wait_ready(base_url: str, timeout: float = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, _ = _http(base_url.rstrip("/") + "/api/health/ready", timeout=5)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def _sse_events(raw: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except ValueError:
                pass
    return events


def command_smoke(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file).expanduser().resolve()
    values = read_env_file(env_file)
    admin = values.get("RVS_ADMIN_TOKEN", "")
    access = values.get("RVS_ACCESS_TOKEN", "")
    base = (args.base_url or "http://127.0.0.1:" + values.get("PUBLIC_PORT", "18080")).rstrip("/")
    checks: Dict[str, Any] = {}
    checks["ready"] = _wait_ready(base)
    status, _, body = _http(base + "/healthz")
    checks["frontend_healthz"] = status == 200
    status, _, body = _http(base + "/api/health")
    checks["health"] = status == 200
    if body:
        try:
            checks["health_status"] = json.loads(body).get("status")
        except ValueError:
            pass
    status, _, _ = _http(base + "/api/sessions")
    checks["sessions_unauthorized"] = status == 401
    status, _, _ = _http(base + "/api/sessions", token=access)
    checks["sessions_authorized"] = status == 200
    status, _, body = _http(base + "/api/sessions", method="POST", token=access, body={})
    checks["session_create"] = status in (200, 201)
    session_id = ""
    if checks["session_create"]:
        try:
            session_id = str(json.loads(body).get("session_id") or "")
        except ValueError:
            pass
    if session_id:
        status, _, _ = _http(
            "{}/api/sessions/{}/handoff/request".format(base, session_id),
            method="POST",
            token=access,
            body={"request_id": "headless-smoke-" + session_id},
        )
        checks["handoff_request"] = status == 200
        status, headers, body = _http(
            "{}/api/sessions/{}/messages".format(base, session_id),
            method="POST",
            token=access,
            body={"prompt": "HEADLESS smoke: do not call the model"},
            accept="text/event-stream",
            timeout=60,
        )
        events = _sse_events(body)
        checks["handoff_sse"] = (
            status == 200
            and "text/event-stream" in headers.get("content-type", "")
            and any(event.get("type") == "handoff" for event in events)
            and any(event.get("type") == "done" for event in events)
        )
    status, _, _ = _http(base + "/api/admin/handoff", token=admin)
    checks["admin_authorized"] = status == 200
    status, _, _ = _http(base + "/api/admin/handoff", token=access)
    checks["admin_rejects_user_token"] = status == 401
    status, _, _ = _http(base + "/admin.html")
    checks["admin_page"] = status == 200
    if session_id:
        _http(
            "{}/api/admin/handoff/{}/close".format(base, session_id),
            method="POST",
            token=admin,
            body={"note": "headless smoke cleanup", "request_id": "headless-smoke-close-" + session_id},
        )
    failed = [name for name, value in checks.items() if value is False]
    payload = {"ok": not failed, "base_url": base, "checks": checks, "failed": failed}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distro", default=DEFAULT_DISTRO)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--base-url", default="")
    parser.add_argument("--tail", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=7200)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "preflight", "build", "up", "restart", "down", "status", "smoke", "logs"):
        sub.add_parser(name)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "check": command_check,
        "preflight": command_preflight,
        "build": command_build,
        "up": command_up,
        "restart": command_restart,
        "down": command_down,
        "status": command_status,
        "smoke": command_smoke,
        "logs": command_logs,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
