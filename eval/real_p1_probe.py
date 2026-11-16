# -*- coding: utf-8 -*-
"""One-off real-LLM acceptance probe for the built robot-vacuum artifact.

- Starts the artifact's normal ``serve.py`` entry with a hidden window.
- Uses the explicit instance environment_file only inside that child process.
- Records answers, support state, retrieval evidence and trace ids.
- Restarts the same storage once and verifies state recovery.
- Never prints or copies credential values.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(r"E:\CODE\Myproject\agent-workspace\robot-vacuum-support")
BUILT = Path(r"E:\CODE\Myproject\agent-workspace\robot-vacuum-support-built")
ENV_FILE = Path(r"E:\CODE\Myproject\agent-workspace\my_agent_harness\.env")
OUT = ROOT / "outputs"
INSTANCE_DIR = OUT / "p1-instance"
STORAGE = OUT / "p1-instance-storage"
PYTHON = r"C:\Users\28980\anaconda3\envs\langchain\python.exe"
HOST = "127.0.0.1"
PORT = 8930
BASE = f"http://{HOST}:{PORT}"


def _artifact():
    dirs = [
        item
        for item in BUILT.glob("robot_vacuum-v*")
        if item.is_dir() and (item / "manifest.json").is_file()
    ]
    if not dirs:
        raise SystemExit("no built v2 artifact")
    return max(dirs, key=lambda item: item.stat().st_mtime)


def _write_instance(artifact: Path):
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "instance_id": "robot-vacuum-p1-real",
        "artifact_id": manifest["artifact_id"],
        "artifact_root": str(artifact),
        "name": "扫地机器人售后客服（P1）",
        "scenario": "robot_vacuum",
        "version": 2,
        "workflow": "agent",
        "host": HOST,
        "port": PORT,
        "storage_root": str(STORAGE),
        "environment_file": str(ENV_FILE),
        "asset_bindings": {},
    }
    path = INSTANCE_DIR / "instance.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _get(path: str, timeout: float = 30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(path: str, payload: dict, timeout: float = 60):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _send(session_id: str, prompt: str, timeout: float = 180):
    request = urllib.request.Request(
        BASE + f"/api/sessions/{session_id}/messages",
        data=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    events = []
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
            if event.get("type") == "done":
                break
    return events


def _start(instance_path: Path):
    OUT.mkdir(parents=True, exist_ok=True)
    log = open(OUT / "p1-server.log", "a", encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("AH_SPEC_ROOT", None)
    process = subprocess.Popen(
        [PYTHON, str(_artifact() / "serve.py"), "--instance-config", str(instance_path)],
        cwd=str(ROOT.parent),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    deadline = time.time() + 90
    last = None
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited early: {process.returncode}")
        try:
            last = _get("/api/health", timeout=3)
            if last.get("ready"):
                return process, last
        except Exception as exc:  # noqa: BLE001
            last = {"error": type(exc).__name__}
        time.sleep(1)
    raise RuntimeError(f"server not ready: {last}")


def _stop(process):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def _events_summary(events):
    answer = ""
    support = None
    retrieval = None
    trace_id = None
    errors = []
    for event in events:
        kind = event.get("type")
        if kind == "answer":
            answer = event.get("answer") or answer
            support = event.get("support") or support
            retrieval = event.get("retrieval") or retrieval
            trace_id = event.get("trace_id") or trace_id
        elif kind == "answer_delta":
            answer += event.get("delta") or ""
        elif kind == "error":
            errors.append(event)
            support = event.get("support") or support
            retrieval = event.get("retrieval") or retrieval
    return {
        "answer": answer,
        "support": support,
        "retrieval": retrieval,
        "trace_id": trace_id,
        "errors": errors,
        "event_types": [event.get("type") for event in events],
    }


def main() -> int:
    artifact = _artifact()
    instance_path = _write_instance(artifact)
    report = {
        "schema_version": 1,
        "kind": "real-llm-probe",
        "note": "One observed run; not a benchmark, success rate, or citation-accuracy claim.",
        "artifact": str(artifact),
        "port": PORT,
        "credential_source": "instance environment_file (values never read/printed here)",
        "turns": [],
        "no_basis": None,
        "ui": None,
        "restart": None,
    }
    process = None
    try:
        process, health = _start(instance_path)
        report["health"] = {
            "status": health.get("status"),
            "ready": health.get("ready"),
            "missing": health.get("missing"),
        }
        index = urllib.request.urlopen(BASE + "/", timeout=15).read().decode("utf-8")
        report["ui"] = {
            "has_support_panel": "售后状态" in index,
            "has_handoff": "整理转人工摘要" in index,
            "has_tried_field": "已试步骤" in index,
            "has_next_field": "下一步" in index,
            "headless_http_only": True,
        }
        session = _post_json("/api/sessions", {"workflow": "agent"})
        session_id = session["session_id"]
        report["session_id"] = session_id
        prompts = [
            "我家的是石头G20，回充失败，充电座指示灯不亮，应该先检查什么？",
            "我已经擦了充电触点，还是不行。",
            "那下一步做什么？",
            "我刚才试过什么？",
        ]
        for index, prompt in enumerate(prompts, 1):
            events = _send(session_id, prompt)
            summary = _events_summary(events)
            detail = _get(f"/api/sessions/{session_id}")
            report["turns"].append(
                {
                    "turn": index,
                    "prompt": prompt,
                    "answer": summary["answer"],
                    "trace_id": summary["trace_id"],
                    "event_types": summary["event_types"],
                    "errors": summary["errors"],
                    "support": detail.get("support"),
                    "retrieval": summary["retrieval"],
                }
            )
        try:
            report["handoff"] = _post_json(
                f"/api/sessions/{session_id}/handoff-summary", {}
            )
        except Exception as exc:  # noqa: BLE001
            report["handoff"] = {"error": type(exc).__name__, "message": str(exc)}
        before_restart = _get(f"/api/sessions/{session_id}")
        report["before_restart"] = {
            "turn": (before_restart.get("support") or {}).get("turn"),
            "active_case": (before_restart.get("support") or {}).get("active_case"),
        }

        no_basis_session = _post_json("/api/sessions", {"workflow": "agent"})["session_id"]
        events = _send(no_basis_session, "Galaxy X99 的官方价格和保修期是多少？")
        no_basis_summary = _events_summary(events)
        no_basis_detail = _get(f"/api/sessions/{no_basis_session}")
        report["no_basis"] = {
            "session_id": no_basis_session,
            "answer": no_basis_summary["answer"],
            "trace_id": no_basis_summary["trace_id"],
            "errors": no_basis_summary["errors"],
            "support": no_basis_detail.get("support"),
            "retrieval": no_basis_summary["retrieval"],
        }

        _stop(process)
        process = None
        time.sleep(2)
        process, health2 = _start(instance_path)
        report["restart"] = {
            "health_ready": health2.get("ready"),
            "recovered": _get(f"/api/sessions/{session_id}"),
            "no_basis_recovered": _get(f"/api/sessions/{no_basis_session}"),
            "reopen": _post_json(f"/api/sessions/{session_id}/reopen", {}),
        }
        return 0
    except Exception as exc:  # noqa: BLE001
        report["fatal_error"] = {"type": type(exc).__name__, "message": str(exc)}
        return 1
    finally:
        _stop(process)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "p1-recharge-real-run.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "ok": "fatal_error" not in report,
                    "turns": len(report.get("turns") or []),
                    "no_basis": bool(report.get("no_basis")),
                    "restart": bool(report.get("restart")),
                    "fatal": report.get("fatal_error"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
