# -*- coding: utf-8 -*-
"""Fixed release-v1 product service helper.

Small stdlib-only wrapper for the single fixed instance
``outputs/release-v1/instance-8902.json``. It never rebuilds or edits the
frozen artifact and never takes an arbitrary config path.

Usage:
  python scripts/manage_product.py status [--json]
  python scripts/manage_product.py start
  python scripts/manage_product.py stop
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "outputs" / "release-v1"
CONFIG = RELEASE_DIR / "instance-8902.json"
SERVICE_DIR = RELEASE_DIR / "service"
PID_FILE = SERVICE_DIR / "service.json"
LOG_FILE = SERVICE_DIR / "service.log"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
READY_TIMEOUT_SECONDS = 90
STOP_TIMEOUT_SECONDS = 12


class HelperError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HelperError(f"缺少文件 {path}") from exc
    except json.JSONDecodeError as exc:
        raise HelperError(f"JSON 无法解析 {path}: {exc}") from exc


def _norm_path(value: str) -> str:
    return str(value).replace("/", "\\").rstrip("\\").lower()


def _load_config() -> dict:
    config_path = CONFIG.resolve()
    release_root = RELEASE_DIR.resolve()
    if not config_path.is_relative_to(release_root):
        raise HelperError("实例配置必须位于 outputs/release-v1 下")
    config = _read_json(config_path)
    required = ("instance_id", "artifact_id", "artifact_root", "host", "port")
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise HelperError("实例配置缺少字段: " + ", ".join(missing))
    artifact_root = Path(str(config["artifact_root"])).resolve()
    if not artifact_root.is_dir() or not (artifact_root / "serve.py").is_file():
        raise HelperError(f"产物入口不存在: {artifact_root / 'serve.py'}")
    if str(config["host"]) not in {"127.0.0.1", "localhost"}:
        raise HelperError("固定实例只允许本机地址")
    try:
        port = int(config["port"])
    except (TypeError, ValueError) as exc:
        raise HelperError("实例端口不是整数") from exc
    if not 1 <= port <= 65535:
        raise HelperError("实例端口超出范围")
    config["_config_path"] = str(config_path)
    config["_artifact_root"] = str(artifact_root)
    return config


def _expected_identity(config: dict) -> dict:
    return {
        "instance_id": config["instance_id"],
        "artifact_id": config["artifact_id"],
    }


def _api(config: dict, path: str, timeout: float = 4.0) -> dict | None:
    url = f"http://{config['host']}:{int(config['port'])}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _probe_instance(config: dict) -> dict | None:
    payload = _api(config, "/api/instance")
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    return payload


def _probe_health(config: dict) -> dict | None:
    payload = _api(config, "/api/health")
    return payload if isinstance(payload, dict) else None


def _identity_ok(instance: dict | None, config: dict) -> bool:
    if not instance:
        return False
    expected = _expected_identity(config)
    return all(instance.get(key) == value for key, value in expected.items())


def _pid_on_port(port: int) -> int | None:
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    pattern = re.compile(
        rf"^\s*TCP\s+\S+:{port}\s+\S+\s+LISTENING\s+(\d+)\s*$", re.IGNORECASE
    )
    for line in (proc.stdout or "").splitlines():
        match = pattern.match(line)
        if match:
            return int(match.group(1))
    return None


def _process_command(pid: int) -> str:
    script = (
        "(Get-CimInstance Win32_Process -Filter "
        f"'ProcessId={int(pid)}').CommandLine"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()


def _command_matches(command: str, config: dict) -> bool:
    if not command:
        return False
    normalized = _norm_path(command)
    serve_py = _norm_path(str(Path(config["_artifact_root"]) / "serve.py"))
    config_path = _norm_path(config["_config_path"])
    return serve_py in normalized and config_path in normalized and "--instance-config" in normalized


def _running_state(config: dict) -> dict:
    instance = _probe_instance(config)
    health = _probe_health(config) if instance else None
    pid = _pid_on_port(int(config["port"]))
    command = _process_command(pid) if pid else ""
    return {
        "api_ok": instance is not None,
        "port_listening": pid is not None,
        "running": instance is not None or pid is not None,
        "pid": pid,
        "command": command,
        "identity_ok": _identity_ok(instance, config),
        "command_ok": _command_matches(command, config),
        "instance": instance,
        "health": health,
    }


def _write_pid(pid: int, config: dict, extra: dict | None = None) -> None:
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "pid": int(pid),
        "written_at": _now(),
        "instance_id": config["instance_id"],
        "artifact_id": config["artifact_id"],
        "config": config["_config_path"],
        "artifact_root": config["_artifact_root"],
        "host": config["host"],
        "port": int(config["port"]),
        "log": str(LOG_FILE),
        "interpreter": sys.executable,
    }
    if extra:
        payload.update(extra)
    temporary = PID_FILE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(PID_FILE)


def _remove_pid(pid: int | None) -> None:
    if not PID_FILE.is_file():
        return
    try:
        recorded = _read_json(PID_FILE)
    except HelperError:
        return
    if pid is None or int(recorded.get("pid", -1)) == int(pid):
        PID_FILE.unlink(missing_ok=True)


def _log_tail(lines: int = 12) -> str:
    if not LOG_FILE.is_file():
        return ""
    content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _kill_tree(pid: int, force: bool) -> subprocess.CompletedProcess:
    command = ["taskkill", "/PID", str(int(pid)), "/T"]
    if force:
        command.append("/F")
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=CREATE_NO_WINDOW,
    )


def _wait_stopped(config: dict, pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pid_on_port(int(config["port"])) is None and _probe_instance(config) is None:
            return True
        time.sleep(0.5)
    return False


def _status_payload(config: dict, state: dict) -> dict:
    return {
        "config": config["_config_path"],
        "instance_id": config["instance_id"],
        "artifact_id": config["artifact_id"],
        "artifact_root": config["_artifact_root"],
        "host": config["host"],
        "port": int(config["port"]),
        "url": f"http://{config['host']}:{int(config['port'])}/",
        "running": bool(state["running"]),
        "api_ok": bool(state["api_ok"]),
        "port_listening": bool(state["port_listening"]),
        "pid": state["pid"],
        "identity_ok": bool(state["identity_ok"]),
        "command_ok": bool(state["command_ok"]),
        "ready": bool((state.get("health") or {}).get("ready")),
        "needs_configuration": list(
            (state.get("health") or {}).get("needs_configuration") or []
        ),
        "health_status": (state.get("health") or {}).get("status"),
    }


def _print_status(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"实例 {payload['instance_id']} · {payload['artifact_id']}")
    print(f"地址 {payload['url']}")
    print(f"状态 {'运行中' if payload['running'] else '已停止'}")
    print(f"身份 {'匹配' if payload['identity_ok'] else '不匹配或不可读'}")
    print(f"进程 PID {payload['pid'] if payload['pid'] else '无'}")
    print(f"命令 {'匹配' if payload['command_ok'] else '不匹配或不可读'}")
    print(f"就绪 {'是' if payload['ready'] else '否'}")
    if payload["needs_configuration"]:
        print("待配置 " + ", ".join(payload["needs_configuration"]))
    print(f"配置 {payload['config']}")


def command_status(as_json: bool = False) -> int:
    config = _load_config()
    state = _running_state(config)
    payload = _status_payload(config, state)
    payload["checked_at"] = _now()
    _print_status(payload, as_json)
    return 0 if (not payload["running"] or payload["identity_ok"]) else 2


def command_start() -> int:
    config = _load_config()
    state = _running_state(config)
    if state["running"]:
        if state["identity_ok"] and state["command_ok"] and state["pid"]:
            SERVICE_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("ab") as log_handle:
                log_handle.write(
                    (
                        f"[{_now()}] adopt {config['instance_id']} "
                        f"pid={state['pid']} no_restart\n"
                    ).encode("utf-8")
                )
            _write_pid(state["pid"], config, {"adopted": True, "adopted_at": _now()})
            print(f"已在运行，未重启。PID {state['pid']}")
            print(f"地址 http://{config['host']}:{int(config['port'])}/")
            return 0
        raise HelperError(
            "端口已被其他进程占用或无法验证身份与命令，拒绝启动，也不停止该进程"
        )

    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    artifact_root = Path(config["_artifact_root"])
    serve_py = artifact_root / "serve.py"
    environment = os.environ.copy()
    with LOG_FILE.open("ab") as log_handle:
        log_handle.write(
            (
                f"\n[{_now()}] start {config['instance_id']} "
                f"port={config['port']} interpreter={sys.executable}\n"
            ).encode("utf-8")
        )
        log_handle.flush()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(serve_py),
                    "--instance-config",
                    config["_config_path"],
                ],
                cwd=str(artifact_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
                close_fds=True,
            )
        except OSError as exc:
            raise HelperError(f"启动失败: {exc}") from exc
    _write_pid(process.pid, config, {"adopted": False, "started_at": _now()})
    print(f"已启动 PID {process.pid}，等待就绪…")

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_state: dict = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HelperError(
                f"进程提前退出，退出码 {process.returncode}\n{_log_tail()}"
            )
        last_state = _running_state(config)
        if (
            last_state["identity_ok"]
            and last_state["command_ok"]
            and (last_state.get("health") or {}).get("ready") is True
        ):
            _write_pid(
                process.pid,
                config,
                {
                    "adopted": False,
                    "started_at": _now(),
                    "ready_at": _now(),
                },
            )
            print(f"已就绪。PID {process.pid}")
            print(f"地址 http://{config['host']}:{int(config['port'])}/")
            return 0
        time.sleep(1.0)

    try:
        _kill_tree(process.pid, force=True)
    except (OSError, subprocess.SubprocessError):
        pass
    _remove_pid(process.pid)
    detail = ""
    if last_state:
        detail = (
            f"身份 {last_state['identity_ok']} 命令 {last_state['command_ok']} "
            f"就绪 {(last_state.get('health') or {}).get('ready')}"
        )
    raise HelperError(
        f"等待就绪超时（{READY_TIMEOUT_SECONDS}s）。{detail}\n{_log_tail()}"
    )


def command_stop() -> int:
    config = _load_config()
    state = _running_state(config)
    if not state["running"]:
        _remove_pid(None)
        print("已停止，无需操作")
        return 0
    if not state["api_ok"] or not state["identity_ok"]:
        raise HelperError("实例身份不匹配或不可读，拒绝停止，未向进程发送终止信号")
    if not state["pid"]:
        raise HelperError("找不到监听端口的进程，拒绝停止")
    if not state["command_ok"]:
        raise HelperError("进程命令与固定产物/配置不匹配，拒绝停止")
    pid = int(state["pid"])
    print(f"停止 PID {pid} 实例 {config['instance_id']}")
    result = _kill_tree(pid, force=False)
    stopped = _wait_stopped(config, pid, STOP_TIMEOUT_SECONDS)
    forced = False
    if not stopped:
        forced = True
        result = _kill_tree(pid, force=True)
        stopped = _wait_stopped(config, pid, STOP_TIMEOUT_SECONDS)
    if not stopped:
        raise HelperError(f"停止失败，请人工检查 PID {pid}: {result.stderr.strip()}")
    _remove_pid(pid)
    print("已停止" + ("，使用了强制终止" if forced else "，端口已释放"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定 release v1 产品服务助手")
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--json", action="store_true", help="status 输出 JSON")
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            return command_status(as_json=args.json)
        if args.action == "start":
            return command_start()
        return command_stop()
    except HelperError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
