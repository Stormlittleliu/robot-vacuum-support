# -*- coding: utf-8 -*-
"""Real two-session smoke for the action-state contract (isolated port 8948).

S1: recharge failure -> executed contact-clean with unknown effect -> correction
    (not tried; only cleared obstacles) -> "what did I try?"
S2: recharge failure -> replaced the dock -> correction (only moved the old
    dock) -> "which steps did I try?"
The service-restart counterexample is checked deterministically in-process; it
does not need a model turn.  No 24-case/Judge/holdout rerun, no 8950 access.
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

import first_turn_context_smoke as ft  # noqa: E402

dl = ft.dl

S1 = [
    "回充失败，我刚才擦了充电触点，还没看结果，怎么办？",
    "说错了，我没擦触点，只是把充电座周围清了一下。",
    "我刚才试过什么？",
]
S2 = [
    "回充失败，我换了新的充电座，还没看回充效果。",
    "说错了，不是换新座，只是把旧充电座挪到了客厅。",
    "我刚才试过哪些步骤？",
]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _facts(artifacts: dict[str, Any]) -> dict[str, Any]:
    case = artifacts.get("case") or {}
    tried_rows = case.get("tried_actions") or []
    not_tried_rows = case.get("not_tried_actions") or []

    def effective(rows):
        return [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("fact_status") or "effective") == "effective"
        ]

    effective_tried = effective(tried_rows)
    effective_not_tried = effective(not_tried_rows)
    return {
        "case_status": case.get("status"),
        "case_stage": case.get("stage"),
        "case_resolved_at": case.get("resolved_at"),
        "case_resolution": case.get("resolution"),
        "closure_evidence": case.get("closure_evidence"),
        "effective_tried": [
            [row.get("action_id"), row.get("outcome")] for row in effective_tried
        ],
        "effective_not_tried": [row.get("action_id") for row in effective_not_tried],
        "retracted_tried": [
            [row.get("action_id"), row.get("outcome"), row.get("retraction_reason")]
            for row in tried_rows
            if str(row.get("fact_status") or "effective") != "effective"
        ],
        "superseded_not_tried": [
            [row.get("action_id"), row.get("supersede_reason")]
            for row in not_tried_rows
            if str(row.get("fact_status") or "effective") != "effective"
        ],
        "fact_audit": [
            {
                "kind": row.get("kind"),
                "action_id": row.get("action_id"),
                "reason": row.get("reason"),
                "message_id": row.get("message_id"),
                "turn": row.get("turn"),
                "replaced_outcome": ((row.get("replaced_fact") or {}).get("outcome")),
            }
            for row in case.get("fact_audit") or []
        ],
        "negation_ambiguity": case.get("negation_ambiguity"),
        "last_fact_binding": case.get("last_fact_binding"),
        "last_asked_actions": case.get("last_asked_actions"),
    }


def _capture(turn: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    data = ft._summarize_turn(turn, artifacts)
    facts = _facts(artifacts)
    data.update(facts)
    # ``_summarize_turn`` lists all rows; expose the authoritative effective set
    # explicitly so a retracted row cannot look current in the smoke evidence.
    data["case_tried"] = facts["effective_tried"]
    data["case_not_tried"] = facts["effective_not_tried"]
    return data


def _turn_trace(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "events": len(events),
        "model_starts": sum(1 for item in events if item.get("event") == "model_start"),
        "tool_starts": sum(1 for item in events if item.get("event") == "tool_start"),
        "tool_ends": sum(1 for item in events if item.get("event") == "tool_end"),
        "tools": sorted(
            {
                str(item.get("tool"))
                for item in events
                if item.get("event") in {"tool_start", "tool_end"} and item.get("tool")
            }
        ),
    }


def _run_session(
    run_dir: Path, prefix: str, texts: list[str], latency
) -> dict[str, Any]:
    session_id = dl._http_json(
        f"http://127.0.0.1:{dl.PORT}/api/sessions", {"workflow": "agent"}
    )["session_id"]
    turns: list[dict[str, Any]] = []
    previous_trace_count = 0
    for index, text in enumerate(texts, start=1):
        turn = dl._send_turn(
            f"http://127.0.0.1:{dl.PORT}/api/sessions/{session_id}/messages",
            f"{prefix}-{index}-{int(time.time())}",
            f"{session_id}-t{index}",
            text,
            latency,
        )
        artifacts = ft._session_artifacts(run_dir, session_id)
        turn_events = (artifacts.get("trace_events") or [])[previous_trace_count:]
        previous_trace_count = len(artifacts.get("trace_events") or [])
        data = _capture(turn, artifacts)
        data["turn_trace"] = _turn_trace(turn_events)
        turns.append(data)
    return {"session_id": session_id, "questions": texts, "turns": turns}


def _meta_state_checks() -> dict[str, Any]:
    from robot_vacuum_support import state as support

    service = support.empty_state()
    service = support.apply_user_turn(service, "回充失败", "meta-1", now=1)["state"]
    service = support.apply_user_turn(service, "服务重启了，还是不行", "meta-2", now=2)["state"]
    service_case = support.public_view(service)["active_case"]
    robot = support.empty_state()
    robot = support.apply_user_turn(robot, "回充失败", "meta-3", now=1)["state"]
    robot = support.apply_user_turn(robot, "重启了机器人，还是不行", "meta-4", now=2)["state"]
    robot_case = support.public_view(robot)["active_case"]
    return {
        "service_restart": {
            "tried": [(row["action_id"], row["outcome"]) for row in service_case["tried_actions"]],
            "not_reset_robot": "reset_robot"
            not in [row["action_id"] for row in service_case["tried_actions"]],
        },
        "robot_restart": {
            "tried": [(row["action_id"], row["outcome"]) for row in robot_case["tried_actions"]],
            "recorded_reset_robot": ("reset_robot", "failed")
            in [(row["action_id"], row["outcome"]) for row in robot_case["tried_actions"]],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--out", default=str(ROOT / "outputs" / "action-state-contract-smoke"))
    parser.add_argument("--env-file", default=str(ROOT.parent / "my_agent_harness" / ".env"))
    parser.add_argument("--port", type=int, default=8948)
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
        "schema_version": "action-state-contract-smoke/v1",
        "started_at": _now_iso(),
        "port": dl.PORT,
        "artifact": built,
        "env_file_used": str(env_file),
        "env_values_read_or_logged": False,
        "sessions": {},
        "meta_state_checks": _meta_state_checks(),
        "error": None,
    }
    try:
        payload["health"] = server.start()
        payload["sessions"]["S1"] = _run_session(run_dir, "contract-s1", S1, latency)
        payload["sessions"]["S2"] = _run_session(run_dir, "contract-s2", S2, latency)
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        server.stop()
        payload["finished_at"] = _now_iso()
        payload["port_released"] = not dl._port_in_use("127.0.0.1", dl.PORT)
        payload["server_log"] = str(run_dir / "server.log")

    s1 = (payload.get("sessions", {}).get("S1") or {}).get("turns") or []
    s2 = (payload.get("sessions", {}).get("S2") or {}).get("turns") or []
    s1 = s1 + [{}] * (3 - len(s1))
    s2 = s2 + [{}] * (3 - len(s2))
    meta = payload["meta_state_checks"]
    checks = {
        "s1_t1_executed_unknown": s1[0].get("effective_tried") == [["clean_contacts", "unknown"]]
        and s1[0].get("case_status") == "active",
        "s1_t2_correction_retracted": s1[1].get("effective_tried")
        == [["clear_dock_obstacles", "unknown"]]
        and s1[1].get("effective_not_tried") == ["clean_contacts"]
        and any(
            row.get("kind") == "retract_tried" and row.get("action_id") == "clean_contacts"
            for row in s1[1].get("fact_audit") or []
        ),
        "s1_t3_recall_no_agent_or_rag": int(s1[2].get("main_agent_requests") or 0) == 0
        and not ((s1[2].get("turn_trace") or {}).get("tools") or []),
        "s1_t3_answer_only_current_facts": "清理充电座周围障碍物" in (s1[2].get("answer") or "")
        and "结果未说明" in (s1[2].get("answer") or "")
        and "清洁充电触点" in (s1[2].get("answer") or "")
        and "没做过" in (s1[2].get("answer") or ""),
        "s2_t1_replace_unknown": s2[0].get("effective_tried") == [["replace_dock", "unknown"]],
        "s2_t2_move_kept_replace_retracted": s2[1].get("effective_tried")
        == [["move_dock", "unknown"]]
        and s2[1].get("effective_not_tried") == ["replace_dock"]
        and any(
            row.get("kind") == "retract_tried" and row.get("action_id") == "replace_dock"
            for row in s2[1].get("fact_audit") or []
        ),
        "s2_t2_no_reseat_drift": "reseat_dock"
        not in [row[0] for row in s2[1].get("effective_tried") or []],
        "s2_t3_recall_no_agent_or_rag": int(s2[2].get("main_agent_requests") or 0) == 0
        and not ((s2[2].get("turn_trace") or {}).get("tools") or []),
        "s2_t3_answer_move_not_replace": "挪动原来的充电座" in (s2[2].get("answer") or "")
        and "结果未说明" in (s2[2].get("answer") or "")
        and "更换新的充电座" in (s2[2].get("answer") or "")
        and "没做过" in (s2[2].get("answer") or ""),
        "meta_service_restart_not_reset": bool(
            (meta.get("service_restart") or {}).get("not_reset_robot")
        ),
        "meta_robot_restart_recorded": bool(
            (meta.get("robot_restart") or {}).get("recorded_reset_robot")
        ),
        "no_turn_error": all(not turn.get("error") for turn in s1 + s2),
        "port_released": bool(payload.get("port_released")),
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
