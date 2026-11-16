# -*- coding: utf-8 -*-
"""One small real latency diagnosis: 1 cold + N-1 warm turns on one query.

What it does
------------
1. Builds a fresh artifact from the current source (old artifacts untouched).
2. Starts a hidden managed child server on 127.0.0.1:8945 by default.
3. Sends the same non-private fault/maintenance question ``--turns`` times in
   one session, so turn 1 is cold (new worker) and the rest are warm (worker
   reuse).  Default 3, matching the earlier evidence; use ``--turns 2`` for a
   smaller acceptance run without overwriting old outputs.
4. Records client, server and worker spans, provider transport records, and
   the existing trace events.
5. Stops the child and releases the port.

It does not run the 24-case evaluation, does not change prompt/routing/timeouts,
does not print or copy credentials, and does not use packet capture or WMI.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
BUILD_BASE = ROOT.parent / "robot-vacuum-support-built"
DEFAULT_ENV = ROOT.parent / "my_agent_harness" / ".env"
QUESTION = "扫地机器人回充失败，充电座指示灯不亮，应该先检查什么？"
PORT = 8945


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _http_json(url: str, payload: dict[str, Any] | None = None, *, timeout: float = 120) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if payload is not None else "GET"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _build_artifact() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "runtime"))
    from modules.artifacts.project import build_project

    result = build_project(ROOT, BUILD_BASE, include_assets=True)
    manifest = result.get("manifest") or {}
    return {
        **result,
        "artifact_id": manifest.get("artifact_id"),
        "artifact_root": result.get("bundle"),
        "version": manifest.get("version"),
        "source": manifest.get("source"),
        "runtime_hash": (manifest.get("runtime") or {}).get("hash"),
    }


def _load_artifact(artifact_root: Path) -> dict[str, Any]:
    """Use an already-built source artifact instead of creating another one."""
    root = artifact_root.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"artifact manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "artifact_id": manifest.get("artifact_id") or root.name,
        "artifact_root": str(root),
        "version": manifest.get("version"),
        "source": manifest.get("source"),
        "runtime_hash": (manifest.get("runtime") or {}).get("hash"),
        "reused_existing_build": True,
    }


def _write_instance(run_dir: Path, artifact: dict[str, Any], env_file: Path) -> Path:
    payload = {
        "schema_version": 1,
        "instance_id": f"robot-vacuum-latency-diagnosis-{run_dir.name}",
        "artifact_id": artifact["artifact_id"],
        "artifact_root": str(artifact["artifact_root"]),
        "name": "robot-vacuum latency diagnosis",
        "scenario": "robot_vacuum",
        "version": int(artifact.get("version") or 0),
        "workflow": "agent",
        "host": "127.0.0.1",
        "port": PORT,
        "storage_root": str(run_dir / "instance-storage"),
        "environment_file": str(env_file),
        "asset_bindings": {},
    }
    path = run_dir / "instance.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class Server:
    def __init__(
        self,
        artifact: dict[str, Any],
        instance: Path,
        log: Path,
        spans_dir: Path,
        provider_records_dir: Path,
    ):
        self.artifact = artifact
        self.instance = instance
        self.log = log
        self.spans_dir = spans_dir
        self.provider_records_dir = provider_records_dir
        self.process: subprocess.Popen | None = None
        self._log_handle = None

    def _flags(self) -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def start(self, *, timeout: float = 180.0) -> dict[str, Any]:
        if _port_in_use("127.0.0.1", PORT):
            raise RuntimeError(f"port {PORT} is already in use")
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log.open("a", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "RVS_LATENCY_TRACE": "1",
                "RVS_PROVIDER_TRACE": "1",
                "RVS_LATENCY_DIR": str(self.spans_dir),
                "RVS_LATENCY_PROCESS": "server",
                "RVS_PROVIDER_TRANSPORT_DIR": str(self.provider_records_dir),
                "PYTHONIOENCODING": "utf-8",
                "PYTHON_DOTENV_DISABLED": "1",
            }
        )
        env.pop("PYTHONPATH", None)
        serve = Path(self.artifact["artifact_root"]) / "serve.py"
        self.process = subprocess.Popen(
            [sys.executable, str(serve), "--instance-config", str(self.instance)],
            cwd=str(self.artifact["artifact_root"]),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=self._log_handle,
            creationflags=self._flags(),
        )
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"server exited with {self.process.returncode}; see {self.log}")
            try:
                last = _http_json("http://127.0.0.1:%d/api/health" % PORT, timeout=3)
                if last.get("ready"):
                    return last
            except Exception as exc:  # noqa: BLE001
                last = {"error": type(exc).__name__}
            time.sleep(1)
        raise RuntimeError(f"server not ready in {timeout:.0f}s: {last}")

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=self._flags(),
                    timeout=20,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None


def _probe_provider_host() -> dict[str, Any]:
    """Unauthenticated connect/TLS only; never a substitute for request timing."""
    proxy = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")}
    if any(proxy.values()):
        return {
            "status": "skipped",
            "reason": "proxy environment present; direct probe path may differ from the model request path",
            "not_request_latency": True,
        }
    host = "api.deepseek.com"
    result: dict[str, Any] = {"host": host, "not_request_latency": True, "dns_status": "included_in_connect"}
    t0 = time.perf_counter()
    try:
        sock = socket.create_connection((host, 443), timeout=5)
        connect_ms = (time.perf_counter() - t0) * 1000
        result["tcp_connect_ms"] = round(connect_ms, 3)
        try:
            context = ssl.create_default_context()
            t1 = time.perf_counter()
            tls = context.wrap_socket(sock, server_hostname=host)
            result["tls_handshake_ms"] = round((time.perf_counter() - t1) * 1000, 3)
            result["tls_version"] = tls.version()
            tls.close()
        finally:
            try:
                sock.close()
            except OSError:
                pass
        result["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    return result


def _send_turn(url: str, request_id: str, turn_id: str, question: str, latency) -> dict[str, Any]:
    data = json.dumps({"prompt": question}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-RVS-Request-Id": request_id,
            "X-RVS-Turn-Id": turn_id,
        },
        method="POST",
    )
    events: list[dict[str, Any]] = []
    started = time.perf_counter()
    with latency.bind_context(request_id=request_id, turn_id=turn_id):
        request_span = latency.open_span("client.request", turn_id=turn_id, request_id=request_id)
        try:
            latency.point("client.request_send")
            response = urllib.request.urlopen(request, timeout=300)
            headers_ms = (time.perf_counter() - started) * 1000
            latency.point(
                "client.response_headers",
                http_status=getattr(response, "status", None),
                headers_ms=round(headers_ms, 3),
                x_rvs_request_id=response.headers.get("X-RVS-Request-Id"),
                x_rvs_cold=response.headers.get("X-RVS-Cold"),
            )
            first_line = None
            first_delta_ms = None
            answer_event_ms = None
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                if first_line is None:
                    first_line = (time.perf_counter() - started) * 1000
                    latency.point("client.first_sse_line", first_line_ms=round(first_line, 3))
                try:
                    event = json.loads(line[6:])
                except ValueError:
                    continue
                events.append(event)
                if event.get("type") == "answer_delta" and first_delta_ms is None:
                    first_delta_ms = (time.perf_counter() - started) * 1000
                    latency.point(
                        "client.first_answer_delta",
                        first_answer_delta_ms=round(first_delta_ms, 3),
                    )
                if event.get("type") == "answer" and answer_event_ms is None:
                    answer_event_ms = (time.perf_counter() - started) * 1000
                    latency.point("client.answer_event", answer_event_ms=round(answer_event_ms, 3))
                if event.get("type") == "done":
                    break
            done_ms = (time.perf_counter() - started) * 1000
            latency.point("client.done", done_ms=round(done_ms, 3))
            if request_span is not None:
                request_span.end(status="ok")
            summary = next((e for e in reversed(events) if e.get("type") in {"answer", "interrupt"}), {})
            return {
                "request_id": request_id,
                "turn_id": turn_id,
                "server_request_id": response.headers.get("X-RVS-Request-Id"),
                "cold_header": response.headers.get("X-RVS-Cold"),
                "http_status": getattr(response, "status", None),
                "client_total_ms": round(done_ms, 3),
                "headers_ms": round(headers_ms, 3),
                "first_sse_line_ms": round(first_line, 3) if first_line is not None else None,
                "first_answer_delta_ms": round(first_delta_ms, 3) if first_delta_ms is not None else None,
                "answer_event_ms": round(answer_event_ms, 3) if answer_event_ms is not None else None,
                "event_types": [e.get("type") for e in events],
                "answer": summary.get("answer") or "",
                "usage": summary.get("usage"),
                "support": summary.get("support"),
                "error": next((e.get("error") for e in events if e.get("type") == "error"), None),
            }
        except Exception as exc:  # noqa: BLE001
            latency.point("client.error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
            if request_span is not None:
                request_span.end(status="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
            return {
                "request_id": request_id,
                "turn_id": turn_id,
                "client_total_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "event_types": [e.get("type") for e in events],
            }


def main() -> int:
    global PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "outputs" / "latency-diagnosis"))
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--turns", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--port", type=int, default=8945)
    parser.add_argument(
        "--artifact-root",
        default=None,
        help="reuse this already-built artifact; default builds a fresh one",
    )
    parser.add_argument("--no-probe", action="store_true")
    args = parser.parse_args()
    PORT = int(args.port)

    run_dir = Path(args.out).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    spans_dir = run_dir / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    provider_records_dir = run_dir / "provider-records"
    provider_records_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "RVS_LATENCY_TRACE": "1",
            "RVS_PROVIDER_TRACE": "1",
            "RVS_LATENCY_DIR": str(spans_dir),
            "RVS_LATENCY_PROCESS": "client",
            "RVS_PROVIDER_TRANSPORT_DIR": str(provider_records_dir),
        }
    )
    from core.observability import latency
    from core.observability import provider_trace

    env_file = Path(args.env_file)
    if not env_file.is_file():
        raise SystemExit(f"environment file not found: {env_file}")

    built = (
        _load_artifact(Path(args.artifact_root))
        if args.artifact_root
        else _build_artifact()
    )
    instance = _write_instance(run_dir, built, env_file)
    server = Server(
        built,
        instance,
        run_dir / "server.log",
        spans_dir,
        provider_records_dir,
    )
    payload: dict[str, Any] = {
        "schema_version": "latency-diagnosis-run/v1",
        "started_at": _now_iso(),
        "question": QUESTION,
        "turns_requested": args.turns,
        "port": PORT,
        "artifact": built,
        "env_file_used": str(env_file),
        "env_values_read_or_logged": False,
        "host": "127.0.0.1",
        "probe": None,
        "provider_trace": provider_trace.status(),
        "provider_records_dir": str(provider_records_dir),
        "turns": [],
        "error": None,
    }
    try:
        health = server.start()
        payload["health"] = health
        if not args.no_probe:
            payload["probe"] = _probe_provider_host()
        session = _http_json("http://127.0.0.1:%d/api/sessions" % PORT, {"workflow": "agent"})
        session_id = session["session_id"]
        payload["session_id"] = session_id
        for index in range(1, args.turns + 1):
            request_id = f"latdiag-{int(time.time())}-{index}"
            turn_id = f"{session_id}-t{index}"
            turn = _send_turn(
                f"http://127.0.0.1:{PORT}/api/sessions/{session_id}/messages",
                request_id,
                turn_id,
                QUESTION,
                latency,
            )
            payload["turns"].append(turn)
            if turn.get("error"):
                payload["error"] = turn["error"]
                break
        try:
            detail = _http_json(
                f"http://127.0.0.1:{PORT}/api/sessions/{session_id}", timeout=60
            )
            payload["session_detail"] = {
                "status": detail.get("status"),
                "transcript": detail.get("transcript"),
                "support": detail.get("support"),
            }
        except Exception as exc:  # noqa: BLE001
            payload["session_detail_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        server.stop()
        payload["finished_at"] = _now_iso()
        payload["port_released"] = not _port_in_use("127.0.0.1", PORT)
        payload["raw_spans_path"] = str(run_dir / "raw-spans.json")
        payload["server_log"] = str(run_dir / "server.log")
        spans = latency.read_spans(spans_dir)
        trace_events: list[dict[str, Any]] = []
        session_id = payload.get("session_id")
        if session_id:
            trace_dir = run_dir / "instance-storage" / "sessions" / session_id / "outputs" / "traces"
            for path in sorted(trace_dir.glob("trace_*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            trace_events.append(json.loads(line))
                        except ValueError:
                            continue
        provider_records: list[dict[str, Any]] = []
        records_path = provider_records_dir / "records.jsonl"
        if records_path.is_file():
            for line in records_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        provider_records.append(json.loads(line))
                    except ValueError:
                        continue
        payload["provider_records_path"] = str(records_path)
        payload["provider_record_count"] = len(provider_records)
        raw = {
            "schema_version": "latency-raw-spans/v1",
            "source": "real diagnostic run; client/server/worker spans",
            "artifact_id": built.get("artifact_id"),
            "session_id": session_id,
            "generated_at": _now_iso(),
            "spans": spans,
            "trace_events": trace_events,
            "provider_records": provider_records,
            "notes": [
                "monotonic clocks are process-local; wall anchors align processes",
                "provider spans are opt-in and never contain headers/query/body/credentials",
                "first body chunk is not first token; product path uses non-streaming invoke",
                "DNS is included in provider.connect_tcp and not separately available",
            ],
        }
        (run_dir / "raw-spans.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (run_dir / "run.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"run": str(run_dir), "turns": len(payload["turns"]), "error": payload.get("error")}, ensure_ascii=False))
    return 0 if not payload.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
