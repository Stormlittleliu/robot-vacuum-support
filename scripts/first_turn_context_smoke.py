# -*- coding: utf-8 -*-
"""Small real first-turn smoke (A + B), isolated port 8947.

Reuses the existing real-run mechanism in ``scripts/run_latency_diagnosis.py``:
fresh/reused artifact, hidden managed child server, instance.json, SSE turn
sender, session detail and trace collection.  It does not rebuild, does not
touch port 8950, does not run the 24-case evaluation/Judge/holdout, and never
prints or copies credential values.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime"))

import run_latency_diagnosis as dl  # noqa: E402

A_TEXT = "充电座指示灯不亮，机器人放上去没反应，怎么办？"
B_TEXT = "它回充不行，帮我看看。"
B_FOLLOWUP = "是机器人自己回不到充电座，在座前面打转。"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _session_artifacts(run_dir: Path, session_id: str) -> dict[str, Any]:
    session_dir = run_dir / "instance-storage" / "sessions" / session_id
    support = _read_json(session_dir / "support_state.json")
    trace_events: list[dict[str, Any]] = []
    trace_dir = session_dir / "outputs" / "traces"
    for path in sorted(trace_dir.glob("trace_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    trace_events.append(json.loads(line))
                except ValueError:
                    continue
    transcript = []
    transcript_path = session_dir / "transcript.jsonl"
    if transcript_path.is_file():
        for line in transcript_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    transcript.append(json.loads(line))
                except ValueError:
                    continue
    trace_summary = {
        "events": len(trace_events),
        "model_starts": sum(1 for item in trace_events if item.get("event") == "model_start"),
        "tool_starts": sum(1 for item in trace_events if item.get("event") == "tool_start"),
        "tool_ends": sum(1 for item in trace_events if item.get("event") == "tool_end"),
        "tools": sorted(
            {
                str(item.get("tool"))
                for item in trace_events
                if item.get("event") in {"tool_start", "tool_end"} and item.get("tool")
            }
        ),
        "tool_statuses": sorted(
            {
                str(item.get("status"))
                for item in trace_events
                if item.get("event") == "tool_end" and item.get("status")
            }
        ),
    }
    case = None
    if isinstance(support, dict):
        active_id = support.get("active_case_id")
        case = next(
            (item for item in support.get("cases") or [] if item.get("case_id") == active_id),
            None,
        )
    return {
        "support_state": support,
        "case": case,
        "trace": trace_summary,
        "trace_events": trace_events,
        "transcript": transcript,
    }


def _summarize_turn(turn: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    support = artifacts.get("support_state") or {}
    case = artifacts.get("case") or {}
    interpretation = (support.get("last_interpretation") or {}).get("interpretation") or {}
    trace_info = (support.get("last_interpretation") or {}).get("trace") or {}
    work_context = case.get("work_context") or {}
    usage = turn.get("usage") or {}
    by_layer = usage.get("by_layer") or {}
    return {
        "request_id": turn.get("request_id"),
        "http_status": turn.get("http_status"),
        "error": turn.get("error"),
        "answer": turn.get("answer") or "",
        "answer_chars": len(turn.get("answer") or ""),
        "usage": usage,
        "scope": support.get("last_turn_scope"),
        "active_case_id": support.get("active_case_id"),
        "case_status": case.get("status"),
        "case_stage": case.get("stage"),
        "case_tried": [
            [item.get("action_id"), item.get("outcome")]
            for item in case.get("tried_actions") or []
        ],
        "case_not_tried": [item.get("action_id") for item in case.get("not_tried_actions") or []],
        "next_step": case.get("next_step"),
        "activation_evidence": case.get("activation_evidence"),
        "work_context": {
            "domain": work_context.get("domain"),
            "topic": work_context.get("topic"),
            "experiencer": work_context.get("experiencer"),
            "observable_symptom": work_context.get("observable_symptom"),
            "interaction_stage": work_context.get("interaction_stage"),
            "ambiguity": work_context.get("ambiguity"),
        },
        "questions": [
            {
                "slot": item.get("slot"),
                "status": item.get("status"),
                "ask_count": item.get("ask_count"),
                "answer_value": item.get("answer_value"),
            }
            for item in case.get("questions") or []
        ],
        "semantic": {
            "method": trace_info.get("method"),
            "model": trace_info.get("model"),
            "usage_status": trace_info.get("usage_status"),
            "total_tokens": (trace_info.get("usage") or {}).get("total_tokens"),
            "validation_errors": trace_info.get("validation_errors"),
            "fallback_reason": trace_info.get("fallback_reason"),
            "topic": interpretation.get("topic"),
            "closure_signal": interpretation.get("closure_signal"),
            "interaction_stage": interpretation.get("interaction_stage"),
            "ambiguity": interpretation.get("ambiguity"),
        },
        "trace": artifacts.get("trace"),
        "main_agent_requests": ((by_layer.get("main_agent") or {}).get("requests")),
        "interpretation_requests": ((by_layer.get("interpretation") or {}).get("requests")),
        "transcript_roles": [item.get("role") for item in artifacts.get("transcript") or []],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "outputs" / "first-turn-context-smoke"))
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--env-file", default=str(ROOT.parent / "my_agent_harness" / ".env"))
    parser.add_argument("--port", type=int, default=8947)
    args = parser.parse_args()

    dl.PORT = int(args.port)
    run_dir = Path(args.out).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        run_dir = run_dir.with_name(run_dir.name + "-" + time.strftime("%Y%m%d-%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=True)
    spans_dir = run_dir / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    provider_records_dir = run_dir / "provider-records"
    provider_records_dir.mkdir(parents=True, exist_ok=True)
    env_file = Path(args.env_file)
    if not env_file.is_file():
        raise SystemExit("environment file not found (values are not printed)")

    built = dl._load_artifact(Path(args.artifact_root))
    instance = dl._write_instance(run_dir, built, env_file)
    server = dl.Server(built, instance, run_dir / "server.log", spans_dir, provider_records_dir)
    from core.observability import latency

    payload: dict[str, Any] = {
        "schema_version": "first-turn-context-smoke/v1",
        "started_at": _now_iso(),
        "port": dl.PORT,
        "artifact": built,
        "env_file_used": str(env_file),
        "env_values_read_or_logged": False,
        "questions": {"A": A_TEXT, "B": B_TEXT, "B_followup": B_FOLLOWUP},
        "sessions": {},
        "error": None,
    }
    try:
        payload["health"] = server.start()
        session_a = dl._http_json(
            f"http://127.0.0.1:{dl.PORT}/api/sessions", {"workflow": "agent"}
        )["session_id"]
        turn_a = dl._send_turn(
            f"http://127.0.0.1:{dl.PORT}/api/sessions/{session_a}/messages",
            f"firstturn-a-{int(time.time())}",
            f"{session_a}-t1",
            A_TEXT,
            latency,
        )
        artifacts_a = _session_artifacts(run_dir, session_a)
        payload["sessions"]["A"] = {
            "session_id": session_a,
            "turn": _summarize_turn(turn_a, artifacts_a),
        }

        session_b = dl._http_json(
            f"http://127.0.0.1:{dl.PORT}/api/sessions", {"workflow": "agent"}
        )["session_id"]
        turn_b1 = dl._send_turn(
            f"http://127.0.0.1:{dl.PORT}/api/sessions/{session_b}/messages",
            f"firstturn-b1-{int(time.time())}",
            f"{session_b}-t1",
            B_TEXT,
            latency,
        )
        artifacts_b1 = _session_artifacts(run_dir, session_b)
        turn_b2 = dl._send_turn(
            f"http://127.0.0.1:{dl.PORT}/api/sessions/{session_b}/messages",
            f"firstturn-b2-{int(time.time())}",
            f"{session_b}-t2",
            B_FOLLOWUP,
            latency,
        )
        artifacts_b2 = _session_artifacts(run_dir, session_b)
        payload["sessions"]["B"] = {
            "session_id": session_b,
            "turn1": _summarize_turn(turn_b1, artifacts_b1),
            "turn2": _summarize_turn(turn_b2, artifacts_b2),
        }
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        server.stop()
        payload["finished_at"] = _now_iso()
        payload["port_released"] = not dl._port_in_use("127.0.0.1", dl.PORT)
        payload["server_log"] = str(run_dir / "server.log")

    a = (payload.get("sessions", {}).get("A") or {}).get("turn") or {}
    b1 = (payload.get("sessions", {}).get("B") or {}).get("turn1") or {}
    b2 = (payload.get("sessions", {}).get("B") or {}).get("turn2") or {}
    checks = {
        "a_scope_recharge": a.get("scope") == "recharge",
        "a_case_active": a.get("case_status") == "active",
        "a_no_stale_followup": "还是不行”是指" not in (a.get("answer") or ""),
        "a_main_agent_called": int(a.get("main_agent_requests") or 0) >= 1,
        "a_retrieval_tool_called": "rag_retrieve" in ((a.get("trace") or {}).get("tools") or []),
        "a_tool_ok": all(
            status in {"success", "ok"}
            for status in ((a.get("trace") or {}).get("tool_statuses") or ["success"])
        ),
        "a_answer_nonempty": int(a.get("answer_chars") or 0) >= 80,
        "b_scope_recharge": b1.get("scope") == "recharge",
        "b_case_active": b1.get("case_status") == "active",
        "b_topic_recharge": (b1.get("work_context") or {}).get("topic") == "recharge_failure",
        "b_semantic_method": (b1.get("semantic") or {}).get("method") in {
            "llm_structured",
            "llm_structured_partial",
            "fallback",
        },
        "b_no_invented_tried": not (b1.get("case_tried") or []),
        "b_followup_same_case": b2.get("active_case_id") == b1.get("active_case_id"),
    }
    payload["checks"] = checks
    payload["passed"] = all(checks.values()) and not payload.get("error")
    (run_dir / "run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "run": str(run_dir),
                "artifact": built.get("artifact_id"),
                "port_released": payload.get("port_released"),
                "passed": payload.get("passed"),
                "error": payload.get("error"),
                "checks": checks,
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
