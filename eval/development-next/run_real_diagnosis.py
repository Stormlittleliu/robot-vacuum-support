# -*- coding: utf-8 -*-
"""Run 3 short real-LLM development scenarios against the built artifact.

The server is a temporary child on port 8941 with a hidden window, started and
terminated inside this process.  It reads credentials only through the
instance's ``environment_file``; this script never opens or prints that file.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT.parent / "robot-vacuum-support-built" / "robot_vacuum-v4-e6b49a5880"
DEFAULT_INSTANCE = ROOT / "outputs" / "context-diagnosis-instance" / "instance.json"
DEFAULT_OUTPUT = ROOT / "outputs" / "context-diagnosis-real-run.json"
LOG_PATH = ROOT / "outputs" / "context-diagnosis-instance" / "server.log"

SCENARIOS = [
    {
        "id": "real-ambiguous-find-dock",
        "label": "用户含糊：找不到充电座",
        "prompt": "回充失败，找不到充电座。",
        "expect": "先问一次机器人回不到座还是用户找不到座的位置，不直接套传感器排查。",
    },
    {
        "id": "real-novice-guided",
        "label": "主动引导：新手 + 回不去",
        "prompt": "我不太懂这些，机器人回不去了，你一步步告诉我先做什么。",
        "expect": "识别新手/回充语境，给一个最低成本步骤和观察点，不要求用户懂术语。",
    },
    {
        "id": "real-tried-and-new-topic",
        "label": "明确已试 + 新问题",
        "prompt": "我已经清理了充电座周围，也擦了触点，还是回不去。对了，拖地时水箱漏水怎么办？",
        "expect": "记录已试；先完整回答水箱漏水；旧回充任务保留，末尾最多一次轻确认，不宣称已解决。",
    },
]


def post_json(url: str, payload: dict, timeout: float = 60):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    return urllib.request.urlopen(request, timeout=timeout)


def wait_health(base: str, timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("status") or payload.get("ready") is not None:
                    return payload
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    raise RuntimeError(f"health timeout: {last_error}")


def sse_answer(base: str, session_id: str, prompt: str, timeout: float = 240) -> dict:
    request = urllib.request.Request(
        f"{base}/api/sessions/{session_id}/messages",
        data=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    answer = None
    error = None
    support = None
    retrieval = None
    usage = None
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            if event.get("type") == "answer":
                answer = event.get("answer")
                support = event.get("support")
                retrieval = event.get("retrieval")
                usage = event.get("usage")
            elif event.get("type") == "error":
                error = event.get("error")
            elif event.get("type") == "done":
                break
    return {
        "answer": answer,
        "error": error,
        "support": support,
        "retrieval": retrieval,
        "usage": usage,
        "wall_ms": int((time.perf_counter() - started) * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-config", default=str(DEFAULT_INSTANCE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--artifact", default=str(ARTIFACT))
    args = parser.parse_args()
    instance = Path(args.instance_config).resolve()
    artifact = Path(args.artifact).resolve()
    output = Path(args.output).resolve()
    if not (artifact / "serve.py").is_file():
        print(f"artifact missing: {artifact}", file=sys.stderr)
        return 2
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    port = 8941
    base = f"http://127.0.0.1:{port}"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_handle = LOG_PATH.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(artifact / "serve.py"), "--instance-config", str(instance)],
        cwd=str(artifact),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    record = {
        "schema_version": "context-diagnosis-real-run/v1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "artifact": str(artifact),
        "instance_config": str(instance),
        "port": port,
        "model": "deepseek-chat",
        "note": "3 short development scenarios; not holdout, not a success rate. Credentials were read only by the instance process and are not copied here.",
        "health": None,
        "scenarios": [],
        "limitations": [],
    }
    try:
        record["health"] = wait_health(base)
        with post_json(base + "/api/sessions", {"workflow": "agent"}) as response:
            session = json.loads(response.read().decode("utf-8"))
        session_id = session["session_id"]
        record["session_id"] = session_id
        for scenario in SCENARIOS:
            result = sse_answer(base, session_id, scenario["prompt"])
            support = result.get("support") or {}
            active = support.get("active_case") or {}
            retrieval = result.get("retrieval") or {}
            review = retrieval.get("review") if isinstance(retrieval, dict) else None
            record["scenarios"].append(
                {
                    "id": scenario["id"],
                    "label": scenario["label"],
                    "prompt": scenario["prompt"],
                    "expect": scenario["expect"],
                    "answer": result.get("answer"),
                    "error": result.get("error"),
                    "usage": result.get("usage"),
                    "wall_ms": result.get("wall_ms"),
                    "support": {
                        "active_case": {
                            "issue": active.get("issue"),
                            "status": active.get("status"),
                            "stage": active.get("stage"),
                            "work_context": active.get("work_context"),
                            "tried_actions": active.get("tried_actions"),
                            "next_step": active.get("next_step"),
                            "closure_evidence": active.get("closure_evidence"),
                        },
                        "open_tasks": support.get("open_tasks"),
                        "pending_confirmation": support.get("pending_confirmation"),
                        "last_interpretation": support.get("last_interpretation"),
                    },
                    "retrieval": {
                        "status": retrieval.get("status") if isinstance(retrieval, dict) else None,
                        "sources": (review or {}).get("sources") if isinstance(review, dict) else None,
                        "citation_status": (review or {}).get("citation_status") if isinstance(review, dict) else None,
                        "notice": (review or {}).get("notice") if isinstance(review, dict) else None,
                    },
                }
            )
    except Exception as exc:  # noqa: BLE001 - recorded, not a fake success
        record["limitations"].append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        log_handle.close()
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "scenarios": len(record["scenarios"]), "limitations": record["limitations"]}, ensure_ascii=False))
    return 0 if record["scenarios"] and not record["limitations"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
