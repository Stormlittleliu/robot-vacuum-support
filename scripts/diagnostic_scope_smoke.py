# -*- coding: utf-8 -*-
"""Real 4-session / 5-turn diagnostic-scope smoke (isolated port 8949).

Frozen inputs/oracle are written to ``outputs/diagnostic-scope-smoke-v1/
input-freeze.json`` before the hidden service starts and before any model call.
The run records per-turn support state, semantic trace, decision snapshot,
observations and turn-level trace, plus raw session artifacts.  Program checks
are structural state/data checks only; answer causality is not scored here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime"))

import first_turn_context_smoke as ft  # noqa: E402

dl = ft.dl

CASES: dict[str, dict[str, Any]] = {
    "ds-live-manual-works-auto-fails-01": {
        "questions": [
            "自动回充还是失败，但我手动把机器人放到充电座上就能充电。是不是证明触点问题已经解决，接下来不用管了？"
        ],
        "oracle": {
            "turn_1": {
                "case_status": "active",
                "observations": {
                    "manual_charging": "works",
                    "automatic_return": "fails",
                },
                "diagnostic_scope": {
                    "task_goal": "automatic_return",
                    "resolved": False,
                    "allowed_conclusion_strength": "task_goal_failed",
                    "root_cause_status": "unconfirmed",
                },
            }
        },
    },
    "ds-live-clearance-manual-only-01": {
        "questions": [
            "我已经清了充电座周围，手动放回可以充电，但还没试过让它自己回去。现在能确定就是遮挡造成的吗？"
        ],
        "oracle": {
            "turn_1": {
                "case_status": "active",
                "observations": {"manual_charging": "works"},
                "automatic_return_must_be_unverified": True,
                "diagnostic_scope": {
                    "task_goal": "automatic_return",
                    "resolved": False,
                    "allowed_conclusion_strength": "scoped_positive_only",
                },
            }
        },
    },
    "ds-live-manual-and-auto-fail-01": {
        "questions": [
            "机器人自己回不到充电座，我把它放上去也充不上电。该先确认什么？"
        ],
        "oracle": {
            "turn_1": {
                "case_status": "active",
                "observations": {
                    "manual_charging": "fails",
                    "automatic_return": "fails",
                },
                "diagnostic_scope": {
                    "task_goal": "automatic_return",
                    "resolved": False,
                    "allowed_conclusion_strength": "task_goal_failed",
                },
            }
        },
    },
    "ds-live-auto-recovered-01": {
        "questions": [
            "机器人自动回充失败，回不到充电座。",
            "现在它能从房间自己找到充电座，并且已经开始充电了。",
        ],
        "oracle": {
            "turn_1": {
                "case_status": "active",
                "observations": {"automatic_return": "fails"},
                "diagnostic_scope": {"resolved": False},
            },
            "turn_2": {
                "case_status": "resolved",
                "observations": {"automatic_return": "works"},
                "diagnostic_scope": {
                    "task_goal": "automatic_return",
                    "resolved": True,
                    "allowed_conclusion_strength": "resolved",
                },
            },
        },
    },
}

SOURCE_FILES = (
    "state.py",
    "diagnostic_scope.py",
    "interpretation.py",
    "actions.py",
    "questions.py",
    "routing.py",
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hashes(artifact_root: Path) -> tuple[dict[str, str], dict[str, str], bool]:
    source: dict[str, str] = {}
    artifact: dict[str, str] = {}
    for name in SOURCE_FILES:
        relative = "runtime/robot_vacuum_support/" + name
        source[relative] = _sha256(ROOT / relative)
        artifact_path = artifact_root / relative
        artifact[relative] = _sha256(artifact_path) if artifact_path.is_file() else ""
    return source, artifact, source == artifact


def _external_check(artifact_root: Path) -> dict[str, Any]:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, str(artifact_root / "run.py"), "--check"],
        cwd=str(ROOT.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
        timeout=180,
    )
    payload: dict[str, Any] = {
        "cwd": str(ROOT.parent),
        "returncode": completed.returncode,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "stdout": completed.stdout.strip()[:2000],
        "stderr": completed.stderr.strip()[:2000],
        "parsed": None,
    }
    try:
        payload["parsed"] = json.loads(completed.stdout)
    except ValueError:
        payload["parsed"] = None
    payload["ok"] = bool(
        completed.returncode == 0
        and isinstance(payload["parsed"], dict)
        and payload["parsed"].get("ok") is True
    )
    return payload


def _session_artifacts(run_dir: Path, session_id: str) -> dict[str, Any]:
    return ft._session_artifacts(run_dir, session_id)


def _effective_observations(active: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(active, dict):
        return []
    return [
        {
            "observation_id": row.get("observation_id"),
            "capability": row.get("capability"),
            "result": row.get("result"),
            "polarity": row.get("polarity"),
            "quote": row.get("quote"),
            "message_id": row.get("message_id"),
            "turn": row.get("turn"),
            "source": row.get("source"),
            "extraction": row.get("extraction"),
            "fact_status": row.get("fact_status"),
        }
        for row in active.get("observations") or []
        if isinstance(row, dict) and str(row.get("fact_status") or "effective") == "effective"
    ]


def _observation_map(turn: dict[str, Any]) -> dict[str, str]:
    return {
        str(row.get("capability")): str(row.get("result"))
        for row in turn.get("observations") or []
    }


def _turn_trace(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "event_count": len(events),
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
        "tool_statuses": sorted(
            {
                str(item.get("status"))
                for item in events
                if item.get("event") == "tool_end" and item.get("status")
            }
        ),
        "events": events,
    }


def _capture_turn(
    turn: dict[str, Any],
    artifacts: dict[str, Any],
    turn_index: int,
    turn_events: list[dict[str, Any]],
) -> dict[str, Any]:
    support = artifacts.get("support_state") or {}
    active = artifacts.get("case") or {}
    interpretation = (support.get("last_interpretation") or {}).get("interpretation") or {}
    trace_info = (support.get("last_interpretation") or {}).get("trace") or {}
    snapshot = support.get("decision_snapshot") or {}
    usage = turn.get("usage") or {}
    by_layer = usage.get("by_layer") or {}
    observations = _effective_observations(active)
    extraction_counts: dict[str, int] = {}
    for row in observations:
        key = str(row.get("extraction") or "unknown")
        extraction_counts[key] = extraction_counts.get(key, 0) + 1
    return {
        "turn": turn_index,
        "request_id": turn.get("request_id"),
        "turn_id": turn.get("turn_id"),
        "server_request_id": turn.get("server_request_id"),
        "http_status": turn.get("http_status"),
        "error": turn.get("error"),
        "answer": turn.get("answer") or "",
        "answer_chars": len(turn.get("answer") or ""),
        "scope": support.get("last_turn_scope"),
        "state": {
            "turn": support.get("turn"),
            "active_case_id": support.get("active_case_id"),
            "last_turn_scope": support.get("last_turn_scope"),
            "last_decision": support.get("last_decision"),
            "case_status": active.get("status"),
            "case_stage": active.get("stage"),
            "resolved_at": active.get("resolved_at"),
            "resolution": active.get("resolution"),
            "closure_evidence": active.get("closure_evidence"),
            "tried_actions": [
                [row.get("action_id"), row.get("outcome")]
                for row in active.get("tried_actions") or []
                if str(row.get("fact_status") or "effective") == "effective"
            ],
            "not_tried_actions": [
                row.get("action_id")
                for row in active.get("not_tried_actions") or []
                if str(row.get("fact_status") or "effective") == "effective"
            ],
            "questions": [
                {
                    "slot": row.get("slot"),
                    "status": row.get("status"),
                    "ask_count": row.get("ask_count"),
                }
                for row in active.get("questions") or []
            ],
        },
        "case": active,
        "observations": observations,
        "observation_extraction_counts": extraction_counts,
        "semantic_observations_accepted": len(
            interpretation.get("capability_observations") or []
        ),
        "observation_audit": active.get("observation_audit") or [],
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
            "capability_observations": interpretation.get("capability_observations"),
            "observation_retractions": interpretation.get("observation_retractions"),
        },
        "snapshot": snapshot,
        "diagnostic_scope": snapshot.get("diagnostic_scope"),
        "usage": usage,
        "main_agent_requests": ((by_layer.get("main_agent") or {}).get("requests")),
        "interpretation_requests": ((by_layer.get("interpretation") or {}).get("requests")),
        "trace": _turn_trace(turn_events),
    }


def _run_session(run_dir: Path, case_id: str, questions: list[str], latency) -> dict[str, Any]:
    session_id = dl._http_json(
        f"http://127.0.0.1:{dl.PORT}/api/sessions", {"workflow": "agent"}
    )["session_id"]
    turns: list[dict[str, Any]] = []
    previous_trace_count = 0
    for index, text in enumerate(questions, start=1):
        turn = dl._send_turn(
            f"http://127.0.0.1:{dl.PORT}/api/sessions/{session_id}/messages",
            f"{case_id}-t{index}-{int(time.time())}",
            f"{session_id}-t{index}",
            text,
            latency,
        )
        artifacts = _session_artifacts(run_dir, session_id)
        turn_events = (artifacts.get("trace_events") or [])[previous_trace_count:]
        previous_trace_count = len(artifacts.get("trace_events") or [])
        turns.append(_capture_turn(turn, artifacts, index, turn_events))
    base = run_dir / "instance-storage" / "sessions" / session_id
    return {
        "session_id": session_id,
        "questions": questions,
        "turns": turns,
        "source_paths": {
            "support_state": str(base / "support_state.json"),
            "transcript": str(base / "transcript.jsonl"),
            "trace_dir": str(base / "outputs" / "traces"),
            "raw_session_dir": str(base),
        },
        "transcript": _session_artifacts(run_dir, session_id).get("transcript") or [],
    }


def _checks(payload: dict[str, Any]) -> dict[str, bool]:
    sessions = payload.get("sessions") or {}
    s1 = ((sessions.get("ds-live-manual-works-auto-fails-01") or {}).get("turns") or [{}])[0]
    s2 = ((sessions.get("ds-live-clearance-manual-only-01") or {}).get("turns") or [{}])[0]
    s3 = ((sessions.get("ds-live-manual-and-auto-fail-01") or {}).get("turns") or [{}])[0]
    s4 = ((sessions.get("ds-live-auto-recovered-01") or {}).get("turns") or [{}, {}])
    s1_obs = _observation_map(s1)
    s2_obs = _observation_map(s2)
    s3_obs = _observation_map(s3)
    s4_t1_obs = _observation_map(s4[0])
    s4_t2_obs = _observation_map(s4[1])
    s4_t2_scope = s4[1].get("diagnostic_scope") or {}
    all_turns = [
        turn
        for session in sessions.values()
        for turn in session.get("turns") or []
    ]
    checks = {
        "s1_manual_works_auto_fails": s1_obs.get("manual_charging") == "works"
        and s1_obs.get("automatic_return") == "fails",
        "s1_active_not_resolved": (s1.get("state") or {}).get("case_status") == "active"
        and (s1.get("diagnostic_scope") or {}).get("resolved") is False
        and (s1.get("state") or {}).get("resolved_at") is None,
        "s1_scope_goal_and_strength": (s1.get("diagnostic_scope") or {}).get("task_goal")
        == "automatic_return"
        and (s1.get("diagnostic_scope") or {}).get("allowed_conclusion_strength")
        in {"task_goal_failed", "scoped_positive_only"},
        "s1_root_cause_unconfirmed": ((s1.get("diagnostic_scope") or {}).get("root_cause") or {}).get(
            "status"
        )
        == "unconfirmed",
        "s1_snapshot_has_scope": bool(s1.get("snapshot"))
        and bool((s1.get("snapshot") or {}).get("diagnostic_scope")),
        "s2_manual_works_auto_unverified": s2_obs.get("manual_charging") == "works"
        and "automatic_return" not in s2_obs,
        "s2_active_not_resolved": (s2.get("state") or {}).get("case_status") == "active"
        and (s2.get("diagnostic_scope") or {}).get("resolved") is False,
        "s2_scope_scoped_positive_only": (s2.get("diagnostic_scope") or {}).get(
            "allowed_conclusion_strength"
        )
        == "scoped_positive_only"
        and "automatic_return"
        in ((s2.get("diagnostic_scope") or {}).get("unverified_capabilities") or []),
        "s3_manual_and_auto_fail": s3_obs.get("manual_charging") == "fails"
        and s3_obs.get("automatic_return") == "fails",
        "s3_active_not_resolved": (s3.get("state") or {}).get("case_status") == "active"
        and (s3.get("diagnostic_scope") or {}).get("resolved") is False,
        "s4_t1_auto_fails_active": s4_t1_obs.get("automatic_return") == "fails"
        and (s4[0].get("state") or {}).get("case_status") == "active",
        "s4_t2_auto_works_resolved": s4_t2_obs.get("automatic_return") == "works"
        and (s4[1].get("state") or {}).get("case_status") == "resolved"
        and s4_t2_scope.get("allowed_conclusion_strength") == "resolved"
        and s4_t2_scope.get("resolved") is True,
        "all_turns_answer_nonempty": all(int(turn.get("answer_chars") or 0) > 0 for turn in all_turns),
        "no_turn_error": all(not turn.get("error") for turn in all_turns),
        "turn_count_five": len(all_turns) == 5,
        "artifact_check_ok": bool((payload.get("artifact_check") or {}).get("ok")),
        "source_artifact_hash_match": bool(payload.get("source_artifact_hash_match")),
        "freeze_written_before_model_calls": bool(payload.get("freeze_written_before_model_calls")),
        "port_released": bool(payload.get("port_released")),
    }
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--out", default=str(ROOT / "outputs" / "diagnostic-scope-smoke-v2"))
    parser.add_argument("--env-file", default=str(ROOT.parent / "my_agent_harness" / ".env"))
    parser.add_argument("--port", type=int, default=8949)
    args = parser.parse_args()

    dl.PORT = int(args.port)
    run_dir = Path(args.out).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty run directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    spans_dir = run_dir / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    provider_records_dir = run_dir / "provider-records"
    provider_records_dir.mkdir(parents=True, exist_ok=True)
    env_file = Path(args.env_file)
    if not env_file.is_file():
        raise SystemExit("environment file not found (values are not printed)")

    built = dl._load_artifact(Path(args.artifact_root))
    source_hashes, artifact_hashes, hash_match = _hashes(Path(built["artifact_root"]))
    freeze = {
        "schema_version": "diagnostic-scope-smoke-freeze/v1",
        "frozen_at": _now_iso(),
        "purpose": "development diagnostic-scope smoke; structural oracle only, no semantic scoring",
        "artifact": built,
        "source_hashes": source_hashes,
        "artifact_hashes": artifact_hashes,
        "source_artifact_hash_match": hash_match,
        "port": dl.PORT,
        "expected_sessions": len(CASES),
        "expected_turn_count": sum(len(item["questions"]) for item in CASES.values()),
        "cases": CASES,
        "program_checks": "state/data only; answer causality is not scored by this file",
        "not_holdout": True,
        "not_success_rate": True,
    }
    freeze_path = run_dir / "input-freeze.json"
    freeze_path.write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    payload: dict[str, Any] = {
        "schema_version": "diagnostic-scope-smoke/v1",
        "run_id": run_dir.name,
        "started_at": _now_iso(),
        "port": dl.PORT,
        "artifact": built,
        "artifact_check": None,
        "source_hashes": source_hashes,
        "artifact_hashes": artifact_hashes,
        "source_artifact_hash_match": hash_match,
        "freeze_path": str(freeze_path),
        "freeze_written_before_model_calls": True,
        "env_file_used": str(env_file),
        "env_values_read_or_logged": False,
        "sessions": {},
        "semantic_quality": {
            "status": "not_scored",
            "note": "Program checks are structural. Answer causality requires human/Judge evaluation.",
        },
        "error": None,
    }

    instance = dl._write_instance(run_dir, built, env_file)
    server = dl.Server(built, instance, run_dir / "server.log", spans_dir, provider_records_dir)
    from core.observability import latency

    try:
        # External-cwd artifact check runs before the hidden service/model.
        payload["artifact_check"] = _external_check(Path(built["artifact_root"]))
        payload["health"] = server.start()
        for case_id, spec in CASES.items():
            payload["sessions"][case_id] = _run_session(
                run_dir, case_id, list(spec["questions"]), latency
            )
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        server.stop()
        payload["finished_at"] = _now_iso()
        payload["port_released"] = not dl._port_in_use("127.0.0.1", dl.PORT)
        payload["server_log"] = str(run_dir / "server.log")

    all_turns = [
        turn
        for session in payload.get("sessions", {}).values()
        for turn in session.get("turns") or []
    ]
    extraction_totals: dict[str, int] = {}
    semantic_accepted_turns = 0
    semantic_validation_errors: list[str] = []
    for turn in all_turns:
        for key, value in (turn.get("observation_extraction_counts") or {}).items():
            extraction_totals[key] = extraction_totals.get(key, 0) + int(value)
        if int(turn.get("semantic_observations_accepted") or 0) > 0:
            semantic_accepted_turns += 1
        for error in (turn.get("semantic") or {}).get("validation_errors") or []:
            semantic_validation_errors.append(str(error))
    payload["observation_extraction_totals"] = extraction_totals
    payload["semantic_accepted_turns"] = semantic_accepted_turns
    payload["semantic_validation_errors"] = semantic_validation_errors
    payload["checks"] = _checks(payload)
    semantic_checks = {
        "semantic_accepted_at_least_one_turn": int(payload.get("semantic_accepted_turns") or 0) >= 1,
        "semantic_observation_is_effective_evidence": int(
            (payload.get("observation_extraction_totals") or {}).get("semantic") or 0
        )
        >= 1,
        "no_capability_observation_missing_quote": not any(
            "missing_evidence_quote" in error
            for error in payload.get("semantic_validation_errors") or []
        ),
    }
    payload["semantic_checks"] = semantic_checks
    payload["semantic_passed"] = all(semantic_checks.values())
    payload["passed"] = all(payload["checks"].values()) and not payload.get("error")
    (run_dir / "run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "run": str(run_dir),
                "artifact": built.get("artifact_id"),
                "port_released": payload.get("port_released"),
                "passed_structural_checks": payload.get("passed"),
                "semantic_passed": payload.get("semantic_passed"),
                "semantic_checks": payload.get("semantic_checks"),
                "observation_extraction_totals": payload.get("observation_extraction_totals"),
                "error": payload.get("error"),
                "checks": payload.get("checks"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
