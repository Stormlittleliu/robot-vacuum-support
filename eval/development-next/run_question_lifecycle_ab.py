# -*- coding: utf-8 -*-
"""Run the fixed 5-turn dialogue against one artifact (baseline or after).

This is a development A/B helper, not a holdout or success-rate runner.  The
server is a hidden child on port 8942 and is terminated in ``finally``.  The
instance environment_file is created only as a path reference; this script never
opens, copies or prints credential values.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = ROOT.parent / "my_agent_harness" / ".env"
FIXED_DIALOGUE = [
    "回充失败，找不到充电座。",
    "我不太懂这些，机器人回不去了，你一步步告诉我先做什么。",
    "我已经清理了充电座周围，也擦了触点，还是回不去。",
    "对了，拖地时水箱漏水怎么办？",
    "谢谢",
]

_REFERENT_QUESTION = re.compile(
    r"(?:机器人|机器).{0,40}(?:回不去|回不了|回不到|打转|对不上|找不到).{0,80}"
    r"(?:您|你|自己).{0,40}(?:找不到|位置|放哪)|"
    r"(?:您|你|自己).{0,40}(?:找不到|位置|放哪).{0,80}"
    r"(?:机器人|机器).{0,40}(?:回不去|回不了|回不到|打转|对不上|找不到)"
)
_QUESTION = re.compile(r"[?？]")


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


def sse_answer(base: str, session_id: str, prompt: str, timeout: float = 300) -> dict:
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


def compact_support(support: dict | None) -> dict:
    support = support or {}
    active = support.get("active_case") or {}
    case_questions = active.get("questions") or []
    # Older artifacts expose clarification_history only.
    legacy_clarifications = active.get("clarification_history") or []
    return {
        "active_case": {
            "case_id": active.get("case_id"),
            "issue": active.get("issue"),
            "status": active.get("status"),
            "stage": active.get("stage"),
            "tried_actions": active.get("tried_actions"),
            "not_tried_actions": active.get("not_tried_actions"),
            "next_step": active.get("next_step"),
            "closure_evidence": active.get("closure_evidence"),
            "questions": case_questions,
            "clarification_history": legacy_clarifications,
            "last_question_audit": active.get("last_question_audit"),
        },
        "open_tasks": support.get("open_tasks"),
        "pending_confirmation": support.get("pending_confirmation"),
        "last_interpretation": support.get("last_interpretation"),
        "last_turn_scope": support.get("last_turn_scope"),
        "last_decision": support.get("last_decision"),
        "decision_snapshot": support.get("decision_snapshot"),
        "last_question_audit": support.get("last_question_audit"),
        "last_turn_metrics": support.get("last_turn_metrics"),
    }


def run(artifact: Path, label: str, output: Path, env_file: Path, port: int = 8942) -> dict:
    artifact = artifact.resolve()
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    run_dir = output.parent / label
    storage = run_dir / "storage"
    instance_dir = run_dir / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)
    instance_config = {
        "schema_version": 1,
        "instance_id": f"inst-question-lifecycle-{label}-{port}",
        "artifact_id": manifest["artifact_id"],
        "artifact_root": str(artifact),
        "name": f"robot-vacuum-question-lifecycle-{label}",
        "scenario": "robot_vacuum",
        "version": int(manifest.get("version") or 4),
        "workflow": "agent",
        "host": "127.0.0.1",
        "port": port,
        "storage_root": str(storage),
        "environment_file": str(env_file),
        "asset_bindings": {},
    }
    config_path = instance_dir / "instance.json"
    config_path.write_text(json.dumps(instance_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    base = f"http://127.0.0.1:{port}"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_handle = (run_dir / "server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(artifact / "serve.py"), "--instance-config", str(config_path)],
        cwd=str(artifact),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    record = {
        "schema_version": "question-lifecycle-ab-run/v1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "label": label,
        "artifact": str(artifact),
        "artifact_id": manifest["artifact_id"],
        "instance_config": str(config_path),
        "port": port,
        "model": "deepseek-chat",
        "note": "fixed 5-turn development dialogue; not holdout, not a success rate; credentials only referenced by environment_file path.",
        "dialogue": FIXED_DIALOGUE,
        "health": None,
        "session_id": None,
        "turns": [],
        "limitations": [],
    }
    try:
        record["health"] = wait_health(base)
        with post_json(base + "/api/sessions", {"workflow": "agent"}) as response:
            session = json.loads(response.read().decode("utf-8"))
        record["session_id"] = session["session_id"]
        for index, prompt in enumerate(FIXED_DIALOGUE, start=1):
            result = sse_answer(base, record["session_id"], prompt)
            retrieved = result.get("retrieval") or {}
            review = retrieved.get("review") if isinstance(retrieved, dict) else None
            answer = str(result.get("answer") or "")
            record["turns"].append(
                {
                    "turn": index,
                    "prompt": prompt,
                    "answer": result.get("answer"),
                    "error": result.get("error"),
                    "wall_ms": result.get("wall_ms"),
                    "usage": result.get("usage"),
                    "referent_question_in_answer": bool(
                        _QUESTION.search(answer) and _REFERENT_QUESTION.search(answer)
                    ),
                    "support": compact_support(result.get("support")),
                    "retrieval": {
                        "status": retrieved.get("status") if isinstance(retrieved, dict) else None,
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
        if proc.poll() is None:
            record["limitations"].append("server process still alive after terminate")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--environment-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--port", type=int, default=8942)
    args = parser.parse_args()
    record = run(
        Path(args.artifact),
        args.label,
        Path(args.output).resolve(),
        Path(args.environment_file).resolve(),
        port=args.port,
    )
    summary = {
        "label": record["label"],
        "artifact_id": record["artifact_id"],
        "turns": len(record["turns"]),
        "errors": sum(1 for turn in record["turns"] if turn.get("error")),
        "referent_question_turns": [
            turn["turn"] for turn in record["turns"] if turn.get("referent_question_in_answer")
        ],
        "limitations": record["limitations"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if record["turns"] and not record["limitations"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
