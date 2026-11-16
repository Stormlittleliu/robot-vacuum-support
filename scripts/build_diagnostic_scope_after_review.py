# -*- coding: utf-8 -*-
"""Prepare the four-case diagnostic-scope after-review workspace.

This script is a *preparation* step: it does not call the Judge, the product
model, or a service.  It accepts the source smoke run written by task A at
``outputs/diagnostic-scope-smoke-v2/run.json`` (or the v1 default) and projects the four fixed
sessions into an isolated after-review workspace.

If the source run is not present yet, or is present but incomplete, the script
writes a ``prepared_waiting_source`` report and does not invent cases or freeze
anything.  When all four sessions are completed and every turn has the required
snapshot/trace/transcript fields, it builds the four cases (1/1/1/2 turns),
validates the evidence bundles, and writes a pre-Judge freeze.

It deliberately does NOT run the Judge.  The next stage invokes the existing
generic DSH Judge CLI with ``--judge-run-id jr-diagnostic-after-20261114-01``.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs" / "diagnostic-scope-smoke-v1"
SOURCE_RUN = SOURCE / "run.json"
INPUT_FREEZE = SOURCE / "input-freeze.json"
OUT = ROOT / "outputs" / "diagnostic-scope-after-review-v2"
REPORT = ROOT / "outputs" / "diagnostic-scope-after-review-report.json"
DSH_JUDGE = ROOT.parent / "dsh-judge"

RUBRIC_VERSION = "diagnostic-scope-v2"
SUITE_ID = "robot-support-diagnostic-scope-after-review-v2"
SUITE_REVISION = "v2-after"
JUDGE_ID = "jr-diagnostic-after-20261114-01"
RUN_PREFIX = "ds-after-development-20261114-"


def set_paths(
    *,
    source_root: str | Path | None = None,
    out: str | Path | None = None,
    report: str | Path | None = None,
    judge_run_id: str | None = None,
) -> None:
    """Set CLI paths explicitly while keeping the old defaults intact."""
    global SOURCE, SOURCE_RUN, INPUT_FREEZE, OUT, REPORT, JUDGE_ID
    if source_root is not None:
        SOURCE = Path(source_root).resolve()
        SOURCE_RUN = SOURCE / "run.json"
        INPUT_FREEZE = SOURCE / "input-freeze.json"
    if out is not None:
        OUT = Path(out).resolve()
    if report is not None:
        REPORT = Path(report).resolve()
    if judge_run_id is not None:
        JUDGE_ID = str(judge_run_id)

AFTER_SPECS: list[dict[str, Any]] = [
    {
        "id": "ds-live-manual-works-auto-fails-01",
        "family_id": "fam-ds-live-manual-works-auto-fails",
        "turns": 1,
        "title": "手动能充但自动回充仍失败：不能证明触点问题已解决",
        "questions": [
            "自动回充还是失败，但我手动把机器人放到充电座上就能充电。是不是证明触点问题已经解决，接下来不用管了？"
        ],
        "turn_contexts": [
            {
                "observation": "the user reports that manual placement on the dock charges while automatic return still fails",
                "task_goal": "automatic return / automatic recharge recovery",
                "causal_question": "whether manual charging proves the contact issue is resolved or is the only cause",
                "next_observation": "test automatic return without manual placement; if it still fails, inspect sensor/IR/dock/power alternatives",
                "closure": "automatic return is directly observed to work",
                "positive": "A report that the robot autonomously returns and charges may confirm automatic recovery, but manual charging alone does not.",
            }
        ],
    },
    {
        "id": "ds-live-clearance-manual-only-01",
        "family_id": "fam-ds-live-clearance-manual-only",
        "turns": 1,
        "title": "清障后手动能充但尚未试自动回充：不能确定就是遮挡",
        "questions": [
            "我已经清了充电座周围，手动放回可以充电，但还没试过让它自己回去。现在能确定就是遮挡造成的吗？"
        ],
        "turn_contexts": [
            {
                "observation": "the user cleared the area around the dock and manual placement charges, but automatic return has not been tried",
                "task_goal": "automatic return / automatic recharge recovery",
                "causal_question": "whether clearance plus manual charging proves obstruction is the sole cause",
                "next_observation": "run automatic return without manual placement; if it still fails, check sensor/IR/signal/power alternatives",
                "closure": "automatic return is directly observed to work and alternatives are accounted for",
                "positive": "An explicit report that the robot autonomously returns and charges may confirm recovery, but manual charging alone does not.",
            }
        ],
    },
    {
        "id": "ds-live-manual-and-auto-fail-01",
        "family_id": "fam-ds-live-manual-and-auto-fail",
        "turns": 1,
        "title": "自动与手动都失败：先做能鉴别的检查，不先下唯一原因",
        "questions": [
            "机器人自己回不到充电座，我把它放上去也充不上电。该先确认什么？"
        ],
        "turn_contexts": [
            {
                "observation": "the user reports that both automatic return and manual placement charging fail",
                "task_goal": "a discriminating next check, not a premature cause conclusion",
                "causal_question": "which candidate cause the first check can separate (power/contact/dock path vs return sensor/IR)",
                "next_observation": "confirm whether the robot powers on and whether the dock/contact path shows charging at all before naming a cause",
                "closure": "the observed failure mode is explained by evidence and a discriminating check has narrowed the cause",
                "positive": "A later explicit report that the robot autonomously returns and charges may confirm automatic recovery; it still does not name a unique faulty part without evidence.",
            }
        ],
    },
    {
        "id": "ds-live-auto-recovered-01",
        "family_id": "fam-ds-live-auto-recovered",
        "turns": 2,
        "title": "自动回充从失败到明确自动恢复：可确认恢复，仍不能无证据说唯一原因",
        "questions": [
            "机器人自动回充失败，回不到充电座。",
            "现在它能从房间自己找到充电座，并且已经开始充电了。",
        ],
        "turn_contexts": [
            {
                "observation": "the user reports that automatic return fails; no local result has been reported yet",
                "task_goal": "automatic return / automatic recharge recovery",
                "causal_question": "whether the answer names a unique faulty part/cause without evidence",
                "next_observation": "ask for one automatic-return observation or a discriminating check, not a manual-placement substitute",
                "closure": "automatic return is directly observed to work",
                "positive": "A later explicit report that the robot autonomously finds the dock and starts charging may confirm recovery.",
            },
            {
                "observation": "the user explicitly reports that the robot autonomously found the dock from the room and started charging",
                "task_goal": "automatic return / automatic recharge recovery (now evidenced)",
                "causal_question": "whether the answer additionally names a unique faulty part/cause without component-level evidence",
                "next_observation": "confirm/monitor the observed recovery; if a cause is claimed, ask for the missing component-level evidence",
                "closure": "the explicit autonomous dock-find + charging observation supports recovery closure; the cause remains unconfirmed",
                "positive": "This is the positive oracle: explicit autonomous docking + charging may confirm automatic capability recovery. It still does not prove which part was faulty without component-level evidence.",
            },
        ],
    },
]

AFTER_SCORING = {
    "automatic": [
        "dataset.schema",
        "dataset.counts_4",
        "dataset.turn_counts_1_1_1_2",
        "dataset.split_disjoint",
        "evidence.real_source_projection",
        "review.pending_human",
    ],
    "human": [
        "diagnostic.observation_scope",
        "diagnostic.causal_exclusivity",
        "diagnostic.condition_vs_fact",
        "diagnostic.discriminative_observation",
        "diagnostic.local_to_global_closure",
    ],
    "denominator": "qa",
    "expected_sources_mode": "current_or_legitimate_history",
    "state_score": "not_applicable_program_checks_are_structural",
}

FORBIDDEN_BUNDLE_STRINGS = (
    "candidate_needs_counterexample",
    "manager_concern",
    "judge_context",
    "criteria_judged_passed",
    "jmc-s1-exclusive-causal-conclusion",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


base = _load_module("diagnostic_scope_base", ROOT / "scripts" / "build_diagnostic_scope_review.py")
projection = _load_module("action_answer_projection", ROOT / "scripts" / "build_action_answer_review.py")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def artifact_short_id(artifact_id: str) -> str:
    text = str(artifact_id or "").strip()
    if not text:
        return "unknown-artifact"
    tail = re.split(r"[-_]", text)[-1]
    if re.fullmatch(r"[0-9a-fA-F]{6,}", tail):
        return tail[:10]
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def after_run_id(artifact_id: str) -> str:
    return f"{RUN_PREFIX}{artifact_short_id(artifact_id)}"


def criteria_for_after_case(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return base.criteria_for_actual_turns(spec, int(spec["turns"]), spec.get("turn_contexts"))


def expected_summary() -> dict[str, Any]:
    return {
        "cases": len(AFTER_SPECS),
        "turns": sum(int(spec["turns"]) for spec in AFTER_SPECS),
        "case_ids": [spec["id"] for spec in AFTER_SPECS],
        "turn_counts": {spec["id"]: int(spec["turns"]) for spec in AFTER_SPECS},
        "rubric_version": RUBRIC_VERSION,
    }


def iter_sessions(run: dict[str, Any]):
    sessions = run.get("sessions")
    if isinstance(sessions, dict):
        for key, value in sessions.items():
            if isinstance(value, dict):
                yield key, value
    elif isinstance(sessions, list):
        for value in sessions:
            if isinstance(value, dict):
                yield value.get("session_key") or value.get("key") or value.get("session_id"), value


def session_id_of(key: str | None, entry: dict[str, Any]) -> str:
    return str(entry.get("session_id") or entry.get("id") or key or "")


def find_session(run: dict[str, Any], wanted_id: str) -> tuple[str | None, dict[str, Any] | None]:
    for key, entry in iter_sessions(run):
        # The fixed ids are the run's session keys; the runtime session_id is
        # the s-... value used for transcript/trace paths. Accept either.
        if key == wanted_id or session_id_of(key, entry) == wanted_id:
            return key, entry
    return None, None


def _field(turn: dict[str, Any], key: str) -> Any:
    if key in turn:
        return turn[key]
    snapshot = turn.get("snapshot")
    if isinstance(snapshot, dict) and key in snapshot:
        return snapshot[key]
    state = turn.get("state")
    if isinstance(state, dict) and key in state:
        return state[key]
    return None


def _load_session_io(session_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path | None, Path | None, list[str]]:
    missing: list[str] = []
    session_dir = SOURCE / "instance-storage" / "sessions" / session_id
    transcript_path = session_dir / "transcript.jsonl"
    trace_paths = sorted((session_dir / "outputs" / "traces").glob("trace_*.jsonl"))
    transcript: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    if not transcript_path.exists():
        missing.append(f"{session_id}: transcript.jsonl missing")
    else:
        transcript = read_jsonl(transcript_path)
        for line_number, row in enumerate(transcript, 1):
            row["_line"] = line_number
    if not trace_paths:
        missing.append(f"{session_id}: trace_*.jsonl missing")
    else:
        trace = read_jsonl(trace_paths[0])
        for line_number, row in enumerate(trace, 1):
            row["_line"] = line_number
    return transcript, trace, transcript_path if transcript_path.exists() else None, trace_paths[0] if trace_paths else None, missing


def _parse_run_started_at(run: dict[str, Any], sessions: list[dict[str, Any]]) -> Any:
    for key in ("started_at", "created_at", "start_time"):
        if run.get(key) is not None:
            return run[key]
    times = []
    for entry in sessions:
        for key in ("created_at", "started_at"):
            if entry.get(key) is not None:
                times.append(entry[key])
    return min(times) if times else None


def _iso_or_raw(value: Any) -> Any:
    return value


def inspect_source() -> dict[str, Any]:
    expected = expected_summary()
    report: dict[str, Any] = {
        "source_dir": str(SOURCE),
        "source_run": str(SOURCE_RUN),
        "input_freeze": str(INPUT_FREEZE),
        "source_present": SOURCE_RUN.exists(),
        "input_freeze_present": INPUT_FREEZE.exists(),
        "source_complete": False,
        "missing": [],
        "missing_fields": [],
        "artifact_id": None,
        "source_run_id": None,
        "run_id": None,
        "expected": expected,
        "sessions": {},
        "source_hashes": {},
        "input_freeze_sha256": None,
        "timing": {},
    }
    if not SOURCE_RUN.exists():
        report["missing"].append(_rel(SOURCE_RUN))
        if not INPUT_FREEZE.exists():
            report["missing"].append(_rel(INPUT_FREEZE))
        return report
    run = read_json(SOURCE_RUN)
    freeze = read_json(INPUT_FREEZE) if INPUT_FREEZE.exists() else {}
    report["source_quality"] = {
        "run_id": run.get("run_id"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "passed": run.get("passed"),
        "checks": run.get("checks"),
        "semantic_checks": run.get("semantic_checks"),
        "semantic_passed": run.get("semantic_passed"),
        "observation_extraction_totals": run.get("observation_extraction_totals"),
        "semantic_accepted_turns": run.get("semantic_accepted_turns"),
        "semantic_validation_errors": run.get("semantic_validation_errors"),
        "port_released": run.get("port_released"),
        "freeze_written_before_model_calls": run.get("freeze_written_before_model_calls"),
    }
    if not INPUT_FREEZE.exists():
        report["missing"].append(_rel(INPUT_FREEZE))
    report["source_hashes"]["run_json_sha256"] = sha256_file(SOURCE_RUN)
    report["input_freeze_sha256"] = sha256_file(INPUT_FREEZE) if INPUT_FREEZE.exists() else None
    if INPUT_FREEZE.exists():
        report["source_hashes"]["input_freeze_sha256"] = report["input_freeze_sha256"]

    run_artifact = (run.get("artifact") or {}).get("artifact_id")
    freeze_artifact = ((freeze.get("artifact") or {}).get("artifact_id"))
    artifact_id = run_artifact or freeze_artifact
    report["artifact_id"] = artifact_id
    report["source_run_id"] = run.get("run_id") or run.get("schema_version") or "diagnostic-scope-smoke/v1"
    report["run_id"] = after_run_id(artifact_id) if artifact_id else None
    if not artifact_id:
        report["missing_fields"].append("artifact_id")
    if freeze:
        if freeze.get("expected_sessions") not in (None, 4):
            report["missing_fields"].append(f"input_freeze.expected_sessions={freeze.get('expected_sessions')}")
        if freeze.get("expected_turn_count") not in (None, 5):
            report["missing_fields"].append(f"input_freeze.expected_turn_count={freeze.get('expected_turn_count')}")

    if run.get("error") not in (None, False):
        report["missing_fields"].append("run.error is not empty")
    if run.get("status") not in (None, "completed"):
        report["missing_fields"].append(f"run.status={run.get('status')}")

    entries = list(iter_sessions(run))
    report["timing"] = {
        "input_freeze_frozen_at": freeze.get("frozen_at"),
        "run_started_at": _iso_or_raw(_parse_run_started_at(run, [entry for _, entry in entries])),
    }
    if freeze.get("frozen_at") and report["timing"]["run_started_at"] is not None:
        try:
            frozen_at = datetime.fromisoformat(str(freeze["frozen_at"]).replace("Z", "+00:00"))
            started = report["timing"]["run_started_at"]
            if isinstance(started, (int, float)):
                started_at = datetime.fromtimestamp(float(started)).astimezone()
            else:
                started_at = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            report["timing"]["input_freeze_before_model"] = frozen_at <= started_at
        except Exception:
            report["timing"]["input_freeze_before_model"] = None
    else:
        report["timing"]["input_freeze_before_model"] = None

    freeze_cases = freeze.get("cases") or {}
    for spec in AFTER_SPECS:
        case_id = spec["id"]
        key, entry = find_session(run, case_id)
        case_info: dict[str, Any] = {
            "case_id": case_id,
            "session_key": key,
            "session_id": None,
            "found": entry is not None,
            "turn_count_expected": int(spec["turns"]),
            "turn_count_actual": None,
            "missing_fields": [],
            "transcript_sha256": None,
            "trace_sha256": None,
        }
        if entry is None:
            case_info["missing_fields"].append("session not found in run.sessions")
            report["sessions"][case_id] = case_info
            report["missing_fields"].append(f"{case_id}: session not found")
            continue
        actual_session_id = session_id_of(key, entry)
        case_info["session_id"] = actual_session_id
        turns = entry.get("turns") or []
        case_info["turn_count_actual"] = len(turns)
        if len(turns) != int(spec["turns"]):
            case_info["missing_fields"].append(f"turn_count {len(turns)} != {spec['turns']}")
        transcript, trace, transcript_path, trace_path, io_missing = _load_session_io(actual_session_id)
        case_info["missing_fields"].extend(io_missing)
        if transcript_path:
            case_info["transcript_sha256"] = sha256_file(transcript_path)
        if trace_path:
            case_info["trace_sha256"] = sha256_file(trace_path)
        users = {int(row.get("turn", 0)): row for row in transcript if row.get("role") == "user"}
        assistants = {int(row.get("turn", 0)): row for row in transcript if row.get("role") == "assistant"}
        for position, run_turn in enumerate(turns, 1):
            if not isinstance(run_turn, dict):
                case_info["missing_fields"].append(f"turn {position}: not an object")
                continue
            turn_index = run_turn.get("index", run_turn.get("turn"))
            if turn_index not in (None, position):
                case_info["missing_fields"].append(f"turn {position}: index={turn_index}")
            content = run_turn.get("question") or run_turn.get("content") or (users.get(position) or {}).get("content")
            answer = run_turn.get("answer") or run_turn.get("response") or (assistants.get(position) or {}).get("answer")
            if not str(content or "").strip():
                case_info["missing_fields"].append(f"turn {position}: user content missing")
            if not str(answer or "").strip():
                case_info["missing_fields"].append(f"turn {position}: answer missing")
            for field in ("state", "semantic", "observations", "diagnostic_scope"):
                value = _field(run_turn, field)
                if value is None:
                    case_info["missing_fields"].append(f"turn {position}: {field} missing")
            if run_turn.get("request_id") is None:
                case_info["missing_fields"].append(f"turn {position}: request_id missing")
            if run_turn.get("http_status") not in (None, 200):
                case_info["missing_fields"].append(f"turn {position}: http_status={run_turn.get('http_status')}")
            if run_turn.get("status") not in (None, "completed", "ok"):
                case_info["missing_fields"].append(f"turn {position}: status={run_turn.get('status')}")
            if position in users and position in assistants:
                if users[position].get("content") != content:
                    case_info["missing_fields"].append(f"turn {position}: transcript user content mismatch")
                if assistants[position].get("content") != answer:
                    case_info["missing_fields"].append(f"turn {position}: transcript answer mismatch")
        if spec.get("questions") and freeze_cases.get(case_id, {}).get("questions"):
            if list(freeze_cases[case_id]["questions"]) != list(spec["questions"]):
                case_info["missing_fields"].append("input_freeze questions mismatch")
        report["sessions"][case_id] = case_info
        if case_info["missing_fields"]:
            report["missing_fields"].append(f"{case_id}: " + "; ".join(case_info["missing_fields"]))
    report["source_complete"] = not report["missing"] and not report["missing_fields"]
    return report


def _normalize_trace_group(events: list[dict[str, Any]], trace_id: str) -> dict[str, Any] | None:
    group = [event for event in events if str(event.get("trace_id")) == trace_id]
    if not group:
        return None
    for position, event in enumerate(group, 1):
        event["_line"] = position
    counts: dict[str, int] = {}
    for event in group:
        kind = str(event.get("event") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "trace_id": trace_id,
        "thread_id": group[0].get("thread_id"),
        "run_ids": sorted({str(row.get("run_id")) for row in group if row.get("run_id")}),
        "parent_run_ids": sorted({str(row.get("parent_run_id")) for row in group if row.get("parent_run_id")}),
        "started_at": min((str(row.get("ts")) for row in group if row.get("ts")), default=None),
        "ended_at": max((str(row.get("ts")) for row in group if row.get("ts")), default=None),
        "event_counts": dict(sorted(counts.items())),
        "event_lines": [row.get("_line") for row in group],
        "events": [
            {
                "event": row.get("event"),
                "ts": row.get("ts"),
                "run_id": row.get("run_id"),
                "parent_run_id": row.get("parent_run_id"),
                "node": row.get("node"),
                "model": row.get("model"),
                "tool": row.get("tool"),
                "tool_call_id": row.get("tool_call_id"),
                "tool_span_id": row.get("tool_span_id"),
                "call_span_id": row.get("call_span_id"),
                "status": row.get("status"),
                "event_line": row.get("_line"),
            }
            for row in group
        ],
        "tool_outputs": projection.dedupe_tool_outputs(group),
        "source_kind": "source_run_turn_trace",
        "not_session_cumulative": True,
    }


def _turn_trace(run_turn: dict[str, Any], trace_events: list[dict[str, Any]], user_at: Any, assistant_at: Any) -> dict[str, Any] | None:
    source_trace = run_turn.get("trace")
    if isinstance(source_trace, dict) and source_trace.get("events"):
        events = copy.deepcopy(source_trace["events"])
        for position, event in enumerate(events, 1):
            event["_line"] = position
        if user_at is not None and assistant_at is not None:
            sliced = projection.trace_slice(events, float(user_at), float(assistant_at))
            if sliced:
                return sliced
        trace_ids = [str(event.get("trace_id")) for event in events if event.get("trace_id")]
        if trace_ids:
            # The turn trace may contain one primary trace plus nested spans;
            # pick the trace id with the most events so the adapter sees one
            # coherent turn-level trace.
            dominant = max(set(trace_ids), key=trace_ids.count)
            return _normalize_trace_group(events, dominant)
    if user_at is not None and assistant_at is not None:
        return projection.trace_slice(trace_events, float(user_at), float(assistant_at))
    return None


def project_session(spec: dict[str, Any], inspection: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    run = read_json(SOURCE_RUN)
    freeze = read_json(INPUT_FREEZE)
    key, entry = find_session(run, spec["id"])
    if entry is None:
        raise ValueError(f"{spec['id']}: session not found")
    sid = session_id_of(key, entry)
    transcript, trace_events, transcript_path, trace_path, missing = _load_session_io(sid)
    if missing:
        raise ValueError("; ".join(missing))
    users = {int(row.get("turn", 0)): row for row in transcript if row.get("role") == "user"}
    assistants = {int(row.get("turn", 0)): row for row in transcript if row.get("role") == "assistant"}
    previous_retrieval: list[dict[str, Any]] = []
    recorded_turns: list[dict[str, Any]] = []
    turn_map: list[dict[str, Any]] = []
    for position, run_turn in enumerate(entry.get("turns") or [], 1):
        user = users.get(position, {})
        assistant = assistants.get(position, {})
        content = run_turn.get("question") or run_turn.get("content") or user.get("content")
        answer = run_turn.get("answer") or run_turn.get("response") or assistant.get("content")
        user_at = run_turn.get("user_at") or user.get("at")
        assistant_at = run_turn.get("assistant_at") or assistant.get("at")
        sliced = _turn_trace(run_turn, trace_events, user_at, assistant_at)
        if not sliced:
            raise ValueError(f"{spec['id']} turn {position}: missing turn trace slice")
        if not sliced.get("trace_id"):
            raise ValueError(f"{spec['id']} turn {position}: turn trace has no trace_id")
        if not sliced.get("events"):
            raise ValueError(f"{spec['id']} turn {position}: turn trace has no events")
        current = (sliced.get("tool_outputs") or []) if isinstance(sliced, dict) else []
        retrieval = run_turn.get("retrieval")
        if not isinstance(retrieval, dict):
            retrieval = {
                "current": current,
                "history": previous_retrieval,
                "current_status": "retrieved" if current else "not_retrieved",
                "history_note": "Earlier turns' real tool outputs retained for legitimate historical reuse; no forced RAG on recall.",
            }
        state = _field(run_turn, "state")
        semantic = _field(run_turn, "semantic")
        observations = _field(run_turn, "observations")
        diagnostic_scope = _field(run_turn, "diagnostic_scope")
        source_snapshot = run_turn.get("snapshot")
        if isinstance(source_snapshot, dict) and all(
            key in source_snapshot for key in ("state", "semantic", "observations", "diagnostic_scope")
        ):
            snapshot = copy.deepcopy(source_snapshot)
        else:
            snapshot = {
                "state": copy.deepcopy(state),
                "semantic": copy.deepcopy(semantic),
                "observations": copy.deepcopy(observations),
                "diagnostic_scope": copy.deepcopy(diagnostic_scope),
            }
        recorded_turns.append({
            "index": position,
            "role": "user",
            "content": content,
            "answer": answer,
            # The generic recorded-run adapter takes one state object per turn.
            # Use the full per-turn snapshot (raw state + semantic + observations
            # + diagnostic_scope) and retain the raw state separately.
            "state": copy.deepcopy(snapshot),
            "raw_state": copy.deepcopy(state),
            "semantic": copy.deepcopy(semantic),
            "observations": copy.deepcopy(observations),
            "diagnostic_scope": copy.deepcopy(diagnostic_scope),
            "snapshot": snapshot,
            "trace": copy.deepcopy(sliced),
            "retrieval": copy.deepcopy(retrieval),
            "request_id": run_turn.get("request_id"),
            "message_id": user.get("message_id") or run_turn.get("message_id"),
            "assistant_message_id": assistant.get("message_id"),
            "user_at": user_at,
            "assistant_at": assistant_at,
            "errors": run_turn.get("errors") or [],
        })
        turn_map.append({
            "turn": position,
            "request_id": run_turn.get("request_id"),
            "trace_id": sliced.get("trace_id") if isinstance(sliced, dict) else None,
            "trace_run_ids": sliced.get("run_ids") if isinstance(sliced, dict) else None,
            "trace_event_lines": sliced.get("event_lines") if isinstance(sliced, dict) else None,
            "transcript_user_line": user.get("_line"),
            "transcript_assistant_line": assistant.get("_line"),
            "user_message_id": user.get("message_id"),
            "assistant_message_id": assistant.get("message_id"),
        })
        previous_retrieval = previous_retrieval + [{**record, "source_turn": position} for record in current]

    artifact_id = inspection["artifact_id"]
    source_hashes = {
        "run_json_sha256": inspection["source_hashes"].get("run_json_sha256"),
        "input_freeze_sha256": inspection.get("input_freeze_sha256"),
        "transcript_sha256": sha256_file(transcript_path) if transcript_path else None,
        "trace_sha256": sha256_file(trace_path) if trace_path else None,
    }
    recorded = {
        "source": {
            "kind": "source_run_raw_session",
            "path": str(SOURCE),
            "source_case_id": spec["id"],
            "source_run_id": inspection.get("source_run_id"),
            "session_id": sid,
            "artifact_id": artifact_id,
        },
        "turns": recorded_turns,
    }
    criteria = criteria_for_after_case(spec)
    case, result = base.build_from_recorded_case(
        recorded,
        new_case_id=spec["id"],
        family_id=spec["family_id"],
        title=spec["title"],
        spec={**spec, "turn_contexts": spec.get("turn_contexts")},
        criteria=criteria,
        scoring=AFTER_SCORING,
        origin_extra={
            "kind": "diagnostic_scope_after_projection",
            "projection_from": "source_run_raw_session",
            "source_smoke_run": _rel(SOURCE_RUN),
            "source_input_freeze": _rel(INPUT_FREEZE),
            "input_freeze_sha256": inspection.get("input_freeze_sha256"),
            "not_the_frozen_v1_split": True,
            "not_baseline_cases": True,
            "suite_revision": SUITE_REVISION,
        },
        source_extra=source_hashes,
    )
    mapping = {
        "case_id": spec["id"],
        "session_key": key,
        "session_id": sid,
        "turns": turn_map,
        "source_hashes": source_hashes,
    }
    return case, result, mapping


def _bundle_checks(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = {}
    problems = []
    case_hashes = {case["id"]: base.case_hash(case) for case in cases}
    for case in cases:
        path = OUT / "evidence_bundles" / f"{case['id']}.json"
        if not path.exists():
            problems.append(f"{case['id']}: bundle missing")
            continue
        bundle = read_json(path)
        rubric = case["answer_review_rubric"]
        catalog = bundle.get("task", {}).get("issue_catalog") or []
        if bundle.get("case_hash") != case_hashes[case["id"]]:
            problems.append(f"{case['id']}: case_hash mismatch")
        if bundle.get("case_id") != case["id"]:
            problems.append(f"{case['id']}: case_id mismatch")
        if len(catalog) != len(rubric):
            problems.append(f"{case['id']}: catalog {len(catalog)} != rubric {len(rubric)}")
        if {row.get("criterion_id") for row in catalog} != {row.get("criterion_id") for row in rubric}:
            problems.append(f"{case['id']}: catalog ids mismatch")
        if any(not row.get("evidence_item_ids") for row in catalog):
            problems.append(f"{case['id']}: empty evidence ids")
        if bundle.get("human_notes") not in ([], None):
            problems.append(f"{case['id']}: human notes present")
        if any(row.get("expected") for row in bundle.get("task", {}).get("expected", [])):
            problems.append(f"{case['id']}: expected oracle present")
        raw = json.dumps(bundle, ensure_ascii=False)
        for needle in FORBIDDEN_BUNDLE_STRINGS:
            if needle in raw:
                problems.append(f"{case['id']}: forbidden string {needle!r}")
        rows[case["id"]] = {
            "bundle_file": f"evidence_bundles/{case['id']}.json",
            "bundle_sha256": sha256_file(path),
            "input_hash": bundle.get("input_hash"),
            "evidence_count": len(bundle.get("evidence") or []),
            "catalog_count": len(catalog),
            "actual_turns": len(bundle.get("conversation", {}).get("actual_turns") or []),
        }
    return {"ok": not problems, "problems": problems, "cases": rows, "case_hashes": case_hashes}


def _run_bundles() -> dict[str, Any]:
    cmd = [
        "node", str(DSH_JUDGE / "scripts" / "build-recorded-bundles.js"),
        "--workspace", str(OUT),
        "--out", str(OUT / "evidence_bundles"),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(cmd, cwd=str(DSH_JUDGE), capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=flags)
    (OUT / "judge_run").mkdir(parents=True, exist_ok=True)
    (OUT / "judge_run" / "build-bundles.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
    (OUT / "judge_run" / "build-bundles.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0:
        raise SystemExit(f"bundle build failed ({completed.returncode}): {completed.stderr[-2000:]}")
    return {"command": cmd, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def judge_command(run_id: str) -> list[str]:
    return [
        "node", "bin/dsh-judge.js", "run",
        "--bundle-dir", str(OUT / "evidence_bundles"),
        "--output-dir", str(OUT / "judge_results" / JUDGE_ID),
        "--run", run_id,
        "--judge-run-id", JUDGE_ID,
        "--concurrency", "2",
        "--timeout", "480000",
    ]


def build_from_source(inspection: dict[str, Any]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    for spec in AFTER_SPECS:
        case, result, mapping = project_session(spec, inspection)
        cases.append(case)
        results.append(result)
        mappings.append(mapping)
    if len(cases) != 4:
        raise SystemExit(f"expected 4 after cases, got {len(cases)}")
    if [len(case["turns"]) for case in cases] != [1, 1, 1, 2]:
        raise SystemExit(f"unexpected turn counts: {[len(case['turns']) for case in cases]}")
    if sum(len(case["turns"]) for case in cases) != 5:
        raise SystemExit("expected 5 total turns")
    if any(case["id"].startswith("asc-") for case in cases):
        raise SystemExit("baseline case id leaked into after suite")

    run_id = inspection["run_id"]
    artifact_id = inspection["artifact_id"]
    suite = {
        "schema_version": "robot-support-eval/v1",
        "suite_id": SUITE_ID,
        "revision": SUITE_REVISION,
        "cases_file": "cases.jsonl",
        "split_file": "split.json",
        "counts": {"development": 4, "holdout": 0},
        "rubric_version": RUBRIC_VERSION,
        "standard_kind": "manager_driven_posthoc_development_standard",
        "not_independent_blind_benchmark": True,
        "source_snapshot": {
            "generated_at": now_iso(),
            "source_run": _rel(SOURCE_RUN),
            "source_input_freeze": _rel(INPUT_FREEZE),
            "source_run_sha256": inspection["source_hashes"].get("run_json_sha256"),
            "input_freeze_sha256": inspection.get("input_freeze_sha256"),
            "artifact_id": artifact_id,
            "sessions": 4,
            "turns": 5,
            "turn_counts": {spec["id"]: int(spec["turns"]) for spec in AFTER_SPECS},
            "note": "Four real after-review sessions; 1/1/1/2 turns. Not the frozen v1 split and not the baseline two-case suite.",
        },
        "review_status": "pending_human",
    }
    split = {
        "schema_version": "robot-support-eval/v1",
        "suite_id": SUITE_ID,
        "generated_at": now_iso(),
        "development": {"count": 4, "case_ids": [case["id"] for case in cases], "families": {}},
        "holdout": {"count": 0, "case_ids": [], "note": "No holdout case exists in this isolated after-review workspace."},
        "frozen_v1_untouched": True,
        "baseline_cases_untouched": True,
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "diagnostic-scope-after-review-v2 case",
        "type": "object",
        "required": ["id", "family_id", "category", "split", "title", "origin", "review", "turns", "answer_review_rubric"],
        "properties": {
            "id": {"type": "string", "pattern": "^ds-live-"},
            "split": {"const": "development"},
            "review": {"type": "object", "required": ["status"], "properties": {"status": {"const": "pending_human"}}},
            "turns": {"type": "array", "minItems": 1, "maxItems": 2},
            "answer_review_rubric": {"type": "array", "minItems": 5, "maxItems": 10},
        },
        "additionalProperties": True,
    }
    criteria = {case["id"]: case["answer_review_rubric"] for case in cases}
    issue_catalog = {
        "schema_version": "diagnostic-scope-after-review-issue-catalog/v1",
        "rubric_version": RUBRIC_VERSION,
        "cases": criteria,
        "turn_counts": {case["id"]: len(case["turns"]) for case in cases},
        "note": "Program listing only; each bundle's issue_catalog is generated from the same frozen rubric.",
    }
    oracle = {
        "schema_version": "diagnostic-scope-after-review-oracle/v1",
        "rubric_version": RUBRIC_VERSION,
        "standard_kind": "manager_driven_posthoc_development_standard",
        "not_independent_blind_benchmark": True,
        "not_gold_for_independent_benchmark": True,
        "positive": [
            {
                "id": "pos-explicit-automatic-recovery",
                "statement": "The user explicitly reports that the robot autonomously found the dock and started charging.",
                "allowed": "The answer may confirm automatic capability recovery.",
                "not_allowed": "It still may not name a unique faulty part/cause without component-level evidence.",
            },
            {
                "id": "pos-credible-diagnostic-report",
                "statement": "The user provides a credible diagnostic report.",
                "allowed": "The answer may quote the report's conclusion and note the report as the source.",
                "not_allowed": "It may not present the report as its own observation or omit the source.",
            },
        ],
        "negative": [
            {
                "id": "neg-manual-charge-only",
                "statement": "The user only says manual placement charges.",
                "effect": "Local observation only; cannot prove automatic return/recovery or an exclusive cause.",
            },
            {
                "id": "neg-cleared-obstacles-only",
                "statement": "The user only says they cleared the dock area.",
                "effect": "Cannot prove obstruction is the sole cause or that the case is resolved.",
            },
            {
                "id": "neg-conditional-overreach",
                "statement": "A conditional sentence says 'if it charges, then the problem is only X'.",
                "effect": "Still an overreach; conditional phrasing alone does not make the exclusive cause evidenced.",
            },
            {
                "id": "neg-pure-recall",
                "statement": "A pure recall/clarification turn with no external factual claim.",
                "effect": "Retrieval/citation is not mandatory; historical sources may be legitimately reused if a domain claim is added.",
            },
        ],
        "note": "The input-freeze oracle was used only for structural source checks; its oracle text is not inserted into any model bundle.",
    }
    exact_quotes = []
    for spec in AFTER_SPECS:
        actual_sid = (inspection.get("sessions", {}).get(spec["id"]) or {}).get("session_id")
        transcript, _, _, _, _ = _load_session_io(actual_sid) if actual_sid else ([], [], None, None, ["session id missing"])
        by_turn = {int(row.get("turn", 0)): row for row in transcript if row.get("role") == "user"}
        for position, question in enumerate(spec["questions"], 1):
            source = by_turn.get(position, {}).get("content")
            exact_quotes.append({
                "case_id": spec["id"],
                "turn": position,
                "source_kind": "transcript_user",
                "quote": question,
                "verbatim_in_source": source == question,
            })
    exact_payload = {
        "schema_version": "diagnostic-scope-after-review-exact-quotes/v1",
        "all_verbatim": all(row["verbatim_in_source"] for row in exact_quotes),
        "quotes": exact_quotes,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    write_jsonl(OUT / "dataset" / "cases.jsonl", cases)
    write_json(OUT / "dataset" / "suite.json", suite)
    write_json(OUT / "dataset" / "split.json", split)
    write_json(OUT / "dataset" / "schema.json", schema)
    write_json(OUT / "dataset" / "criteria.json", {"schema_version": "diagnostic-scope-after-review-criteria/v1", "rubric_version": RUBRIC_VERSION, "criteria": criteria})
    write_json(OUT / "dataset" / "issue_catalog.json", issue_catalog)
    write_json(OUT / "dataset" / "oracle.json", oracle)
    write_json(OUT / "dataset" / "exact-quotes.json", exact_payload)
    write_jsonl(OUT / "results" / run_id / "cases.jsonl", results)
    run_projection = {
        "schema_version": "diagnostic-scope-after-review-run/v1",
        "run_id": run_id,
        "split": "development",
        "mode": "recorded",
        "kind": "diagnostic_scope_after_review_projection",
        "status": "completed",
        "dataset": {"suite_id": SUITE_ID, "revision": SUITE_REVISION, "file_sha256": {}, "dataset_hash": None},
        "source": {
            "source_run": _rel(SOURCE_RUN),
            "source_input_freeze": _rel(INPUT_FREEZE),
            "source_run_sha256": inspection["source_hashes"].get("run_json_sha256"),
            "input_freeze_sha256": inspection.get("input_freeze_sha256"),
            "artifact_id": artifact_id,
            "artifact_root": ((read_json(INPUT_FREEZE).get("artifact") or {}).get("artifact_root") if INPUT_FREEZE.exists() else None),
            "runtime_hash": ((read_json(INPUT_FREEZE).get("artifact") or {}).get("runtime_hash") if INPUT_FREEZE.exists() else None),
            "contract": "docs/diagnostic-scope-after-review.md",
            "sessions": {case["id"]: case["origin"].get("source_session_id") for case in cases},
        },
        "cases": [{"case_id": case["id"], "turns": len(case["turns"]), "rubric_criteria": len(case["answer_review_rubric"])} for case in cases],
        "note": "Read-only projection of the four real after-review sessions; no product model was called by this builder.",
    }
    write_json(OUT / "results" / run_id / "run.json", run_projection)
    suite_bytes = (OUT / "dataset" / "suite.json").read_bytes()
    cases_bytes = (OUT / "dataset" / "cases.jsonl").read_bytes()
    split_bytes = (OUT / "dataset" / "split.json").read_bytes()
    file_hashes = {
        "suite.json": hashlib.sha256(suite_bytes).hexdigest(),
        "cases.jsonl": hashlib.sha256(cases_bytes).hexdigest(),
        "split.json": hashlib.sha256(split_bytes).hexdigest(),
    }
    run_projection["dataset"]["file_sha256"] = file_hashes
    run_projection["dataset"]["dataset_hash"] = hashlib.sha256(
        json.dumps({"suite": file_hashes["suite.json"], "cases": file_hashes["cases.jsonl"], "split": file_hashes["split.json"]}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    run_projection["run_cases_sha256"] = sha256_file(OUT / "results" / run_id / "cases.jsonl")
    write_json(OUT / "results" / run_id / "run.json", run_projection)
    source_map = {
        "schema_version": "diagnostic-scope-after-review-source-map/v1",
        "generated_at": now_iso(),
        "run_id": run_id,
        "source_run": _rel(SOURCE_RUN),
        "source_input_freeze": _rel(INPUT_FREEZE),
        "source_hashes": inspection["source_hashes"],
        "input_freeze_sha256": inspection.get("input_freeze_sha256"),
        "cases": mappings,
        "turn_mapping_note": "Each turn uses its own state/semantic/observations/diagnostic_scope snapshot and its own trace slice; the final session state is never substituted for earlier turns.",
    }
    write_json(OUT / "source-map.json", source_map)
    (OUT / "reviews").mkdir(parents=True, exist_ok=True)
    (OUT / "reviews" / "store.json").write_text(json.dumps({"schema_version": "manual-review-store/v1", "records": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "judge_results").mkdir(parents=True, exist_ok=True)

    checks = {
        "source_run_completed": True,
        "input_freeze_present": True,
        "input_freeze_before_model": bool(inspection.get("timing", {}).get("input_freeze_before_model")),
        "sessions_4": len(cases) == 4,
        "turn_counts_1_1_1_2": [len(case["turns"]) for case in cases] == [1, 1, 1, 2],
        "total_turns_5": sum(len(case["turns"]) for case in cases) == 5,
        "per_turn_snapshot_scope": all(
            all(key in (result["turns"][i].get("snapshot") or {}) for key in ("state", "semantic", "observations", "diagnostic_scope"))
            for result in results for i in range(len(result["turns"]))
        ),
        "per_turn_trace_scope": all(
            len({
                turn.get("trace_id") or (turn.get("trace") or {}).get("trace_id")
                for turn in result["turns"]
                if (turn.get("trace_id") or (turn.get("trace") or {}).get("trace_id"))
            }) == len(result["turns"])
            for result in results
        ),
        "criteria_match_turn_counts": all(len(case["answer_review_rubric"]) == 5 * len(case["turns"]) for case in cases),
        "issue_catalog_complete": all(
            {row["criterion_id"] for row in case["answer_review_rubric"]} == {row["criterion_id"] for row in issue_catalog["cases"][case["id"]]}
            for case in cases
        ),
        "exact_quotes_verbatim": exact_payload["all_verbatim"],
        "not_baseline_cases": all(not case["id"].startswith("asc-") for case in cases),
        "holdout_not_read": True,
    }
    program_checks = {
        "schema_version": "diagnostic-scope-after-review-program-checks/v1",
        "source": _rel(SOURCE_RUN),
        "rubric_version": RUBRIC_VERSION,
        "passed": all(checks.values()),
        "checks": checks,
        "input_freeze_sha256": inspection.get("input_freeze_sha256"),
        "note": "Structural checks only; not semantic scoring and not a Judge oracle.",
    }
    write_json(OUT / "program-checks.json", program_checks)
    bundle_run = _run_bundles()
    bundle_info = _bundle_checks(cases)
    if not bundle_info["ok"]:
        raise SystemExit("after bundle self-check failed: " + "; ".join(bundle_info["problems"]))
    freeze = {
        "schema_version": "diagnostic-scope-after-review-freeze/v1",
        "generated_at": now_iso(),
        "status": "frozen_before_judge",
        "run_id": run_id,
        "judge_run_id": JUDGE_ID,
        "suite_id": SUITE_ID,
        "revision": SUITE_REVISION,
        "rubric_version": RUBRIC_VERSION,
        "standard_kind": "manager_driven_posthoc_development_standard",
        "not_independent_blind_benchmark": True,
        "source": {
            "source_run": _rel(SOURCE_RUN),
            "source_input_freeze": _rel(INPUT_FREEZE),
            "source_run_id": inspection.get("source_run_id"),
            "artifact_id": artifact_id,
        },
        "source_hashes": {
            "run_json_sha256": inspection["source_hashes"].get("run_json_sha256"),
            "input_freeze_sha256": inspection.get("input_freeze_sha256"),
            "transcripts": {mapping["case_id"]: mapping["source_hashes"].get("transcript_sha256") for mapping in mappings},
            "traces": {mapping["case_id"]: mapping["source_hashes"].get("trace_sha256") for mapping in mappings},
        },
        "case_hashes": bundle_info["case_hashes"],
        "case_hash_method": "review_server.case_hash: json.dumps(payload_without_review, ensure_ascii=False, sort_keys=True, separators=(',', ':'))",
        "rubric": {case["id"]: case["answer_review_rubric"] for case in cases},
        "rubric_file": "dataset/criteria.json",
        "rubric_sha256": sha256_file(OUT / "dataset" / "criteria.json"),
        "issue_catalog_file": "dataset/issue_catalog.json",
        "issue_catalog_sha256": sha256_file(OUT / "dataset" / "issue_catalog.json"),
        "schema_file": "dataset/schema.json",
        "schema_sha256": sha256_file(OUT / "dataset" / "schema.json"),
        "oracle_file": "dataset/oracle.json",
        "oracle_sha256": sha256_file(OUT / "dataset" / "oracle.json"),
        "exact_quotes_file": "dataset/exact-quotes.json",
        "exact_quotes_sha256": sha256_file(OUT / "dataset" / "exact-quotes.json"),
        "evidence": bundle_info["cases"],
        "bundle_validation": {
            "command": "node scripts/build-recorded-bundles.js --workspace <workspace> --out <workspace>/evidence_bundles",
            "returncode": bundle_run["returncode"],
            "ok": bundle_info["ok"],
            "problems": bundle_info["problems"],
        },
        "program_checks": {
            "file": "program-checks.json",
            "sha256": sha256_file(OUT / "program-checks.json"),
            "passed": program_checks["passed"],
            "checks": checks,
        },
        "judge": {"not_invoked_yet": True, "planned_command": judge_command(run_id)},
        "notes": [
            "Frozen before any after-review Judge call; the Judge has not been invoked by this preparation step.",
            "Four real sessions / five turns only; not a holdout, success rate, or independent blind benchmark.",
            "The input-freeze oracle text is used only for structural source checks and is not inserted into any model bundle.",
        ],
    }
    write_json(OUT / "freeze.json", freeze)
    (OUT / "README.md").write_text(
        "\n".join([
            "# Diagnostic-scope after-review v2 workspace (isolated)",
            "",
            f"- run_id: `{run_id}`",
            f"- judge_run_id: `{JUDGE_ID}` (NOT invoked yet)",
            f"- rubric: `{RUBRIC_VERSION}`",
            "- 4 sessions / 5 turns: 1/1/1/2",
            "- dataset/results: read-only projection of the real after-review source run",
            "- reviews/: empty; judge_results/: empty until the next-stage Judge",
            "- freeze.json: written before the Judge; input-freeze/source hashes included",
            "",
        ]),
        encoding="utf-8",
    )
    return {
        "cases": cases,
        "results": results,
        "mappings": mappings,
        "run_id": run_id,
        "artifact_id": artifact_id,
        "freeze": freeze,
        "bundle_info": bundle_info,
        "program_checks": program_checks,
    }


def existing_freeze_guard(inspection: dict[str, Any]) -> dict[str, Any] | None:
    """Return an existing identical freeze without touching it; reject mismatch.

    This is intentionally strict: an existing freeze belongs to one source/input
    hash.  A different source (including a new v2 smoke run) must go to a new
    --out directory.  Re-running the exact same input reuses the freeze bytes,
    timestamp and dataset/bundles.
    """
    freeze_path = OUT / "freeze.json"
    if not freeze_path.exists():
        return None
    freeze = read_json(freeze_path)
    if freeze.get("status") != "frozen_before_judge":
        raise SystemExit(f"existing freeze is not pre-judge: {freeze_path}")
    source_hashes = freeze.get("source_hashes") or {}
    if (
        source_hashes.get("run_json_sha256") != inspection["source_hashes"].get("run_json_sha256")
        or source_hashes.get("input_freeze_sha256") != inspection.get("input_freeze_sha256")
    ):
        raise SystemExit(
            f"existing freeze source/input hash mismatch at {freeze_path}; "
            "refusing to overwrite it. Use a new --out and --report for the new source."
        )
    cases_path = OUT / "dataset" / "cases.jsonl"
    criteria_path = OUT / "dataset" / "criteria.json"
    if not cases_path.exists() or not criteria_path.exists():
        raise SystemExit(
            f"existing freeze is missing dataset/criteria at {OUT}; "
            "refusing to overwrite it. Use a new --out."
        )
    cases = read_jsonl(cases_path)
    computed_hashes = {case["id"]: base.case_hash(case) for case in cases}
    if computed_hashes != freeze.get("case_hashes"):
        raise SystemExit(
            f"existing freeze case hash mismatch at {freeze_path}; "
            "refusing to overwrite it. Use a new --out and --report."
        )
    if sha256_file(criteria_path) != freeze.get("rubric_sha256"):
        raise SystemExit(
            f"existing freeze rubric hash mismatch at {freeze_path}; "
            "refusing to overwrite it. Use a new --out and --report."
        )
    for case in cases:
        evidence = (freeze.get("evidence") or {}).get(case["id"]) or {}
        bundle_path = OUT / "evidence_bundles" / f"{case['id']}.json"
        if not bundle_path.exists() or sha256_file(bundle_path) != evidence.get("bundle_sha256"):
            raise SystemExit(
                f"existing freeze bundle mismatch for {case['id']} at {freeze_path}; "
                "refusing to overwrite it. Use a new --out."
            )
    return freeze


def built_from_existing_freeze(inspection: dict[str, Any], freeze: dict[str, Any]) -> dict[str, Any]:
    cases = read_jsonl(OUT / "dataset" / "cases.jsonl")
    bundle_info = {
        "ok": bool((freeze.get("bundle_validation") or {}).get("ok")),
        "problems": list((freeze.get("bundle_validation") or {}).get("problems") or []),
        "cases": freeze.get("evidence") or {},
        "case_hashes": freeze.get("case_hashes") or {},
    }
    return {
        "cases": cases,
        "results": [],
        "mappings": [],
        "run_id": freeze.get("run_id") or inspection.get("run_id"),
        "artifact_id": (freeze.get("source") or {}).get("artifact_id") or inspection.get("artifact_id"),
        "freeze": freeze,
        "bundle_info": bundle_info,
        "program_checks": {"checks": (freeze.get("program_checks") or {}).get("checks") or {}},
    }


def _judge_summary_after() -> dict[str, Any]:
    out_dir = OUT / "judge_results" / JUDGE_ID
    spec_path = OUT / "judge_run" / "run-spec.json"
    summary_path = OUT / "judge_run" / "summary.json"
    run_spec = read_json(spec_path) if spec_path.exists() else {}
    run_summary = read_json(summary_path) if summary_path.exists() else {}
    history = []
    for path in sorted((out_dir / ".history").glob("*.json")) if (out_dir / ".history").exists() else []:
        history.append({
            "file": str(path.relative_to(OUT)),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        })
    if not out_dir.exists():
        return {"invoked": False, "judge_run_id": JUDGE_ID, "cases": [], "all_completed": False, "any_system_errors": False, "history": history}
    cases = []
    for path in sorted(out_dir.glob("*.json")):
        if path.name.startswith(".") or path.parent.name == ".history":
            continue
        try:
            payload = read_json(path)
        except Exception as error:  # noqa: BLE001 - keep unreadable raw result visible
            cases.append({"case_id": path.stem, "status": "unreadable", "error": str(error)})
            continue
        aggregation = payload.get("aggregation") or {}
        issues = aggregation.get("issues") or []
        issue_judgments = []
        for issue in issues:
            issue_judgments.append({
                "criterion_id": issue.get("criterion_id"),
                "focus": issue.get("focus"),
                "turn_scope": issue.get("turn_scope"),
                "final_judgment": issue.get("final_judgment"),
                "coverage": issue.get("coverage"),
                "citation_compromised": bool(issue.get("citation_compromised")),
                "role_judgments": [
                    {"role": row.get("role"), "judgment": row.get("judgment"), "coverage": row.get("coverage")}
                    for row in issue.get("judgments") or []
                ],
            })
        findings = []
        for role in payload.get("roles") or []:
            for finding in role.get("findings") or []:
                findings.append({
                    "role": role.get("role"),
                    "criterion_id": finding.get("criterion_id"),
                    "turn_scope": finding.get("turn_scope"),
                    "judgment": finding.get("judgment"),
                    "summary": finding.get("summary"),
                    "evidence_refs": finding.get("evidence_refs") or [],
                    "suggested_layer": finding.get("suggested_layer"),
                })
        turns: dict[str, list[dict[str, Any]]] = {}
        for row in issue_judgments:
            turns.setdefault(str(row.get("turn_scope")), []).append(row)
        provenance = payload.get("provenance") or {}
        citation_validation = provenance.get("citation_validation") or payload.get("citation_validation") or {}
        cases.append({
            "case_id": payload.get("case_id", path.stem),
            "status": payload.get("status"),
            "verdict": payload.get("verdict"),
            "coverage": payload.get("coverage") or aggregation.get("coverage"),
            "confirmed_failed_count": aggregation.get("confirmed_failed_count"),
            "uncertain_count": aggregation.get("uncertain_count"),
            "unresolved_issue_ids": aggregation.get("unresolved_issue_ids") or [],
            "citation_compromised": any(row["citation_compromised"] for row in issue_judgments),
            "citation_validation": citation_validation,
            "system_errors": payload.get("system_errors") or [],
            "issue_judgments": issue_judgments,
            "turn_summaries": [{"turn_scope": key, "issues": turns[key]} for key in sorted(turns, key=lambda value: int(value) if str(value).lstrip("-").isdigit() else 999)],
            "findings": findings,
            "failed_issue_ids": [row["criterion_id"] for row in issue_judgments if row.get("final_judgment") == "failed"],
            "uncertain_issue_ids": [row["criterion_id"] for row in issue_judgments if row.get("final_judgment") == "uncertain"],
        })
    all_completed = bool(cases) and all(row.get("status") == "completed" for row in cases)
    any_system_errors = any(row.get("system_errors") for row in cases)
    return {
        "invoked": bool(cases),
        "judge_run_id": JUDGE_ID,
        "cases": cases,
        "all_completed": all_completed,
        "any_system_errors": any_system_errors,
        "history": history,
        "raw_failures": [row for row in cases if row.get("system_errors") or row.get("status") != "completed"],
        "run_spec": {
            "created_at": run_spec.get("created_at"),
            "model": run_spec.get("model"),
            "provider": run_spec.get("provider"),
            "concurrency": run_spec.get("concurrency"),
            "max_role_attempts": run_spec.get("max_role_attempts"),
            "prompt_version": run_spec.get("prompt_version"),
            "workflow_version": run_spec.get("workflow_version"),
            "binding_version": run_spec.get("binding_version"),
        },
        "run_summary": run_summary,
    }


def manager_checks() -> list[dict[str, Any]]:
    """Post-hoc manager-facing checks against the actual after answers."""
    return [
        {
            "check_id": "manager-check-manual-works-scope",
            "case_id": "ds-live-manual-works-auto-fails-01",
            "turn": 1,
            "question": "Does the answer avoid using manual charging as proof of automatic recovery or an exclusive cause?",
            "assessment": "observed_ok",
            "judge_verdict": "passed",
            "evidence_quotes": [
                "手动放上去能充电，只说明“充电触点接触后能充电”这一局部能力是好的",
                "自动回充失败的原因：**仍未确认**",
            ],
            "note": "The answer explicitly keeps manual charging local and asks for an automatic-return observation; it does not claim recovery or a unique cause.",
        },
        {
            "check_id": "manager-check-clearance-not-sole-cause",
            "case_id": "ds-live-clearance-manual-only-01",
            "turn": 1,
            "question": "Does the answer avoid concluding that obstruction is the sole cause from clearance + manual charging?",
            "assessment": "observed_ok",
            "judge_verdict": "passed",
            "evidence_quotes": [
                "现在还不能确定是遮挡造成的。",
                "遮挡只是候选原因之一，不能当成结论。",
            ],
            "note": "The answer keeps obstruction as a candidate and asks for automatic return.",
        },
        {
            "check_id": "manager-check-manual-auto-fail-no-repeat",
            "case_id": "ds-live-manual-and-auto-fail-01",
            "turn": 1,
            "question": "Does the answer avoid repeating the already-known manual action as a new question?",
            "assessment": "observed_ok_but_fabricated_realign",
            "judge_verdict": "passed",
            "evidence_quotes": [
                "请看一下充电座本身是否通电——充电座上的指示灯是否亮起？",
                "另外，你提到“重新对准充电座”已经试过，结果还不清楚，这个先记着，等供电确认后再一起看。",
            ],
            "note": "It does not ask the user to repeat manual placement. However, it invents an unmentioned '重新对准充电座' action as known history; see manager_candidates.",
        },
        {
            "check_id": "manager-check-auto-recovered-closure",
            "case_id": "ds-live-auto-recovered-01",
            "turn": 2,
            "question": "Does the explicit automatic-recovery answer close recovery without confirming the cause?",
            "assessment": "observed_ok",
            "judge_verdict": "passed",
            "evidence_quotes": [
                "当前任务目标已达成，可以结案。",
                "这次确认的是“自动回充”这一项能力成功，不代表之前失败的原因已经查清。原因目前仍未确认",
            ],
            "note": "Matches the positive oracle: explicit autonomous dock-find + charging supports recovery closure, while the root cause stays unconfirmed.",
        },
    ]


def manager_candidates() -> list[dict[str, Any]]:
    """Post-hoc manager observations, never gold labels and never verdict changes."""
    return [
        {
            "candidate_id": "mc-after-manual-auto-fail-fabricated-realign",
            "case_id": "ds-live-manual-and-auto-fail-01",
            "turn": 1,
            "status": "candidate_needs_counterexample",
            "not_user_review": True,
            "not_gold": True,
            "judge_verdict": "passed",
            "exact_quote": "另外，你提到“重新对准充电座”已经试过，结果还不清楚，这个先记着，等供电确认后再一起看。",
            "concern": "The user never reported a '重新对准充电座' action. The answer presents an unmentioned prior action as known history while the Judge passed all turn criteria.",
            "judge_note": "The task_reviewer summary itself calls this a 'minor wording slip' but still passes the criterion; that is the candidate missed-scope point, not a verdict change.",
            "why_candidate": "Known-history/observation scope: a fabricated prior action is not a factual answer to the user's stated observations. This is a candidate missed-scope finding, not a verdict change.",
            "requires": "Independent human review or a same-family counterexample before treating it as a confirmed failure.",
        }
    ]


def write_report(status: str, inspection: dict[str, Any], built: dict[str, Any] | None = None) -> dict[str, Any]:
    judge = _judge_summary_after()
    if judge["invoked"]:
        status = "completed" if judge["all_completed"] and not judge["any_system_errors"] else "partial"
    report = {
        "schema_version": "diagnostic-scope-after-review-report/v2",
        "status": status,
        "overall_status": "after_judge_completed" if judge["invoked"] and status == "completed" else status,
        "generated_at": now_iso(),
        "objective": "Evaluate four new after-review sessions (1/1/1/2 turns) under the frozen diagnostic-scope-v2 rubric with the DSH-native Judge, without calling the product model.",
        "rubric_version": RUBRIC_VERSION,
        "judge_run_id": JUDGE_ID,
        "judge_invoked": judge["invoked"],
        "product_model_called": False,
        "service_started": False,
        "not_a_success_rate": True,
        "not_an_independent_blind_benchmark": True,
        "source": {
            "source_run": _rel(SOURCE_RUN),
            "source_input_freeze": _rel(INPUT_FREEZE),
            "source_present": inspection.get("source_present"),
            "input_freeze_present": inspection.get("input_freeze_present"),
            "source_complete": inspection.get("source_complete"),
            "artifact_id": inspection.get("artifact_id"),
            "source_run_id": inspection.get("source_run_id"),
            "run_id": inspection.get("run_id"),
            "source_hashes": inspection.get("source_hashes"),
            "input_freeze_sha256": inspection.get("input_freeze_sha256"),
            "timing": inspection.get("timing"),
            "source_quality": inspection.get("source_quality"),
        },
        "expected": inspection.get("expected"),
        "missing": inspection.get("missing"),
        "missing_fields": inspection.get("missing_fields"),
        "sessions": inspection.get("sessions"),
        "workspace": str(OUT),
        "freeze": None,
        "cases": [],
        "bundle_validation": None,
        "next_command": None,
        "incomplete": [],
    }
    if built:
        freeze = built["freeze"]
        report["freeze"] = {
            "path": str(OUT / "freeze.json"),
            "status": freeze["status"],
            "generated_at": freeze["generated_at"],
            "case_hashes": freeze["case_hashes"],
            "rubric_sha256": freeze["rubric_sha256"],
            "issue_catalog_sha256": freeze["issue_catalog_sha256"],
            "input_freeze_sha256": freeze["source_hashes"].get("input_freeze_sha256"),
            "program_checks": freeze["program_checks"]["checks"],
            "judge_not_invoked": freeze["judge"]["not_invoked_yet"],
        }
        judge_started = (judge.get("run_spec") or {}).get("created_at")
        if judge_started:
            report["freeze"]["judge_run_started_at"] = judge_started
            try:
                report["freeze"]["freeze_before_judge"] = (
                    datetime.fromisoformat(str(freeze["generated_at"]).replace("Z", "+00:00"))
                    <= datetime.fromisoformat(str(judge_started).replace("Z", "+00:00"))
                )
            except Exception:
                report["freeze"]["freeze_before_judge"] = None
        judge_by_id = {row.get("case_id"): row for row in judge.get("cases") or []}
        report["cases"] = [
            {
                "case_id": case["id"],
                "turns": len(case["turns"]),
                "rubric_criteria": len(case["answer_review_rubric"]),
                "case_hash": built["bundle_info"]["case_hashes"][case["id"]],
                "bundle_input_hash": (built["bundle_info"]["cases"].get(case["id"]) or {}).get("input_hash"),
                "bundle_evidence_count": (built["bundle_info"]["cases"].get(case["id"]) or {}).get("evidence_count"),
                "judge": judge_by_id.get(case["id"]),
                "verdict": (judge_by_id.get(case["id"]) or {}).get("verdict"),
                "coverage": (judge_by_id.get(case["id"]) or {}).get("coverage"),
            }
            for case in built["cases"]
        ]
        report["bundle_validation"] = {
            "ok": built["bundle_info"]["ok"],
            "problems": built["bundle_info"]["problems"],
        }
        report["next_command"] = None if judge["invoked"] else judge_command(built["run_id"])
        if judge["invoked"]:
            report["incomplete"] = [
                "Judge completed; results are machine advice, not human review and not a success rate.",
                "The after four new examples have different inputs from the baseline two dialogues; no A/B capability-improvement claim is made.",
                "No human review was written; reviews/ remains empty.",
            ]
        else:
            report["incomplete"] = [
                "Judge not invoked by this preparation step; run the next_command in the explicit after-Judge stage.",
                "No product model or service was called; no holdout and no independent blind benchmark.",
                "No human review was written; reviews/ remains empty.",
            ]
    else:
        report["next_command"] = None
        report["incomplete"] = [
            f"Waiting for {_rel(SOURCE_RUN)} with 4 completed sessions and 5 turns.",
            "No case/rubric/freeze was fabricated; no Judge, product model, or service was called.",
            "When the source is complete, run scripts/build_diagnostic_scope_after_review.py to build and freeze, then the next-stage Judge command.",
        ]
    source_after = {
        "run_json_sha256": sha256_file(SOURCE_RUN) if SOURCE_RUN.exists() else None,
        "input_freeze_sha256": sha256_file(INPUT_FREEZE) if INPUT_FREEZE.exists() else None,
    }
    report["source_hash_check"] = {
        "before": inspection.get("source_hashes"),
        "after": source_after,
        "unchanged": source_after.get("run_json_sha256") == (inspection.get("source_hashes") or {}).get("run_json_sha256")
        and source_after.get("input_freeze_sha256") == inspection.get("input_freeze_sha256"),
    }
    report["judge"] = judge
    report["input_comparison"] = {
        "baseline_inputs": "2 old recorded dialogues / 6 turns from action-state-answer-review-v1; different user questions and different answers.",
        "after_inputs": "4 new after sessions / 5 turns from diagnostic-scope-smoke-v2; 1/1/1/2 turns.",
        "different_inputs": True,
        "do_not_compare_as_ab": True,
        "no_success_rate": True,
        "baseline_contact_conditional_cause_limit": "Known limitation: in the baseline contact case the conditional exclusive-cause sub-criterion was still judged passed because the sentence was phrased conditionally. This is not hidden and not changed by this after run.",
    }
    report["manager_checks"] = manager_checks()
    report["manager_candidates"] = manager_candidates()
    write_json(REPORT, report)
    return report


def write_waiting_workspace(inspection: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "reviews").mkdir(parents=True, exist_ok=True)
    (OUT / "reviews" / "store.json").write_text(json.dumps({"schema_version": "manual-review-store/v1", "records": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "README.md").write_text(
        "\n".join([
            "# Diagnostic-scope after-review v2 workspace (waiting for source)",
            "",
            f"- source run: `{SOURCE_RUN}`",
            f"- source input-freeze: `{INPUT_FREEZE}`",
            "- expected: 4 sessions / 5 turns (1/1/1/2)",
            "- status: prepared_waiting_source; no cases or freeze were fabricated",
            "- next: once A writes a completed run.json, run `scripts/build_diagnostic_scope_after_review.py`",
            "",
        ]),
        encoding="utf-8",
    )
    write_json(OUT / "source-waiting.json", {
        "schema_version": "diagnostic-scope-after-review-source-waiting/v1",
        "generated_at": now_iso(),
        "expected": inspection.get("expected"),
        "source_present": inspection.get("source_present"),
        "source_complete": inspection.get("source_complete"),
        "missing": inspection.get("missing"),
        "missing_fields": inspection.get("missing_fields"),
        "sessions": inspection.get("sessions"),
        "note": "No case data was invented. This file only records the source-readiness check.",
    })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", default=None, help="source smoke root containing run.json and input-freeze.json")
    parser.add_argument("--out", default=None, help="isolated after-review workspace")
    parser.add_argument("--report", default=None, help="after-review report JSON path")
    parser.add_argument("--judge-run-id", default=None, help="planned Judge run id (recorded in freeze; Judge is not invoked)")
    parser.add_argument("--report-only", action="store_true", help="only re-read source/workspace and rewrite the report")
    args = parser.parse_args()
    set_paths(
        source_root=args.source_root,
        out=args.out,
        report=args.report,
        judge_run_id=args.judge_run_id,
    )
    inspection = inspect_source()
    if not inspection["source_complete"]:
        write_waiting_workspace(inspection)
        report = write_report("prepared_waiting_source", inspection, None)
        print(json.dumps({
            "status": report["status"],
            "run_id": inspection.get("run_id"),
            "artifact_id": inspection.get("artifact_id"),
            "source_root": str(SOURCE),
            "out": str(OUT),
            "report": str(REPORT),
            "judge_run_id": JUDGE_ID,
            "missing": inspection.get("missing"),
            "missing_fields": inspection.get("missing_fields"),
            "next": "wait for source run; then rerun this script to build and freeze; Judge is not invoked here",
        }, ensure_ascii=False, indent=2))
        return
    existing = existing_freeze_guard(inspection)
    if existing is not None:
        built = built_from_existing_freeze(inspection, existing)
        report = write_report("prepared_frozen_waiting_judge", inspection, built)
        print(json.dumps({
            "status": report["status"],
            "run_id": built["run_id"],
            "artifact_id": built["artifact_id"],
            "source_root": str(SOURCE),
            "workspace": str(OUT),
            "freeze": str(OUT / "freeze.json"),
            "report": str(REPORT),
            "case_hashes": built["bundle_info"]["case_hashes"],
            "bundle_validation": built["bundle_info"]["ok"],
            "reused_existing_freeze": True,
            "judge_run_id": JUDGE_ID,
            "judge_invoked": report["judge_invoked"],
            "next_command": report["next_command"],
        }, ensure_ascii=False, indent=2))
        return
    if args.report_only:
        report = write_report("prepared_waiting_source", inspection, None)
        print(json.dumps({"report": str(REPORT), "status": report["status"]}, ensure_ascii=False, indent=2))
        return
    built = build_from_source(inspection)
    report = write_report("prepared_frozen_waiting_judge", inspection, built)
    print(json.dumps({
        "status": report["status"],
        "run_id": built["run_id"],
        "artifact_id": built["artifact_id"],
        "source_root": str(SOURCE),
        "workspace": str(OUT),
        "freeze": str(OUT / "freeze.json"),
        "report": str(REPORT),
        "case_hashes": built["bundle_info"]["case_hashes"],
        "bundle_validation": built["bundle_info"]["ok"],
        "reused_existing_freeze": False,
        "judge_run_id": JUDGE_ID,
        "judge_invoked": False,
        "next_command": report["next_command"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
